#!/usr/bin/env python3
"""S7-A: 熵编码 CPU 真码流 + 往返.

按 DTM 自身 API 跑通(不重写算法):
  compress : y=g_a(x) -> g_a_sft -> z=h_a(y) -> z_hat=round(z); psi=h_s(z_hat); scale=hyper_scale(z_hat)
             -> context(y,psi) -> means ; indexes=build_indexes(scale)
             -> y_strings=gaussian_conditional.compress(y,indexes,means) ; z_strings=entropy_bottleneck.compress(z)
  decompress: z_hat <- strings[1]; psi/scale <- h_s/hyper_scale(z_hat)
             y_residual <- gaussian_conditional.decompress(strings[0], indexes)
             y_hat = context.decompress(y_residual, psi) -> g_s_sft -> g_s -> x_hat
产出: 真字节数(bpp) + 往返一致性 + 重建图 PNG
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
import s0_export_onnx as s0            # noqa: E402
from models.MIK_MCM_codecs import (    # noqa: E402
    build_feature_coding_Mean_MCM_Scale, build_feature_extractor)

PAD_H, PAD_W = 576, 960
SRC_H, SRC_W = 544, 960
OUT = os.path.abspath(os.path.join(HERE, "..", "out_s7"))


def write_png(path, rgb_u8, w, h):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgb_u8[y * w * 3:(y + 1) * w * 3]

    def ch(tag, data):
        return len(data).to_bytes(4, "big") + tag + data + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big")
    png = b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
        + ch(b"IDAT", zlib.compress(bytes(raw), 6)) + ch(b"IEND", b"")
    open(path, "wb").write(png)


def main():
    os.makedirs(OUT, exist_ok=True)
    fe = build_feature_extractor()
    fc = build_feature_coding_Mean_MCM_Scale(quality=3, parallel_computing=True)
    ck = torch.load(os.path.join(s0.ROOT, "packages/Model/MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth"),
                    map_location=s0.DEV)
    fe.load_state_dict(ck["feature_extractor"])
    fc.gaussian_conditional.scale_table = fc.gaussian_conditional.scale_table.to(torch.int32)
    fc.load_state_dict(ck["feature_coding"])
    fc._update_per_channel_shifts(ck["per_channel_shifts"]["feature_coding"])
    fe.eval(); fc.eval()
    for m in (fe, fc):
        m.requires_grad_(False)
    fc.context.quantize_func = partial(fc.quantize)

    p = os.path.join(s0.ROOT, "logs/samples/000000481386.jpg")
    im = Image.open(p).convert("RGB").resize((SRC_W, SRC_H))
    a = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0
    x = torch.from_numpy(a).unsqueeze(0)
    x = F.pad(x, (0, PAD_W - SRC_W, 0, PAD_H - SRC_H), mode="constant", value=0)
    H, W = PAD_H // 16, PAD_W // 16
    q_map = torch.full((1, 1, H, W), s0.Q_FACTOR)

    t0 = time.time()
    with torch.no_grad():
        f0 = fe(x)
        out = fc.compress(f0, q_map, rate_control_flag=False, after_padding_H=PAD_H, after_padding_W=PAD_W)
        t_enc = time.time() - t0
        y_b, z_b = out["strings"][0][0], out["strings"][1][0]
        n_y, n_z = len(y_b), len(z_b)
        print(f"编码: y_strings={n_y} B  z_strings={n_z} B  合计={n_y + n_z} B  耗时={t_enc:.1f} s")
        for tag, npx in (("模型输入 960x544", 960 * 544), ("内容 960x540", 960 * 540),
                         ("原图 1920x1080", 1920 * 1080)):
            print(f"  bpp（对 {tag}）= {(n_y + n_z) * 8 / npx:.5f}")
        np.save(os.path.join(OUT, "y_strings.npy"), np.frombuffer(y_b, dtype=np.uint8))
        np.save(os.path.join(OUT, "z_strings.npy"), np.frombuffer(z_b, dtype=np.uint8))

        t0 = time.time()
        x_hat_rec = fc.decompress(out["strings"], out["shape"], q_map)
        print(f"解码耗时 = {time.time() - t0:.1f} s")
        x_fwd = fc.g_s(fc.g_s_sft(out["strings"] and fc.context.decompress(
            fc.gaussian_conditional.decompress(out["strings"][0], fc.gaussian_conditional.build_indexes(
                fc.hyper_scale_decoder(torch.round(fc.hyper_encoder(fc.g_a_sft(fc.g_a(f0), q_map),
                                                                     h=PAD_H, w=PAD_W).clone()),
                                       h=PAD_H, w=PAD_W))),
            fc.hyper_decoder(torch.round(fc.hyper_encoder(fc.g_a_sft(fc.g_a(f0), q_map),
                                                          h=PAD_H, w=PAD_W).clone()), h=PAD_H, w=PAD_W)),
            q_map))
        d = (x_hat_rec - x_fwd).abs()
        print(f"往返一致性: 真码流解码 vs 同路径前向  max_abs={float(d.max()):.3g} corr="
              f"{float(np.corrcoef(x_hat_rec.numpy().ravel(), x_fwd.numpy().ravel())[0, 1]):.6f}")
        img = torch.clamp(x_hat_rec[0, :, :SRC_H, :SRC_W], 0, 1).permute(1, 2, 0).numpy()
        u8 = np.rint(img * 255.0).astype(np.uint8).tobytes()
        write_png(os.path.join(OUT, "rec_from_bitstream.png"), u8, SRC_W, SRC_H)
        img0 = torch.clamp(x[0, :, :SRC_H, :SRC_W], 0, 1).permute(1, 2, 0).numpy()
        write_png(os.path.join(OUT, "input.png"),
                  np.rint(img0 * 255.0).astype(np.uint8).tobytes(), SRC_W, SRC_H)
    print(f"产物 -> {OUT}")


if __name__ == "__main__":
    main()
