#!/usr/bin/env python3
"""V6：在**同一帧**上用 RM（FP32）出 REC，用来判定"偏色/发灰"是搬板造成的还是 DCM 本来如此。

口径完全照 s7b：`fc.decompress(...)["x_hat"]` → `rec(feat)` → BGR → 翻成 RGB 存 PNG。
产 DTM/out_v1/rm_rec/：input.png / rm_rec_q03.png / rm_rec_q31.png + 通道均值打印。
"""

import os
import sys
import types
from functools import partial

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.modules.setdefault("torchac", types.ModuleType("torchac"))
import s0_export_onnx as s0                                     # noqa: E402
import v1_make_visuals as v1                                    # noqa: E402
from models.rec.rec import build_rec_model                      # noqa: E402

PAD_H, PAD_W, CONTENT_H = 576, 960, 540
Q_TABLE = {3: 0.265, 31: 0.870}
OUT = os.path.join(PROJ, "DTM", "out_v1", "rm_rec")


def write_png(path, u8_rgb, w, h):
    """与 s7b / 板端 v2 完全相同的 PNG 写法（pix 是 BGR ⇒ 传 u8[:, :, ::-1]）。"""
    import struct
    import zlib
    if not isinstance(u8_rgb, (bytes, bytearray, memoryview)):
        u8_rgb = np.ascontiguousarray(u8_rgb).reshape(-1).astype(np.uint8).tobytes()
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += u8_rgb[y * w * 3:(y + 1) * w * 3]

    def ch(tag, d):
        return len(d).to_bytes(4, "big") + tag + d + (zlib.crc32(tag + d) & 0xFFFFFFFF).to_bytes(4, "big")
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + ch(b"IDAT", zlib.compress(bytes(raw), 6)) + ch(b"IEND", b""))


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    fe, fc = v1.load_chain()
    rec = build_rec_model()
    rec.load_state_dict(torch.load(os.path.join(s0.ROOT, "packages/Model/REC_model/model_rec.pt"),
                                   map_location=s0.DEV))
    rec.eval()
    rec.requires_grad_(False)

    x = v1.load_frame()
    u8 = np.rint(np.clip(x[0, :, :CONTENT_H, :].permute(1, 2, 0).numpy(), 0, 1) * 255).astype(np.uint8)
    write_png(os.path.join(OUT, "input.png"), u8[:, :, ::-1].tobytes(), PAD_W, CONTENT_H)
    print("input 通道均值(BGR) =", u8.reshape(-1, 3).mean(0).round(1), "饱和 =",
          float(np.abs(u8.astype(np.float32) - u8.mean(2, keepdims=True)).max(2).mean()))

    for q, f in Q_TABLE.items():
        qm = torch.full((1, 1, PAD_H // 16, PAD_W // 16), f, dtype=torch.float32)
        with torch.no_grad():
            f0 = fe(x)
            out = fc.compress(f0, qm, rate_control_flag=False,
                              after_padding_H=PAD_H, after_padding_W=PAD_W)
            feat = fc.decompress(out["strings"], out["shape"], qm)["x_hat"]
            pix = rec(feat)
        n = len(out["strings"][0][0]) + len(out["strings"][1][0])
        p = np.clip(pix[0, :, :CONTENT_H, :].permute(1, 2, 0).numpy(), 0, 255).astype(np.uint8)
        write_png(os.path.join(OUT, f"rm_rec_q{q:02d}.png"), p[:, :, ::-1].tobytes(), PAD_W, CONTENT_H)
        print(f"RM q={q:2d}  码流={n} B  {n * 8 / (960 * 544):.5f} bpp  "
              f"REC 通道均值(BGR)={p.reshape(-1, 3).mean(0).round(1)}  "
              f"饱和={float(np.abs(p.astype(np.float32) - p.mean(2, keepdims=True)).max(2).mean()):.1f}")
    print("V6_DONE", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
