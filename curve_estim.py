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
import threading
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

# **（v3.86 裁定 O109 / O111）** 两块示意图与状态行之间的**额外间隔**（px）：图示区整体下移该值，
# 由按钮区高度同步吸收（`ui.button_layout.height_px` 96 → 93 → **90**）⇒ 画布总高仍 600。
# **O109 = 3 px；O111 再加宽到 6 px**（用户两次裁定同向累加）。
# 与 `WIN_W/WIN_H` 同类：**§3.4 布局契约常量，不是 config 键**；放在几何单源里以便自洽断言。
PLOT_TOP_GAP = 6

E_STATE = -5
E_NPU = -2

# 曲线场几何档位键（§10.4 `pipeline.curve_geometry.effective` 的取值域）
LEVEL_KEYS = ("256x144", "480x272", "960x544")


# ── 几何单源（§3.4 / §3.5；**v3.86 就地裁定 O58 = 唯一来源**）───────────────
# 全部由 §10.4 **既有键**推导（**不新增键**）；TH2a / TH4 / TH5 / `osd` / `ui_buttons`
# 一律读本函数，**禁止在别处出现第二份几何数值**（O58 原登记只指 `display_mipi._geom()`，
# 本次扩到 `osd.py` / `ui_buttons.py`；见 §15 O58）。
def resolve_geometry(cfg: Any) -> Dict[str, Any]:
    """解析**全部几何**（曲线估计段 + 视图段）并做 §3.4 / §3.5 自洽校验。

    曲线估计段（TH2a / RGA）：`eff_*` / `pad_*` / `pad_top` / `slice_*` / `hq_*`；
    视图段（§3.4 布局 + §3.5 两态）：两窗、OSD 区、按钮区、全屏 HQ 粘贴矩形、HDMI 画布。
    """
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
    # **（v3.84）** HQ 关键帧输入尺寸（TH3 用）：`hq.active_shape`（如 "960x544"）。
    # RGA 是单例（`CurveEstimator.init()` 里的 `rga.configure` 是**唯一**一次 configure）⇒ HQ 目标
    # 缓冲必须并入同一次 configure，故几何在此统一解析（§10.3「几何单一入口」）。
    # **（v3.86 追加 35）** 按 §3.1 `hq_shape_ladder` 主档规则拆出**内容高**与 **pad 行数**：
    # 主档 960×544 = 内容 960×540 + 上下各 2 行零 pad（降级档 768×432 / 512×288 原生 16:9 ⇒ pad 0）。
    hq_shape = str(cfg.require_final("hq.active_shape"))
    hq_w, hq_h = (int(v) for v in hq_shape.lower().split("x"))
    hq_content_h = int(round(hq_w * 9.0 / 16.0))
    hq_pad_top = (hq_h - hq_content_h) // 2

    # ── 视图段（§3.4 布局：两窗 512×288 + OSD + 按钮 = 画布；§3.5 全屏 = HQ 1:1 居中）──
    cw, ch = (int(v) for v in cfg.require_final("display.mipi_canvas"))
    hw, hh = (int(v) for v in cfg.require_final("display.hdmi_canvas"))
    osd_h = int(cfg.require_final("ui.osd_metrics_px"))
    row_h = int(cfg.require_final("ui.osd_status_row_px"))
    plot_w, plot_h = (int(v) for v in cfg.require_final("ui.osd_plot_px"))
    bl = cfg.require_final("ui.button_layout")
    btn_h, btn_rows = int(bl["height_px"]), int(bl["rows"])
    keys = tuple((str(k["name"]), int(k["x"]), int(k["w"])) for k in bl["keys"])
    btn_y = ch - btn_h * btn_rows
    osd_y = btn_y - osd_h
    if osd_y < WIN_H or osd_y + osd_h != btn_y or btn_y + btn_h * btn_rows != ch:
        raise ValueError(
            f"§3.4 布局不自洽：窗高 {WIN_H} + OSD {osd_h} + 按钮 {btn_h}×{btn_rows} ≠ 画布高 {ch}")
    if row_h + PLOT_TOP_GAP + plot_h != osd_h:
        raise ValueError(
            f"§3.4 OSD 不自洽：状态行 {row_h} + 图示上间隔 {PLOT_TOP_GAP} + 图示 {plot_h} ≠ 指标区 {osd_h}"
            "（O109：3 px 用于加宽状态行↔图示的间隔）")
    min_px = int(cfg.require_final("ui.hit_area_min_px"))
    for name, x, w in keys:
        if w < min_px or x < 0 or x + w > cw:
            raise ValueError(f"§3.4 按钮 {name} 宽 {w} < 命中区下限 {min_px} 或越界（x={x}）")

    return {
        # 采集 / 曲线估计段（TH2a + RGA）
        "cap_w": 1920, "cap_h": 1080,
        "eff_w": eff_w, "eff_h": eff_h,
        "pad_w": pad_w, "pad_h": pad_h, "pad_top": pad_top,
        "win_w": WIN_W, "win_h": WIN_H,
        "canvas_w": cw, "canvas_h": ch,
        "core_mask": int(cfg.require_final("rga.core_mask")),
        "n_slices": N_SLICES, "slice_h": pad_h,
        "slice_w_in": slice_w_in, "slice_w_out": slice_w_out,
        "hq_w": hq_w, "hq_h": hq_h,
        "hq_content_h": hq_content_h, "hq_pad_top": hq_pad_top,
        # 视图段（TH4 / TH5 / osd / ui_buttons）
        "win_orig": (0, 0, WIN_W, WIN_H),
        "win_enh": (WIN_W, 0, WIN_W, WIN_H),
        "osd_y": osd_y, "osd_h": osd_h,
        "status_row_y": osd_y, "status_row_h": row_h,
        "plot_y": osd_y + row_h, "plot_w": plot_w, "plot_h": plot_h,
        "button_y": btn_y, "button_h": btn_h, "button_keys": keys,
        # 全屏 HQ 视图：HQ 原生 960×540 在 1024×600 画布上 **1:1 居中**（左右 32 / 上下 30 黑边）
        "hq_paste": ((cw - hq_w) // 2, (ch - hq_content_h) // 2, hq_w, hq_content_h),
        # HDMI 镜像画布（§3.5）：常规态 = MIPI 画布整幅 1.875×；全屏态 = HQ 整幅 2.0×
        "hdmi_canvas_w": hw, "hdmi_canvas_h": hh,
        "mirror_dst": (hw, hh),
        "hq_dst": (hw, hh),
    }


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
        # ── **（v3.86 裁定 O123）TH2a 独立线程**（§6.1 第 413 / 415 行要求"独立循环、不在帧路径"）──
        # `request()` 在调用（主）线程里**只做 RGA 缩放**（持帧时长 = 一次 RGA），NPU 双条带 + EMA
        # 由 worker 执行 ⇒ 显示循环不再被 ~16 ms 的估计阻塞。RGA 仍**单调用方 = 主线程**
        # （`native/rknn_core.c` 无 RGA 调用）⇒ 不引入 RGA 并发。
        self._req_skipped = 0
        self._est_fail = 0                       # worker 内估计失败次数（不静默）
        self._th: Optional[threading.Thread] = None
        self._stop = False
        self._busy = False                       # True = 有一轮请求在 worker 手里（含在跑）
        self._mtx = threading.Lock()             # 保护 `_busy` / `_first` / `_cur` / `_latest`
        self._cv = threading.Condition(self._mtx)
        self._latest: Optional[CurveSnapshot] = None

    # ── 几何与精度（全部来自 §10.4 键表）────────────────────────────────
    def _default_model(self) -> str:
        root = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(root, "models", "zde", "zde_curve_256x272_int8_tk232.rknn")

    def resolve_geometry(self) -> Dict[str, Any]:
        """几何单源（模块级 `resolve_geometry(cfg)`）的唯一调用点（§10.3「几何单一入口」）。"""
        self.geom = resolve_geometry(self.cfg)
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
        # **（v3.86 裁定 O123）** 启动 TH2a worker（与 TH3 `keyframe_infer` 同构：daemon 线程 + 条件变量）
        self._stop = False
        self._th = threading.Thread(target=self._worker, name="TH2a-curve", daemon=True)
        self._th.start()
        return 0

    def shutdown(self) -> None:
        # **（v3.86 裁定 O123）** 先停 worker（等在跑的那一轮收尾；单轮 ~16 ms，2 s 为有界兜底），
        # 再释放 NPU / RGA —— 顺序有约束：worker 未停就 `deinit` 会踩在途调用。
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._th is not None:
            self._th.join(timeout=2.0)
            self._th = None
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

    # ── 线程化入口（**v3.86 裁定 O123**：曲线估计移出显示循环）──────────────
    def request(self, frame: Optional[dict] = None) -> int:
        """**非阻塞**请求一轮估计（帧路径的唯一入口）：
        在本线程**同步只做 RGA 缩放**（NV12 1080p → 480×272；持帧时长 = 一次 RGA，lease 语义不变），
        随后把 NPU 双条带 + EMA 交给 worker ⇒ `main.py` 每帧代价从 ~16 ms 降到一次 RGA。

        返回 `0` = 已受理；`E_STATE` = 上一轮仍在 worker 手里（本轮**跳过**，绝不触碰共享估计缓冲）；
        其余负值 = RGA 失败（与 `estimate()` 同返回码语义）。
        """
        if not self.ready():
            return E_STATE
        with self._cv:
            if self._busy:
                self._req_skipped += 1
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
            self._busy = True
            self._cv.notify_all()
        return 0

    def latest(self) -> Optional[CurveSnapshot]:
        """**非阻塞**取「最近一次已发布」的曲线快照（读侧只读已发布块，§5 双缓冲意图不变）。"""
        return self._latest

    def wait_first(self, timeout_s: float = 2.0) -> Optional[CurveSnapshot]:
        """**只供首拍**（`main.py` 第 0 帧）：等第一次发布，有界 `timeout_s`。

        理由：O123 之前首帧是"同步估计 + 立即发布"，故帧路径从第 0 帧起就持有有效曲线场；
        线程化后发布是异步的 ⇒ 首帧会短暂为空。此方法把这一处等回来（**仅启动期一次**）；
        **稳态帧路径不得调用**（否则又把基层等待带回帧路径）。
        """
        with self._cv:
            if self._latest is None and not self._stop:
                self._cv.wait_for(lambda: self._latest is not None or self._stop,
                                  timeout=timeout_s)
            return self._latest

    def _worker(self) -> None:
        """TH2a worker：NPU 双条带 + EMA + 发布。**不触 RGA**（RGA 单调用方 = 主线程）。"""
        while True:
            with self._cv:
                while not self._busy and not self._stop:
                    self._cv.wait()
                if not self._busy:
                    return                     # 停止且无待办 ⇒ 退出
            alpha = self.alpha if self.alpha > 0 else 0.0
            est = _n.rga.est(self.geom)
            rc = _n.rknn.estimate(est, self._buffers[self._cur], self._dst_prec(),
                                  float(alpha), True, self._first)
            with self._cv:
                if rc == 0:
                    self._first = False
                    self._est_calls += 1
                    self._update_freq()
                    self._latest = self._publish_locked()
                else:
                    self._est_fail += 1        # 失败不静默：计数可经 `counters()` 读出
                self._busy = False
                self._cv.notify_all()          # 唤醒 `wait_first()`（首拍）与任何等待者

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
        """§5 `publish_curve`：发布**刚估计出的那一块**缓冲，然后把写指针切到另一块。

        **（v3.86 裁定 O104 修正）** 原实现是「先 `self._cur ^= 1`、再取 `self._buffers[self._cur]`」
        ⇒ 返回的是**上一次**写入的那块（实测 `published(k) == written(k-1)`），且**首拍返回的是
        从未写过的全零缓冲** ⇒ ① 交付链多出一级曲线滞后（旧配置 `est_every=6` 时 +400 ms）；
        ② 头 `est_every` 帧用的曲线是全零 ⇒ 那几帧的增强是**空操作**。
        现改为「先取当前写缓冲发布，再翻转写指针」——已发布的那块在下一轮估计中不再被写，
        仍满足 §5「读侧只用最新完整版」的双缓冲意图。
        """
        with self._cv:
            return self._publish_locked()

    def _publish_locked(self) -> CurveSnapshot:
        """`publish()` 的实现体（**调用方须已持 `_mtx`/`_cv`**）：worker 与模块自检共用。"""
        self.seq_est += 1
        buf_idx = self._cur
        snap = CurveSnapshot(self._buffers[buf_idx], self.seq_est, self.f_est, self.alpha,
                             time.monotonic_ns(), buf_idx)
        self._cur ^= 1                      # 下一轮 `estimate()` 写另一块
        level = self.cfg.get("pipeline.curve_level")
        self.log.event("curve_estim", "curve_publish", seq_est=self.seq_est,
                       f_est=round(self.f_est, 3), alpha=self.alpha,
                       curve_level=level, precision=self.precision())
        return snap

    def latest_field(self):
        """**最近一次已发布**的曲线场（发布后写指针已翻转 ⇒ 在 `self._cur ^ 1`，见 `publish()`）。"""
        return self._buffers[self._cur ^ 1]

    def counters(self) -> Dict[str, Any]:
        return {"seq_est": self.seq_est, "f_est": round(self.f_est, 3), "alpha": self.alpha,
                "est_calls": self._est_calls, "rga_fallbacks": self._rga_fallbacks,
                # **（v3.86 裁定 O123）** 线程化后的两个诊断量（非日志事件 / 非 config 键）
                "req_skipped": self._req_skipped, "est_fail": self._est_fail,
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
