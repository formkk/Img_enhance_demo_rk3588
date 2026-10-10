#!/usr/bin/env python3
"""S4-前置: 用 DTM 自带 COCO 样图 vs 随机图, 对比 rec 内部量级 -- 判断"FP16 溢出"是否真实输入下也存在.

注意: pixel_mean/std 取 detectron2 默认(BGR [103.530,116.280,123.675] / std 1.0);
      packages/Environment 为空, 未能读到官方 cfg => 此值为**假定口径**(如实标注).
"""
from __future__ import annotations

import os
import sys

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s0_export_onnx as s0                      # noqa: E402
from models.rec.rec import build_rec_model       # noqa: E402
from models.MIK_MCM_codecs import (              # noqa: E402
    build_feature_coding_Mean_MCM_Scale, build_feature_extractor)
from functools import partial                    # noqa: E402

PAD_H, PAD_W = 576, 960
SRC_H, SRC_W = 544, 960
MEAN = np.array([103.530, 116.280, 123.675], dtype=np.float32).reshape(3, 1, 1)  # BGR


def load_real(denorm="default"):
    p = os.path.join(s0.ROOT, "logs/samples/000000481386.jpg")
    im = Image.open(p).convert("RGB").resize((SRC_W, SRC_H))
    arr = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1)   # -> BGR CHW
    x = torch.from_numpy(arr).unsqueeze(0)
    if denorm == "default":            # detectron2 默认(假定口径) — 已证伪
        x = (x - torch.from_numpy(MEAN)) / 1.0
    elif denorm == "div255":           # 仅 /255 -> [0,1], 与随机图同量级
        x = x / 255.0
    return F.pad(x, (0, PAD_W - SRC_W, 0, PAD_H - SRC_H), mode="constant", value=0)


def stats(name, t):
    a = t.detach().numpy()
    print(f"    {name:28s} min={a.min():.4g} max={a.max():.4g} mean={a.mean():.4g} "
          f"absmax={np.abs(a).max():.4g}")


def run(tag, img, fe, fc, rec):
    print(f"  == {tag} ==")
    with torch.no_grad():
        stats("input image", img)
        f0 = fe(img); stats("feature_extractor", f0)
        y = fc.g_a_sft(fc.g_a(f0), torch.full((1, 1, PAD_H // 16, PAD_W // 16), s0.Q_FACTOR))
        stats("y (latent)", y)
        x_hat = fc.g_s(fc.g_s_sft(y, torch.full((1, 1, PAD_H // 16, PAD_W // 16), s0.Q_FACTOR)))
        stats("x_hat (=rec 输入, 已含 g_s_sft)", x_hat)
        cur = x_hat
        for gname, seq in (("head", rec.head), ("body", rec.body), ("tail", rec.tail)):
            if gname == "body":
                h = rec.head(x_hat.clone())
                cur = h
            elif gname == "tail":
                h = rec.head(x_hat.clone()); cur = rec.body(h.clone()) + h
            for i, ch in enumerate(seq):
                cur = ch(cur.clone())
                stats(f"{gname}{i}_{type(ch).__name__}", cur)
        out = cur.clamp(0, 255); stats("clamp(0,255)", out)
        u8 = out.numpy().astype(np.uint8)
        print(f"    -> 输出图像: mean={u8.mean():.1f} 饱和255占比={(u8==255).mean()*100:.1f}% "
              f"饱和0占比={(u8==0).mean()*100:.1f}%")


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

    torch.manual_seed(0)
    run("真实图 + 仅 /255 归一化 (与随机图同量级)", load_real("div255"), fe, fc, rec)
    run("随机图(与 S2/S3 同口径)", torch.rand(1, 3, PAD_H, PAD_W), fe, fc, rec)


if __name__ == "__main__":
    main()
