#!/usr/bin/env python3
"""S9-A: 为本轮「板端单帧 e2e」备资产(离线, 开发机).

产出 DTM/out_s9/board/:
  x_in.npy          (1,3,576,960) f32  板端输入(= RM 口径 BGR/255, 960x544 上下补零)
  q_map.npy         (1,1,36,60)   f32  rate_control 常量
  ents.npz          熵编码常量表(与输入无关, 从 ckpt 加载的 buffer)
  y_str.bin/z_str.bin  PC 侧真码流(用于板上解码对拍)
  ref_*.npy         各环节 PyTorch 参考量
  info.json         形状/字节数/bpp
"""
from __future__ import annotations

import json
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

OUT = os.path.abspath(os.path.join(HERE, "..", "out_s9", "board"))
SRC_H, SRC_W = 544, 960


def main():
    os.makedirs(OUT, exist_ok=True)
    fe, fc, rec = s0.build_models()
    for m in (fe, fc, rec):
        m.requires_grad_(False)
    fc.context.quantize_func = partial(fc.quantize)

    gc, eb = fc.gaussian_conditional, fc.entropy_bottleneck
    info = {
        "gc_quantized_cdf": list(gc._quantized_cdf.shape),
        "gc_cdf_length": list(gc._cdf_length.shape),
        "gc_offset": list(gc._offset.shape),
        "gc_scale_table": list(gc.scale_table.shape),
        "gc_scale_table_dtype": str(gc.scale_table.dtype),
        "gc_scale_table_range": [int(gc.scale_table.min()), int(gc.scale_table.max())],
        "eb_quantized_cdf": list(eb._quantized_cdf.shape),
        "eb_cdf_length": list(eb._cdf_length.shape),
        "eb_offset": list(eb._offset.shape),
    }
    if gc._quantized_cdf.numel() == 0 or eb._quantized_cdf.numel() == 0:
        raise RuntimeError("熵表为空: ckpt 未含 buffer, 需先跑 update()")

    H, W = s0.PAD_H // 16, s0.PAD_W // 16
    q_map = torch.full((1, 1, H, W), s0.Q_FACTOR)
    im = Image.open(os.path.join(s0.ROOT, "logs/samples/000000481386.jpg")).convert("RGB").resize((SRC_W, SRC_H))
    a = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0
    x = F.pad(torch.from_numpy(a).unsqueeze(0), (0, 0, 0, s0.PAD_H - SRC_H), mode="constant", value=0)

    with torch.no_grad():
        f0 = fe(x)
        y = fc.g_a_sft(fc.g_a(f0), q_map)
        z = fc.hyper_encoder(y, h=s0.PAD_H, w=s0.PAD_W)
        z_hat = torch.round(z.clone())
        psi = fc.hyper_decoder(z_hat, h=s0.PAD_H, w=s0.PAD_W)
        scale = fc.hyper_scale_decoder(z_hat, h=s0.PAD_H, w=s0.PAD_W)
        _, mu, _, _ = fc.context(y=y, hyper_params=psi)
        idx_y = gc.build_indexes(scale)
        y_str = gc.compress(y, idx_y, means=mu)
        z_str = eb.compress(z)
        # 解码(PC 参考)
        z_hat2 = eb.decompress(z_str, z.size()[-2:])
        psi2 = fc.hyper_decoder(z_hat2, h=s0.PAD_H, w=s0.PAD_W)
        scale2 = fc.hyper_scale_decoder(z_hat2, h=s0.PAD_H, w=s0.PAD_W)
        y_res = gc.decompress(y_str, gc.build_indexes(scale2))
        y_hat = fc.context.decompress(y_res, psi2)
        y_sft = fc.g_s_sft(y_hat, q_map)
        x_hat = fc.g_s(y_sft)
        pix = rec(x_hat)

    yb, zb = y_str[0], z_str[0]
    n = len(yb) + len(zb)
    info.update({
        "y_bytes": len(yb), "z_bytes": len(zb), "total_bytes": n,
        "bpp_960x544": n * 8 / (960 * 544), "bpp_960x540": n * 8 / (960 * 540),
        "z_hat_close_to_z": bool(torch.equal(z_hat2, z_hat)),
        "psi2_close_to_psi": bool(torch.allclose(psi2, psi)),
    })

    def sv(name, t):
        t = t[0] if isinstance(t, (list, tuple)) else t
        np.save(os.path.join(OUT, f"ref_{name}.npy"), t.detach().cpu().numpy().astype(np.float32))

    np.save(os.path.join(OUT, "x_in.npy"), x.numpy().astype(np.float32))
    np.save(os.path.join(OUT, "q_map.npy"), q_map.numpy().astype(np.float32))
    np.save(os.path.join(OUT, "ref_f0_16.npy"), f0.numpy().astype(np.float16))
    for nm, t in (("y", y), ("z_hat", z_hat), ("psi", psi), ("scale", scale), ("mu", mu),
                  ("y_res", y_res), ("y_hat", y_hat), ("y_sft", y_sft), ("x_hat", x_hat), ("pix", pix)):
        sv(nm, t)
    open(os.path.join(OUT, "y_str.bin"), "wb").write(yb)
    open(os.path.join(OUT, "z_str.bin"), "wb").write(zb)
    np.save(os.path.join(OUT, "ref_sym_y.npy"), np.rint((y - mu).numpy()).astype(np.int32))
    np.save(os.path.join(OUT, "ref_sym_z.npy"),
            np.rint((z - eb._get_medians().detach().reshape(1, -1, 1, 1)).numpy()).astype(np.int32))
    np.savez(os.path.join(OUT, "ents.npz"),
             gc_cdf=gc._quantized_cdf.numpy().astype(np.int32),
             gc_clen=gc._cdf_length.numpy().astype(np.int32),
             gc_off=gc._offset.numpy().astype(np.int32),
             gc_scale=gc.scale_table.numpy().astype(np.int32),
             eb_cdf=eb._quantized_cdf.numpy().astype(np.int32),
             eb_clen=eb._cdf_length.numpy().astype(np.int32),
             eb_off=eb._offset.numpy().astype(np.int32),
             eb_medians=eb._get_medians().detach().numpy().astype(np.float32).ravel())
    with open(os.path.join(OUT, "info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print("assets ->", OUT)


if __name__ == "__main__":
    main()
