#!/usr/bin/env python3
"""S6: 07b(context/MCM) 的 phase 级二分 -- 定位那 0.04% 长尾来源.

背景: 07b 整网板上 corr 0.958、|偏差|>0.5 仅占 0.040%。本脚本把 slice0 拆开:
  - 逐 phase(p0..p3): 把 3 个输入按通道拼成 1 个输入(绕过 RKNN 3 输入属性查询失败);
  - 累计阶段(cum1..cum3): 跑到第 k 个 stage, 看误差是否随 stage 累积;
  - round 单测: 纯逐元素 round(clamp(.)), 检验"离散跳变"本身是否在板上就已发散。
产物: DTM/out_s6/*.onnx/*.rknn + io/{in_,ref_}*.npy + meta.json
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import torch
import torch.nn as nn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s0_export_onnx as s0            # noqa: E402
import s0d_context_static as s0d       # noqa: E402
from models.MIK_MCM_codecs import (    # noqa: E402
    build_feature_coding_Mean_MCM_Scale, build_feature_extractor)
from rknn.api import RKNN              # noqa: E402
from functools import partial          # noqa: E402

OUT = os.path.abspath(os.path.join(HERE, "..", "out_s6"))
IO = os.path.join(OUT, "io")
OPSET = 12
IMIN, IMAX = -2147483648, 2147483647


class Wrap(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, *xs):
        return self.fn(*xs)


class PhaseSingle(nn.Module):
    """三个输入按通道拼接成单输入 -> phase 网."""

    def __init__(self, phase, c_hp, c_cc):
        super().__init__()
        self.p = phase
        self.c_hp, self.c_cc = c_hp, c_cc

    def forward(self, x):
        a = x[:, :self.c_hp]
        b = x[:, self.c_hp:self.c_hp + self.c_cc]
        c = x[:, self.c_hp + self.c_cc:]
        return self.p(a, b, c)


class SlicePartial(nn.Module):
    """slice0 跑到第 n_stages 个 stage, 返回 (yh_last, spatial)."""

    def __init__(self, ctx, st, n_stages, C_in, C_hp, H, W):
        super().__init__()
        self.ctx, self.st = ctx, st
        self.n, self.C_in, self.C_hp, self.H, self.W = n_stages, C_in, C_hp, H, W

    def forward(self, y_pre, psi):
        B, H, W, C_in, C_hp = 1, self.H, self.W, self.C_in, self.C_hp
        hp_l = self.st._down(psi, C_hp, H, W)
        cc_l = self.st._down(torch.zeros((B, C_in, H, W)), C_in, H, W)
        in_l = self.st._down(y_pre, C_in, H, W)
        spatial = torch.zeros((B, C_in, H // 2, W // 2))
        yh_list = []
        for stg in range(self.n):
            mu = self.ctx.MCM[0][stg](hp_l[stg], cc_l[stg], spatial)
            d2 = (in_l[stg] - mu).repeat(1, 2, 1, 1)
            resi = torch.clamp(d2, IMIN, IMAX).round()[:, :C_in]
            yh = resi + mu
            yh_list.append(yh)
            spatial = torch.cat(yh_list, dim=1)
        return yh_list[-1], spatial


def build_segments(fe, fc):
    H, W = s0.PAD_H // 16, s0.PAD_W // 16
    C, C_in, C_hp = 128, 64, 256
    q_map = torch.full((1, 1, H, W), s0.Q_FACTOR)
    with torch.no_grad():
        y = fc.g_a_sft(fc.g_a(fe(torch.rand(1, 3, s0.PAD_H, s0.PAD_W))), q_map)
        z = fc.hyper_encoder(y, h=s0.PAD_H, w=s0.PAD_W)
        psi = fc.hyper_decoder(torch.round(z.clone()), h=s0.PAD_H, w=s0.PAD_W)
    y_pre = torch.chunk(y, 2, dim=1)[0]
    st = s0d.ContextStatic(fc.context, H, W)
    for m in (st,):
        m.requires_grad_(False)

    segs = []
    # ---- 逐 phase(单输入拼接) ----
    with torch.no_grad():
        hp_l = st._down(psi.clone(), C_hp, H, W)
        cc_l = st._down(torch.zeros((1, C_in, H, W)), C_in, H, W)
        in_l = st._down(y_pre.clone(), C_in, H, W)
        spatial = torch.zeros((1, C_in, H // 2, W // 2))
        for stg in range(4):
            cat_in = torch.cat((hp_l[stg], cc_l[stg], spatial), dim=1)
            segs.append((f"07b_p{stg}_phase", PhaseSingle(fc.context.MCM[0][stg], C_hp, C_in), [cat_in]))
            mu = fc.context.MCM[0][stg](hp_l[stg].clone(), cc_l[stg].clone(), spatial.clone())
            d2 = (in_l[stg] - mu).repeat(1, 2, 1, 1)
            resi = torch.clamp(d2, IMIN, IMAX).round()[:, :C_in]
            yh = resi + mu
            spatial = torch.cat((spatial, yh), dim=1) if stg > 0 else yh
        # round 单测: 用 stage3 的 d2
        segs.append(("07b_round_only", Wrap(lambda x: torch.clamp(x, IMIN, IMAX).round()), [d2]))
        # 累计阶段
        for k in (1, 2, 3):
            segs.append((f"07b_cum{k}", SlicePartial(fc.context, st, k, C_in, C_hp, H, W), [y_pre, psi]))
    return segs


def main():
    os.makedirs(IO, exist_ok=True)
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

    meta, ok = {}, 0
    for name, mod, inputs in build_segments(fe, fc):
        mod.requires_grad_(False)
        base = [t.detach().clone() for t in inputs]
        try:
            with torch.no_grad():
                out = mod(*[t.clone() for t in base])
                outs = list(out) if isinstance(out, (tuple, list)) else [out]
                torch.onnx.export(mod, tuple(t.clone() for t in base), os.path.join(OUT, name + ".onnx"),
                                  opset_version=OPSET, input_names=[f"in{i}" for i in range(len(base))],
                                  output_names=[f"out{i}" for i in range(len(outs))],
                                  do_constant_folding=True)
        except Exception as e:                        # noqa: BLE001
            print(f"{name:22s} EXPORT_FAIL {type(e).__name__}: {str(e)[:70]}")
            continue
        for i, t in enumerate(base):
            np.save(os.path.join(IO, f"in_{name}_{i}.npy"), t.numpy().astype(np.float32))
        for i, t in enumerate(outs):
            np.save(os.path.join(IO, f"ref_{name}_{i}.npy"), t.detach().cpu().numpy().astype(np.float32))
        meta[name] = {"inputs": [list(t.shape) for t in base],
                      "refs": [list(t.shape) for t in outs], "rknn": None}
        rk = RKNN(verbose=False)
        st_ = "converted"
        try:
            rk.config(target_platform="rk3588")
            if rk.load_onnx(model=os.path.join(OUT, name + ".onnx")) != 0:
                raise RuntimeError("load_onnx")
            if rk.build(do_quantization=False) != 0:
                raise RuntimeError("build")
            if rk.export_rknn(os.path.join(OUT, name + ".rknn")) != 0:
                raise RuntimeError("export")
            meta[name]["rknn"] = name + ".rknn"
            ok += 1
        except Exception as e:                        # noqa: BLE001
            st_ = f"RKNN_FAIL {type(e).__name__}: {str(e)[:70]}"
        finally:
            try:
                rk.release()
            except Exception:                         # noqa: BLE001
                pass
        print(f"{name:22s} {st_}  in={[tuple(t.shape) for t in base]} out={[tuple(t.shape) for t in outs]}")
        sys.stdout.flush()

    json.dump(meta, open(os.path.join(IO, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n段数 {len(meta)}  已转 {ok}  -> {OUT}")


if __name__ == "__main__":
    main()
