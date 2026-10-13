#!/usr/bin/env python3
"""V10：诊断"任务尺寸(1344x768)上重建图变横条纹"——是**尺寸**问题还是**补零**问题。

判据（与画图无关，纯标量）：
    corr(x_hat, f0)  —— 解码特征 vs 编码器特征的相关性；好码流应接近 1，坏了会掉到 0 附近
    同时存 REC 图与字节数，便于目视。
对照三组：
    A  内容 960x544 → 补零 960x576   （= v1/v6，已知**正常**）
    B  内容 1344x768，不补零          （纯尺寸因素）
    C  内容 1333x750 → 补零 1344x768  （= v9，出问题的那组）
"""

import os
import sys
import types

import numpy as np
import torch
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.modules.setdefault("torchac", types.ModuleType("torchac"))
import s0_export_onnx as s0                                     # noqa: E402
import v1_make_visuals as v1                                    # noqa: E402
import v6_rm_rec_probe as v6                                    # noqa: E402
from models.rec.rec import build_rec_model                      # noqa: E402

FRAME = os.path.join(PROJ, "hq_optimize/captures/noise_diag/hq_final_1.png")
OUT = os.path.join(PROJ, "DTM", "out_v1", "size_probe")
CASES = [("A_960x544_pad576", (544, 960), (576, 960)),
         ("B_768x1344_nopad", (768, 1344), (768, 1344)),
         ("C_750x1333_pad768x1344", (750, 1333), (768, 1344))]


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    fe, fc = v1.load_chain()
    rec = build_rec_model()
    rec.load_state_dict(torch.load(os.path.join(s0.ROOT, "packages/Model/REC_model/model_rec.pt"),
                                   map_location=s0.DEV))
    rec.eval()
    rec.requires_grad_(False)
    im0 = Image.open(FRAME).convert("RGB")

    for tag, content, padded in CASES:
        CH, CW = content
        PH, PW = padded
        im = im0.resize((CW, CH))
        a = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0
        if (PH, PW) != (CH, CW):
            a = np.pad(a, ((0, 0), (0, PH - CH), (0, PW - CW)))
        x = torch.from_numpy(a).unsqueeze(0)
        qm = torch.full((1, 1, PH // 16, PW // 16), 0.870, dtype=torch.float32)   # 第 31 档
        with torch.no_grad():
            f0 = fe(x)
            out = fc.compress(f0, qm, rate_control_flag=False,
                              after_padding_H=PH, after_padding_W=PW)
            feat = fc.decompress(out["strings"], out["shape"], qm)["x_hat"]
            pix = rec(feat)
        n = len(out["strings"][0][0]) + len(out["strings"][1][0])
        a0 = f0.reshape(-1).numpy()
        a1 = feat.reshape(-1).numpy()
        corr = float(np.corrcoef(a0, a1)[0, 1])
        p = np.clip(pix[0, :, :CH, :CW].permute(1, 2, 0).numpy(), 0, 255).astype(np.uint8)
        v6.write_png(os.path.join(OUT, f"{tag}.png"), p[:, :, ::-1].tobytes(), CW, CH)
        print(f"{tag:26s} 输入{CH}x{CW}→{PH}x{PW}  码流={n:6d} B  {n * 8 / (CW * CH):.5f} bpp  "
              f"f0={tuple(f0.shape)} x_hat={tuple(feat.shape)}  corr={corr:.4f}  "
              f"REC均值={p.reshape(-1, 3).mean(0).round(1)}")
    print("V10_DONE", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
