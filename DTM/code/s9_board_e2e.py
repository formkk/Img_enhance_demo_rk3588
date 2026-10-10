#!/usr/bin/env python3
"""S9-C: 板上单帧 DCM 端到端(11 个 rknn + CPU rANS, 板上真跑).

链: x -> 01 -> 02 -> 03 -> 04(z) -> round -> 05(psi) / 06b(scale)
        -> 07b(mu) -> rANS 编码(y 用 means=mu; z 用 medians) -> 真码流
    -> rANS 解码 -> 05/06b -> 07d(resi+mu) -> 08 -> 09 -> 10b -> 重建像素

运行: PYTHONPATH=<numpy dir>:<this dir> python3 s9_board_e2e.py
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
OUTDIR = os.path.join(BASE, "out")
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
    """GaussianConditional.build_indexes 的 numpy 等价(lower_bound_scale 对 ×1024 域为 no-op)."""
    s = np.maximum(scale, float(table[0]) / 1024.0)
    idx = np.zeros(s.shape, np.int32)
    for t in table[:-1]:
        idx += (s > t)
    return idx


def rans_enc(sym, idx, cdf, cl, off):
    e = RansEncoder()
    return e.encode_with_indexes(sym.reshape(-1).tolist(), idx.reshape(-1).tolist(),
                                 cdf.tolist(), cl.tolist(), off.tolist())


def rans_dec(blob, idx, cdf, cl, off, shape):
    d = RansDecoder()
    v = d.decode_with_indexes(blob, idx.reshape(-1).tolist(), cdf.tolist(),
                              cl.tolist(), off.tolist())
    return np.asarray(v, dtype=np.float32).reshape(shape)


def write_png(path, u8_rgb, w, h):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += u8_rgb[y * w * 3:(y + 1) * w * 3]

    def ch(tag, d):
        return len(d).to_bytes(4, "big") + tag + d + (zlib.crc32(tag + d) & 0xFFFFFFFF).to_bytes(4, "big")
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + ch(b"IDAT", zlib.compress(bytes(raw), 6)) + ch(b"IEND", b""))


def cmp(got, ref):
    got = np.asarray(got, dtype=np.float32)
    ref = np.asarray(ref, dtype=np.float32)
    d = np.abs(got - ref)
    denom = float(np.abs(ref).mean()) + 1e-12
    corr = float(np.corrcoef(got.ravel(), ref.ravel())[0, 1]) if got.size > 1 else 1.0
    return {"max_abs": float(d.max()), "mean_abs": float(d.mean()),
            "rel_mean": float(d.mean() / denom), "corr": corr}


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    E = np.load(os.path.join(BASE, "ents.npz"))
    gc_cdf, gc_clen, gc_off, gc_scale = E["gc_cdf"], E["gc_clen"], E["gc_off"], E["gc_scale"]
    eb_cdf, eb_clen, eb_off = E["eb_cdf"], E["eb_clen"], E["eb_off"]
    med = E["eb_medians"].reshape(1, -1, 1, 1)

    x = np.load(os.path.join(BASE, "x_in.npy"))
    q = np.load(os.path.join(BASE, "q_map.npy"))

    nets = {}
    try:
        for k, f in MODELS.items():
            nets[k] = Net(os.path.join(BASE, f))
        print("11 nets loaded:", ",".join(MODELS))

        ts = {}
        t0 = time.time(); f0 = nets["01"](x); ts["01_fe"] = time.time() - t0
        t0 = time.time(); y = nets["03"](nets["02"](f0), q); ts["02_03"] = time.time() - t0
        t0 = time.time(); z = nets["04"](y); ts["04"] = time.time() - t0
        z_hat = np.rint(z).astype(np.float32)
        t0 = time.time(); psi = nets["05"](z_hat); ts["05"] = time.time() - t0
        t0 = time.time(); scale = nets["06b"](z_hat); ts["06b"] = time.time() - t0
        t0 = time.time(); mu = nets["07e"](y, psi); ts["07e_means"] = time.time() - t0

        t0 = time.time()
        idx_y = build_indexes(scale, gc_scale)
        sym_y = np.rint(y - mu).astype(np.int32)
        y_board = rans_enc(sym_y, idx_y, gc_cdf, gc_clen, gc_off)
        sym_z = np.rint(z - med).astype(np.int32)
        idx_z = np.broadcast_to(np.arange(z.shape[1], dtype=np.int32).reshape(1, -1, 1, 1),
                                z.shape).copy()
        z_board = rans_enc(sym_z, idx_z, eb_cdf, eb_clen, eb_off)
        ts["rANS_enc"] = time.time() - t0

        t0 = time.time()
        z_hat2 = rans_dec(z_board, idx_z, eb_cdf, eb_clen, eb_off, z.shape) + med
        psi2 = nets["05"](z_hat2); scale2 = nets["06b"](z_hat2)
        y_res = rans_dec(y_board, build_indexes(scale2, gc_scale), gc_cdf, gc_clen, gc_off, y.shape)
        ts["rANS_dec"] = time.time() - t0

        t0 = time.time()
        y_hat = nets["07d"](y_res, psi2)
        y_sft = nets["08"](y_hat, q)
        x_hat = nets["09"](y_sft)
        pix = nets["10b"](x_hat)
        ts["07d_08_09_10b"] = time.time() - t0
    finally:
        for n in nets.values():
            n.release()

    y_pc = open(os.path.join(BASE, "y_str.bin"), "rb").read()
    z_pc = open(os.path.join(BASE, "z_str.bin"), "rb").read()
    rep = {
        "nets": len(MODELS),
        "board_y_bytes": len(y_board), "pc_y_bytes": len(y_pc), "y_byte_equal": bytes(y_board) == y_pc,
        "board_z_bytes": len(z_board), "pc_z_bytes": len(z_pc), "z_byte_equal": bytes(z_board) == z_pc,
        "sym_y_mismatch": int((sym_y.ravel() != np.load(os.path.join(BASE, "ref_sym_y.npy")).ravel()).sum())
        if os.path.exists(os.path.join(BASE, "ref_sym_y.npy")) else None,
        "sym_z_mismatch": int((sym_z.ravel() != np.load(os.path.join(BASE, "ref_sym_z.npy")).ravel()).sum())
        if os.path.exists(os.path.join(BASE, "ref_sym_z.npy")) else None,
        "sym_y_total": int(sym_y.size), "sym_z_total": int(sym_z.size),
        "timing_s": {k: round(v, 3) for k, v in ts.items()},
    }
    tot = len(y_board) + len(z_board)
    rep["board_total_bytes"] = tot
    rep["bpp_960x544"] = tot * 8 / (960 * 544)

    refs = {"f0": None, "y": None, "z_hat": None, "psi": None, "scale": None, "mu": None,
            "y_res": None, "y_hat": None, "y_sft": None, "x_hat": None, "pix": None}
    got = {"y": y, "z_hat": z_hat, "psi": psi, "scale": scale, "mu": mu, "y_res": y_res,
           "y_hat": y_hat, "y_sft": y_sft, "x_hat": x_hat, "pix": pix}
    for nm, g in got.items():
        p = os.path.join(BASE, f"ref_{nm}.npy")
        if os.path.exists(p):
            refs[nm] = np.load(p)
            rep[f"cmp_{nm}"] = cmp(g, refs[nm])
    p = os.path.join(BASE, "ref_f0_16.npy")
    if os.path.exists(p):
        rep["cmp_f0"] = cmp(f0, np.load(p).astype(np.float32))

    np.save(os.path.join(OUTDIR, "board_pix.npy"), pix)
    np.save(os.path.join(OUTDIR, "board_y_hat.npy"), y_hat)
    u8 = np.clip(pix[0, :, :544, :].transpose(1, 2, 0), 0, 255).astype(np.uint8)
    write_png(os.path.join(OUTDIR, "board_rec.png"), u8[:, :, ::-1].tobytes(), 960, 544)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    with open(os.path.join(OUTDIR, "board_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print("S9_BOARD_E2E_DONE")


if __name__ == "__main__":
    main()
