#!/usr/bin/env python3
"""osd.py —— OSD 指标区与按钮区渲染（TH4 的显示内容；布局取值见主基准 §3.4）

布局（§3.4 定稿）：
    画布 1024×600 = 原图窗 512×288（Y 0–287）｜ 增强窗 512×288（X 512–1023）
                  + **OSD 指标区 1024×216**（Y 288–503）
                       ├ 状态/数值行 1024×36（Y 288–323）
                       └ 图示区 1024×180（Y 324–503）
                  + 按钮区 1024×96（Y 504–599，1 行 2 键各 512 px；v3.86 裁定 O101 取消「存图」键）

状态/数值行 **8 字段**（§3.4）：运行模式 / 帧率 / 端到端延迟两段 / 曲线估计频率 / 下次触发倒计时 /
                              原图窗亮度均值 / 增强窗亮度均值 / 亮度增益 Δ
图示区：**tone-mapping 曲线图**（R/G/B 三线 + 对角参考虚线）｜ **亮度直方图**（原图灰 / 增强绿叠加，256 bin）
        **两块图各自的左上角**有一行中文说明（**v3.86 裁定 O101**；见下方 `ZH_*`）

纪律（§3.4、§13 OSD 行）：
  · **数值必须真实**；**PSNR/SSIM 等有参考量一律不入实时 OSD**（只在离线实验报告里出现）；
  · 统计**降采样 4×**（512×288 → 128×72 = 9,216 样本）+ **降频 1 Hz**（`ui.osd_stats_decimate` / `ui.osd_stats_hz`）；
  · 曲线图由曲线场求均值后查表绘制。

字体：系统 PSF 控制台字体（stdlib `gzip` 解压 + 解析 PSF1/PSF2），**不内嵌字体表**。
"""

from __future__ import annotations

import gzip
import os
import struct as _struct
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import _starlight_native as _n

import curve_estim as curve_mod

# ── §3.4 布局（**几何唯一来源 = `curve_estim.resolve_geometry(cfg)`**；v3.86 就地裁定 O58）──
# 本模块**不再持有任何几何常量**（原 `OSD_Y` / `OSD_H` / `PLOT_Y` / `BUTTON_*` 硬编码已删）；
# 坐标一律取自 `OsdRenderer` 在构造时解析的几何单源，取值来源 = §10.4 的
# `display.mipi_canvas` / `ui.osd_metrics_px` / `ui.osd_status_row_px` / `ui.osd_plot_px` /
# `ui.button_layout`（**键表零增删**）。
# **（v3.88 界面优化 / 按钮全中文）** 三个按钮的标签改为**全中文**：原 ASCII 标签
# （`TRIGGER HQ` / 回退文本 `DCM_DEMO` / `EXIT`）与「EXIT + (长按3秒)」两段式方案
# （v3.86 裁定 O114 的 `BUTTON_ZH_SUFFIX`）**一并作废**——屏上只出现中文位图，
# 读数 = `BUTTON_ZH` 映射的预渲染位图（`btn_hq` / `btn_dcm` / `btn_exit`，
# 见 `tools/gen_osd_zh_bitmap.py`）。理由：按钮是演示 UI 的门面，ASCII 与中文混排
# 风格不齐；板端 PSF 无 CJK 字形 ⇒ 全中文只能走既有预渲染位图方案（运行期零字体依赖）。
# 按键语义（§3.4）：触发 HQ 路高质量增强 / DCM 演示路特征检测 / 长按 3 秒退出
# （`ui.reset_hold_ms`，§10.6-C；「长按3秒」要求按 O114 原意保留在键上）。
# **位图缺失时的回退**：不画任何文字（装饰缺失不阻塞主链路；键区底色/描边仍在，
# 命中判定只读 config，不受影响）。
BUTTON_ZH = {"trigger_hq": "btn_hq", "dcm_demo": "btn_dcm", "reset_exit": "btn_exit"}
BTN_ZH_GAP = 12              # 中文位图与键边缘的最小间距（px）

# 颜色（RGB888）
C_BG = 0x101010
C_TEXT = 0xE0E0E0
C_DIM = 0x808080
C_IDENT = 0x606060        # 对角参考线
C_R = 0xE05050
C_G = 0x50E050
C_B = 0x5080E0
C_HIST_ORIG = 0x909090
C_HIST_ENH = 0x40C040
C_BTN = 0x303840
C_BTN_EDGE = 0x606870
C_CURSOR = 0xF0F0F0          # 指针十字（O81：画进画布）
# **（v3.86 裁定 O112）** 两块**图名**（「色调映射曲线」/「亮度直方图」）的统一颜色：
# 取**琥珀色**——OSD 既有色板（白/灰/红/绿/蓝/按钮灰/指针白）中**未使用过**的暖色系，
# 与数据标记（红/绿/蓝）和状态行文字（白）都能区分，故适合做"标签"而非"数据"。
C_CAPTION = 0xE0B040

# 8 次迭代 = §3.3 / §4.1 的**契约值**（与 `realtime_apply.CURVE_ITERS` 同值，**不是 config 键**）。
# OSD 的色调映射曲线必须与内核 `apply_core.c` 同式，故在此复刻同一常量。
CURVE_ITERS = 8
C_CURSOR_CORE = 0x101010     # 十字中心点（与浅色十字形成对比）

# 3 键（§3.4：触发HQ / DCM演示 / 长按退出）的文字标签 = `BUTTON_ZH` 映射的中文位图，
# **位置与宽度**取自 `ui.button_layout`（几何单源），不再在此写死。

# ── 图示区两行中文说明（**v3.86 裁定 O101**）──────────────────────────────
# 板端**没有 CJK 字形**（OSD 文本只用 PSF 控制台字体，板端无 FreeType / PIL）⇒ 两行说明用
# **开发机预渲染的 1bpp 位图**静态资源入库，运行期按相对路径读取、用既有 native 原语
# `_n.osd.blit_glyph`(scale=1) blit；**运行期零字体依赖、零新增 native 接口**。
# 生成方式与格式见 `tools/gen_osd_zh_bitmap.py`（PIL + 开发机 CJK TTF，仅开发机依赖）。
# 位置 = 两块图**各自的左上角**（x 起点取几何单源的块起点 + `ZH_CAPTION_PAD`，y 起点 = `plot_y`）；
# 说明带高 = 位图高 + `ZH_CAPTION_GAP`，**图的内容区随之下移** ⇒ 说明与图元**不重叠、不遮挡**。
ZH_LABELS = {"curve": "色调映射曲线", "hist": "亮度直方图",
             # **（v3.86 裁定 O108 / O109）** 状态行中文标签（同一份预渲染资源；板端 PSF 无 CJK 字形）
             # 全角冒号「：」烘进位图（PSF 只有半角 `:`）；「均值提升」按裁定**不带冒号**。
             "lat": "端延时：", "rate": "帧率：", "lm": "均值提升",   # 键名与资源记录名逐字一致
             # **（v3.87 追加 37 / DCM 演示路进度带）** 冻结期居中进度带的三行阶段名 + 第二行
             # 「已用时 X.X 秒」的中文段（X.X 由 PSF 渲染）；资源缺失 ⇒ 进度带回退英文 PSF 文本。
             "stg_enh": "RetinexFormer 网络增强中", "stg_codec": "DCM 特征编解码中",
             "stg_detect": "特征域目标检测中", "elapsed": "已用时", "sec": "秒",
             # **（v3.88 界面优化 / 窗口中文标签）** 上部两窗左上角的中文标签（§3.4 的
             # 原图窗 / 增强窗）。键名与资源记录名逐字一致；墨迹高 25 px 与两块图名一致。
             # **（2026-10-14 用户裁定）** 增强窗标签改为「ZDE++实时增强」（点名实时路所用的曲线模型）。
             "win_orig": "原始图像", "win_enh": "ZDE++实时增强",
             # **（v3.88 界面优化 / 三键全中文；2026-10-14）** 按钮区标签**全中文**
             # （原 ASCII「TRIGGER HQ / DCM_DEMO / EXIT」与 O114 的「ASCII 段 + 中文后缀」两段式**作废**）
             # ⇒ 由 `BUTTON_ZH` 映射注入 `_buttons()`（与 `ZH_LABELS` 分开，因按钮是"整键一条位图"）。
             "btn_hq": "高质量增强", "btn_dcm": "DCM 特征检测", "btn_exit": "退出（长按3秒）",
             # **（v3.88 界面优化 / HQ 全屏叠层；2026-10-14）** 左上角标签 + 右下角指标卡三行
             # （卡片中文 28 → **24 px**；「已用时 / 提升倍数 / 灰度熵 / 清晰度」与底部「处理链」整行
             # 均已按用户裁定删除 ⇒ 相应记录作废）。
             "hq_title": "Retinexformer 增强",
             "hq_mean": "亮度均值", "hq_dark": "暗部占比", "hq_sat": "饱和度",
             # **（v3.88 界面优化 / DCM 全屏「压缩·带宽」叠层；2026-10-14 四次裁定）**
             # 底部居中一行（黑底绿字）+ 卡片三行；`dc_title`（左上角标签）与
             # `dc_feat`（特征FP16）、`dc_det`（检出(解码特征)）均已按用户裁定删除。
             "dc_foot": "DCM码流重建图",
             "dc_px": "原图像素", "dc_stream": "DCM码流", "dc_ratio": "压缩/节省",
             # **（2026-10-14）** HQ 路等待期进度带的阶段 1（多帧时域平均）；阶段 2 复用 `stg_enh`。
             "stg_mfa": "多帧时域平均中"}
ZH_BITMAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "osd_zh_labels.bin")
ZH_CAPTION_PAD = 6      # 说明位图左缩进（与图示区内边距 `pad` 同值）
ZH_CAPTION_GAP = 6      # 说明位图与图元之间的**最小间距**（px）
# **（v3.86 裁定 O109）** 两块示意图整体**下移 `curve_estim.PLOT_TOP_GAP`（3 px）**，用来**加宽状态行
# 与图示之间的间隔**；由按钮区高度同步 −3 px 吸收（`ui.button_layout.height_px` 96 → 93）⇒ 画布总高仍 600。
# 该常量**定义在几何单源 `curve_estim`**（它同时承担 §3.4 的自洽断言），此处只引用。


def load_zh_labels(path: str = ZH_BITMAP) -> Dict[str, Tuple[int, int, bytes]]:
    """读取预渲染中文位图资源（自描述容器：`SLZH1\\n` + 每行 `name w h stride\\n` + 1bpp 位图）。

    资源缺失/格式非法一律**返回空表**（说明文字不画）——**不得因 OSD 装饰阻塞主链路**
    （规则 `30-board-safety.md` §5 同口径）。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    if raw[:6] != b"SLZH1\n":
        return {}
    out: Dict[str, Tuple[int, int, bytes]] = {}
    pos = 6
    while pos < len(raw):
        nl = raw.find(b"\n", pos)
        if nl < 0:
            return out
        try:
            name, w_s, h_s, stride_s = raw[pos:nl].decode("ascii").split()
            w, h, stride = int(w_s), int(h_s), int(stride_s)
        except (ValueError, UnicodeDecodeError):
            return out
        pos = nl + 1
        if w <= 0 or h <= 0 or stride != (w + 7) // 8 or pos + stride * h > len(raw):
            return out
        out[name] = (w, h, raw[pos:pos + stride * h])
        pos += stride * h
    return out


# PSF 字体候选（按优先级；任一可用即可）
FONT_CANDIDATES = (
    "/usr/share/consolefonts/Lat7-Terminus18x10.psf.gz",
    "/usr/share/consolefonts/Lat7-Fixed14.psf.gz",
    "/usr/share/consolefonts/Lat15-VGA16.psf.gz",
    "/usr/share/consolefonts/Uni2-TerminusBold18x10.psf.gz",
)

# **（2026-10-14 用户裁定）** **小一号**的 PSF 候选，供「HQ 全屏指标卡的数值」专用：
# 主字体（`Lat7-Terminus18x10`，gh=18）在 `scale=2` 下墨迹 ≈26 px，**高于卡内 24 px 的中文**
# ⇒ 换用 `gh=16` 的一档（`scale=2` ⇒ 字格 16×32、墨迹 ≈22 px）使其与中文等高。
# 板上实测该字体存在（`Lat7-Terminus16.psf.gz`，PSF1 gw=8 gh=16）；缺失 ⇒ **回退主字体**。
FONT_CANDIDATES_SMALL = (
    "/usr/share/consolefonts/Lat7-Terminus16.psf.gz",
    "/usr/share/consolefonts/Lat7-Fixed14.psf.gz",
    "/usr/share/consolefonts/CyrAsia-Terminus16.psf.gz",
)


class Font:
    """PSF1 / PSF2 控制台字体（1bpp 位图）。"""

    def __init__(self, glyph_w: int, glyph_h: int, glyphs: List[bytes], first: int = 0):
        """`first` = 字形表的**码位基数**。

        PSF1/PSF2 控制台字体的字形表**按码位顺序**存放（`glyphs[i]` = 码位 `first + i` 的字形）；
        板端字体 `Lat7-Terminus18x10.psf.gz`（PSF2、nglyph=256）的 `glyphs[i]` 即**码位 i**
        ⇒ 基数必须是 **0**。原默认值 32 会让全部字符索引左移 32 位（`T`→`4`、空格→glyph 0），
        OSD 全部乱码；对照渲染证据见 `docs/evidence/M2-2-hq-align.md` §OSD 文本（v3.86 修正）。
        """
        self.w = glyph_w
        self.h = glyph_h
        self.glyphs = glyphs
        self.first = first

    @staticmethod
    def load(path: str) -> "Font":
        with gzip.open(path, "rb") as f:
            raw = f.read()
        if raw[:2] == b"\x36\x04":                       # PSF1
            # PSF1 头：magic[0:2] = 0x36 0x04 / mode[2] / charsize[3]
            mode, charsize = raw[2], raw[3]
            nglyph = 512 if (mode & 0x01) else 256
            off = 4
            gw, gh = 8, charsize
            if not (1 <= charsize <= 4096):
                raise ValueError(f"PSF1 charsize 非法：{charsize}")
        elif raw[:4] == b"\x72\xb5\x4a\x86":             # PSF2
            # PSF2 头（全部 little-endian uint32）：
            # magic[0:4] / version[4:8] / headersize[8:12] / flags[12:16] /
            # length[16:20]（字形数）/ charsize[20:24] / height[24:28] / width[28:32]
            hdr = int.from_bytes(raw[8:12], "little")
            flags = int.from_bytes(raw[12:16], "little")
            nglyph = int.from_bytes(raw[16:20], "little")
            charsize = int.from_bytes(raw[20:24], "little")
            gh = int.from_bytes(raw[24:28], "little")
            gw = int.from_bytes(raw[28:32], "little")
            off = hdr
            # flags bit0 = 有 Unicode 表；该表**附在字形数据之后**，故不影响本实现按序取字形
        else:
            raise ValueError(f"不是 PSF 字体：{path}")
        # 合法性校验：**必须**在任何越界写之前拦住（曾因头偏移写错导致 gw/gh 为垃圾值 → 段错误）
        if not (1 <= gw <= 64 and 1 <= gh <= 64 and 1 <= charsize <= 64 * 64 and nglyph > 0):
            raise ValueError(f"PSF 头字段非法：gw={gw} gh={gh} charsize={charsize} nglyph={nglyph}")
        stride = (gw + 7) // 8
        if charsize < stride * gh:
            raise ValueError(f"PSF charsize({charsize}) < stride×gh({stride * gh})")
        glyphs = [raw[off + i * charsize: off + (i + 1) * charsize] for i in range(nglyph)]
        return Font(gw, gh, glyphs)

    def glyph(self, ch: str) -> bytes:
        idx = ord(ch) - self.first
        if idx < 0 or idx >= len(self.glyphs):
            idx = ord("?") - self.first
        if idx < 0 or idx >= len(self.glyphs):
            idx = 0
        return self.glyphs[idx]


def _find_font(candidates: Sequence[str] = FONT_CANDIDATES) -> Optional[Font]:
    for p in candidates:
        if os.path.exists(p):
            try:
                return Font.load(p)
            except (OSError, ValueError):
                continue
    return None


class Stats:
    """§3.4 的统计：**降采样 4×** + **降频 1 Hz**。无 numpy，纯 Python 逐样本累加。"""

    def __init__(self, decimate: int = 4, hz: float = 1.0):
        self.decimate = max(1, int(decimate))
        self.period_ns = int(1e9 / max(1e-6, float(hz)))
        self._last_ns = 0
        self.mean_orig = 0.0
        self.mean_enh = 0.0
        self.hist_orig = [0] * 256
        self.hist_enh = [0] * 256
        self.samples = 0

    def maybe_update(self, raw, enh, w: int, h: int, now_ns: Optional[int] = None) -> bool:
        """**（v3.86 裁定 O142）** `raw` / `enh` 三条路径**同口径**：

        ① **native 缓冲**（帧路径，`main.py` 传 `_n.rga.win` / `_n.rga.enh`）—— **零拷贝 + C 统计**
           （`_n.osd.stats`，**裁定 O142-候选①**，追加 99）；
        ② **零参回调**（`lambda: win.to_bytes()`）—— 只在到期帧拷贝 + Python 统计（对照 / 回退）；
        ③ **`bytes`** —— Python 统计（本模块 `main()` 自检用）。

        **1 Hz 判定（`ui.osd_stats_hz`）始终在本方法唯一一处**：不采用 `if stats_due(): …` 式，
        以免同一判据出现两处而日后分叉。
        """
        now = now_ns if now_ns is not None else time.monotonic_ns()
        if now - self._last_ns < self.period_ns:
            return False
        self._last_ns = now
        # **（v3.86 裁定 O142-候选①）** 到期后取样。三条路径，口径**逐字一致**：
        #   ① **native 缓冲**（帧路径，`main.py` 直接传 `_n.rga.win/enh`）⇒ **零拷贝 + C 统计**；
        #   ② 零参回调（`lambda: win.to_bytes()`）⇒ 只在到期帧拷贝 + Python 统计（保留作对照/回退）；
        #   ③ `bytes` 直传 ⇒ Python 统计（`osd.py` 自检用）。
        if _n is not None and hasattr(raw, "to_bytes") and not callable(raw):
            mo, me, n, hb_o, hb_e = _n.osd.stats(raw, enh, int(w), int(h), int(self.decimate))
            self.hist_orig = list(_struct.unpack(f"<{256}I", hb_o))
            self.hist_enh = list(_struct.unpack(f"<{256}I", hb_e))
            self.mean_orig, self.mean_enh, self.samples = float(mo), float(me), int(n)
            return True
        if callable(raw):
            raw = raw()
        if callable(enh):
            enh = enh()
        d = self.decimate
        hist_o = [0] * 256
        hist_e = [0] * 256
        so = se = 0
        n = 0
        for y in range(0, h, d):
            row = y * w
            for x in range(0, w, d):
                i = (row + x) * 3
                yo = (raw[i] * 77 + raw[i + 1] * 150 + raw[i + 2] * 29) >> 8
                ye = (enh[i] * 77 + enh[i + 1] * 150 + enh[i + 2] * 29) >> 8
                hist_o[yo] += 1
                hist_e[ye] += 1
                so += yo
                se += ye
                n += 1
        if n == 0:
            return False
        self.hist_orig, self.hist_enh = hist_o, hist_e
        self.mean_orig = so / n
        self.mean_enh = se / n
        self.samples = n
        return True


class OsdRenderer:
    """把 7 字段 + 两块图示 + 2 个按钮画到画布缓冲上（§3.4）。"""

    def __init__(self, cfg: Any, canvas_w: int, canvas_h: int,
                 status_only: bool = False, no_stats: bool = False):
        self.cfg = cfg
        # **（v3.86 追加 97）①-2 的 OSD 瘦身钩子（非 config 键）**：
        #   · `status_only` = **只画状态行**（跳 `_plot_area`：曲线图 + 直方图 + 图名位图都不画）；
        #   · `no_stats` = 连统计也不做（`update_stats()` 直接返回）⇒ 状态行的均值字段保持初值。
        self.status_only = bool(status_only)
        self.no_stats = bool(no_stats)
        self.cw, self.ch = canvas_w, canvas_h
        # **几何单源（§15 O58 就地裁定）**：OSD 区 / 状态行 / 图示区 / 按钮区 rect 全部取自
        # `curve_estim.resolve_geometry(cfg)`，并与本类收到的画布尺寸交叉核对。
        g = curve_mod.resolve_geometry(cfg)
        if (int(g["canvas_w"]), int(g["canvas_h"])) != (int(canvas_w), int(canvas_h)):
            raise ValueError(
                f"画布尺寸与几何单源不一致：{canvas_w}×{canvas_h} vs "
                f"{g['canvas_w']}×{g['canvas_h']}（§3.4）")
        self.g = g
        self.osd_y, self.osd_h = int(g["osd_y"]), int(g["osd_h"])
        self.status_row_y = int(g["status_row_y"])
        self.plot_y, self.plot_h = int(g["plot_y"]), int(g["plot_h"])
        self.btn_y, self.btn_h = int(g["button_y"]), int(g["button_h"])
        self.btn_keys = tuple(g["button_keys"])
        self.zh = load_zh_labels()          # 图示区中文说明位图（资源缺失则空表 ⇒ 不画）
        self.font = _find_font()
        # **（2026-10-14 用户裁定）** **小一号**字体（`FONT_CANDIDATES_SMALL`）供指标卡数值专用；
        # 缺失 ⇒ 留 None，`text_small` 回退主字体（**不阻塞**）。
        self.font_small = _find_font(FONT_CANDIDATES_SMALL)
        # 字号来自 `ui.osd_font_px`（M2 键；未定稿时用 §3.4 的 36/26 做比例缩放）
        px = cfg.get("ui.osd_font_px")
        _px = px if isinstance(px, (list, tuple)) else (36, 26)
        self.scale = self._scale_for(_px)
        self.scale_small = self._scale_for(_px, self.font_small) if self.font_small else self.scale
        self.decimate = cfg.get("ui.osd_stats_decimate") or 4
        self.stats_hz = cfg.get("ui.osd_stats_hz") or 1
        self.stats = Stats(self.decimate, self.stats_hz)

    def _scale_for(self, px: Sequence[int], font: Optional["Font"] = None) -> int:
        f = font if font is not None else self.font
        if not f:
            return 1
        want = int(px[0]) if px else 36
        return max(1, int(round(want / f.h)))

    @staticmethod
    def _ink_rows(f: "Font", ch: str) -> Tuple[int, int]:
        """字形**墨迹的上下行号**（含端点，字体自身坐标）；全空 ⇒ `(0, f.h-1)`。"""
        g = f.glyph(ch)
        bpr = (f.w + 7) // 8
        top = bot = None
        for ry in range(f.h):
            if any(g[ry * bpr:(ry + 1) * bpr]):
                if top is None:
                    top = ry
                bot = ry
        return (0, f.h - 1) if top is None else (top, bot)

    def small_metrics(self) -> Tuple[Optional["Font"], int]:
        """`text_small` 实际使用的 **(字体, scale)**；小字体缺失 ⇒ **回退主字体**（与 `text` 一致）。"""
        if self.font_small is None:
            return self.font, self.scale
        return self.font_small, self.scale_small

    def small_dy(self, ref_ink_h: int) -> int:
        """小字体文本相对**中文位图**的 y 偏移（使两者**墨迹垂直居中**）。

        中文位图 blit 在 `y` ⇒ 墨迹占 `[y, y+ref_ink_h)`、中点 `y + ref_ink_h/2`；
        小字体画在 `y+dy` ⇒ 墨迹中点 `y + dy + (t+b+1)·scale_small/2`（`t`/`b` = 字形墨迹上下行）
        ⇒ `dy = ref_ink_h/2 − (t+b+1)·scale_small/2`（取整）。以数字 `0` 为**代表字形**。
        调用点：HQ 指标卡（`ref_ink_h=24`）、DCM 进度带「已用时」（`ref_ink_h=28`）。
        """
        f, sc = self.small_metrics()
        if f is None:
            return 0
        t, b = self._ink_rows(f, "0")
        return int(round(ref_ink_h / 2.0 - (t + b + 1) * sc / 2.0))

    # ── 文本 ───────────────────────────────────────────────────────────
    def text(self, canvas, x: int, y: int, s: str, rgb: int = C_TEXT, scale: Optional[int] = None) -> int:
        if not self.font:
            return -1
        sc = scale or self.scale
        cx = x
        for ch in s:
            _n.osd.blit_glyph(canvas, self.cw, self.ch, cx, y, sc, self.font.glyph(ch),
                              self.font.w, self.font.h, rgb)
            cx += self.font.w * sc
        return cx

    def text_small(self, canvas, x: int, y: int, s: str, rgb: int = C_TEXT) -> int:
        """用**小一号**的 PSF 字体渲染文本（**HQ 指标卡数值 / DCM 进度带数字专用**；v3.88）。

        目的 = 让数值的**墨迹高度降一档**（主字体 `scale=2` 下数字墨迹 24 px；小字体 20 px），
        并与同行的中文位图做**墨迹垂直居中**（偏移见 `small_dy`）。
        字体缺失 ⇒ **回退主字体**，行为与 `text` 一致。返回结束 x。
        """
        f, sc = self.small_metrics()
        if not f:
            return -1
        cx = x
        for ch in s:
            _n.osd.blit_glyph(canvas, self.cw, self.ch, cx, y, sc, f.glyph(ch),
                              f.w, f.h, rgb)
            cx += f.w * sc
        return cx

    def blit_zh(self, canvas, x: int, y: int, name: str, rgb: int = C_TEXT) -> int:
        """把预渲染中文位图记录 `name` blit 到 `(x, y)`（scale=1）并返回**位图宽**（缺失返回 0）。

        **（v3.87 追加 37）** 公开版供 `display_mipi.mark_busy` 的冻结期进度带使用
        （原 `_zh_status` 为状态行私有、颜色与垂直居中口径写死）；资源缺失不画、不报错。
        """
        rec = self.zh.get(name)
        if not rec:
            return 0
        w, h, bits = rec
        _n.osd.blit_glyph(canvas, self.cw, self.ch, x, y, 1, bits, w, h, rgb)
        return w

    # ── 主渲染 ─────────────────────────────────────────────────────────
    def render_into(self, canvas, state: Dict[str, Any]) -> None:
        """`state` 键：mode / fps / lat_capture_ms / lat_submit_ms / curve_hz / countdown_s /
        mean_orig / mean_enh / curve_field / curve_w / curve_h。"""
        _n.osd.fill_rect(canvas, self.cw, self.ch, 0, self.osd_y, self.cw, self.osd_h, C_BG)
        _n.osd.fill_rect(canvas, self.cw, self.ch, 0, self.btn_y, self.cw, self.btn_h, C_BG)
        self._status_row(canvas, state)
        # **（v3.88 界面优化）** 上部两窗左上角的中文标签（原图窗「原始图像」/ 增强窗「增强图像」）：
        # 窗口图像由 native `rga_canvas_compose_to` 先合成、OSD 后画在其上（`display_mipi.compose_flip`）
        # ⇒ 标签画在本方法内即**自动位于图像之上**；HDMI 为画布 1.875× 镜像 ⇒ 两屏同现。
        # 窗口 rect 取自几何单源（`win_orig` / `win_enh`，`curve_estim.resolve_geometry`）；
        # 资源缺失 ⇒ 不画（不阻塞主链路）。`status_only` 时同图示区一并跳过。
        if not self.status_only:
            self._plot_area(canvas, state)
            self._window_labels(canvas)
        self._buttons(canvas)

    def _window_labels(self, canvas) -> None:
        """两窗左上角中文标签（1bpp 预渲染位图，scale=1；色取图名同款 `C_CAPTION`）。"""
        for name in ("win_orig", "win_enh"):
            rec = self.zh.get(name)
            if not rec:
                continue
            wx, wy = self.g[name][0], self.g[name][1]
            w, h, bits = rec
            _n.osd.blit_glyph(canvas, self.cw, self.ch,
                              wx + ZH_CAPTION_PAD, wy + ZH_CAPTION_PAD, 1, bits, w, h, C_CAPTION)

    def _zh_status(self, canvas, x: int, y: int, name: str) -> int:
        """把状态行的**中文标签**（1bpp 预渲染位图）blit 到 `(x, y)` 并返回**结束 x**。

        **（v3.86 裁定 O108）** 板端 PSF 控制台字体**无 CJK 字形** ⇒ 状态行的中文标签
        （「端延时」「均值提升」）与图示区说明同法：**开发机预渲染 + 运行期 `blit_glyph`**
        （scale=1，零运行期字体依赖）。垂直方向按 ASCII 字格居中。资源缺失时**只跳过标签**。
        """
        rec = self.zh.get(name)
        if not rec:
            return x
        w, h, bits = rec
        cy = y + max(0, (self.font.h * self.scale - h) // 2)
        _n.osd.blit_glyph(canvas, self.cw, self.ch, x, cy, 1, bits, w, h, C_TEXT)
        return x + w

    def _status_row(self, canvas, st: Dict[str, Any]) -> None:
        """状态/数值行 **5 字段**（§3.4；**v3.86 裁定 O108 重排**）。

        **全部为无参考量**——PSNR/SSIM 一律不入实时 OSD。分区 = 左区（**端延时** / **帧率**）+
        右区（**均值提升**：原图窗均值 > 增强窗均值，Δ 随行）；两区之间保证 ≥8 px 间隙。

        **O108 变更（相对 O107 的 6 字段）**：① **删「曲线估计频率」`C x.xHz`**（本屏是**演示界面
        而非系统监控界面**，该值是运维/诊断量）；② **延迟改为单值、定宽 3 位零填充**（`063ms`）——
        去掉 `T` 前缀与「采集→提交 + 提交→vblank」两段串，**避免数值跳动时整行横向移位**；
        ③ **帧率改用「最近 1 s 滑窗」统计**（非进程启动以来的累计均值）；④ **`T` / `L` 两处英文
        标签改中文**（`_zh_status`，位图资源）。
        """
        lat = st.get("lat_total_ms", 0.0)
        fps = st.get("fps", 0.0)
        mo = st.get("mean_orig", 0.0)
        me = st.get("mean_enh", 0.0)
        y = self.status_row_y + 2
        gap_lbl, gap_fld = 6, 24          # 标签↔值 6 px；**字段之间 24 px**（拆开各字段）
        # 左区：端延时（**3 位零填充**；≥1000 ms 截到 999 以免撑宽）→ 帧率；两者**取不同颜色**
        ms = max(0, min(999, int(round(lat))))
        x = 8
        x = self._zh_status(canvas, x, y, "lat") + gap_lbl
        x = self.text(canvas, x, y, f"{ms:03d}ms", C_B) + gap_fld      # 端延时值 = 蓝
        x = self._zh_status(canvas, x, y, "rate") + gap_lbl
        x = self.text(canvas, x, y, f"{fps:.1f}fps", C_G)              # 帧率值 = 绿
        # 右区：均值提升（右对齐）——原图值取**直方图原图灰**、增强值取**直方图增强绿**（同色可对读）
        g1, arrow, g2 = f"{mo:.1f}", " --> ", f"{me:.1f}"              # **（O109）去掉 `d+` 增益项**
        lbl_w = self.zh["lm"][0] if "lm" in self.zh else 0
        rw = lbl_w + gap_lbl + (len(g1) + len(arrow) + len(g2)) * self.font.w * self.scale
        rx = max(self.cw - 8 - rw, x + gap_fld)
        rx = self._zh_status(canvas, rx, y, "lm") + gap_lbl
        rx = self.text(canvas, rx, y, g1, C_HIST_ORIG)
        rx = self.text(canvas, rx, y, arrow, C_DIM)
        self.text(canvas, rx, y, g2, C_HIST_ENH)

    def _plot_area(self, canvas, st: Dict[str, Any]) -> None:
        """左：tone-mapping 曲线图（R/G/B + 对角参考）；右：亮度直方图（原图灰 / 增强绿）。

        **v3.86 裁定 O101**：每块图左上角加一行中文说明。说明占用块顶部的**说明带**
        （高 = 位图高 + `ZH_CAPTION_GAP`），图的内容区自说明带之下开始 ⇒ 二者不重叠；
        块起点与块宽取自几何单源（`plot_w` = 半画布、`plot_y` / `plot_h` = 图示区）。
        """
        half = self.cw // 2
        cap = self._caption_band_h()
        # **（v3.86 裁定 O109）** 两块图整体**下移 `PLOT_TOP_GAP` px**（加宽与状态行的间隔）；
        # 下移量由按钮区高度同步 −3 px 吸收 ⇒ 图底仍不压按钮区。
        y0, ph = self.plot_y + cap + curve_mod.PLOT_TOP_GAP, self.plot_h - cap
        self._curve_plot(canvas, 0, y0, half, ph, st)
        self._histogram(canvas, half, y0, half, ph)
        self._zh_caption(canvas, 0, "curve")
        self._zh_caption(canvas, half, "hist")

    def _caption_band_h(self) -> int:
        """说明带高度 = 位图高 + `ZH_CAPTION_GAP`（无资源时 0 ⇒ 图占满图示区）。"""
        hs = [h for _w, h, _b in self.zh.values()]
        return (max(hs) + ZH_CAPTION_GAP) if hs else 0

    def _zh_caption(self, canvas, x0: int, name: str) -> None:
        """把预渲染中文位图 blit 到该块**左上角**（以 1bpp 字形原语，scale=1）。"""
        rec = self.zh.get(name)
        if not rec:
            return
        w, h, bits = rec
        _n.osd.blit_glyph(canvas, self.cw, self.ch,
                          x0 + ZH_CAPTION_PAD, self.plot_y, 1, bits, w, h, C_CAPTION)

    def _curve_plot(self, canvas, x0: int, y0: int, w: int, h: int, st: Dict[str, Any]) -> None:
        field = st.get("curve_field")
        cw = st.get("curve_w", 0)
        chh = st.get("curve_h", 0)
        pad = 6
        # 对角参考（恒等映射）虚线
        for t in range(0, 100, 8):
            a = t / 100.0
            b = min(1.0, a + 0.06)
            _n.osd.draw_line(canvas, self.cw, self.ch,
                             x0 + pad + int(a * (w - 2 * pad)), y0 + h - pad - int(a * (h - 2 * pad)),
                             x0 + pad + int(b * (w - 2 * pad)), y0 + h - pad - int(b * (h - 2 * pad)),
                             1, C_IDENT)
        if not field or not cw or not chh:
            return
        # **（v3.86 裁定 O104 修正）按 §3.4 画真正的色调映射曲线**：横轴 = **输入亮度** 0→1、
        # 纵轴 = **输出亮度** 0→1；每通道取该通道曲线场的**全图均值 α**（§3.4 原文「全图均值参数」），
        # 再由与内核**同式**的 8 次迭代复合 `t ← t + α(t²−t)`（`native/apply_core.c`）得到映射。
        # ⚠️ 原实现画的是「曲线参数沿水平方向的剖面」（横轴 = 列索引、纵轴 = **原始 α**、
        # 且 α 为负时被 `clamp(α,0,1)` 压成 0 ⇒ 恒为贴底直线），与 §3.4 的三条口径均不符。
        # α 是**曲线参数**而非亮度 ⇒ 取均值时**不做 clamp**（本链路 α<0 才表示提亮）。
        mv = field.to_bytes() if hasattr(field, "to_bytes") else bytes(field)
        fp16 = st.get("curve_precision", "fp16") == "fp16"
        plane = cw * chh
        step_y = max(1, chh // 16)
        step_x = max(1, cw // 32)
        n_lut = 64
        aa: Dict[int, float] = {}
        for ch in range(3):
            color = (C_R, C_G, C_B)[ch]
            base = ch * plane
            s = 0.0
            cnt = 0
            for y in range(0, chh, step_y):
                row = base + y * cw
                for i in range(0, cw, step_x):
                    idx = row + i
                    if fp16:
                        off = idx * 2
                        s += _half_to_float(mv[off] | (mv[off + 1] << 8))
                    else:
                        s += mv[idx] / 255.0
                    cnt += 1
            a = (s / cnt) if cnt else 0.0
            aa[ch] = a
            prev: Optional[Tuple[int, int]] = None
            for j in range(n_lut + 1):
                t = j / float(n_lut)
                v = t
                for _it in range(CURVE_ITERS):
                    v = v + a * (v * v - v)
                if v < 0.0:
                    v = 0.0
                elif v > 1.0:
                    v = 1.0
                px = x0 + pad + int(t * (w - 2 * pad))
                py = y0 + h - pad - int(v * (h - 2 * pad))
                if prev is not None:
                    _n.osd.draw_line(canvas, self.cw, self.ch, prev[0], prev[1], px, py, 2, color)
                prev = (px, py)
        # **（v3.86 裁定 O105）实时工作点**：把「原图窗亮度均值」标到曲线上。
        # 语义**只标输入均值**（= 画面主体落在曲线的哪一段 + 模型在该输入处的映射值）；
        # **刻意不标"实际增强窗均值"**——曲线是凸的，`mean(f(x)) ≥ f(mean(x))`（Jensen），
        # 两点落差属**口径差**而非缺陷，标上去易被误读成"图错了"（见 §15 O105）。
        # 坐落在 **G 通道曲线**上：G 的亮度权重最大（0.587），与状态行的"亮度均值"口径最近。
        mo = st.get("mean_orig")
        if mo and 1 in aa:
            t0 = max(0.0, min(1.0, float(mo) / 255.0))
            v0 = t0
            for _it in range(CURVE_ITERS):
                v0 = v0 + aa[1] * (v0 * v0 - v0)
            v0 = 0.0 if v0 < 0.0 else (1.0 if v0 > 1.0 else v0)
            px0 = x0 + pad + int(t0 * (w - 2 * pad))
            py0 = y0 + h - pad - int(v0 * (h - 2 * pad))
            yb = y0 + h - pad                      # 横轴基线
            for seg in range(py0, yb, 6):          # 竖引导虚线（工作点 → 横轴）
                _n.osd.draw_line(canvas, self.cw, self.ch, px0, seg, px0, min(seg + 3, yb),
                                 1, C_DIM)
            _n.osd.fill_rect(canvas, self.cw, self.ch, px0 - 3, py0 - 3, 7, 7, C_G)

    def _histogram(self, canvas, x0: int, y0: int, w: int, h: int) -> None:
        pad = 6
        inner_w = w - 2 * pad
        inner_h = h - 2 * pad
        peak = max(1, max(self.stats.hist_orig), max(self.stats.hist_enh))
        for b in range(256):
            px = x0 + pad + int(b / 256.0 * inner_w)
            ho = int(self.stats.hist_orig[b] / peak * inner_h)
            he = int(self.stats.hist_enh[b] / peak * inner_h)
            if ho:
                _n.osd.fill_rect(canvas, self.cw, self.ch, px, y0 + h - pad - ho, 1, ho, C_HIST_ORIG)
            if he:
                _n.osd.fill_rect(canvas, self.cw, self.ch, px, y0 + h - pad - he, 1, he, C_HIST_ENH)

    def _buttons(self, canvas) -> None:
        """按钮区三键：**全中文标签**（预渲染 1bpp 位图，v3.88 界面优化）。

        每个键按位图**合计宽度居中**（水平居中 + 按钮区垂直居中）；
        位图缺失时**只画键区底色/描边、不画文字**——装饰缺失不阻塞主链路
        （命中判定只读 `ui.button_layout`，不受影响）。
        """
        for name, bx, bw in self.btn_keys:
            _n.osd.fill_rect(canvas, self.cw, self.ch, bx + 2, self.btn_y + 2,
                             bw - 4, self.btn_h - 4, C_BTN)
            _n.osd.fill_rect(canvas, self.cw, self.ch, bx + 2, self.btn_y + 2, bw - 4, 1, C_BTN_EDGE)
            rec = self.zh.get(BUTTON_ZH.get(name, ""))
            if not rec:
                continue
            w, h, bits = rec
            x = bx + (bw - w) // 2
            y = self.btn_y + (self.btn_h - h) // 2
            _n.osd.blit_glyph(canvas, self.cw, self.ch, x, y, 1, bits, w, h, C_TEXT)

    def draw_cursor(self, canvas, pos) -> None:
        """把指针位置画进画布（**v3.85 裁定 O81：光标每帧画进画布**；镜像下两屏都出现）。

        `pos` = 画布域 `(x, y)`；画一个 9×9 的十字 + 中心点（尺寸为绘制常量，非 layout 取值）。
        """
        if not pos:
            return
        x, y = int(pos[0]), int(pos[1])
        _n.osd.fill_rect(canvas, self.cw, self.ch, x - 4, y, 9, 1, C_CURSOR)
        _n.osd.fill_rect(canvas, self.cw, self.ch, x, y - 4, 1, 9, C_CURSOR)
        _n.osd.fill_rect(canvas, self.cw, self.ch, x - 1, y - 1, 3, 3, C_CURSOR_CORE)

    def update_stats(self, raw, enh, w: int, h: int) -> bool:
        # **（v3.86 追加 97）** `no_stats` ⇒ 连统计都不做（状态行均值字段保持初值；
        # 只用于"OSD 瘦身"实验的第二级，不得作为交付路径）
        if self.no_stats:
            return False
        return self.stats.maybe_update(raw, enh, w, h)


def _half_to_float(h: int) -> float:
    """IEEE 754 half → float（无 numpy）。"""
    s = (h >> 15) & 1
    e = (h >> 10) & 0x1F
    m = h & 0x3FF
    if e == 0:
        v = m * 2.0 ** -24
    elif e == 31:
        v = float("inf") if m == 0 else float("nan")
    else:
        v = (1 + m / 1024.0) * 2.0 ** (e - 15)
    return -v if s else v


def main() -> int:
    import config as starlight_config

    cfg = starlight_config.load()
    f = _find_font()
    print(f"字体：{'未找到' if not f else FONT_CANDIDATES[0]}  尺寸={None if not f else (f.w, f.h)}")
    g = curve_mod.resolve_geometry(cfg)
    r = OsdRenderer(cfg, int(g["canvas_w"]), int(g["canvas_h"]))
    print(f"scale={r.scale} decimate={r.decimate} hz={r.stats_hz}")
    print(f"按钮区：{r.btn_keys}（命中区下限 ui.hit_area_min_px；几何来自 ui.button_layout）")
    print(f"OSD 指标区 Y {r.osd_y}..{r.osd_y + r.osd_h - 1}"
          f"（状态行 {g['status_row_h']} + 图示 {r.plot_h}）；"
          f"按钮区 Y {r.btn_y}..{r.btn_y + r.btn_h - 1}（画布 {r.cw}×{r.ch}）")
    print(f"中文说明：{ZH_BITMAP} → " + (
        ", ".join(f"{k}={v[0]}×{v[1]}" for k, v in r.zh.items()) or "（资源缺失，不画）"))
    print(f"说明带高 {r._caption_band_h()} px（含 {ZH_CAPTION_GAP} px 间隙）；"
          f"图内容区 Y {r.plot_y + r._caption_band_h()}..{r.plot_y + r.plot_h - 1}")
    return 0 if f else 1


if __name__ == "__main__":
    raise SystemExit(main())
