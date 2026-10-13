#!/usr/bin/env python3
"""B8b（147 探针）：同一张 COCO val 图，分别以 **RGB / BGR** 喂标准检测器，打印框数与最高分。

用途：定位 b8 像素域参考"零框"的原因（detectron2 标准像素管线吃 **RGB**；而 RM 的 DCM 特征管线吃 **BGR**）。
只读、单图、秒级。
"""

import glob
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

XMIN, XMAX = 800, 1333


def resize_like_rm(im):
    h, w = im.height, im.width
    s = XMIN * 1.0 / min(h, w)
    nh, nw = (XMIN, s * w) if h < w else (s * h, XMIN)
    if max(nh, nw) > XMAX:
        k = XMAX * 1.0 / max(nh, nw)
        nh, nw = nh * k, nw * k
    return im.resize((int(nw + 0.5), int(nh + 0.5)))


def main() -> int:
    cfg = get_cfg()
    cfg.merge_from_file(os.path.join(STEP1, "src/detectron2/configs/COCO-Detection/"
                                            "faster_rcnn_X_101_32x8d_FPN_3x.yaml"))
    cfg.MODEL.WEIGHTS = os.path.join(DTM, "packages/Model/Detector_model/model_final_68b088.pkl")
    cfg.MODEL.DEVICE = "cuda"
    cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = 0.05
    m = build_model(cfg)
    DetectionCheckpointer(m).load(cfg.MODEL.WEIGHTS)
    m.eval()

    files = sorted(glob.glob(os.path.join(COCO, "val2017", "*.jpg")))
    random.Random(0).shuffle(files)
    for fi in (0, 1):
        im = resize_like_rm(Image.open(files[fi]).convert("RGB"))
        W, H = im.width, im.height
        ph = 64 * ((H // 64) + 1) - H if H % 64 else 0
        pw = 64 * ((W // 64) + 1) - W if W % 64 else 0
        PAD_H, PAD_W = H + ph, W + pw
        base255 = np.asarray(im).astype(np.float32).transpose(2, 0, 1)          # RGB, 0..255
        base255 = np.pad(base255, ((0, 0), (0, ph), (0, pw)))
        base01 = base255 / 255.0                                                # 错误示范
        print(f"--- {os.path.basename(files[fi])}  {W}x{H} -> pad {PAD_W}x{PAD_H} ---", flush=True)
        for tag, t in (("RGB 0..255", base255), ("RGB 0..1 ", base01),
                       ("BGR 0..255", base255[::-1])):
            x = torch.from_numpy(np.ascontiguousarray(t)).unsqueeze(0).cuda()
            with torch.no_grad():
                o = m([{"image": x[0], "height": PAD_H, "width": PAD_W}])[0]["instances"]
            sc = o.scores.cpu().numpy()
            print(f"  {tag}: 框数(>=0.05) = {len(sc)} | max = "
                  f"{(sc.max() if len(sc) else 0):.3f} | >=0.30 = {int((sc >= 0.30).sum())}",
                  flush=True)
    print("PROBE_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
