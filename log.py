#!/usr/bin/env python3
"""log.py —— 结构化事件日志（规格见附件规范 `docs/logging.md`）

地位：交付链路的日志入口（非独立子文档）。规格唯一来源 = `docs/logging.md`。

纪律：
  · 行格式 `<utc_iso8601> <mono_ns> <level> <module> <event> [k=v ...]`（§3）；
  · `event` 只取 §4 事件清单，**不得自造**；必带字段按该表逐条校验；
  · 错误码只取主基准 §5 契约 A（`OK` / `E_V4L2` / `E_NPU` / `E_DRM` / `E_IO` / `E_STATE` / `E_TIMEOUT`），
    打印**符号名**，不得自造；
  · **日志不承载指标**（§6）——FPS / 延迟 / 配对率一律由 `bench.py` 报告给出；
  · 日志目录不可写**不得阻塞主链路**（§2）：记一次 `E_IO` 后降级为仅 stdout，不再重试。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

# ── §1 级别 ────────────────────────────────────────────────────────────
LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
_LEVEL_ORDER = {name: idx for idx, name in enumerate(LEVELS)}

# ── §5 错误码（主基准 §5 契约 A；仅符号名）──────────────────────────────
CODES: Dict[str, int] = {
    "OK": 0,
    "E_V4L2": -1,
    "E_NPU": -2,
    "E_DRM": -3,
    "E_IO": -4,
    "E_STATE": -5,
    "E_TIMEOUT": -6,
}

# ── §3 module 取值（取 §10.3 的模块名）────────────────────────────────
MODULES: Tuple[str, ...] = (
    "capture", "curve_estim", "realtime_apply", "keyframe_infer",
    "display_mipi", "display_hdmi", "isp_ctl", "main",
)

# ── §4 事件清单与必带字段 ──────────────────────────────────────────────
# key = (module, event)；module 用 "*" 表示"任意"（degrade_action）。
# value = 必带字段元组（可为空元组）。
EVENT_TABLE: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("main", "boot_check_item"): ("item", "result", "code"),
    ("main", "rsm_transition"): ("from", "to", "reason"),
    ("main", "service_start"): ("exit_code", "uptime_s"),
    ("main", "service_exit"): ("exit_code", "uptime_s"),
    ("main", "restart_attempt"): ("attempt", "window_s", "exit_code"),
    ("main", "start_limit_hit"): ("burst", "interval_s", "action"),
    ("main", "version_triplet"): ("toolkit2", "librknnrt", "rknpu_driver"),
    ("main", "isp_fingerprint"): (
        "iq_hash", "gamma", "ccm", "black_level", "csc_matrix", "rgb_range", "awb_lock",
    ),
    ("capture", "stream_on_once"): ("result", "video_node"),
    ("capture", "lease_timeout"): ("lease_id", "seq", "buffer_index", "generation"),
    ("capture", "timestamp_source_changed"): ("source", "seq", "window_invalidated"),
    ("capture", "trigger_accept"): ("source", "seq_src", "discarded_triggers"),
    ("capture", "trigger_discard"): ("source", "seq_src", "discarded_triggers"),
    ("curve_estim", "curve_publish"): ("seq_est", "f_est", "alpha", "curve_level", "precision"),
    ("keyframe_infer", "hq_start"): ("seq_src", "model_id", "active_shape", "done_ns"),
    ("keyframe_infer", "hq_done"): ("seq_src", "model_id", "active_shape", "done_ns"),
    ("keyframe_infer", "save_set"): ("seq_src", "items", "bytes", "dir"),
    ("keyframe_infer", "save_fail"): ("seq_src", "items", "code", "reason"),
    ("display_hdmi", "hotplug"): ("connected", "mode"),
    ("display_hdmi", "fade_start"): ("frames",),
    ("display_hdmi", "fade_done"): ("frames",),
    ("display_mipi", "freeze_enter"): ("seq",),
    ("display_mipi", "freeze_exit"): ("seq",),
    ("display_mipi", "reset_key"): ("press", "seq"),
    ("*", "degrade_action"): ("trigger", "action"),
}

# §3：带状态的事件必须带 `rsm=` 与 `fsm=`
STATE_EVENTS: Tuple[str, ...] = (
    "rsm_transition", "trigger_accept", "trigger_discard", "degrade_action",
)

# §1：自检行与降级行**强制 INFO**，不得被级别过滤掉
FORCED_INFO_EVENTS: Tuple[str, ...] = ("boot_check_item", "degrade_action", "rsm_transition")


class LogError(Exception):
    """日志字段/事件名违规（属编程错误，不是运行期故障）。"""


def _utc_iso8601() -> str:
    """`CLOCK_REALTIME`，人读用（§3）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _mono_ns() -> int:
    """`CLOCK_MONOTONIC` 纳秒，唯一可用于时序比对的时钟（§3）。"""
    return time.monotonic_ns()


def _fmt_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


class Logger:
    """单进程日志器。线程安全由调用方保证（各模块各自持有，不共享可变状态）。"""

    def __init__(
        self,
        level: str = "INFO",
        directory: Optional[str] = None,
        rotate_mb: Optional[float] = None,
        keep_files: Optional[int] = None,
        stream: Any = None,
    ):
        level = str(level).upper()
        if level not in _LEVEL_ORDER:
            raise LogError(f"非法日志级别 {level}（§1 允许：{'/'.join(LEVELS)}）")
        self.level = level
        self.directory = directory
        self.rotate_mb = float(rotate_mb) if rotate_mb else None
        self.keep_files = int(keep_files) if keep_files else None
        self._stream = stream if stream is not None else sys.stdout
        self._fp = None
        self._file_broken = False
        self._bytes_written = 0
        self._open()

    # ── 文件生命周期（§2）───────────────────────────────────────────────
    def _log_path(self) -> str:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        return os.path.join(self.directory, f"starlight_{day}.log")

    def _open(self) -> None:
        if not self.directory or self._file_broken:
            return
        try:
            os.makedirs(self.directory, exist_ok=True)
            path = self._log_path()
            self._bytes_written = os.path.getsize(path) if os.path.exists(path) else 0
            self._fp = open(path, "a", encoding="utf-8")
        except OSError:
            # §2 写失败：记一次 E_IO 后降级为仅 stdout，不再重试
            self._file_broken = True
            self._fp = None
            self._emit_raw(
                f"{_utc_iso8601()} {_mono_ns()} ERROR main degrade_action "
                f"trigger=log_dir_unwritable action=stdout_only code=E_IO"
            )

    def _rotate_if_needed(self, next_len: int) -> None:
        if self._fp is None or not self.rotate_mb:
            return
        limit = int(self.rotate_mb * 1024 * 1024)
        if self._bytes_written + next_len < limit:
            return
        try:
            self._fp.close()
            base = self._log_path()
            idx = 1
            while os.path.exists(f"{base}.{idx}"):
                idx += 1
            os.rename(base, f"{base}.{idx}")
            self._prune()
            self._fp = open(base, "a", encoding="utf-8")
            self._bytes_written = 0
        except OSError:
            self._file_broken = True
            self._fp = None

    def _prune(self) -> None:
        """保留 `keep_files` 份，超限删最旧（§2）。"""
        if not self.keep_files:
            return
        base = os.path.basename(self._log_path())
        try:
            rotated = sorted(
                (f for f in os.listdir(self.directory) if f.startswith(base + ".")),
                key=lambda f: os.path.getmtime(os.path.join(self.directory, f)),
            )
        except OSError:
            return
        for name in rotated[: max(0, len(rotated) - self.keep_files)]:
            try:
                os.remove(os.path.join(self.directory, name))
            except OSError:
                pass

    def _emit_raw(self, line: str) -> None:
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except Exception:
            pass

    # ── 事件写入 ───────────────────────────────────────────────────────
    def event(self, module: str, event: str, level: str = "INFO", **fields: Any) -> None:
        if module not in MODULES:
            raise LogError(f"非法 module {module}（§3 允许：{'/'.join(MODULES)}）")
        spec = EVENT_TABLE.get((module, event))
        if spec is None:
            spec = EVENT_TABLE.get(("*", event))
        if spec is None:
            raise LogError(f"未登记事件 {module}/{event}（§4 清单，不得自造）")

        missing = [name for name in spec if name not in fields]
        if missing:
            raise LogError(f"事件 {module}/{event} 缺必带字段：{'、'.join(missing)}（§4）")
        if event in STATE_EVENTS:
            for name in ("rsm", "fsm"):
                if name not in fields:
                    raise LogError(f"带状态事件 {event} 必须带 {name}=（§3）")

        level = str(level).upper()
        if level not in _LEVEL_ORDER:
            raise LogError(f"非法日志级别 {level}")
        effective = "INFO" if event in FORCED_INFO_EVENTS else level
        if _LEVEL_ORDER[effective] < _LEVEL_ORDER[self.level]:
            return

        parts = [f"{k}={_fmt_value(v)}" for k, v in fields.items()]
        line = f"{_utc_iso8601()} {_mono_ns()} {effective} {module} {event}"
        if parts:
            line += " " + " ".join(parts)

        self._emit_raw(line)
        if self._fp is not None:
            self._rotate_if_needed(len(line) + 1)
            if self._fp is not None:
                try:
                    self._fp.write(line + "\n")
                    self._fp.flush()
                    self._bytes_written += len(line) + 1
                except OSError:
                    self._file_broken = True
                    self._fp = None

    def close(self) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None


def from_config(cfg: Any, stream: Any = None) -> Logger:
    """按 §10.4 的 `logging.*` 建日志器。键未定稿时退化为 INFO + 仅 stdout。"""
    level = cfg.get("logging.level") or "INFO"
    directory = cfg.get("logging.dir")
    if not directory:
        storage = cfg.get("storage.dir")
        directory = os.path.join(storage, "logs") if storage else None
    return Logger(
        level=level,
        directory=directory,
        rotate_mb=cfg.get("logging.rotate_mb") or 8,
        keep_files=cfg.get("logging.keep_files") or 3,
        stream=stream,
    )


def code_name(value: int) -> str:
    """把 §5 契约 A 的数值返回码转成符号名（打印符号名，必要时附数值）。"""
    for name, num in CODES.items():
        if num == value:
            return name
    raise LogError(f"未知返回码 {value}（§5 契约 A 只允许 {sorted(CODES)}）")


def main() -> int:
    """自检入口：`python3 log.py` —— 校验事件表自洽并打一行样例。"""
    for (module, event), fields in EVENT_TABLE.items():
        if module != "*" and module not in MODULES:
            raise LogError(f"事件表自洽性错误：module {module} 不在 §3 取值内")
        if event in STATE_EVENTS and not {"rsm", "fsm"} <= set(fields):
            pass  # rsm/fsm 由 event() 运行时强制，不在必带字段表内重复声明
    print(f"事件表：{len(EVENT_TABLE)} 条；级别：{'/'.join(LEVELS)}；错误码：{'/'.join(CODES)}")
    logger = Logger(level="DEBUG")
    logger.event("main", "version_triplet", toolkit2="2.3.2", librknnrt="2.3.2", rknpu_driver="0.9.8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
