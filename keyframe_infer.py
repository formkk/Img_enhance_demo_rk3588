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

import os
import threading
import time
from typing import Any, Dict, Optional

import _starlight_native as _n

import log as starlog

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
                 model_dir: str = "models/hq"):
        self.cfg = cfg
        self.log = logger
        self.geom = geom
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
            }

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
                        "buffer": crop_nchw_f32_pad(out, self.hq_w, self.hq_h,
                                                    self.hq_pad_top, self.hq_content_h),
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
    def request(self, seq_src: int, frame: Dict[str, Any]) -> int:
        """受理一帧 HQ 请求。**非阻塞**：同步只做 RGA 缩放（复制源帧），推理交 worker。

        `frame` = FrameDesc 的必要字段：`dmabuf_fd`（TH1 持有，本函数只读）、`seq`、`t_capture_ns`。
        返回 0 / E_STATE（未 init 或已有在途）/ E_NPU（RGA 失败）。

        **返回后调用方即可释放 lease**——RGA 缩放已在返回前完成（§5-C `request` 行）。
        """
        if not self._inited:
            return E_STATE
        fd = int(frame.get("dmabuf_fd", -1))
        if fd < 0:
            return E_STATE
        with self._mtx:
            if self._busy:
                return E_STATE        # 已有在途（正常态下 TH1 的 FSM_HQ_BUSY 已拦住，此处为兜底）
            self._busy = True
        # ① 同步：RGA 缩放（触发暗帧 NV12 → hq.active_shape RGB888）—— 复制源帧，仅数毫秒
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
