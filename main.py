#!/usr/bin/env python3
"""main.py —— 交付链路主程序：线程编排 + 启动序/退出序（§10.3、§5、§10.6-B）

线程（§5 线程模型）：
    TH1  采集/控制   capture.py      —— `QBUF` 唯一持有者、`seq` 分配者、lease 签发者
    TH2a 曲线估计    curve_estim.py  —— RGA + ZDE++ 双条带 + EMA，5~15 Hz
    TH2b 逐帧应用    realtime_apply  —— 8 iter 单遍融合（C/NEON）
    TH4  MIPI 显示   display_mipi.py —— DRM 单 CRTC 画布合成 + OSD + pageflip
    TH5  HDMI 输出   display_hdmi.py —— 热插拔监测 + RSM 接线（M2-1 第①批；无 HDMI 时进
                                       `MIPI_PREVIEW_ONLY`）。§5 写 TH5 为"线程"，实现为
                                       **主循环低频步进**（已登记偏离）；`publish()` 随 HQ 推迟

**启动序（§5）**：TH4（DSI/CRTC 就绪）→ TH2 → TH1（全生命周期只 stream on 一次）→ TH5
**退出序（§5）**：先停发新 lease → 等在途 lease/fence 有界清空 → TH1 `STREAMOFF` 并释放 DMABUF 池
                → TH4 释放 CRTC/plane
**软复位（O26）**：只做退出序前两段，**不 STREAMOFF、不释放池与 CRTC/plane**。

运行模式（§10.6-B，**不设 `runtime.mode` 键**）：
    演示 = systemd 拉起本文件；bench = SSH 前台跑 `tools/bench.py`；调试 = 前台 + `logging.level=DEBUG`
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from typing import Any, Dict, Optional

import _starlight_native as _n

import capture as capture_mod
import config as starlight_config
import curve_estim as curve_mod
import display_hdmi as hdmi_mod
import display_mipi as display_mod
import keyframe_infer as kf_mod
import log as starlog
import realtime_apply as ra
import ui_buttons as ui_mod

# 曲线场精度档 → 扩展取值
WIN_W, WIN_H = curve_mod.WIN_W, curve_mod.WIN_H


class Demo:
    def __init__(self, cfg: Any, logger: starlog.Logger, frames: int = 0, est_every: int = 6,
                 hdmi_every: int = 6, trigger_at: int = 0):
        self.cfg = cfg
        self.log = logger
        self.max_frames = frames
        self.est_every = max(1, est_every)
        # TH5 热插拔低频步进周期（帧）：与 `est_every` 同风格；6 帧 ≈ 200 ms @30 fps
        self.hdmi_every = max(1, hdmi_every)
        # **[验证钩子]** 第 N 帧注入一次 TOUCH 触发（0 = 不注入）；生产触发源是触控 evdev
        self.trigger_at = trigger_at
        self._triggered = False
        self.stop = threading.Event()
        self.cap: Optional[capture_mod.Capture] = None
        self.est: Optional[curve_mod.CurveEstimator] = None
        self.disp: Optional[display_mod.DisplayMipi] = None
        self.hdmi: Optional[hdmi_mod.DisplayHdmi] = None
        self.kf: Optional[kf_mod.KeyframeInfer] = None
        self.buttons: Optional[ui_mod.ButtonHit] = None
        self.curve = None
        self.counters: Dict[str, Any] = {"frames": 0, "estimates": 0, "flips": 0,
                                         "flip_skipped": 0, "dropped_stale": 0,
                                         "lease_timeouts": 0, "discarded_triggers": 0,
                                         "hq_started": 0, "hq_done": 0, "hq_flips_delta": 0}
        # §6.5 延迟两段分解 + 按 seq 回填配对：`lat` = 端到端总计（采集→vblank），
        # `lat_app` = 采集→提交（应用侧），`lat_submit` = 提交→vblank（显示排队）。
        self.lat: list = []
        self.lat_app: list = []
        self.lat_submit: list = []
        self.pair_ok = 0

    # ── 启动序 ─────────────────────────────────────────────────────────
    def boot(self) -> int:
        cfg = self.cfg
        self.log.event("main", "version_triplet",
                       toolkit2=cfg.get("runtime.toolkit2_ver"),
                       librknnrt=cfg.get("runtime.librknnrt_ver"),
                       rknpu_driver=cfg.get("runtime.rknpu_driver_ver"))

        # ① TH4：DSI/CRTC 就绪
        self.disp = display_mod.DisplayMipi(cfg, self.log)
        rc = self.disp.init(with_osd=True)
        if rc != 0:
            self.log.event("main", "boot_check_item", item="TH4", result="fail",
                           code=starlog.code_name(rc))
            return rc
        self.buttons = ui_mod.ButtonHit(cfg)

        # ② TH2：曲线估计（RGA + NPU）
        self.est = curve_mod.CurveEstimator(cfg, self.log)
        rc = self.est.init()
        if rc != 0:
            self.log.event("main", "boot_check_item", item="TH2a", result="fail",
                           code=starlog.code_name(rc))
            return rc
        self.est.alpha = float(cfg.require_final("pipeline.ema_alpha"))

        # 曲线应用内核（§5 GAP-1：启动时一次性注入几何）
        ra.shutdown()
        rc = ra.configure_from_config(cfg)
        if rc != 0:
            self.log.event("main", "boot_check_item", item="realtime_apply", result="fail",
                           code=starlog.code_name(rc))
            return rc

        # ③ TH1：只 stream on 一次
        self.cap = capture_mod.Capture(cfg, self.log)
        rc = self.cap.init()
        if rc != 0:
            self.log.event("main", "boot_check_item", item="TH1", result="fail",
                           code=starlog.code_name(rc))
            return rc
        rc = self.cap.stream_on_once()
        if rc != 0:
            return rc

        # ④ TH5：HDMI 存在则启用，否则 `MIPI_PREVIEW_ONLY`（§5 启动序末位）
        # 热插拔探测走 sysfs 只读轮询（不碰 DRM 探测）。`init()` 内含首次探测 + RSM 接线，
        # 发 `hotplug` 与 `rsm_transition`（不自造事件）。HDMI 是可降级项（规则 30 §2）。
        self.hdmi = hdmi_mod.DisplayHdmi(cfg, self.log, self.cap)
        self.hdmi.init()
        # 第②批：HDMI 画布走 DRM 槽 1，**复用 TH4 已取得的 master fd**（master 是 per-fd）
        # ⇒ 必须在 `self.disp.init()` 之后调用。失败/未接均不阻塞主链路。
        self.hdmi.init_canvas()

        # ⑤ TH3：关键帧（HQ）推理。**独立 OS 线程**（§5 原文口径）——HQ 单次 ~8 s，
        # 若内联在主循环会让 MIPI 冻结 8 s，违反 §5「TH4 不得因结果槽空而锁帧」。
        # 前置：RGA 已 configure（TH2 的 `est.init()` 里唯一一次，含 hq_w/hq_h 目标缓冲）。
        # 失败（如模型缺失）**不阻塞主链路**——触发时 `request` 会返 E_STATE。
        self.kf = kf_mod.KeyframeInfer(cfg, self.log, self.est.geom)
        rc = self.kf.init()
        if rc != 0:
            self.log.event("main", "boot_check_item", item="TH3", result="fail",
                           code=starlog.code_name(rc))
        return 0

    # ── 主循环（TH2b + 编排；TH2a 与 TH4 在此串行推进，M2 单进程内可接受）──
    def run(self) -> int:
        assert self.cap and self.est and self.disp
        g = self.est.geom
        win = _n.rga.win(g)
        enh = _n.rga.enh(g)
        i = 0
        while not self.stop.is_set():
            t0 = time.monotonic_ns()
            f = self.cap.acquire(1, 500)
            if f is None:
                c = self.cap.counters()
                self.counters["lease_timeouts"] = c["lease_timeouts"]
                continue
            self.counters["frames"] += 1

            # 原图窗（RGA：NV12 → 512×288 RGB888）
            _n.rga.blit_nv12_fd(int(f["dmabuf_fd"]), 1)

            # TH2a：低频估计（每 est_every 帧一次）
            if i % self.est_every == 0:
                if self.est.estimate(f, alpha=self.est.alpha) == 0:
                    self.curve = self.est.publish()
                    self.counters["estimates"] += 1

            # TH5：低频热插拔步进（每 hdmi_every 帧一次；§5 写"线程"⇒ 已登记偏离）
            if self.hdmi is not None and i % self.hdmi_every == 0:
                self.hdmi.poll()

            # **[验证钩子]** 第 N 帧注入一次 TOUCH 触发（生产触发源 = 触控 evdev）
            if self.trigger_at and not self._triggered and self.counters["frames"] >= self.trigger_at:
                self._triggered = True
                self.cap.submit_trigger(capture_mod.SRC_TOUCH)

            # TH3 编排（§5）：① 消抖期满则受理触发 → 用**本帧**作触发暗帧投递 HQ；
            # ② HQ 完成（worker 空闲）则回 IDLE。**`request` 只同步做 RGA 缩放**（复制源帧），
            # 推理在 TH3 worker 线程内跑（~8 s），故此处**不阻塞主循环**。
            ev = self.cap.poll_trigger()
            if ev is not None and self.kf is not None:
                # `request` 返回后本帧即可释放 lease（§5-C：先复制源帧再释放）
                rc_kf = self.kf.request(ev["seq_src"], f)
                if rc_kf != 0:
                    self.log.event("main", "boot_check_item", item="TH3_request",
                                   result="fail", code=starlog.code_name(rc_kf))
                    self.cap.hq_done()          # 受理失败即回 IDLE，避免 FSM 卡在 HQ_BUSY
                else:
                    self.counters["hq_started"] += 1
                    self._hq_flips_at_start = self.counters["flips"]
            elif self.cap.fsm == capture_mod.FSM_HQ_BUSY and self.kf is not None \
                    and not self.kf.busy():
                self.cap.hq_done()              # HQ 完成 → 回 IDLE
                self.counters["hq_done"] += 1
                # §5 关键约束的证据：HQ 在途期间 MIPI 仍翻页（TH4 未被冻结）
                self.counters["hq_flips_delta"] = (
                    self.counters["flips"] - getattr(self, "_hq_flips_at_start", 0))

            # 释放 lease（TH1 引用计数归零后 QBUF 回池）
            self.cap.release(f["lease_id"])

            # TH2b：8 iter 单遍融合（交付内核）
            if self.curve is not None:
                ra.apply_window(win, self.curve.field, enh, WIN_W, WIN_H)

            # 统计（降采样 4× + 降频 1 Hz；`Stats` 内部自行节流，故可逐帧调用）
            if self.disp.osd is not None and self.curve is not None:
                self.disp.osd.update_stats(win.to_bytes(), enh.to_bytes(), WIN_W, WIN_H)

            # TH4：合成 + OSD + pageflip
            st = {
                "mode": self.cap.rsm,
                "fps": self._fps(),
                "lat_capture_ms": 0.0,
                "lat_submit_ms": (time.monotonic_ns() - t0) / 1e6,
                "curve_hz": self.est.f_est,
                "countdown_s": 0,
                "seq": int(f["seq"]),                 # §5-B ControlState（O56）：供 TH4 seq 回填配对
                "t_capture_ns": int(f["t_capture_ns"]),
                "mean_orig": self.disp.osd.stats.mean_orig if self.disp.osd else 0.0,
                "mean_enh": self.disp.osd.stats.mean_enh if self.disp.osd else 0.0,
                "curve_field": self.curve.field,
                "curve_w": g["eff_w"], "curve_h": g["eff_h"],
                "curve_precision": self.est.precision(),
            }
            r = self.disp.compose_flip(enh, win, st)
            if r == 0:
                self.counters["flips"] += 1
                lp = self.disp.lat_pair()
                if lp is not None:
                    self.lat.append(lp["total_ms"])
                    self.lat_app.append(lp["seg_capture_submit_ms"])
                    self.lat_submit.append(lp["seg_submit_vblank_ms"])
                    self.pair_ok += 1
            elif r == 1:
                self.counters["flip_skipped"] += 1

            i += 1
            if self.max_frames and i >= self.max_frames:
                break

        c = self.cap.counters()
        self.counters["dropped_stale"] = c["dropped_stale"]
        self.counters["lease_timeouts"] = c["lease_timeouts"]
        self.counters["discarded_triggers"] = c["discarded_triggers"]
        return 0

    def _fps(self) -> float:
        return self.counters["frames"] / max(1e-6, time.monotonic() - self._t_start) \
            if getattr(self, "_t_start", None) else 0.0

    def _lat_pct(self, p: float, data: Optional[list] = None) -> float:
        s = sorted(self.lat if data is None else data)
        if not s:
            return 0.0
        return s[min(len(s) - 1, int(len(s) * p))]

    # ── 退出序 ─────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        self.stop.set()
        # TH5 无 DRM 资源（第①批不碰 DRM），先停步进
        if self.hdmi:
            self.hdmi.shutdown()
        # TH3：停 worker 线程并释放 HQ 模型（**在途推理会被等完**，最长 ~8 s、有界）。
        # 必须在 TH1 停流之前——`request` 只做 RGA 缩放（已复制源帧），停流后不再有新请求。
        if self.kf:
            self.kf.shutdown()
        # ① 停发新 lease ② 等在途 lease/fence 有界清空
        if self.cap:
            self.cap.soft_reset()
        # ③ TH1 STREAMOFF + 释放 DMABUF 池
        if self.cap:
            self.cap.shutdown()
        # ④ TH4 释放 CRTC/plane
        if self.est:
            self.est.shutdown()
        if self.disp:
            self.disp.shutdown()
        ra.shutdown()


def main() -> int:
    p = argparse.ArgumentParser(description="RK3588 极暗光增强实时演示 —— 交付链路主程序")
    p.add_argument("--frames", type=int, default=0, help="跑 N 帧后退出（0 = 直到信号）")
    p.add_argument("--est-every", type=int, default=6, help="每 N 帧估计一次")
    p.add_argument("--hdmi-every", type=int, default=6, help="每 N 帧做一次 HDMI 热插拔步进")
    # **验证钩子**（非生产路径）：在第 N 帧注入一次 `TOUCH` 触发，用于验证 TH3 通路。
    # 生产触发源 = 触控 evdev（§5 TH1「四源共用一套触发状态机」），其接线尚未落地。
    p.add_argument("--trigger-at", type=int, default=0, help="[验证钩子] 第 N 帧注入一次 TOUCH 触发")
    p.add_argument("--config", default="", help="config.yaml 路径（默认仓库根）")
    args = p.parse_args()

    cfg = starlight_config.load(args.config or None)
    logger = starlog.from_config(cfg)
    d = Demo(cfg, logger, frames=args.frames, est_every=args.est_every,
             hdmi_every=args.hdmi_every, trigger_at=args.trigger_at)

    def on_sig(signum, _frame):
        logger.event("main", "service_exit", exit_code=0, uptime_s=0)
        d.stop.set()

    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    logger.event("main", "service_start", exit_code=0, uptime_s=0)
    t0 = time.monotonic()
    d._t_start = t0
    rc = d.boot()
    if rc != 0:
        print(f"启动失败 rc={rc}（TH4 需 DRM master：先 `systemctl isolate multi-user.target`）")
        d.shutdown()
        return 1
    try:
        rc = d.run()
    finally:
        # TH3 的 `info()` 须在 `shutdown()` **之前**取（shutdown 会 deinit 模型、置 `_th=None`）
        th3_info = d.kf.info() if d.kf is not None else None
        d.shutdown()

    c = d.counters
    wall = max(1e-6, time.monotonic() - t0)
    print(f"frames={c['frames']} estimates={c['estimates']} flips={c['flips']} "
          f"flip_skipped={c['flip_skipped']} dropped_stale={c['dropped_stale']} "
          f"lease_timeouts={c['lease_timeouts']} discarded_triggers={c['discarded_triggers']}")
    print(f"wall={wall:.2f}s  fps={c['frames'] / wall:.1f}")
    # §6.5 延迟口径：按 `seq` 回填配对（禁止按时间就近配对）；配对率与丢弃帧数必须随报告给出
    paired = d.pair_ok
    print(f"配对率={paired / max(1, c['flips']):.4f}"
          f"（已配对 {paired} / flip {c['flips']}；未回填丢弃 {c['flips'] - paired}；"
          f"过期帧丢弃 {c['flip_skipped']}）")
    print(f"延迟·采集→提交（应用侧）    P50={d._lat_pct(0.50, d.lat_app):.1f} ms  "
          f"P95={d._lat_pct(0.95, d.lat_app):.1f} ms")
    print(f"延迟·提交→vblank（显示排队）P50={d._lat_pct(0.50, d.lat_submit):.1f} ms  "
          f"P95={d._lat_pct(0.95, d.lat_submit):.1f} ms")
    print(f"延迟·端到端（采集→vblank）  P50={d._lat_pct(0.50):.1f} ms  "
          f"P95={d._lat_pct(0.95):.1f} ms")
    hz = float(d.cfg.require_final("display.panel_refresh_hz"))
    print(f"实测面板刷新周期={1e3 / hz:.2f} ms（{hz:g} Hz）")
    base = d.cfg.get("metrics.idle_link_baseline")
    print(f"M1 空链路基线={base if base else '缺失（M1 并行产出）'} ⇒ 超标归因尚不可闭合（§6.5）")
    print(f"f_est={d.est.f_est:.2f} Hz  alpha={d.est.alpha:.4f}")
    print(f"stream_on_count={_n.capture.stream_on_count()}（契约：恒为 1）")
    # TH3：HQ 触发通路（§5）。`hq_flips_delta` = HQ 在途期间 MIPI 的翻页数——
    # **非 0 即证明 TH4 未被 HQ 冻结**（§5「TH4 不得因结果槽空而锁帧」）。
    if d.kf is not None:
        print(f"TH3: hq_started={c['hq_started']} hq_done={c['hq_done']} "
              f"hq_flips_delta={c['hq_flips_delta']}（>0 ⇒ HQ 在途期间 MIPI 仍翻页）")
        print(f"TH3 info（shutdown 前）: {th3_info}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
