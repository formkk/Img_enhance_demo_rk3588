#!/usr/bin/env python3
"""curve_estim.py —— TH2a：RGA + ZDE++ + EMA（低频）（§10.3 交付模块）

链路（§3.3）：最新帧 → RGA 有效区（`curve_geometry.effective`，落 pad 缓冲第 `pad_top` 行）
             → ZDE++ **2 条带空间切分**（core0+core1，O29 裁定选项 (a)）→ 3ch 曲线场
             → EMA（§9.1 公式）→ **双缓冲发布**。

契约（§5 契约 B/C）：
  · `estimate(const FrameDesc*) -> int`（TH2a，5~15 Hz）；
  · `publish_curve(const CurveSnapshot*) -> int`：**双缓冲发布，读侧只用最新完整版**；
  · `CurveSnapshot` 字段 = `field`（3ch；有效区）/ `seq_est` / `f_est`(Hz) / `alpha` / `publish_ns`；
  · 曲线场精度按 §3.1 契约第 3 条（O23 裁定全档 FP16；U8 为可选优化）。

EMA（§9.1）：`p_t = α·p_{t-1} + (1−α)·p̂_t`，α 为**历史权重**；
             90% 收敛更新数 `N = ln(0.1)/ln(α)`，收敛时间 `N/f_est ≤ 400 ms`（留 100 ms 余量）。
             **α 与 f_est 的定稿值由 M2 按实测频率写入 config**（`pipeline.ema_alpha` / `pipeline.est_freq_hz`）。
"""

from __future__ import annotations

import math
import os
import time
from typing import Any, Dict, Optional

import _starlight_native as _n

import log as starlog

# §3.4 / §3.1 定稿值：显示窗 = 画布左右各一（画布 1024×600 → 每窗 512×288）。
# 这是契约常量（同 CURVE_ITERS），不是 config 键；config 定稿后由 `validate_geometry()` 交叉核对。
WIN_W, WIN_H = 512, 288

# 条带参数（与 tools/pipeline_probe.c 的实测口径一致）
N_SLICES = 2
HALO = 7                       # 感受野半径（7 次 3×3）
CORE_MASK_SLICE = (1, 2)       # RKNN_NPU_CORE_0 = 1 / CORE_1 = 2（O29 裁定 core0+core1）

E_STATE = -5
E_NPU = -2

# 曲线场几何档位键（§10.4 `pipeline.curve_geometry.effective` 的取值域）
LEVEL_KEYS = ("256x144", "480x272", "960x544")


class CurveSnapshot:
    """§5 契约 B 的 CurveSnapshot。`field` 为扩展持有的缓冲（零拷贝，只读使用）。"""

    __slots__ = ("field", "seq_est", "f_est", "alpha", "publish_ns", "buffer_index")

    def __init__(self, field, seq_est: int, f_est: float, alpha: float, publish_ns: int,
                 buffer_index: int):
        self.field = field
        self.seq_est = seq_est
        self.f_est = f_est
        self.alpha = alpha
        self.publish_ns = publish_ns
        self.buffer_index = buffer_index

    def as_dict(self) -> Dict[str, Any]:
        return {"seq_est": self.seq_est, "f_est": round(self.f_est, 3), "alpha": self.alpha,
                "publish_ns": self.publish_ns, "buffer_index": self.buffer_index,
                "curve_w": None, "curve_h": None}


def derive_alpha(f_est_hz: float, budget_ms: float = 400.0) -> float:
    """按 §9.1 由**实测频率**推 α：取满足 `N/f_est ≤ budget` 的最大 N（即最小的平滑）。

    `N = ln(0.1)/ln(α)` ⇒ 给定 N 时 `α = 0.1**(1/N)`。返回 α ∈ (0,1)。
    """
    if f_est_hz <= 0:
        raise ValueError("f_est_hz 必须 > 0")
    n = max(1, int(math.floor(budget_ms / 1000.0 * f_est_hz)))
    return round(0.1 ** (1.0 / n), 4)


class CurveEstimator:
    """TH2a 主体（单线程调用）。"""

    def __init__(self, cfg: Any, logger: starlog.Logger, model_path: Optional[str] = None):
        self.cfg = cfg
        self.log = logger
        self.model_path = model_path or self._default_model()
        self.geom: Dict[str, int] = {}
        self.seq_est = 0
        self.f_est = 0.0
        self.alpha = 0.0
        self._last_est_ns: Optional[int] = None
        self._est_intervals: list = []
        self._buffers = []          # 双缓冲：每个 = (field_buffer, meta)
        self._cur = 0
        self._first = True
        self._est_calls = 0
        self._rga_fallbacks = 0

    # ── 几何与精度（全部来自 §10.4 键表）────────────────────────────────
    def _default_model(self) -> str:
        root = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(root, "models", "zde", "zde_curve_256x272_int8_tk232.rknn")

    def resolve_geometry(self) -> Dict[str, int]:
        cfg = self.cfg
        level = cfg.require_final("pipeline.curve_level")
        if level not in LEVEL_KEYS:
            raise ValueError(f"pipeline.curve_level={level!r} 不在 {LEVEL_KEYS}（§10.4 取值域）")
        eff = (cfg.get("pipeline.curve_geometry.effective") or {})[level]
        pad = (cfg.get("pipeline.curve_geometry.pad_to") or {})[level]
        eff_w, eff_h = int(eff[0]), int(eff[1])
        pad_w, pad_h = int(pad[0]), int(pad[1])
        if pad_h < eff_h or pad_w < eff_w:
            raise ValueError("curve_geometry.pad_to 必须 ≥ effective")
        pad_top = (pad_h - eff_h) // 2
        # 条带输入宽：≥ slice_w_out + 2*HALO，按 8 对齐（与 rknn_split_probe 同式）
        slice_w_out = pad_w // N_SLICES
        slice_w_in = ((slice_w_out + 2 * HALO + 7) // 8) * 8
        self.geom = {
            "cap_w": 1920, "cap_h": 1080,
            "eff_w": eff_w, "eff_h": eff_h,
            "pad_w": pad_w, "pad_h": pad_h, "pad_top": pad_top,
            "win_w": WIN_W, "win_h": WIN_H,
            "canvas_w": int(cfg.require_final("display.mipi_canvas")[0]),
            "canvas_h": int(cfg.require_final("display.mipi_canvas")[1]),
            "core_mask": int(cfg.require_final("rga.core_mask")),
            "n_slices": N_SLICES, "slice_h": pad_h,
            "slice_w_in": slice_w_in, "slice_w_out": slice_w_out,
        }
        return self.geom

    def validate_geometry(self) -> None:
        """用 §3.4 的定稿布局交叉核对画布尺寸（画布 = 两窗 + OSD + 按钮）。"""
        cw, ch = self.geom["canvas_w"], self.geom["canvas_h"]
        if cw < 2 * WIN_W:
            raise ValueError(f"display.mipi_canvas 宽 {cw} < 2×窗宽 {2 * WIN_W}（§3.4）")
        if ch < WIN_H:
            raise ValueError(f"display.mipi_canvas 高 {ch} < 窗高 {WIN_H}（§3.4）")

    def precision(self) -> str:
        p = self.cfg.get("pipeline.curve_field_precision")
        if p not in ("fp16", "u8"):
            raise ValueError(f"pipeline.curve_field_precision={p!r} 非法（§10.4）")
        return p

    def _dst_prec(self) -> int:
        """发布域精度码（与 native/rknn_core.h 的 rknn_field_t 一致）。"""
        return 1 if self.precision() == "fp16" else 2

    def curve_bytes(self) -> int:
        g = self.geom
        return 3 * g["eff_w"] * g["eff_h"] * (2 if self.precision() == "fp16" else 1)

    # ── 生命周期 ───────────────────────────────────────────────────────
    def init(self) -> int:
        self.resolve_geometry()
        self.validate_geometry()
        rc = _n.rga.configure(self.geom)
        if rc != 0:
            return rc
        rk_cfg = {
            "est_w": self.geom["pad_w"], "est_h": self.geom["pad_h"],
            "eff_h": self.geom["eff_h"], "pad_top": self.geom["pad_top"],
            "n_slices": N_SLICES, "slice_h": self.geom["slice_h"],
            "slice_w_in": self.geom["slice_w_in"], "slice_w_out": self.geom["slice_w_out"],
            "core_mask": list(CORE_MASK_SLICE),
        }
        rc = _n.rknn.init(self.model_path, rk_cfg)
        if rc != 0:
            _n.rga.shutdown()
            return rc
        # 双缓冲：两块曲线场（§5：读侧只用最新完整版）。
        # 尺寸按**发布域精度**定（FP16 = 2·3·eff_h·eff_w；U8 = 1·…），
        # 不能复用 RGA 的 U8 尺寸缓冲——NPU 输出在 float32 中间域算完后按精度转换写出。
        n = self.curve_bytes()
        self._buffers = [_n.alloc(n), _n.alloc(n)]
        self._cur = 0
        self._first = True
        return 0

    def shutdown(self) -> None:
        _n.rknn.deinit()
        _n.rga.shutdown()
        self._buffers = []

    def ready(self) -> bool:
        return bool(_n.rknn.ready())

    # ── 估计 ───────────────────────────────────────────────────────────
    def estimate(self, frame: Optional[dict], alpha: Optional[float] = None,
                 use_ema: bool = True) -> int:
        """跑一轮估计（RGA 几何 + NPU 双条带 + EMA），结果落在当前写缓冲。"""
        if not self.ready():
            return E_STATE
        if frame is not None:
            fd = int(frame["dmabuf_fd"])
            rc = _n.rga.blit_nv12_fd(fd, 0)            # 0 = RGA_DST_EST
            if rc != 0:
                self._rga_fallbacks += 1
                buf = _n.capture.buffer(int(frame["buffer_index"]))
                rc = _n.rga.blit_nv12_va(buf, 0)       # staging 兜底
            if rc != 0:
                return rc
        if alpha is None:
            alpha = self.alpha if self.alpha > 0 else 0.0
        est = _n.rga.est(self.geom)
        rc = _n.rknn.estimate(est, self._buffers[self._cur], self._dst_prec(),
                              float(alpha), use_ema, self._first)
        if rc != 0:
            return rc
        self._first = False
        self._est_calls += 1
        self._update_freq()
        return 0

    def _update_freq(self) -> None:
        now = time.monotonic_ns()
        if self._last_est_ns is not None:
            dt = (now - self._last_est_ns) / 1e9
            if dt > 0:
                self._est_intervals.append(1.0 / dt)
                if len(self._est_intervals) > 60:
                    self._est_intervals.pop(0)
                self.f_est = sum(self._est_intervals) / len(self._est_intervals)
        self._last_est_ns = now

    def publish(self) -> Optional[CurveSnapshot]:
        """§5 `publish_curve`：交换双缓冲并把 `seq_est` 自增后发布。"""
        self._cur ^= 1
        self.seq_est += 1
        snap = CurveSnapshot(self._buffers[self._cur], self.seq_est, self.f_est, self.alpha,
                             time.monotonic_ns(), self._cur)
        level = self.cfg.get("pipeline.curve_level")
        self.log.event("curve_estim", "curve_publish", seq_est=self.seq_est,
                       f_est=round(self.f_est, 3), alpha=self.alpha,
                       curve_level=level, precision=self.precision())
        return snap

    def latest_field(self):
        return self._buffers[self._cur]

    def counters(self) -> Dict[str, Any]:
        return {"seq_est": self.seq_est, "f_est": round(self.f_est, 3), "alpha": self.alpha,
                "est_calls": self._est_calls, "rga_fallbacks": self._rga_fallbacks,
                "last_estimate_ns": _n.rknn.last_estimate_ns(),
                "slice_mask_ret": [_n.rknn.slice_mask_ret(i) for i in range(N_SLICES)],
                "rga": _n.rga.info()}


def main() -> int:
    """自检：配置 → 开相机 → 跑 N 轮估计 → 打印 f_est / 条带绑核 → 软复位退出。"""
    import sys

    import config as starlight_config
    import capture as capture_mod

    cfg = starlight_config.load()
    logger = starlog.from_config(cfg)
    cap = capture_mod.Capture(cfg, logger)
    est = CurveEstimator(cfg, logger, sys.argv[1] if len(sys.argv) > 1 else None)

    if cap.init() != 0:
        print("FAIL: capture init")
        return 1
    if cap.stream_on_once() != 0:
        print("FAIL: stream_on_once")
        cap.shutdown()
        return 1
    if est.init() != 0:
        print(f"FAIL: curve_estim init（模型 {est.model_path}）")
        cap.shutdown()
        return 1
    print(f"几何 = {est.geom}")
    print(f"曲线场字节 = {est.curve_bytes()}（精度 {est.precision()}）")

    n = 40
    for _ in range(n):
        f = cap.acquire(1, 1000)
        if f is None:
            continue
        est.estimate(f, alpha=0.0, use_ema=False)
        cap.release(f["lease_id"])
    est.publish()
    c = est.counters()
    print(f"counters = {c}")
    print(f"实测 f_est = {c['f_est']} Hz ⇒ §9.1 推 α = {derive_alpha(c['f_est']) if c['f_est'] > 0 else 'n/a'}")
    est.shutdown()
    cap.shutdown()
    return 0 if c["est_calls"] == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
