#!/usr/bin/env python3
"""S8-B: 板上用 DTM 自带 rANS(编译为 aarch64) 复现 RM 的熵编码.

口径(与本机 compressai.ans.RansEncoder 一致):
  * cdf 必须是**逐行二维** list(每行 = 一个 CDF 级), 行宽 = cdf.size / len(cdf_length);
  * encode_with_indexes(symbols, indexes, cdfs, cdfs_sizes, offsets) -> py::bytes
  * decode_with_indexes(encoded, indexes, cdfs, cdfs_sizes, offsets) -> list[int] (已含 offset)
"""
import os
import sys
import time

import numpy as np
from rans import RansEncoder, RansDecoder

IO = os.path.dirname(os.path.abspath(__file__))


def load(t):
    sym = np.load(f"{IO}/{t}_sym.npy")
    idx = np.load(f"{IO}/{t}_idx.npy")
    cdf = np.load(f"{IO}/{t}_cdf.npy")
    cl = np.load(f"{IO}/{t}_clen.npy")
    off = np.load(f"{IO}/{t}_off.npy")
    nrow = int(cl.size)
    row_w = int(cdf.size) // nrow
    cdf = cdf.reshape(nrow, row_w)
    return sym, idx, cdf, cl, off, row_w


def main():
    ok_all = True
    for t in ("v0", "v1"):
        sym, idx, cdf, cl, off, row_w = load(t)
        ref = open(f"{IO}/{t}_ref.bin", "rb").read()

        t0 = time.time()
        enc = RansEncoder()
        out = enc.encode_with_indexes(sym.tolist(), idx.tolist(), cdf.tolist(),
                                      cl.tolist(), off.tolist())
        te = time.time() - t0

        t0 = time.time()
        dec = RansDecoder()
        back = dec.decode_with_indexes(out, idx.tolist(), cdf.tolist(),
                                       cl.tolist(), off.tolist())
        td = time.time() - t0

        back = np.asarray(back, dtype=np.int32)
        byte_eq = bytes(out) == ref
        rt = np.array_equal(back, sym.astype(np.int32))
        ok_all = ok_all and byte_eq and rt
        print(f"{t}: nsym={sym.size} nrow={cdf.shape[0]} roww={row_w} "
              f"enc={len(out)}B(ref {len(ref)}B) byte_equal={byte_eq} roundtrip={rt} "
              f"编码{te:.2f}s 解码{td:.2f}s")
        sys.stdout.flush()
    print("S8_RESULT", "PASS" if ok_all else "FAIL")


if __name__ == "__main__":
    main()
