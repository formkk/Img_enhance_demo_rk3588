#!/usr/bin/env python3
"""S0-A2: (a) 修 rec 的 Where 类型冲突并导出; (b) 对 g_s 做逐子模块二分, 定位 11.5 偏差来源.

rec 的根因: models/rec/common.py split_0to255 用 torch.where(input>255, 255, input),
            标量 255 为 int, 与 float 张量在 ONNX Where 里类型冲突.
            数学等价 = clamp(0, 255).
g_s 的 11.5: 未知 => 逐子模块导出对拍定位.

产物: DTM/out_s0/10b_rec_clamp.onnx + DTM/out_s0/s0c_report.json
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import torch
import torch.nn as nn

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "DTM-V-1.5"))
OUT = os.path.abspath(os.path.join(HERE, "..", "out_s0"))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import compressai  # noqa: E402

compressai.set_entropy_coder("ans")

from models.MIK_MCM_codecs import (  # noqa: E402
    build_feature_coding_Mean_MCM_Scale,
    build_feature_extractor,
)
from models.rec.rec import build_rec_model  # noqa: E402

DEV = torch.device("cpu")
OPSET = 12
PAD_H, PAD_W = 576, 960
Q_FACTOR = 0.265
OUTDIR = OUT

torch.manual_seed(0)


class Wrap(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, *xs):
        return self.fn(*xs)


class RecFixed(nn.Module):
    """REC.forward 的等价改写: 末级 where-based split_0to255 -> clamp(0,255)."""

    def __init__(self, rec):
        super().__init__()
        self.head, self.body, self.tail = rec.head, rec.body, rec.tail

    def forward(self, x):
        h = self.head(x)
        b = self.body(h) + h
        return self.tail(b).clamp(0.0, 255.0)


def export_and_cmp(name, mod, inputs):
    path = os.path.join(OUTDIR, f"{name}.onnx")
    mod.requires_grad_(False)
    # 部分子模块含 inplace LeakyReLU: 调用/trace 都会就地改输入 => 留一份原值, 每次喂独立副本
    base = [t.detach().clone() for t in inputs]
    with torch.no_grad():
        ref = mod(*[t.clone() for t in base])
        ref = ref if isinstance(ref, torch.Tensor) else ref[0]
        torch.onnx.export(mod, tuple(t.clone() for t in base), path, opset_version=OPSET,
                          input_names=[f"in{i}" for i in range(len(base))],
                          output_names=["out"], do_constant_folding=True)
    import onnxruntime as ort
    sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    names = [i.name for i in sess.get_inputs()]
    out = sess.run(None, {n: t.numpy() for n, t in zip(names, [t.clone() for t in base])})[0]
    r = ref.numpy()
    return {
        "shape": list(r.shape),
        "ref_range": [float(r.min()), float(r.max())],
        "max_abs_diff": float(np.abs(out - r).max()),
        "rel": float(np.abs(out - r).max() / max(1e-9, np.abs(r).max())),
        "ops": _ops(path),
    }


def _ops(path):
    import onnx
    g = onnx.load(path).graph
    d = {}
    for n in g.node:
        d[n.op_type] = d.get(n.op_type, 0) + 1
    return dict(sorted(d.items()))


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    fe = build_feature_extractor()
    fc = build_feature_coding_Mean_MCM_Scale(quality=3, parallel_computing=True)
    ckpt = torch.load(os.path.join(ROOT, "packages/Model/MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth"),
                      map_location=DEV)
    fe.load_state_dict(ckpt["feature_extractor"])
    fc.gaussian_conditional.scale_table = fc.gaussian_conditional.scale_table.to(torch.int32)
    fc.load_state_dict(ckpt["feature_coding"])
    fc._update_per_channel_shifts(ckpt["per_channel_shifts"]["feature_coding"])
    fe.eval(); fc.eval()
    for m in (fe, fc):
        m.requires_grad_(False)

    rep = {}

    # ---------- (a) rec ----------
    from functools import partial
    fc.context.quantize_func = partial(fc.quantize)
    q_map = torch.full((1, 1, PAD_H // 16, PAD_W // 16), Q_FACTOR)
    with torch.no_grad():
        y0 = fe(torch.rand(1, 3, PAD_H, PAD_W))
        y = fc.g_a_sft(fc.g_a(y0), q_map)
        x_hat = fc.g_s(y)                      # g_s 输出 = rec 的输入(真实量级)

    rec = build_rec_model()
    rec.load_state_dict(torch.load(os.path.join(ROOT, "packages/Model/REC_model/model_rec.pt"), map_location=DEV))
    rec.eval()
    recf = RecFixed(rec)

    # 原版 vs 修版: 数值是否等价
    with torch.no_grad():
        o = rec(x_hat.clone()); n = recf(x_hat.clone())   # inplace 会改输入 => 各喂副本
    rep["rec_orig_vs_fixed_pt"] = float((o - n).abs().max())
    rep["rec_orig_range"] = [float(o.min()), float(o.max())]
    rep["rec_fixed_range"] = [float(n.min()), float(n.max())]
    rep["rec_fixed"] = export_and_cmp("10b_rec_clamp", recf, [x_hat])

    # ---------- (b) g_s 二分 ----------
    gs = fc.g_s
    rep["g_s_children"] = [type(c).__name__ for c in gs]
    # 用真实 y(非 randn) 作输入
    rep["g_s_full_realistic"] = export_and_cmp("09b_g_s_realistic", Wrap(lambda x: gs(x)), [y])
    rep["g_s_full_randn"] = export_and_cmp("09c_g_s_randn", Wrap(lambda x: gs(x)),
                                           [torch.randn(1, 128, PAD_H // 16, PAD_W // 16)])

    # 逐子模块
    cur = y
    rep["g_s_per_child"] = {}
    for i, ch in enumerate(gs):
        with torch.no_grad():
            cur_out = ch(cur.clone())          # inplace 会改 cur => 先克隆
        e = export_and_cmp(f"09d_g_s_child{i}_{type(ch).__name__}",
                           Wrap(lambda x, _m=ch: _m(x)), [cur.clone()])
        rep["g_s_per_child"][f"{i}:{type(ch).__name__}"] = e
        cur = cur_out

    with open(os.path.join(OUTDIR, "s0c_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)

    print("== rec ==")
    print("  原版 vs clamp 修版(PyTorch) max abs diff =", rep["rec_orig_vs_fixed_pt"])
    print("  修版导出与对拍:", {k: rep["rec_fixed"][k] for k in ("shape", "max_abs_diff", "rel")})
    print("  ops:", rep["rec_fixed"]["ops"])
    print("== g_s ==")
    print("  children:", rep["g_s_children"])
    for k in ("g_s_full_realistic", "g_s_full_randn"):
        e = rep[k]
        print(f"  {k}: ref_range={e['ref_range']} max_abs_diff={e['max_abs_diff']:.6g} rel={e['rel']:.3g}")
    print("  逐子模块:")
    for k, e in rep["g_s_per_child"].items():
        print(f"    {k}: ref_range={e['ref_range']} max_abs_diff={e['max_abs_diff']:.6g} rel={e['rel']:.3g}")
    print("产物 ->", OUTDIR)


if __name__ == "__main__":
    main()
