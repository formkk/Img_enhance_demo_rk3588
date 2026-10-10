#!/usr/bin/env python3
"""S3: 板上跑 out_s3 的全部段(读 io/meta.json 自动列段)."""
import json, os, sys
import numpy as np
from rknnlite.api import RKNNLite

BASE = os.path.dirname(os.path.abspath(__file__))
IO = os.path.join(BASE, "io")

meta = json.load(open(os.path.join(IO, "meta.json"), encoding="utf-8"))
rep = {}
for name, m in meta.items():
    if not m.get("rknn"):
        continue
    rk = RKNNLite(verbose=False)
    try:
        if rk.load_rknn(os.path.join(BASE, m["rknn"])) != 0:
            raise RuntimeError("load_rknn")
        if rk.init_runtime() != 0:
            raise RuntimeError("init_runtime")
        ins = [np.load(os.path.join(IO, f"in_{name}_{i}.npy")) for i in range(len(m["inputs"]))]
        outs = rk.inference(inputs=ins, data_format="nchw")
        if outs is None:
            raise RuntimeError("inference None")
        for i, o in enumerate(outs):
            np.save(os.path.join(IO, f"out_{name}_{i}.npy"), np.asarray(o, dtype=np.float32))
        rep[name] = {"status": "OK", "n_out": len(outs)}
    except Exception as e:                       # noqa: BLE001
        rep[name] = {"status": "FAIL", "error": f"{type(e).__name__}: {e}"[:300]}
    finally:
        try:
            rk.release()
        except Exception:                        # noqa: BLE001
            pass
    print(f"{name:38s} {rep[name]['status']} {rep[name].get('error','')}")
    sys.stdout.flush()
json.dump(rep, open(os.path.join(IO, "board_report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("OK", sum(1 for v in rep.values() if v["status"] == "OK"), "/", len(rep))
