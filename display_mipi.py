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

import curve_estim as curve_mod
import log as starlog
import osd as osd_mod

E_DRM = -3
E_STATE = -5

# **（v3.86 裁定 O121）** DRM 取 master 的**有界重试**参数（**机制值，非 §10.4 的 config 键**）。
#
# 为什么需要：桌面（gnome-shell / Xorg）与本单元**互斥**占用 `card0` 的 DRM master，而
# `systemctl isolate multi-user.target` 的排序**只能保证 `gdm.service` 停完** —— Xorg 是由 gdm 经
# logind 拉起的**用户会话进程**（`gdm-x-session` / `session-*.scope`，**不在 `display-manager.service`
# 的 cgroup 内**）⇒ `graphical.target` 的 stop 并**不等它退出** ⇒ 本单元首拍会**抢在 master 释放之前**，
# 必然 `boot_check_item item=TH4 result=fail code=E_DRM`。
# 2026-09-22 实测两种场景：① **点桌面图标**后连点 4 次，前 3 次均「首拍失败 → `Restart` 2 s 后成功」，
# 屏上出现 2~4 s 黑档 ⇒ 用户误判为「单击无反应」（§15 O121）；② **开机首拍**亦失败一次、`Restart` 救回
# （§15 O118）。`Conflicts=graphical.target` + `After=graphical.target` 的排序方案**已实测无效并撤销**。
#
# 口径：**只对 `E_DRM` 重试**（其余错误立即返回）、**有界**（最多 `DRM_MASTER_RETRY_N` 次、间隔
# `DRM_MASTER_RETRY_MS` ms ⇒ 上限 ≈ 4 s）、全部失败仍返回 `E_DRM`（**返回码语义不变**）。
DRM_MASTER_RETRY_N = 8
DRM_MASTER_RETRY_MS = 500

# **（v3.86 裁定 O122）** 翻页失联的**有界自愈**参数（**机制值，非 §10.4 的 config 键**）。
#
# 为什么需要：内核控制台（`fbcon`）在 **VT 切换**时、桌面会话启停时、以及热插拔时，都会对 CRTC
# 重做 `SetCrtc`（实测两块 plane 都被挂到 `fb=225`，其 `allocated by = [fbcon]`）⇒ 此后本单元
# `drmModePageFlip` **持续失败**。旧实现把负返回值静默吞掉（`main.py` 只统计 `0` / `1`）
# ⇒ **画面永久停留、无日志、无自愈**（实测 49 min / 43786 帧只翻 5 帧，见 §15 O122）。
#
# 口径：**只对负值自愈**（`1` = 过期帧丢弃，属 §5 正常语义，不自愈）；每帧最多
# `DRM_DISP_RECOVER_N` 轮「重建 CRTC（`drm.recover`）+ 重试本帧翻页」；**返回码语义不变**
# （自愈成功仍返回 `0` / `1`，失败仍返回负值）。
DRM_DISP_RECOVER_N = 2


def _write_png_rgb888(path: str, rgb: bytes, w: int, h: int) -> int:
    """RGB888 紧凑缓冲 → PNG（8bit RGB，filter 0，**不做任何色调处理**）。

    与 `tools/realtime_frame_review.py` 的 `write_png` 同法（stdlib `zlib`，无第三方依赖）；
    放本模块以免交付链路依赖 `tools/` 下的脚本文件（脚本非可导入模块）。
    """
    import struct as _s
    import zlib as _z

    raw = bytearray()
    stride = w * 3
    for y in range(h):
        raw.append(0)                                  # filter type 0（None）
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + tag + data
                + (_z.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big"))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", _s.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", _z.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    try:
        with open(path, "wb") as f:
            f.write(png)
    except OSError:
        return E_DRM
    return 0


class DisplayMipi:
    """TH4 主体（单线程调用）。"""

    def __init__(self, cfg: Any, logger: starlog.Logger, card: str = "/dev/dri/card0"):
        self.cfg = cfg
        self.log = logger
        self.card = card
        self.canvas_w = 0
        self.canvas_h = 0
        self.geom: Optional[Dict[str, Any]] = None   # 几何单源（§15 O58；`init()` 里解析）
        self.nbuf = 2
        self.cur = 0
        self._inited = False
        self.master_retries = 0      # O121：`init()` 里取 master 实际用掉的重试次数（0 = 首拍成功）
        self._flips = 0
        self._skipped = 0
        # **（v3.86 裁定 O122）** 翻页失联 / 自愈的**边沿计数**（供 `main.py` 记 `degrade_action`，
        # **不新增日志事件 / 字段**）：`pageflip_lost` = 当前是否处于失联态；
        # `lost_edges` / `recover_edges` 各自只在**状态翻转时** +1。
        self.pageflip_lost = False
        self.lost_edges = 0
        self.recover_edges = 0
        self.osd: Optional[osd_mod.OsdRenderer] = None
        # §6.5 延迟两段分解：最近一次 flip 的（提交时刻 / vblank 时刻 / 取回的 seq / 本帧 t_capture_ns）
        # 各段 = submit − t_capture（采集→提交）与 flip − submit（提交→vblank），全部 CLOCK_MONOTONIC。
        # 命名带 `_lat_` 前缀，避免与公开方法 `last_flip_ns()` 同名遮蔽。
        self._lat_submit_ns = 0
        self._lat_flip_ns = 0
        self._lat_flip_seq = 0
        self._lat_t_capture_ns = 0
        self._last_idx = 0            # 最近一次已提交翻页的画布缓冲索引（供 TH5 镜像读源）
        self._fs_seq = 0              # 全屏态已绘制的 seq（内容静态：同一 seq 不重复贴图）

    def init(self, with_osd: bool = True, osd_status_only: bool = False,
             osd_no_stats: bool = False) -> int:
        # **几何单源（v3.86 就地裁定 O58）**：画布 / 两窗 / OSD 区 / 按钮区 rect 一律取自
        # `curve_estim.resolve_geometry(cfg)`；本模块**不再持有任何几何常量**。
        self.geom = curve_mod.resolve_geometry(self.cfg)
        canvas = self.cfg.require_final("display.mipi_canvas")
        self.canvas_w, self.canvas_h = int(canvas[0]), int(canvas[1])
        # **（v3.86 裁定 O121）取 DRM master 的**有界重试**（只对 `E_DRM`；见文件头常量注释）
        self.master_retries = 0
        rc = 0
        for attempt in range(DRM_MASTER_RETRY_N + 1):
            rc = _n.drm.configure({
                "card": self.card,
                "conn_id": 0,                     # 0 = 运行时按 connector 类型（DSI）解析，禁写死
                "canvas_w": self.canvas_w,
                "canvas_h": self.canvas_h,
                "nbuf": self.nbuf,
            })
            if rc == 0:
                self.master_retries = attempt     # 0 = 首拍即成功
                break
            if rc != E_DRM or attempt >= DRM_MASTER_RETRY_N:
                break                             # 非「取 master」类失败 / 重试用尽 ⇒ 立即返回
            time.sleep(DRM_MASTER_RETRY_MS / 1000.0)
        if rc != 0:
            return rc
        self._inited = True
        if with_osd:
            self.osd = osd_mod.OsdRenderer(self.cfg, self.canvas_w, self.canvas_h,
                                           status_only=osd_status_only, no_stats=osd_no_stats)
        return 0

    def shutdown(self) -> None:
        if self._inited:
            _n.drm.shutdown()
            self._inited = False

    def _flip(self, idx: int, seq: int) -> int:
        """翻页 + **有界自愈**（v3.86 裁定 O122）。返回语义与 `_n.drm.compose_flip` 逐字一致
        （**0** = 已翻页；**1** = 本帧被丢弃；**负值** = 错误）。

        负值（= `E_DRM`，与 RGA 错误码**同值** ⇒ 阶段只能由 `drm.info()["last_error"]` 区分）时：
        以当前画布 fb 重建 CRTC（`drm.recover`，不换 buffer ⇒ 不闪黑）并**重试本帧**，
        最多 `DRM_DISP_RECOVER_N` 轮；仍失败 ⇒ 原样返回负值，按既有口径交给调用方。
        失联 / 恢复各记一个**边沿**计数，供 `main.py` 记 `degrade_action`（**不新增事件 / 字段**）。
        """
        rc = _n.drm.compose_flip(idx, seq)
        if rc >= 0:
            if self.pageflip_lost:                 # 失联 → 恢复（外部 modeset 自行还原）
                self.pageflip_lost = False
                self.recover_edges += 1
            return rc
        healed = False
        for _ in range(DRM_DISP_RECOVER_N):
            if _n.drm.recover() != 0:
                break
            rc = _n.drm.compose_flip(idx, seq)
            if rc >= 0:
                healed = True
                break
        if healed:
            self.pageflip_lost = False
            self.recover_edges += 1                # 「已恢复」边沿
        elif not self.pageflip_lost:               # 「显示失联」边沿（失联态只记一次）
            self.pageflip_lost = True
            self.lost_edges += 1
        return rc

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
            # §3.5：**全屏 HQ 视图下 OSD 与按钮区隐藏**（常规视图照常）
            if str(st.get("view", "normal")) == "normal":
                self.osd.render_into(canvas, st)
            # O81：**光标每帧画进画布**（画在最上层；镜像下两屏都出现，属预期行为）
            self.osd.draw_cursor(canvas, st.get("cursor_pos"))

        rc = self._flip(idx, seq)
        if rc == 1:
            self._skipped += 1
            return 1
        if rc != 0:
            return rc
        self._last_idx = idx
        self.cur ^= 1
        self._flips += 1
        # §6.5：按 seq 回填配对（禁止按时间就近配对）。回调取回的 seq 与提交值不一致即配对失败，
        # 调用方据此计入丢弃、该帧不进入延迟统计。
        self._lat_submit_ns = int(_n.drm.last_submit_ns())
        self._lat_flip_ns = int(_n.drm.last_flip_ns())
        self._lat_flip_seq = int(_n.drm.last_flip_seq())
        self._lat_t_capture_ns = t_capture_ns if self._lat_flip_seq == seq else 0
        return 0

    def compose_fullscreen(self, hq_rgb, seq: int, hq_seq: int = 0) -> int:
        """**全屏 HQ 视图**（§3.5）：MIPI 画布 = 黑底 + HQ **1:1 居中**（左右 32 / 上下 30 黑边）。

        HQ 内容在 O90 冻结期内**静止** ⇒ 同一 `hq_seq` **只贴图一次**（贴完保持不清屏）；
        **OSD 与按钮区不画**（§3.5）。返回语义同 `compose_flip`（0 = 已翻页；1 = 本帧被丢弃）。
        """
        if not self._inited:
            return E_STATE
        g = self.geom
        px, py, pw, ph = (int(v) for v in g["hq_paste"])
        idx = self.cur
        canvas = _n.drm.canvas(idx, {"canvas_w": self.canvas_w, "canvas_h": self.canvas_h})
        if int(hq_seq) != self._fs_seq:
            src = hq_rgb.to_bytes() if hasattr(hq_rgb, "to_bytes") else bytes(hq_rgb)
            row = pw * 3
            if len(src) < ph * row:
                return E_STATE
            img = bytearray(self.canvas_w * self.canvas_h * 3)     # 整幅黑底（零填充 = §3.5 黑边）
            for y in range(ph):
                s = y * row
                d = ((py + y) * self.canvas_w + px) * 3
                img[d:d + row] = src[s:s + row]
            canvas.write(bytes(img))
            self._fs_seq = int(hq_seq)
        rc = self._flip(idx, int(seq))
        if rc == 1:
            self._skipped += 1
            return 1
        if rc != 0:
            return rc
        self._last_idx = idx
        self.cur ^= 1
        self._flips += 1
        return 0

    def last_flip_idx(self) -> int:
        """最近一次提交翻页的画布缓冲索引（TH5 镜像据此读源，避免读到未提交的那一页）。"""
        return self._last_idx

    def mark_busy(self, seq: int, text: str = "HQ PROCESSING ...") -> int:
        """触发受理瞬间的"处理中"提示（§3.5 / §6.2：**触发→OSD ≤50 ms**）。

        做法 = 复制**上一帧已翻页的画布**到另一缓冲、叠一行提示、翻页一次 ⇒ 冻结期内画面静止，
        提示常驻。返回 0 = 已翻页；负值 = 错误（HDMI/MIPI 主链路不受影响）。
        """
        if not self._inited:
            return E_STATE
        g = self.geom
        cfg = {"canvas_w": self.canvas_w, "canvas_h": self.canvas_h}
        prev = _n.drm.canvas(self._last_idx, cfg)
        nxt_idx = self._last_idx ^ 1
        nxt = _n.drm.canvas(nxt_idx, cfg)
        nxt.write(prev.to_bytes())            # 整幅复制（1024×600×3 ≈1.8 MB memcpy）
        if self.osd is not None:
            self.osd.text(nxt, 8, int(g["status_row_y"]) + 2, text, 0xFFD050)
        rc = self._flip(nxt_idx, int(seq))
        if rc == 0:
            self._last_idx = nxt_idx
            self.cur = self._last_idx ^ 1
            self._flips += 1
        return rc

    def dump_png(self, path: str, slot: int = 0, idx: Optional[int] = None) -> int:
        """把**链路原样**的合成画布导出为 PNG（**零色调后处理**；`slot` 0 = MIPI、1 = HDMI）。

        `idx` = 画布缓冲索引（缺省：MIPI 用最近翻页的那一页、HDMI 用 0）——由调用方按 TH5 的
        最近翻页索引传入，避免导出到"未提交"的那一页。口径 = 与审查工具同法（stdlib `zlib`，filter 0）。
        """
        if slot == 0:
            i = self._last_idx if idx is None else int(idx)
            canvas = _n.drm.canvas(i, {"canvas_w": self.canvas_w, "canvas_h": self.canvas_h})
            w, h = self.canvas_w, self.canvas_h
        else:
            g = self.geom
            w, h = int(g["hdmi_canvas_w"]), int(g["hdmi_canvas_h"])
            i = 0 if idx is None else int(idx)
            canvas = _n.drm.canvas_slot(1, i, {"canvas_w": w, "canvas_h": h})
        return _write_png_rgb888(path, canvas.to_bytes(), w, h)

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

    def _geom(self) -> Dict[str, Any]:
        """RGA 几何入参 = 几何单源（§15 O58：硬编码已删；`init()` 前调用则即时解析）。"""
        if self.geom is None:
            self.geom = curve_mod.resolve_geometry(self.cfg)
        return self.geom

    def info(self) -> Dict[str, Any]:
        d = dict(_n.drm.info())
        d["flips"] = self._flips
        d["flip_skipped"] = self._skipped
        return d

    def last_flip_ns(self) -> int:
        return int(_n.drm.last_flip_ns())

    def last_flip_seq(self) -> int:
        """最近一次 MIPI pageflip **回调取回**的 `seq`（§6.5 配对口径；供 `--seq-log` 明细）。"""
        return int(_n.drm.last_flip_seq())


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
