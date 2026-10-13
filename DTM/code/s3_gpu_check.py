#!/usr/bin/env python3
"""S3：验证「CPU 编译的 detectron2 能否在 GPU 上跑教师网」。

依据：本版 detectron2 的 ROIAlign 用 `torchvision.ops.roi_align`、NMS 用
`torchvision.ops.batched_nms`（见 detectron2/layers/{roi_align,nms}.py 头部），
故 **不需要 CUDA 编译的 `_C`** —— 只需把模型搬到 GPU。

判据：用 sweep/q19 的解码特征跑 feature 模式，GPU 结果应与 CPU 版
（out/sweep/report.json 里 q19 的 12 框 / top1=cup 0.591）一致。

用法（147）：
  cd /home/jiangfeng/jf_app/dcm_step1
  CUDA_VISIBLE_DEVICES=0 PYTHONPATH=/home/jiangfeng/jf_app/dcm_step1/libs \
  /home/jiangfeng/anaconda3/envs/dtm-v/bin/python -u s3_gpu_check.py
"""

import argparse
import os
import sys

import torch

LIBS = "/home/jiangfeng/jf_app/dcm_step1/libs"
RM_SCRIPTS = "/home/jiangfeng/jf_app/DTM-V-1.5/scripts/DCM_Datasets_Feature_Inference_Scripts"
sys.path.insert(0, LIBS)
sys.path.insert(0, RM_SCRIPTS)
sys.path.insert(0, "/home/jiangfeng/jf_app/dcm_step1")

from detectron2.checkpoint import DetectionCheckpointer          # noqa: E402
from detectron2.config import get_cfg                            # noqa: E402
from detectron2.data import DatasetMapper                        # noqa: E402
from detectron2.modeling import build_model                      # noqa: E402
from detectron2.structures import ImageList                      # noqa: E402
from Adapter import build_feature_adapter                        # noqa: E402
import s1_feature_domain_det as s1                               # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--config", default="/home/jiangfeng/jf_app/dcm_step1/src/detectron2/configs/"
                                        "COCO-Detection/faster_rcnn_X_101_32x8d_FPN_3x.yaml")
    ap.add_argument("--weights", default="/home/jiangfeng/jf_app/DTM-V-1.5/packages/Model/"
                                         "Detector_model/model_final_68b088.pkl")
    ap.add_argument("--adapter_ckpt", default="/home/jiangfeng/jf_app/DTM-V-1.5/packages/Model/"
                                              "MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth")
    ap.add_argument("--feature", default="data/sweep/q19/res2_hq_final_1.pt")
    ap.add_argument("--image", default="data/img_1333x750.png")
    args = ap.parse_args()
    dev = torch.device(args.device)
    print("torch", torch.__version__, "cuda_avail", torch.cuda.is_available(), "->", args.device, flush=True)

    cfg = get_cfg()
    cfg.merge_from_file(args.config)
    cfg.MODEL.WEIGHTS = args.weights
    cfg.MODEL.DEVICE = args.device
    cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = 0.05
    model = build_model(cfg)
    DetectionCheckpointer(model).load(args.weights)
    model.eval().to(dev)
    adapter = build_feature_adapter(128, 256)
    ck = torch.load(args.adapter_ckpt, map_location=dev)
    adapter.load_state_dict(ck["feature_adapter"])
    adapter.eval().to(dev)
    print("model + adapter on", dev, flush=True)

    inputs = [DatasetMapper(cfg, is_train=None)(dict(file_name=args.image))]
    images = [(x["image"].to(dev) - model.pixel_mean) / model.pixel_std for x in inputs]
    images = ImageList.from_tensors(images, model.backbone.size_divisibility)
    print("image_sizes", images.image_sizes, flush=True)

    res2 = torch.load(args.feature, map_location=dev).to(dev)
    with torch.no_grad():
        res2 = adapter(res2)
        r3 = model.backbone.bottom_up.res3(res2)
        r4 = model.backbone.bottom_up.res4(r3)
        r5 = model.backbone.bottom_up.res5(r4)
        fpn = s1.my_fpn_forward({"res2": res2, "res3": r3, "res4": r4, "res5": r5}, model)
        proposals, _ = model.proposal_generator(images, fpn)
        inst_f, _ = model.roi_heads(images, fpn, proposals, None)

    sc = inst_f[0].scores.tolist()
    cl = inst_f[0].pred_classes.tolist()
    n = sum(1 for s in sc if float(s) >= 0.05)
    top = [[s1.COCO80[int(c)], round(float(s), 3)]
           for s, c in sorted(zip(sc, cl), reverse=True)[:10]]
    print(f"device={args.device}  boxes(>0.05)={n}")
    print("top10 =", top)
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
