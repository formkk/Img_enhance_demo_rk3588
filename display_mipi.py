#!/usr/bin/env python3
"""display_mipi.py —— TH4：DRM DSI 画布合成 + OSD + 按钮纹理（§10.3 交付模块）

契约（§5 契约 C 节）：
    `compose_flip(const uint8* enh_win, const uint8* raw_win, const ControlState*) -> int`
    TH4；**过期帧丢弃**（上一 flip 未完成时不排队）。

硬约束：
  · **DRM 单 CRTC（DSI）**；connector / CRTC **一律运行时解析，禁止写死 crtc id**（§2.3、§11.1-9）；
  · 画布 = `DRM_FORMAT_RGB888`，左右双窗（左原图 / 右增强）+ OSD 指标区 + 按钮区（§3.4）；
  · **显示时间戳取 pageflip 完成的 vblank**（§6.5），不取提交时刻；
  · 无 HDMI 时不阻塞主链路（RSM 进 `MIPI_PREVIEW_ONLY`，规则 30 §2）。

取值来源：`display.mipi_canvas`（画布尺寸）、`rga.core_mask`、`ui.*`（OSD/按钮布局，M2 键）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import _starlight_native as _n

import log as starlog
import osd as osd_mod

E_DRM = -3
E_STATE = -5


class DisplayMipi:
    """TH4 主体（单线程调用）。"""

    def __init__(self, cfg: Any, logger: starlog.Logger, card: str = "/dev/dri/card0"):
        self.cfg = cfg
        self.log = logger
        self.card = card
        self.canvas_w = 0
        self.canvas_h = 0
        self.nbuf = 2
        self.cur = 0
        self._inited = False
        self._flips = 0
        self._skipped = 0
        self.osd: Optional[osd_mod.OsdRenderer] = None
        # §6.5 延迟两段分解：最近一次 flip 的（提交时刻 / vblank 时刻 / 取回的 seq / 本帧 t_capture_ns）
        # 各段 = submit − t_capture（采集→提交）与 flip − submit（提交→vblank），全部 CLOCK_MONOTONIC。
        # 命名带 `_lat_` 前缀，避免与公开方法 `last_flip_ns()` 同名遮蔽。
        self._lat_submit_ns = 0
        self._lat_flip_ns = 0
        self._lat_flip_seq = 0
        self._lat_t_capture_ns = 0

    def init(self, with_osd: bool = True) -> int:
        canvas = self.cfg.require_final("display.mipi_canvas")
        self.canvas_w, self.canvas_h = int(canvas[0]), int(canvas[1])
        rc = _n.drm.configure({
            "card": self.card,
            "conn_id": 0,                     # 0 = 运行时按 connector 类型（DSI）解析，禁写死
            "canvas_w": self.canvas_w,
            "canvas_h": self.canvas_h,
            "nbuf": self.nbuf,
        })
        if rc != 0:
            return rc
        self._inited = True
        if with_osd:
            self.osd = osd_mod.OsdRenderer(self.cfg, self.canvas_w, self.canvas_h)
        return 0

    def shutdown(self) -> None:
        if self._inited:
            _n.drm.shutdown()
            self._inited = False

    # ── §5 契约入口 ────────────────────────────────────────────────────
    def compose_flip(self, enh_win, raw_win, state: Optional[Dict[str, Any]] = None) -> int:
        """把两窗合成到画布并翻页。`enh_win`/`raw_win` 为 RGA 持有的 512×288 RGB888 缓冲。

        返回 0 = 已翻页；1 = **本帧被丢弃**（上一 flip 未完成，§5「过期帧丢弃」）；负值 = 错误。

        `state` 中的 `seq` / `t_capture_ns`（§5-B ControlState，O56 裁定新增）用于 §6.5 的
        **按 seq 回填配对**：seq 经 pageflip 的 user_data 透传、回调原样取回，与提交值比对；
        配对成功后由 `last_*` 三个时刻给出延迟两段分解。
        """
        if not self._inited:
            return E_STATE
        st = state or {}
        seq = int(st.get("seq", 0))
        t_capture_ns = int(st.get("t_capture_ns", 0))
        # 两窗由 RGA 缓冲持有；本函数的参数用于显式校验来源一致（防止传错缓冲）
        win = _n.rga.win(self._geom())
        enh = _n.rga.enh(self._geom())
        if raw_win is not None and raw_win is not win:
            win.write(raw_win)
        if enh_win is not None and enh_win is not enh:
            enh.write(enh_win)

        idx = self.cur
        canvas = _n.drm.canvas(idx, {"canvas_w": self.canvas_w, "canvas_h": self.canvas_h})
        rc = _n.rga.canvas_compose_to(canvas, self.canvas_w, self.canvas_h)
        if rc != 0:
            return rc
        if self.osd is not None:
            self.osd.render_into(canvas, st)

        rc = _n.drm.compose_flip(idx, seq)
        if rc == 1:
            self._skipped += 1
            return 1
        if rc != 0:
            return rc
        self.cur ^= 1
        self._flips += 1
        # §6.5：按 seq 回填配对（禁止按时间就近配对）。回调取回的 seq 与提交值不一致即配对失败，
        # 调用方据此计入丢弃、该帧不进入延迟统计。
        self._lat_submit_ns = int(_n.drm.last_submit_ns())
        self._lat_flip_ns = int(_n.drm.last_flip_ns())
        self._lat_flip_seq = int(_n.drm.last_flip_seq())
        self._lat_t_capture_ns = t_capture_ns if self._lat_flip_seq == seq else 0
        return 0

    def lat_pair(self) -> Optional[Dict[str, float]]:
        """最近一次成功配对的延迟两段分解（ms）；未配对返回 None（§6.5）。"""
        if self._lat_t_capture_ns == 0 or self._lat_flip_ns == 0:
            return None
        return {
            "seq": float(self._lat_flip_seq),
            "seg_capture_submit_ms": (self._lat_submit_ns - self._lat_t_capture_ns) / 1e6,
            "seg_submit_vblank_ms": (self._lat_flip_ns - self._lat_submit_ns) / 1e6,
            "total_ms": (self._lat_flip_ns - self._lat_t_capture_ns) / 1e6,
        }

    def _geom(self) -> Dict[str, int]:
        return {"cap_w": 1920, "cap_h": 1080,
                "eff_w": 480, "eff_h": 270, "pad_w": 480, "pad_h": 272, "pad_top": 1,
                "win_w": 512, "win_h": 288,
                "canvas_w": self.canvas_w, "canvas_h": self.canvas_h, "core_mask": 0}

    def info(self) -> Dict[str, Any]:
        d = dict(_n.drm.info())
        d["flips"] = self._flips
        d["flip_skipped"] = self._skipped
        return d

    def last_flip_ns(self) -> int:
        return int(_n.drm.last_flip_ns())


def main() -> int:
    """自检：需先取 DRM master（`sudo systemctl isolate multi-user.target`），结束恢复桌面。

    只验「connector 运行时解析 + 画布双缓冲 + pageflip」通路，不出真实画面。
    """
    import config as starlight_config

    cfg = starlight_config.load()
    logger = starlog.from_config(cfg)
    d = DisplayMipi(cfg, logger)
    rc = d.init(with_osd=False)
    if rc != 0:
        print(f"FAIL: drm.configure rc={rc}（取 master 失败？需先 systemctl isolate multi-user.target）")
        return 1
    info = d.info()
    print(f"connector={info['connector_id']} crtc={info['crtc_id']} mode={info['mode_name']} "
          f"{info['hdisplay']}x{info['vdisplay']}@{info['vrefresh']}")
    print("crtc/connector 均为运行时解析（禁写死 crtc id）")

    ok = 0
    for i in range(60):
        idx = i % 2
        canvas = _n.drm.canvas(idx, {"canvas_w": d.canvas_w, "canvas_h": d.canvas_h})
        # 交替填色，肉眼可见翻页
        canvas.write(bytes([(i * 4) % 256]) * (d.canvas_w * d.canvas_h * 3))
        # 本自检只验 DRM 通路：直连 `drm.compose_flip`，**不经** `DisplayMipi.compose_flip`
        # （后者需 RGA 已 configure 才能取窗缓冲，属交付链路路径，由 main.py 覆盖）
        if _n.drm.compose_flip(idx) == 0:
            ok += 1
        time.sleep(0.01)
    print(f"翻页 {ok}/60  {_n.drm.info()}")
    print(f"末次 vblank 时间戳 = {d.last_flip_ns()} mono_ns"
          f"（§6.5：显示时间戳取 vblank 绝对时刻，不取提交时刻）")
    d.shutdown()
    return 0 if ok >= 55 else 1


if __name__ == "__main__":
    raise SystemExit(main())
