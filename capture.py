#!/usr/bin/env python3
"""capture.py —— TH1：V4L2 mainpath + DMABUF 池 + lease + 四源触发状态机 + evdev 触控
（§10.3 交付模块）

契约（主基准 §5）：
  · 本模块是 **`QBUF` 权的唯一持有者**，也是 `seq` 的唯一分配者（单调递增，下游不得重编号）；
  · 消费者只持 **lease**；`release()` 后引用计数归零才 QBUF 回池；
  · **任何模块不得 `close(dmabuf_fd)`**；
  · `stream_on_once()` 在**进程全生命周期内只允许成功一次**（O26：软复位不 STREAMOFF、不重建）；
  · 无可用缓冲时丢**最旧的未签发 lease** 帧；**禁止强制回收仍在使用的 lease**。

状态机（§5）：
  RSM  = BOOT_CHECK → RUN_DEMO ⇄ MIPI_PREVIEW_ONLY → SHUTDOWN / FAULT_EXIT
  FSM  = IDLE → DEBOUNCE → HQ_BUSY → IDLE；`HQ_BUSY` 期间触发**一律丢弃并计数**（不排队、无 pending 位）

设备 I/O 与 NPU/RGA/DRM 全部在同目录 `_starlight_native` 扩展内完成；本文件只做编排与状态机。
"""

from __future__ import annotations

import os
import select
import struct
import time
from typing import Any, Callable, Dict, List, Optional

import _starlight_native as _n

import log as starlog

# ── §5 契约的 RSM / 触发 FSM 取值 ──────────────────────────────────────
RSM_BOOT_CHECK = "BOOT_CHECK"
RSM_RUN_DEMO = "RUN_DEMO"
RSM_MIPI_PREVIEW_ONLY = "MIPI_PREVIEW_ONLY"
RSM_SHUTDOWN = "SHUTDOWN"
RSM_FAULT_EXIT = "FAULT_EXIT"
RSM_ALL = (RSM_BOOT_CHECK, RSM_RUN_DEMO, RSM_MIPI_PREVIEW_ONLY, RSM_SHUTDOWN, RSM_FAULT_EXIT)

FSM_IDLE = "IDLE"
FSM_DEBOUNCE = "DEBOUNCE"
FSM_HQ_BUSY = "HQ_BUSY"
FSM_ALL = (FSM_IDLE, FSM_DEBOUNCE, FSM_HQ_BUSY)

# §5 的合法 RSM 迁移（非法迁移一律 E_STATE，禁止绕过状态机直接改状态）
RSM_LEGAL = {
    RSM_BOOT_CHECK: (RSM_RUN_DEMO, RSM_MIPI_PREVIEW_ONLY, RSM_FAULT_EXIT),
    RSM_RUN_DEMO: (RSM_MIPI_PREVIEW_ONLY, RSM_SHUTDOWN),
    RSM_MIPI_PREVIEW_ONLY: (RSM_RUN_DEMO, RSM_SHUTDOWN),
    RSM_SHUTDOWN: (),
    RSM_FAULT_EXIT: (),
}

E_STATE = -5
E_TIMEOUT = -6

# 触发源（§5 TriggerEvent.source）
SRC_TOUCH, SRC_GPIO, SRC_TIMER, SRC_UART = "TOUCH", "GPIO", "TIMER", "UART"

# MIPI 按钮区 3 键（§3.4）；命中区下限由 `ui.hit_area_min_px` 给出
BTN_TRIGGER_HQ = "trigger_hq"
BTN_SAVE = "save"
BTN_RESET_EXIT = "reset_exit"


class CaptureError(Exception):
    pass


class TouchReader:
    """evdev 触控读取（`/dev/input/eventN`，由 config/参数给出）。无设备时降级为不可用。"""

    def __init__(self, path: str, logger: starlog.Logger):
        self.path = path
        self.logger = logger
        self.fd: Optional[int] = None
        self.name = ""

    def open(self) -> bool:
        try:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            self.fd = None
            return False
        try:
            with open(f"/sys/class/input/{os.path.basename(self.path)}/device/name") as f:
                self.name = f.read().strip()
        except OSError:
            self.name = "unknown"
        return True

    def read_events(self) -> List[tuple]:
        """返回 [(type, code, value)]；每事件 16 字节（timeval 8 + type/code/value 各 2/2/4）。"""
        if self.fd is None:
            return []
        out = []
        try:
            r, _, _ = select.select([self.fd], [], [], 0)
            if not r:
                return []
            data = os.read(self.fd, 16 * 64)
        except OSError:
            return []
        for off in range(0, len(data) - 15, 16):
            _sec, _usec, etype, code, value = struct.unpack_from("llHHi", data, off)
            out.append((etype, code, value))
        return out

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Capture:
    """TH1 采集线程主体（单线程调用；不做内部加锁）。"""

    def __init__(self, cfg: Any, logger: starlog.Logger, on_trigger: Optional[Callable[[dict], None]] = None):
        self.cfg = cfg
        self.log = logger
        self.on_trigger = on_trigger
        self.rsm = RSM_BOOT_CHECK
        self.fsm = FSM_IDLE
        self.discarded_triggers = 0
        self.debounce_deadline_ns = 0
        self.seq_src_next = 0
        self.touch: Optional[TouchReader] = None
        self._inited = False
        self._reset_press_ns: Optional[int] = None

    # ── 生命周期 ───────────────────────────────────────────────────────
    def init(self) -> int:
        node = self.cfg.require_final("camera.video_node")
        rc = _n.capture.init(node, 4, 1920, 1080)
        if rc != 0:
            self._transition(RSM_FAULT_EXIT, reason="v4l2_init_failed")
            return rc
        self._inited = True
        return 0

    def stream_on_once(self) -> int:
        rc = _n.capture.stream_on_once()
        self.log.event("capture", "stream_on_once",
                       result="ok" if rc == 0 else "fail",
                       video_node=self.cfg.require_final("camera.video_node"),
                       code=starlog.code_name(rc))
        return rc

    def shutdown(self) -> None:
        """进程终止路径：STREAMOFF + 释放。**软复位不得走这里**（O26）。"""
        if self.touch:
            self.touch.close()
        if self._inited:
            _n.capture.deinit()
            self._inited = False

    def soft_reset(self) -> int:
        """软复位（O26）：只停发新 lease + 有界清空在途 lease，**不 STREAMOFF、不释放池**。"""
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if _n.capture.counters()["outstanding_leases"] == 0:
                break
            time.sleep(0.005)
        if _n.capture.counters()["outstanding_leases"] != 0:
            return E_TIMEOUT
        self.fsm = FSM_IDLE
        self.debounce_deadline_ns = 0
        self._transition(RSM_BOOT_CHECK, reason="soft_reset")
        return 0

    # ── 状态机 ─────────────────────────────────────────────────────────
    def _transition(self, to: str, reason: str) -> int:
        if to not in RSM_ALL:
            raise CaptureError(f"未知 RSM 状态 {to}")
        if to not in RSM_LEGAL[self.rsm] and to != self.rsm:
            return E_STATE
        if to == self.rsm:
            return 0
        frm, self.rsm = self.rsm, to
        # §4 的必带字段名是 `from`（Python 关键字，只能用 **{} 传）
        self.log.event("main", "rsm_transition", **{"from": frm, "to": to, "reason": reason},
                       rsm=to, fsm=self.fsm)
        return 0

    def set_rsm(self, to: str, reason: str) -> int:
        return self._transition(to, reason)

    def submit_trigger(self, source: str) -> int:
        """四源共用的触发入口。返回 `seq_src`（>0 表示受理），0 表示丢弃。"""
        if source not in (SRC_TOUCH, SRC_GPIO, SRC_TIMER, SRC_UART):
            raise CaptureError(f"未知触发源 {source}")
        if self.rsm not in (RSM_RUN_DEMO, RSM_MIPI_PREVIEW_ONLY):
            self._discard(source, reason="rsm_not_accepting")
            return 0
        if self.fsm == FSM_HQ_BUSY:
            self._discard(source, reason="hq_busy")      # §5：一律丢弃、不排队、无 pending 位
            return 0
        if self.fsm == FSM_DEBOUNCE:
            return 0                                     # 消抖窗内静默忽略（不计入丢弃）
        self.fsm = FSM_DEBOUNCE
        self.debounce_deadline_ns = time.monotonic_ns() + int(
            self.cfg.require_final("trigger.debounce_ms")) * 1_000_000
        return 0  # 受理发生在消抖期满（poll 里）

    def _discard(self, source: str, reason: str) -> None:
        self.discarded_triggers += 1
        self.log.event("capture", "trigger_discard", source=source, seq_src=0,
                       discarded_triggers=self.discarded_triggers,
                       rsm=self.rsm, fsm=self.fsm, reason=reason)

    def poll_trigger(self) -> Optional[dict]:
        """消抖期满则分配 `seq_src` 并投递触发事件（返回事件字典）。"""
        if self.fsm != FSM_DEBOUNCE:
            return None
        if time.monotonic_ns() < self.debounce_deadline_ns:
            return None
        self.seq_src_next += 1
        self.fsm = FSM_HQ_BUSY
        ev = {"seq_src": self.seq_src_next, "source": SRC_TOUCH,
              "discarded_triggers": self.discarded_triggers}
        self.log.event("capture", "trigger_accept", source=SRC_TOUCH, seq_src=self.seq_src_next,
                       discarded_triggers=self.discarded_triggers, rsm=self.rsm, fsm=self.fsm)
        if self.on_trigger:
            self.on_trigger(ev)
        return ev

    def hq_done(self) -> None:
        """TH3 完成（M2 内 = 弹层演示结束）后回到 IDLE。"""
        if self.fsm == FSM_HQ_BUSY:
            self.fsm = FSM_IDLE

    # ── 帧与 lease ─────────────────────────────────────────────────────
    def acquire(self, consumers: int = 1, timeout_ms: int = 1000) -> Optional[dict]:
        return _n.capture.acquire(consumers, timeout_ms)

    def release(self, lease_id: int) -> int:
        return int(_n.capture.release(lease_id))

    def frame_buffer(self, index: int):
        return _n.capture.buffer(index)

    def counters(self) -> Dict[str, Any]:
        c = dict(_n.capture.counters())
        c["discarded_triggers"] = self.discarded_triggers
        c["rsm"] = self.rsm
        c["fsm"] = self.fsm
        return c


def main() -> int:
    """自检：init → stream_on_once（并验证第二次被拒）→ 取 30 帧 → 软复位 → 退出。"""
    import config as starlight_config

    cfg = starlight_config.load()
    logger = starlog.from_config(cfg)
    cap = Capture(cfg, logger)

    rc = cap.init()
    if rc != 0:
        print(f"FAIL: init rc={rc}")
        return 1
    rc = cap.stream_on_once()
    if rc != 0:
        print(f"FAIL: stream_on_once rc={rc}")
        cap.shutdown()
        return 1
    again = cap.stream_on_once()
    print(f"第二次 stream_on_once = {again}（期望 {E_STATE}）")

    ok = 0
    for _ in range(30):
        f = cap.acquire(1, 1000)
        if f is None:
            break
        assert f["w"] == 1920 and f["h"] == 1080, f"尺寸异常 {f['w']}x{f['h']}"
        cap.release(f["lease_id"])
        ok += 1
    c = cap.counters()
    print(f"取帧 {ok}/30  counters={c}")
    print(f"stream_on_count = {_n.capture.stream_on_count()}（契约：恒为 1）")
    rr = cap.soft_reset()
    print(f"soft_reset rc={rr}；复位后 stream_on_count = {_n.capture.stream_on_count()}")
    cap.shutdown()
    return 0 if (ok == 30 and again == E_STATE and _n.capture.stream_on_count() == 1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
