#!/usr/bin/env python3
"""V9：按**特征域任务推理的真实口径**重建 Step 1 的资产。

口径（两侧必须一致，否则框坐标全错）：
  · 编解码器：`--image_resize_xmin 800` / `--image_resize_xmax 1333` / `padding_factor 64`（tools/main.py L139-146, L461）
  · 检测器：config 默认 `INPUT.MIN_SIZE_TEST 800` / `MAX_SIZE_TEST 1333`（inference_task.py 未覆盖）⇒ 天然对齐
  ⇒ 我们的帧 960×540 → 短边缩到 800（PIL resize，与 RM 同一写法）→ 1422×800 → 补零到 64 的倍数（1472×832）

产出（`DTM/out_v1/taskframe/`）：
    q03/ · q31/
        res2_hq_final_1.pt     解码特征 (1,128,208,368) —— 喂 147 上的任务网（=- 09_g_s 输出）
        rec_q03.png            低码率重建图 1422×800（画框用）
        img_1422x800.png       同尺寸的（增强）原图 —— 供 detectron2 取 image_sizes
    report.json

用法：~/anaconda3/envs/dtm/bin/python DTM/code/v9_dump_taskframe_assets.py
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
from models.rec.rec import build_rec_model                      # noqa: E402

XMIN, XMAX, PADF = 800, 1333, 64
Q = {3: 0.265, 31: 0.870}
IMG_ID = "hq_final_1"
FRAME = os.path.join(PROJ, "hq_optimize/captures/noise_diag/hq_final_1.png")
OUT = os.path.join(PROJ, "DTM", "out_v1", "taskframe")


def resize_like_rm(im: Image.Image):
    """与 tools/main.py preprocess_image_data 同一写法：短边到 xmin，长边不超 xmax。"""
    h, w = im.height, im.width
    scale = XMIN * 1.0 / min(h, w)
    newh, neww = (XMIN, scale * w) if h < w else (scale * h, XMIN)
    if max(newh, neww) > XMAX:
        sc = XMAX * 1.0 / max(newh, neww)
        newh, neww = newh * sc, neww * sc
    return im.resize((int(neww + 0.5), int(newh + 0.5)))        # PIL 默认重采样，与 RM 相同


def pad_to(hw, f=PADF):
    h, w = hw
    ph = f * ((h // f) + 1) - h if h % f else 0               # 与 RM L397-398 同式
    pw = f * ((w // f) + 1) - w if w % f else 0
    return ph, pw


def main() -> int:
    im0 = Image.open(FRAME).convert("RGB")
    im = resize_like_rm(im0)
    W, H = im.width, im.height
    ph, pw = pad_to((H, W))
    PAD_H, PAD_W = H + ph, W + pw
    print(f"帧 {im0.width}x{im0.height} → 短边 800 → {W}x{H} → 补零到 {PAD_W}x{PAD_H}"
          f"（补 {pw} 列 / {ph} 行）")

    os.makedirs(OUT, exist_ok=True)
    im.save(os.path.join(OUT, f"img_{W}x{H}.png"))

    fe, fc = v1.load_chain()
    rec = build_rec_model()
    rec.load_state_dict(torch.load(os.path.join(s0.ROOT, "packages/Model/REC_model/model_rec.pt"),
                                   map_location=s0.DEV))
    rec.eval()
    rec.requires_grad_(False)

    a = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0     # BGR/255
    a = np.pad(a, ((0, 0), (0, ph), (0, pw)))
    x = torch.from_numpy(a).unsqueeze(0)
    rep = {"frame": IMG_ID, "src": [im0.width, im0.height], "task_size": [W, H],
           "padded": [PAD_W, PAD_H], "pad": [pw, ph], "rows": []}

    for q, f in Q.items():
        d = os.path.join(OUT, f"q{q:02d}")
        os.makedirs(d, exist_ok=True)
        qm = torch.full((1, 1, PAD_H // 16, PAD_W // 16), f, dtype=torch.float32)
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
        rep["rows"].append({"q": q, "q_factor": f, "y_bytes": len(out["strings"][0][0]),
                            "z_bytes": len(out["strings"][1][0]), "total_bytes": n,
                            "bpp": n * 8 / (W * H), "feat_shape": list(feat.shape)})
        print(f"q={q:2d}  码流={n} B  {n * 8 / (W * H):.5f} bpp  特征 {tuple(feat.shape)}"
              f"  值域=[{float(feat.min()):.3f},{float(feat.max()):.3f}]  → {d}")

    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=1)
    print("V9_DONE", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
