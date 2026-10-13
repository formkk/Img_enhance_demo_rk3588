#!/usr/bin/env python3
"""S0-A1: hyper_scale_decoder 的 float 等价重写 + ONNX 导出与对拍.

原实现(models/Fix_point_layer.py IntConv2d_sigma.forward):
    x = clamp(x, -2^15, 2^15-1) -> int32 卷积 -> 逐通道算术右移 >> s_c
    其中 s_c = clamp(per_channel_shift_c + (in_precision - out_precision), 0, 31)

float 等价(ONNX/RKNN 友好, 无位运算):
    y = floor( conv(x, W) / 2^s_c )        # 算术右移 == 向下取整除法

口径: 该等价用于"板端移植版"; 只要求 编码端与解码端一致 + 数值接近 RM,
      不要求与 RM 逐位一致(草案 R5).

产物: DTM/out_s0_1344/06b_hyper_scale_decoder_float.onnx + s0b_report.json
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "DTM-V-1.5"))
OUT = os.path.abspath(os.path.join(HERE, "..", "out_s0_1344"))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import compressai  # noqa: E402

compressai.set_entropy_coder("ans")

from models.MIK_MCM_codecs import (  # noqa: E402
    build_feature_coding_Mean_MCM_Scale,
    build_feature_extractor,
)

DEV = torch.device("cpu")
OPSET = 12
PAD_H, PAD_W = 768, 1344
Q_FACTOR = 0.265


class IntConvSigmaAsFloat(nn.Module):
    """IntConv2d_sigma 的 float 等价: 整数卷积 + 逐通道右移 -> 浮点除 + floor."""

    def __init__(self, iconv):
        super().__init__()
        self.weight = iconv.weight                       # 整数值权重(共享)
        self.bias = iconv.bias
        self.stride = iconv.stride
        self.padding = iconv.padding
        self.groups = iconv.groups
        self.max_signal_value = int(iconv.max_signal_value)
        s = iconv.per_channel_shift.clone().float() + (iconv.in_precision - iconv.out_precision)
        self.register_buffer("shift", s.clamp(0, 31))

    def forward(self, x):
        x = x.clamp(-self.max_signal_value - 1, self.max_signal_value).float()
        w = self.weight.detach().float()
        b = None if self.bias is None else self.bias.detach().float()
        y = F.conv2d(x, w, b, self.stride, self.padding, 1, self.groups)
        y = y / torch.pow(torch.tensor(2.0), self.shift).view(1, -1, 1, 1)
        return torch.floor(y)


class HyperScaleDecoderFloat(nn.Module):
    """HyperScaleDecoder 的 float 等价(结构同原, 仅换三处定点卷积)."""

    def __init__(self, hsd):
        super().__init__()
        self.c1 = IntConvSigmaAsFloat(hsd.conv1)
        self.c2 = IntConvSigmaAsFloat(hsd.depthwise)
        self.c3 = IntConvSigmaAsFloat(hsd.pointwise)
        self.ps = hsd.ps
        self.bitdepth = int(hsd.sigma_bitdepth)

    def forward(self, x):
        # 等价 x.to(torch.int32)(向零截断): 本网输入恒为 round(z) 后的整数值,
        # 故 trunc / floor / 恒等 三者等价; 用 floor 是为避免 Ceil(RKNN 无 lowering)
        x = torch.floor(x.float())
        x = torch.relu(self.c1(x))
        x = torch.relu(self.c2(x))
        x = self.c3(x)
        x = self.ps(x)
        return x.abs().clamp(0.0, float(2 ** self.bitdepth - 1))


def build():
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
    for m in (fe, fc):
        m.requires_grad_(False)
    return fe, fc


def main():
    os.makedirs(OUT, exist_ok=True)
    fe, fc = build()
    hsd = fc.hyper_scale_decoder
    flo = HyperScaleDecoderFloat(hsd)
    flo.requires_grad_(False)

    # 真实输入: 走一遍编码端到 z_hat
    from functools import partial

    fc.context.quantize_func = partial(fc.quantize)
    q_map = torch.full((1, 1, PAD_H // 16, PAD_W // 16), Q_FACTOR)
    with torch.no_grad():
        y0 = fe(torch.rand(1, 3, PAD_H, PAD_W))
        y = fc.g_a_sft(fc.g_a(y0), q_map)
        z = fc.hyper_encoder(y, h=PAD_H, w=PAD_W)
        z_hat = torch.round(z.clone())

        ref = hsd(z_hat)                  # 原定点实现(注意: 会就地改 weight dtype)
        alt = flo(z_hat)                  # float 等价

    ref_f = ref.float()
    d = (alt - ref_f).abs()
    rep = {
        "input_z_hat": list(z_hat.shape),
        "ref_range": [float(ref_f.min()), float(ref_f.max())],
        "shift_per_channel_unique": sorted({int(v) for v in flo.c1.shift.tolist()}),
        "max_abs_diff": float(d.max()),
        "mean_abs_diff": float(d.mean()),
        "exact_match_rate": float((d == 0).float().mean()),
        "near_match_rate(abs<=1)": float((d <= 1).float().mean()),
    }
    print("== PyTorch: 定点 vs float 等价 ==")
    for k, v in rep.items():
        print(f"  {k}: {v}")

    # 导出 float 等价
    path = os.path.join(OUT, "06b_hyper_scale_decoder_float.onnx")
    torch.onnx.export(
        flo, (z_hat,), path, opset_version=OPSET,
        input_names=["z_hat"], output_names=["scale"], do_constant_folding=True,
    )
    import onnx
    import onnxruntime as ort

    g = onnx.load(path).graph
    ops = {}
    for n in g.node:
        ops[n.op_type] = ops.get(n.op_type, 0) + 1
    rep["onnx_ops"] = dict(sorted(ops.items()))

    sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    out = sess.run(None, {"z_hat": z_hat.numpy()})[0]
    d2 = np.abs(out - ref_f.numpy())
    rep["onnx_vs_ref_max_abs_diff"] = float(d2.max())
    rep["onnx_vs_ref_exact_rate"] = float((d2 == 0).mean())
    rep["onnx_vs_float_max_abs_diff"] = float(np.abs(out - alt.numpy()).max())

    print("== ONNX(float 等价) ==")
    print("  ops:", rep["onnx_ops"])
    print("  vs 定点 ref: max_abs_diff=%.6g exact=%.4f" % (rep["onnx_vs_ref_max_abs_diff"], rep["onnx_vs_ref_exact_rate"]))
    print("  vs float 等价(PT): max_abs_diff=%.6g" % rep["onnx_vs_float_max_abs_diff"])

    with open(os.path.join(OUT, "s0b_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print("产物 ->", path)


if __name__ == "__main__":
    main()
