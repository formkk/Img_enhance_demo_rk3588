#!/usr/bin/env python3
"""keyframe_infer.py —— TH3：关键帧（HQ）推理（§10.3 交付模块）

契约（§5 契约 C 节）：
    `request(uint64 seq_src, const FrameDesc*) -> int`   0 / E_STATE / E_NPU
    `pop_result(HqResult*) -> int`                       0 / E_TIMEOUT
    ~~`save_set_abcd(uint64 seq_src, const SaveRequest*) -> int`~~  **（v3.86 裁定 O102）随「存图」整体取消而移除**

**本版实现范围（v3.84）**：
  · **已实现** `request()` / `pop_result()`——触发 → 缩放 → HQ 推理 → 替换式结果槽。
  · **（v3.86 裁定 O102）** 原 `save_set_abcd()`（触发存盘集合 A~D，经 `realtime_apply.apply_full()`
    走满幅应用）**随「存图」功能整体取消而移除**（§8.3 整节取消；**O72 ① 因对象消失而关闭**）。

数据流（§5 TH3 段 + **v3.86 追加 35 的 §3.1 口径**）：
    触发暗帧（FrameDesc 的 dmabuf）→ **RGA** 缩放到 `hq.active_shape` 的**内容区**（主档 960×540，
    落在 960×544 缓冲的第 2..541 行；上下各 2 行为**零 pad**，由 `native/rga_core.c` 在缓冲池
    初始化时一次性 memset 并保持）→ **hq_core** 打包为 NHWC float32 [0,255]（整幅 960×544，
    pad 行随同入模）→ **HQ 模型 FP16**（core2，O36）→ 结果 NCHW float32 [0,1] **裁掉 pad 行
    回内容高 960×540** → 存入**替换式槽**（只留最新一张）。
    pad/crop 口径的**唯一来源 = `curve_estim.resolve_geometry()` 的 `hq_content_h` / `hq_pad_top`**。

**线程形态（v3.84 裁定：真线程）**：TH3 是**独立 OS 线程**（§5 原文口径）。
**理由**：HQ 单次推理 **~8 s**，若在主循环内联，会让 **MIPI 冻结 8 s**，直接违反 §5
「处理中 MIPI 增强窗照常刷新（TH4 不得因结果槽空而锁帧）」——8 s 与 TH5 的 200 ms 轮询
不是一个量级，故**不适用** TH5 那种"主循环低频步进"的形态（O66 只管 TH5）。

**同步/异步切分**（关键）：
  · `request()` **同步**完成 **RGA 缩放**（= §5-C 要求的"先复制源帧再释放 lease"）——仅数毫秒，
    返回后调用方即可安全释放 lease；
  · **异步**部分（打包 + NPU 推理，~8 s）交给 worker 线程；`request()` **立即返回**，主循环不受阻。
  · **同一时刻只跑一次推理**——由 TH1 的 `FSM_HQ_BUSY` 保证（再触发一律丢弃、不排队），
    故 `_in_f32` / 输出缓冲不会被并发覆写。

硬约束：
  · **替换式槽**：只留最新一张；`pop_result` 取走后槽清空（§5「HQ 结果槽（替换式）」）；
  · **再触发一律丢弃并计数**（`discarded_triggers`）——由 TH1 `capture.submit_trigger` 负责，
    本模块不排队、不补做、无 pending 位；
  · **TH4 不得因结果槽空而锁帧**——本模块不参与 MIPI 合成。

模型选择：`hq.model_id` **仍空缺**（O30 挂起）⇒ 本版按 `hq.active_shape` 推导路径
`models/hq/hq_<W>x<H>_fp16_tk232.rknn`（**临时口径，登记于 O72**），待 O30 定稿后改由 `hq.model_id` 决定。

RGA 是**单例**（`curve_estim.init()` 里唯一一次 `rga.configure`）⇒ HQ 目标缓冲的分配并入
`curve_estim.resolve_geometry()` 的几何（含 `hq_w`/`hq_h`），本模块只取用 `_n.rga.hq(geom)`。
"""

from __future__ import annotations

import array
import os
import threading
import time
from typing import Any, Dict, Optional

import _starlight_native as _n

import log as starlog
import realtime_apply as ra

E_NPU = -2
E_STATE = -5
E_TIMEOUT = -6

# RGA 目标枚举（与 native/rga_core.h 的 rga_dst_t 一致）
RGA_DST_HQ = 2

# O36 裁定：HQ 分核 = `RKNN_NPU_CORE_2`（枚举值 4，见 native/rknn_api.h）
HQ_CORE_MASK = 4

# HqResult.active_shape 枚举（§5-B）
SHAPE_ENUM = {"960x544": 0, "768x432": 1, "512x288": 2}


def crop_nchw_f32_pad(buf, w, h, pad_top, content_h):
    """NCHW float32 输出**裁掉上下 pad 行**（§3.1；结果面 = `w×content_h`）。

    NCHW 每通道面为 `h×w` 个 float 连续存放 ⇒ 裁行 = **每通道一段连续字节切片**（无逐像素开销）。
    """
    if pad_top == 0 and content_h == h:
        return bytes(buf)
    row = w * 4
    plane = h * w * 4
    cplane = content_h * w * 4
    out = bytearray(3 * cplane)
    for c in range(3):
        s = c * plane + pad_top * row
        out[c * cplane:(c + 1) * cplane] = buf[s:s + cplane]
    return bytes(out)


class KeyframeInfer:
    """TH3 主体（**独立 OS 线程**；`request()` 非阻塞，推理在 worker 内进行）。"""

    def __init__(self, cfg: Any, logger: starlog.Logger, geom: Dict[str, int],
                 model_dir: str = "models/hq", curve_src: Any = None,
                 chroma_gain: float = 1.0, chroma_denoise_r: int = 0,
                 tm_b: float = 0.0, tm_target: float = 0.0,
                 tm_first: bool = False):
        self.cfg = cfg
        self.log = logger
        self.geom = geom
        # **[A/B 钩子]（非 config 键）** HQ 输入侧接 ZDE++ 曲线的曲线场来源：
        # 传入 `CurveEstimator.latest`（无参可调用，返回 `CurveSnapshot` 或 `None`）= 开启；
        # `None` = 关闭（**默认，交付链路行为不变**）。曲线应用语义由 `realtime_apply` 的
        # `preserve_ratio` 决定（生产保比）。接线见 `main.py --hq-curve-in`。
        self.curve_src = curve_src
        # **[A/B 钩子]（非 config 键）** 曲线之后、进模型之前的**黑位锚定色调映射**（§15 O162 候选）：
        # `out = rgb + (Y' − Y)`，`Y' = (Y − b)·s`（保色度，只动亮度对比），带高光软压缩（>=0.8 半程）。
        # `b = tm_b × p1`（曲线后黑位）、`s` 由 `tm_target`（目标 Y 均值）反解。
        # **`tm_b = 0` = 关闭 ⇒ 逐位恒等**（默认；交付行为不变）。
        self.tm_b = float(tm_b)
        self.tm_target = float(tm_target)
        # **[A/B 实验]（非 config 键）** 色调映射的**顺序**：`False`（默认）= 曲线 → TM（生产定稿）；
        # `True` = **TM → 曲线**（"情形 C"实验：验证曲线是否会把 TM 挣来的对比度再抹平）。
        self.tm_first = bool(tm_first)
        # **[A/B 钩子]（非 config 键）链路末显式色度增强** —— 在 HQ 模型输出（NCHW f32 [0,1]）
        # **之后**、`to_rgb888`/OSD 合成**之前**，对结果幅做**保持亮度的线性色度增益**（口径见
        # `_apply_output_chroma`）。`1.0` = **逐位恒等 ⇒ 交付链路行为不变**；
        # 具体取值**待 GT 标定**（§15 O161，见主基准 §14 追加 9）。
        self.chroma_gain = float(chroma_gain)
        self._last_clip_pct = 0.0
        # **[A/B 钩子]（非 config 键）「只滤高频色度」的网后空间降噪** —— 在 HQ 模型输出
        # （NCHW f32 [0,1]）**之后**、色度增益**之前**，对**色度**做半径 `r` 的分离式盒式低通
        # （保亮度、保色比语义；口径见 `_apply_chroma_denoise`）。`r ≤ 0` = **逐位恒等**。
        # **2026-10-09 目测定稿 = `r = 2`**（由 `main.py` 的 `Demo` 默认注入；反向 opt-out =
        # `--hq-chroma-denoise 0`）。见主基准 §14 追加 15 / §15 O164。
        self.chroma_denoise_r = max(0, int(chroma_denoise_r))
        self._last_cdn_clip_pct = 0.0
        self.hq_w = int(geom["hq_w"])
        self.hq_h = int(geom["hq_h"])
        # **（v3.86 追加 35）** §3.1 pad 口径（唯一来源 = `curve_estim.resolve_geometry()`）：
        # 模型输入 = 整幅 `hq_w×hq_h`（含上下各 `hq_pad_top` 行零 pad）；
        # 槽内结果 = **裁掉 pad 行**后的内容幅 `hq_w×hq_content_h`（如 960×540）。
        self.hq_pad_top = int(geom.get("hq_pad_top", 0))
        self.hq_content_h = int(geom.get("hq_content_h", self.hq_h))
        self.model_dir = model_dir
        self._inited = False
        self._model_id = ""
        # 替换式结果槽（只留最新一张）；由 `_mtx` 保护（worker 写 / 主循环读）
        self._slot: Optional[Dict[str, Any]] = None
        self._runs = 0
        self._last_run_ns = 0
        # 输入缓冲：NHWC float32 [0,255]（H×W×3×4 字节）——**只由 worker 触碰**
        self._in_f32 = bytearray(self.hq_w * self.hq_h * 3 * 4)
        # ── worker 线程 ────────────────────────────────────────────────
        self._th: Optional[threading.Thread] = None
        self._mtx = threading.Lock()                  # 保护 `_slot` / `_runs` / `_busy`
        self._cv = threading.Condition()              # 唤醒 worker
        self._pending = False                         # 有新请求待处理
        self._pending_seq = 0
        self._stop = False
        self._busy = False                            # 正在推理（供 main.py 判 `hq_done`）
        self._last_rc = 0

    # ── 生命周期 ───────────────────────────────────────────────────────
    def init(self) -> int:
        """载入 HQ 模型（core2）并起 worker 线程。返回 0 / E_NPU。"""
        if self._inited:
            return E_STATE
        shape = str(self.cfg.require_final("hq.active_shape"))
        model = os.path.join(self.model_dir, f"hq_{shape}_fp16_tk232.rknn")
        if not os.path.exists(model):
            return E_NPU
        rc = _n.rknn_hq.init(model, {"w": self.hq_w, "h": self.hq_h, "core_mask": HQ_CORE_MASK})
        if rc != 0:
            return rc
        self._model_id = model
        self._inited = True
        self._th = threading.Thread(target=self._worker, name="TH3-keyframe", daemon=True)
        self._th.start()
        return 0

    def shutdown(self) -> None:
        """停 worker 并释放模型。**在途推理会被等完**（最长 ~8 s，有界）。"""
        if self._inited:
            with self._cv:
                self._stop = True
                self._cv.notify_all()
            if self._th is not None:
                self._th.join(timeout=30.0)
                self._th = None
            _n.rknn_hq.deinit()
            self._inited = False
            with self._mtx:
                self._slot = None
                self._busy = False

    def busy(self) -> bool:
        """是否有 HQ 推理在途（供 `main.py` 判何时 `capture.hq_done()`）。"""
        with self._mtx:
            return self._busy

    def info(self) -> Dict[str, Any]:
        with self._mtx:
            return {
                "model_id": self._model_id,
                "shape": f"{self.hq_w}x{self.hq_h}",
                "core_mask_ret": int(_n.rknn_hq.core_mask_ret()) if self._inited else -1,
                "runs": self._runs,
                "last_run_ns": self._last_run_ns,
                "last_rc": self._last_rc,
                "busy": self._busy,
                "slot_full": self._slot is not None,
                "thread_alive": bool(self._th and self._th.is_alive()),
                "curve_in": self.curve_src is not None,
                "chroma_gain": self.chroma_gain,
                "chroma_clip_pct": self._last_clip_pct,
                "chroma_denoise_r": self.chroma_denoise_r,
                "chroma_dn_clip_pct": self._last_cdn_clip_pct,
            }

    # ── **[A/B 钩子]** HQ 输入侧曲线应用（非契约步；默认不启用）────────────
    def _apply_input_curve(self, hq_rgb: Any) -> Any:
        """把 TH2a **最近一次已发布**的 ZDE++ 曲线场施加到 HQ 输入的**内容区**。

        口径：只作用于 `hq_pad_top .. hq_pad_top+hq_content_h` 行（上下 pad 保持零）；应用实现
        = 交付 `realtime_apply.apply_window`（NEON 单遍融合，语义由 `preserve_ratio` 决定）。
        无有效曲线场（首拍尚未发布）或应用失败时**原样返回**（不改变既有行为）。
        """
        snap = self.curve_src()
        row = self.hq_w * 3
        a = self.hq_pad_top * row
        b = a + self.hq_content_h * row
        full = None
        do_tm = self.tm_b > 0.0 and self.tm_target > 0.0
        if self.tm_first and do_tm:              # 情形 C：TM 置于曲线之前
            full = bytearray(hq_rgb.to_bytes())
            self._tonemap_content(full, a, b, self.hq_w, self.hq_content_h)
        if snap is not None and snap.field is not None:
            src = full if full is not None else bytearray(hq_rgb.to_bytes())
            dst = bytearray(self.hq_w * self.hq_content_h * 3)
            if ra.apply_window(src[a:b], snap.field, dst, self.hq_w, self.hq_content_h) == 0:
                src[a:b] = dst
                full = src
        # **色调映射与曲线解耦**（2026-10-09）：曲线可关（`--no-hq-curve-in`），此时 TM 直接作用于
        # **原始输入**（= 情形 B）；`--hq-tm-first` 时 TM 改为置于曲线**之前**（= 情形 C）。
        if do_tm and not self.tm_first:
            if full is None:
                full = bytearray(hq_rgb.to_bytes())
            self._tonemap_content(full, a, b, self.hq_w, self.hq_content_h)
        return full if full is not None else hq_rgb

    def _tonemap_content(self, buf: bytearray, off: int, end: int, w: int, h: int) -> None:
        """**黑位锚定色调映射**（曲线之后、进模型之前；就地改 `buf[off:end]`）。

        `Y' = (Y − b)·s`，三通道同加 `(Y' − Y)` ⇒ **保色度、只动亮度对比**；
        高光软压缩：`Y' > 0.8×255` 的部分按半程压缩（防打爆 B 通道）。
        `b = tm_b × p1`（1/16 子采样取分位）、`s = tm_target / (mean − b)`。
        **纯标准库**（交付环境无 numpy）；在 worker 线程内、每触发一次，不在帧路径上。
        """
        n = w * h
        Y = [0.0] * n
        j = off
        for i in range(n):
            Y[i] = 0.299 * buf[j] + 0.587 * buf[j + 1] + 0.114 * buf[j + 2]
            j += 3
        samp = sorted(Y[::16])
        b = samp[int(len(samp) * 0.01)] * self.tm_b
        mean = sum(Y) / n
        s = self.tm_target / (mean - b) if mean > b else 1.0
        k0 = 204.0
        j = off
        for i in range(n):
            yp = (Y[i] - b) * s
            if yp > k0:
                yp = k0 + (yp - k0) * 0.5
            if yp < 0.0:
                yp = 0.0
            elif yp > 255.0:
                yp = 255.0
            d = yp - Y[i]
            v = buf[j] + d
            buf[j] = 0 if v < 0.0 else (255 if v > 255.0 else int(v + 0.5))
            v = buf[j + 1] + d
            buf[j + 1] = 0 if v < 0.0 else (255 if v > 255.0 else int(v + 0.5))
            v = buf[j + 2] + d
            buf[j + 2] = 0 if v < 0.0 else (255 if v > 255.0 else int(v + 0.5))
            j += 3

    # ── **[A/B 钩子]** 链路末显式色度增强（非契约步；`k=1.0` 时逐位恒等）──────────
    def _apply_output_chroma(self, buf: bytes) -> bytes:
        """HQ 结果幅 → **保持亮度的线性色度增益** → 同布局同长度输出。

        口径（§15 O161）：
            `Y  = 0.299R + 0.587G + 0.114B`（Rec.601，与链路 CSC 同族）
            `C' = clip(Y + k·(C − Y), 0, 1)`，C ∈ {R,G,B}
        ⇒ **Y 逐像素不变**（亮度不动），只把色度线性缩放 ⇒ `k` 可由 GT 直接标定。
        取值：`k = 1.0` **提前返回原对象 ⇒ 逐位恒等**（交付行为不变）；`k > 1` 提饱和、`k < 1` 降饱和。
        布局不变（3 个通道面各 `hq_content_h×hq_w` 个 float32）⇒ 下游 `to_rgb888` 无感。
        `_last_clip_pct` = 逐值削顶率（k>1 时 >0，作为过饱和的安全信号，经 `info()` 暴露）。
        """
        k = self.chroma_gain
        if k == 1.0 or not buf:
            return buf
        # **只依赖标准库**：交付运行环境（systemd 以 root 运行 `/usr/bin/python3`）**没有 numpy**
        # （2026-10-09 实测：root 与 cat 均 `ModuleNotFoundError`）⇒ 不得引入 numpy 依赖。
        # 本步在 worker 线程内、**每触发一次**（不在帧路径上），纯 Python 开销可接受。
        a = array.array("f")
        a.frombytes(buf)
        m = len(a) // 3
        if m == 0:
            return buf
        r_plane, g_plane, b_plane = a[:m], a[m:2 * m], a[2 * m:]
        orr = array.array("f", bytes(4 * m))
        og = array.array("f", bytes(4 * m))
        ob = array.array("f", bytes(4 * m))
        km = k - 1.0
        clipped = 0
        for i in range(m):
            r = r_plane[i]
            g = g_plane[i]
            b = b_plane[i]
            y = 0.299 * r + 0.587 * g + 0.114 * b
            r2 = r + km * (r - y)
            g2 = g + km * (g - y)
            b2 = b + km * (b - y)
            if r2 < 0.0:
                r2 = 0.0
                clipped += 1
            elif r2 > 1.0:
                r2 = 1.0
                clipped += 1
            if g2 < 0.0:
                g2 = 0.0
                clipped += 1
            elif g2 > 1.0:
                g2 = 1.0
                clipped += 1
            if b2 < 0.0:
                b2 = 0.0
                clipped += 1
            elif b2 > 1.0:
                b2 = 1.0
                clipped += 1
            orr[i] = r2
            og[i] = g2
            ob[i] = b2
        self._last_clip_pct = 100.0 * clipped / (3.0 * m)
        return orr.tobytes() + og.tobytes() + ob.tobytes()

    # ── **[A/B 钩子]** 只滤高频色度（网后空间降噪；`r<=0` 时逐位恒等）────────────────
    def _apply_chroma_denoise(self, buf: bytes) -> bytes:
        """**只滤高频色度**：保亮度、仅对色度做空间低通（分离式盒式模糊，边缘复制）。

        口径（非 config 键）：
            `Y  = 0.299R + 0.587G + 0.114B`（与链路 CSC 同族）
            `Cb = B − Y`、`Cr = R − Y`  →  **半径 `r` 的盒式低通**  →
            `R' = Y + Cr'`、`B' = Y + Cb'`、`G' = Y − (0.299·Cr' + 0.114·Cb') / 0.587`
        ⇒ **亮度逐像素不变**（保色比语义不受损），只削掉色度的**高频**（彩色噪点 / 彩斑）；
        与后续色度增益（线性）**可交换**，放在其**上游** ⇒ 增益作用在已平滑的色度上。

        `r ≤ 0` ⇒ **提前返回原对象 ⇒ 逐位恒等**（交付行为不变）。布局不变（3 通道面各
        `hq_content_h×hq_w` 个 float32）⇒ 下游 `to_rgb888` 无感。`_last_cdn_clip_pct` = 重建削顶率。

        **纯标准库**（交付环境无 numpy，承 §14 追加 10）；在 worker 线程内、每触发一次，不在帧路径上。
        复杂度 O(w·h)（running-sum 盒式、与 `r` 无关）。
        """
        r = self.chroma_denoise_r
        if r <= 0 or not buf:
            return buf
        a = array.array("f")
        a.frombytes(buf)
        w, h = self.hq_w, self.hq_content_h
        n = w * h
        if n == 0 or len(a) != 3 * n:
            return buf
        rp, gp, bp = a[:n], a[n:2 * n], a[2 * n:3 * n]
        yv = array.array("f", bytes(4 * n))
        cb = array.array("f", bytes(4 * n))
        cr = array.array("f", bytes(4 * n))
        for i in range(n):
            rr = rp[i]
            gg = gp[i]
            bb = bp[i]
            y = 0.299 * rr + 0.587 * gg + 0.114 * bb
            yv[i] = y
            cb[i] = bb - y
            cr[i] = rr - y
        inv = 1.0 / (2 * r + 1)
        for ch in (cb, cr):
            tmp = array.array("f", bytes(4 * n))
            for y0 in range(h):                      # 水平：逐行 running sum
                base = y0 * w
                s = ch[base] * r                     # 左侧越界 ⇒ 复制第 0 列
                for t in range(0, r + 1):
                    xx = t if t < w else w - 1
                    s += ch[base + xx]
                tmp[base] = s * inv
                for x in range(1, w):
                    add = x + r
                    if add >= w:
                        add = w - 1
                    sub = x - 1 - r
                    if sub < 0:
                        sub = 0
                    s += ch[base + add] - ch[base + sub]
                    tmp[base + x] = s * inv
            for x in range(w):                       # 垂直：逐列 running sum（写回 ch）
                s = tmp[x] * r                       # 上侧越界 ⇒ 复制第 0 行
                for t in range(0, r + 1):
                    yy = t if t < h else h - 1
                    s += tmp[yy * w + x]
                ch[x] = s * inv
                for y0 in range(1, h):
                    add = y0 + r
                    if add >= h:
                        add = h - 1
                    sub = y0 - 1 - r
                    if sub < 0:
                        sub = 0
                    s += tmp[add * w + x] - tmp[sub * w + x]
                    ch[y0 * w + x] = s * inv
        kr = 0.299 / 0.587
        kb = 0.114 / 0.587
        orr = array.array("f", bytes(4 * n))
        og = array.array("f", bytes(4 * n))
        ob = array.array("f", bytes(4 * n))
        clipped = 0
        for i in range(n):
            y = yv[i]
            b_c = cb[i]
            r_c = cr[i]
            r2 = y + r_c
            g2 = y - kr * r_c - kb * b_c
            b2 = y + b_c
            if r2 < 0.0:
                r2 = 0.0
                clipped += 1
            elif r2 > 1.0:
                r2 = 1.0
                clipped += 1
            if g2 < 0.0:
                g2 = 0.0
                clipped += 1
            elif g2 > 1.0:
                g2 = 1.0
                clipped += 1
            if b2 < 0.0:
                b2 = 0.0
                clipped += 1
            elif b2 > 1.0:
                b2 = 1.0
                clipped += 1
            orr[i] = r2
            og[i] = g2
            ob[i] = b2
        self._last_cdn_clip_pct = 100.0 * clipped / (3.0 * n)
        return orr.tobytes() + og.tobytes() + ob.tobytes()

    # ── worker ─────────────────────────────────────────────────────────
    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._pending and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                self._pending = False
                seq = self._pending_seq
            # 长耗时段（~8 s）：打包 + NPU 推理。**不持任何锁**，故主循环不受阻。
            rc, out, run_ns = E_NPU, b"", 0
            try:
                hq_rgb = _n.rga.hq(self.geom)
                if self.curve_src is not None:
                    hq_rgb = self._apply_input_curve(hq_rgb)
                _n.rknn_hq.pack_rgb888_nhwc_f32(hq_rgb, self._in_f32, self.hq_w, self.hq_h)
                out = bytearray(self.hq_w * self.hq_h * 3 * 4)
                rc = _n.rknn_hq.run(self._in_f32, out)
                run_ns = int(_n.rknn_hq.last_run_ns())
            except Exception as exc:
                # 线程内异常**不得静默吞掉**：记录后照常发 `hq_done`（done_ns=0 表示未产出结果），
                # 失败码经 `_last_rc` 由 `info()` 暴露。
                # **注**：`docs/logging.md` §4 未为 TH3 注册"失败"事件，而自造事件名违反规则
                # `20-contracts.md` §1 ⇒ 本版**不自造**；若需专用失败事件，须先走 §15 裁定并回写 logging.md。
                self._exc = str(exc)[:160]
            with self._mtx:
                self._last_rc = rc
                if rc == 0:
                    done_ns = time.monotonic_ns()
                    self._slot = {
                        # HqResult.buffer（模型 raw output 的**内容幅**：NCHW f32 [0,1]，已裁 pad 行）
                        # 链路末两级（均非契约步）：**先只滤高频色度**（保亮度）→ **再色度增益**。
                        "buffer": self._apply_output_chroma(
                            self._apply_chroma_denoise(
                                crop_nchw_f32_pad(out, self.hq_w, self.hq_h,
                                                  self.hq_pad_top, self.hq_content_h))),
                        "seq_src": int(seq),
                        "model_id": self._model_id,
                        "active_shape": SHAPE_ENUM.get(f"{self.hq_w}x{self.hq_h}", 0),
                        "done_ns": done_ns,
                        "shape": (self.hq_w, self.hq_content_h),
                    }
                    self._runs += 1
                    self._last_run_ns = run_ns
                else:
                    done_ns = 0                          # 未产出结果
                self._busy = False
            self.log.event("keyframe_infer", "hq_done", seq_src=int(seq),
                           model_id=self._model_id,
                           active_shape=SHAPE_ENUM.get(f"{self.hq_w}x{self.hq_h}", 0),
                           done_ns=done_ns)

    # ── §5-C 契约入口 ──────────────────────────────────────────────────
    def request(self, seq_src: int, frame: Optional[Dict[str, Any]] = None,
                pre_scaled: bool = False) -> int:
        """受理一帧 HQ 请求。**非阻塞**：同步只做 RGA 缩放（复制源帧），推理交 worker。

        `frame` = FrameDesc 的必要字段：`dmabuf_fd`（TH1 持有，本函数只读）、`seq`、`t_capture_ns`。
        返回 0 / E_STATE（未 init 或已有在途）/ E_NPU（RGA 失败）。

        **返回后调用方即可释放 lease**——RGA 缩放已在返回前完成（§5-C `request` 行）。

        **[A/B 钩子]（非契约入口）** `pre_scaled=True` = **跳过 RGA 缩放**：调用方已把 HQ 目标
        缓冲（`_n.rga.hq(geom)`，`hq_w×hq_h` RGB888）准备好——用于 §15 **O158** 的「N 帧时域
        平均」（平均结果由 native `mfa` 内核写回该缓冲）。此时 `frame` 可为 `None`。
        """
        if not self._inited:
            return E_STATE
        if not pre_scaled:
            fd = int(frame.get("dmabuf_fd", -1)) if frame else -1
            if fd < 0:
                return E_STATE
        with self._mtx:
            if self._busy:
                return E_STATE        # 已有在途（正常态下 TH1 的 FSM_HQ_BUSY 已拦住，此处为兜底）
            self._busy = True
        # ① 同步：RGA 缩放（触发暗帧 NV12 → hq.active_shape RGB888）—— 复制源帧，仅数毫秒
        if not pre_scaled:
            rc = _n.rga.blit_nv12_fd(fd, RGA_DST_HQ)
            if rc != 0:
                with self._mtx:
                    self._busy = False
                return E_NPU
        # ② 异步：唤醒 worker（打包 + NPU 推理，~8 s）
        with self._cv:
            self._pending = True
            self._pending_seq = int(seq_src)
            self._cv.notify()
        self.log.event("keyframe_infer", "hq_start", seq_src=int(seq_src),
                       model_id=self._model_id,
                       active_shape=SHAPE_ENUM.get(f"{self.hq_w}x{self.hq_h}", 0),
                       done_ns=0)                 # 尚未完成
        return 0

    def pop_result(self) -> Optional[Dict[str, Any]]:
        """取走最新结果（**取走后槽清空**）。无结果返回 None（契约的 `E_TIMEOUT` 语义）。"""
        with self._mtx:
            r = self._slot
            self._slot = None
            return r

    # **（v3.86 裁定 O102）** 原 `save_set_abcd()`（§5-C 签名）随「存图」功能整体取消而移除：
    # 其唯一路径是经 `realtime_apply.apply_full()` 做满幅 1920×1080 应用（§8.3 集合 B），
    # 而 `apply_full` 与 §8.3 均已随之取消 ⇒ **无对象，不保留空壳**。
