#!/usr/bin/env python3
"""B7（147 上跑）：测**教师在 DCM 解码特征域**的召回 —— 即整条 DCM→检测 链的天花板。

口径：
- 输入 `pre_val/index.jsonl`（500 张 COCO val；含 b1 存的教师框，**分数已按 KEEP_SCORE=0.30 过滤**）
- GT = `instances_val2017.json`，按文件名匹配；**排除 iscrowd=1**；标签索引→类别 id 由 json 中
  isthing 类别**升序**推导（等价 detectron2 的 COCO_CATEGORIES 顺序）
- GT 框按 b1 的 `resize_like_rm` 同口径缩放到**内容坐标**（x *= W/W0、y *= H/H0），与教师框同域
- 配对：同类 + IoU>=0.5 ⇒ 报 GT 召回、教师框命中率、并按框尺寸分档

性质：**只读、离机、非验收依据**；不改 config、不碰板端。
用法：python b7_teacher_recall.py [pre_val 目录] [coco 根]
"""

import json
import os
import sys
from collections import defaultdict

PRE = "/home/jiangfeng/jf_app/dcm_b/pre_val"
COCO = "/home/jiangfeng/datasets/coco2017"
IOU_T = 0.5


def iou(a, b):
    x1 = max(a[0], b[0]); y1 = max(a[1], b[1])
    x2 = min(a[2], b[2]); y2 = min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def bucket(area):
    if area < 32 * 32:
        return "small"
    if area < 96 * 96:
        return "medium"
    return "large"


def main() -> int:
    pre = sys.argv[1] if len(sys.argv) > 1 else PRE
    coco = sys.argv[2] if len(sys.argv) > 2 else COCO
    rows = [json.loads(l) for l in open(os.path.join(pre, "index.jsonl"), encoding="utf-8") if l.strip()]
    print("pre_val 样本 =", len(rows), flush=True)
    bs = [r["bytes"] for r in rows]
    print("mean_bytes = %.0f | mean_bpp = %.5f"
          % (sum(bs) / len(bs),
             sum(r["bytes"] * 8 / (r["W"] * r["H"]) for r in rows) / len(rows)), flush=True)

    ann = json.load(open(os.path.join(coco, "annotations", "instances_val2017.json"), encoding="utf-8"))
    cat_ids = sorted(c["id"] for c in ann["categories"] if c.get("isthing", 1) == 1)
    cid2idx = {c: i for i, c in enumerate(cat_ids)}
    print("isthing 类别数 =", len(cat_ids), flush=True)
    info = {im["id"]: im for im in ann["images"]}
    by_name = {im["file_name"]: im for im in ann["images"]}
    gt_by_name = defaultdict(list)
    for a in ann["annotations"]:
        gt_by_name[info[a["image_id"]]["file_name"]].append(a)

    n_gt = 0
    used = 0
    # 多阈值：同一份框（存的是 >=0.05 的全部）在不同分数门限下分别统计
    THRS = (0.05, 0.10, 0.30)
    hit = {t: 0 for t in THRS}
    ntb = {t: 0 for t in THRS}
    thit = {t: 0 for t in THRS}
    by_size = defaultdict(lambda: {t: [0, 0] for t in THRS})
    for r in rows:
        fn = os.path.basename(r["src"])
        gts = gt_by_name.get(fn)
        im = by_name.get(fn)
        if gts is None or im is None:
            continue
        used += 1
        fx = float(r["W"]) / float(im["width"])
        fy = float(r["H"]) / float(im["height"])
        tb = [(int(c), float(sc), [float(v) for v in b])
              for b, sc, c in zip(r["boxes"], r["scores"], r["labels"])]
        gtb = []
        for g in gts:
            if g.get("iscrowd", 0) == 1 or g["category_id"] not in cid2idx:
                continue
            x, y, w, h = g["bbox"]
            gtb.append((cid2idx[g["category_id"]], [x * fx, y * fy, (x + w) * fx, (y + h) * fy]))
        n_gt += len(gtb)
        for t in THRS:
            sub = [x for x in tb if x[1] >= t]
            ntb[t] += len(sub)
            for gc, gb in gtb:
                bk = bucket((gb[2] - gb[0]) * (gb[3] - gb[1]))
                by_size[bk][t][0] += 1
                if any(c == gc and iou(b, gb) >= IOU_T for c, sc, b in sub):
                    hit[t] += 1
                    by_size[bk][t][1] += 1
            for c, sc, b in sub:
                if any(c == gc and iou(b, gb) >= IOU_T for gc, gb in gtb):
                    thit[t] += 1

    print(f"参与样本 = {used} / {len(rows)}")
    print(f"GT(非 crowd) = {n_gt}")
    for t in THRS:
        print(f"  thr>{t:.2f}: recall@0.5 = {hit[t] / max(1, n_gt):.3f} "
              f"({hit[t]}/{n_gt}) | 框={ntb[t]} hit-rate = {thit[t] / max(1, ntb[t]):.3f}")
    for k in ("small", "medium", "large"):
        line = "  %-6s:" % k
        for t in THRS:
            a, b = by_size[k][t]
            line += f" [{t:.2f}] {b / max(1, a):.3f} (GT={a})"
        print(line)
    print("B7_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
