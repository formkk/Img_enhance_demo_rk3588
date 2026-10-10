#!/usr/bin/env python3
"""S4: 07b 输入顺序交换测试(板上)."""
import os, sys
import numpy as np
from rknnlite.api import RKNNLite

BASE = os.path.dirname(os.path.abspath(__file__))
IO = os.path.join(BASE, "io")
y = np.load(os.path.join(IO, "in_07b_context_mcm_static_0.npy"))
psi = np.load(os.path.join(IO, "in_07b_context_mcm_static_1.npy"))
print("y", y.shape, "psi", psi.shape)

rk = RKNNLite(verbose=False)
assert rk.load_rknn(os.path.join(BASE, "07b_context_mcm_static.rknn")) == 0
assert rk.init_runtime() == 0
for tag, ins in (("normal", [y, psi]), ("swapped", [psi, y])):
    try:
        outs = rk.inference(inputs=ins, data_format="nchw")
        if outs is None:
            raise RuntimeError("inference None")
        for i, o in enumerate(outs):
            np.save(os.path.join(IO, f"out_07b_{tag}_{i}.npy"), np.asarray(o, dtype=np.float32))
        print(f"{tag:8s} OK  shapes={[list(np.asarray(o).shape) for o in outs]}")
    except Exception as e:                                    # noqa: BLE001
        print(f"{tag:8s} FAIL {type(e).__name__}: {str(e)[:200]}")
    sys.stdout.flush()
rk.release()
