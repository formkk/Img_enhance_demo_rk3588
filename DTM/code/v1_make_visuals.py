#!/usr/bin/env python3
"""V1: 「码流可视化」+「特征图可视化」（单帧，离线，开发机）。

目的 = 为「DCM 演示路」准备两个**能让人看见 DCM 技术**的画面元素：
  ① 码流可视化 —— 把**真码流的每一个 bit 画成一个像素**（+ 开头 64 字节 hexdump）
                    ⇒ 让"一帧只有 1.7 KB"这件事**可见**；
  ② 特征图可视化 —— 把 DCM 实际在编的两层张量铺成缩略图阵列：
                    · res2 特征 128×144×240（**保真对象**，stride 4）
                    · y      128×36×60 （**真正进熵编码的**，stride 16，即码流里那 128 张"小图"）

口径（与 S7 同）：输入帧按 RM 口径 BGR/255；内容高 540 → 补零到 544 → 到 576（模型 PAD_H）；
链路走 DTM 参考软件本体（`fc.compress` 里就是真 rANS）⇒ 这里的码流是**真码流**。

**注意（口径标注）**：本脚本产的是 **RM/FP32（开发机）** 口径；演示上板后同一脚本照搬，
数字会变成板端 FP16 口径（y 3512 B）。两者都如实标注，不混用。

用法：~/anaconda3/envs/dtm/bin/python DTM/code/v1_make_visuals.py
"""

from __future__ import annotations

import os
import sys
import types
from functools import partial

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# `models/__init__.py` 会拉起 `CLIC_based_codecs` → `import torchac`（JIT 编译，需要 ninja）。
# 本脚本走 MIK_MCM 路径、**完全不碰 torchac**（它只在 CLIC 类的函数体内用到）⇒ 桩掉即可。
sys.modules.setdefault("torchac", types.ModuleType("torchac"))
import s0_export_onnx as s0                                    # noqa: E402
from models.MIK_MCM_codecs import (                            # noqa: E402
    build_feature_coding_Mean_MCM_Scale, build_feature_extractor)

PAD_H, PAD_W = 576, 960          # 模型输入（补零后）
CONTENT_H = 540                  # 我们这帧的真实内容高
MID_H = 544                      # 先补到 544（与 S7 可比），再补到 576
FRAME = "hq_optimize/captures/noise_diag/hq_final_1.png"
OUT = os.path.abspath(os.path.join(HERE, "..", "out_v1"))

FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
BG = (255, 255, 255)
INK = (32, 34, 38)
MUTED = (110, 116, 122)
RULE = (214, 217, 221)
BIT1 = (28, 32, 38)              # bit=1
BIT0 = (238, 240, 242)           # bit=0


def font(size):
    return ImageFont.truetype(FONT_PATH, size)


def load_chain():
    fe = build_feature_extractor()
    fc = build_feature_coding_Mean_MCM_Scale(quality=3, parallel_computing=True)
    ck = torch.load(os.path.join(s0.ROOT, "packages/Model/MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth"),
                    map_location=s0.DEV)
    fe.load_state_dict(ck["feature_extractor"])
    fc.gaussian_conditional.scale_table = fc.gaussian_conditional.scale_table.to(torch.int32)
    fc.load_state_dict(ck["feature_coding"])
    fc._update_per_channel_shifts(ck["per_channel_shifts"]["feature_coding"])
    for m in (fe, fc):
        m.eval()
        m.requires_grad_(False)
    fc.context.quantize_func = partial(fc.quantize)
    return fe, fc


def load_frame():
    """真帧 -> RM 口径张量：RGB->BGR、/255，再补零到 960x576。"""
    proj = os.path.abspath(os.path.join(HERE, "..", ".."))
    p = FRAME if os.path.isabs(FRAME) else os.path.join(proj, FRAME)
    im = Image.open(p).convert("RGB").resize((PAD_W, CONTENT_H))
    a = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0
    a = np.pad(a, ((0, 0), (0, MID_H - CONTENT_H), (0, 0)))
    x = torch.from_numpy(a).unsqueeze(0)
    return F.pad(x, (0, 0, 0, PAD_H - MID_H), mode="constant", value=0)


# ---------------------------------------------------------------- ① 码流可视化
def bits_of(blob: bytes, per_row: int = 128) -> np.ndarray:
    b = np.unpackbits(np.frombuffer(blob, dtype=np.uint8))       # MSB-first，每字节 8 bit
    pad = (-len(b)) % per_row
    if pad:
        b = np.concatenate([b, np.zeros(pad, np.uint8)])
    return (b.reshape(-1, per_row) * 255).astype(np.uint8)       # 1 -> 255(黑), 0 -> 0(白)


def ribbon_png(blob: bytes, scale: int, per_row: int = 128) -> Image.Image:
    m = bits_of(blob, per_row)
    img = Image.fromarray(m, mode="L").resize((per_row * scale, m.shape[0] * scale), Image.NEAREST)
    img = img.convert("RGB")
    arr = np.asarray(img).copy()
    arr[arr[:, :, 0] == 255] = BIT1
    arr[arr[:, :, 0] == 0] = BIT0
    return Image.fromarray(arr, "RGB")


def hexdump_lines(blob: bytes, nbytes: int = 64, per_line: int = 16):
    out = []
    for i in range(0, min(nbytes, len(blob)), per_line):
        chunk = blob[i:i + per_line]
        hx = " ".join(f"{b:02x}" for b in chunk)
        out.append(f"{i:08x}  {hx}")
    return out


def draw_stream_figure(yb: bytes, zb: bytes, total_px: int, raw_bytes: int,
                       title: str = "真实码流：一个像素 = 1 bit") -> Image.Image:
    scale, per_row = 4, 128
    yb_img = ribbon_png(yb, scale, per_row)
    zb_img = ribbon_png(zb, scale, per_row)
    n = len(yb) + len(zb)
    W, M = 900, 40
    H = 118 + yb_img.height + 46 + zb_img.height + 34 + 5 * 30 + 40
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    f_title, f_body, f_small, f_mono = font(26), font(17), font(15), font(15)

    d.text((M, 26), title, font=f_title, fill=INK)
    d.text((M, 58), f"y 流（特征）{len(yb)} B　+　z 流（超先验）{len(zb)} B　=　{n} B/帧"
                    f"　{n * 8 / total_px:.4f} bpp @960×544", font=f_body, fill=MUTED)
    d.text((M, 84), f"同一帧原始 960×540×3 = {raw_bytes / 1e6:.2f} MB　⇒　约 {round(raw_bytes / n)}× 压缩",
           font=f_body, fill=MUTED)

    top = 118
    im.paste(yb_img, (M, top))
    d.rectangle([M - 1, top - 1, M + yb_img.width, top + yb_img.height], outline=RULE)
    tx = M + yb_img.width + 24
    d.text((tx, top + 6), "y 流（编码后的特征）", font=f_body, fill=INK)
    d.text((tx, top + 32), f"{len(yb)} B = {len(yb) * 8:,} bit", font=f_mono, fill=MUTED)
    d.text((tx, top + 58), f"{yb_img.height // scale} 行 × 128 bit", font=f_small, fill=MUTED)

    top2 = top + yb_img.height + 46
    im.paste(zb_img, (M, top2))
    d.rectangle([M - 1, top2 - 1, M + zb_img.width, top2 + zb_img.height], outline=RULE)
    d.text((tx, top2 - 6), "z 流（超先验）", font=f_body, fill=INK)
    d.text((tx, top2 + 18), f"{len(zb)} B = {len(zb) * 8:,} bit", font=f_mono, fill=MUTED)

    top3 = top2 + zb_img.height + 34
    d.text((M, top3), "码流开头 64 字节（hexdump）", font=f_body, fill=INK)
    for i, line in enumerate(hexdump_lines(yb)):
        d.text((M, top3 + 26 + i * 20), line, font=f_mono, fill=MUTED)
    return im


# ---------------------------------------------------------------- ② 特征图可视化
def norm_u8(ch: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(ch, 1.0), np.percentile(ch, 99.0)
    if hi - lo < 1e-9:
        hi, lo = float(ch.max()), float(ch.min())
    v = np.clip((ch - lo) / max(hi - lo, 1e-9), 0, 1)
    return (v * 255).astype(np.uint8)


def atlas(t: torch.Tensor, thumb_w: int, thumb_h: int, title: str, note: str) -> Image.Image:
    a = t[0].detach().cpu().numpy() if t.dim() == 4 else t.detach().cpu().numpy()
    C = a.shape[0]
    cols, gap, M = 16, 3, 40
    rows = (C + cols - 1) // cols
    W = M * 2 + cols * thumb_w + (cols - 1) * gap
    H = 96 + rows * thumb_h + (rows - 1) * gap + 46
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((M, 26), title, font=font(24), fill=INK)
    d.text((M, 60), note, font=font(15), fill=MUTED)
    for i in range(C):
        r, c = divmod(i, cols)
        ch = norm_u8(a[i])
        t_img = Image.fromarray(ch, mode="L").convert("RGB")
        method = Image.LANCZOS if (ch.shape[1] > thumb_w or ch.shape[0] > thumb_h) else Image.NEAREST
        t_img = t_img.resize((thumb_w, thumb_h), method)
        x = M + c * (thumb_w + gap)
        y = 96 + r * (thumb_h + gap)
        im.paste(t_img, (x, y))
        d.rectangle([x - 1, y - 1, x + thumb_w, y + thumb_h], outline=RULE)
    d.text((M, H - 30), "每通道独立拉伸到 p1–p99（仅显示用，不改数据）· 每行 16 个通道，从通道 0 起",
           font=font(14), fill=MUTED)
    return im


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    fe, fc = load_chain()
    x = load_frame()
    q_map = torch.full((1, 1, PAD_H // 16, PAD_W // 16), s0.Q_FACTOR, dtype=torch.float32)

    with torch.no_grad():
        f0 = fe(x)                                    # res2 特征: 128x144x240
        y = fc.g_a_sft(fc.g_a(f0), q_map)             # 进熵编码的潜在: 128x36x60
        out = fc.compress(f0, q_map, rate_control_flag=False,
                          after_padding_H=PAD_H, after_padding_W=PAD_W)
    yb = bytes(out["strings"][0][0])
    zb = bytes(out["strings"][1][0])

    print(f"帧: {FRAME}  内容 {PAD_W}x{CONTENT_H}（补零到 {PAD_W}x{PAD_H}）")
    print(f"真码流: y={len(yb)} B  z={len(zb)} B  合计={len(yb) + len(zb)} B"
          f"  bpp@960x544={(len(yb) + len(zb)) * 8 / (960 * 544):.5f}")
    print(f"res2 特征 f0: {tuple(f0.shape)}  值域 [{float(f0.min()):.2f}, {float(f0.max()):.2f}]"
          f"  均值 {float(f0.mean()):.3f}  标准差 {float(f0.std()):.3f}")
    print(f"潜在   y     : {tuple(y.shape)}  值域 [{float(y.min()):.2f}, {float(y.max()):.2f}]"
          f"  均值 {float(y.mean()):.3f}  标准差 {float(y.std()):.3f}"
          f"  非零占比 {float((y.abs() > 1e-6).float().mean()):.3f}")

    p1 = os.path.join(OUT, "stream_ribbon.png")
    draw_stream_figure(yb, zb, 960 * 544, PAD_W * CONTENT_H * 3).save(p1)
    p2 = os.path.join(OUT, "feature_atlas_y.png")
    atlas(y, 54, 90, "y = 真正进熵编码的特征（128×36×60）",
          "这是码流里那 128 张「小图」；DCM 编的就是它们，不是像素").save(p2)
    p3 = os.path.join(OUT, "feature_atlas_res2.png")
    atlas(f0, 54, 90, "res2 特征 = DCM 的保真对象（128×144×240, stride 4）",
          "机器下游真正要用的那层；码流只带它的紧凑潜在 y（见另一张），解码后由 y 重建回这一层").save(p3)
    with open(os.path.join(OUT, "stream_head.bin"), "wb") as f:
        f.write(yb)
    print(f"产物 -> {p1}\n        {p2}\n        {p3}\n        {os.path.join(OUT, 'stream_head.bin')}")
    print("V1_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
