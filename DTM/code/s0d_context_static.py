#!/usr/bin/env python3
"""S0-A3: Context(MCM) 的静态化等价 + ONNX 导出与对拍 (关口 2).

原实现(src/codec/components/contexts/context.py + utils.py) 的问题:
  - down_shuffle/up_shuffle 用 y.numel()>>1 / k=m//W / shape 派生做 reshape/split/stack
    => 导出成 Shape/Gather/Unsqueeze/Concat 一大片动态形状算子, 且 ORT 加载即报 Type Error;
  - Context.forward 的 F.pad(..., w%2, h%2) 与 mask2(全1) 掩码写 0, 对本尺寸均为空操作.

静态化口径(固定 B=1,C=128,H=36,W=60):
  * 所有 reshape/split 尺寸用 Python int 常量(取自已知的 B,C,H,W), 不用 numel()/shape();
  * 去掉空操作: pad(0) 与全 1 掩码;
  * quantize 等价为 round(clamp(x, int32min, int32max))(STE 的 detach 在推理/导出下等价于 round).

产物: DTM/out_s0/07b_context_mcm_static.onnx + s0d_report.json
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

DEV = torch.device("cpu")
OPSET = 12
PAD_H, PAD_W = 576, 960          # y: 36x60, psi: 36x60
Q_FACTOR = 0.265
INT32_MIN, INT32_MAX = -2147483648, 2147483647


class ContextStatic(nn.Module):
    """Context(MCM) 的静态化等价(fixed shapes, 无 numel()/shape 派生)."""

    def __init__(self, ctx, H: int, W: int):
        super().__init__()
        self.cc_convs = ctx.cc_convs
        self.MCM = ctx.MCM
        self.H, self.W = H, W

    # ---------- 与 ContextUtils 同算式, 只把运行期尺寸换成常量 ----------
    @staticmethod
    def _down(y, C, H, W):
        B = y.shape[0]
        m = (B * C * H * W) // 2
        k = m // W
        W2, H2 = W // 2, H // 2
        yf1 = y.reshape(m, 2)
        s1, s2 = torch.split(yf1, 1, dim=1)
        yf2 = s1.reshape(k, 2, W2)
        s11, s12 = torch.split(yf2, 1, dim=1)
        yf3 = s2.reshape(k, 2, W2)
        s21, s22 = torch.split(yf3, 1, dim=1)
        return (s11.reshape(B, C, H2, W2), s22.reshape(B, C, H2, W2),
                s21.reshape(B, C, H2, W2), s12.reshape(B, C, H2, W2))   # 原实现返回序

    @staticmethod
    def _up(parts, B, C, h, w):
        part0, part3, part1, part2 = parts
        H2, W2 = h * 2, w * 2
        n = B * C * h * w * 2
        a = torch.stack((part0, part2), dim=3).reshape(n)
        b = torch.stack((part1, part3), dim=3).reshape(n)
        return torch.stack((a, b), dim=1).reshape(B, C, H2, W2)

    @staticmethod
    def _pad_channels(x, C2):
        B, C1, h, w = x.shape
        zeros = torch.zeros((B, C2 - C1, h, w), dtype=x.dtype)
        return torch.cat((x, zeros), dim=1)

    def _slice_pred(self, inp, hp, slice_rec, idx, C_in, C_hp, B, H, W):
        cc = self.cc_convs(slice_rec) if slice_rec is not None else torch.zeros((B, C_in, H, W))
        ch_s, ch_e = C_in * idx, C_in * (idx + 1)
        hp_l = self._down(hp, C_hp, H, W)
        cc_l = self._down(cc, C_in, H, W)
        in_l = self._down(inp, C_in, H, W)

        spatial = torch.zeros((B, C_in, H // 2, W // 2))
        mu_l, yh_l = [], []
        for st in range(4):
            mu_st = self.MCM[idx][st](hp_l[st], cc_l[st], spatial)
            diff2 = (in_l[st] - mu_st).repeat(1, 2, 1, 1)
            # quantize: 全 1 掩码 => 无需抹零; clamp 到 int32 range(本量级为 no-op) => round
            resi_dq = torch.clamp(diff2, INT32_MIN, INT32_MAX).round()[:, ch_s:ch_e]
            yh = resi_dq + mu_st
            yh_l.append(yh)
            mu_l.append(mu_st)
            spatial = torch.cat(yh_l, dim=1)
        return self._up(yh_l, B, C_in, H // 2, W // 2), self._up(mu_l, B, C_in, H // 2, W // 2)

    def forward(self, y, hyper_params):
        B, C, H, W = y.shape
        assert (B, C, H, W) == (1, 128, self.H, self.W), f"静态版仅支持 (1,128,{self.H},{self.W})，收到 {(B,C,H,W)}"
        C_hp = int(hyper_params.shape[1])
        C_in = C // 2
        y_pre, y_last = torch.chunk(y, 2, dim=1)
        rec1, mu1 = self._slice_pred(y_pre, hyper_params, None, 0, C_in, C_hp, B, H, W)
        rec2, mu2 = self._slice_pred(y_last, hyper_params, rec1, 1, C_in, C_hp, B, H, W)
        y_rec = torch.cat((rec1, rec2), dim=1)
        mu = torch.cat((mu1, mu2), dim=1)
        return y_rec, mu


def main():
    from functools import partial

    os.makedirs(OUT, exist_ok=True)
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
    fc.context.quantize_func = partial(fc.quantize)

    H, W = PAD_H // 16, PAD_W // 16      # 36, 60
    q_map = torch.full((1, 1, H, W), Q_FACTOR)
    with torch.no_grad():
        y = fc.g_a_sft(fc.g_a(fe(torch.rand(1, 3, PAD_H, PAD_W))), q_map)
        z = fc.hyper_encoder(y, h=PAD_H, w=PAD_W)
        psi = fc.hyper_decoder(torch.round(z.clone()), h=PAD_H, w=PAD_W)
        ref = fc.context(y=y, hyper_params=psi)          # (y_rec, mu, resi_q, resi_dq)

    st = ContextStatic(fc.context, H, W)
    st.requires_grad_(False)
    with torch.no_grad():
        s_rec, s_mu = st(y.clone(), psi.clone())

    rep = {
        "H_W": [H, W],
        "psi_range": [float(psi.min()), float(psi.max())],
        "mu_vs_orig_max_abs_diff": float((s_mu - ref[1]).abs().max()),
        "y_rec_vs_orig_max_abs_diff": float((s_rec - ref[0]).abs().max()),
        "mu_range": [float(ref[1].min()), float(ref[1].max())],
    }
    print("== PyTorch: 原 Context vs 静态化 ==")
    for k in ("mu_vs_orig_max_abs_diff", "y_rec_vs_orig_max_abs_diff", "mu_range", "psi_range"):
        print(f"  {k}: {rep[k]}")

    path = os.path.join(OUT, "07b_context_mcm_static.onnx")
    torch.onnx.export(st, (y.clone(), psi.clone()), path, opset_version=OPSET,
                      input_names=["y", "psi"], output_names=["y_rec", "mu"],
                      do_constant_folding=True)
    import onnx
    import onnxruntime as ort

    ops = {}
    for n in onnx.load(path).graph.node:
        ops[n.op_type] = ops.get(n.op_type, 0) + 1
    rep["onnx_ops"] = dict(sorted(ops.items()))
    dyn = [o for o in ops if o in ("Shape", "Gather", "Unsqueeze", "Squeeze", "Expand",
                                   "ConstantOfShape", "Tile", "ReduceProd", "NonZero")]
    rep["dynamic_shape_ops_left"] = dyn

    sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    o = sess.run(None, {"y": y.numpy(), "psi": psi.numpy()})
    rep["onnx_mu_vs_orig_max_abs_diff"] = float(np.abs(o[1] - ref[1].numpy()).max())
    rep["onnx_yrec_vs_orig_max_abs_diff"] = float(np.abs(o[0] - ref[0].numpy()).max())

    print("== ONNX(静态化) ==")
    print("  ops:", rep["onnx_ops"])
    print("  残留动态形状算子:", dyn)
    print("  vs 原 Context: mu max_abs_diff=%.6g ; y_rec max_abs_diff=%.6g"
          % (rep["onnx_mu_vs_orig_max_abs_diff"], rep["onnx_yrec_vs_orig_max_abs_diff"]))

    with open(os.path.join(OUT, "s0d_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print("产物 ->", path)


if __name__ == "__main__":
    main()
