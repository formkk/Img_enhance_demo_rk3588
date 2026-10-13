#!/usr/bin/env python3
"""S2：码率↔检出 扫描（147 侧）——**同一份任务网只加载一次**，遍历多档解码特征统计 FEATURE 域框数。

推理口径与 s1 的 feature 模式逐字一致（Adapter → res3/4/5 → my_fpn_forward → RPN/ROI），
但省掉了 s1 的像素域模式与逐档重新加载模型（420MB × N 次）的开销。

用法（147 上，CPU-only detectron2）：
  cd /home/jiangfeng/jf_app/dcm_step1
  CUDA_VISIBLE_DEVICES="" PYTHONPATH=/home/jiangfeng/jf_app/dcm_step1/libs \
  /home/jiangfeng/anaconda3/envs/dtm-v/bin/python -u s2_feature_sweep.py \
      --image data/img_1333x750.png --sweep data/sweep --out out/sweep
"""

import argparse
import json
import os
import sys

import torch

LIBS = "/home/jiangfeng/jf_app/dcm_step1/libs"
RM_SCRIPTS = "/home/jiangfeng/jf_app/DTM-V-1.5/scripts/DCM_Datasets_Feature_Inference_Scripts"
SCRIPT_DIR = "/home/jiangfeng/jf_app/dcm_step1"
sys.path.insert(0, LIBS)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, RM_SCRIPTS)

from detectron2.checkpoint import DetectionCheckpointer          # noqa: E402
from detectron2.config import get_cfg                            # noqa: E402
from detectron2.data import DatasetMapper                        # noqa: E402
from detectron2.modeling import build_model                      # noqa: E402
from detectron2.structures import ImageList                      # noqa: E402
from Adapter import build_feature_adapter                        # noqa: E402
import s1_feature_domain_det as s1                               # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/home/jiangfeng/jf_app/dcm_step1/src/detectron2/configs/"
                                        "COCO-Detection/faster_rcnn_X_101_32x8d_FPN_3x.yaml")
    ap.add_argument("--weights", default="/home/jiangfeng/jf_app/DTM-V-1.5/packages/Model/"
                                         "Detector_model/model_final_68b088.pkl")
    ap.add_argument("--adapter_ckpt", default="/home/jiangfeng/jf_app/DTM-V-1.5/packages/Model/"
                                              "MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth")
    ap.add_argument("--image", required=True)
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--img_id", default="hq_final_1")
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
    print("model + adapter loaded (cpu, once)", flush=True)

    inputs = [DatasetMapper(cfg, is_train=None)(dict(file_name=args.image))]
    images = [(x["image"].to(device) - model.pixel_mean) / model.pixel_std for x in inputs]
    images = ImageList.from_tensors(images, model.backbone.size_divisibility)
    print("image_sizes =", images.image_sizes, flush=True)

    rate = {}
    rp = os.path.join(args.sweep, "report.json")
    if os.path.exists(rp):
        with open(rp, encoding="utf-8") as f:
            for r in json.load(f)["rows"]:
                rate[int(r["q"])] = r

    qs = sorted(int(d[1:]) for d in os.listdir(args.sweep)
                if d.startswith("q") and os.path.isdir(os.path.join(args.sweep, d)))
    report = {"image": args.image, "rows": []}
    for q in qs:
        featp = os.path.join(args.sweep, f"q{q:02d}", f"res2_{args.img_id}.pt")
        recp = os.path.join(args.sweep, f"q{q:02d}", f"rec_q{q:02d}.png")
        res2 = torch.load(featp, map_location=device).to(device)
        with torch.no_grad():
            res2 = adapter(res2)
            r3 = model.backbone.bottom_up.res3(res2)
            r4 = model.backbone.bottom_up.res4(r3)
            r5 = model.backbone.bottom_up.res5(r4)
            fpn = s1.my_fpn_forward({"res2": res2, "res3": r3, "res4": r4, "res5": r5}, model)
            proposals, _ = model.proposal_generator(images, fpn)
            inst_f, _ = model.roi_heads(images, fpn, proposals, None)
        top = [[s1.COCO80[int(c)], round(float(s), 3)]
               for s, c in sorted(zip(inst_f[0].scores.tolist(), inst_f[0].pred_classes.tolist()),
                                  reverse=True)]
        n5 = sum(1 for s in inst_f[0].scores.tolist() if float(s) >= 0.05)
        n3 = sum(1 for s in inst_f[0].scores.tolist() if float(s) >= 0.30)
        tag = f"q{q} {rate.get(q, {}).get('bpp', 0):.4f}bpp"
        s1.draw(recp, inst_f[0], os.path.join(args.out, f"overlay_feature_q{q:02d}.png"),
                f"FEATURE 域  q{q} ({tag})", (230, 60, 60))
        row = {"q": q, "q_factor": rate.get(q, {}).get("q_factor"),
               "total_bytes": rate.get(q, {}).get("total_bytes"),
               "bpp": rate.get(q, {}).get("bpp"),
               "n_gt0.05": n5, "n_gt0.30": n3, "n_proposals": int(len(proposals[0])),
               "top10": top[:10]}
        report["rows"].append(row)
        print(f"q={q:2d}  {row['total_bytes']} B  {row['bpp']:.5f} bpp  "
              f"框(>0.05)={n5:2d}  框(>0.30)={n3:2d}  top1={top[0] if top else None}", flush=True)

    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("S2_DONE", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
