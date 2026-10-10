#!/usr/bin/env python3
"""S2 前置: 生成板上对拍所需的输入(.npy) 与 PyTorch 参考输出(.npy).

复用 s0/s0b/s0c/s0d 的建模与用例定义(含 06b/07b/10b 三个修版模块), 保证
"板上跑的是同一份输入"。产物 DTM/out_s2/io/ + meta.json。
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import s0_export_onnx as s0            # noqa: E402
import s0b_hsd_float_equiv as s0b      # noqa: E402
import s0c_fix_rec_and_bisect_gs as s0c  # noqa: E402
import s0d_context_static as s0d       # noqa: E402
from models.rec.rec import build_rec_model  # noqa: E402
from models.MIK_MCM_codecs import (  # noqa: E402
    build_feature_coding_Mean_MCM_Scale,
    build_feature_extractor,
)

IO = os.path.abspath(os.path.join(HERE, "..", "out_s2", "io"))
torch.manual_seed(1234)


def main():
    from functools import partial

    os.makedirs(IO, exist_ok=True)
    fe = build_feature_extractor()
    fc = build_feature_coding_Mean_MCM_Scale(quality=3, parallel_computing=True)
    ckpt = torch.load(os.path.join(s0.ROOT, "packages/Model/MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth"),
                      map_location=s0.DEV)
    fe.load_state_dict(ckpt["feature_extractor"])
    fc.gaussian_conditional.scale_table = fc.gaussian_conditional.scale_table.to(torch.int32)
    fc.load_state_dict(ckpt["feature_coding"])
    fc._update_per_channel_shifts(ckpt["per_channel_shifts"]["feature_coding"])
    fe.eval(); fc.eval()
    for m in (fe, fc):
        m.requires_grad_(False)
    fc.context.quantize_func = partial(fc.quantize)

    rec = build_rec_model()
    rec.load_state_dict(torch.load(os.path.join(s0.ROOT, "packages/Model/REC_model/model_rec.pt"),
                                   map_location=s0.DEV))
    rec.eval()

    # 用例: 01..09 复用 s0.cases; 06b/07b/10b 用修版模块
    cases = list(s0.cases(fe, fc, rec))
    H, W = s0.PAD_H // 16, s0.PAD_W // 16
    y_r, psi_r = s0d_inputs(fe, fc, H, W)

    # 09 在 s0.cases 里已用真实分布输入
    cases.append(("06b_hyper_scale_decoder_float", s0b.HyperScaleDecoderFloat(fc.hyper_scale_decoder),
                  [torch.round(fc.hyper_encoder(y_r, h=s0.PAD_H, w=s0.PAD_W))]))
    cases.append(("07b_context_mcm_static", s0d.ContextStatic(fc.context, H, W), [y_r, psi_r]))
    cases.append(("10b_rec_clamp", s0c.RecFixed(rec), [fc.g_s(y_r)]))

    meta = {}
    for name, mod, inputs in cases:
        mod.requires_grad_(False)
        with torch.no_grad():
            out = mod(*[t.clone() for t in inputs])
        outs = list(out) if isinstance(out, (tuple, list)) else [out]
        for i, t in enumerate(inputs):
            np.save(os.path.join(IO, f"in_{name}_{i}.npy"), t.detach().cpu().numpy().astype(np.float32))
        for i, t in enumerate(outs):
            np.save(os.path.join(IO, f"ref_{name}_{i}.npy"), t.detach().cpu().numpy().astype(np.float32))
        meta[name] = {"inputs": [list(t.shape) for t in inputs],
                      "refs": [list(t.shape) for t in outs]}
        print(f"{name:34s} in={[tuple(t.shape) for t in inputs]} ref={[tuple(t.shape) for t in outs]}")

    with open(os.path.join(IO, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("IO ->", IO)


def s0d_inputs(fe, fc, H, W):
    q_map = torch.full((1, 1, H, W), s0.Q_FACTOR)
    with torch.no_grad():
        y = fc.g_a_sft(fc.g_a(fe(torch.rand(1, 3, s0.PAD_H, s0.PAD_W))), q_map)
        z = fc.hyper_encoder(y, h=s0.PAD_H, w=s0.PAD_W)
        psi = fc.hyper_decoder(torch.round(z.clone()), h=s0.PAD_H, w=s0.PAD_W)
    return y.detach(), psi.detach()


if __name__ == "__main__":
    main()
