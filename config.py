#!/usr/bin/env python3
"""config.py —— 运行期配置加载与键表白名单校验（§10.4 的落盘载体读取）

地位：交付链路的配置入口（非独立子文档）。取值唯一来源 = 仓库根 `config.yaml`，
      键名唯一来源 = 主基准 §10.4「config.yaml 键表」。

纪律（规则 `20-contracts.md` §1）：
  · **表外键禁止**——本模块对 `config.yaml` 逐键做白名单校验，出现表外键即报错；
  · 键不存在即未定稿 ⇒ 缺失键同样报错（不得静默用默认值顶替）；
  · 空值（`null`）= 未定稿，本模块**不**在加载期拒绝（M2 键按阶段逐步落值），
    由调用方通过 `require_final()` 在用到该键时显式断言。

本模块不写 config、不做现场修改（§10.4：换值 = 改文件后重启）。
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

# ── 键表白名单（主基准 §10.4；与 config.yaml 逐键一致）────────────────────
# 说明：`camera.gain_*` 是 §10.4 的**通配键名**（子键名随 M1 的 `v4l2-ctl -L` 实测定义），
#       故以 `*` 结尾者按前缀匹配；其余为精确键名。
KEY_TABLE: Tuple[str, ...] = (
    # 硬件
    "board.model", "board.cam_port", "board.cam_lanes", "board.overlay",
    # 采集
    "camera.video_node", "camera.rkisp_node", "camera.mainpath_only",
    "camera.mode_class", "camera.fps", "camera.vblank_lines", "camera.exposure_us", "camera.gain_*",
    # ISP
    "isp.presets", "isp.fingerprint", "isp.csc_matrix", "isp.rgb_range",
    "isp.nr_3d_ref_frames",
    # 图像
    "rga.core_mask",
    # 显示
    "display.screen_plugin", "display.dsi_connector", "display.hdmi_connector",
    "display.mipi_canvas", "display.hdmi_canvas", "display.panel_refresh_hz",
    "display.mode_htotal", "display.mode_vtotal", "display.dsi_lane_rate",
    "display.hdmi_mode_selection",
    # 链路
    "pipeline.curve_level", "pipeline.curve_geometry", "pipeline.curve_field_precision",
    "pipeline.apply_scope", "pipeline.curve_upsample", "pipeline.est_freq_hz",
    "pipeline.ema_alpha",
    # HQ（**v3.85 裁定 O82**：`hq.hold_policy` / `hq.fade_ms` / `hq.mipi_preview_ms` /
    #     `hq.mipi_preview_rect` **四键取消**——"替换保持 / 淡入 / 弹层"对象消失，见 §3.5、§10.4）
    "hq.model_id", "hq.shape_ladder", "hq.active_shape", "hq.pad_crop_policy",
    # 触发（**v3.85 裁定 O82**：`trigger.sources` / `trigger.interval_s` **两键取消**，
    #     触发源收敛为单源「指针点击」⇒ 本域仅剩 `debounce_ms`）
    "trigger.debounce_ms",
    # UI
    "ui.button_layout", "ui.hit_area_min_px", "ui.reset_hold_ms",
    "ui.osd_metrics_px", "ui.osd_status_row_px", "ui.osd_plot_px",
    "ui.osd_status_fields", "ui.osd_stats_decimate", "ui.osd_stats_hz",
    "ui.osd_font_px",
    # UI（**v3.85 裁定 O79 / O81 新增 2 键**：全屏 HQ 视图兜底退出 / 指针输入鼠标 evdev 节点）
    "ui.fullscreen_timeout_ms", "ui.pointer_mouse_node",
    # 计量
    "metrics.window_frames", "metrics.timestamp_domain", "metrics.timestamp_source",
    "metrics.pair_by", "metrics.report_breakdown", "metrics.idle_link_baseline",
    # 存储（**v3.86 裁定 O102**：`storage.*` 六键随「存图」功能整体取消而移除，见 §10.4）
    # 模型
    "model.zde_weight_hash", "model.zde_output_channels", "model.zde_output_layout",
    "model.zde_output_dims", "model.zde_ft_version", "model.hq_fallback_id",
    # 运行时
    "runtime.toolkit2_ver", "runtime.librknnrt_ver", "runtime.rknpu_driver_ver",
    # 工程
    "logging.level", "logging.dir", "logging.rotate_mb", "logging.keep_files",
)

# `pipeline.curve_geometry` 是 §10.4 明列的**子表**（v3.29 裁定 O34-②：「三档 shape 表」由
# `pipeline.curve_level` 取值域 + `pipeline.curve_geometry` 的 effective/pad_to 子表承载）。
# 其子键名不在 §10.4 逐条列出 ⇒ 以 `config.yaml` 已落盘的 5 项为准，新增须先回写 §10.4。
CURVE_GEOMETRY_CHILDREN: Tuple[str, ...] = (
    "effective", "pad_to", "pad_fill", "pad_mechanism", "output_take",
)

CONTAINER_KEYS: Tuple[str, ...] = (
    "board", "camera", "isp", "rga", "display", "pipeline", "hq", "trigger",
    "ui", "metrics", "model", "runtime", "logging",   # **（O102）** 原 `storage` 容器随存图取消而移除
)

# 不下探的子树（其内部结构由 §10.4 的子表口径承载，非独立键名）
OPAQUE_SUBTREES: Tuple[str, ...] = (
    "pipeline.curve_geometry",   # §10.4「三档 shape 表」：effective / pad_to 子表（v3.29 裁定 O34-②）
    "pipeline.apply_scope",      # §10.4：显示 = 窗分辨率 / 存盘 = 满幅一帧
    "ui.button_layout",          # §10.4：2 键单行、行高 96 px、各 512 px（v3.86 裁定 O101）
)

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


class ConfigError(Exception):
    """配置加载或校验失败（返回码纪律见 §5 契约 A：本类错误在调用方映射为 E_IO / E_STATE）。"""


def _is_wildcard(key: str) -> bool:
    return key.endswith("*")


def _wildcard_prefix(key: str) -> str:
    return key[:-1]  # 去掉末尾的 `*`，如 "camera.gain_*" -> "camera.gain_"


def _leaf_paths(
    tree: Dict[str, Any], prefix: str = "", opaque: Tuple[str, ...] = ()
) -> Iterable[Tuple[str, Any]]:
    """深度优先产出 (点号路径, 值)；容器节点本身不产出。

    `opaque` 内的路径**不再下探**（其子树是 §10.4 明列的子表，子结构由该表承载），
    例如 `pipeline.curve_geometry`（含 `effective` / `pad_to` 两张 shape→尺寸映射表）。
    """
    for name, value in tree.items():
        path = f"{prefix}{name}"
        if isinstance(value, dict) and value and path not in opaque:
            yield from _leaf_paths(value, path + ".", opaque)
        else:
            yield path, value


def _validate_leaf(path: str, value: Any) -> None:
    for opaque in OPAQUE_SUBTREES:
        if path.startswith(opaque + "."):
            return   # 位于不下探子表内 ⇒ 由该子表口径承载，不在此校验
    for key in KEY_TABLE:
        if _is_wildcard(key):
            if path.startswith(_wildcard_prefix(key)):
                return
        elif path == key:
            return
    raise ConfigError(f"表外键：{path} 不在主基准 §10.4 的 config 键表内（规则 20-contracts.md §1）")


class Config:
    """只读配置快照。所有取值经 `get()` 以点号路径读取，不做隐式默认。"""

    def __init__(self, tree: Dict[str, Any], raw: bytes, path: str):
        self._tree = tree
        self._raw = raw
        self.path = path

    # ── 读取 ───────────────────────────────────────────────────────────
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._tree
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require_final(self, dotted: str) -> Any:
        """取一个**必须已定稿**的键；空值（`null`）即未定稿 ⇒ 抛错（§10.4 三态规则）。"""
        value = self.get(dotted, None)
        if value is None:
            raise ConfigError(f"键 {dotted} 为空 = 未定稿（§10.4），本阶段不得使用")
        return value

    def as_dict(self) -> Dict[str, Any]:
        return self._tree

    def is_final(self, dotted: str) -> bool:
        return self.get(dotted, None) is not None

    def pending_keys(self) -> List[str]:
        """列出全部仍为空的键（= 未定稿），供阶段自检与出口闸门清单取证。"""
        return sorted(p for p, v in _leaf_paths(self._tree, opaque=OPAQUE_SUBTREES) if v is None)

    # ── 测量条件随行（§6.5：config 快照哈希）────────────────────────────
    def snapshot_hash(self) -> str:
        return hashlib.sha256(self._raw).hexdigest()


def load(path: Optional[str] = None) -> Config:
    """加载并校验配置。任何表外键 / 缺失键 / 语法错误均抛 `ConfigError`。"""
    path = path or DEFAULT_PATH
    try:
        with open(path, "rb") as fp:
            raw = fp.read()
    except OSError as exc:
        raise ConfigError(f"无法读取配置 {path}: {exc}") from exc
    try:
        tree = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置 YAML 解析失败 {path}: {exc}") from exc
    if not isinstance(tree, dict):
        raise ConfigError(f"配置根节点必须是映射，实际为 {type(tree).__name__}")

    # ① 表外键检查（含 curve_geometry 子表）
    for path_, value in _leaf_paths(tree, opaque=OPAQUE_SUBTREES):
        _validate_leaf(path_, value)

    # ② 缺失键检查：§10.4 的每个非通配键都必须出现在文件里（空值 ≠ 缺失）
    present = {p for p, _ in _leaf_paths(tree, opaque=OPAQUE_SUBTREES)}
    missing = []
    for key in KEY_TABLE:
        if _is_wildcard(key):
            prefix = _wildcard_prefix(key)
            if not any(p.startswith(prefix) for p in present):
                missing.append(key)  # 通配键在 M1 前允许整体缺席（子键名未定，O34-③）
        elif key not in present:
            missing.append(key)
    # 通配键整体缺席不算违规（§10.4 明文：子键名随 M1 实测定义）
    hard_missing = [k for k in missing if not _is_wildcard(k)]
    if hard_missing:
        raise ConfigError("缺失键（§10.4 有定义但文件中不存在）：" + "、".join(sorted(hard_missing)))

    return Config(tree, raw, path)


def main() -> int:
    """自检入口：`python3 config.py [path]` —— 打印快照哈希与未定稿键清单。"""
    import sys

    cfg = load(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"config: {cfg.path}")
    print(f"sha256: {cfg.snapshot_hash()}")
    pending = cfg.pending_keys()
    print(f"未定稿键（null）：{len(pending)}")
    for key in pending:
        print(f"  - {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
