#!/usr/bin/env python3
"""V11：码率↔检出 扫描（开发机侧）——按 q 档批量导出**任务尺寸**的解码特征 + 低码率重建图。

口径与 v9 完全一致（RM：短边 800 / 长边<=1333 / 补零到 64 倍数），只把 q 从两档扩成多档。
产出 DTM/out_v1/sweep/：
    q<NN>/res2_hq_final_1.pt    解码特征（喂 147 的任务网）
    q<NN>/rec_q<NN>.png         低码率重建图（画框底图）
    report.json                 每档 y/z 字节数 / bpp / 特征形状
用法：~/anaconda3/envs/dtm/bin/python DTM/code/v11_q_sweep_dump.py
"""

import json
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
import v9_dump_taskframe_assets as v9                           # noqa: E402
from models.rec.rec import build_rec_model                      # noqa: E402

QLIST = [0, 3, 7, 11, 15, 19, 23, 27, 31]
QFACTOR = {0: 0.200, 3: 0.265, 7: 0.351, 11: 0.438, 15: 0.524,
           19: 0.611, 23: 0.697, 27: 0.784, 31: 0.870}
IMG_ID = "hq_final_1"
OUT = os.path.join(PROJ, "DTM", "out_v1", "sweep")


def main() -> int:
    im0 = Image.open(v9.FRAME).convert("RGB")
    im = v9.resize_like_rm(im0)
    W, H = im.width, im.height
    ph, pw = v9.pad_to((H, W))
    PAD_H, PAD_W = H + ph, W + pw
    print(f"帧 {im0.width}x{im0.height} → {W}x{H} → 补零到 {PAD_W}x{PAD_H}（补 {pw} 列 / {ph} 行）")

    os.makedirs(OUT, exist_ok=True)
    fe, fc = v1.load_chain()
    rec = build_rec_model()
    rec.load_state_dict(torch.load(os.path.join(s0.ROOT, "packages/Model/REC_model/model_rec.pt"),
                                   map_location=s0.DEV))
    rec.eval()
    rec.requires_grad_(False)

    a = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0
    a = np.pad(a, ((0, 0), (0, ph), (0, pw)))
    x = torch.from_numpy(a).unsqueeze(0)
    rep = {"frame": IMG_ID, "task_size": [W, H], "padded": [PAD_W, PAD_H], "rows": []}

    for q in QLIST:
        d = os.path.join(OUT, f"q{q:02d}")
        os.makedirs(d, exist_ok=True)
        qm = torch.full((1, 1, PAD_H // 16, PAD_W // 16), QFACTOR[q], dtype=torch.float32)
        with torch.no_grad():
            f0 = fe(x)
            out = fc.compress(f0, qm, rate_control_flag=False,
                              after_padding_H=PAD_H, after_padding_W=PAD_W)
            feat = fc.decompress(out["strings"], out["shape"], qm)["x_hat"]
            pix = rec(feat)
        n = len(out["strings"][0][0]) + len(out["strings"][1][0])
        torch.save(feat.cpu(), os.path.join(d, f"res2_{IMG_ID}.pt"))
        p = np.clip(pix[0, :, :H, :W].permute(1, 2, 0).numpy(), 0, 255).astype(np.uint8)
        v6.write_png(os.path.join(d, f"rec_q{q:02d}.png"), p[:, :, ::-1].tobytes(), W, H)
        rep["rows"].append({"q": q, "q_factor": QFACTOR[q],
                            "y_bytes": len(out["strings"][0][0]),
                            "z_bytes": len(out["strings"][1][0]), "total_bytes": n,
                            "bpp": n * 8 / (W * H), "feat_shape": list(feat.shape)})
        print(f"q={q:2d} f={QFACTOR[q]:.3f}  码流={n:6d} B  {n * 8 / (W * H):.5f} bpp"
              f"  特征 {tuple(feat.shape)} → {d}")

    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=1)
    print("V11_DONE", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
