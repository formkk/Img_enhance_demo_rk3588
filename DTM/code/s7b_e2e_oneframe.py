#!/usr/bin/env python3
"""S7-B: 端到端单帧贯通（离线，开发机）.

链: 预处理(缩到 960x544 + 补零到 960x576) -> feature_extractor -> g_a -> g_a_sft
    -> hyper_encoder -> z_hat -> hyper_decoder/hyper_scale_decoder -> context -> means
    -> **熵编码（真码流）** -> **熵解码** -> context.decompress -> g_s_sft -> g_s (= 重建特征, 128ch)
    -> **REC** -> 重建像素 (BGR uint8, 同 RM 口径)
"""
from __future__ import annotations

import os
import struct
import sys
import time
import zlib
from functools import partial

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s0_export_onnx as s0                                   # noqa: E402
from models.MIK_MCM_codecs import (                           # noqa: E402
    build_feature_coding_Mean_MCM_Scale, build_feature_extractor)
from models.rec.rec import build_rec_model                    # noqa: E402

PAD_H, PAD_W = 576, 960
SRC_H, SRC_W = 544, 960                                       # 内容尺寸(上下补零到 576)
OUT = os.path.abspath(os.path.join(HERE, "..", "out_s7"))


def write_png(path, u8, w, h):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += u8[y * w * 3:(y + 1) * w * 3]

    def ch(t, d):
        return len(d).to_bytes(4, "big") + t + d + (zlib.crc32(t + d) & 0xFFFFFFFF).to_bytes(4, "big")
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + ch(b"IDAT", zlib.compress(bytes(raw), 6)) + ch(b"IEND", b""))


def main():
    os.makedirs(OUT, exist_ok=True)
    fe = build_feature_extractor()
    fc = build_feature_coding_Mean_MCM_Scale(quality=3, parallel_computing=True)
    rec = build_rec_model()
    ck = torch.load(os.path.join(s0.ROOT, "packages/Model/MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth"),
                    map_location=s0.DEV)
    fe.load_state_dict(ck["feature_extractor"])
    fc.gaussian_conditional.scale_table = fc.gaussian_conditional.scale_table.to(torch.int32)
    fc.load_state_dict(ck["feature_coding"])
    fc._update_per_channel_shifts(ck["per_channel_shifts"]["feature_coding"])
    rec.load_state_dict(torch.load(os.path.join(s0.ROOT, "packages/Model/REC_model/model_rec.pt"),
                                   map_location=s0.DEV))
    for m in (fe, fc, rec):
        m.eval()
        m.requires_grad_(False)
    fc.context.quantize_func = partial(fc.quantize)

    im = Image.open(os.path.join(s0.ROOT, "logs/samples/000000481386.jpg")).convert("RGB").resize((SRC_W, SRC_H))
    a = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0   # 与 RM 一致: BGR/255
    x = F.pad(torch.from_numpy(a).unsqueeze(0), (0, 0, 0, PAD_H - SRC_H), mode="constant", value=0)
    q_map = torch.full((1, 1, PAD_H // 16, PAD_W // 16), s0.Q_FACTOR)

    ts = {}
    with torch.no_grad():
        t = time.time(); f0 = fe(x); ts["feature_extractor"] = time.time() - t
        t = time.time()
        out = fc.compress(f0, q_map, rate_control_flag=False, after_padding_H=PAD_H, after_padding_W=PAD_W)
        ts["编码(前向+熵编码)"] = time.time() - t
        yb, zb = out["strings"][0][0], out["strings"][1][0]
        t = time.time(); feat = fc.decompress(out["strings"], out["shape"], q_map)["x_hat"]
        ts["解码(熵解码+重建特征)"] = time.time() - t
        t = time.time(); pix = rec(feat); ts["REC(特征->像素)"] = time.time() - t

    n = len(yb) + len(zb)
    print(f"真码流: y={len(yb)} B  z={len(zb)} B  合计={n} B")
    for tag, npx in (("960x544(模型输入)", 960 * 544), ("960x540(内容)", 960 * 540), ("1920x1080(原图)", 1920 * 1080)):
        print(f"  真码率 {n*8/npx:.5f} bpp  (对 {tag})")
    for k, v in ts.items():
        print(f"  耗时 {k} = {v:.2f} s")
    print(f"重建特征 128ch 值域 [{float(feat.min()):.3f}, {float(feat.max()):.3f}] 形状 {tuple(feat.shape)}")
    print(f"重建像素 形状 {tuple(pix.shape)} 值域 [{float(pix.min()):.1f}, {float(pix.max()):.1f}] 均值 {float(pix.mean()):.1f}")

    # 输入(可视): 用 BGR/255 输入反翻回 RGB 存图
    u8_in = np.rint(np.clip(x[0, :, :SRC_H, :SRC_W].permute(1, 2, 0).numpy(), 0, 1) * 255).astype(np.uint8)
    write_png(os.path.join(OUT, "input.png"), u8_in[:, :, ::-1].tobytes(), SRC_W, SRC_H)
    # 重建: REC 输出为 BGR -> 翻成 RGB（同 RM 的 [:, :, ::-1]），裁到内容区
    u8 = np.clip(pix[0, :, :SRC_H, :SRC_W].permute(1, 2, 0).numpy(), 0, 255).astype(np.uint8)
    write_png(os.path.join(OUT, "rec_from_bitstream.png"), u8[:, :, ::-1].tobytes(), SRC_W, SRC_H)
    print(f"产物 -> {OUT}/input.png , {OUT}/rec_from_bitstream.png")


if __name__ == "__main__":
    main()
