#!/usr/bin/env python3
"""S5: 复核 07b(context/MCM) 的输入口径与数值域.

对照源码:
  MIKImageEncoder.forward : features = feature_extractor(image)
                            feature_coding.compress(features, q_map, ...)
  CodingNet.compress      : y = g_a(x); y = g_a_sft(y, q_map); z = hyper_encoder(y)
                            z_hat = round(z); psi = hyper_decoder(z_hat)
                            context(y=y, hyper_params=psi)     <-- 07b 的两个输入

本脚本对比两种输入来源: (a) 真实 COCO 样图 /255; (b) 随机图 —— 看数值域是否 OOD,
并把静态化 ONNX(ORT) 与 PyTorch 在**两种输入**上分别对拍.
"""
from __future__ import annotations

import os
import sys
from functools import partial

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s0_export_onnx as s0            # noqa: E402
import s0d_context_static as s0d       # noqa: E402
from models.MIK_MCM_codecs import (    # noqa: E402
    build_feature_coding_Mean_MCM_Scale, build_feature_extractor)

PAD_H, PAD_W = 576, 960
SRC_H, SRC_W = 544, 960


def real_image():
    p = os.path.join(s0.ROOT, "logs/samples/000000481386.jpg")
    im = Image.open(p).convert("RGB").resize((SRC_W, SRC_H))
    a = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0
    x = torch.from_numpy(a).unsqueeze(0)
    return F.pad(x, (0, PAD_W - SRC_W, 0, PAD_H - SRC_H), mode="constant", value=0)


def rng(t):
    a = t.detach().numpy()
    return f"min={a.min():.4g} max={a.max():.4g} mean={a.mean():.4g} std={a.std():.4g}"


def main():
    fe = build_feature_extractor()
    fc = build_feature_coding_Mean_MCM_Scale(quality=3, parallel_computing=True)
    ck = torch.load(os.path.join(s0.ROOT, "packages/Model/MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth"),
                    map_location=s0.DEV)
    fe.load_state_dict(ck["feature_extractor"])
    fc.gaussian_conditional.scale_table = fc.gaussian_conditional.scale_table.to(torch.int32)
    fc.load_state_dict(ck["feature_coding"])
    fc._update_per_channel_shifts(ck["per_channel_shifts"]["feature_coding"])
    fe.eval(); fc.eval()
    for m in (fe, fc):
        m.requires_grad_(False)
    fc.context.quantize_func = partial(fc.quantize)

    H, W = PAD_H // 16, PAD_W // 16
    q_map = torch.full((1, 1, H, W), s0.Q_FACTOR)
    st = s0d.ContextStatic(fc.context, H, W)
    st.requires_grad_(False)

    import onnxruntime as ort
    sess = ort.InferenceSession(os.path.join(HERE, "..", "out_s0", "07b_context_mcm_static.onnx"),
                                providers=["CPUExecutionProvider"])

    for tag, img in (("真实图(/255)", real_image()), ("随机图(与 S2/S3 同)", torch.rand(1, 3, PAD_H, PAD_W))):
        print(f"== {tag} ==")
        with torch.no_grad():
            f0 = fe(img);                      print("   feature_extractor:", rng(f0))
            y = fc.g_a_sft(fc.g_a(f0), q_map); print("   y  (07b 输入1):", rng(y))
            z = fc.hyper_encoder(y, h=PAD_H, w=PAD_W)
            psi = fc.hyper_decoder(torch.round(z.clone()), h=PAD_H, w=PAD_W)
            print("   psi(07b 输入2):", rng(psi))
            ref = fc.context(y=y.clone(), hyper_params=psi.clone())[1]
            print("   mu (PyTorch 原):", rng(ref))
            s = st(y.clone(), psi.clone())[1]
            print("   静态化 vs 原   : max_abs=%.3g" % float((s - ref).abs().max()))
            o = torch.from_numpy(sess.run(None, {"y": y.numpy(), "psi": psi.numpy()})[1])
            d = (o - ref).abs()
            corr = float(np.corrcoef(ref.numpy().ravel(), o.numpy().ravel())[0, 1])
            print("   ONNX(ORT) vs 原: max_abs=%.3g rel=%.3g corr=%.8f"
                  % (float(d.max()), float(d.max() / max(1e-12, float(ref.abs().max()))), corr))


if __name__ == "__main__":
    main()
