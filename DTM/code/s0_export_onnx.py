#!/usr/bin/env python3
"""S0: 把 DTM-V 1.5 的单帧编解码子网逐个导出 ONNX, 并清点算子.

目的 = 回答"这些网能不能转 RKNN"(草案 DTM/立项草案-DCM演示路.md 的 3.0 S0 与 8.6 关口 1/2).
本脚本只导出与对拍: 不改权重/不写回 DTM 源码/不联网/不碰板子.

用法:
    export PATH=$HOME/anaconda3/envs/dtm/bin:$PATH
    python3 DTM/code/s0_export_onnx.py

产物: DTM/out_s0/<name>.onnx + s0_report.json + s0_summary.txt
"""
from __future__ import annotations

import collections
import json
import os
import sys
import traceback

import numpy as np
import torch
import torch.nn as nn

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "DTM-V-1.5"))
OUT = os.path.abspath(os.path.join(HERE, "..", "out_s0"))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # DTM 内部用相对路径(packages/...)

import compressai  # noqa: E402

compressai.set_entropy_coder("ans")

from models.MIK_MCM_codecs import (  # noqa: E402
    build_feature_coding_Mean_MCM_Scale,
    build_feature_extractor,
)
from models.rec.rec import build_rec_model  # noqa: E402

DEV = torch.device("cpu")          # 板上无 CUDA; 导出在 CPU 语义下做
OPSET = 12
PAD_H, PAD_W = 576, 960            # 960x544 源图 pad 到 960x576(草案 8.3)
Q_FACTOR = 0.265                   # rate_control_index_table[3](tools/main.py:226-231)


def build_models():
    """按 tools/main.py:1182-1207 的同一口径构建 + 加载权重."""
    fe = build_feature_extractor()
    fc = build_feature_coding_Mean_MCM_Scale(quality=3, parallel_computing=True)

    ckpt = torch.load(
        os.path.join(ROOT, "packages/Model/MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth"),
        map_location=DEV,
    )
    fe.load_state_dict(ckpt["feature_extractor"])
    fc.gaussian_conditional.scale_table = fc.gaussian_conditional.scale_table.to(torch.int32)
    fc.load_state_dict(ckpt["feature_coding"])
    fc._update_per_channel_shifts(ckpt["per_channel_shifts"]["feature_coding"])
    fe.eval()
    fc.eval()

    rec = build_rec_model()
    rec.load_state_dict(
        torch.load(os.path.join(ROOT, "packages/Model/REC_model/model_rec.pt"), map_location=DEV)
    )
    rec.eval()
    return fe, fc, rec


class Wrap(nn.Module):
    """把带额外 kwargs 的子模块包成可导出模块(kwargs 在包装时固化)."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, *xs):
        return self.fn(*xs)


def cases(fe, fc, rec):
    """返回 [(name, module, [示例输入])]; 形状见草案 8.3."""
    from functools import partial

    fc.context.quantize_func = partial(fc.quantize)
    q_map = torch.full((1, 1, PAD_H // 16, PAD_W // 16), Q_FACTOR, dtype=torch.float32)

    def ctx_means(y, psi):
        return fc.context(y=y, hyper_params=psi)[1]      # means_hat

    # 真实分布输入: randn 会被 ResAU 放大到 ~1e7, 使对拍失去意义(见 s0c 二分)
    with torch.no_grad():
        y_real = fc.g_a_sft(fc.g_a(fe(torch.rand(1, 3, PAD_H, PAD_W))), q_map)

    return [
        ("01_feature_extractor", Wrap(lambda x: fe(x)), [torch.randn(1, 3, PAD_H, PAD_W)]),
        ("02_g_a", Wrap(lambda x: fc.g_a(x)), [torch.randn(1, 128, PAD_H // 4, PAD_W // 4)]),
        ("03_g_a_sft", Wrap(lambda x, q: fc.g_a_sft(x, q)),
         [torch.randn(1, 128, PAD_H // 16, PAD_W // 16), q_map]),
        ("04_hyper_encoder", Wrap(lambda y: fc.hyper_encoder(y, h=PAD_H, w=PAD_W)),
         [torch.randn(1, 128, PAD_H // 16, PAD_W // 16)]),
        ("05_hyper_decoder", Wrap(lambda z: fc.hyper_decoder(z, h=PAD_H, w=PAD_W)),
         [torch.randn(1, 128, PAD_H // 64, PAD_W // 64)]),
        ("06_hyper_scale_decoder", Wrap(lambda z: fc.hyper_scale_decoder(z, h=PAD_H, w=PAD_W)),
         [torch.randn(1, 128, PAD_H // 64, PAD_W // 64)]),
        ("07_context_mcm", Wrap(ctx_means),
         [torch.randn(1, 128, PAD_H // 16, PAD_W // 16),
          torch.randn(1, 256, PAD_H // 16, PAD_W // 16)]),
        ("08_g_s_sft", Wrap(lambda x, q: fc.g_s_sft(x, q)),
         [torch.randn(1, 128, PAD_H // 16, PAD_W // 16), q_map]),
        ("09_g_s", Wrap(lambda x: fc.g_s(x)), [y_real]),
        ("10_rec", Wrap(lambda x: rec(x)), [torch.randn(1, 128, PAD_H // 4, PAD_W // 4)]),
    ]


def op_stats(path):
    import onnx

    g = onnx.load(path).graph
    return dict(sorted(collections.Counter(n.op_type for n in g.node).items()))


def numeric_diff(module, inputs, path):
    import onnxruntime as ort

    with torch.no_grad():
        ref = module(*[t.clone() for t in inputs])    # inplace 会改输入 => 喂副本
    ref = ref if isinstance(ref, torch.Tensor) else ref[0]
    ref = ref.detach().cpu().numpy()

    sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    names = [i.name for i in sess.get_inputs()]
    out = sess.run(None, {n: t.clone().detach().cpu().numpy() for n, t in zip(names, inputs)})[0]
    return float(np.max(np.abs(out - ref))), tuple(ref.shape)


def main():
    os.makedirs(OUT, exist_ok=True)
    print("构建模型 ...")
    fe, fc, rec = build_models()
    for m in (fe, fc, rec):
        m.requires_grad_(False)               # Wrap 用闭包持子模块, 必须对真身关梯度
    print("OK")

    report, lines = {}, []
    for name, mod, inputs in cases(fe, fc, rec):
        entry = {"inputs": [tuple(t.shape) for t in inputs]}
        path = os.path.join(OUT, f"{name}.onnx")
        try:
            mod.requires_grad_(False)             # 导出前关梯度, 否则参数被当常量插入
            # 部分子模块含 inplace LeakyReLU: 调用/trace 会就地改输入 => 留原值, 每次喂副本
            inputs = [t.detach().clone().requires_grad_(False) for t in inputs]
            with torch.no_grad():
                mod(*[t.clone() for t in inputs])   # 先确认能前向
            torch.onnx.export(
                mod, tuple(t.clone() for t in inputs), path, opset_version=OPSET,
                input_names=[f"in{i}" for i in range(len(inputs))],
                output_names=["out"], do_constant_folding=True,
            )
            entry["export"] = "OK"
            entry["ops"] = op_stats(path)
        except Exception as e:                    # noqa: BLE001
            entry["export"] = "FAIL"
            entry["error"] = f"{type(e).__name__}: {e}".split("\n")[0][:400]
            entry["trace"] = traceback.format_exc().strip().split("\n")[-3:]
            report[name] = entry
            lines.append(f"[FAIL] {name}: {entry['error']}")
            continue

        try:
            d, oshape = numeric_diff(mod, inputs, path)
            entry["onnx_max_abs_diff"] = d
            entry["output_shape"] = list(oshape)
        except Exception as e:                    # noqa: BLE001
            entry["onnx_max_abs_diff"] = None
            entry["onnx_err"] = f"{type(e).__name__}: {e}".split("\n")[0][:200]
        report[name] = entry
        ops = ", ".join(f"{k}x{v}" for k, v in entry["ops"].items())
        lines.append(f"[ OK ] {name}: diff={entry.get('onnx_max_abs_diff')} | {ops}")

    with open(os.path.join(OUT, "s0_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT, "s0_summary.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("\n".join(lines))
    print(f"\n产物 -> {OUT}")


if __name__ == "__main__":
    main()
