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

# **（v3.87 追加 37 / DCM 演示路进度带）** 冻结期居中进度带的**边线色**（RGB888）：
# 取 OSD 既有琥珀色（`osd.C_CAPTION` 同值 0xE0B040——OSD 色板中未使用的暖色系，
# 与数据标记和状态行文字均可区分）；本模块不 import osd 的常量（避免环依赖），故照值复刻。
OVL_EDGE = 0xE0B040

# **（v3.88 界面优化 / HQ 全屏叠层）** 全屏 HQ 结果图上的**中文说明叠层**的像素常量
# （**机制值，非 §10.4 的 config 键**）；图像 rect 取自几何单源 `hq_paste`（960×540 居中）。
# **（2026-10-14 用户裁定）** 卡片中文 28 → **24 px**；**底部「处理链」整行取消**
# ⇒ `HQ_FOOT_H` 与底部绘制块一并删除；**「提升倍数」「灰度熵」「清晰度」删除**，卡片最终**三行**
# 无参考指标（亮度均值 / 暗部占比 / 饱和度）⇒ 340×170 → **300×144**；**数值改用小一号 PSF**
# （`osd.text_small`）使墨迹与 24 px 中文等高。
HQ_OVL_PAD = 8         # 左上角标签 / 右下角指标卡与图像边缘的内缩（px）
HQ_CARD_W = 300        # 右下角指标卡宽（px；= 最宽一行「暗部占比 62% -> 18%」+ 两侧内边距）
HQ_CARD_H = 144        # 右下角指标卡高（px；= 三行文字 + 直方图 + 上下内边距）

# **（2026-10-14 用户裁定）** DCM 全屏（第三视图态）「压缩·带宽」卡片的像素常量
# （机制值，非 §10.4 的 config 键）；卡片 = **三行文字**、**无直方图**。
DCM_CARD_W = 320       # 宽（px；= 最宽一行「压缩/节省 61x / 98.4%」+ 两侧内边距）
DCM_CARD_H = 100       # 高（px；= 三行 × 28 + 上下内边距；**2026-10-14 依次去掉「特征FP16」
#                       与「检出(解码特征)」两行** ⇒ 五行 156 → 四行 128 → **三行 100**）
DCM_FOOT_FG = 0x50E050  # 图像底部居中一行（黑底**绿字**）的前景色（= OSD 调色板 `C_G` 同值）
DCM_FOOT_H = 40        # 该行的黑底条高度（px；25 px 墨迹上下各留 7~8 px）


def _fmt_kbmb(n: int) -> str:
    """字节数 → 紧凑文本（**演示口径：1 KB = 1024 B、1 MB = 1024 KB**）。"""
    if n >= 1 << 20:
        return "%.2fMB" % (n / float(1 << 20))
    if n >= 1 << 10:
        return "%.1fKB" % (n / float(1 << 10))
    return "%dB" % n


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
        # **MIPI 面板白平衡校正**（`--mipi-wb-preset`；**默认 None = 关闭 = 逐位不变**）：
        # 只作用于 **DSI 槽画布**（HDMI 槽不受影响）；内核 = `_n.wb`（纯整数查表）。
        # **非契约入口**；语义 = 线性域 per-channel 增益（见 `native/wb_core.h`）。
        self._wb_lut: Optional[bytes] = None
        # §6.5 延迟两段分解：最近一次 flip 的（提交时刻 / vblank 时刻 / 取回的 seq / 本帧 t_capture_ns）
        # 各段 = submit − t_capture（采集→提交）与 flip − submit（提交→vblank），全部 CLOCK_MONOTONIC。
        # 命名带 `_lat_` 前缀，避免与公开方法 `last_flip_ns()` 同名遮蔽。
        self._lat_submit_ns = 0
        self._lat_flip_ns = 0
        self._lat_flip_seq = 0
        self._lat_t_capture_ns = 0
        self._last_idx = 0            # 最近一次已提交翻页的画布缓冲索引（供 TH5 镜像读源）
        self._fs_seq = 0              # 全屏态已绘制的 seq（内容静态：同一 seq 不重复贴图）
        # **（v3.87 追加 37 / DCM 演示路进度带）** 冻结期**起点快照**（整幅画布字节，
        # 无提示文字）：进度带每阶段/每秒重画时先写回它 ⇒ 旧文字不残留、画面不漂移。
        # `mark_busy(resnap=True)`（冻结进入）时刷新；整个冻结期有效。
        self._freeze_snap: Optional[bytes] = None

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

    def set_panel_wb(self, gains) -> int:
        """设置 **MIPI 面板白平衡校正**（线性域 per-channel 增益；**非契约入口**）。

        `gains` = `(r, g, b)` 或 `None`；**全为 1.0 或 None ⇒ 关闭**（不建表 ⇒ 逐位不变）。
        只影响 **DSI 槽**画布；HDMI 槽不经此路。
        """
        if not gains:
            self._wb_lut = None
            return 0
        gr, gg, gb = (float(v) for v in gains)
        if gr == 1.0 and gg == 1.0 and gb == 1.0:
            self._wb_lut = None
            return 0
        self._wb_lut = _n.wb.build_lut(gr, gg, gb)
        return 0

    def _wb_apply(self, buf, npix: int) -> None:
        """把面板白平衡 LUT 应用到 `buf` 的**前 `npix` 个像素**（`_wb_lut` 为 None ⇒ 直接返回）。

        **（v3.87 裁定 O163 补记 / 2026-10-13）应用点分两种**：
        · **常规视图（每帧）** —— 在 `compose_flip` 里对**两个 RGA 源窗**（`win` / `enh`，
          各 512×288）应用，再由 `canvas_compose_to` **硬件**缩放进画布；
        · **全屏 HQ 视图（每次贴图仅一次）** —— 对**画布**应用（HQ 图 1:1 直贴画布，
          不经源窗；贴图只在 `hq_seq` 变化时发生 ⇒ 每周期一次、不在帧路径上）。

        **为什么常规视图必须用源窗**：画布分配自 `system-uncached-dma32`（DMA 一致性所需，
        见 `drm_core.c` 画布堆选择注释）⇒ CPU 逐字节查表 ~45 ns/像素，全画布 614,400 px
        实测 **94.5 ms/帧**（板上微基准，2026-10-13）；源窗分配自 `system-dma32`（**带缓存**），
        两窗合计实测 **0.62 ms/帧** ⇒ **153 倍**。LUT 为逐通道点运算 ⇒ 先校正后缩放与先缩放后校正
        在图像区等效（差异 ≤1 LSB、不可见）；**OSD / 光标画在画布上 ⇒ 不再受校正**（已知视觉变化）。
        """
        if self._wb_lut is not None:
            _n.wb.apply(buf, self._wb_lut, npix)

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

        # **（v3.87 裁定 O163 补记 / 2026-10-13）** 面板白平衡 LUT 在**源窗**上应用
        # （带缓存 `system-dma32`，两窗 0.62 ms）而非画布（uncached，94.5 ms）——
        # 画布堆属性与微基准见 `_wb_apply` docstring。OSD 随后画在画布上 ⇒ 不受校正。
        g = self._geom()
        wnpix = int(g["win_w"]) * int(g["win_h"])
        self._wb_apply(win, wnpix)
        self._wb_apply(enh, wnpix)

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

    def compose_fullscreen(self, hq_rgb, seq: int, hq_seq: int = 0,
                           overlay: Optional[Dict[str, Any]] = None) -> int:
        """**全屏 HQ 视图**（§3.5）：MIPI 画布 = 黑底 + HQ **1:1 居中**（左右 32 / 上下 30 黑边）。

        HQ 内容在 O90 冻结期内**静止** ⇒ 同一 `hq_seq` **只贴图一次**（贴完保持不清屏）；
        **OSD 指标区与按钮区不画**（§3.5）。返回语义同 `compose_flip`（0 = 已翻页；1 = 本帧被丢弃）。

        **（v3.88 界面优化）** `overlay` 非空 ⇒ 贴图后画**中文说明叠层**（左上角标签 +
        右下角指标卡 + 底部处理链一行，见 `_draw_hq_overlay`）。叠层画在 `_wb_apply`
        **之后**——与常规视图 OSD 同口径（面板白平衡**不作用于文字**，见 `_wb_apply` docstring）。
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
            # **（v3.87 裁定 O163 补记）** 全屏 HQ 直贴画布 ⇒ 白平衡仍在**画布**上应用
            # （每周期仅一次、冻结期内，不在帧路径上；常规视图已改源窗，见 `_wb_apply`）。
            self._wb_apply(canvas, self.canvas_w * self.canvas_h)
            # **（v3.88 界面优化）** 中文说明叠层：画在白平衡**之后**（同常规视图 OSD 口径）。
            # **按 `overlay["kind"]` 分发**：`"hq"` ⇒ HQ 指标卡（`_draw_hq_overlay`）；
            # `"dcm"` ⇒ DCM「压缩·带宽」卡（`_draw_dcm_overlay`，2026-10-14 用户裁定）。
            # ⚠️ 首版此处**写死**调 `_draw_hq_overlay` ⇒ DCM 视图错显 HQ 卡片（2026-10-14 实测发现并修复）。
            if overlay:
                if overlay.get("kind") == "dcm":
                    self._draw_dcm_overlay(canvas, overlay)
                else:
                    self._draw_hq_overlay(canvas, overlay)
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

    def mark_busy(self, seq: int, text: str = "HQ PROCESSING ...",
                  stage_zh: Optional[str] = None, stage_en: str = "",
                  elapsed_s: Optional[float] = None, resnap: bool = False) -> int:
        """触发受理瞬间的"处理中"提示（§3.5 / §6.2：**触发→OSD ≤50 ms**）。

        做法 = 复制**上一帧已翻页的画布**到另一缓冲、叠一行提示、翻页一次 ⇒ 冻结期内画面静止，
        提示常驻。返回 0 = 已翻页；负值 = 错误（HDMI/MIPI 主链路不受影响）。

        **（v3.87 追加 37 / DCM 演示路进度带）** `stage_zh` 非空 ⇒ 在画面**居中**叠一条
        **高对比进度带**（黑底 + 琥珀边线）：第一行 = 阶段名（中文位图记录 `stage_zh`，
        资源缺失回退英文 `stage_en`），第二行 = 「已用时 X.X 秒」（`elapsed_s` 非空时）。
        进度带由调用方（`main._freeze_overlay_tick`）按**阶段切换或 1 s 节流**重画 ⇒ 必须以
        **冻结起点快照**为底图（否则旧文字残留、画面漂移）：`resnap=True`（冻结进入时）把
        当前画布存为 `_freeze_snap`，之后每次重画先写回快照再画新文字。
        `stage_zh` 为空 ⇒ **旧行为不变**（只画状态行，供 HQ 键路径使用）。
        """
        if not self._inited:
            return E_STATE
        g = self.geom
        cfg = {"canvas_w": self.canvas_w, "canvas_h": self.canvas_h}
        if stage_zh is None:
            # 旧行为：整幅复制上一帧（含其既有内容）+ 状态行提示
            prev = _n.drm.canvas(self._last_idx, cfg)
            nxt_idx = self._last_idx ^ 1
            nxt = _n.drm.canvas(nxt_idx, cfg)
            nxt.write(prev.to_bytes())        # 整幅复制（1024×600×3 ≈1.8 MB memcpy）
            if self.osd is not None:
                self.osd.text(nxt, 8, int(g["status_row_y"]) + 2, text, 0xFFD050)
        else:
            if resnap or self._freeze_snap is None:
                prev = _n.drm.canvas(self._last_idx, cfg)
                self._freeze_snap = prev.to_bytes()   # 冻结底图（无提示文字）
            nxt_idx = self._last_idx ^ 1
            nxt = _n.drm.canvas(nxt_idx, cfg)
            nxt.write(self._freeze_snap)              # 干净底图 ⇒ 旧文字不残留
            if self.osd is not None:
                self.osd.text(nxt, 8, int(g["status_row_y"]) + 2, text, 0xFFD050)
                self._draw_freeze_overlay(nxt, stage_zh, stage_en, elapsed_s)
        rc = self._flip(nxt_idx, int(seq))
        if rc == 0:
            self._last_idx = nxt_idx
            self.cur = self._last_idx ^ 1
            self._flips += 1
        return rc

    def _draw_freeze_overlay(self, canvas, stage_zh: str, stage_en: str,
                             elapsed_s: Optional[float]) -> None:
        """冻结期**居中高对比进度带**（v3.87 追加 37）：黑底 + 琥珀边线；

        第一行 = 阶段名（中文位图优先；缺失回退英文 PSF ×2）；第二行 = 「已用时 X.X 秒」
        （中文段位图 28 px + **小一号 PSF 数字**，见下；位图全缺失时回退全英文）。
        **（2026-10-14 用户裁定）** 数字改用 `text_small`（墨迹 **24 → 20 px**）并按墨迹居中
        对齐 28 px 中文。资源缺失一律**不画对应行**，不得因装饰阻塞冻结提示
        （规则 `30-board-safety.md` §5 同口径）。
        """
        cw, ch = self.canvas_w, self.canvas_h
        band_h = min(128, ch)
        band_y = (ch - band_h) // 2
        _n.osd.fill_rect(canvas, cw, ch, 0, band_y, cw, band_h, 0x000000)
        _n.osd.fill_rect(canvas, cw, ch, 0, band_y, cw, 2, OVL_EDGE)
        _n.osd.fill_rect(canvas, cw, ch, 0, band_y + band_h - 2, cw, 2, OVL_EDGE)
        zh = self.osd.zh or {}
        # ── 第一行：阶段名（居中）──
        y1 = band_y + 18
        rec = zh.get(stage_zh)
        if rec is not None:
            w, h, bits = rec
            _n.osd.blit_glyph(canvas, cw, ch, max(0, (cw - w) // 2), y1, 1,
                              bits, w, h, 0xFFD050)
        elif stage_en and self.osd.font is not None:
            tw = len(stage_en) * self.osd.font.w * 2
            self.osd.text(canvas, max(0, (cw - tw) // 2), y1, stage_en, 0xFFD050, scale=2)
        # ── 第二行：已用时 X.X 秒（中文段 + PSF 数字，整体居中）──
        if elapsed_s is None:
            return
        y2 = band_y + 70
        e_rec, s_rec = zh.get("elapsed"), zh.get("sec")
        # **（2026-10-14 用户裁定）** 数字段改用**小一号 PSF**（`text_small`）⇒ 数字墨迹
        # **24 → 20 px**（「缩 4 号」），并按**墨迹居中**对齐 28 px 的中文位图
        # （`small_dy(28) = 0`）。宽度按**小字体步进**计（`f_num.w · sc_num`）——
        # ⚠️ 曾按 scale=1 计宽（漏 ×2）⇒ 「秒」位图重叠覆盖小数位（2026-10-13 实测发现）。
        f_num, sc_num = self.osd.small_metrics()
        adv = (f_num.w * sc_num) if f_num is not None else 0
        dy = self.osd.small_dy(28)
        if e_rec is None and s_rec is None:
            # 位图全缺失 ⇒ 全英文回退（有字体才画；同样用小一号字体保持该行风格一致）
            if adv:
                en = "ELAPSED %.1f S" % float(elapsed_s)
                tw = len(en) * adv
                self.osd.text_small(canvas, max(0, (cw - tw) // 2), y2 + dy, en, 0xFFFFFF)
            return
        num = " %.1f " % float(elapsed_s)
        num_w = len(num) * adv
        total = (e_rec[0] if e_rec else 0) + num_w + (s_rec[0] if s_rec else 0)
        x = max(0, (cw - total) // 2)
        if e_rec is not None:
            w, h, bits = e_rec
            _n.osd.blit_glyph(canvas, cw, ch, x, y2, 1, bits, w, h, 0xFFFFFF)
            x += w
        if adv:
            self.osd.text_small(canvas, x, y2 + dy, num, 0xFFFFFF)
            x += num_w
        if s_rec is not None:
            w, h, bits = s_rec
            _n.osd.blit_glyph(canvas, cw, ch, x, y2, 1, bits, w, h, 0xFFFFFF)

    # ── **（v3.88 界面优化）** 全屏 HQ 视图的中文说明叠层 ──────────────────────
    def _draw_hq_overlay(self, canvas, info: Dict[str, Any]) -> None:
        """全屏 HQ 结果图上的**中文说明叠层**（v3.88 界面优化；§3.5 新增）。

        三块（坐标取自几何单源的 `hq_paste` = 960×540 居中矩形）：
          ① **左上角标签**「Retinexformer 增强」（25 px 琥珀，与实时路两窗标签同规格）；
          ② **右下角指标卡**（**实底**黑块 + 琥珀上下边线）：**三行无参考指标**
             「原图 → 增强」—— 亮度均值 / 暗部占比（%）/ 饱和度（%）
             （口径见 `main._hq_overlay_info`；数值用**小一号 PSF** 渲染，与 24 px 中文等高；
             亮度直方图小图叠在下方，口径同 OSD 直方图）。
        **（2026-10-14 用户裁定）** 卡片中文由 28 → **24 px**、**数值改用小一号 PSF**；
        **不再显示「已用时」「提升倍数」「灰度熵」「清晰度」**；**底部「处理链」整行取消**
        （原 `hq_chain` / `hq_l1` / `hq_l4` 位图记录一并作废）；卡片按行数 → `300×144`。

        **为何指标卡是实底**：native OSD **无 alpha 混合**（只有 1 bpp blit / 实心矩形 / 线）⇒
        叠在图像上的卡片必然是实底色块（遮住图像右下角，属本项的设计取舍）。

        `info` 键（缺项即不画对应内容，**一律不阻塞投屏**，规则 `30-board-safety.md` §5）：
        `mean_orig` / `mean_enh` / `dark_orig` / `dark_enh` / `sat_orig` / `sat_enh`
        / `hist_orig` / `hist_enh`。
        """
        cw, ch = self.canvas_w, self.canvas_h
        zh = (getattr(self.osd, "zh", None) or {}) if self.osd is not None else {}
        pad = HQ_OVL_PAD
        px, py, pw, ph = (int(v) for v in self.geom["hq_paste"])

        def _bm(x: int, y: int, name: str, rgb: int, gap: int = 8) -> int:
            """blit 一条中文位图记录；返回结束 x（缺资源则原样返回，不推进）。"""
            rec = zh.get(name)
            if rec is None:
                return x
            w, h, bits = rec
            _n.osd.blit_glyph(canvas, cw, ch, x, y, 1, bits, w, h, rgb)
            return x + w + gap

        # ── ① 左上角标签（琥珀，与两窗标签同色同规格）──
        _bm(px + pad, py + pad, "hq_title", osd_mod.C_CAPTION, gap=0)

        # ── ② 右下角指标卡（实底 + 上下边线）──
        cx = px + pw - pad - HQ_CARD_W
        cy = py + ph - pad - HQ_CARD_H
        _n.osd.fill_rect(canvas, cw, ch, cx, cy, HQ_CARD_W, HQ_CARD_H, 0x101010)
        _n.osd.fill_rect(canvas, cw, ch, cx, cy, HQ_CARD_W, 2, OVL_EDGE)
        _n.osd.fill_rect(canvas, cw, ch, cx, cy + HQ_CARD_H - 2, HQ_CARD_W, 2, OVL_EDGE)
        ix, iy = cx + pad, cy + pad
        # **三行**无参考指标（原图 → 增强）；缺项即不画该行（不阻塞投屏）。
        # 数值用**小一号** PSF 渲染（`osd.text_small`）⇒ 墨迹与 24 px 中文等高（见 `osd.py`）。
        rows = (
            ("hq_mean", " %d -> %d", info.get("mean_orig"), info.get("mean_enh")),
            ("hq_dark", " %d%% -> %d%%", info.get("dark_orig"), info.get("dark_enh")),
            ("hq_sat", " %d%% -> %d%%", info.get("sat_orig"), info.get("sat_enh")),
        )
        for k, (name, fmt, vo, ve) in enumerate(rows):
            y = iy + 28 * k
            x = _bm(ix, y, name, osd_mod.C_TEXT, gap=6)
            if vo is not None and ve is not None:
                self.osd.text_small(canvas, x, y + self.osd.small_dy(24), fmt % (vo, ve),
                                    osd_mod.C_HIST_ENH)
        self._hist_small(canvas, cx + pad, cy + 96, HQ_CARD_W - 2 * pad, HQ_CARD_H - 104,
                         info.get("hist_orig"), info.get("hist_enh"))

    def _hist_small(self, canvas, x0: int, y0: int, w: int, h: int,
                    hist_o, hist_e) -> None:
        """指标卡内的**亮度直方图小图**（原图灰 / 增强绿叠加；口径同 `osd._histogram`）。

        `hist_o` / `hist_e` 缺失（资源或统计不可用）⇒ 不画，**不阻塞投屏**。
        """
        if not hist_o or not hist_e or w <= 2 or h <= 2:
            return
        peak = max(1, max(hist_o), max(hist_e))
        inner = w - 2
        for b in range(256):
            col = x0 + 1 + int(b / 256.0 * inner)
            ho = int(hist_o[b] / peak * (h - 2))
            he = int(hist_e[b] / peak * (h - 2))
            if ho:
                _n.osd.fill_rect(canvas, self.canvas_w, self.canvas_h, col, y0 + h - 1 - ho,
                                 1, ho, osd_mod.C_HIST_ORIG)
            if he:
                _n.osd.fill_rect(canvas, self.canvas_w, self.canvas_h, col, y0 + h - 1 - he,
                                 1, he, osd_mod.C_HIST_ENH)

    # ── **（2026-10-14 用户裁定）** DCM 全屏（第三视图态）的「压缩·带宽」叠层 ──────────
    def _draw_dcm_overlay(self, canvas, info: Dict[str, Any]) -> None:
        """DCM 全屏的**压缩·带宽说明叠层**（2026-10-14 用户裁定；§3.5 第三视图态）。

        两块（坐标取自几何单源的 `hq_paste` = 960×540 居中矩形；与 HQ 叠层同规格）：
          ① **图像底部居中一行**（**黑底绿字**）「**DCM码流重建图**」（**2026-10-14 用户裁定：
             取消原左上角标签**，改在图像底部注明"此画面由码流重建"）；
          ② **右下角卡片**（**实底**黑块 + 琥珀上下边线）**三行**：原图像素 / DCM码流 /
             压缩·节省。数值用**小一号 PSF**（`osd.text_small`）+ `small_dy(24)`，
             与 24 px 中文**墨迹垂直居中**。

        **口径**（详见 `main._dcm_overlay_info`）：`原图像素` = DCM 的输入画面（HQ 增强图
        960×540 RGB888）；`DCM码流` = **rANS 熵编码后**的 `y_bytes+z_bytes`（= **实际传输量**）；
        `压缩` = 原图像素÷码流、`节省` = 1 − 码流÷原图像素。
        **（2026-10-14 用户裁定）** 卡片**依次去掉「特征FP16」行与「检出(解码特征)」行** ⇒
        只留**实测 / 直接可读**的量：前者是 FP16 未压缩口径的**推算**（非实测产物），后者**屏上无说明、
        易被误当成精度指标** ⇒ **检测结果改由"图像上画出的框"呈现**。
        ⚠️ 由此屏上**不再有任何精度免责字样** ⇒ 免责说明**只保留在文档**（§3.5 / §14 追加 43·44·46·47）：
        本演示**无 GT**、屏上数字**不是精度指标**、**不做同屏 A/B**（§8.23）——**讲解时须口头说明**。

        `info` 键：`px_bytes` / `stream_bytes` / `ratio` / `saved_pct`；
        缺项即不画对应内容，**不阻塞贴图**（规则 `30-board-safety.md` §5）。
        """
        cw, ch = self.canvas_w, self.canvas_h
        zh = (getattr(self.osd, "zh", None) or {}) if self.osd is not None else {}
        pad = HQ_OVL_PAD
        px, py, pw, ph = (int(v) for v in self.geom["hq_paste"])

        def _bm(x: int, y: int, name: str, rgb: int, gap: int = 8) -> int:
            rec = zh.get(name)
            if rec is None:
                return x
            w, h, bits = rec
            _n.osd.blit_glyph(canvas, cw, ch, x, y, 1, bits, w, h, rgb)
            return x + w + gap

        # ── ① 图像底部**居中**一行：**黑底绿字**「DCM码流重建图」（2026-10-14 用户裁定）——
        # 用以注明"屏上这张画面是由码流重建出来的"（**左上角标签已按用户要求去掉**）。
        rec = zh.get("dc_foot")
        if rec is not None:
            w, h, bits = rec
            bw = w + 2 * pad
            bx, by = px + (pw - bw) // 2, py + ph - DCM_FOOT_H
            _n.osd.fill_rect(canvas, cw, ch, bx, by, bw, DCM_FOOT_H, 0x000000)
            _n.osd.blit_glyph(canvas, cw, ch, bx + pad, by + (DCM_FOOT_H - h) // 2, 1,
                              bits, w, h, DCM_FOOT_FG)

        # ── ② 右下角卡片（实底 + 上下边线）──
        cx = px + pw - pad - DCM_CARD_W
        cy = py + ph - pad - DCM_CARD_H
        _n.osd.fill_rect(canvas, cw, ch, cx, cy, DCM_CARD_W, DCM_CARD_H, 0x101010)
        _n.osd.fill_rect(canvas, cw, ch, cx, cy, DCM_CARD_W, 2, OVL_EDGE)
        _n.osd.fill_rect(canvas, cw, ch, cx, cy + DCM_CARD_H - 2, DCM_CARD_W, 2, OVL_EDGE)
        n_px = info.get("px_bytes")
        n_st, ratio, saved = info.get("stream_bytes"), info.get("ratio"), info.get("saved_pct")
        rows = (
            ("dc_px", " %s" % _fmt_kbmb(n_px) if n_px else None),
            ("dc_stream", " %s" % _fmt_kbmb(n_st) if n_st else None),
            ("dc_ratio", " %.0fx / %.1f%%" % (ratio, saved)
             if ratio is not None and saved is not None else None),
        )
        ix, iy = cx + pad, cy + pad
        for k, (name, val) in enumerate(rows):
            y = iy + 28 * k
            x = _bm(ix, y, name, osd_mod.C_TEXT, gap=6)
            if val is not None:
                self.osd.text_small(canvas, x, y + self.osd.small_dy(24), val,
                                    osd_mod.C_HIST_ENH)

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
