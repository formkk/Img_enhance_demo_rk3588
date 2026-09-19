#!/usr/bin/env python3
"""realtime_apply.py —— 曲线应用（§10.3 交付模块；TH2b 调窗分辨率、TH3 调触发满幅存盘）

地位：交付链路的曲线应用门面。实现体 = 同目录的 `_realtime_apply` pybind11 扩展
      （C/NEON 单遍融合，源码 `native/apply_core.c` + `native/realtime_apply_pybind.cpp`）。

契约（主基准 §5 契约 C 节，签名逐字）：
    apply_window(rgb, curve, dst, w, h) -> int      # 窗分辨率（TH2b 实时链路）
    apply_full(nv12, curve, dst_rgb, w, h) -> int   # 满幅（TH3 触发存盘集合 B）

不变量（§3.3、§3.1 契约第 3 条、docs/design/realtime_apply.md）：
  · **8 次迭代单遍融合**——未融合即判未达标；
  · 曲线场在迭代中复用同一份，每目标像素**只采样一次**；
  · 曲线场按 §3.1 契约以 **FP16**（全档，O23 裁定）存取，U8 为可选优化；
  · 本模块**无逐帧可变状态**：几何/精度由 `configure_from_config()` 在启动时一次性注入，
    之后只读 ⇒ 可被 TH2b 与 TH3 并发调用。

依赖：仅标准库 + `_realtime_apply`（**不依赖 numpy**；板上镜像自带 python3 无 numpy）。
"""

from __future__ import annotations

import os
import struct
from typing import Any, Optional, Sequence, Tuple

try:
    import _realtime_apply as _native
except ImportError as exc:  # pragma: no cover - 构建缺失时的可读报错
    raise ImportError(
        "未找到 _realtime_apply 扩展。请先在板上原生构建："
        "`cmake -S . -B build && cmake --build build`（规格见 docs/build.md）"
    ) from exc

# 迭代次数 = 契约值（§3.3 / §4.1：8 次迭代必须单遍融合）。**不是 config 键**，不入 §10.4。
CURVE_ITERS = 8

# §5 契约 A 的返回码符号名（本模块只用 0 与 -5）
OK = 0
E_STATE = -5

# 曲线场有效区档位 → §10.4 `pipeline.curve_geometry.effective` 的键
LEVEL_KEYS = ("256x144", "480x272", "960x544")


def _geometry_from_config(cfg: Any) -> Tuple[Tuple[int, int], str, str, str]:
    """从 config 取出曲线场有效区、精度、色彩矩阵、量化范围。取值源全部是 §10.4 的键。"""
    level = cfg.get("pipeline.curve_level")
    if level not in LEVEL_KEYS:
        raise ValueError(f"pipeline.curve_level = {level!r} 不在 {LEVEL_KEYS} 内（§10.4 取值域）")
    effective = cfg.get("pipeline.curve_geometry.effective") or {}
    if level not in effective:
        raise ValueError(f"pipeline.curve_geometry.effective 缺档位 {level}（§10.4）")
    cw, ch = effective[level]

    precision = cfg.get("pipeline.curve_field_precision")
    if precision not in ("fp16", "u8"):
        raise ValueError(
            f"pipeline.curve_field_precision = {precision!r} 只允许 'fp16' / 'u8'（§10.4）"
        )

    # isp.csc_matrix / isp.rgb_range 是 M1 键，当前为空 ⇒ 传 'unset'（**不得猜测取值**），
    # 此时 apply_full 返回 E_STATE；apply_window 不受影响。
    csc = cfg.get("isp.csc_matrix") or "unset"
    rng = cfg.get("isp.rgb_range") or "unset"
    return (int(cw), int(ch)), precision, str(csc), str(rng)


def configure(curve_w: int, curve_h: int, precision: str,
              csc: str = "unset", rng: str = "unset", iters: int = CURVE_ITERS) -> int:
    """一次性注入曲线场几何与色彩参数。**必须在任何工作线程启动前调用。**"""
    return int(_native.configure(curve_w, curve_h, iters, precision, csc, rng))


def configure_from_config(cfg: Any) -> int:
    """按 §10.4 键表注入配置（推荐入口，由 main.py 在启动序里调用一次）。"""
    (cw, ch), precision, csc, rng = _geometry_from_config(cfg)
    return configure(cw, ch, precision, csc, rng)


def is_configured() -> bool:
    return bool(_native.is_configured())


def shutdown() -> None:
    _native.shutdown()


def impl_id() -> str:
    return str(_native.impl_id())


def curve_bytes(curve_w: int, curve_h: int, precision: str) -> int:
    return int(_native.curve_bytes(curve_w, curve_h, precision))


# ── §5 契约 C 节的两个入口 ─────────────────────────────────────────────
def apply_window(rgb: Any, curve: Any, dst: Any, w: int, h: int) -> int:
    """窗分辨率曲线应用。`rgb` / `dst` = RGB888 紧排缓冲（≥ w*h*3），`curve` = 3 通道平面场。"""
    return int(_native.apply_window(rgb, curve, dst, int(w), int(h)))


def apply_full(nv12: Any, curve: Any, dst_rgb: Any, w: int, h: int) -> int:
    """满幅曲线应用（NV12 → RGB888 后 8 iter）。仅触发存盘集合 B 使用。"""
    return int(_native.apply_full(nv12, curve, dst_rgb, int(w), int(h)))


def apply_scalar(rgb: Any, curve: Any, dst: Any, w: int, h: int) -> int:
    """与 NEON 同形的标量参考（自检用，非契约入口）。"""
    return int(_native.apply_scalar(rgb, curve, dst, int(w), int(h)))


# ── 自检辅助（非契约入口）─────────────────────────────────────────────
def synth_rgb(w: int, h: int, seed: int = 1) -> bytearray:
    """确定性合成 RGB888（LCG，无外部依赖），供数值一致性检查。"""
    buf = bytearray(w * h * 3)
    state = seed & 0xFFFFFFFF
    for i in range(len(buf)):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        buf[i] = (state >> 16) & 0xFF
    return buf


def synth_curve(cw: int, ch: int, precision: str, seed: int = 7) -> bytes:
    """确定性合成 3 通道平面曲线场（值域 [0,1]）。"""
    n = cw * ch * 3
    vals = []
    state = seed & 0xFFFFFFFF
    for _ in range(n):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        vals.append((state >> 16) / 65535.0)
    if precision == "fp16":
        return struct.pack(f"<{n}e", *vals)
    return bytes(int(v * 255.0 + 0.5) for v in vals)


def _selfcheck(precision: str, w: int = 512, h: int = 288,
               cw: int = 480, ch: int = 270) -> int:
    """NEON vs 标量逐位对账 + 基本健全性检查（§11.2 的数值一致性检查子项）。"""
    shutdown()
    rc = configure(cw, ch, precision)
    assert rc == OK, f"configure 失败 rc={rc}"
    rgb = synth_rgb(w, h, seed=3)
    curve = synth_curve(cw, ch, precision, seed=11)
    a = bytearray(w * h * 3)
    b = bytearray(w * h * 3)
    rc1 = apply_window(rgb, curve, a, w, h)
    rc2 = apply_scalar(rgb, curve, b, w, h)
    assert rc1 == OK and rc2 == OK, f"apply rc={rc1}/{rc2}"
    mismatch = sum(1 for i in range(len(a)) if a[i] != b[i])
    max_abs = max((abs(a[i] - b[i]) for i in range(len(a))), default=0)
    print(f"[{precision}] {w}x{h} curve={cw}x{ch} NEON-vs-scalar "
          f"mismatch_bytes={mismatch} max_abs={max_abs}")
    if mismatch != 0:
        raise SystemExit(f"FAIL: NEON 与标量不一致（mismatch={mismatch}）")
    if not any(a):
        raise SystemExit("FAIL: 输出全 0，链路可疑")
    print(f"[{precision}] OK（NEON 与标量逐位一致，输出非空）")
    return 0


def main() -> int:
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        for prec in ("fp16", "u8"):
            _selfcheck(prec)
        shutdown()
        return 0
    print(f"impl: {impl_id()}")
    print(f"configured: {is_configured()}")
    print(f"CURVE_ITERS={CURVE_ITERS}（§3.3 契约值）")
    print("用法：python3 realtime_apply.py --selfcheck")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
