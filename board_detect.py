# -*- coding: utf-8 -*-
"""board_detect.py —— 方案 B 混合架构板上全链（backbone+FPN / RPN / boxhead 走 NPU，
anchor+decode+NMS / RoIAlign / 最终 fast_rcnn 解码走 CPU numpy），并与 GPU 两阶段输出逐框比对。

环境：板上 python3 + /home/cat/rknn_ab/site/232/（rknnlite 2.3.2），不 import torch/detectron2。

口径（全部从 detectron2 源码 / 配置取，未猜）：
  softmax          : FastRCNNOutputLayers.predict_probs -> F.softmax(scores, dim=-1)（对 81 类含背景）
  drop background  : fast_rcnn_inference_single_image: scores = scores[:, :-1]（丢最后一列背景）-> 80 类
  类别相关 delta   : predict_boxes = box2box.apply_deltas(deltas(N,320), proposals(N,4)) -> (N,320)
                     -> view(N,80,4)；filter 后 boxes[filter_mask] 按 (row,class) 选该类 4 个 delta 的解码框
  box transform    : Box2BoxTransform.apply_deltas，weights=(10,10,5,5)，scale_clamp=log(1000/16)
  clip             : Boxes.clip(image_shape)，image_shape = proposals.image_size = pre-pad resize (newh,neww)
  最终 NMS         : batched_nms（torchvision 坐标偏移技巧：box + class*(max_coord+1) 后单次 nms），thresh=0.5
  topk             : keep[:DETECTIONS_PER_IMAGE=100]
  分数阈           : SCORE_THRESH_TEST = 0.05（filter_mask = scores > 0.05）

用法（板上）：
  PYTHONPATH=/home/cat/rknn_ab/site/232 python3 board_detect.py
"""

import os
import sys
import time
import json

import numpy as np
from rknnlite.api import RKNNLite

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rpn_np          # noqa: E402
import roialign_np     # noqa: E402

DIR = "/home/cat/dcm_x101"
BACKBONE_RKNN = os.path.join(DIR, "x101_backbone_fpn.rknn")
RPN_RKNN = os.path.join(DIR, "rpn_head.rknn")
BOXHEAD_RKNN = os.path.join(DIR, "boxhead.rknn")
META = os.path.join(HERE, "meta.json")

# ---- 解码超参（来自 defaults.py + faster_rcnn_X_101_32x8d_FPN_3x + eval 脚本 --score_threshold 默认 0.05）----
SCALE_CLAMP = float(np.log(1000.0 / 16))
BBOX_REG_WEIGHTS = (10.0, 10.0, 5.0, 5.0)
SCORE_THRESH = 0.05
NMS_THRESH = 0.5
TOPK = 100
NUM_CLASSES = 80  # 前景类数（logits 81 = 80 + 背景）
# **（2026-10-14 DCM 演示路 / 用户裁定）类屏蔽**：此处列出的 COCO 类索引，在最终解码时
# **把其分数清零** ⇒ 该类**永不通过分数阈**。用途 = 压掉场景中不可能出现的类的误检
# （实测：暗箱里检出 bed 0.72 / couch 0.94）。
# **默认空元组 ⇒ 本文件行为与改动前逐位相同**（零回归；`main()` 的板-GPU 逐框比对不受影响）。
# 由调用方注入（`dcm_demo.py --drop-classes`；**组件本身不写死策略**）。
DROP_CLASSES = ()

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

# FPN 特征 p2~p6 的期望 shape（用于校验 RKNN 输出顺序）
EXPECT_P = [(1, 256, 192, 336), (1, 256, 96, 168), (1, 256, 48, 84),
            (1, 256, 24, 42), (1, 256, 12, 21)]


def softmax(x):
    x = x.astype(np.float32)
    m = x.max(axis=-1, keepdims=True)
    e = np.exp(x - m)
    return (e / e.sum(axis=-1, keepdims=True)).astype(np.float32)


def apply_deltas_classwise(deltas, boxes):
    """Box2BoxTransform.apply_deltas 的类别相关版：deltas (N,320) -> (N,80,4) xyxy（fp32）。"""
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
    return np.stack((x1, y1, x2, y2), axis=-1).astype(np.float32)  # (N,80,4)


def fast_rcnn_decode(cls_logits, box_deltas, proposals, image_hw, K):
    """复刻 fast_rcnn_inference_single_image。返回 list[dict(cls,score,xyxy)]，坐标在画布系(已 clip 到 newh,neww)。

    cls_logits (N,81); box_deltas (N,320); proposals (N,4) 画布系; image_hw=(newh,neww); K=真实 proposal 数
    （index>=K 为补 pad 的假 proposal，强制不进 filter，避免假阳性）。
    """
    N = cls_logits.shape[0]
    H, W = int(image_hw[0]), int(image_hw[1])
    probs = softmax(cls_logits)               # (N,81)
    scores = probs[:, :NUM_CLASSES]           # drop background -> (N,80)
    # **（2026-10-14 用户裁定）类屏蔽**（见 `DROP_CLASSES` 注释）。**必须是"清零"而不是
    # "删掉该 logit 后重新 softmax"** —— softmax 是**互斥归一化**，重归一化会把被删类的概率
    # 质量**转移**给其它类（例：bed 0.72 ⇒ cake 冒到 0.31）⇒ 误检只换了张标签；清零则让该框
    # **直接不过分数阈**，且**其它类的分数一个都不变**。
    if DROP_CLASSES:
        scores[:, list(DROP_CLASSES)] = 0.0
    pred_boxes = apply_deltas_classwise(box_deltas, proposals)  # (N,80,4)

    # finite 过滤（detectron2 valid_mask，按 proposal 行）
    finite = np.isfinite(pred_boxes).all(axis=(1, 2)) & np.isfinite(scores).all(axis=1)

    # clip 到 image_shape（x->[0,W], y->[0,H]）
    pred_boxes[:, :, 0] = np.clip(pred_boxes[:, :, 0], 0, W)
    pred_boxes[:, :, 1] = np.clip(pred_boxes[:, :, 1], 0, H)
    pred_boxes[:, :, 2] = np.clip(pred_boxes[:, :, 2], 0, W)
    pred_boxes[:, :, 3] = np.clip(pred_boxes[:, :, 3], 0, H)

    filter_mask = scores > SCORE_THRESH       # (N,80)
    filter_mask &= finite[:, None]
    if K < N:                                 # 屏蔽补 pad 的假 proposal
        filter_mask[K:, :] = False

    if not filter_mask.any():
        return []
    filter_inds = np.argwhere(filter_mask)    # (R',2) 行主序 (row, class)
    boxes_sel = pred_boxes[filter_mask]       # (R',4)
    scores_sel = scores[filter_mask]          # (R',)
    classes_sel = filter_inds[:, 1]

    # batched_nms：坐标偏移技巧（class*(max_coord+1)）后单次 nms
    max_coord = float(boxes_sel.max())
    offsets = classes_sel.astype(np.float32) * (np.float32(max_coord) + np.float32(1.0))
    boxes_for_nms = boxes_sel + offsets[:, None]
    keep = rpn_np.nms(boxes_for_nms, scores_sel, NMS_THRESH)
    keep = keep[:TOPK]

    out = []
    for k in keep:
        out.append({"cls": int(classes_sel[k]),
                    "score": float(scores_sel[k]),
                    "xyxy": [float(v) for v in boxes_sel[k]]})
    return out


def iou(a, b):
    x1 = max(a[0], b[0]); y1 = max(a[1], b[1])
    x2 = min(a[2], b[2]); y2 = min(a[3], b[3])
    iw = max(0.0, x2 - x1); ih = max(0.0, y2 - y1)
    inter = iw * ih
    aa = (a[2] - a[0]) * (a[3] - a[1])
    ab = (b[2] - b[0]) * (b[3] - b[1])
    u = aa + ab - inter
    return inter / u if u > 0 else 0.0


def greedy_match(gpu, board, iou_thr=0.5):
    cands = []
    for gi, g in enumerate(gpu):
        for bi, b in enumerate(board):
            if g["cls"] == b["cls"]:
                v = iou(g["xyxy"], b["xyxy_orig"])  # GPU 原图系 vs 板上框÷scale 回原图系
                if v >= iou_thr:
                    cands.append((v, gi, bi))
    cands.sort(key=lambda x: -x[0])
    gused, bused, matches = set(), set(), []
    for v, gi, bi in cands:
        if gi not in gused and bi not in bused:
            gused.add(gi); bused.add(bi)
            matches.append((gi, bi, v))
    return matches


def load_model(path):
    rk = RKNNLite(verbose=False)
    assert rk.load_rknn(path) == 0, path
    assert rk.init_runtime() == 0, path
    return rk


def main():
    meta = json.load(open(META))
    img_ids = list(meta.keys())

    print("加载 RKNN 模型 ...")
    t0 = time.perf_counter()
    rk_bb = load_model(BACKBONE_RKNN)
    rk_rpn = load_model(RPN_RKNN)
    rk_bh = load_model(BOXHEAD_RKNN)
    print(f"3 模型加载+init 耗时: {time.perf_counter() - t0:.3f} s")

    # ---- 预热（不计时）----
    rng = np.random.default_rng(0)
    _ = rk_bb.inference(inputs=[(rng.standard_normal((1, 128, 192, 336)).astype(np.float32) * 0.05)],
                        data_format="nchw")
    _ = rk_rpn.inference(inputs=[(rng.standard_normal(s).astype(np.float32) * 0.5) for s in EXPECT_P],
                         data_format="nchw")
    _ = rk_bh.inference(inputs=[(rng.standard_normal((1000, 256, 7, 7)).astype(np.float32) * 0.5)],
                        data_format="nchw")
    print("预热完成\n")

    stage_t = {"backbone": [], "rpn": [], "anchor_nms": [], "roialign": [], "boxhead": [], "decode": []}
    results = {}

    for iid in img_ids:
        m = meta[iid]
        newh, neww = m["newh"], m["neww"]
        scale = m["scale"]
        res2 = np.load(os.path.join(HERE, f"res2_{int(iid):012d}.npy")).astype(np.float32)
        assert res2.shape == (1, 128, 192, 336), res2.shape

        # 1) backbone+FPN
        t = time.perf_counter()
        p = rk_bb.inference(inputs=[res2], data_format="nchw")
        stage_t["backbone"].append(time.perf_counter() - t)
        p = [np.asarray(x, dtype=np.float32) for x in p]
        for i, s in enumerate(EXPECT_P):
            assert p[i].shape == s, f"p{i+2} shape {p[i].shape} != {s}"

        # 2) RPN 头（喂 p 的副本：rknnlite 会就地把 NCHW 输入改写为 NHWC，
        #    若不复制将污染 p，导致后续 RoIAlign 拿到 (H,W,C) 布局而 shape 错位）
        p_for_rpn = [x.copy() for x in p]
        t = time.perf_counter()
        rpn_out = rk_rpn.inference(inputs=p_for_rpn, data_format="nchw")
        stage_t["rpn"].append(time.perf_counter() - t)
        rpn_out = [np.asarray(x, dtype=np.float32) for x in rpn_out]
        obj_list = rpn_out[0:5]   # (1,3,Hi,Wi)
        dlt_list = rpn_out[5:10]  # (1,12,Hi,Wi)

        # 3) anchor + decode + NMS（CPU numpy）
        t = time.perf_counter()
        prop_boxes, prop_scores = rpn_np.rpn_proposals(obj_list, dlt_list, (newh, neww), RPN_CFG)
        stage_t["anchor_nms"].append(time.perf_counter() - t)
        K = prop_boxes.shape[0]

        # 4) pad 到 1000（boxhead 固定 N=1000）
        if K < 1000:
            pad = np.zeros((1000 - K, 4), dtype=np.float32)
            prop_pad = np.concatenate([prop_boxes, pad], axis=0)
        else:
            prop_pad = prop_boxes[:1000].copy()
            K = 1000

        # 5) RoIAlign（p2~p5，CPU numpy）
        t = time.perf_counter()
        pooled = roialign_np.roi_align(p[0:4], prop_pad, output_size=(7, 7),
                                       scales=(0.25, 0.125, 0.0625, 0.03125),
                                       sampling_ratio=0, canonical_box_size=224,
                                       canonical_level=4, aligned=True)
        stage_t["roialign"].append(time.perf_counter() - t)
        assert pooled.shape == (1000, 256, 7, 7), pooled.shape

        # 6) box_head + predictor（NPU）
        t = time.perf_counter()
        bh = rk_bh.inference(inputs=[np.ascontiguousarray(pooled, dtype=np.float32)], data_format="nchw")
        stage_t["boxhead"].append(time.perf_counter() - t)
        cls_logits = np.asarray(bh[0], dtype=np.float32)   # (1000,81)
        box_deltas = np.asarray(bh[1], dtype=np.float32)   # (1000,320)
        assert cls_logits.shape == (1000, 81) and box_deltas.shape == (1000, 320)

        # 保存原始 boxhead 输出，供离线多阈解码诊断（FP16 影响）
        np.savez(os.path.join(HERE, f"raw_{iid}.npz"), cls_logits=cls_logits,
                 box_deltas=box_deltas, prop=prop_pad, K=K, newh=newh, neww=neww, scale=scale)

        # 7) 最终 CPU 解码（softmax + 类别相关 delta + box transform + NMS + topk）
        t = time.perf_counter()
        dets = fast_rcnn_decode(cls_logits, box_deltas, prop_pad, (newh, neww), K)
        stage_t["decode"].append(time.perf_counter() - t)

        # 坐标换算到原图系：÷scale
        for d in dets:
            d["xyxy_orig"] = [v / scale for v in d["xyxy"]]

        # ---- 与 GPU 比对 ----
        gpu = m["gpu"]
        matches = greedy_match(gpu, dets)
        ngpu = len(gpu)
        nmatch = len(matches)
        rate = nmatch / ngpu if ngpu else float("nan")
        ious = [mt[2] for mt in matches]
        sdiffs = [abs(gpu[mt[0]]["score"] - dets[mt[1]]["score"]) for mt in matches]
        results[iid] = {"K": K, "ndet_board": len(dets), "ngpu": ngpu, "nmatch": nmatch,
                        "rate": rate, "dets": dets,
                        "mean_iou": float(np.mean(ious)) if ious else None,
                        "mean_sdiff": float(np.mean(sdiffs)) if sdiffs else None}

        tot = sum(stage_t[k][-1] for k in stage_t)
        print(f"[img {iid}] K={K} 板帧={len(dets)} GPU帧={ngpu} 配对={nmatch} "
              f"配对率={rate*100:.1f}% meanIoU={np.mean(ious) if ious else float('nan'):.3f} "
              f"mean|Δscore|={np.mean(sdiffs) if sdiffs else float('nan'):.3f} | "
              f"bb={stage_t['backbone'][-1]:.3f} rpn={stage_t['rpn'][-1]:.3f} "
              f"anc={stage_t['anchor_nms'][-1]:.3f} roi={stage_t['roialign'][-1]:.3f} "
              f"bh={stage_t['boxhead'][-1]:.3f} dec={stage_t['decode'][-1]:.3f} 总={tot:.3f}s")

    # ---- 汇总 ----
    print("\n==== 各阶段耗时（5 张均值 / 单张）====")
    names = {"backbone": "backbone+FPN", "rpn": "RPN头", "anchor_nms": "anchor+NMS",
             "roialign": "RoIAlign", "boxhead": "boxhead", "decode": "最终解码"}
    total_mean = 0.0
    for k in stage_t:
        arr = np.array(stage_t[k])
        total_mean += arr.mean()
        print(f"  {names[k]:14s}: mean={arr.mean()*1000:8.2f} ms  min={arr.min()*1000:8.2f}  max={arr.max()*1000:8.2f}")
    print(f"  {'检测总耗时':14s}: mean={total_mean*1000:8.2f} ms")

    print("\n==== 逐框比对汇总 ====")
    print(f"{'img':>8} {'GPU帧':>5} {'板帧':>5} {'配对':>4} {'配对率':>7} {'meanIoU':>8} {'mean|Δs|':>9}")
    tg = tb = tm = 0
    for iid in img_ids:
        r = results[iid]
        tg += r["ngpu"]; tb += r["ndet_board"]; tm += r["nmatch"]
        print(f"{iid:>8} {r['ngpu']:>5} {r['ndet_board']:>5} {r['nmatch']:>4} "
              f"{r['rate']*100:>6.1f}% {r['mean_iou'] if r['mean_iou'] else float('nan'):>8.3f} "
              f"{r['mean_sdiff'] if r['mean_sdiff'] else float('nan'):>9.3f}")
    print(f"{'合计':>8} {tg:>5} {tb:>5} {tm:>4} {(tm/tg*100 if tg else 0):>6.1f}%")

    json.dump(results, open(os.path.join(HERE, "board_results.json"), "w"))
    print("\n结果已存 board_results.json")
    for rk in (rk_bb, rk_rpn, rk_bh):
        rk.release()
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
