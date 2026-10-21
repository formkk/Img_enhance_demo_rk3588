#!/usr/bin/env python3
"""b3c_eval_official.py —— 特征域检测器的**官方口径 mAP** 评测（COCO 标准 mAP@[.5:.95]）。

口径来源（本文件为**重写版**，2026-10-15）
----------------------------------------
原脚本 `jf_app/dcm_c/b3c_eval_official.py` 已于 2026-10-15 随 `jf_app/*` 被清理 ⇒ 按记录重写。
参照物（均在本仓库内，未丢失）：
  · `DTM/DTM-V-1.5/scripts/DCM_Datasets_Feature_Inference_Scripts/inference_task.py`
      L306-331 = 喂特征的核心前向（`DatasetMapper` → `ImageList` → adapter → bottom_up.res3/4/5
      → `my_fpn_forward` → `proposal_generator` → `roi_heads` → `_postprocess`）
  · 同目录 `eval.py` = `COCOeval(cocoGt, cocoDt, 'bbox')`（本脚本用同一 API 与同一指标列）
  · `DTM/code/s1_feature_domain_det.py` = `my_fpn_forward` 的抄写本

与官方评测的**唯一差异**（如实声明）：官方 `eval.py` 读一份"只含被评图片"的 GT json；
本脚本**现场生成**该 GT（从 `instances_val2017.json` 里筛出 `--img-dir` 内的 500 张 id）
⇒ 指标口径（COCOeval / bbox / 12 项 stats）与官方一致，**mAP 数值可直接与旧记录对比**。

指标列（与 §8.21(二)(四) 表一致）：
  mAP@[.5:.95] = stats[0]、AP@0.50 = stats[1]、AP@0.75 = stats[2]、AR@100 = stats[8]

用法（147）
----------
  CUDA_VISIBLE_DEVICES=0 PYTHONPATH=<d2src>:<RM_SCRIPTS> \
  /home/jiangfeng/anaconda3/envs/dtm-v/bin/python -u /path/b3c_eval_official.py \
      --img-dir /home/jiangfeng/datasets/coco_val_500 \
      --res2-dir <coco500 的 q31 res2 目录> \
      --ann /home/jiangfeng/datasets/coco2017/annotations/instances_val2017.json \
      --detector-weights <model_final_68b088.pkl> --adapter-model-path <codec.pth> \
      --ckpt <ep6.pth | 省略=官方权重原样> [--identity] --out <评测输出目录>
"""

import argparse
import json
import os
import sys

import torch

from detectron2.config import get_cfg
from detectron2.data import DatasetMapper
from detectron2.modeling import build_model
from detectron2.checkpoint import DetectionCheckpointer
from detectron2.structures import ImageList
from Adapter import build_feature_adapter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from b2c_finetune import DCMBackbone, build_d_module, my_fpn_forward  # noqa: E402

XMIN, XMAX = 800, 1333


def setup_cfg(args):
    cfg = get_cfg()
    cfg.merge_from_file(args.config_file)
    cfg.MODEL.WEIGHTS = args.detector_weights
    cfg.MODEL.DEVICE = args.device
    cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = args.score_thresh
    cfg.INPUT.MIN_SIZE_TEST = XMIN
    cfg.INPUT.MAX_SIZE_TEST = XMAX
    cfg.freeze()
    return cfg


def build_gt_json(ann_in, img_ids, out_path):
    """筛出被评图片的 GT（COCOeval 的 cocoGt）。"""
    with open(ann_in, "r", encoding="utf-8") as fh:
        ann = json.load(fh)
    ids = {int(i) for i in img_ids}
    out = {
        "info": ann.get("info", {}),
        "licenses": ann.get("licenses", []),
        "images": [im for im in ann["images"] if int(im["id"]) in ids],
        "annotations": [a for a in ann["annotations"] if int(a["image_id"]) in ids],
        "categories": ann["categories"],
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    print(f"[gt] {len(out['images'])} 图 / {len(out['annotations'])} 标注 ⇒ {out_path}", flush=True)
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--img-dir", required=True, help="被评图片目录（coco_val_500）")
    ap.add_argument("--res2-dir", required=True, help="同批图片的 q31 res2 目录（res2_<imgid>.pt）")
    ap.add_argument("--ann", required=True, help="instances_val2017.json")
    ap.add_argument("--config-file",
                    default="configs/COCO-Detection/faster_rcnn_X_101_32x8d_FPN_3x.yaml")
    ap.add_argument("--detector-weights", required=True)
    ap.add_argument("--adapter-model-path", required=True)
    ap.add_argument("--ckpt", default="", help="微调后的 ep<N>.pth（省略=官方权重原样）")
    ap.add_argument("--identity", action="store_true",
                    help="D 保持零初始化（恒等）⇒ 官方锚点对照")
    ap.add_argument("--score-thresh", type=float, default=0.05)
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    files = sorted(f for f in os.listdir(args.img_dir) if f.lower().endswith((".jpg", ".png")))
    img_ids = [f.split(".")[0] for f in files]
    gt_path = build_gt_json(args.ann, img_ids, os.path.join(args.out, "coco500_gt.json"))
    # COCO `category_id` 有空洞（1..90）⇒ 模型输出的是 **0..79 连续索引** ⇒ 落盘前必须反查回原 id
    with open(args.ann, "r", encoding="utf-8") as fh:
        cats = json.load(fh)["categories"]
    inv_cat = {i: int(c["id"]) for i, c in enumerate(cats)}

    cfg = setup_cfg(args)
    model = build_model(cfg)
    DetectionCheckpointer(model).load(cfg.MODEL.WEIGHTS)
    adapter = build_feature_adapter(128, 256)
    ck = torch.load(args.adapter_model_path, map_location="cpu")
    adapter.load_state_dict(ck["feature_adapter"])
    model.backbone = DCMBackbone(model.backbone, adapter, build_d_module())
    if args.ckpt:
        sd = torch.load(args.ckpt, map_location="cpu")
        model.load_state_dict(sd)
        print(f"[ckpt] loaded {args.ckpt}", flush=True)
    elif not args.identity:
        print("[ckpt] 官方权重原样（未加载微调权重）", flush=True)
    model.to(torch.device(args.device)).eval()
    mapper = DatasetMapper(cfg, is_train=False)

    dets = []
    with torch.no_grad():
        for f in files:
            iid = int(f.split(".")[0])
            inputs = [mapper(dict(file_name=os.path.join(args.img_dir, f)))]
            images = [x["image"].to(args.device) for x in inputs]
            images = [(x - model.pixel_mean) / model.pixel_std for x in images]
            images = ImageList.from_tensors(images, model.backbone.size_divisibility)
            res2 = torch.load(os.path.join(args.res2_dir, f"res2_{f.split('.')[0]}.pt"),
                              map_location=args.device).float()
            model.backbone.set_res2(res2)
            # **（2026-10-15 修）** 不能 `model(images)`：`GeneralizedRCNN.forward/inference` 期望的是
            # **batched_inputs（dict 列表）**，传 ImageList 会在 `preprocess_image` 里对 3-D 张量取 `x["image"]`
            # ⇒ `IndexError: too many indices for tensor of dimension 3`（本次踩到）。
            # 正确做法 = **照抄官方 `inference_task.py` L328-331：直接调三个子模块**。
            features = model.backbone(images.tensor)          # adapter → D → res3/4/5 → my_fpn_forward
            proposals, _ = model.proposal_generator(images, features)
            results, _ = model.roi_heads(images, features, proposals, None)
            outputs = model._postprocess(results, inputs, images.image_sizes)
            pred = outputs[0]
            inst = pred["instances"].to("cpu")
            for (x1, y1, x2, y2), s, c in zip(inst.pred_boxes.tensor.tolist(),
                                              inst.scores.tolist(),
                                              inst.pred_classes.tolist()):
                dets.append({"image_id": iid, "category_id": inv_cat[int(c)],
                             "bbox": [x1, y1, x2 - x1, y2 - y1], "score": float(s)})
    res_path = os.path.join(args.out, "infer_json.json")
    with open(res_path, "w", encoding="utf-8") as fh:
        json.dump(dets, fh)
    print(f"[infer] {len(dets)} 框 / {len(files)} 图 ⇒ {res_path}", flush=True)

    from pycocotools.coco import COCO
    from pycocotools.cocoeval import COCOeval
    coco_gt = COCO(gt_path)
    coco_dt = coco_gt.loadRes(res_path)
    ev = COCOeval(coco_gt, coco_dt, "bbox")
    ev.evaluate()
    ev.accumulate()
    ev.summarize()
    st = ev.stats
    # **（2026-10-16 S17d）** 追加**面积分档**（`st[3..5]` = small/medium/large AP）——
    # 旧记录 §8.21 ep24 的短板是 `small = 0.305`，需与本次各轮同口径比较。
    line = (f"RESULT mAP@[.5:.95]={st[0]:.4f} AP@0.50={st[1]:.4f} "
            f"AP@0.75={st[2]:.4f} AR@100={st[8]:.4f} "
            f"AP_small={st[3]:.4f} AP_med={st[4]:.4f} AP_large={st[5]:.4f}")
    print(line, flush=True)
    with open(os.path.join(args.out, "summary.txt"), "w", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
