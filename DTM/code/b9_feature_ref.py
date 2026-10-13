#!/usr/bin/env python3
"""B9（147 上跑）：**无损特征参考** —— 教师吃 `f0 = fe(x)`（**量化前**的 DCM 潜在特征，不经编解码）。

用途：把"q31 相对像素域损失 34%"**拆成两段**：
- 像素域（b8）      → `f0`  ：**DCM 特征表示 + adapter/FPN 路径**的代价
- `f0`              → `x_hat`@q31 ：**量化 + 熵编码**的代价（= 码率真正买到的东西）

口径与 b1 **完全一致**（同 500 图、同 resize/pad、同 `_postprocess(PAD_H,PAD_W)`、同 `>0.30` 过滤），
唯一差别 = **用 `f0` 替掉 `decompress(...)["x_hat"]`**。
性质：只读、离机、非验收依据。
用法：python b9_feature_ref.py [out_dir]
"""

import glob
import json
import os
import random
import sys
import types

import numpy as np

sys.modules.setdefault("torchac", types.ModuleType("torchac"))
import compressai                                                    # noqa: F401,E402
import torch                                                        # noqa: E402
from PIL import Image                                               # noqa: E402

DTM = "/home/jiangfeng/jf_app/DTM-V-1.5"
STEP1 = "/home/jiangfeng/jf_app/dcm_step1"
LIBS = os.path.join(STEP1, "libs")
RM_SCRIPTS = os.path.join(DTM, "scripts/DCM_Datasets_Feature_Inference_Scripts")
COCO = "/home/jiangfeng/datasets/coco2017"
sys.path.insert(0, DTM)
sys.path.insert(0, LIBS)
sys.path.insert(0, RM_SCRIPTS)
sys.path.insert(0, STEP1)
os.chdir(DTM)

from models.MIK_MCM_codecs import (                                  # noqa: E402
    build_feature_extractor, build_feature_coding_Mean_MCM_Scale)

XMIN, XMAX, PADF = 800, 1333, 64
KEEP_SCORE = 0.30


def resize_like_rm(im):
    h, w = im.height, im.width
    s = XMIN * 1.0 / min(h, w)
    nh, nw = (XMIN, s * w) if h < w else (s * h, XMIN)
    if max(nh, nw) > XMAX:
        k = XMAX * 1.0 / max(nh, nw)
        nh, nw = nh * k, nw * k
    return im.resize((int(nw + 0.5), int(nh + 0.5)))


def pad_to(hw, f=PADF):
    h, w = hw
    ph = f * ((h // f) + 1) - h if h % f else 0
    pw = f * ((w // f) + 1) - w if w % f else 0
    return ph, pw


def load_chain(dev):
    fe = build_feature_extractor()
    fc = build_feature_coding_Mean_MCM_Scale(quality=3, parallel_computing=True)
    ck = torch.load(os.path.join(DTM, "packages/Model/MIKcodec_MCM_model/"
                                      "dtm-V_entropy_vm_1_270000.pth"), map_location="cpu")
    fe.load_state_dict(ck["feature_extractor"])
    fc.gaussian_conditional.scale_table = fc.gaussian_conditional.scale_table.to(torch.int32)
    fc.load_state_dict(ck["feature_coding"])
    fc._update_per_channel_shifts(ck["per_channel_shifts"]["feature_coding"])
    fe.eval().to(dev)
    fc.eval().to(dev)
    from functools import partial
    fc.context.quantize_func = partial(fc.quantize)
    fe.requires_grad_(False)
    fc.requires_grad_(False)
    return fe, fc


def load_teacher(dev):
    from detectron2.config import get_cfg
    from detectron2.modeling import build_model
    from detectron2.checkpoint import DetectionCheckpointer
    from detectron2.structures import ImageList
    from Adapter import build_feature_adapter
    cfg = get_cfg()
    cfg.merge_from_file(os.path.join(STEP1, "src/detectron2/configs/COCO-Detection/"
                                            "faster_rcnn_X_101_32x8d_FPN_3x.yaml"))
    cfg.MODEL.WEIGHTS = os.path.join(DTM, "packages/Model/Detector_model/model_final_68b088.pkl")
    cfg.MODEL.DEVICE = str(dev)
    cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = 0.05
    model = build_model(cfg)
    DetectionCheckpointer(model).load(cfg.MODEL.WEIGHTS)
    model.eval().to(dev)
    adapter = build_feature_adapter(128, 256)
    ck = torch.load(os.path.join(DTM, "packages/Model/MIKcodec_MCM_model/"
                                      "dtm-V_entropy_vm_1_270000.pth"), map_location=dev)
    adapter.load_state_dict(ck["feature_adapter"])
    adapter.eval().to(dev)
    return model, adapter, ImageList


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "/home/jiangfeng/jf_app/dcm_b/pre_feat0"
    dev = torch.device("cuda")
    os.makedirs(out, exist_ok=True)
    fe, _ = load_chain(dev)
    model, adapter, ImageList = load_teacher(dev)
    print("chain + teacher ready", flush=True)

    files = sorted(glob.glob(os.path.join(COCO, "val2017", "*.jpg")))
    random.Random(0).shuffle(files)
    files = files[:500]
    print("样本 =", len(files), flush=True)

    with open(os.path.join(out, "index.jsonl"), "w", encoding="utf-8") as fh, torch.no_grad():
        for k, p in enumerate(files):
            im = resize_like_rm(Image.open(p).convert("RGB"))
            W, H = im.width, im.height
            ph, pw = pad_to((H, W))
            PAD_H, PAD_W = H + ph, W + pw
            a = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0
            a = np.pad(a, ((0, 0), (0, ph), (0, pw)))
            x = torch.from_numpy(a).unsqueeze(0).to(dev)
            with torch.no_grad():
                f0 = fe(x)                                   # ← 量化前特征（无损参照）
                imgs = [(x[0] - model.pixel_mean) / model.pixel_std]
                imgs = ImageList.from_tensors(imgs, model.backbone.size_divisibility)
                a2 = adapter(f0)
                r3 = model.backbone.bottom_up.res3(a2)
                r4 = model.backbone.bottom_up.res4(r3)
                r5 = model.backbone.bottom_up.res5(r4)
                import s1_feature_domain_det as s1
                fpn = s1.my_fpn_forward({"res2": a2, "res3": r3, "res4": r4, "res5": r5}, model)
                props, _ = model.proposal_generator(imgs, fpn)
                det, _ = model.roi_heads(imgs, fpn, props, None)
                inst = model._postprocess(det, [dict(image=x[0])], [(PAD_H, PAD_W)])[0]["instances"]
            bx = inst.pred_boxes.tensor.detach().cpu().numpy().astype(np.float32)
            sc = inst.scores.detach().cpu().numpy().astype(np.float32)
            cl = inst.pred_classes.detach().cpu().numpy().astype(np.int16)
            m = sc >= KEEP_SCORE
            fh.write(json.dumps({"idx": k, "file": "", "tag": "coco", "src": p, "q": -1,
                                 "bytes": 0, "W": W, "H": H, "PAD_W": PAD_W, "PAD_H": PAD_H,
                                 "boxes": bx[m].tolist(), "scores": sc[m].tolist(),
                                 "labels": cl[m].tolist()}, ensure_ascii=False) + "\n")
            if k % 50 == 0 or k == len(files) - 1:
                print(f"[{k + 1}/{len(files)}] 框={int(m.sum())}", flush=True)
    print("B9_DONE", out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
