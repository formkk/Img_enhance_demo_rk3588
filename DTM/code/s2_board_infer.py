#!/usr/bin/env python3
"""S2: 板上用 rknnlite 跑 10 个 .rknn, 落 out_*.npy 供回传对拍."""
from __future__ import annotations

import json
import os
import sys

import numpy as np
from rknnlite.api import RKNNLite

BASE = os.path.dirname(os.path.abspath(__file__))
IO = os.path.join(BASE, "io")
CASES = ["01_feature_extractor", "02_g_a", "03_g_a_sft", "04_hyper_encoder", "05_hyper_decoder",
         "06b_hyper_scale_decoder_float", "07b_context_mcm_static", "08_g_s_sft", "09_g_s",
         "10b_rec_clamp"]


def main():
    meta = json.load(open(os.path.join(IO, "meta.json"), encoding="utf-8"))
    rep = {}
    for name in CASES:
        m = meta[name]
        rk = RKNNLite(verbose=False)
        try:
            r = rk.load_rknn(os.path.join(BASE, name + ".rknn"))
            if r != 0:
                raise RuntimeError(f"load_rknn ret={r}")
            r = rk.init_runtime()
            if r != 0:
                raise RuntimeError(f"init_runtime ret={r}")
            ins = [np.load(os.path.join(IO, f"in_{name}_{i}.npy")) for i in range(len(m["inputs"]))]
            outs = rk.inference(inputs=ins, data_format="nchw")
            if outs is None:
                raise RuntimeError("inference returned None")
            for i, o in enumerate(outs):
                np.save(os.path.join(IO, f"out_{name}_{i}.npy"), np.asarray(o, dtype=np.float32))
            rep[name] = {"status": "OK", "n_out": len(outs),
                         "shapes": [list(np.asarray(o).shape) for o in outs]}
        except Exception as e:                       # noqa: BLE001
            rep[name] = {"status": "FAIL", "error": f"{type(e).__name__}: {e}"[:400]}
        finally:
            try:
                rk.release()
            except Exception:                        # noqa: BLE001
                pass
        print(f"{name:34s} {rep[name]['status']} {rep[name].get('shapes', rep[name].get('error',''))}")
        sys.stdout.flush()
    json.dump(rep, open(os.path.join(IO, "board_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("OK", sum(1 for v in rep.values() if v["status"] == "OK"), "/", len(CASES))


if __name__ == "__main__":
    main()
