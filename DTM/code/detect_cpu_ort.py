#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机 CPU 检测（方案 B 混合架构的 onnxruntime 移植）：x101_backbone_fpn / rpn_head / boxhead
三个 ONNX 走 onnxruntime CPU，anchor+NMS / RoIAlign / 最终解码走**板上原样**的 numpy 实现。

**用途** = 给 137 张 HQ 输出打伪标签（供 labelme 人工修正）。选本机而非板上的理由：
本机不占板上 NPU / DRM（`starlight-demo` 服务不受影响），且端到端可调试。

**口径（逐字对齐板端 `board_detect.py`，未重写）**：
  · numpy 逻辑（rpn_np / roialign_np / fast_rcnn_decode / apply_deltas_classwise）从
    板上 `/home/cat/dcm_x101/e2e/` **原样拷贝**，不重新实现 ⇒ 解码行为与部署链一致；
  · 预处理 = letterbox 到 1344×768（检测器所需 shape），PAD 用 0；
  · anchor 生成 / decode / NMS / RoIAlign 参数全部来自 RPN_CFG 与 roialign 默认值。

**⚠️ 与板端的已知差异（如实登记）**：
  1. 推理后端 = onnxruntime CPU FP32 vs 板端 RKNN FP16 ⇒ 数值有 FP16 舍入级差异
     （板上 vs GPU 实测 mean|Δscore| 0.15，本机应更接近 GPU）；
  2. 输入 = **原图**（letterbox 后）经 backbone，而板端吃的是 **DCM 解码特征 res2**
     ⇒ 本机链路**不经 DCM 编解码**。对伪标签而言这是**有利**的：伪标签来自更干净的
     特征，人工修正时不易被编解码噪声误导；但与部署链的输入不同源，需在报告登记。

**纪律**：伪标签**非验收依据**（规则 10-evidence.md §1），人工修正后才入训练。

用法：
  python3 detect_cpu_ort.py --input <png 或目录> --out <json> [--score 0.05] [--limit N]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import roialign_np  # noqa: E402  板上原样
import rpn_np       # noqa: E402  板上原样

HERE = os.path.dirname(os.path.abspath(__file__))
X101 = os.path.join(os.path.dirname(os.path.dirname(HERE)), "assets", "x101")
BACKBONE_ONNX = os.path.join(X101, "x101_backbone_fpn.onnx")
RPN_ONNX = os.path.join(X101, "rpn_head.onnx")
BOXHEAD_ONNX = os.path.join(X101, "boxhead.onnx")

# ── 以下常量逐字对齐板端 board_detect.py ──────────────────────────────
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
    "offset": 0.0,
    "pre_nms_topk": 1000,
    "post_nms_topk": 1000,
    "nms_thresh": 0.7,
    "min_box_size": 0,
    "bbox_reg_weights": (1.0, 1.0, 1.0, 1.0),
}

EXPECT_P = [(1, 256, 192, 336), (1, 256, 96, 168), (1, 256, 48, 84),
            (1, 256, 24, 42), (1, 256, 12, 21)]

# detectron2 X-101-FPN_3x 预处理：短边缩到 800、长边上限 1333，PAD 到 32 的倍数
SHORT_EDGE = 800
MAX_LONG = 1333
SIZE_DIVISIBILITY = 32
# 本机输入 960×540 → 短边 800 得 800×1422 > 1333 ⇒ 改为长边受限：scale=1333/960=1.389
# 得 1333×750 → pad 到 1344×768（与板端 res2 的 1344×768 输入一致）
PIXEL_MEAN = np.array([103.530, 116.280, 123.675], dtype=np.float32)
PIXEL_STD = np.array([1.0, 1.0, 1.0], dtype=np.float32)

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
    dx = d[:, :, 0] / wx
    dy = d[:, :, 1] / wy
    dw = d[:, :, 2] / ww
    dh = d[:, :, 3] / wh
    dw = np.minimum(dw, np.float32(SCALE_CLAMP))
    dh = np.minimum(dh, np.float32(SCALE_CLAMP))
    pred_ctr_x = dx * widths[:, None] + ctr_x[:, None]
    pred_ctr_y = dy * heights[:, None] + ctr_y[:, None]
    pred_w = np.exp(dw) * widths[:, None]
    pred_h = np.exp(dh) * heights[:, None]
    x1 = pred_ctr_x - 0.5 * pred_w
    y1 = pred_ctr_y - 0.5 * pred_h
    x2 = pred_ctr_x + 0.5 * pred_w
    y2 = pred_ctr_y + 0.5 * pred_h
    return np.stack((x1, y1, x2, y2), axis=-1).astype(np.float32)


def fast_rcnn_decode(cls_logits, box_deltas, proposals, image_hw, K):
    """逐字对齐板端 fast_rcnn_decode（去掉 DROP_CLASSES —— 本机伪标签不做类屏蔽）。"""
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
    filter_inds = np.argwhere(filter_mask)
    boxes_sel = pred_boxes[filter_mask]
    scores_sel = scores[filter_mask]
    classes_sel = filter_inds[:, 1]
    max_coord = float(boxes_sel.max())
    offsets = classes_sel.astype(np.float32) * (np.float32(max_coord) + np.float32(1.0))
    boxes_for_nms = boxes_sel + offsets[:, None]
    keep = rpn_np.nms(boxes_for_nms, scores_sel, NMS_THRESH)
    keep = keep[:TOPK]
    return [{"cls": int(classes_sel[k]), "score": float(scores_sel[k]),
             "xyxy": [float(v) for v in boxes_sel[k]]} for k in keep]


def letterbox(img_bgr, short=SHORT_EDGE, maxlong=MAX_LONG, div=SIZE_DIVISIBILITY):
    """等比缩放（长边≤maxlong、短边≤short 的较大者优先保证长边）→ PAD 到 div 倍数。

    960×540：scale=1333/960=1.389 ⇒ 1333×750 ⇒ pad 到 1344×768。
    返回 (padded_rgb_float, newh, neww, scale)。
    """
    h, w = img_bgr.shape[:2]
    s = min(maxlong / max(h, w), short / min(h, w))
    newh, neww = int(round(h * s)), int(round(w * s))
    ph, pw = int(np.ceil(newh / div) * div), int(np.ceil(neww / div) * div)
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB).astype(np.float32)
    resized = cv2.resize(rgb, (neww, newh), interpolation=cv2.INTER_LINEAR)
    out = np.zeros((ph, pw, 3), dtype=np.float32)
    out[:newh, :neww] = resized
    return out, newh, neww, s


def preprocess(img_bgr):
    """→ NCHW float32（BGR→RGB + 减 detectron2 mean），返回 (tensor, newh, neww, scale)。"""
    padded, newh, neww, s = letterbox(img_bgr)
    x = padded - PIXEL_MEAN[None, None, :]
    x = np.transpose(x, (2, 0, 1))[None].astype(np.float32)
    return np.ascontiguousarray(x), newh, neww, s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="png 文件或目录")
    ap.add_argument("--out", required=True, help="输出 json 路径")
    ap.add_argument("--score", type=float, default=0.05, help="伪标签过滤阈（默认 0.05 = 板端 SCORE_THRESH）")
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

    so = ort.SessionOptions()
    so.intra_op_num_threads = os.cpu_count()
    print("加载 ONNX（CPU）...", flush=True)
    t0 = time.time()
    sess_bb = ort.InferenceSession(BACKBONE_ONNX, so, providers=["CPUExecutionProvider"])
    sess_rpn = ort.InferenceSession(RPN_ONNX, so, providers=["CPUExecutionProvider"])
    sess_bh = ort.InferenceSession(BOXHEAD_ONNX, so, providers=["CPUExecutionProvider"])
    print(f"3 模型加载 {time.time()-t0:.1f}s", flush=True)

    bb_in = sess_bb.get_inputs()[0].name
    rpn_names = [i.name for i in sess_rpn.get_inputs()]
    bh_in = sess_bh.get_inputs()[0].name

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
        x, newh, neww, scale = preprocess(img)
        p = sess_bb.run(None, {bb_in: x})
        p = [np.asarray(v, dtype=np.float32) for v in p]
        for j, s in enumerate(EXPECT_P):
            if p[j].shape != s:
                raise RuntimeError(f"{stem}: p{j+2} shape {p[j].shape} != {s}")
        # rknnlite 会就地把输入改写为 NHWC；onnxruntime 不会，但仍传副本以对齐板端口径
        rpn_out = sess_rpn.run(None, {n: p[j].copy() for j, n in enumerate(rpn_names)})
        obj_list = [np.asarray(v, dtype=np.float32) for v in rpn_out[0:5]]
        dlt_list = [np.asarray(v, dtype=np.float32) for v in rpn_out[5:10]]
        prop_boxes, prop_scores = rpn_np.rpn_proposals(obj_list, dlt_list, (newh, neww), RPN_CFG)
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
        bh = sess_bh.run(None, {bh_in: np.ascontiguousarray(pooled, dtype=np.float32)})
        cls_logits = np.asarray(bh[0], dtype=np.float32)
        box_deltas = np.asarray(bh[1], dtype=np.float32)
        dets = fast_rcnn_decode(cls_logits, box_deltas, prop_pad, (newh, neww), K)
        # 画布系 → 原图系（÷scale）
        for d in dets:
            d["xyxy_orig"] = [v / scale for v in d["xyxy"]]
            d["name"] = COCO_CLASSES[d["cls"]]
        results[stem] = {"K": int(K), "ndet": len(dets), "dets": dets,
                         "newh": newh, "neww": neww, "scale": float(scale),
                         "img_hw": [int(img.shape[0]), int(img.shape[1])]}
        print(f"[{i+1}/{len(files)}] {stem} K={K} ndet={len(dets)} "
              f"({time.time()-t1:.1f}s, 总 {time.time()-t_all:.0f}s)", flush=True)

    with open(args.out, "w") as fh:
        json.dump({"n": len(results), "score_thres": SCORE_THRESH,
                   "wall_sec": round(time.time() - t_all, 1),
                   "backend": "onnxruntime CPU FP32（板端为 RKNN FP16）",
                   "input_domain": "原图 letterbox（板端为 DCM 解码特征 res2）",
                   "eff": "伪标签，非验收依据（规则 10-evidence.md §1）",
                   "results": results}, fh, ensure_ascii=False, indent=1)
    print(f"\ndone {len(results)} 张 → {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
