#!/usr/bin/env python3
"""S3: 07b/10b 的子图二分 -- 本机导出 ONNX + 转 RKNN + 落 IO(输入/参考).

二分切法:
  10b(rec): 按 head/body/tail 的每个子模块切 12 段(重点盯 subpel_conv3x3=Conv+PixelShuffle);
  07b(context): 直击三个原语 -- down_shuffle / up_shuffle / MCM_phase1, 外加整段 slice0.

产物: DTM/out_s3/*.onnx + *.rknn; DTM/out_s3/io/{in_,ref_}_*.npy + meta.json
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
from models.rec.rec import build_rec_model  # noqa: E402
from models.MIK_MCM_codecs import (  # noqa: E402
    build_feature_coding_Mean_MCM_Scale,
    build_feature_extractor,
)
from rknn.api import RKNN              # noqa: E402

OUT = os.path.abspath(os.path.join(HERE, "..", "out_s3"))
IO = os.path.join(OUT, "io")
OPSET = 12


class Wrap(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, *xs):
        return self.fn(*xs)


def build_segments(fe, fc, rec):
    from functools import partial

    fc.context.quantize_func = partial(fc.quantize)
    H, W = s0.PAD_H // 16, s0.PAD_W // 16
    q_map = torch.full((1, 1, H, W), s0.Q_FACTOR)
    with torch.no_grad():
        y = fc.g_a_sft(fc.g_a(fe(torch.rand(1, 3, s0.PAD_H, s0.PAD_W))), q_map).detach()
        z = fc.hyper_encoder(y, h=s0.PAD_H, w=s0.PAD_W)
        psi = fc.hyper_decoder(torch.round(z.clone()), h=s0.PAD_H, w=s0.PAD_W).detach()
        x_hat = fc.g_s(y.clone()).detach()

    segs = []

    # ---------- 10b: rec 逐子模块 ----------
    with torch.no_grad():
        h = rec.head(x_hat.clone())
        b = rec.body(h.clone()) + h
        t = rec.tail(b.clone())
        t = t.clamp(0.0, 255.0)
    chains = [("head", rec.head, x_hat), ("body", rec.body, h), ("tail", rec.tail, b)]
    for gname, seq, start in chains:
        cur = start
        for i, ch in enumerate(seq):
            segs.append((f"10b_{gname}{i}_{type(ch).__name__}", Wrap(lambda x, _m=ch: _m(x)), [cur]))
            with torch.no_grad():
                cur = ch(cur.clone())
    segs.append(("10b_tail9_clamp", Wrap(lambda x: x.clamp(0.0, 255.0)), [t]))

    # ---------- 07b: 三个原语 + slice0 ----------
    C, C_hp = 128, int(psi.shape[1])
    C_in = C // 2
    segs.append(("07b_prim_down_shuffle_y", Wrap(lambda x: s0d.ContextStatic._down(x, C, H, W)), [y]))
    with torch.no_grad():
        yp = s0d.ContextStatic._down(y.clone(), C, H, W)
    segs.append(("07b_prim_up_shuffle", Wrap(lambda a, b_, c, d:
                                             s0d.ContextStatic._up((a, b_, c, d), 1, C, H // 2, W // 2)),
                 list(yp)))
    with torch.no_grad():
        hp_l = s0d.ContextStatic._down(psi.clone(), C_hp, H, W)
        cc0 = torch.zeros((1, C_in, H, W))
        cc_l = s0d.ContextStatic._down(cc0, C_in, H, W)
        sp0 = torch.zeros((1, C_in, H // 2, W // 2))
        mu0 = fc.context.MCM[0][0](hp_l[0].clone(), cc_l[0].clone(), sp0.clone())
    segs.append(("07b_prim_phase1_stage0", Wrap(lambda a, b_, c: fc.context.MCM[0][0](a, b_, c)),
                 [hp_l[0].detach(), cc_l[0].detach(), sp0.detach()]))

    st = s0d.ContextStatic(fc.context, H, W)
    y_pre = torch.chunk(y, 2, dim=1)[0].detach()
    segs.append(("07b_slice0", Wrap(lambda a, b_, c, d:
                                    st._slice_pred(a, b_, None, 0, C_in, C_hp, 1, H, W)),
                 [y_pre, psi]))
    segs.append(("07b_full", Wrap(lambda a, b_: st(a, b_)), [y, psi]))
    return segs


def main():
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
    rec = build_rec_model()
    rec.load_state_dict(torch.load(os.path.join(s0.ROOT, "packages/Model/REC_model/model_rec.pt"),
                                   map_location=s0.DEV))
    rec.eval()
    for m in (fe, fc, rec):
        m.requires_grad_(False)

    meta, ok = {}, 0
    for name, mod, inputs in build_segments(fe, fc, rec):
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
        except Exception as e:                             # noqa: BLE001
            print(f"{name:38s} EXPORT_FAIL {type(e).__name__}: {str(e)[:80]}")
            continue
        for i, tt in enumerate(base):
            np.save(os.path.join(IO, f"in_{name}_{i}.npy"), tt.numpy().astype(np.float32))
        for i, tt in enumerate(outs):
            np.save(os.path.join(IO, f"ref_{name}_{i}.npy"), tt.detach().cpu().numpy().astype(np.float32))
        meta[name] = {"inputs": [list(t.shape) for t in base],
                      "refs": [list(t.shape) for t in outs], "rknn": None}

        rk = RKNN(verbose=False)
        st = "converted"
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
        except Exception as e:                             # noqa: BLE001
            st = f"RKNN_FAIL {type(e).__name__}: {str(e)[:80]}"
        finally:
            try:
                rk.release()
            except Exception:                              # noqa: BLE001
                pass
        print(f"{name:38s} {st}")
        sys.stdout.flush()

    with open(os.path.join(IO, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"\n段数 {len(meta)}  已转 RKNN {ok}  -> {OUT}")


if __name__ == "__main__":
    main()
