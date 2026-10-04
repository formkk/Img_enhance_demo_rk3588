#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/color_ratio_review.py —— 实时路「保色比」量化审查工具（**只读**）

地位：交付工具（§10.3 登记）。效力：**审查素材与量化随行，非验收依据**（规则 `00-ssot.md` §3.5）。
用途：作为实时路微调方案**第②层验收（"色"）的可复现手段** —— 对「增强前 / 增强后」同帧图像
量化 **保色比保持率 / 削顶占比 / 通道平衡 / 暗部与平坦区噪声 / 平均亮度**，并把**所用口径逐条标注**。
**判据（合格线）一律由调用方给或不判 —— 本脚本不硬编码任何"合格线"**（判据待 §15 裁定）。

两种输入形态
------------
(a) **单图双区**（一张画布里含"原图区/增强区"，如 `/canvas.png` 的左右两窗）：
      python3 tools/color_ratio_review.py --single <IMG> \
              --region "L,R|T,B" --region "L,R|T,B"
      ``` 或坐标：`--rect x0,y0,w,h`（可给两次）；**第一个区 = 增强前，第二个区 = 增强后**。
(b) **双图**（增强前/后各一张，同帧）：
      python3 tools/color_ratio_review.py --before A.png --after B.png

指标清单与口径出处（**逐条标注；不复制取值、不裁定未决项**）
----------------------------------------------------------
1. **保色比保持率**：逐像素 `(R/G)_后 / (R/G)_前 − 1`（相对）与 `(R/G)_后 − (R/G)_前`（绝对），
   同 `B/G` ⇒ 汇总为 **逐像素 + 16×16 分块**的 **均值 / 中位 / P95**。
   方法学出处 = 主基准 §14 追加 157 ②(c)（`(R/G)`/`(B/G)` 偏差）；本工具 = **重建版**（见下"重建说明"）。
2. **削顶占比**：`==255` 的百分比，**分通道**（R/G/B；另给"任一通道"）。
   依据 = **削顶是保比链上唯一会破坏色比的环节**（`native/apply_core.c` 的 `store_group4` 对缩放后
   通道各自 clip 到 [0,1]，见 §14 追加 157；本工具不重新定义内核，仅量化其输出）。
3. **通道平衡 `gr_balance`** = `mean(G)/mean(R)`（全窗 + 最暗 25% 块）——
   口径 **照抄** `tools/realtime_frame_review.py:162-171`（本工具直接 import 复用）。
4. **暗部/平坦区噪声**（空间口径）：
   · 暗部块 σ **直接复用** `tools/realtime_frame_review.py:129-159` 的 `dark_block_stats`
     （16×16 块、**块均值=R 通道均值最小 25%** ⇒ σ 中位数、σ/均值）；
   · 平坦区 = **原图 Sobel 能量最低 10% 的块**（口径出处 = `docs/evidence/rt-zde-optimize-0929.md:23`
     与 `docs/evidence/zde-int8-banding-ab.md:81`；掩码**取自"增强前"图**、同掩码套用于"增强后"）；
     报 σ、σ/均值、**色度 σ(R−G) / σ(B−G)**（色度口径 = 追加 164 ⑧ / `rt-zde-optimize-0929.md:29`）。
5. **平均亮度**与**亮度偏移**（增强后 − 增强前）：亮度 = **BT.601 整数近似** `(77R+150G+29B)>>8`，
   口径 **照抄** `tools/realtime_frame_review.py:174-180`（本工具直接 import 复用）。
6. **BGR/RGB 口径显式**：图像读取后**统一转 RGB**（惯例见 `tools/hq_eval.py:151-154` 的
   `COLOR_BGR2RGB`）；`--input-order {rgb,bgr}` 指定源序（默认 `rgb`），**所用口径写入 JSON** `color_order`。
   本工具**只做通道序归一化，不做任何色调/色彩处理**。

只读纪律与非线性后处理禁令（**硬约束**）
----------------------------------------
本工具**只读**输入图像，**不写入、不删改任何输入产物**；输出只落在 `--json-out` / `--png-out`。
**禁止任何 γ / 对比度 / 直方图均衡 / 色调映射等非线性后处理**（规则 `.trae/rules/10-evidence.md` §2，
v3.83 裁定 `_view` γ 副本已废弃）；`--png-out` 产物**仅供目视查看、一律不为判据**：
侧并图 = 原样裁剪拼接（零处理）；差分图 = 相对保色比偏差的**灰度幅值图**（非链路输出的色调后处理）。

重建说明（本文件为重建版及依据）
--------------------------------
此前实时路保色比对比用的是**临时脚本**（未沉淀）：`docs/evidence/reports/rt-color-1001/` 内**只有
报告与图、无脚本**（`cmp_tool_report.json` / `zde_review2_report.json` 由 `tools/realtime_frame_review.py`
产出；保色比对比脚本本身**未取得**）。本工具按该目录报告的记录**反推口径重建**，并对齐既有报告：
`tools/realtime_frame_review.py` 的 `mean_rgb` / `gr_balance` / `dark_block_stats` **原样复用**（非复刻），
故 `gr_balance`、暗部块 σ、逐通道均值三项**与既有报告逐位一致**（见 §14 追加 177 的实跑记录）。

用法 / 输出
-----------
    --json-out PATH   指标 JSON（缺省只打印到 stdout）
    --png-out DIR     目视可视化目录（侧并 / 差分；**仅供查看，不为判据**）

**本工具只读输入；判据一律"待裁定"**（不在脚本里硬编码合格线）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
# **复用既有纯函数口径**（`dark_block_stats` / `gr_balance` / `mean_rgb` / `max_rgb` /
# `luma_from_rgb888` / `_median` / `_percentile`）—— 避免复刻导致口径漂移。
# ⚠️ 该模块在开发机会把交付扩展导入失败记为 `_IMPORT_ERR`（纯函数仍可用）；本工具**不依赖扩展**。
import realtime_frame_review as rfr  # noqa: E402

BS = rfr.SNR_BS          # 16：与既有块口径一致（realtime_frame_review.py:75）
DARK_FRAC = 0.25         # 暗部块 = 块均值最小 25%（realtime_frame_review.py:129）
FLAT_FRAC = 0.10         # 平坦区 = Sobel 能量最低 10%（rt-zde-optimize-0929.md:23）

# 口径出处表（逐条标注；供报告随行，不复制取值）
CALIBERS = [
    {"metric": "保色比保持率 (R/G, B/G 偏差)", "source": "主基准 §14 追加 157 ②(c)；本工具重建（临时脚本未取得）"},
    {"metric": "削顶占比 (=255, 分通道)", "source": "native/apply_core.c store_group4（§14 追加 157 ②）；保比链唯一破坏色比的环节"},
    {"metric": "通道平衡 gr_balance", "source": "tools/realtime_frame_review.py:162-171（直接复用）"},
    {"metric": "暗部块 σ (空间口径)", "source": "tools/realtime_frame_review.py:129-159（直接复用 dark_block_stats）"},
    {"metric": "平坦区筛选 (Sobel 能量最低 10%)", "source": "docs/evidence/rt-zde-optimize-0929.md:23；docs/evidence/zde-int8-banding-ab.md:81"},
    {"metric": "色度 σ(R−G) / σ(B−G)", "source": "主基准 §14 追加 164 ⑧；docs/evidence/rt-zde-optimize-0929.md:29"},
    {"metric": "亮度 (BT.601 整数近似)", "source": "tools/realtime_frame_review.py:174-180（直接复用 luma_from_rgb888）"},
    {"metric": "RGB 口径归一化", "source": "tools/hq_eval.py:151-154（COLOR_BGR2RGB 惯例）"},
]


# ── I/O（只读；统一转 RGB，禁后处理）────────────────────────────────────────
def load_rgb(path, input_order="rgb"):
    """读图并**统一为 RGB**（`hq_eval.py:151-154` 口径）。只做通道序归一化，无色调处理。"""
    with Image.open(path) as im:
        arr = np.asarray(im.convert("RGB"), dtype=np.uint8)   # H×W×3
    applied = input_order
    if input_order == "bgr":          # 源为 BGR（如 cv2.imwrite 产物）⇒ B/G/R → R/G/B
        arr = arr[:, :, ::-1].copy()
        applied = "bgr→rgb"
    return arr, applied


def parse_region(spec):
    """`L,R|T,B` → (L, R, T, B)（**左闭右开**）。"""
    xr, yr = spec.split("|")
    L, R = (int(v) for v in xr.split(","))
    T, B = (int(v) for v in yr.split(","))
    return L, R, T, B


def parse_rect(spec):
    """`x0,y0,w,h` → (x0, x0+w, y0, y0+h)（**左闭右开**）。"""
    x0, y0, w, h = (int(v) for v in spec.split(","))
    return x0, x0 + w, y0, y0 + h


def crop(arr, box):
    L, R, T, B = box
    h, w = arr.shape[:2]
    if not (0 <= L < R <= w and 0 <= T < B <= h):
        raise ValueError("区域越界：%r 不在图像 %dx%d 内" % (box, w, h))
    return arr[T:B, L:R, :].copy()


# ── 指标（复用既有口径）────────────────────────────────────────────────────
def image_metrics(rgb):
    """逐图指标：均值/max、gr_balance、暗部块、平均亮度、削顶占比。"""
    h, w = rgb.shape[:2]
    buf = rgb.tobytes()                       # RGB888 连续缓冲（与既有工具同布局）
    dk = rfr.dark_block_stats(buf, w, h, bs=BS, frac=DARK_FRAC)
    luma = np.frombuffer(rfr.luma_from_rgb888(buf, w, h), dtype=np.uint8)
    ch_eq255 = {c: float((rgb[:, :, i] == 255).mean() * 100.0)
                for i, c in enumerate("RGB")}
    any255 = float((np.any(rgb == 255, axis=2)).mean() * 100.0)
    return {
        "shape_hw": [h, w],
        "mean_rgb": rfr.mean_rgb(buf),                        # 复用（realtime_frame_review.py:120-122）
        "max_rgb": rfr.max_rgb(buf),                          # 复用
        "gr_balance": rfr.gr_balance(buf, w, h, dk),          # 复用（:162-171）
        "dark_block": {**dk,
                       "sigma_over_mean": (round(dk["sigma_median"] / dk["r_mean"], 4)
                                           if dk["r_mean"] else None)},   # 暗部块 σ（:129-159）
        "luma": {                                             # 复用（:174-180，BT.601 整数近似）
            "mean": round(float(luma.mean()), 3),
            "median": round(float(np.median(luma)), 3),
            "p95": round(float(np.percentile(luma, 95)), 3),
        },
        "clipping_eq255_pct": {**ch_eq255, "any_channel": any255},
    }


def _sobel_energy(gray):
    """Sobel 梯度幅值（|gx|+|gy|）；灰度域。用于**平坦区**筛选（`rt-zde-optimize-0929.md:23`）。"""
    g = gray.astype(np.float64)
    gx = np.zeros_like(g)
    gy = np.zeros_like(g)
    gx[1:-1, 1:-1] = ((-1 * g[:-2, :-2] + 1 * g[:-2, 2:])
                      + (-2 * g[1:-1, :-2] + 2 * g[1:-1, 2:])
                      + (-1 * g[2:, :-2] + 1 * g[2:, 2:]))
    gy[1:-1, 1:-1] = ((-1 * g[:-2, :-2] - 2 * g[:-2, 1:-1] - 1 * g[:-2, 2:])
                      + (1 * g[2:, :-2] + 2 * g[2:, 1:-1] + 1 * g[2:, 2:]))
    return np.abs(gx) + np.abs(gy)


def flat_mask(rgb_before, bs=BS, frac=FLAT_FRAC):
    """**平坦区掩码**（块级 bool）：原图 Sobel 能量最低 `frac` 的 bs×bs 块。

    掩码**取自"增强前"图**（口径出处 = `rt-zde-optimize-0929.md:23` 的「原图 Sobel 能量最低 10 %」）。
    """
    h, w = rgb_before.shape[:2]
    luma = np.frombuffer(rfr.luma_from_rgb888(rgb_before.tobytes(), w, h),
                         dtype=np.uint8).reshape(h, w)
    energy = _sobel_energy(luma)
    bw, bh = w // bs, h // bs
    eblocks = np.array([[energy[by * bs:(by + 1) * bs, bx * bs:(bx + 1) * bs].mean()
                         for bx in range(bw)] for by in range(bh)])
    flat = eblocks.flatten()
    order = np.argsort(flat, kind="stable")
    k = max(1, int(flat.size * frac))
    keep = set(order[:k].tolist())
    mask = np.zeros((bh, bw), dtype=bool)
    for idx in keep:
        mask[idx // bw, idx % bw] = True
    return mask, bw, bh


def region_block_stats(rgb_before, rgb_after, mask, bw, bh, bs=BS):
    """在给定块掩码上统计 σ / σ·均值⁻¹ / 色度 σ —— **mask 由"增强前"图选出、两图同掩码**。"""
    def _one(rgb):
        sig_r, sig_g, sig_b, sig_rg, sig_bg, ratio = [], [], [], [], [], []
        for by in range(bh):
            for bx in range(bw):
                if not mask[by, bx]:
                    continue
                blk = rgb[by * bs:(by + 1) * bs, bx * bs:(bx + 1) * bs, :].astype(np.float64)
                R, G, B = blk[:, :, 0], blk[:, :, 1], blk[:, :, 2]
                sr, sg = R.std(), G.std()
                sig_r.append(sr)
                sig_g.append(sg)
                sig_b.append(B.std())
                sig_rg.append((R - G).std())
                sig_bg.append((B - G).std())
                mr = R.mean()
                if mr:
                    ratio.append(sr / mr)          # σ(R)/mean(R)（沿用 realtime_frame_review 的 R 通道口径）
        n = len(sig_r)
        return {
            "blocks": int(mask.sum()),
            "sigma_R_median": round(float(np.median(sig_r)), 3) if n else None,
            "sigma_G_median": round(float(np.median(sig_g)), 3) if n else None,
            "sigma_B_median": round(float(np.median(sig_b)), 3) if n else None,
            "sigmaR_over_meanR_median": round(float(np.median(ratio)), 4) if ratio else None,
            "chroma_sigma_RminusG_median": round(float(np.median(sig_rg)), 3) if n else None,
            "chroma_sigma_BminusG_median": round(float(np.median(sig_bg)), 3) if n else None,
        }
    return {"before": _one(rgb_before), "after": _one(rgb_after)}


# ── 保色比保持率（重建；逐像素 + 分块）──────────────────────────────────────
def _dev_stats(dev_abs, dev_rel):
    """逐像素偏差统计（均值/中位/P95）。"""
    def _s(a):
        return {"mean": round(float(a.mean()), 6), "median": round(float(np.median(a)), 6),
                "p95": round(float(np.percentile(a, 95)), 6)}
    return {"n_pixels": int(dev_abs.size), "abs": _s(dev_abs), "rel": _s(dev_rel)}


def ratio_preservation(b_before, b_after, bs=BS):
    """保色比保持率：`(R/G)` / `(B/G)` 的**增强前后偏差**（绝对与相对）逐像素 + 分块。"""
    out = {"note": "相对 = |后/前 − 1|；绝对 = |后 − 前|；按同帧同像素（规则 10-evidence.md §2）"}
    if b_before.shape != b_after.shape:
        out["error"] = "前后形状不一致（%r vs %r）⇒ 逐像素项不可算" % (
            list(b_before.shape[:2]), list(b_after.shape[:2]))
        return out
    bf = b_before.astype(np.float64)
    af = b_after.astype(np.float64)
    eps = 1.0
    valid = ((bf[:, :, 1] > eps) & (af[:, :, 1] > eps)
             & (bf[:, :, 0] > eps) & (af[:, :, 0] > eps)
             & (bf[:, :, 2] > eps) & (af[:, :, 2] > eps))
    for ch, i in (("R_over_G", 0), ("B_over_G", 2)):
        br = bf[:, :, i] / bf[:, :, 1]
        ar = af[:, :, i] / af[:, :, 1]
        m = valid & (br > 1e-6)
        out[ch] = _dev_stats(np.abs(ar[m] - br[m]), np.abs(ar[m] / br[m] - 1.0))
    # 分块（16×16）：块均值比 → 块级偏差
    h, w = b_before.shape[:2]
    bw, bh = w // bs, h // bs
    for ch, i in (("R_over_G", 0), ("B_over_G", 2)):
        dabs, drel = [], []
        for by in range(bh):
            for bx in range(bw):
                bb = bf[by * bs:(by + 1) * bs, bx * bs:(bx + 1) * bs, :]
                aa = af[by * bs:(by + 1) * bs, bx * bs:(bx + 1) * bs, :]
                if bb[:, :, 1].mean() <= eps or aa[:, :, 1].mean() <= eps:
                    continue
                br = bb[:, :, i].mean() / bb[:, :, 1].mean()
                ar = aa[:, :, i].mean() / aa[:, :, 1].mean()
                if br <= 1e-6:
                    continue
                dabs.append(abs(ar - br))
                drel.append(abs(ar / br - 1.0))
        n = len(dabs)
        out[ch]["block"] = ({"n_blocks": n,
                             "abs_mean": round(float(np.mean(dabs)), 6),
                             "abs_median": round(float(np.median(dabs)), 6),
                             "abs_p95": round(float(np.percentile(dabs, 95)), 6),
                             "rel_mean": round(float(np.mean(drel)), 6),
                             "rel_median": round(float(np.median(drel)), 6),
                             "rel_p95": round(float(np.percentile(drel, 95)), 6)}
                            if n else None)
    return out


# ── PNG（仅供查看，不为判据；零色调后处理）──────────────────────────────────
def _to_pil(rgb):
    return Image.fromarray(np.ascontiguousarray(rgb))


def write_side_by_side(path, before, after):
    """侧并图：原样裁剪拼接（零处理；高度对齐取较小者）。"""
    hb, wb = before.shape[:2]
    ha, wa = after.shape[:2]
    hh = min(hb, ha)
    b = _to_pil(before[:hh])
    a = _to_pil(after[:hh])
    canvas = Image.new("RGB", (b.width + a.width, hh))
    canvas.paste(b, (0, 0))
    canvas.paste(a, (b.width, 0))
    canvas.save(path)
    return {"path": path, "kind": "side_by_side(原样拼接、零处理)"}


def write_ratio_diff(path, before, after, ch=0):
    """相对保色比偏差的**灰度幅值图**（仅供查看；非链路输出的色调后处理）。"""
    if before.shape != after.shape:
        return None
    bf = before.astype(np.float64)
    af = after.astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        br = bf[:, :, ch] / bf[:, :, 1]
        ar = af[:, :, ch] / af[:, :, 1]
        rel = np.abs(ar / br - 1.0)
    rel = np.nan_to_num(rel, nan=0.0, posinf=0.0)
    gray = np.clip(rel * 255.0, 0, 255).astype(np.uint8)     # 直接幅值，无 γ/色偏
    _to_pil(np.stack([gray] * 3, axis=-1)).save(path)
    return {"path": path, "kind": "ratio_rel_diff/gray(幅值图、仅供查看)"}


# ── 主流程 ─────────────────────────────────────────────────────────────────
def build_inputs(a):
    """解析两种输入形态 → (label, before_rgb, after_rgb, input_meta)。"""
    if a.single:
        if not a.region and not a.rect:
            raise SystemExit("ERR 单图双区须给两次 `--region` 或 `--rect`")
        specs = a.rect if a.rect else a.region
        if len(specs) != 2:
            raise SystemExit("ERR 单图双区须**恰给两次**区域（%d 次）" % len(specs))
        img, applied = load_rgb(a.single, a.input_order)
        boxes = [parse_rect(s) if a.rect else parse_region(s) for s in specs]
        meta = {"mode": "single_dual_region", "image": os.path.abspath(a.single),
                "region_specs": specs, "box_before": list(boxes[0]), "box_after": list(boxes[1]),
                "color_order_applied": applied}
        return "单图双区", crop(img, boxes[0]), crop(img, boxes[1]), meta
    if a.before and a.after:
        b, ob = load_rgb(a.before, a.input_order)
        af, oa = load_rgb(a.after, a.input_order)
        meta = {"mode": "dual_image", "before": os.path.abspath(a.before),
                "after": os.path.abspath(a.after),
                "color_order_applied": "%s / %s" % (ob, oa)}
        return "双图", b, af, meta
    raise SystemExit("ERR 请给 `--before A --after B`（双图）或 `--single IMG --region/--rect ×2`")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="实时路保色比量化审查（只读；非验收依据；判据待裁定）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group("输入（二选一）")
    src.add_argument("--before", default="", help="增强前图（双图形态）")
    src.add_argument("--after", default="", help="增强后图（双图形态）")
    src.add_argument("--single", default="", help="单图（含原图区/增强区）")
    src.add_argument("--region", action="append", default=[],
                     metavar="L,R|T,B", help="区域（左闭右开）；单图双区须给两次")
    src.add_argument("--rect", action="append", default=[],
                     metavar="x0,y0,w,h", help="矩形；单图双区须给两次")
    src.add_argument("--input-order", choices=["rgb", "bgr"], default="rgb",
                     help="源通道序（默认 rgb；bgr = 读后 BGR→RGB，惯例见 hq_eval.py:151-154）")
    ap.add_argument("--json-out", default="", help="指标 JSON 输出路径（缺省只打印 stdout）")
    ap.add_argument("--png-out", default="", help="目视可视化目录（侧并/差分；仅供查看、不为判据）")
    a = ap.parse_args(argv)

    label, before, after, in_meta = build_inputs(a)

    mask, bw, bh = flat_mask(before)
    before_m = {**image_metrics(before),
                "flat_region": region_block_stats(before, before, mask, bw, bh)["before"]}
    after_m = {**image_metrics(after),
               "flat_region": region_block_stats(before, after, mask, bw, bh)["after"]}
    rep = {
        "schema": "color-ratio-review/v1",
        "tool": "tools/color_ratio_review.py",
        "scope": "实时路微调方案第②层（色）审查手段；口径出处见 calibers",
        "status": "审查素材与量化随行，**非验收依据**（规则 00-ssot.md §3.5）",
        "input": in_meta,
        "calibers": CALIBERS,
        "thresholds": "**待 §15 裁定**（本脚本不硬编码任何合格线）",
        "params": {"block_px": BS, "dark_frac": DARK_FRAC, "flat_frac": FLAT_FRAC,
                   "color_order_applied": in_meta.get("color_order_applied")},
        "before": before_m,
        "after": after_m,
        "compare": {
            "ratio_preservation": ratio_preservation(before, after),      # ① 保色比保持率
            "clipping_eq255_pct_delta": {                                  # ② 削顶占比（后 − 前）
                c: round(after_m["clipping_eq255_pct"][c]
                         - before_m["clipping_eq255_pct"][c], 4)
                for c in ("R", "G", "B", "any_channel")},
            "gr_balance": {                                                # ③ 通道平衡
                "before": before_m["gr_balance"], "after": after_m["gr_balance"],
                "delta_whole": round(after_m["gr_balance"]["whole_g_over_r"]
                                     - before_m["gr_balance"]["whole_g_over_r"], 4),
            },
            "luma_shift": round(after_m["luma"]["mean"]
                                - before_m["luma"]["mean"], 3),            # ⑤ 亮度偏移
        },
        "notes": [
            "平坦区掩码取自**增强前**图（Sobel 能量最低 10% 块），同掩码套用于增强后",
            "保色比保持率为**重建口径**（临时脚本未取得；见文件头「重建说明」）",
            "本工具只读输入；无任何色调/色彩后处理（规则 10-evidence.md §2）",
        ],
    }

    # PNG 可视化（仅供查看、不为判据）
    if a.png_out:
        os.makedirs(a.png_out, exist_ok=True)
        vis = []
        vis.append(write_side_by_side(os.path.join(a.png_out, "side_by_side.png"), before, after))
        for ch, nm in ((0, "R_over_G"), (2, "B_over_G")):
            v = write_ratio_diff(os.path.join(a.png_out, "%s_rel_diff.png" % nm), before, after, ch)
            if v:
                vis.append(v)
        rep["png_out"] = vis

    text = json.dumps(rep, ensure_ascii=False, indent=2)
    if a.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(a.json_out)), exist_ok=True)
        with open(a.json_out, "w") as fh:
            fh.write(text + "\n")
        print("[report] %s" % a.json_out)
    print(text)
    print("\n[%s] gr_balance 前 %.4f → 后 %.4f（Δ%+.4f）｜亮度 %s → %s（Δ%+.3f）"
          % (label,
             rep["compare"]["gr_balance"]["before"]["whole_g_over_r"],
             rep["compare"]["gr_balance"]["after"]["whole_g_over_r"],
             rep["compare"]["gr_balance"]["delta_whole"],
             rep["before"]["luma"]["mean"], rep["after"]["luma"]["mean"],
             rep["compare"]["luma_shift"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())