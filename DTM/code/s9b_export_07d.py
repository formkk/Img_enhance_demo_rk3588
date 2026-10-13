#!/usr/bin/env python3
"""S9-B: 导出 07d = Context(MCM) 的**解码分支**(context.decompress)静态版并转 RKNN.

背景: 07b 是 Context.forward 的静态化, 输出 (y_rec, mu) —— 解码端要的不是它.
解码端走 `Context.decompress(resi, psi)`: 与 forward 同算式算 mu, 但把「输入」当残差,
y_hat = resi + mu (不再走 quantize)。二者第二 slice 的 slice_rec 不同 => 必须单独导一个网。

产物: DTM/out_s0_1344/07d_context_decompress_static.{onnx,rknn} + s9b_report.json
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import torch
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s0_export_onnx as s0            # noqa: E402
import s0d_context_static as s0d       # noqa: E402

OUT = os.path.abspath(os.path.join(HERE, "..", "out_s0_1344"))
TARGET = "rk3588"


class ContextDecompressStatic(s0d.ContextStatic):
    """Context.decompress 的静态等价(fixed B=1,C=128,H,W)."""

    def _slice_pred_dec(self, inp, hp, slice_rec, idx, C_in, C_hp, B, H, W):
        cc = self.cc_convs(slice_rec) if slice_rec is not None else torch.zeros((B, C_in, H, W))
        hp_l = self._down(hp, C_hp, H, W)
        cc_l = self._down(cc, C_in, H, W)
        in_l = self._down(inp, C_in, H, W)

        spatial = torch.zeros((B, C_in, H // 2, W // 2))
        yh_l = []
        for st in range(4):
            mu_st = self.MCM[idx][st](hp_l[st], cc_l[st], spatial)
            yh = in_l[st] + mu_st                 # 解码: 残差 = 输入本值(不过 quantize)
            yh_l.append(yh)
            spatial = torch.cat(yh_l, dim=1)
        return self._up(yh_l, B, C_in, H // 2, W // 2)

    def forward(self, resi, params):
        B, C, H, W = resi.shape
        assert (B, C, H, W) == (1, 128, self.H, self.W), \
            f"静态版仅支持 (1,128,{self.H},{self.W})，收到 {(B, C, H, W)}"
        C_hp = int(params.shape[1])
        C_in = C // 2
        r_pre, r_last = torch.chunk(resi, 2, dim=1)
        rec1 = self._slice_pred_dec(r_pre, params, None, 0, C_in, C_hp, B, H, W)
        rec2 = self._slice_pred_dec(r_last, params, rec1, 1, C_in, C_hp, B, H, W)
        return torch.cat((rec1, rec2), dim=1)


def main():
    from functools import partial

    os.makedirs(OUT, exist_ok=True)
    fe, fc, rec = s0.build_models()
    for m in (fe, fc, rec):
        m.requires_grad_(False)
    fc.context.quantize_func = partial(fc.quantize)

    H, W = s0.PAD_H // 16, s0.PAD_W // 16
    q_map = torch.full((1, 1, H, W), s0.Q_FACTOR)

    # 用真实链路产生的 (resi, psi) 做验证, 保证输入在域内
    src = np.asarray(Image.open(os.path.join(s0.ROOT, "logs/samples/000000481386.jpg"))
                     .convert("RGB").resize((1333, 750)))
    a = src[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0
    x = torch.nn.functional.pad(torch.from_numpy(a).unsqueeze(0),
                                (0, s0.PAD_W - 1333, 0, s0.PAD_H - 750), mode="constant", value=0)
    with torch.no_grad():
        y = fc.g_a_sft(fc.g_a(fe(x)), q_map)
        z = fc.hyper_encoder(y, h=s0.PAD_H, w=s0.PAD_W)
        psi = fc.hyper_decoder(torch.round(z.clone()), h=s0.PAD_H, w=s0.PAD_W)
        scale = fc.hyper_scale_decoder(torch.round(z.clone()), h=s0.PAD_H, w=s0.PAD_W)
        idxs = fc.gaussian_conditional.build_indexes(scale)
        y_str = fc.gaussian_conditional.compress(y, idxs, means=fc.context(y=y, hyper_params=psi)[1])
        resi = fc.gaussian_conditional.decompress(y_str, fc.gaussian_conditional.build_indexes(scale))
        ref = fc.context.decompress(resi, psi)

    st = ContextDecompressStatic(fc.context, H, W)
    st.requires_grad_(False)
    with torch.no_grad():
        got = st(resi.clone(), psi.clone())
    rep = {"torch_static_vs_orig_max_abs": float((got - ref).abs().max()),
           "resi_range": [float(resi.min()), float(resi.max())],
           "psi_range": [float(psi.min()), float(psi.max())],
           "ref_range": [float(ref.min()), float(ref.max())]}
    print("== PyTorch: 原 context.decompress vs 静态化 ==")
    print("   max_abs =", rep["torch_static_vs_orig_max_abs"])

    onnx_path = os.path.join(OUT, "07d_context_decompress_static.onnx")
    torch.onnx.export(st, (resi.clone(), psi.clone()), onnx_path, opset_version=s0.OPSET,
                      input_names=["resi", "psi"], output_names=["y_hat"], do_constant_folding=True)

    import onnxruntime as ort
    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    o = sess.run(None, {"resi": resi.numpy(), "psi": psi.numpy()})[0]
    rep["onnx_vs_orig_max_abs"] = float(np.abs(o - ref.numpy()).max())
    print("   ONNX(ORT) vs 原: max_abs =", rep["onnx_vs_orig_max_abs"])

    from rknn.api import RKNN
    rknn_path = os.path.join(OUT, "07d_context_decompress_static.rknn")
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

    with open(os.path.join(OUT, "s9b_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
