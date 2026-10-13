#!/usr/bin/env python3
"""B3：把 B 阶段训练出的学生头（ckpt/best.pth）导出 ONNX（上板第一步）。

输入 = 128ch 解码特征 (1,128,192,336)；输出 = RAW (1,N,84)（前 80 为 cls logits、后 4 为 ltrb）。
刻意**不带 sigmoid / GN**（同 t0 口径）⇒ ONNX→RKNN 干净；sigmoid/解码/NMS 留板端 numpy。

用法：~/anaconda3/envs/dtm/bin/python DTM/code/b3_export_onnx.py
"""

import json
import os
import sys
import types

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.modules.setdefault("torchac", types.ModuleType("torchac"))
from b2_train import FeatureDet                                        # noqa: E402

OUT = os.path.join(PROJ, "DTM", "assets", "fdet")
CKPT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(OUT, "best_ep15.pth")
FEAT = os.path.join(OUT, "feat_q19.npy")
OPSET = 12


def main() -> int:
    model = FeatureDet().eval()
    ck = torch.load(CKPT, map_location="cpu")
    model.load_state_dict(ck["model"])
    for p in model.parameters():
        p.requires_grad_(False)
    print("loaded", os.path.basename(CKPT), "epoch =", ck.get("epoch", "?"))

    x = torch.from_numpy(np.load(FEAT)).float()
    assert tuple(x.shape) == (1, 128, 192, 336), tuple(x.shape)
    with torch.no_grad():
        ref = model(x).numpy()
    print("torch out", ref.shape)

    onnx_path = os.path.join(OUT, "fdet_trained.onnx")
    torch.onnx.export(model, (x,), onnx_path, opset_version=OPSET,
                      input_names=["feat"], output_names=["pred"],
                      do_constant_folding=True)
    print("onnx ->", onnx_path, os.path.getsize(onnx_path), "B")

    import onnx
    ops = sorted({n.op_type for n in onnx.load(onnx_path).graph.node})
    print("onnx op_types:", ops)

    import onnxruntime as ort
    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    o = sess.run(None, {"feat": x.numpy().astype(np.float32)})[0]
    np.save(os.path.join(OUT, "onnx_out_trained_q19.npy"), o)
    dmax = float(np.abs(o - ref).max())
    print("ONNX(ORT) vs torch: max|Δ| =", dmax, " mean|Δ| =", float(np.abs(o - ref).mean()))

    with open(os.path.join(OUT, "b3_report.json"), "w", encoding="utf-8") as f:
        json.dump({"ckpt": os.path.basename(CKPT), "epoch": ck.get("epoch"),
                   "input_shape": list(x.shape), "output_shape": list(o.shape),
                   "onnx_op_types": ops,
                   "onnx_vs_torch_max_abs": dmax,
                   "onnx_vs_torch_mean_abs": float(np.abs(o - ref).mean())},
                  f, ensure_ascii=False, indent=1)
    print("B3_DONE", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
