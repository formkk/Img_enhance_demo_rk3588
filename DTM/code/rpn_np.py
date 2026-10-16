# -*- coding: utf-8 -*-
"""
rpn_np.py —— detectron2 RPN 后处理（anchor 生成 + box decode + pre/post NMS top-k）的纯 numpy 复刻。
不 import torch / detectron2，可直接在板端 CPU 运行。

口径逐项对齐（全部从源码/配置取，未猜）：
  anchor 生成 : detectron2/modeling/anchor_generator.py AnchorGenerator
                 SIZES=[[32],[64],[128],[256],[512]] / ASPECT_RATIOS=[[0.5,1.0,2.0]] / OFFSET=0.0
                 strides=[4,8,16,32,64]（p2~p6）；cell anchor: w=sqrt(s^2/a), h=a*w，中心 (0,0)；
                 网格 shift=arange(offset*stride, grid*stride, stride)，行主序 (y,x)，
                 anchor 序号 = ((y*Wi + x)*A + a)，与 logits/deltas 的 flatten 口径一致。
  logits/deltas: detectron2/modeling/proposal_generator/rpn.py RPN.forward 的 permute 口径：
                 obj  (N,A,Hi,Wi)   -> (N,Hi,Wi,A)   -> (N,Hi*Wi*A)
                 dlt  (N,A*4,Hi,Wi) -> (N,A,4,Hi,Wi) -> (N,Hi,Wi,A,4) -> (N,Hi*Wi*A,4)
  box decode   : detectron2/modeling/box_regression.py Box2BoxTransform.apply_deltas
                 weights=(1,1,1,1)，dw/dh 上限 scale_clamp=log(1000/16)，全程 fp32。
  后处理       : detectron2/modeling/proposal_generator/proposal_utils.py find_top_rpn_proposals
                 每级 pre_nms_topk(=1000) -> 拼接 -> finite 过滤 -> clip 到 image_hw
                 -> nonempty(min_size=0) -> 每级 NMS(0.7) -> 跨级按分数降序取 post_nms_topk(=1000)。
                 topk 在原始 logits 上进行（sigmoid 单调，等价于在 sigmoid 分数上取 topk）。
  NMS          : torchvision 0.13.1 batched_nms：boxes.numel()>4000 时走 _batched_nms_vanilla
                 （按 level 分组各自 nms，再按分数降序合并）；nms 内核按分数降序贪心、
                 IoU 严格大于阈值才抑制。
"""

import numpy as np

# Box2BoxTransform._DEFAULT_SCALE_CLAMP = math.log(1000.0 / 16)
SCALE_CLAMP = float(np.log(1000.0 / 16))


def _broadcast_params(params, num_features):
    """与 detectron2 anchor_generator._broadcast_params 同语义：标量 list 或长度为 1 时广播到每一级。"""
    if not isinstance(params[0], (list, tuple)):  # 标量 list，如 [0.5, 1.0, 2.0]
        params = [params] * num_features
    elif len(params) == 1:
        params = params * num_features
    assert len(params) == num_features
    return params


def generate_cell_anchors(sizes, aspect_ratios):
    """与 AnchorGenerator.generate_cell_anchors 一致：python float64 计算后转 float32（同 torch.tensor 默认 dtype）。"""
    anchors = []
    for size in sizes:
        area = float(size) ** 2.0
        for ar in aspect_ratios:
            w = np.sqrt(area / ar)
            h = ar * w
            anchors.append([-w / 2.0, -h / 2.0, w / 2.0, h / 2.0])
    return np.asarray(anchors, dtype=np.float64).astype(np.float32)


def generate_anchors(grid_hw, stride, sizes, aspect_ratios, offset=0.0):
    """与 AnchorGenerator._grid_anchors 一致。返回 (gh*gw*A, 4) float32，行主序 (y,x) x anchor 序。"""
    gh, gw = int(grid_hw[0]), int(grid_hw[1])
    cell = generate_cell_anchors(sizes, aspect_ratios)  # (A, 4) float32
    shift_x = np.arange(offset * stride, gw * stride, step=stride, dtype=np.float32)
    shift_y = np.arange(offset * stride, gh * stride, step=stride, dtype=np.float32)
    sy, sx = np.meshgrid(shift_y, shift_x, indexing="ij")
    sx = sx.reshape(-1)
    sy = sy.reshape(-1)
    shifts = np.stack((sx, sy, sx, sy), axis=1)  # (gh*gw, 4)
    anchors = shifts[:, None, :] + cell[None, :, :]
    return anchors.reshape(-1, 4).astype(np.float32)


def decode_deltas(deltas, anchors, weights=(1.0, 1.0, 1.0, 1.0), scale_clamp=SCALE_CLAMP):
    """与 Box2BoxTransform.apply_deltas 一致（fp32）。deltas (M,4)、anchors (M,4) -> boxes (M,4) xyxy。"""
    deltas = np.asarray(deltas, dtype=np.float32)
    anchors = np.asarray(anchors, dtype=np.float32)
    widths = anchors[:, 2] - anchors[:, 0]
    heights = anchors[:, 3] - anchors[:, 1]
    ctr_x = anchors[:, 0] + 0.5 * widths
    ctr_y = anchors[:, 1] + 0.5 * heights
    wx, wy, ww, wh = weights
    dx = deltas[:, 0] / wx
    dy = deltas[:, 1] / wy
    dw = deltas[:, 2] / ww
    dh = deltas[:, 3] / wh
    dw = np.minimum(dw, np.float32(scale_clamp))
    dh = np.minimum(dh, np.float32(scale_clamp))
    pred_ctr_x = dx * widths + ctr_x
    pred_ctr_y = dy * heights + ctr_y
    pred_w = np.exp(dw) * widths
    pred_h = np.exp(dh) * heights
    x1 = pred_ctr_x - 0.5 * pred_w
    y1 = pred_ctr_y - 0.5 * pred_h
    x2 = pred_ctr_x + 0.5 * pred_w
    y2 = pred_ctr_y + 0.5 * pred_h
    return np.stack((x1, y1, x2, y2), axis=-1).astype(np.float32)


def nms(boxes, scores, thresh):
    """纯 numpy 贪心 NMS：按分数降序（stable）逐个选取，抑制与其 IoU 严格大于 thresh 的框。
    与 torchvision nms 内核语义一致。返回 keep 索引（按分数降序）。"""
    boxes = np.asarray(boxes, dtype=np.float32)
    scores = np.asarray(scores, dtype=np.float32)
    n = boxes.shape[0]
    if n == 0:
        return np.empty(0, dtype=np.int64)
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = np.argsort(-scores, kind="stable")
    keep = []
    idxs = order
    while idxs.size > 0:
        i = idxs[0]
        keep.append(int(i))
        if idxs.size == 1:
            break
        rest = idxs[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        union = areas[i] + areas[rest] - inter
        iou = inter / union
        idxs = rest[~(iou > thresh)]
    return np.asarray(keep, dtype=np.int64)


def rpn_proposals(obj_list, dlt_list, image_hw, cfg, return_logits=False):
    """detectron2 RPN 后处理的纯 numpy 复刻（单张图，N=1）。

    参数:
        obj_list: 长度 5 的 list，元素 (1, A, Hi, Wi) float —— objectness logits（未过 sigmoid），
                  即 RKNN 的 obj2~obj6。
        dlt_list: 长度 5 的 list，元素 (1, A*4, Hi, Wi) float —— box regression deltas，
                  即 RKNN 的 dlt2~dlt6。
        image_hw: (H, W) —— detectron2 images.image_sizes（resize 后、padding 前的尺寸），
                  proposal 的 clip 边界与该坐标系。
        cfg: dict，键: sizes / aspect_ratios / strides / offset / pre_nms_topk / post_nms_topk /
             nms_thresh / min_box_size / bbox_reg_weights。
        return_logits: 为 True 时额外返回原始 objectness logits（detectron2 的 objectness_logits 口径）。

    返回:
        (boxes_xyxy (K,4) float32, scores (K,) float32)
        scores = sigmoid(objectness)，按分数降序；return_logits=True 时返回 (boxes, scores, logits)。
        与 detectron2 find_top_rpn_proposals 的输出逐框一致。
    """
    H, W = int(image_hw[0]), int(image_hw[1])
    num_levels = len(cfg["strides"])
    sizes = _broadcast_params(cfg["sizes"], num_levels)
    aspect_ratios = _broadcast_params(cfg["aspect_ratios"], num_levels)
    strides = list(cfg["strides"])
    offset = float(cfg.get("offset", 0.0))
    pre_nms_topk = int(cfg["pre_nms_topk"])
    post_nms_topk = int(cfg["post_nms_topk"])
    nms_thresh = float(cfg["nms_thresh"])
    min_box_size = float(cfg.get("min_box_size", 0.0))
    weights = cfg.get("bbox_reg_weights", (1.0, 1.0, 1.0, 1.0))

    assert len(obj_list) == len(dlt_list) == num_levels

    # 1. 每级：anchor 生成 -> decode -> pre-nms topk（find_top_rpn_proposals 第 1 步）
    topk_boxes, topk_scores, topk_lvl = [], [], []
    for lvl, (obj, dlt) in enumerate(zip(obj_list, dlt_list)):
        obj = np.asarray(obj, dtype=np.float32)
        dlt = np.asarray(dlt, dtype=np.float32)
        assert obj.ndim == 4 and dlt.ndim == 4, "obj/dlt 需为 (1, A, Hi, Wi) / (1, A*4, Hi, Wi)"
        assert obj.shape[0] == 1 and dlt.shape[0] == 1, "仅支持单张图（N=1）"
        A, Hi, Wi = obj.shape[1], obj.shape[2], obj.shape[3]
        # RPN.forward 的 permute 口径
        logits = obj.transpose(0, 2, 3, 1).reshape(-1)  # (Hi*Wi*A,)
        deltas = (
            dlt.reshape(dlt.shape[0], A, 4, Hi, Wi)
            .transpose(0, 3, 4, 1, 2)
            .reshape(-1, 4)  # (Hi*Wi*A, 4)
        )
        anchors = generate_anchors((Hi, Wi), strides[lvl], sizes[lvl], aspect_ratios[lvl], offset)
        boxes = decode_deltas(deltas, anchors, weights)  # (Hi*Wi*A, 4)
        k = min(logits.shape[0], pre_nms_topk)
        topk_idx = np.argsort(-logits, kind="stable")[:k]
        topk_boxes.append(boxes[topk_idx])
        topk_scores.append(logits[topk_idx])
        topk_lvl.append(np.full(k, lvl, dtype=np.int64))

    # 2. 跨级拼接
    boxes = np.concatenate(topk_boxes, axis=0)  # (sum_k, 4)
    scores = np.concatenate(topk_scores, axis=0)  # logits
    lvl = np.concatenate(topk_lvl, axis=0)

    # 3. finite 过滤（valid_mask）
    valid = np.isfinite(boxes).all(axis=1) & np.isfinite(scores)
    if not valid.all():
        boxes, scores, lvl = boxes[valid], scores[valid], lvl[valid]

    # 4. clip 到图像边界（Boxes.clip: x->[0,W], y->[0,H]）
    boxes[:, 0] = np.clip(boxes[:, 0], 0, W)
    boxes[:, 1] = np.clip(boxes[:, 1], 0, H)
    boxes[:, 2] = np.clip(boxes[:, 2], 0, W)
    boxes[:, 3] = np.clip(boxes[:, 3], 0, H)

    # 5. nonempty 过滤（min_box_size）
    keep = (boxes[:, 2] - boxes[:, 0] > min_box_size) & (boxes[:, 3] - boxes[:, 1] > min_box_size)
    if not keep.all():
        boxes, scores, lvl = boxes[keep], scores[keep], lvl[keep]

    # 6. 每级 NMS，再跨级按分数降序取 post_nms_topk（torchvision _batched_nms_vanilla 口径）
    keep_list = []
    for l in np.unique(lvl):
        m = lvl == l
        idx = np.nonzero(m)[0]
        sub_keep = nms(boxes[m], scores[m], nms_thresh)
        keep_list.append(idx[sub_keep])
    keep_idx = np.concatenate(keep_list, axis=0)
    order = np.argsort(-scores[keep_idx], kind="stable")
    keep_idx = keep_idx[order][:post_nms_topk]

    out_boxes = boxes[keep_idx]
    out_logits = scores[keep_idx]
    out_scores = (1.0 / (1.0 + np.exp(-out_logits.astype(np.float64)))).astype(np.float32)
    if return_logits:
        return out_boxes, out_scores, out_logits
    return out_boxes, out_scores
