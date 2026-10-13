#!/usr/bin/env python3
"""V4：板端「码率-档位」扫描（同一份 RKNN，只换 q_map 这个输入张量）。

对 q = 0..31：
    f0 = 01(x)（与 q 无关，只算一次）
    y  = 03(02(f0), q_map[q])  → 04(z) → round → 05(psi) / 06b(scale) → 07e(y,psi)=mu
    → rANS 编码（真码流）→ rANS 解码 → 07d → 08(q_map) → 09 → 10b → REC
产出 <BASE>/out/sweep/：
    q00.png … q31.png      各档重建图（960×540，供本机做检出对比）
    sweep_report.json      各档字节数 / bpp / 耗时

运行（板端）：cd <BASE> && PYTHONPATH=<BASE>:/home/cat/rknn_ab/site/232 python3 v4_board_rate_sweep.py
"""

import json
import os
import struct
import sys
import time
import zlib

import numpy as np
from rans import RansDecoder, RansEncoder
from rknnlite.api import RKNNLite

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "out", "sweep")
PAD_H, PAD_W, CONTENT_H = 576, 960, 540
QH, QW = PAD_H // 16, PAD_W // 16

# tools/main.py: rate_control_index_table
Q_TABLE = [0.200, 0.222, 0.243, 0.265, 0.286, 0.308, 0.330, 0.351, 0.373, 0.395, 0.416, 0.438,
           0.459, 0.481, 0.503, 0.524, 0.546, 0.567, 0.589, 0.611, 0.632, 0.654, 0.675, 0.697,
           0.719, 0.740, 0.762, 0.784, 0.805, 0.827, 0.848, 0.870]

MODELS = {
    "01": "01_feature_extractor.rknn", "02": "02_g_a.rknn", "03": "03_g_a_sft.rknn",
    "04": "04_hyper_encoder.rknn", "05": "05_hyper_decoder.rknn",
    "06b": "06b_hyper_scale_decoder_float.rknn", "07e": "07e_context_means_static.rknn",
    "07d": "07d_context_decompress_static.rknn", "08": "08_g_s_sft.rknn",
    "09": "09_g_s.rknn", "10b": "10b_rec_clamp.rknn",
}


class Net:
    def __init__(self, path):
        self.rk = RKNNLite(verbose=False)
        if self.rk.load_rknn(path) != 0:
            raise RuntimeError(f"load_rknn {path}")
        if self.rk.init_runtime() != 0:
            raise RuntimeError(f"init_runtime {path}")

    def __call__(self, *ins):
        o = self.rk.inference(inputs=list(ins), data_format="nchw")
        o = [np.asarray(t, dtype=np.float32) for t in o]
        return o[0] if len(o) == 1 else o

    def release(self):
        try:
            self.rk.release()
        except Exception:                        # noqa: BLE001
            pass


def build_indexes(scale, table):
    s = np.maximum(scale, float(table[0]) / 1024.0)
    idx = np.zeros(s.shape, np.int32)
    for t in table[:-1]:
        idx += (s > t)
    return idx


def write_png(path, u8_rgb, w, h):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += u8_rgb[y * w * 3:(y + 1) * w * 3]

    def ch(tag, d):
        return len(d).to_bytes(4, "big") + tag + d + (zlib.crc32(tag + d) & 0xFFFFFFFF).to_bytes(4, "big")
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + ch(b"IDAT", zlib.compress(bytes(raw), 6)) + ch(b"IEND", b""))


def main():
    os.makedirs(OUT, exist_ok=True)
    E = np.load(os.path.join(BASE, "ents.npz"))
    gc_cdf, gc_clen, gc_off, gc_scale = E["gc_cdf"], E["gc_clen"], E["gc_off"], E["gc_scale"]
    eb_cdf, eb_clen, eb_off = E["eb_cdf"], E["eb_clen"], E["eb_off"]
    med = E["eb_medians"].reshape(1, -1, 1, 1)
    x = np.load(os.path.join(BASE, "x_v1.npy"))

    nets, rows = {}, []
    try:
        for k, f in MODELS.items():
            nets[k] = Net(os.path.join(BASE, f))
        t = time.time()
        f0 = nets["01"](x)
        print(f"f0 = 01(x)  {f0.shape}  {time.time() - t:.3f}s（与档位无关，只算一次）")

        for q in range(32):
            qm = np.full((1, 1, QH, QW), Q_TABLE[q], np.float32)
            t0 = time.time()
            y = nets["03"](nets["02"](f0), qm)
            z = nets["04"](y)
            z_hat = np.rint(z).astype(np.float32)
            psi = nets["05"](z_hat)
            scale = nets["06b"](z_hat)
            mu = nets["07e"](y, psi)
            sym_y = np.rint(y - mu).astype(np.int32)
            yb = RansEncoder().encode_with_indexes(
                sym_y.reshape(-1).tolist(), build_indexes(scale, gc_scale).reshape(-1).tolist(),
                gc_cdf.tolist(), gc_clen.tolist(), gc_off.tolist())
            sym_z = np.rint(z - med).astype(np.int32)
            idx_z = np.broadcast_to(np.arange(z.shape[1], dtype=np.int32).reshape(1, -1, 1, 1),
                                    z.shape).copy()
            zb = RansEncoder().encode_with_indexes(
                sym_z.reshape(-1).tolist(), idx_z.reshape(-1).tolist(),
                eb_cdf.tolist(), eb_clen.tolist(), eb_off.tolist())
            z_hat2 = np.asarray(RansDecoder().decode_with_indexes(
                bytes(zb), idx_z.reshape(-1).tolist(), eb_cdf.tolist(),
                eb_clen.tolist(), eb_off.tolist()), np.float32).reshape(z.shape) + med
            psi2 = nets["05"](z_hat2)
            scale2 = nets["06b"](z_hat2)
            y_res = np.asarray(RansDecoder().decode_with_indexes(
                bytes(yb), build_indexes(scale2, gc_scale).reshape(-1).tolist(), gc_cdf.tolist(),
                gc_clen.tolist(), gc_off.tolist()), np.float32).reshape(y.shape)
            y_hat = nets["07d"](y_res, psi2)
            pix = nets["10b"](nets["09"](nets["08"](y_hat, qm)))
            n = len(yb) + len(zb)
            u8 = np.clip(pix[0, :, :CONTENT_H, :].transpose(1, 2, 0), 0, 255).astype(np.uint8)
            write_png(os.path.join(OUT, f"q{q:02d}.png"),
                      np.ascontiguousarray(u8[:, :, ::-1]).tobytes(), PAD_W, CONTENT_H)
            rows.append({"q": q, "q_factor": Q_TABLE[q], "y_bytes": len(yb), "z_bytes": len(zb),
                         "total_bytes": n, "bpp_960x544": n * 8 / (960 * 544),
                         "sec": round(time.time() - t0, 3)})
            print(f"q={q:2d} factor={Q_TABLE[q]:.3f}  y={len(yb):5d} z={len(zb):4d}"
                  f"  合计={n:5d} B  {n * 8 / (960 * 544):.5f} bpp  {time.time() - t0:.2f}s")
    finally:
        for n in nets.values():
            n.release()

    with open(os.path.join(OUT, "sweep_report.json"), "w", encoding="utf-8") as f:
        json.dump({"frame": "hq_final_1", "content": [PAD_W, CONTENT_H], "rows": rows}, f, indent=1)
    print("V4_SWEEP_DONE", OUT)


if __name__ == "__main__":
    main()
