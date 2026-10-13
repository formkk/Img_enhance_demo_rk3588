#!/usr/bin/env python3
"""B1（B 阶段预计算，147 上跑）：COCO/现场帧 → DCM 链 → 解码特征 res2 + 教师伪标签。

每个样本：
    PIL 读图 → RM 口径 resize(短边800/长边<=1333) → 补零到 64 倍数
    → fe → fc.compress(q) → fc.decompress → res2 (1,128,192,336)
    → 教师(adapter + X-101 FPN + RPN/ROI) → 检测框(xyxy, 原图/内容坐标)
产物：--out/<idx>.npy (float16 res2) + --out/index.jsonl (每行一条元数据)

用法（147）：
  cd /home/jiangfeng/jf_app/DTM-V-1.5
  CUDA_VISIBLE_DEVICES=0 PYTHONPATH=<libs>:<dcm_step1> \
  /home/jiangfeng/anaconda3/envs/dtm-v/bin/python -u /path/b1_precompute.py \
      --out /home/jiangfeng/jf_app/dcm_b/pre --src coco --split train --limit 64
"""

import argparse
import glob
import json
import os
import random
import sys
import time
import types
from functools import partial

import numpy as np

# ---- 导入顺序敏感：compressai 必须早于 models ----
sys.modules.setdefault("torchac", types.ModuleType("torchac"))
import compressai                                                   # noqa: F401,E402
import torch                                                        # noqa: E402
from PIL import Image                                               # noqa: E402

DTM = "/home/jiangfeng/jf_app/DTM-V-1.5"
LIBS = "/home/jiangfeng/jf_app/dcm_step1/libs"
RM_SCRIPTS = "/home/jiangfeng/jf_app/DTM-V-1.5/scripts/DCM_Datasets_Feature_Inference_Scripts"
STEP1 = "/home/jiangfeng/jf_app/dcm_step1"
COCO = "/home/jiangfeng/datasets/coco2017"
sys.path.insert(0, DTM)
sys.path.insert(0, LIBS)
sys.path.insert(0, RM_SCRIPTS)
sys.path.insert(0, STEP1)
os.chdir(DTM)                        # DTM 内部用相对路径

from models.MIK_MCM_codecs import (                                  # noqa: E402
    build_feature_extractor, build_feature_coding_Mean_MCM_Scale)

XMIN, XMAX, PADF = 800, 1333, 64
QFACTOR = {0: 0.200, 3: 0.265, 7: 0.351, 11: 0.438, 15: 0.524,
           19: 0.611, 23: 0.697, 27: 0.784, 31: 0.870}
KEEP_SCORE = 0.30                    # 伪标签阈值（教师分数）


def resize_like_rm(im):
    h, w = im.height, im.width
    scale = XMIN * 1.0 / min(h, w)
    newh, neww = (XMIN, scale * w) if h < w else (scale * h, XMIN)
    if max(newh, neww) > XMAX:
        sc = XMAX * 1.0 / max(newh, neww)
        newh, neww = newh * sc, neww * sc
    return im.resize((int(neww + 0.5), int(newh + 0.5)))


def pad_to(hw, f=PADF):
    h, w = hw
    ph = f * ((h // f) + 1) - h if h % f else 0
    pw = f * ((w // f) + 1) - w if w % f else 0
    return ph, pw


def load_chain(dev):
    fe = build_feature_extractor()
    fc = build_feature_coding_Mean_MCM_Scale(quality=3, parallel_computing=True)
    ck = torch.load(os.path.join(DTM, "packages/Model/MIKcodec_MCM_model/"
                                      "dtm-V_entropy_vm_1_270000.pth"), map_location="cpu")
    fe.load_state_dict(ck["feature_extractor"])
    fc.gaussian_conditional.scale_table = fc.gaussian_conditional.scale_table.to(torch.int32)
    fc.load_state_dict(ck["feature_coding"])
    fc._update_per_channel_shifts(ck["per_channel_shifts"]["feature_coding"])
    fe.eval().to(dev)
    fc.eval().to(dev)
    fc.context.quantize_func = partial(fc.quantize)
    fe.requires_grad_(False)
    fc.requires_grad_(False)
    return fe, fc


def load_teacher(dev):
    from detectron2.config import get_cfg
    from detectron2.modeling import build_model
    from detectron2.checkpoint import DetectionCheckpointer
    from detectron2.structures import ImageList
    from Adapter import build_feature_adapter
    cfg = get_cfg()
    cfg.merge_from_file(os.path.join(STEP1, "src/detectron2/configs/COCO-Detection/"
                                            "faster_rcnn_X_101_32x8d_FPN_3x.yaml"))
    cfg.MODEL.WEIGHTS = os.path.join(DTM, "packages/Model/Detector_model/model_final_68b088.pkl")
    cfg.MODEL.DEVICE = str(dev)
    cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = 0.05
    model = build_model(cfg)
    DetectionCheckpointer(model).load(cfg.MODEL.WEIGHTS)
    model.eval().to(dev)
    adapter = build_feature_adapter(128, 256)
    ck = torch.load(os.path.join(DTM, "packages/Model/MIKcodec_MCM_model/"
                                      "dtm-V_entropy_vm_1_270000.pth"), map_location=dev)
    adapter.load_state_dict(ck["feature_adapter"])
    adapter.eval().to(dev)
    return model, adapter, ImageList


def list_sources(args):
    out = []
    if args.src in ("coco", "both"):
        pat = os.path.join(COCO, f"{args.split}2017", "*.jpg")
        files = sorted(glob.glob(pat))
        rnd = random.Random(args.seed)
        rnd.shuffle(files)
        out += [("coco", p) for p in files[:args.limit]]
    if args.src in ("indomain", "both"):
        ind = []
        for ext in ("png", "jpg", "jpeg", "bmp"):
            ind += sorted(glob.glob(os.path.join(args.indomain, "**", f"*.{ext}"), recursive=True))
        ind = sorted(set(ind))
        out += [("indomain", p) for p in ind[:args.indomain_limit]]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--src", default="coco", choices=["coco", "indomain", "both"])
    ap.add_argument("--split", default="train", choices=["train", "val"])
    ap.add_argument("--limit", type=int, default=64)
    ap.add_argument("--indomain", default="/home/jiangfeng/jf_app/dcm_b/indomain",
                    help="本项目自己的演示域帧目录（--src indomain/both 时使用）")
    ap.add_argument("--indomain_limit", type=int, default=64)
    ap.add_argument("--qset", default="3,7,11,15,19,23,27,31")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--keep-score", type=float, default=KEEP_SCORE,
                    help="伪标签分数下限（默认 0.30；做召回上限口径时传 0.05）")
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    dev = torch.device(args.device)
    qset = [int(x) for x in args.qset.split(",") if x.strip()]
    os.makedirs(args.out, exist_ok=True)
    items = list_sources(args)
    print(f"src={args.src} split={args.split} 样本数={len(items)} qset={qset}", flush=True)

    fe, fc = load_chain(dev)
    model, adapter, ImageList = load_teacher(dev)
    print("chain + teacher ready", flush=True)

    idx_path = os.path.join(args.out, "index.jsonl")
    t_all = time.perf_counter()
    with open(idx_path, "a", encoding="utf-8") as fh:
        for k, (tag, path) in enumerate(items):
            try:
                im = resize_like_rm(Image.open(path).convert("RGB"))
            except Exception as e:
                print(f"[skip] {path} {type(e).__name__}", flush=True)
                continue
            W, H = im.width, im.height
            ph, pw = pad_to((H, W))
            PAD_H, PAD_W = H + ph, W + pw
            a = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0
            a = np.pad(a, ((0, 0), (0, ph), (0, pw)))
            x = torch.from_numpy(a).unsqueeze(0).to(dev)
            q = random.Random(args.seed * 100003 + k).choice(qset)
            qm = torch.full((1, 1, PAD_H // 16, PAD_W // 16), QFACTOR[q], dtype=torch.float32).to(dev)
            t0 = time.perf_counter()
            with torch.no_grad():
                f0 = fe(x)
                out = fc.compress(f0, qm, rate_control_flag=False,
                                  after_padding_H=PAD_H, after_padding_W=PAD_W)
                res2 = fc.decompress(out["strings"], out["shape"], qm)["x_hat"]
                nb = len(out["strings"][0][0]) + len(out["strings"][1][0])
                # ---- 教师 ----（image_sizes 用**补零尺寸**(PAD_H,PAD_W)）----
                # 网络实际处理的是补零后的张量，raw 预测即**补零坐标系**；补零只追加在右/下，
                # 故内容的补零坐标 == 内容坐标。给 (H,W) 会再乘 ~0.98 ⇒ 框系统性偏小 2~4%（实测）。
                imgs = [(x[0] - model.pixel_mean) / model.pixel_std]
                imgs = ImageList.from_tensors(imgs, model.backbone.size_divisibility)
                a2 = adapter(res2)
                r3 = model.backbone.bottom_up.res3(a2)
                r4 = model.backbone.bottom_up.res4(r3)
                r5 = model.backbone.bottom_up.res5(r4)
                import s1_feature_domain_det as s1
                fpn = s1.my_fpn_forward({"res2": a2, "res3": r3, "res4": r4, "res5": r5}, model)
                props, _ = model.proposal_generator(imgs, fpn)
                det, _ = model.roi_heads(imgs, fpn, props, None)
                inst = model._postprocess(det, [dict(image=x[0])], [(PAD_H, PAD_W)])[0]["instances"]
            bx = inst.pred_boxes.tensor.detach().cpu().numpy().astype(np.float32)
            sc = inst.scores.detach().cpu().numpy().astype(np.float32)
            cl = inst.pred_classes.detach().cpu().numpy().astype(np.int16)
            m = sc >= args.keep_score
            bx, sc, cl = bx[m], sc[m], cl[m]
            f = os.path.join(args.out, f"{k:07d}.npy")
            np.save(f, res2[0].detach().cpu().numpy().astype(np.float16))
            fh.write(json.dumps({"idx": k, "file": os.path.basename(f), "tag": tag,
                                 "src": path, "q": q, "bytes": int(nb),
                                 "W": W, "H": H, "PAD_W": PAD_W, "PAD_H": PAD_H,
                                 "boxes": bx.tolist(), "scores": sc.tolist(),
                                 "labels": cl.tolist()}, ensure_ascii=False) + "\n")
            if k % 10 == 0 or k == len(items) - 1:
                dt = time.perf_counter() - t_all
                print(f"[{k+1}/{len(items)}] q={q:2d} {nb}B 框={len(bx)} "
                      f"({time.perf_counter()-t0:.2f}s/图, 累计 {dt:.0f}s)", flush=True)
    print("B1_DONE", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
