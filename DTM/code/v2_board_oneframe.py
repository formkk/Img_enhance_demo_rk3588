#!/usr/bin/env python3
"""V2：板端单帧 DCM 全流程 + dump 可视化所需数据（板上真跑）。

链（与 s9_board_e2e.py 同，12 个 rknn + CPU rANS）：
    x -> 01 -> 02 -> 03(q_map) = y
      -> 04(z) -> round -> 05(psi) / 06b(scale) -> 07e(y,psi) = mu
      -> rANS 编码（y 用 mu+scale；z 用 medians）→ **真码流**
      -> rANS 解码 -> 05/06b -> 07d(resi,psi) -> 08(q_map) -> 09 -> 10b = 重建像素(REC)

产出（<BASE>/out/）：
    board_y.npy        (1,128,36,60) f32   进熵编码的潜在（可视化用）
    board_yb.bin                            真码流 y 流（可视化用）
    board_zb.bin                            真码流 z 流
    board_pix.npy      (1,3,576,960) f32    REC 重建像素（BGR, 0..255）
    board_rec.png      960x540（内容区）     REC 重建图（目视）
    board_report.json                       字节数 / bpp / 耗时

运行（板端）：
    cd <BASE> && PYTHONPATH=/home/cat/rknn_ab/site/232 python3 v2_board_oneframe.py
"""

import json
import os
import struct
import time
import zlib

import numpy as np
from rans import RansDecoder, RansEncoder
from rknnlite.api import RKNNLite

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "out")
PAD_H, PAD_W = 576, 960
CONTENT_H = 540

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
        if o is None:
            raise RuntimeError("inference None")
        o = [np.asarray(t, dtype=np.float32) for t in o]
        return o[0] if len(o) == 1 else o

    def release(self):
        try:
            self.rk.release()
        except Exception:                       # noqa: BLE001
            pass


def build_indexes(scale, table):
    s = np.maximum(scale, float(table[0]) / 1024.0)
    idx = np.zeros(s.shape, np.int32)
    for t in table[:-1]:
        idx += (s > t)
    return idx


def rans_enc(sym, idx, cdf, cl, off):
    return RansEncoder().encode_with_indexes(sym.reshape(-1).tolist(), idx.reshape(-1).tolist(),
                                             cdf.tolist(), cl.tolist(), off.tolist())


def rans_dec(blob, idx, cdf, cl, off, shape):
    v = RansDecoder().decode_with_indexes(blob, idx.reshape(-1).tolist(),
                                          cdf.tolist(), cl.tolist(), off.tolist())
    return np.asarray(v, dtype=np.float32).reshape(shape)


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
    q = np.load(os.path.join(BASE, "q_map.npy"))
    print("x:", x.shape, x.dtype, "| q:", q.shape)

    nets, ts = {}, {}
    try:
        for k, f in MODELS.items():
            nets[k] = Net(os.path.join(BASE, f))
        print("nets loaded:", len(nets))

        t = time.time(); f0 = nets["01"](x); ts["01_feature_extractor"] = time.time() - t
        t = time.time(); y = nets["03"](nets["02"](f0), q); ts["02_03_g_a_sft"] = time.time() - t
        t = time.time(); z = nets["04"](y); ts["04_hyper_encoder"] = time.time() - t
        z_hat = np.rint(z).astype(np.float32)
        t = time.time(); psi = nets["05"](z_hat); ts["05_hyper_decoder"] = time.time() - t
        t = time.time(); scale = nets["06b"](z_hat); ts["06b_scale"] = time.time() - t
        t = time.time(); mu = nets["07e"](y, psi); ts["07e_means"] = time.time() - t

        t = time.time()
        sym_y = np.rint(y - mu).astype(np.int32)
        yb = rans_enc(sym_y, build_indexes(scale, gc_scale), gc_cdf, gc_clen, gc_off)
        sym_z = np.rint(z - med).astype(np.int32)
        idx_z = np.broadcast_to(np.arange(z.shape[1], dtype=np.int32).reshape(1, -1, 1, 1), z.shape).copy()
        zb = rans_enc(sym_z, idx_z, eb_cdf, eb_clen, eb_off)
        ts["rANS_encode"] = time.time() - t

        t = time.time()
        z_hat2 = rans_dec(zb, idx_z, eb_cdf, eb_clen, eb_off, z.shape) + med
        psi2 = nets["05"](z_hat2)
        scale2 = nets["06b"](z_hat2)
        y_res = rans_dec(yb, build_indexes(scale2, gc_scale), gc_cdf, gc_clen, gc_off, y.shape)
        ts["rANS_decode+hyper"] = time.time() - t

        t = time.time()
        y_hat = nets["07d"](y_res, psi2)
        y_sft = nets["08"](y_hat, q)
        x_hat = nets["09"](y_sft)
        pix = nets["10b"](x_hat)
        ts["07d_08_09_10b"] = time.time() - t
    finally:
        for n in nets.values():
            n.release()

    np.save(os.path.join(OUT, "board_y.npy"), y.astype(np.float32))
    np.save(os.path.join(OUT, "board_pix.npy"), pix.astype(np.float32))
    open(os.path.join(OUT, "board_yb.bin"), "wb").write(bytes(yb))
    open(os.path.join(OUT, "board_zb.bin"), "wb").write(bytes(zb))

    u8 = np.clip(pix[0, :, :CONTENT_H, :].transpose(1, 2, 0), 0, 255).astype(np.uint8)
    write_png(os.path.join(OUT, "board_rec.png"), np.ascontiguousarray(u8[:, :, ::-1]).tobytes(),
              PAD_W, CONTENT_H)

    n = len(yb) + len(zb)
    rep = {
        "nets": len(MODELS),
        "y_bytes": len(yb), "z_bytes": len(zb), "total_bytes": n,
        "bpp_960x544": n * 8 / (960 * 544), "bpp_960x540": n * 8 / (960 * 540),
        "y_shape": list(y.shape), "pix_shape": list(pix.shape),
        "timing_s": {k: round(v, 3) for k, v in ts.items()},
        "total_s": round(sum(ts.values()), 3),
    }
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    with open(os.path.join(OUT, "board_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print("V2_BOARD_DONE")


if __name__ == "__main__":
    main()
