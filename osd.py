#!/usr/bin/env python3
"""osd.py —— OSD 指标区与按钮区渲染（TH4 的显示内容；布局取值见主基准 §3.4）

布局（§3.4 定稿）：
    画布 1024×600 = 原图窗 512×288（Y 0–287）｜ 增强窗 512×288（X 512–1023）
                  + **OSD 指标区 1024×216**（Y 288–503）
                       ├ 状态/数值行 1024×36（Y 288–323）
                       └ 图示区 1024×180（Y 324–503）
                  + 按钮区 1024×96（Y 504–599，1 行 3 键各 341 px）

状态/数值行 **8 字段**（§3.4）：运行模式 / 帧率 / 端到端延迟两段 / 曲线估计频率 / 下次触发倒计时 /
                              原图窗亮度均值 / 增强窗亮度均值 / 亮度增益 Δ
图示区：**tone-mapping 曲线图**（R/G/B 三线 + 对角参考虚线）｜ **亮度直方图**（原图灰 / 增强绿叠加，256 bin）

纪律（§3.4、§13 OSD 行）：
  · **数值必须真实**；**PSNR/SSIM 等有参考量一律不入实时 OSD**（只在离线实验报告里出现）；
  · 统计**降采样 4×**（512×288 → 128×72 = 9,216 样本）+ **降频 1 Hz**（`ui.osd_stats_decimate` / `ui.osd_stats_hz`）；
  · 曲线图由曲线场求均值后查表绘制。

字体：系统 PSF 控制台字体（stdlib `gzip` 解压 + 解析 PSF1/PSF2），**不内嵌字体表**。
"""

from __future__ import annotations

import gzip
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import _starlight_native as _n

# ── §3.4 布局常量（定稿值；`ui.*` 定稿后由 `OsdRenderer` 交叉核对）──────
OSD_Y = 288
OSD_H = 216
STATUS_ROW_Y = 288
STATUS_ROW_H = 36
PLOT_Y = 324
PLOT_H = 180
BUTTON_Y = 504
BUTTON_H = 96

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

# 3 键（§3.4：触发HQ / 存图 / 复位·退出），各 341 px
BUTTONS = (("TRIGGER HQ", 0), ("SAVE", 341), ("RESET/EXIT", 682))
BUTTON_W = 341

# PSF 字体候选（按优先级；任一可用即可）
FONT_CANDIDATES = (
    "/usr/share/consolefonts/Lat7-Terminus18x10.psf.gz",
    "/usr/share/consolefonts/Lat7-Fixed14.psf.gz",
    "/usr/share/consolefonts/Lat15-VGA16.psf.gz",
    "/usr/share/consolefonts/Uni2-TerminusBold18x10.psf.gz",
)


class Font:
    """PSF1 / PSF2 控制台字体（1bpp 位图）。"""

    def __init__(self, glyph_w: int, glyph_h: int, glyphs: List[bytes], first: int = 32):
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


def _find_font() -> Optional[Font]:
    for p in FONT_CANDIDATES:
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
        now = now_ns if now_ns is not None else time.monotonic_ns()
        if now - self._last_ns < self.period_ns:
            return False
        self._last_ns = now
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
    """把 8 字段 + 两块图示 + 3 个按钮画到画布缓冲上（§3.4）。"""

    def __init__(self, cfg: Any, canvas_w: int, canvas_h: int):
        self.cfg = cfg
        self.cw, self.ch = canvas_w, canvas_h
        self.font = _find_font()
        # 字号来自 `ui.osd_font_px`（M2 键；未定稿时用 §3.4 的 36/26 做比例缩放）
        px = cfg.get("ui.osd_font_px")
        self.scale = self._scale_for(px if isinstance(px, (list, tuple)) else (36, 26))
        self.decimate = cfg.get("ui.osd_stats_decimate") or 4
        self.stats_hz = cfg.get("ui.osd_stats_hz") or 1
        self.stats = Stats(self.decimate, self.stats_hz)

    def _scale_for(self, px: Sequence[int]) -> int:
        if not self.font:
            return 1
        want = int(px[0]) if px else 36
        return max(1, int(round(want / self.font.h)))

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

    # ── 主渲染 ─────────────────────────────────────────────────────────
    def render_into(self, canvas, state: Dict[str, Any]) -> None:
        """`state` 键：mode / fps / lat_capture_ms / lat_submit_ms / curve_hz / countdown_s /
        mean_orig / mean_enh / curve_field / curve_w / curve_h。"""
        _n.osd.fill_rect(canvas, self.cw, self.ch, 0, OSD_Y, self.cw, OSD_H, C_BG)
        _n.osd.fill_rect(canvas, self.cw, self.ch, 0, BUTTON_Y, self.cw, BUTTON_H, C_BG)
        self._status_row(canvas, state)
        self._plot_area(canvas, state)
        self._buttons(canvas)

    def _status_row(self, canvas, st: Dict[str, Any]) -> None:
        """8 字段 4 区（§3.4）。**全部为无参考量**——PSNR/SSIM 一律不入实时 OSD。"""
        mode = st.get("mode", "RUN_DEMO")
        fps = st.get("fps", 0.0)
        l1 = st.get("lat_capture_ms", 0.0)
        l2 = st.get("lat_submit_ms", 0.0)
        chz = st.get("curve_hz", 0.0)
        cd = st.get("countdown_s", 0)
        mo = st.get("mean_orig", 0.0)
        me = st.get("mean_enh", 0.0)
        f = f"{mode}  {fps:.1f}fps  T {l1:.0f}+{l2:.0f}={l1 + l2:.0f}ms  " \
            f"C {chz:.1f}Hz  N{cd}s"
        self.text(canvas, 8, STATUS_ROW_Y + 2, f)
        g = f"L {mo:.1f}  ->  {me:.1f}  d+{me - mo:.1f}"
        self.text(canvas, self.cw - 8 - len(g) * self.font.w * self.scale, STATUS_ROW_Y + 2, g,
                  C_HIST_ENH)

    def _plot_area(self, canvas, st: Dict[str, Any]) -> None:
        """左：tone-mapping 曲线图（R/G/B + 对角参考）；右：亮度直方图（原图灰 / 增强绿）。"""
        half = self.cw // 2
        self._curve_plot(canvas, 0, PLOT_Y, half, PLOT_H, st)
        self._histogram(canvas, half, PLOT_Y, half, PLOT_H)

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
        # 由曲线场求**均值曲线**后查表绘制（§3.4）；用只读缓冲逐通道抽样
        mv = field.to_bytes() if hasattr(field, "to_bytes") else bytes(field)
        fp16 = st.get("curve_precision", "fp16") == "fp16"
        step = max(1, cw // 64)
        for ch in range(3):
            color = (C_R, C_G, C_B)[ch]
            base = ch * cw * chh
            prev: Optional[Tuple[int, int]] = None
            for i in range(0, cw, step):
                s = 0.0
                for y in range(0, chh, max(1, chh // 16)):
                    idx = base + y * cw + i
                    if fp16:
                        off = idx * 2
                        s += _half_to_float(mv[off] | (mv[off + 1] << 8))
                    else:
                        s += mv[idx] / 255.0
                cnt = len(range(0, chh, max(1, chh // 16)))
                v = max(0.0, min(1.0, s / cnt))
                px = x0 + pad + int(i / (cw - 1) * (w - 2 * pad))
                py = y0 + h - pad - int(v * (h - 2 * pad))
                if prev:
                    _n.osd.draw_line(canvas, self.cw, self.ch, prev[0], prev[1], px, py, 2, color)
                prev = (px, py)

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
        for label, bx in BUTTONS:
            _n.osd.fill_rect(canvas, self.cw, self.ch, bx + 2, BUTTON_Y + 2,
                             BUTTON_W - 4, BUTTON_H - 4, C_BTN)
            _n.osd.fill_rect(canvas, self.cw, self.ch, bx + 2, BUTTON_Y + 2, BUTTON_W - 4, 1, C_BTN_EDGE)
            if self.font:
                tw = len(label) * self.font.w * self.scale
                self.text(canvas, bx + (BUTTON_W - tw) // 2, BUTTON_Y + (BUTTON_H - self.font.h * self.scale) // 2,
                          label, C_TEXT)

    def update_stats(self, raw, enh, w: int, h: int) -> bool:
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
    r = OsdRenderer(cfg, 1024, 600)
    print(f"scale={r.scale} decimate={r.decimate} hz={r.stats_hz}")
    print(f"按钮区：{BUTTONS}（各 {BUTTON_W} px，命中区下限 ui.hit_area_min_px）")
    print(f"OSD 指标区 Y {OSD_Y}..{OSD_Y + OSD_H - 1}（状态行 {STATUS_ROW_H} + 图示 {PLOT_H}）")
    return 0 if f else 1


if __name__ == "__main__":
    raise SystemExit(main())
