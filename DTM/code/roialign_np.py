# -*- coding: utf-8 -*-
"""
roialign_np.py —— detectron2 ROIPooler（roi_heads.box_pooler，pooler_type="ROIAlignV2"）的纯 numpy 复刻。
不 import torch / detectron2 / torchvision，可直接在板端 CPU 运行。

口径逐项对齐（全部从源码 / 配置 / 运行时实测取，未猜）：
  ROIPooler  : detectron2/modeling/poolers.py。本模型（faster_rcnn_X_101_32x8d_FPN_3x.yaml
               + Base-RCNN-FPN.yaml）运行时实测 model.roi_heads.box_pooler：
                 output_size=(7,7)；scales=(1/4,1/8,1/16,1/32)（ROI_HEADS.IN_FEATURES=["p2".."p5"]，共 4 级）；
                 sampling_ratio=0（POOLER_SAMPLING_RATIO 默认 0 → 自适应网格）；
                 pooler_type="ROIAlignV2" → torchvision roi_align 的 aligned=True；
                 canonical_box_size=224，canonical_level=4 ⇒ min_level=2，max_level=5。
  分级公式   : assign_boxes_to_levels（detectron2 0.6 内即 map_rois_to_fpn_levels）：
                 box_sizes = sqrt((x2-x1)*(y2-y1))
                 level = clamp(floor(canonical_level + log2(box_sizes/canonical_box_size + 1e-8)),
                               min_level, max_level) - min_level
  roi_align  : detectron2 layers/roi_align.py ROIAlign.forward → torchvision.ops.roi_align
               （torchvision 0.13.1，torch.ops.torchvision.roi_align；CUDA 内核
               csrc/ops/cuda/roi_align_kernel.cu 与 CPU 内核数学等价）：
                 offset = aligned ? 0.5 : 0.0
                 roi_start = coord*spatial_scale - offset（x1,y1,x2,y2 四边都减）
                 bin_size = (roi_end-roi_start)/pooled_size
                 grid = sampling_ratio>0 ? sampling_ratio : ceil(roi_size/pooled_size)
                 count = max(grid_h*grid_w, 1)
                 采样点 y = roi_start_h + ph*bin_size_h + (iy+0.5)*bin_size_h/grid_h（x 同理）
                 双线性：y<-1||y>H||x<-1||x>W → 0；y<=0→0、x<=0→0；
                        y_low=(int)y；y_low>=H-1 ⇒ y_high=y_low=H-1 且 y=y_low（x 同理）；
                        val = hy*hx*v[y_low,x_low] + hy*lx*v[y_low,x_high]
                            + ly*hx*v[y_high,x_low] + ly*lx*v[y_high,x_high]
                 输出 = Σval / count
  拼回顺序   : ROIPooler.forward 按 level 分组、index_put_ 回原顺序；本实现同。

实现说明（向量化版，与原逐循环版逐元素 bit-exact）：
  原实现用 Python 四层循环（49 bin × max_grid_h × max_grid_w），每次循环都对整个 level 的
  全部 proposal 做一批双线性（valid 掩码外的点白算，实测浪费约 2.4×），1000 proposals 耗时
  约 4.8 s（i7-13700K）。本版策略：
    1) 按 level 分组后，再按 (grid_h, grid_w) 分桶——同桶内所有 proposal 的采样点张量
       shape 一致 (m, 49, grid_h, grid_w)，可用纯广播一次性算出全部采样点坐标，无一点浪费；
    2) 双线性 gather 用 numpy fancy indexing（feat 布局预转置为 (H,W,C) 行 gather），
       4 邻域索引与权重一次算完，加权和表达式与原版逐字同序；
    3) 段和：桶内 val  reshape 为 (m*49, grid_h*grid_w, C) 后沿段轴 np.cumsum 取 [:, -1]。
       cumsum 是顺序部分和且每行（段）从零起算，精确复现原版"按 (iy 外、ix 内) 行优先
       逐点累加"的浮点顺序（np.sum/reduceat 的 pairwise 求和会改变舍入，故不用）；
    4) 通道分块（chan_block）：gather→加权→cumsum→段和 按通道块循环，(P, cb) 中间张量
       控制在 cache 量级，避免纯 DRAM 流量瓶颈；
    5) 按累计采样点数分块（chunk_points）控制内存峰值。
  退化 ROI（grid_h*grid_w <= 0，如 x2<=x1）不入桶，输出保持 0，与原版一致。
"""

import math

import numpy as np


def assign_boxes_to_levels(boxes_xyxy, min_level, max_level, canonical_box_size, canonical_level):
    """detectron2 poolers.assign_boxes_to_levels（旧名 map_rois_to_fpn_levels）的 numpy 复刻（fp32）。

    返回 (N,) int64，值域 [0, max_level-min_level]（相对 min_level 的级内偏移）。
    """
    boxes = np.asarray(boxes_xyxy, dtype=np.float32).reshape(-1, 4)
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    box_sizes = np.sqrt(areas)
    levels = np.floor(
        np.float32(canonical_level)
        + np.log2(box_sizes / np.float32(canonical_box_size) + np.float32(1e-8))
    )
    levels = np.clip(levels, min_level, max_level)
    return levels.astype(np.int64) - min_level


def _bilinear_indices_weights(y, x, H, W):
    """torchvision roi_align bilinear_interpolate 的索引与权重计算（批量版）。

    y/x: (P,) float32 连续坐标。返回 (i1,i2,i3,i4, w1,w2,w3,w4, in_range)：
    4 邻域在 (H*W,) 扁平特征里的行号、双线性权重 (P,1)、越界掩码 (P,)。
    边界逻辑与原逐循环版逐字一致：y<=0→0；y_low>=H-1 ⇒ 钳到 H-1；越界（y<-1||y>H）置零。
    """
    yy = np.where(y <= np.float32(0), np.float32(0), y)
    xx = np.where(x <= np.float32(0), np.float32(0), x)
    y_low = yy.astype(np.int64)
    x_low = xx.astype(np.int64)
    y_high = y_low + 1
    x_high = x_low + 1
    top = y_low >= H - 1
    if top.any():
        y_low = np.where(top, H - 1, y_low)
        y_high = np.where(top, H - 1, y_high)
        yy = np.where(top, np.float32(H - 1), yy)
    left = x_low >= W - 1
    if left.any():
        x_low = np.where(left, W - 1, x_low)
        x_high = np.where(left, W - 1, x_high)
        xx = np.where(left, np.float32(W - 1), xx)
    ly = (yy - y_low.astype(np.float32)).astype(np.float32)
    lx = (xx - x_low.astype(np.float32)).astype(np.float32)
    hy = np.float32(1) - ly
    hx = np.float32(1) - lx
    i1 = y_low * W + x_low
    i2 = y_low * W + x_high
    i3 = y_high * W + x_low
    i4 = y_high * W + x_high
    w1 = (hy * hx).astype(np.float32)[:, None]
    w2 = (hy * lx).astype(np.float32)[:, None]
    w3 = (ly * hx).astype(np.float32)[:, None]
    w4 = (ly * lx).astype(np.float32)[:, None]
    in_range = (y >= np.float32(-1.0)) & (y <= np.float32(H)) \
        & (x >= np.float32(-1.0)) & (x <= np.float32(W))
    return i1, i2, i3, i4, w1, w2, w3, w4, in_range


def _roi_align_level(feat, rois, output_size, spatial_scale, sampling_ratio, aligned,
                     chunk_points=8192, chan_block=64):
    """单级 RoIAlign：feat (C,H,W) float32；rois (K,5)=[batch_idx,x1,y1,x2,y2] float32。
    返回 (K,C,oh,ow) float32，逐元素对齐 torchvision 0.13.1 roi_align 内核。

    向量化策略：按 (grid_h, grid_w) 分桶 → 桶内广播算全部采样点 → fancy indexing
    双线性 gather → 沿段轴 cumsum 还原顺序累加；通道分块 + 点数分块控内存。
    加权和表达式与原版逐字同序；实测 np.take(out=) / 原地 ufunc 比表达式更慢，故不采用。
    """
    C, H, W = feat.shape
    K = rois.shape[0]
    oh, ow = int(output_size[0]), int(output_size[1])
    out = np.zeros((K, C, oh, ow), dtype=np.float32)
    if K == 0:
        return out

    offset = np.float32(0.5) if aligned else np.float32(0.0)
    scale = np.float32(spatial_scale)
    x1 = rois[:, 1] * scale - offset
    y1 = rois[:, 2] * scale - offset
    x2 = rois[:, 3] * scale - offset
    y2 = rois[:, 4] * scale - offset
    roi_w = (x2 - x1).astype(np.float32)
    roi_h = (y2 - y1).astype(np.float32)
    if not aligned:
        # torchvision：!aligned 时把畸形 ROI 强制成 1x1
        roi_w = np.maximum(roi_w, np.float32(1.0))
        roi_h = np.maximum(roi_h, np.float32(1.0))
    bin_h = (roi_h / np.float32(oh)).astype(np.float32)
    bin_w = (roi_w / np.float32(ow)).astype(np.float32)
    if sampling_ratio > 0:
        grid_h = np.full(K, int(sampling_ratio), dtype=np.int64)
        grid_w = np.full(K, int(sampling_ratio), dtype=np.int64)
    else:
        grid_h = np.ceil(roi_h / np.float32(oh)).astype(np.int64)
        grid_w = np.ceil(roi_w / np.float32(ow)).astype(np.int64)

    # 特征预转置为 (H,W,C) 行 gather 布局（数值不变，访存连续）
    feat_flat = np.ascontiguousarray(feat.transpose(1, 2, 0)).reshape(H * W, C)

    n_bins = oh * ow
    ph_f = np.repeat(np.arange(oh, dtype=np.float32), ow)   # (49,) b = ph*ow+pw
    pw_f = np.tile(np.arange(ow, dtype=np.float32), oh)     # (49,)

    # ---- 按 (grid_h, grid_w) 分桶：同桶 shape 一致，可纯广播批量 ----
    valid = (grid_h >= 1) & (grid_w >= 1)
    vidx = np.nonzero(valid)[0]
    if vidx.size == 0:
        return out
    keys = grid_h[vidx] * 1024 + grid_w[vidx]
    order = np.argsort(keys, kind="stable")
    skeys = keys[order]
    bnd = np.nonzero(np.diff(skeys))[0] + 1
    starts = np.concatenate([[0], bnd])
    ends = np.concatenate([bnd, [skeys.size]])

    for s, e in zip(starts, ends):
        sel_all = vidx[order[s:e]]
        gh_v = int(skeys[s]) // 1024
        gw_v = int(skeys[s]) % 1024
        ghw = gh_v * gw_v
        cnt_f = np.float32(max(ghw, 1))          # count = max(grid_h*grid_w, 1)
        gh_f = np.float32(gh_v)
        gw_f = np.float32(gw_v)
        iy_f = np.arange(gh_v, dtype=np.float32)
        ix_f = np.arange(gw_v, dtype=np.float32)
        per_prop = n_bins * ghw
        n_sub = max(1, int(chunk_points // per_prop))
        for s0 in range(0, sel_all.size, n_sub):
            sub = sel_all[s0:s0 + n_sub]
            m = sub.size
            # ---- 采样点坐标（运算顺序与原逐循环版一致：float32，(y1+A)+B 左结合）----
            bin_hs = bin_h[sub][:, None, None]                # (m,1,1)
            bin_ws = bin_w[sub][:, None, None]
            y = y1[sub][:, None, None] + ph_f[None, :, None] * bin_hs \
                + (iy_f[None, None, :] + np.float32(0.5)) * bin_hs / gh_f   # (m,49,gh)
            x = x1[sub][:, None, None] + pw_f[None, :, None] * bin_ws \
                + (ix_f[None, None, :] + np.float32(0.5)) * bin_ws / gw_f   # (m,49,gw)
            shape4 = (m, n_bins, gh_v, gw_v)
            # 广播到 (m,49,gh,gw) 后拉平：序即 (proposal, bin, iy, ix) 行优先
            y_p = np.broadcast_to(y[:, :, :, None], shape4).ravel()
            x_p = np.broadcast_to(x[:, :, None, :], shape4).ravel()
            i1, i2, i3, i4, w1, w2, w3, w4, in_range = _bilinear_indices_weights(y_p, x_p, H, W)
            all_in = bool(in_range.all())
            # ---- 通道分块：gather → 加权 → 段内 cumsum → 段和 ----
            # 加权和表达式与原版逐字同序：((w1*v1 + w2*v2) + w3*v3) + w4*v4
            res = np.empty((m, C, oh, ow), dtype=np.float32)
            for c0 in range(0, C, chan_block):
                cb = min(chan_block, C - c0)
                v1 = feat_flat[i1, c0:c0 + cb]                # (P,cb)
                v2 = feat_flat[i2, c0:c0 + cb]
                v3 = feat_flat[i3, c0:c0 + cb]
                v4 = feat_flat[i4, c0:c0 + cb]
                val = w1 * v1 + w2 * v2 + w3 * v3 + w4 * v4
                if not all_in:
                    val *= in_range[:, None]
                # 段轴 cumsum：每行（=一个 (proposal,bin) 段）从零起顺序累加，bit-exact
                cs = np.cumsum(val.reshape(m * n_bins, ghw, cb), axis=1)
                sums = cs[:, -1, :] / cnt_f                              # (m*49,cb)
                res[:, c0:c0 + cb] = sums.reshape(m, oh, ow, cb).transpose(0, 3, 1, 2)
            out[sub] = res
    return out


def roi_align(features_list, proposals_xyxy, output_size=(7, 7),
              scales=(0.25, 0.125, 0.0625, 0.03125), sampling_ratio=0,
              canonical_box_size=224, canonical_level=4, aligned=True):
    """detectron2 ROIPooler.forward 的纯 numpy 复刻。

    Args:
        features_list: list of (1,C,H,W) 或 (C,H,W) float32 —— p2~p5（顺序与 scales 对应）。
        proposals_xyxy: (N,4) float32，原图坐标系 xyxy（单张图，batch=1）。
        output_size / scales / sampling_ratio / canonical_*: 默认值即本模型 box_pooler 实测口径。
    返回 (N,C,oh,ow) float32，与 detectron2 model.roi_heads.box_pooler 逐元素一致。
    """
    feats = []
    for f in features_list:
        f = np.asarray(f, dtype=np.float32)
        if f.ndim == 4:
            if f.shape[0] != 1:
                raise ValueError("仅支持单张图（batch=1）")
            f = f[0]
        feats.append(np.ascontiguousarray(f))
    boxes = np.asarray(proposals_xyxy, dtype=np.float32).reshape(-1, 4)
    N = boxes.shape[0]
    C = feats[0].shape[0]
    oh, ow = int(output_size[0]), int(output_size[1])
    out = np.zeros((N, C, oh, ow), dtype=np.float32)
    if N == 0:
        return out

    num_levels = len(feats)
    if len(scales) != num_levels:
        raise ValueError("scales 数量与特征级数不一致")
    min_level = int(round(-math.log2(scales[0])))
    max_level = int(round(-math.log2(scales[-1])))
    level_assignments = assign_boxes_to_levels(
        boxes, min_level, max_level, canonical_box_size, canonical_level
    )
    # convert_boxes_to_pooler_format：前置 batch 列（单张图全 0）
    rois = np.concatenate([np.zeros((N, 1), dtype=np.float32), boxes], axis=1)
    for level in range(num_levels):
        inds = np.nonzero(level_assignments == level)[0]
        if inds.size == 0:
            continue
        out[inds] = _roi_align_level(
            feats[level], rois[inds], (oh, ow), scales[level], sampling_ratio, aligned
        )
    return out
