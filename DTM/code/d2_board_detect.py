"""d2：板端单帧检测验证（板上跑，`rknnlite` + numpy，无 cv2/PIL）。

输入（由 d3 在开发机侧备好，随脚本一起 scp 到板端同目录）：
    best_ckpt_1_voc20_bayer4ch_320.rknn   检测器（RKNN FP16）
    net_in.npz                            网络输入 4x320x320 float32 + letterbox 元信息
    onnx_out.npy                          ONNX 参考输出 (1,8400,25)（FP32）——数值交叉校验
    ref_boxes.json                        ONNX 参考的解码+NMS 结果 —— 逐项对比
    frame_rgb_uint8.npy                   原帧 RGB uint8 —— 画框用

产出：
    board_det_boxes.png                   叠框结果
    board_det_boxes.json                  板端检出列表 + 与参考的差异 + 耗时

用法（板端）：python3 d2_board_detect.py
"""

import json
import os
import struct
import time
import zlib

import numpy as np
from rknnlite.api import RKNNLite

HERE = os.path.dirname(os.path.abspath(__file__))
RKNN_PATH = os.path.join(HERE, "best_ckpt_1_voc20_bayer4ch_320.rknn")
OUT_PNG = os.path.join(HERE, "board_det_boxes.png")
OUT_JSON = os.path.join(HERE, "board_det_boxes.json")

VOC20 = ['aeroplane', 'bicycle', 'bird', 'boat', 'bottle', 'bus', 'car', 'cat', 'chair', 'cow',
         'diningtable', 'dog', 'horse', 'motorbike', 'person', 'pottedplant', 'sheep', 'sofa',
         'train', 'tvmonitor']
CONF, IOU, MAX_DET = 0.2, 0.45, 300


# ---------- 解码 / NMS（与 d3 同口径） ----------
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


def decode_nms(pred, ratio, pad, orig_hw, conf_thres=None, iou_thres=IOU, max_det=MAX_DET):
    conf_thres = CONF if conf_thres is None else conf_thres
    boxes, obj, cls = pred[:, :4], pred[:, 4], pred[:, 5:]
    scores_all = cls * obj[:, None]
    cls_id = scores_all.argmax(1)
    scores = scores_all[np.arange(scores_all.shape[0]), cls_id]
    m = scores > CONF
    boxes, scores, cls_id = boxes[m], scores[m], cls_id[m]
    if boxes.shape[0] == 0:
        return []
    xy = np.empty_like(boxes)
    xy[:, 0] = boxes[:, 0] - boxes[:, 2] / 2
    xy[:, 1] = boxes[:, 1] - boxes[:, 3] / 2
    xy[:, 2] = boxes[:, 0] + boxes[:, 2] / 2
    xy[:, 3] = boxes[:, 1] + boxes[:, 3] / 2
    xy[:, [0, 2]] = (xy[:, [0, 2]] - pad[0]) / ratio
    xy[:, [1, 3]] = (xy[:, [1, 3]] - pad[1]) / ratio
    xy[:, [0, 2]] = xy[:, [0, 2]].clip(0, orig_hw[1] - 1)
    xy[:, [1, 3]] = xy[:, [1, 3]].clip(0, orig_hw[0] - 1)
    out = []
    for c in np.unique(cls_id):
        idx = np.where(cls_id == c)[0]
        keep = nms_np(xy[idx], scores[idx], IOU)[:MAX_DET]
        for k in keep:
            j = idx[k]
            out.append({"cls": int(cls_id[j]), "name": VOC20[int(cls_id[j])],
                        "score": float(scores[j]), "xyxy": [float(v) for v in xy[j]]})
    out.sort(key=lambda d: -d["score"])
    return out


def iou_xyxy(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, x2 - x1), max(0.0, y2 - y1)
    inter = iw * ih
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / (ua + 1e-9)


# ---------- 画框 / 写 PNG（纯 numpy + zlib，板上无 cv2/PIL） ----------
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


COLORS = [(255, 64, 64), (64, 255, 64), (64, 128, 255), (255, 255, 64), (255, 64, 255),
          (64, 255, 255), (255, 160, 64), (160, 64, 255), (64, 255, 160), (200, 200, 200)]


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
    d = np.load(os.path.join(HERE, "net_in.npz"))
    net = d["net"]
    ratio = float(d["ratio"])
    pad = d["pad"].astype(np.int64)
    orig_hw = d["orig_hw"].astype(np.int64)
    onnx_out = np.load(os.path.join(HERE, "onnx_out.npy"))
    ref = json.load(open(os.path.join(HERE, "ref_boxes.json")))["boxes"]
    frame = np.load(os.path.join(HERE, "frame_rgb_uint8.npy"))
    print("input:", net.shape, net.dtype, "| orig(h,w)=", tuple(orig_hw), "| ref boxes:", len(ref))

    rk = RKNNLite(verbose=False)
    print("load_rknn:", rk.load_rknn(RKNN_PATH))
    print("init_runtime:", rk.init_runtime())

    outs = None
    for fmt in ("nchw", "nhwc"):
        try:
            t0 = time.perf_counter()
            o = rk.inference(inputs=[net[None]], data_format=fmt)
            dt = time.perf_counter() - t0
            o = np.asarray(o[0]).reshape(1, -1, 25)
            dmax = float(np.abs(o - onnx_out).max())
            print(f"  data_format={fmt}: out {o.shape} | vs ONNX max|Δ|={dmax:.5f} | {dt*1000:.1f} ms")
            if outs is None or dmax < 1e-3:
                outs, used_fmt = o, fmt
            if dmax < 1e-3:
                break
        except Exception as e:
            print(f"  data_format={fmt} FAIL:", type(e).__name__, str(e)[:120])

    pred = outs[0].astype(np.float32)
    boxes = decode_nms(pred, ratio, pad, orig_hw)

    # 与 ONNX 参考逐项对比（按类别+最高 IoU 配对）
    matched = 0
    for r in ref:
        best = 0.0
        for b in boxes:
            if b["cls"] == r["cls"]:
                best = max(best, iou_xyxy(r["xyxy"], b["xyxy"]))
        if best >= 0.5:
            matched += 1

    vis = draw_boxes(frame, boxes)
    write_png(OUT_PNG, vis)
    report = {
        "rknn": os.path.basename(RKNN_PATH),
        "data_format": used_fmt,
        "input_shape": list(net.shape),
        "orig_hw": [int(v) for v in orig_hw],
        "ratio": ratio, "pad": [int(v) for v in pad],
        "conf": CONF, "iou": IOU,
        "vs_onnx_max_abs": float(np.abs(outs - onnx_out).max()),
        "vs_onnx_mean_abs": float(np.abs(outs - onnx_out).mean()),
        "board_boxes": len(boxes),
        "ref_boxes": len(ref),
        "matched_iou50": matched,
        "boxes": boxes,
    }
    with open(OUT_JSON, "w") as f:
        json.dump(report, f, indent=1)

    print(f"board boxes: {len(boxes)} | ref: {len(ref)} | matched(IoU>=0.5): {matched}")
    for b in boxes[:12]:
        print("   ", b["name"], round(b["score"], 3), [round(v, 1) for v in b["xyxy"]])
    print("vs ONNX max|Δ|:", round(report["vs_onnx_max_abs"], 5))
    print("BOARD_DET_OK", OUT_PNG)
    rk.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
