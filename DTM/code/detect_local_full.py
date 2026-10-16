#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机全链检测适配层（方案 1）：DTM-V-1.5 codec（GPU）+ x101 ONNX（CPU）+ 板上原样 numpy。

**链路**（与部署链 `dcm_demo.py` 一致，仅后端不同）：
  HQ PNG → resize_like_rm（短边800/长边≤1333）→ pad 到 64 倍数 → fe → fc.compress
  → fc.decompress → res2_hat → backbone+FPN(ONNX) → RPN(ONNX) → anchor+NMS(板上 numpy)
  → RoIAlign(板上 numpy) → boxhead(ONNX) → fast_rcnn_decode(板上原样逻辑)

**与板端的差异（如实登记，写进输出 json）**：
  1. codec = GPU PyTorch FP32 vs 板端 RKNN FP16 + MCM numpy FP32（07e/07d）
     ⇒ res2 数值有差异（板上 vs 147-FP32 实测 corr 0.9978、max|Δ|0.236，§8.23）；
  2. 检测三网 = onnxruntime CPU FP32 vs 板端 RKNN FP16；
  3. 预处理 pad = **64 倍数**（DTM-V-1.5 口径，板端同）而非 32。

**用途** = 给 137 张 HQ 输出打伪标签。**伪标签非验收依据**（规则 10-evidence.md §1）。

用法：
  python3 detect_local_full.py --input <png|dir> --out <json> [--q 31] [--score 0.05]
                               [--save-res2-dir <dir>] [--limit N]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import types
from functools import partial

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))                    # 仓库根
DTM = os.path.join(REPO, "DTM", "DTM-V-1.5")

# ⚠️ 路径顺序关键：DTM 的 src/models/packages-Environment 必须在 HERE 之前，
#    否则 `models/__init__.py`（from models import utils）解析不到 src 下的依赖。
#    且**绝对路径**（chdir 后相对路径失效）。HERE 保留在末位以供 import rpn_np/roialign_np。
for p in (os.path.join(DTM, "packages", "Environment"), os.path.join(DTM, "models"),
          os.path.join(DTM, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)
if HERE in sys.path:
    sys.path.remove(HERE)
sys.path.append(HERE)
os.chdir(DTM)                     # DTM 内部用相对路径

sys.modules.setdefault("torchac", types.ModuleType("torchac"))
import cv2  # noqa: E402
import torch  # noqa: E402
import onnxruntime as ort  # noqa: E402
from PIL import Image  # noqa: E402

from models.MIK_MCM_codecs import (  # noqa: E402
    build_feature_extractor, build_feature_coding_Mean_MCM_Scale)

import roialign_np  # noqa: E402  板上原样
import rpn_np       # noqa: E402  板上原样

XMIN, XMAX, PADF = 800, 1333, 64
QFACTOR = {0: 0.200, 3: 0.265, 7: 0.351, 11: 0.438, 15: 0.524,
           19: 0.611, 23: 0.697, 27: 0.784, 31: 0.870}

# ── 检测常量：逐字对齐板端 board_detect.py ──────────────────────────
SCALE_CLAMP = float(np.log(1000.0 / 16))
BBOX_REG_WEIGHTS = (10.0, 10.0, 5.0, 5.0)
SCORE_THRESH = 0.05
NMS_THRESH = 0.5
TOPK = 100
NUM_CLASSES = 80
RPN_CFG = {
    "sizes": [[32], [64], [128], [256], [512]],
    "aspect_ratios": [[0.5, 1.0, 2.0]],
    "strides": [4, 8, 16, 32, 64],
    "offset": 0.0, "pre_nms_topk": 1000, "post_nms_topk": 1000,
    "nms_thresh": 0.7, "min_box_size": 0,
    "bbox_reg_weights": (1.0, 1.0, 1.0, 1.0),
}
EXPECT_P = [(1, 256, 192, 336), (1, 256, 96, 168), (1, 256, 48, 84),
            (1, 256, 24, 42), (1, 256, 12, 21)]

COCO_CLASSES = ["person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
                "truck", "boat", "traffic light", "fire hydrant", "stop sign",
                "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
                "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
                "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
                "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
                "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
                "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
                "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
                "couch", "potted plant", "bed", "dining table", "toilet", "tv",
                "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
                "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
                "scissors", "teddy bear", "hair drier", "toothbrush"]


def softmax(x):
    x = x.astype(np.float32)
    m = x.max(axis=-1, keepdims=True)
    e = np.exp(x - m)
    return (e / e.sum(axis=-1, keepdims=True)).astype(np.float32)


def apply_deltas_classwise(deltas, boxes):
    deltas = np.asarray(deltas, dtype=np.float32)
    boxes = np.asarray(boxes, dtype=np.float32)
    N = deltas.shape[0]
    widths = boxes[:, 2] - boxes[:, 0]
    heights = boxes[:, 3] - boxes[:, 1]
    ctr_x = boxes[:, 0] + 0.5 * widths
    ctr_y = boxes[:, 1] + 0.5 * heights
    wx, wy, ww, wh = BBOX_REG_WEIGHTS
    d = deltas.reshape(N, NUM_CLASSES, 4)
    dx, dy = d[:, :, 0] / wx, d[:, :, 1] / wy
    dw = np.minimum(d[:, :, 2] / ww, np.float32(SCALE_CLAMP))
    dh = np.minimum(d[:, :, 3] / wh, np.float32(SCALE_CLAMP))
    pcx = dx * widths[:, None] + ctr_x[:, None]
    pcy = dy * heights[:, None] + ctr_y[:, None]
    pw = np.exp(dw) * widths[:, None]
    ph = np.exp(dh) * heights[:, None]
    return np.stack((pcx - 0.5 * pw, pcy - 0.5 * ph,
                     pcx + 0.5 * pw, pcy + 0.5 * ph), axis=-1).astype(np.float32)


def fast_rcnn_decode(cls_logits, box_deltas, proposals, image_hw, K):
    N = cls_logits.shape[0]
    H, W = int(image_hw[0]), int(image_hw[1])
    probs = softmax(cls_logits)
    scores = probs[:, :NUM_CLASSES]
    pred_boxes = apply_deltas_classwise(box_deltas, proposals)
    finite = np.isfinite(pred_boxes).all(axis=(1, 2)) & np.isfinite(scores).all(axis=1)
    pred_boxes[:, :, 0] = np.clip(pred_boxes[:, :, 0], 0, W)
    pred_boxes[:, :, 1] = np.clip(pred_boxes[:, :, 1], 0, H)
    pred_boxes[:, :, 2] = np.clip(pred_boxes[:, :, 2], 0, W)
    pred_boxes[:, :, 3] = np.clip(pred_boxes[:, :, 3], 0, H)
    filter_mask = scores > SCORE_THRESH
    filter_mask &= finite[:, None]
    if K < N:
        filter_mask[K:, :] = False
    if not filter_mask.any():
        return []
    fi = np.argwhere(filter_mask)
    boxes_sel = pred_boxes[filter_mask]
    scores_sel = scores[filter_mask]
    classes_sel = fi[:, 1]
    max_coord = float(boxes_sel.max())
    offsets = classes_sel.astype(np.float32) * (np.float32(max_coord) + np.float32(1.0))
    keep = rpn_np.nms(boxes_sel + offsets[:, None], scores_sel, NMS_THRESH)[:TOPK]
    return [{"cls": int(classes_sel[k]), "score": float(scores_sel[k]),
             "xyxy": [float(v) for v in boxes_sel[k]]} for k in keep]


# ── codec（b1_precompute.py 口径逐字复用）──────────────────────────
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
    return (f * ((h // f) + 1) - h if h % f else 0,
            f * ((w // f) + 1) - w if w % f else 0)


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
    fc.context.quantize_func = partial(fc.quantize)
    fe.requires_grad_(False)
    fc.requires_grad_(False)
    return fe, fc


def enc_dec_res2(fe, fc, img_bgr, q, dev):
    """HQ BGR 图 → q31 编解码往返 → res2_hat float32 (1,128,H,W)，返回 (res2, newh, neww, scale)。"""
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    im = Image.fromarray(rgb)
    ow, oh = im.size
    im = resize_like_rm(im)
    nw, nh = im.size
    scale = nw / ow
    ph, pw = pad_to((nh, nw))
    x = torch.from_numpy(np.asarray(im, dtype=np.uint8)[:, :, ::-1].copy())  # BGR
    x = x.permute(2, 0, 1)[None].float() / 255.0
    if ph or pw:
        x = torch.nn.functional.pad(x, (0, pw, 0, ph))
    x = x.to(dev)
    with torch.no_grad():
        f0 = fe(x)
        out = fc.compress(f0, q_map_factor=QFACTOR[q])
        rec = fc.decompress(out["strings"], out["shape"], q_map_factor=QFACTOR[q])
    res2 = rec["x_hat"].float().cpu().numpy()
    return res2, nh, nw, scale


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--q", type=int, default=31)
    ap.add_argument("--score", type=float, default=0.05)
    ap.add_argument("--save-res2-dir", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if os.path.isdir(args.input):
        files = sorted(glob.glob(os.path.join(args.input, "*.png")))
    else:
        files = [args.input]
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"!! {args.input} 无 png", file=sys.stderr)
        return 2

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"加载 codec（{dev}）...", flush=True)
    fe, fc = load_chain(dev)
    so = ort.SessionOptions()
    so.intra_op_num_threads = os.cpu_count()
    X101 = os.path.join(REPO, "DTM", "assets", "x101")
    print("加载 ONNX（CPU）...", flush=True)
    s_bb = ort.InferenceSession(os.path.join(X101, "x101_backbone_fpn.onnx"), so,
                                providers=["CPUExecutionProvider"])
    s_rpn = ort.InferenceSession(os.path.join(X101, "rpn_head.onnx"), so,
                                 providers=["CPUExecutionProvider"])
    s_bh = ort.InferenceSession(os.path.join(X101, "boxhead.onnx"), so,
                                providers=["CPUExecutionProvider"])
    bb_in = s_bb.get_inputs()[0].name
    rpn_names = [i.name for i in s_rpn.get_inputs()]
    bh_in = s_bh.get_inputs()[0].name
    if args.save_res2_dir:
        os.makedirs(args.save_res2_dir, exist_ok=True)

    global SCORE_THRESH
    if args.score != SCORE_THRESH:
        SCORE_THRESH = args.score

    results = {}
    t_all = time.time()
    for i, f in enumerate(files):
        stem = os.path.splitext(os.path.basename(f))[0]
        t1 = time.time()
        img = cv2.imread(f)
        if img is None:
            results[stem] = {"error": "imread_failed"}
            continue
        res2, newh, neww, scale = enc_dec_res2(fe, fc, img, args.q, dev)
        if args.save_res2_dir:
            np.save(os.path.join(args.save_res2_dir, f"{stem}_res2.npy"), res2)
        p = [np.asarray(v, dtype=np.float32) for v in s_bb.run(None, {bb_in: res2})]
        for j, s in enumerate(EXPECT_P):
            if p[j].shape != s:
                raise RuntimeError(f"{stem}: p{j+2} {p[j].shape} != {s}")
        rpn_out = s_rpn.run(None, {n: p[j].copy() for j, n in enumerate(rpn_names)})
        obj_list = [np.asarray(v, np.float32) for v in rpn_out[0:5]]
        dlt_list = [np.asarray(v, np.float32) for v in rpn_out[5:10]]
        prop_boxes, _ = rpn_np.rpn_proposals(obj_list, dlt_list, (newh, neww), RPN_CFG)
        K = prop_boxes.shape[0]
        if K < 1000:
            prop_pad = np.concatenate([prop_boxes, np.zeros((1000 - K, 4), np.float32)], 0)
        else:
            prop_pad = prop_boxes[:1000].copy()
            K = 1000
        pooled = roialign_np.roi_align(p[0:4], prop_pad, output_size=(7, 7),
                                       scales=(0.25, 0.125, 0.0625, 0.03125),
                                       sampling_ratio=0, canonical_box_size=224,
                                       canonical_level=4, aligned=True)
        bh = s_bh.run(None, {bh_in: np.ascontiguousarray(pooled, dtype=np.float32)})
        dets = fast_rcnn_decode(np.asarray(bh[0], np.float32),
                                np.asarray(bh[1], np.float32),
                                prop_pad, (newh, neww), K)
        for d in dets:
            d["xyxy_orig"] = [v / scale for v in d["xyxy"]]
            d["name"] = COCO_CLASSES[d["cls"]]
        results[stem] = {"K": int(K), "ndet": len(dets), "dets": dets,
                         "newh": int(newh), "neww": int(neww), "scale": float(scale),
                         "img_hw": [int(img.shape[0]), int(img.shape[1])]}
        print(f"[{i+1}/{len(files)}] {stem} K={K} ndet={len(dets)} "
              f"({time.time()-t1:.1f}s, 总 {time.time()-t_all:.0f}s)", flush=True)

    with open(args.out, "w") as fh:
        json.dump({"n": len(results), "q": args.q, "score_thres": SCORE_THRESH,
                   "wall_sec": round(time.time() - t_all, 1),
                   "codec": "GPU PyTorch FP32（板端 = RKNN FP16 + MCM numpy FP32）",
                   "detector": "onnxruntime CPU FP32（板端 = RKNN FP16）",
                   "eff": "伪标签，非验收依据（规则 10-evidence.md §1）",
                   "results": results}, fh, ensure_ascii=False, indent=1)
    print(f"\ndone {len(results)} 张 → {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
