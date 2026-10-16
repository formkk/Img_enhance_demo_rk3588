#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 COCO 预训练 YOLO11x 给 HQ 输出图打伪标签（labelme 格式），供人工修正。

**为什么用 YOLO11x（而不是我们现役的 DCM 检测器）**：
  · 类别空间一致 —— YOLO11x 官方权重 = **COCO 80 类**，与 DCM 规范检测器
    （Faster R-CNN X-101-FPN，COCO 80 类）**同类同序** ⇒ 伪标签可直接用于
    特征域微调，无需类映射；
  · 指标更高 —— YOLO11x COCO mAP@[.5:.95] **54.7**，远高于现役 X-101 的 43.0
    ⇒ 伪标签质量更好（X-101 的低置信虚警率本已实测偏高，见立项草案 §8.19）。

**权重** = `Ultralytics/YOLO11` 的 `yolo11x.pt`（HF 镜像下载，非 GitHub）；
本地副本 `/home/jenkolux/datasets/yolo_weights/yolo11x.pt`（仓库外，不入 git）。

**输出** = labelme 可直接打开的工作目录（图 + 同名 json）：
  <out-dir>/<stem>.png   原图（**原样复制，零后处理**）
  <out-dir>/<stem>.json  labelme 格式（shapes = rectangle；score 存 `flags.score`，
                         **不进 label 文本**，避免污染训练类名）
  <out-dir>/pseudo_manifest.json  汇总（阈值 / 每图框数 / 权重哈希）

**纪律**：
  · 伪标签**非验收依据**（规则 `10-evidence.md` §1），必须人工修正后才可入训练；
  · 图像**原样归档**，不做任何非线性色调处理（规则 `10-evidence.md` §2）；
    `--vis` 另存叠框图**仅用于目视复核**，是标注绘制、非色调变换；
  · **许可提示** = YOLO11 权重为 **AGPL-3.0**（强 copyleft）。本脚本仅作**离线内部
    工具**产出标注，不随产品分发；若将来要交付/商用，需单独核实许可（同 §8.19
    对 YOLOv6 许可的登记口径）。

用法：
  python3 yolo_pseudo_label.py --src-dir <HQ png 目录> --out-dir <labelme 目录> \
      [--weights /path/yolo11x.pt] [--conf 0.25] [--iou 0.5] [--imgsz 640] [--vis]
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
import time

DEFAULT_WEIGHTS = "/home/jenkolux/datasets/yolo_weights/yolo11x.pt"


def sha256(path, blk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(blk), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-dir", required=True, help="HQ 输出 PNG 目录")
    ap.add_argument("--out-dir", required=True, help="labelme 工作目录（图 + json）")
    ap.add_argument("--weights", default=DEFAULT_WEIGHTS)
    ap.add_argument("--conf", type=float, default=0.25, help="置信度阈（默认 0.25）")
    ap.add_argument("--iou", type=float, default=0.5, help="NMS IoU 阈")
    ap.add_argument("--imgsz", type=int, default=640,
                    help="推理尺寸。**默认 640 = YOLO 原生训练尺寸**（最可靠）。"
                         "实测：>960 时该权重输出整体塌陷（同一张图最高置信度 "
                         "0.728@640 → 0.248@1280，且类别错标），CPU/GPU 一致、确定性 ⇒ "
                         "**不得用大 imgsz**（2026-10-16 实测）")
    ap.add_argument("--device", default="0")
    ap.add_argument("--vis", action="store_true", help="另存叠框图（仅目视复核，非色调处理）")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    from ultralytics import YOLO
    import cv2

    if not os.path.isfile(args.weights):
        print(f"!! 权重不存在: {args.weights}", file=sys.stderr)
        return 2
    files = sorted(glob.glob(os.path.join(args.src_dir, "*.png")))
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"!! {args.src_dir} 无 png", file=sys.stderr)
        return 2

    os.makedirs(args.out_dir, exist_ok=True)
    vis_dir = os.path.join(args.out_dir, "_vis")
    if args.vis:
        os.makedirs(vis_dir, exist_ok=True)

    model = YOLO(args.weights)
    names = model.names                       # {0:'person', ...} = COCO 80
    print(f"权重 {args.weights}\n  类别数 = {len(names)}（COCO 80）", flush=True)

    rows, t0 = [], time.time()
    for i, f in enumerate(files):
        stem = os.path.splitext(os.path.basename(f))[0]
        r = model.predict(source=f, conf=args.conf, iou=args.iou, imgsz=args.imgsz,
                          device=args.device, verbose=False)[0]
        img = cv2.imread(f)
        h, w = img.shape[:2]
        shapes = []
        for b in r.boxes:
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0].tolist()]
            cls = int(b.cls[0].item())
            sc = float(b.conf[0].item())
            shapes.append({
                "label": names[cls],
                "points": [[x1, y1], [x2, y2]],
                "group_id": None,
                "shape_type": "rectangle",
                "flags": {"score": round(sc, 4), "cls_id": cls},
            })
        lj = {"version": "5.5.0", "flags": {}, "shapes": shapes,
              "imagePath": os.path.basename(f), "imageData": None,
              "imageHeight": int(h), "imageWidth": int(w),
              # 非标准扩展字段：记录伪标签来源，便于事后追溯（labelme 读取时会忽略）
              "_pseudo": {"model": "yolo11x", "weights_sha256_12": sha256(args.weights)[:12],
                          "conf": args.conf, "iou": args.iou, "imgsz": args.imgsz}}
        with open(os.path.join(args.out_dir, f"{stem}.json"), "w") as fh:
            json.dump(lj, fh, ensure_ascii=False, indent=1)
        # 图像原样复制（零后处理）
        shutil.copy2(f, os.path.join(args.out_dir, f"{stem}.png"))
        if args.vis:
            cv2.imwrite(os.path.join(vis_dir, f"{stem}.png"), r.plot())
        rows.append({"stem": stem, "ndet": len(shapes),
                     "classes": sorted({s["label"] for s in shapes})})
        print(f"[{i+1}/{len(files)}] {stem} ndet={len(shapes)} ({time.time()-t0:.0f}s)",
              flush=True)

    man = {"model": "yolo11x", "weights": args.weights,
           "weights_sha256": sha256(args.weights),
           "conf": args.conf, "iou": args.iou, "imgsz": args.imgsz,
           "n_images": len(rows), "n_boxes": sum(r["ndet"] for r in rows),
           "src_dir": os.path.abspath(args.src_dir),
           "out_dir": os.path.abspath(args.out_dir),
           "class_space": "COCO 80（与 DCM 检测器同类同序）",
           "license_note": "YOLO11 权重 AGPL-3.0；仅作离线内部标注工具，不随产品分发",
           "eff": "伪标签，非验收依据（规则 10-evidence.md §1）；须人工修正后入训练",
           "wall_sec": round(time.time() - t0, 1),
           "images": rows}
    with open(os.path.join(args.out_dir, "pseudo_manifest.json"), "w") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=1)
    print(f"\ndone {len(rows)} 张 / {man['n_boxes']} 框 → {args.out_dir} "
          f"({man['wall_sec']}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
