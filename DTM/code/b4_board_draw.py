#!/usr/bin/env python3
"""B4（板端）：rknnlite 跑**训练好的**学生头「128ch 解码特征 → 框」，解码+NMS（numpy），叠框到低码率重建图上。

输入（scp 到板端同目录）：
    fdet_trained.rknn        B3 产出（FP16）
    feat_q19.npy             输入特征 (1,128,192,336) float32
    base_q19.npy             底图 RGB uint8 (750,1333,3) = q19 低码率重建图
    onnx_out_trained_q19.npy onnxruntime 参考输出 (1,N,84) —— 数值交叉校验
产出：
    board_trained_boxes.png / board_trained_boxes.json

说明：头已在 COCO（q∈{3,7,…,31} 抖动）上**教师蒸馏训练收敛**（val200 R@0.05=0.375 / R@0.30=0.244，biou=0.567）；
      本步验「同一网络在板上跑出的框」与 ONNX 数值一致，并给出真实叠框图。
用法（板端）：cd <DIR> && PYTHONPATH=/home/cat/rknn_ab/site/232 python3 b4_board_draw.py
"""

import json
import os
import struct
import time
import zlib

import numpy as np
from rknnlite.api import RKNNLite

HERE = os.path.dirname(os.path.abspath(__file__))
RKNN_PATH = os.path.join(HERE, "fdet_trained.rknn")
OUT_PNG = os.path.join(HERE, "board_trained_boxes.png")
OUT_JSON = os.path.join(HERE, "board_trained_boxes.json")

# (stride, h, w) —— 必须与训练/导出时的层序一致（level_hw）
LEVELS = [(8, 96, 168), (16, 48, 84), (32, 24, 42), (64, 12, 21)]
NC = 80
CONFS = (0.03, 0.05, 0.10, 0.30)
DRAW_CONF, IOU = 0.05, 0.45
IMG_H, IMG_W = 750, 1333


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -60, 60)))


def nms_np(boxes, scores, iou_thres):
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_thres]
    return keep


def decode(pred, conf_thres):
    """pred: (N,84) 前 80 cls logits + 后 4 ltrb（各级按 LEVELS 顺序）。"""
    boxes, scores, cls = [], [], []
    off = 0
    for s, h, w in LEVELS:
        n = h * w
        blk = pred[off:off + n]
        off += n
        l, t, r, b = blk[:, -4], blk[:, -3], blk[:, -2], blk[:, -1]
        sc = sigmoid(blk[:, :NC])
        ci = sc.argmax(1)
        smax = sc[np.arange(n), ci]
        gx, gy = np.meshgrid(np.arange(w), np.arange(h))
        cx = (gx.reshape(-1).astype(np.float32) + 0.5) * s
        cy = (gy.reshape(-1).astype(np.float32) + 0.5) * s
        m = smax > conf_thres
        if m.any():
            boxes.append(np.stack([cx[m] - l[m] * s, cy[m] - t[m] * s,
                                   cx[m] + r[m] * s, cy[m] + b[m] * s], 1))
            scores.append(smax[m])
            cls.append(ci[m])
    if not boxes:
        return []
    boxes = np.concatenate(boxes)
    scores = np.concatenate(scores)
    cls = np.concatenate(cls)
    keep_all = []
    for c in np.unique(cls):
        idx = np.where(cls == c)[0]
        for k in nms_np(boxes[idx], scores[idx], IOU):
            j = idx[k]
            xy = boxes[j].copy()
            xy[[0, 2]] = np.clip(xy[[0, 2]], 0, IMG_W - 1)
            xy[[1, 3]] = np.clip(xy[[1, 3]], 0, IMG_H - 1)
            keep_all.append({"cls": int(cls[j]), "score": float(scores[j]),
                             "xyxy": [float(v) for v in xy]})
    keep_all.sort(key=lambda d: -d["score"])
    return keep_all


COLORS = [(255, 64, 64), (64, 255, 64), (64, 128, 255), (255, 255, 64), (255, 64, 255),
          (64, 255, 255), (255, 160, 64), (160, 64, 255), (64, 255, 160), (200, 200, 200)]


def draw_boxes(rgb, boxes, lw=3):
    img = rgb.copy()
    h, w = img.shape[:2]
    for b in boxes:
        x1, y1, x2, y2 = [int(round(v)) for v in b["xyxy"]]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w - 1, x2), min(h - 1, y2)
        if x2 <= x1 or y2 <= y1:
            continue
        col = COLORS[b["cls"] % len(COLORS)]
        img[y1:y1 + lw, x1:x2 + 1] = col
        img[y2 - lw + 1:y2 + 1, x1:x2 + 1] = col
        img[y1:y2 + 1, x1:x1 + lw] = col
        img[y1:y2 + 1, x2 - lw + 1:x2 + 1] = col
    return img


def write_png(path, rgb):
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        f.write(chunk(b"IEND", b""))


def main():
    feat = np.load(os.path.join(HERE, "feat_q19.npy")).astype(np.float32)
    base = np.load(os.path.join(HERE, "base_q19.npy"))
    ref = np.load(os.path.join(HERE, "onnx_out_trained_q19.npy"))
    npts = sum(h * w for _, h, w in LEVELS)
    print("feat", feat.shape, "| base", base.shape, "| ref", ref.shape, "| N", npts, flush=True)

    rk = RKNNLite(verbose=False)
    print("load_rknn:", rk.load_rknn(RKNN_PATH), flush=True)
    print("init_runtime:", rk.init_runtime(), flush=True)

    outs, used_fmt, ms = None, None, None
    for fmt in ("nchw", "nhwc"):
        try:
            t0 = time.perf_counter()
            o = rk.inference(inputs=[feat], data_format=fmt)
            dt = (time.perf_counter() - t0) * 1000
            o = np.asarray(o[0]).reshape(-1, NC + 4)
            dmax = float(np.abs(o - ref.reshape(-1, NC + 4)).max())
            print(f"  data_format={fmt}: out {o.shape} | vs ONNX max|Δ|={dmax:.5f} | {dt:.1f} ms",
                  flush=True)
            if outs is None or dmax < 1e-3:
                outs, used_fmt, ms = o, fmt, dt
            if dmax < 1e-3:
                break
        except Exception as e:
            print(f"  data_format={fmt} FAIL:", type(e).__name__, str(e)[:160], flush=True)
    if outs is None:
        print("BOTH data_format FAILED")
        return 1

    outs = outs.astype(np.float32)
    counts = {c: len(decode(outs, c)) for c in CONFS}
    boxes = decode(outs, DRAW_CONF)
    vis = draw_boxes(base, boxes)
    write_png(OUT_PNG, vis)

    report = {"rknn": os.path.basename(RKNN_PATH), "data_format": used_fmt,
              "n_points": npts, "iou": IOU, "draw_conf": DRAW_CONF,
              "counts_by_conf": {str(c): counts[c] for c in CONFS},
              "infer_ms": round(ms, 1),
              "vs_onnx_max_abs": float(np.abs(outs - ref.reshape(-1, NC + 4)).max()),
              "vs_onnx_mean_abs": float(np.abs(outs - ref.reshape(-1, NC + 4)).mean()),
              "boxes_drawn": len(boxes), "boxes": boxes[:60]}
    with open(OUT_JSON, "w") as f:
        json.dump(report, f, indent=1)

    print("board distinct-score counts:", {c: counts[c] for c in CONFS}, flush=True)
    for b in boxes[:12]:
        print("   cls", b["cls"], round(b["score"], 3), [round(v, 1) for v in b["xyxy"]], flush=True)
    print("vs ONNX max|Δ|:", round(report["vs_onnx_max_abs"], 5), "| infer", round(ms, 1), "ms",
          flush=True)
    print("BOARD_TRAINED_OK", OUT_PNG, flush=True)
    rk.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
