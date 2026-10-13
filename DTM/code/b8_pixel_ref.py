#!/usr/bin/env python3
"""B8（147 上跑）：**像素域参考**——同一教师、同一批 COCO val、同一几何、**不经 DCM**，出框写 index.jsonl。

用途：给 b7 提供"无压缩上限"的对照，从而量化 **DCM 特征域到底付出了多少精度**。
口径：
- 图像 resize/pad **与 b1 完全同式**（`resize_like_rm` + `pad_to(64)`）⇒ 唯一变量 = 走不走 DCM
- 教师 = 标准 Faster R-CNN X-101-32x8d-FPN 3x **直接吃像素**（detectron2 原生 forward）
- `_postprocess` 的 image_size 取 **(PAD_H, PAD_W)**，与 b1 修正后的口径一致（框落在同一坐标系）
- 框按 `KEEP_SCORE = 0.30` 过滤，与 b1 一致 ⇒ 与 b7 可逐项对比
- 图像顺序与 b1 **完全一致**（sorted glob + random.Random(0).shuffle + 取前 500）⇒ 同一批图

性质：**只读、离机、非验收依据**；不改 config、不碰板端。
用法：python b8_pixel_ref.py [out_dir]
"""

import glob
import json
import os
import random
import sys
import types

import numpy as np

sys.modules.setdefault("torchac", types.ModuleType("torchac"))
import torch                                                        # noqa: E402
from PIL import Image                                               # noqa: E402

DTM = "/home/jiangfeng/jf_app/DTM-V-1.5"
STEP1 = "/home/jiangfeng/jf_app/dcm_step1"
COCO = "/home/jiangfeng/datasets/coco2017"
sys.path.insert(0, DTM)
sys.path.insert(0, os.path.join(STEP1, "libs"))
sys.path.insert(0, STEP1)
os.chdir(DTM)

from detectron2.config import get_cfg                                 # noqa: E402
from detectron2.modeling import build_model                           # noqa: E402
from detectron2.checkpoint import DetectionCheckpointer               # noqa: E402

XMIN, XMAX, PADF = 800, 1333, 64
KEEP_SCORE = 0.30


def resize_like_rm(im):
    h, w = im.height, im.width
    scale = XMIN * 1.0 / min(h, w)
    newh, neww = (XMIN, scale * w) if h < w else (scale * h, XMIN)
    if max(newh, neww) > XMAX:
        sc = XMAX * 1.0 / max(newh, neww)
        newh, neww = newh * sc, neww * sc
    return im.resize((int(neww + 0.5), int(newh + 0.5)))


def pad_to(hw, f=PADF):
    h, w = hw
    ph = f * ((h // f) + 1) - h if h % f else 0
    pw = f * ((w // f) + 1) - w if w % f else 0
    return ph, pw


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = args[0] if args else "/home/jiangfeng/jf_app/dcm_b/pre_pixel"
    keep = KEEP_SCORE
    for a in sys.argv[1:]:
        if a.startswith("--keep-score="):
            keep = float(a.split("=", 1)[1])
    cfg = get_cfg()
    cfg.merge_from_file(os.path.join(STEP1, "src/detectron2/configs/COCO-Detection/"
                                            "faster_rcnn_X_101_32x8d_FPN_3x.yaml"))
    cfg.MODEL.WEIGHTS = os.path.join(DTM, "packages/Model/Detector_model/model_final_68b088.pkl")
    cfg.MODEL.DEVICE = "cuda"
    cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = 0.05
    model = build_model(cfg)
    DetectionCheckpointer(model).load(cfg.MODEL.WEIGHTS)
    model.eval()
    print("teacher(pixel) loaded", flush=True)

    files = sorted(glob.glob(os.path.join(COCO, "val2017", "*.jpg")))
    random.Random(0).shuffle(files)          # 与 b1 同序
    files = files[:500]
    os.makedirs(out, exist_ok=True)
    print("样本 =", len(files), flush=True)

    with open(os.path.join(out, "index.jsonl"), "w", encoding="utf-8") as fh, torch.no_grad():
        for k, p in enumerate(files):
            im = resize_like_rm(Image.open(p).convert("RGB"))
            W, H = im.width, im.height
            ph, pw = pad_to((H, W))
            PAD_H, PAD_W = H + ph, W + pw
            # **0~255 且 BGR**：detectron2 的像素管线自己减 PIXEL_MEAN=(103.53,116.28,123.675)，
            # 那是 **BGR** 序的 COCO 均值；若除以 255 则输入近乎全黑 ⇒ 检测器零输出（实测）。
            a = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1)
            a = np.pad(a, ((0, 0), (0, ph), (0, pw)))
            x = torch.from_numpy(a).unsqueeze(0).cuda()
            o = model([{"image": x[0], "height": PAD_H, "width": PAD_W}])[0]["instances"]
            bx = o.pred_boxes.tensor.cpu().numpy().astype(np.float32)
            sc = o.scores.cpu().numpy().astype(np.float32)
            cl = o.pred_classes.cpu().numpy().astype(np.int16)
            m = sc >= keep
            fh.write(json.dumps({"idx": k, "file": "", "tag": "coco", "src": p, "q": -1,
                                 "bytes": 0, "W": W, "H": H, "PAD_W": PAD_W, "PAD_H": PAD_H,
                                 "boxes": bx[m].tolist(), "scores": sc[m].tolist(),
                                 "labels": cl[m].tolist()}, ensure_ascii=False) + "\n")
            if k % 50 == 0 or k == len(files) - 1:
                print(f"[{k + 1}/{len(files)}] 框={int(m.sum())}", flush=True)
    print("B8_DONE", out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
