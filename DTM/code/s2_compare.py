#!/usr/bin/env python3
"""S2: 板上 RKNN 输出 vs PyTorch 参考 -- 逐网对拍."""
from __future__ import annotations

import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
IO = os.path.abspath(os.path.join(HERE, "..", "out_s2", "io"))
CASES = ["01_feature_extractor", "02_g_a", "03_g_a_sft", "04_hyper_encoder", "05_hyper_decoder",
         "06b_hyper_scale_decoder_float", "07b_context_mcm_static", "08_g_s_sft", "09_g_s",
         "10b_rec_clamp"]


def main():
    rep = {}
    for name in CASES:
        refs = sorted(p for p in os.listdir(IO) if p.startswith(f"ref_{name}_"))
        rec = {}
        for p in refs:
            i = p[len(f"ref_{name}_"):-4]
            r = np.load(os.path.join(IO, p))
            f = os.path.join(IO, f"out_{name}_{i}.npy")
            if not os.path.exists(f):
                continue
            o = np.load(f)
            if o.shape != r.shape:
                rec[i] = {"shape_mismatch": [list(o.shape), list(r.shape)]}
                continue
            d = np.abs(o.astype(np.float64) - r.astype(np.float64))
            scale = max(1e-12, float(np.abs(r).max()))
            rec[i] = {"shape": list(r.shape), "max_abs": float(d.max()),
                      "rel": float(d.max() / scale), "mean_abs": float(d.mean()),
                      "ref_range": [float(r.min()), float(r.max())]}
        rep[name] = rec
        s = " ".join(f"[o{k}] max={v.get('max_abs')} rel={v.get('rel'):.3g}" if "max_abs" in v
                     else f"[o{k}] {v}" for k, v in rec.items())
        print(f"{name:34s} {s}")

    with open(os.path.join(IO, "s2_compare.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print("\n-> ", os.path.join(IO, "s2_compare.json"))


if __name__ == "__main__":
    main()
