#!/usr/bin/env python3
"""S1：用 RM 的权威任务网（detectron2 Faster R-CNN X-101-32x8d-FPN 3x）在**解码特征**上出框。

调用口径严格抄自 RM 自己的脚本：
  scripts/DCM_Datasets_Feature_Inference_Scripts/inference_task.py
      L306-331（喂特征的核心 8 行）+ L234-268（my_fpn_forward）+ Adapter.py（Conv2d(128→256,k3,p1)+ReLU）

两种模式（都跑，互为对照）：
  · pixel   ：把**原图**喂标准检测器          ⇒ 该帧在这套检测器下的"像素域上限"
  · feature ：把**解码特征**经 adapter 喂 FPN ⇒ 我们要回答的问题
  ⚠️ 特征模式**不代表** RM 的完整评测流程（此处不做 COCO json/eval），只出框供目视与计数。

运行（147 上，CPU-only detectron2）：
  cd /home/jiangfeng/jf_app/dcm_step1
  CUDA_VISIBLE_DEVICES="" PYTHONPATH=/home/jiangfeng/jf_app/dcm_step1/libs \
  /home/jiangfeng/anaconda3/envs/dtm-v/bin/python s1_feature_domain_det.py \
      --feature data/q03/res2_hq_final_1.pt --image data/img_1333x750.png \
      --rec data/rec_q03.png --out out/q03
"""

import argparse
import json
import os
import sys

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image, ImageDraw, ImageFont

LIBS = "/home/jiangfeng/jf_app/dcm_step1/libs"
RM_SCRIPTS = "/home/jiangfeng/jf_app/DTM-V-1.5/scripts/DCM_Datasets_Feature_Inference_Scripts"
sys.path.insert(0, LIBS)
sys.path.insert(0, RM_SCRIPTS)

from detectron2.checkpoint import DetectionCheckpointer          # noqa: E402
from detectron2.config import get_cfg                            # noqa: E402
from detectron2.data import DatasetMapper                        # noqa: E402
from detectron2.modeling import build_model                      # noqa: E402
from detectron2.structures import ImageList                      # noqa: E402
from Adapter import build_feature_adapter                        # noqa: E402

COCO80 = ["person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
          "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
          "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
          "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
          "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
          "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
          "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
          "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
          "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
          "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
          "toothbrush"]
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


def my_fpn_forward(bottom_up_features, model):
    """抄自 RM inference_task.py L234-268（只跑 FPN 的 lateral/top-down，跳过 backbone stem）。"""
    x = [bottom_up_features[f] for f in model.backbone.in_features[::-1]]
    results = []
    prev_features = model.backbone.lateral_convs[0](x[0])
    results.append(model.backbone.output_convs[0](prev_features))
    for features, lateral_conv, output_conv in zip(
            x[1:], model.backbone.lateral_convs[1:], model.backbone.output_convs[1:]):
        top_down_features = F.interpolate(prev_features, scale_factor=2, mode="nearest")
        lateral_features = lateral_conv(features)
        prev_features = lateral_features + top_down_features
        if model.backbone._fuse_type == "avg":
            prev_features /= 2
        results.insert(0, output_conv(prev_features))
    if model.backbone.top_block is not None:
        top_block_in_feature = bottom_up_features.get(model.backbone.top_block.in_feature, None)
        if top_block_in_feature is None:
            top_block_in_feature = results[model.backbone._out_features.index(
                model.backbone.top_block.in_feature)]
        results.extend(model.backbone.top_block(top_block_in_feature))
    assert len(model.backbone._out_features) == len(results)
    return dict(zip(model.backbone._out_features, results))


def draw(rec_path, inst, out_path, title, box_color):
    im = Image.open(rec_path).convert("RGB")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT, 22) if os.path.exists(FONT) else None
    d.text((10, 8), title, fill=(255, 255, 255), font=f)
    keep = 0
    boxes = inst.pred_boxes.tensor.tolist()
    scores = inst.scores.tolist()
    classes = inst.pred_classes.tolist()
    for (x1, y1, x2, y2), s, c in zip(boxes, scores, classes):
        if float(s) < 0.05:
            continue
        keep += 1
        d.rectangle([x1, y1, x2, y2], outline=box_color, width=3)
        d.text((x1 + 2, max(2, y1 - 26)), f"{COCO80[int(c)]} {float(s):.2f}", fill=box_color, font=f)
    im.save(out_path)
    return keep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/home/jiangfeng/jf_app/dcm_step1/src/detectron2/configs/"
                                        "COCO-Detection/faster_rcnn_X_101_32x8d_FPN_3x.yaml")
    ap.add_argument("--weights", default="/home/jiangfeng/jf_app/DTM-V-1.5/packages/Model/"
                                         "Detector_model/model_final_68b088.pkl")
    ap.add_argument("--adapter_ckpt", default="/home/jiangfeng/jf_app/DTM-V-1.5/packages/Model/"
                                              "MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth")
    ap.add_argument("--feature", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--rec", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    cfg = get_cfg()
    cfg.merge_from_file(args.config)
    cfg.MODEL.WEIGHTS = args.weights
    cfg.MODEL.DEVICE = "cpu"
    cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = 0.05
    device = torch.device("cpu")
    model = build_model(cfg)
    DetectionCheckpointer(model).load(args.weights)
    model.eval().to(device)
    adapter = build_feature_adapter(128, 256)
    ck = torch.load(args.adapter_ckpt, map_location=device)
    adapter.load_state_dict(ck["feature_adapter"])
    adapter.eval().to(device)
    print("model + adapter loaded (cpu)", flush=True)

    inputs = [DatasetMapper(cfg, is_train=None)(dict(file_name=args.image))]
    images = [x["image"].to(device) for x in inputs]
    images = [(x - model.pixel_mean) / model.pixel_std for x in images]
    images = ImageList.from_tensors(images, model.backbone.size_divisibility)
    print("image_sizes =", images.image_sizes, flush=True)

    report = {"feature": args.feature, "image": args.image, "tag": args.tag, "modes": {}}

    # ---- 模式 1：pixel（标准检测器，像素域上限）----
    with torch.no_grad():
        out_pix = model(inputs)
    inst_pix = out_pix[0]["instances"]
    n_pix = draw(args.rec, inst_pix, os.path.join(args.out, "overlay_pixel.png"),
                 f"PIXEL 域（原图喂检测器）{args.tag}", (60, 220, 60))
    report["modes"]["pixel"] = {"n>0.05": n_pix,
                                "top": [[COCO80[int(c)], round(float(s), 3)]
                                        for s, c in sorted(zip(inst_pix.scores.tolist(),
                                                               inst_pix.pred_classes.tolist()),
                                                           reverse=True)[:10]]}
    print("PIXEL 域:", report["modes"]["pixel"], flush=True)

    # ---- 模式 2：feature（解码特征 → adapter → FPN → RPN/ROI）----
    res2 = torch.load(args.feature, map_location=device).to(device)
    with torch.no_grad():
        res2 = adapter(res2)
        r3 = model.backbone.bottom_up.res3(res2)
        r4 = model.backbone.bottom_up.res4(r3)
        r5 = model.backbone.bottom_up.res5(r4)
        fpn = my_fpn_forward({"res2": res2, "res3": r3, "res4": r4, "res5": r5}, model)
        proposals, _ = model.proposal_generator(images, fpn)
        results, _ = model.roi_heads(images, fpn, proposals, None)
        out_feat = model._postprocess(results, inputs, images.image_sizes)
    inst_f = out_feat[0]["instances"]
    n_f = draw(args.rec, inst_f, os.path.join(args.out, "overlay_feature.png"),
               f"FEATURE 域（解码特征喂任务网）{args.tag}", (230, 60, 60))
    report["modes"]["feature"] = {"n>0.05": n_f, "n_proposals": int(len(proposals[0])),
                                  "top": [[COCO80[int(c)], round(float(s), 3)]
                                          for s, c in sorted(zip(inst_f.scores.tolist(),
                                                                 inst_f.pred_classes.tolist()),
                                                             reverse=True)[:10]]}
    print("FEATURE 域:", report["modes"]["feature"], flush=True)

    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("S1_DONE", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
