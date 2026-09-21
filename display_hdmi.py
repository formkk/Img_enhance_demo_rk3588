#!/usr/bin/env python3
"""display_hdmi.py —— TH5：HDMI 输出（热插拔 → RSM 接线）（§10.3 交付模块）

契约（§5 契约 C 节）：
    `publish(const HqResult*) -> int`     0 / E_DRM   TH5；事件驱动
    `on_hotplug(bool connected) -> int`   0 / E_DRM   触发 RSM 迁移（§5）

**本版实现范围（第①批 + 第②批，M2-1）**：
  · **热插拔探测 = sysfs 只读轮询**（`/sys/class/drm/<card>-<conn>/status`），路径由既有键
    `display.hdmi_connector` 拼接，**零新增键**；
  · 热插拔 → RSM 迁移：插入 → `RUN_DEMO`，拔出 → `MIPI_PREVIEW_ONLY`（§5 运行态 RSM）；
  · **（第②批）HDMI 画布 + 「未触发」态**：走 **DRM 单实例多 CRTC 的槽 1**
    （`drm.configure_slot(1, …)`，与 DSI 共用同一 master fd——master 是 per-fd，不可各开一份）；
    内容 = §3.5 的「未触发」态 = **黑底 + "等待触发"**，**写一次即保持不清屏**。
  · **探测不取 DRM master、无副作用** ⇒ 桌面态亦可运行；**HDMI 缺席是可降级项**
    （规则 `30-board-safety.md` §2），不阻塞主链路。

**未实现（已登记为偏离，见主基准 §15 与 `.trae/documents/TH5-HDMI-实现方案.md`）**：
  · `publish()`（O67）：本批**无 HQ 生产者**（`keyframe_infer` 属 M2-2），随 HQ 淡入一并推迟；
  · §3.5 的「处理中」角标 / 300 ms 淡入 / MIPI 全宽弹层（O68；依赖 `hq.*` 四键，定稿时点 = M3）。

状态机归属（v3.8 裁定 O3 = S2）：HDMI 显示**不另立状态机**，只用 §3.5 的状态表 + §5 的运行态 RSM。
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import _starlight_native as _n

import capture as capture_mod
import log as starlog
import osd as osd_mod

E_DRM = -3
E_STATE = -5

# 去抖：连续 N 次采样一致才采纳（低频步进下约 2 × 轮询周期）
DEBOUNCE_SAMPLES = 2

# DRM CRTC 槽：0 = DSI（TH4）、1 = HDMI（TH5）。与 `native/drm_core.h` 的取值一致。
DRM_SLOT_HDMI = 1


class DisplayHdmi:
    """TH5 主体（主循环低频步进调用）。

    §5 写 TH5 为"线程"，本实现为**主循环低频步进**（不建真线程）——此为**已登记偏离**。
    """

    def __init__(self, cfg: Any, logger: starlog.Logger, capture: Any,
                 card: str = "/dev/dri/card0"):
        self.cfg = cfg
        self.log = logger
        self.cap = capture                      # 持 `capture` 以触发 RSM（自检时可传 None）
        self.card = card
        card_name = os.path.basename(card)      # `/dev/dri/card0` → `card0`
        conn = str(cfg.require_final("display.hdmi_connector"))
        self.sysfs = f"/sys/class/drm/{card_name}-{conn}"
        self.status_path = f"{self.sysfs}/status"
        self.modes_path = f"{self.sysfs}/modes"
        # `display.hdmi_mode_selection`（M2-1 定稿 = `edid_prefer_1080p`）：构造期取，避免运行期抛错
        self._mode_policy = str(cfg.require_final("display.hdmi_mode_selection"))
        self.mode = "unknown"
        self._connected = False                 # 已采纳的物理状态
        self._pending: Optional[bool] = None    # 待确认的采样值
        self._pending_n = 0
        self._inited = False
        # ── 第②批：HDMI 画布（DRM 槽 1）────────────────────────────────
        self.canvas_w = 0
        self.canvas_h = 0
        self.nbuf = 2
        self._drm_ok = False                    # 槽 1 是否配置成功（失败则只做 RSM，不出图）
        self._idle_drawn = False                # 「未触发」态是否已写（写一次即保持，§3.5）
        self._flips = 0

    # ── 探测（纯只读 sysfs）────────────────────────────────────────────
    def probe(self) -> Optional[bool]:
        """读 sysfs `status`：`connected` → True、`disconnected` → False、读失败 → None（**不抛异常**）。"""
        try:
            with open(self.status_path, "r", encoding="ascii") as fp:
                raw = fp.read().strip()
        except OSError:
            return None
        if raw == "connected":
            return True
        if raw == "disconnected":
            return False
        return None

    def _select_mode(self) -> str:
        """按 `display.hdmi_mode_selection` 从 sysfs `modes`（= EDID mode list）选定结果。

        纯只读 sysfs：第①批**不碰 DRM**，故不调 `drmModeGetConnector`。未连接时 `modes` 为空
        ⇒ `unknown`（不伪造取值）。
        """
        try:
            with open(self.modes_path, "r", encoding="ascii") as fp:
                modes = [ln.strip() for ln in fp if ln.strip()]
        except OSError:
            return "unknown"
        if not modes:
            return "unknown"
        if self._mode_policy == "edid_prefer_1080p" and "1920x1080" in modes:
            return "1920x1080"
        return modes[0]

    # ── 生命周期 ───────────────────────────────────────────────────────
    def init(self) -> int:
        """探测一次并按结果接线：插入 → `RUN_DEMO`，否则 → `MIPI_PREVIEW_ONLY`。

        HDMI 缺席是**可降级项**（规则 `30-board-safety.md` §2）：本函数不因 HDMI 未接而失败。
        """
        cur = self.probe()
        if cur is None:
            cur = False                         # 读失败按未连接处理（不抛异常）
        self._pending, self._pending_n = None, 0
        rc = self.on_hotplug(cur)
        self._inited = True
        return rc

    def shutdown(self) -> None:
        """幂等清理。第①批**无 DRM 资源**（不碰 DRM），仅置位。"""
        self._inited = False
        self._pending, self._pending_n = None, 0

    # ── §5 契约入口 ────────────────────────────────────────────────────
    def poll(self) -> int:
        """**低频步进**入口：连续 `DEBOUNCE_SAMPLES` 次采样一致才采纳（去抖）；变化时才调 `on_hotplug`。"""
        if not self._inited:
            return E_STATE
        cur = self.probe()
        if cur is None:
            cur = False                         # 读失败按 disconnected 处理（不抛异常）
        if cur == self._connected:
            self._pending, self._pending_n = None, 0
            return 0
        self._pending_n = self._pending_n + 1 if cur == self._pending else 1
        self._pending = cur
        if self._pending_n < DEBOUNCE_SAMPLES:
            return 0
        self._pending, self._pending_n = None, 0
        rc = self.on_hotplug(cur)
        # 插回时补配 HDMI 画布（拔插后 CRTC 可能已失效；未接时 `init_canvas` 自行跳过）
        if cur and rc == 0:
            self.init_canvas()
        return rc

    def on_hotplug(self, connected: bool) -> int:
        """§5-C 签名：`connected` → `RUN_DEMO`（`reason=hdmi_ok`）；否则 → `MIPI_PREVIEW_ONLY`（`reason=hdmi_out`）。

        RSM 迁移由 `capture.set_rsm` 内部发 `main/rsm_transition`（**本模块不自造事件**）。
        `connected=` 记录**物理动作结果**（与拔插一致），故无论迁移是否受理都更新采纳态；
        迁移未被受理（如处于 `SHUTDOWN` / `FAULT_EXIT`）时返回 `E_DRM`。
        """
        target = capture_mod.RSM_RUN_DEMO if connected else capture_mod.RSM_MIPI_PREVIEW_ONLY
        reason = "hdmi_ok" if connected else "hdmi_out"
        rc = 0
        if self.cap is not None:
            rc = int(self.cap.set_rsm(target, reason=reason))
        self.mode = self._select_mode() if connected else "unknown"
        self.log.event("display_hdmi", "hotplug", connected=connected, mode=self.mode)
        self._connected = connected
        # 插回后需重画「未触发」态（拔插期间 CRTC 可能已重新 modeset、画布内容不保证保留）
        if connected:
            self._idle_drawn = False
        return 0 if rc == 0 else E_DRM

    # ── 第②批：HDMI 画布（DRM 槽 1）──────────────────────────────────
    def init_canvas(self) -> int:
        """配置 HDMI 画布（DRM **槽 1**）并画一次「未触发」态（§3.5）。

        **前提**：DSI（槽 0）已由 TH4 配置——本函数**复用同一 master fd**（master 是 per-fd）。
        **HDMI 缺席是可降级项**：未连接时**不配置 CRTC**（无 EDID ⇒ 无可用 mode），
        直接返回 `0`（`_drm_ok = False`，只做 RSM，不出图），**不阻塞主链路**。
        """
        if self._drm_ok:
            # 已配置过（如插回）：无需重新 configure（DRM 不允许重复配置同一槽），
            # 但「未触发」态需重画一次（拔插后内容不保证保留）
            return self.draw_idle()
        if not _n.drm.is_configured():
            # 槽 0 未就绪 ⇒ 无 master fd 可复用，跳过（不自行取 master：会与 TH4 争）
            return 0
        if self.probe() is not True:
            return 0                            # 未连接：无可选 mode，跳过（可降级）
        canvas = self.cfg.require_final("display.hdmi_canvas")
        self.canvas_w, self.canvas_h = int(canvas[0]), int(canvas[1])
        rc = _n.drm.configure_slot(DRM_SLOT_HDMI, {
            "card": self.card,
            "conn_id": 0,                       # 0 = 运行时按 connector 类型（HDMI-A）解析
            "canvas_w": self.canvas_w,
            "canvas_h": self.canvas_h,
            "nbuf": self.nbuf,
        })
        if rc != 0:
            # HDMI 是可降级项（规则 30 §2）：槽 1 配置失败**不阻塞主链路**，也不改 RSM
            # （RSM 由 `on_hotplug` 依 sysfs 驱动，与本函数解耦）
            return 0
        self._drm_ok = True
        self._idle_drawn = False
        return self.draw_idle()

    def draw_idle(self) -> int:
        """画 §3.5 的「未触发」态 = **黑底 + "等待触发"**，然后 pageflip 一次。

        **写一次即"保持不清屏"**（§3.5 语义），无需每帧重绘——由 `_idle_drawn` 守卫。
        """
        if not self._drm_ok or self._idle_drawn:
            return 0
        cfg = {"canvas_w": self.canvas_w, "canvas_h": self.canvas_h}
        canvas = _n.drm.canvas_slot(DRM_SLOT_HDMI, 0, cfg)
        # 黑底（画布在 configure 时已整幅清黑一次，此处显式再填一次以保证插回后干净）
        _n.osd.fill_rect(canvas, self.canvas_w, self.canvas_h, 0, 0,
                         self.canvas_w, self.canvas_h, 0x000000)
        # "等待触发"居中：复用 osd 的字形原语与字体（布局由本模块决定，§10.3）
        font = osd_mod._find_font()
        if font is not None:
            text = "WAITING FOR TRIGGER"
            scale = max(1, int(round(self.canvas_h / 24 / font.h)))   # 约 1/24 画布高
            tw = len(text) * font.w * scale
            x = max(0, (self.canvas_w - tw) // 2)
            y = max(0, (self.canvas_h - font.h * scale) // 2)
            cx = x
            for ch in text:
                _n.osd.blit_glyph(canvas, self.canvas_w, self.canvas_h, cx, y, scale,
                                  font.glyph(ch), font.w, font.h, 0xC8C8C8)
                cx += font.w * scale
        rc = _n.drm.compose_flip_slot(DRM_SLOT_HDMI, 0)
        if rc == 0:
            self._flips += 1
            self._idle_drawn = True
        return rc

    def info(self) -> dict:
        """HDMI 槽的只读状态（供上报；**不参与** §6.5 的 MIPI 延迟统计）。"""
        d = dict(_n.drm.info_slot(DRM_SLOT_HDMI))
        d["drm_ok"] = self._drm_ok
        d["idle_drawn"] = self._idle_drawn
        d["flips"] = self._flips
        return d


def main() -> int:
    """自检（**纯只读 sysfs，无副作用**）：打印 connector 状态与 EDID 选定结果。

    不取 DRM master、不建 DRM 资源 ⇒ 桌面态亦可直接跑。§11.2 M2-1 ① 的端到端拔插判据
    由 `python3 main.py --frames 300` 覆盖。
    """
    import config as starlight_config

    cfg = starlight_config.load()
    logger = starlog.from_config(cfg)
    d = DisplayHdmi(cfg, logger, None)
    print(f"sysfs: {d.status_path}（策略 {d._mode_policy}）")
    ok = 0
    for _ in range(10):
        st = d.probe()
        name = "connected" if st is True else ("disconnected" if st is False else "unreadable")
        print(f"  status={name}  mode={d._select_mode()}")
        if st is not None:
            ok += 1
        time.sleep(0.2)
    print(f"可读 {ok}/10（读失败按 disconnected 处理，不抛异常）")
    return 0 if ok == 10 else 1


if __name__ == "__main__":
    raise SystemExit(main())
