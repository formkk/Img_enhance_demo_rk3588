#!/usr/bin/env python3
"""S9-E: 导出 07e = 只要 Context(MCM) 的**隐式预测 mu**(单输出) 并转 RKNN.

原因(板端实测): 07b 的 ONNX 有 2 个输出(y_rec, mu), ORT 下二者 maxdiff=1.0,
但转 RKNN 后 **两个输出退化成同一份**(逐位相等) => 板上取到的 mu 实际是 y_rec,
导致 `round(y-mu)` 符号错、码流翻倍、重建崩。
=> 编码端只需要 mu, 单独导一个**单输出**网即可绕开该退化。

产物: DTM/out_s0_1344/07e_context_means_static.{onnx,rknn} + s9e_report.json
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import torch
import torch.nn as nn
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s0_export_onnx as s0            # noqa: E402
import s0d_context_static as s0d       # noqa: E402

OUT = os.path.abspath(os.path.join(HERE, "..", "out_s0_1344"))
TARGET = "rk3588"


class MeansOnly(nn.Module):
    """只返回 mu 的包装(单输出)."""

    def __init__(self, static_ctx):
        super().__init__()
        self.st = static_ctx

    def forward(self, y, psi):
        return self.st(y, psi)[1]


def main():
    from functools import partial

    os.makedirs(OUT, exist_ok=True)
    fe, fc, rec = s0.build_models()
    for m in (fe, fc, rec):
        m.requires_grad_(False)
    fc.context.quantize_func = partial(fc.quantize)

    H, W = s0.PAD_H // 16, s0.PAD_W // 16
    q_map = torch.full((1, 1, H, W), s0.Q_FACTOR)
    src = np.asarray(Image.open(os.path.join(s0.ROOT, "logs/samples/000000481386.jpg"))
                     .convert("RGB").resize((1333, 750)))
    a = src[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0
    x = torch.nn.functional.pad(torch.from_numpy(a).unsqueeze(0),
                                (0, s0.PAD_W - 1333, 0, s0.PAD_H - 750), mode="constant", value=0)
    with torch.no_grad():
        y = fc.g_a_sft(fc.g_a(fe(x)), q_map)
        z = fc.hyper_encoder(y, h=s0.PAD_H, w=s0.PAD_W)
        psi = fc.hyper_decoder(torch.round(z.clone()), h=s0.PAD_H, w=s0.PAD_W)
        ref = fc.context(y=y, hyper_params=psi)      # (y_rec, mu, resi_q, resi_dq)

    mod = MeansOnly(s0d.ContextStatic(fc.context, H, W))
    mod.requires_grad_(False)
    with torch.no_grad():
        got = mod(y.clone(), psi.clone())
    rep = {"torch_means_vs_orig_max_abs": float((got - ref[1]).abs().max()),
           "mu_range": [float(ref[1].min()), float(ref[1].max())]}
    print("== PyTorch: means-only vs 原 mu ==", rep["torch_means_vs_orig_max_abs"])

    onnx_path = os.path.join(OUT, "07e_context_means_static.onnx")
    torch.onnx.export(mod, (y.clone(), psi.clone()), onnx_path, opset_version=s0.OPSET,
                      input_names=["y", "psi"], output_names=["mu"], do_constant_folding=True)

    import onnxruntime as ort
    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    o = sess.run(None, {"y": y.numpy(), "psi": psi.numpy()})[0]
    rep["onnx_vs_orig_max_abs"] = float(np.abs(o - ref[1].numpy()).max())
    print("   ONNX(ORT) vs 原 mu: max_abs =", rep["onnx_vs_orig_max_abs"])

    from rknn.api import RKNN
    rknn_path = os.path.join(OUT, "07e_context_means_static.rknn")
    r = RKNN(verbose=False)
    try:
        r.config(target_platform=TARGET)
        assert r.load_onnx(model=onnx_path) == 0, "load_onnx"
        assert r.build(do_quantization=False) == 0, "build"
        assert r.export_rknn(rknn_path) == 0, "export_rknn"
        rep["rknn"] = {"path": os.path.basename(rknn_path), "size": os.path.getsize(rknn_path)}
        print("   RKNN 转换 OK ->", rknn_path, os.path.getsize(rknn_path), "B")
    finally:
        r.release()

    with open(os.path.join(OUT, "s9e_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
