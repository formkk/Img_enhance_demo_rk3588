"""d3：开发机侧——按他们 `inferer.process_image` 的口径构造检测器输入，并产出 ONNX 参考结果。

复刻的链路（`mq_td_demo/yolov6/core/inferer.py`）：
    letterbox(img, 640, stride=32) -> BGR->RGB、HWC->CHW -> png2bayer_patten -> 4x320x320（0..255 float）

产出（写到 --outdir，默认 DTM/out_d1）：
    net_in.npz          检测器网络输入（4x320x320 float32）+ letterbox 元信息
    frame_rgb_uint8.npy 原帧（RGB，uint8）——给板端画框用
    onnx_out.npy        ONNX（onnxruntime）原始输出 (1,8400,25) —— 交叉校验基准
    ref_boxes.json      ONNX 参考的解码+NMS 结果（与板端逐项对比）

用法：
    ~/anaconda3/envs/dtm/bin/python DTM/code/d3_prep_detect_input.py
"""

import argparse
import json
import os
import sys

import cv2
import numpy as np

VOC20 = ['aeroplane', 'bicycle', 'bird', 'boat', 'bottle', 'bus', 'car', 'cat', 'chair', 'cow',
         'diningtable', 'dog', 'horse', 'motorbike', 'person', 'pottedplant', 'sheep', 'sofa',
         'train', 'tvmonitor']

IMG_SIZE = 640          # 他们的 --img-size 默认值
STRIDE = 32
CONF = 0.2              # 他们的默认 conf_thres
IOU = 0.45              # 他们的默认 iou_thres
MAX_DET = 300
PAD_VALUE = 114


def letterbox(im, new_shape=640, stride=32, color=PAD_VALUE):
    """YOLOv5/YOLOv6 风格 letterbox（copy 自其 utils 逻辑）。"""
    shape = im.shape[:2]
    r = min(new_shape / shape[0], new_shape / shape[1])
    new_unpad = (int(round(shape[1] * r)), int(round(shape[0] * r)))  # (w, h)
    dw, dh = new_shape - new_unpad[0], new_shape - new_unpad[1]
    # 他们调用 letterbox(..., auto=False) ⇒ 走 YOLOv5 的 "pad 到方形" 分支（不做 mod stride）
    dw, dh = dw / 2, dh / 2
    im = cv2.resize(im, new_unpad, interpolation=cv2.INTER_LINEAR) if (shape[1], shape[0]) != new_unpad else im
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im = cv2.copyMakeBorder(im, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(color,) * im.shape[2])
    return im, r, (left, top)


def png2bayer_patten(im):
    """== 他们 `mqzx_process.py::png2bayer_patten`（4ch 由 RGB 子采样构造）。"""
    R, G, B = im[0:1], im[1:2], im[2:3]
    R0 = R[:, 0::2, 0::2]
    G0 = G[:, 0::2, 0::2]
    G1 = G[:, 1::2, 1::2]
    B0 = B[:, 0::2, 0::2]
    return np.concatenate([R0, G0, B0, G1], axis=0)


DIV_MAX = 255.0


def compute_mean(net4):
    """== 他们 `inferer.compute_mean`。**注意**：必须先拿到 float（见 `mqzx_data_process`），
    否则 uint8 的 `G0+G1` 会**溢出**（实测会把 134.7 算成 109.7 ⇒ 误触发提亮）。"""
    R, G0, B, G1 = net4[0], net4[1], net4[2], net4[3]
    G = (G0 + G1) / 2
    Y = 0.59 * G + 0.3 * R + 0.11 * B
    return float(np.mean(Y))


def mqzx_data_process(net4):
    """== 他们 `inferer.mqzx_data_process(image)`：4 通道 -> 亮度归一化。

    他们原码 `compute_mean(image * (255.0 / div_max))` 里的 `* 1.0` **不是冗余**：
    它把 uint8 变成 float，避免 `G0+G1` 溢出（本机实测溢出会把 134.71 算成 109.74
    ⇒ `ratio_` 误判为 1.093 ⇒ 平白提亮、虚警增多）。此处照抄该行为。
    `ratio_ = max(120/均值, 1)`：帧均值 ≥120 ⇒ **逐位恒等**（只 ÷255）。
    """
    dtype_ = net4.dtype
    div_max = 65535.0 if dtype_.name == "uint16" else DIV_MAX
    img_mean = compute_mean(net4 * (DIV_MAX / div_max))
    ratio = max(120.0 / img_mean, 1.0)
    img = net4.astype(np.float32) / div_max
    if ratio == 1.0:
        return img
    im1 = img * ratio
    im2 = img ** (1.0 / np.log2(ratio * 2.0))
    mask = im1 >= 0.5
    return (im2 * mask) + (im1 * (~mask))


def process_image(img_src):
    """复刻他们 `process_image`：letterbox -> BGR2RGB/CHW -> png2bayer_patten -> mqzx_data_process。"""
    image = letterbox(img_src, IMG_SIZE, stride=STRIDE)[0]
    image = image.transpose((2, 0, 1))[::-1]        # HWC->CHW, BGR->RGB
    return mqzx_data_process(png2bayer_patten(image)).astype(np.float32)  # (4,320,320) in [0,1]


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
    """pred: (8400,25) => [x,y,w,h,1,cls(20)]（bbox 已是 letterbox 像素、xywh 中心式）。"""
    conf_thres = CONF if conf_thres is None else conf_thres
    boxes, obj, cls = pred[:, :4], pred[:, 4], pred[:, 5:]
    scores_all = cls * obj[:, None]
    cls_id = scores_all.argmax(1)
    scores = scores_all[np.arange(scores_all.shape[0]), cls_id]
    m = scores > conf_thres
    boxes, scores, cls_id = boxes[m], scores[m], cls_id[m]
    if boxes.shape[0] == 0:
        return []
    # xywh(中心) -> xyxy(letterbox 坐标)
    xy = np.empty_like(boxes)
    xy[:, 0] = boxes[:, 0] - boxes[:, 2] / 2
    xy[:, 1] = boxes[:, 1] - boxes[:, 3] / 2
    xy[:, 2] = boxes[:, 0] + boxes[:, 2] / 2
    xy[:, 3] = boxes[:, 1] + boxes[:, 3] / 2
    # letterbox -> 原图
    xy[:, [0, 2]] = (xy[:, [0, 2]] - pad[0]) / ratio
    xy[:, [1, 3]] = (xy[:, [1, 3]] - pad[1]) / ratio
    xy[:, [0, 2]] = xy[:, [0, 2]].clip(0, orig_hw[1] - 1)
    xy[:, [1, 3]] = xy[:, [1, 3]].clip(0, orig_hw[0] - 1)
    out = []
    for c in np.unique(cls_id):
        idx = np.where(cls_id == c)[0]
        keep = nms_np(xy[idx], scores[idx], iou_thres)[:max_det]
        for k in keep:
            j = idx[k]
            out.append({"cls": int(cls_id[j]), "name": VOC20[int(cls_id[j])],
                        "score": float(scores[j]), "xyxy": [float(v) for v in xy[j]]})
    out.sort(key=lambda d: -d["score"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", default="hq_optimize/captures/noise_diag/hq_final_1.png")
    ap.add_argument("--onnx", default="DTM/assets/best_ckpt_1_voc20_bayer4ch_320.onnx")
    ap.add_argument("--outdir", default="DTM/out_d1")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    bgr = cv2.imread(args.frame, cv2.IMREAD_COLOR)
    assert bgr is not None, f"读不到帧: {args.frame}"
    orig_hw = bgr.shape[:2]
    net = process_image(bgr)
    print("frame:", args.frame, "orig(h,w)=", orig_hw, "| net:", net.shape, net.dtype,
          "| min/max", float(net.min()), float(net.max()))

    # letterbox 元信息（重算一次，保持与 process_image 内部一致）
    _, ratio, pad = letterbox(bgr, IMG_SIZE, stride=STRIDE)

    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    np.save(os.path.join(args.outdir, "frame_rgb_uint8.npy"), rgb)
    np.savez(os.path.join(args.outdir, "net_in.npz"), net=net, ratio=ratio,
             pad=np.array(pad, np.int64), orig_hw=np.array(orig_hw, np.int64))

    import onnxruntime as ort
    sess = ort.InferenceSession(args.onnx, providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0]
    print("onnx input:", inp.name, inp.shape, inp.type)
    out = sess.run(None, {inp.name: net[None].astype(np.float32)})[0]
    print("onnx out:", out.shape, "| boxes[0:3]:", np.round(out[0, :3, :5], 2).tolist())
    np.save(os.path.join(args.outdir, "onnx_out.npy"), out)

    ref = decode_nms(out[0].astype(np.float32), ratio, pad, orig_hw)
    with open(os.path.join(args.outdir, "ref_boxes.json"), "w") as f:
        json.dump({"frame": args.frame, "orig_hw": list(orig_hw), "ratio": ratio,
                   "pad": list(pad), "conf": CONF, "iou": IOU, "boxes": ref}, f, indent=1)

    # 本地参考叠图（仅目视用；板端有自己的叠图）
    vis = bgr.copy()
    for b in ref:
        x1, y1, x2, y2 = [int(round(v)) for v in b["xyxy"]]
        cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 0, 255), 2)
    cv2.imwrite(os.path.join(args.outdir, "ref_overlay.png"), vis)

    print(f"ref boxes: {len(ref)}")
    for b in ref[:12]:
        print("   ", b["name"], round(b["score"], 3), [round(v, 1) for v in b["xyxy"]])
    print("PREP_OK", args.outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
