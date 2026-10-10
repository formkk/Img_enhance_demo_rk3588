#!/usr/bin/env python3
"""S4: 按【正确口径】重做 10b(rec) 的 IO -- 修正 S2/S3 的夹具错误.

错误: x_hat = g_s(y)             (漏了解码端反 SFT)
正确: x_hat = g_s(g_s_sft(y_hat))  (tools/main.py 解码链: g_s_sft -> g_s)
"""
from __future__ import annotations

import os
import sys

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s0_export_onnx as s0                 # noqa: E402
import s0c_fix_rec_and_bisect_gs as s0c     # noqa: E402
from models.rec.rec import build_rec_model  # noqa: E402
from models.MIK_MCM_codecs import (         # noqa: E402
    build_feature_coding_Mean_MCM_Scale, build_feature_extractor)
from functools import partial               # noqa: E402

IO = os.path.abspath(os.path.join(HERE, "..", "out_s2", "io"))


def main():
    fe = build_feature_extractor()
    fc = build_feature_coding_Mean_MCM_Scale(quality=3, parallel_computing=True)
    ckpt = torch.load(os.path.join(s0.ROOT, "packages/Model/MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth"),
                      map_location=s0.DEV)
    fe.load_state_dict(ckpt["feature_extractor"])
    fc.gaussian_conditional.scale_table = fc.gaussian_conditional.scale_table.to(torch.int32)
    fc.load_state_dict(ckpt["feature_coding"])
    fc._update_per_channel_shifts(ckpt["per_channel_shifts"]["feature_coding"])
    fe.eval(); fc.eval()
    rec = build_rec_model()
    rec.load_state_dict(torch.load(os.path.join(s0.ROOT, "packages/Model/REC_model/model_rec.pt"),
                                   map_location=s0.DEV))
    rec.eval()
    for m in (fe, fc, rec):
        m.requires_grad_(False)
    fc.context.quantize_func = partial(fc.quantize)

    H, W = s0.PAD_H // 16, s0.PAD_W // 16
    q_map = torch.full((1, 1, H, W), s0.Q_FACTOR)
    with torch.no_grad():
        y = fc.g_a_sft(fc.g_a(fe(torch.rand(1, 3, s0.PAD_H, s0.PAD_W))), q_map)
        x_hat = fc.g_s(fc.g_s_sft(y, q_map))                     # <= 正确口径
        mod = s0c.RecFixed(rec); mod.requires_grad_(False)
        ref = mod(x_hat.clone())
    np.save(os.path.join(IO, "in_10b_rec_clamp_0.npy"), x_hat.detach().numpy().astype(np.float32))
    np.save(os.path.join(IO, "ref_10b_rec_clamp_0.npy"), ref.detach().numpy().astype(np.float32))
    print("x_hat 值域:", float(x_hat.min()), float(x_hat.max()))
    print("ref 值域  :", float(ref.min()), float(ref.max()), " mean:", float(ref.mean()))
    print("饱和255占比: %.2f%%" % float((ref == 255).float().mean() * 100))
    print("已重写 in_/ref_10b_rec_clamp_0.npy")


if __name__ == "__main__":
    main()
