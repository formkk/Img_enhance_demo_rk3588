#!/usr/bin/env python3
"""S3: 板上段输出 vs PyTorch 参考 -- 定位第一处发散."""
import json, os
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
IO = os.path.abspath(os.path.join(HERE, "..", "out_s3", "io"))
meta = json.load(open(os.path.join(IO, "meta.json"), encoding="utf-8"))
rep = {}
for name, m in meta.items():
    if not m.get("rknn"):
        continue
    rec = {}
    for i in range(len(m["refs"])):
        rf, of = os.path.join(IO, f"ref_{name}_{i}.npy"), os.path.join(IO, f"out_{name}_{i}.npy")
        if not os.path.exists(of):
            continue
        r, o = np.load(rf), np.load(of)
        if r.shape != o.shape:
            rec[str(i)] = {"shape_mismatch": [list(o.shape), list(r.shape)]}
            continue
        d = np.abs(o.astype(np.float64) - r.astype(np.float64))
        corr = float(np.corrcoef(r.ravel(), o.ravel())[0, 1]) if (r.std() > 0 and o.std() > 0) else None
        rec[str(i)] = {"shape": list(r.shape), "max_abs": float(d.max()),
                       "rel": float(d.max() / max(1e-12, float(np.abs(r).max()))),
                       "corr": corr,
                       "ref_range": [float(r.min()), float(r.max())]}
    rep[name] = rec
    s = "  ".join(f"[{k}] rel={v['rel']:.3g} corr={v['corr']}" if "rel" in v else f"[{k}] {v}"
                  for k, v in rec.items())
    print(f"{name:38s} {s}")
json.dump(rep, open(os.path.join(IO, "s3_compare.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("\n->", os.path.join(IO, "s3_compare.json"))
