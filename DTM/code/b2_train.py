#!/usr/bin/env python3
"""B2（学生头训练，147 上跑）：在**解码特征**上，用教师伪标签蒸馏 FeatureDet。

- 数据：B1 产出的 <dir>/{idx}.npy (float16 res2) + index.jsonl（含教师伪标签 boxes/scores/labels）
- 模型：与 t0 完全同构的 FeatureDet（adapter warm start 自 RM feature_adapter）
- 目标：FCOS 式分配（尺寸区间 + 中心在框内）→ 分类 sigmoid focal、回归 GIoU
- **注意**：COCO 图尺寸不一 ⇒ 解码特征 HxW **每样本不同**，各层尺寸按 res2.shape 动态推导
- 评测：学生框 vs 教师框，按类 + IoU>=0.5 配对

用法（147）：
  CUDA_VISIBLE_DEVICES=0 /home/jiangfeng/anaconda3/envs/dtm-v/bin/python -u b2_train.py \
      --train /home/jiangfeng/jf_app/dcm_b/pre_train --val /home/jiangfeng/jf_app/dcm_b/pre_val \
      --out /home/jiangfeng/jf_app/dcm_b/ckpt --epochs 20
"""

import argparse
import json
import os
import sys
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

DTM = "/home/jiangfeng/jf_app/DTM-V-1.5"
NUM_CLS = 80
RANGES = [(0, 64), (64, 128), (128, 256), (256, 1e9)]
STRIDES = [8, 16, 32, 64]


def levels_of(H, W):
    """由 res2 的 (H,W) 推各层 (stride,h,w)：4 次 conv3x3 stride2 pad1 ⇒ 每级 ceil(/2)。"""
    out, h, w = [], H, W
    for s in STRIDES:
        h = (h + 1) // 2
        w = (w + 1) // 2
        out.append((s, h, w))
    return out


class FeatureDet(nn.Module):
    def __init__(self, num_cls=NUM_CLS, ch=256):
        super().__init__()
        self.adapter = nn.Sequential(nn.Conv2d(128, ch, 3, padding=1), nn.ReLU(inplace=True))
        self.l3 = nn.Sequential(nn.Conv2d(ch, ch, 3, stride=2, padding=1), nn.ReLU(inplace=True))
        self.l4 = nn.Sequential(nn.Conv2d(ch, ch, 3, stride=2, padding=1), nn.ReLU(inplace=True))
        self.l5 = nn.Sequential(nn.Conv2d(ch, ch, 3, stride=2, padding=1), nn.ReLU(inplace=True))
        self.l6 = nn.Sequential(nn.Conv2d(ch, ch, 3, stride=2, padding=1), nn.ReLU(inplace=True))
        self.tower = nn.Sequential(nn.Conv2d(ch, ch, 3, padding=1), nn.ReLU(inplace=True),
                                   nn.Conv2d(ch, ch, 3, padding=1), nn.ReLU(inplace=True))
        self.cls = nn.Conv2d(ch, num_cls, 3, padding=1)
        self.reg = nn.Conv2d(ch, 4, 3, padding=1)
        # 回归偏置初值 = 4（stride 单位）⇒ 起步即 ~8×stride 的框。
        # 必要性：GIoU 在退化点框处 (px2-px1).clamp(min=0) 梯度为 0；center sampling 下
        # 正样本目标普遍 = 半个框长（远大于 0），若从 0 起步则**永远零梯度**（实测 reg 恒 0.90 不降）。
        nn.init.constant_(self.reg.bias, 4.0)

    def forward(self, x):
        f = self.adapter(x)
        l3 = self.l3(f); l4 = self.l4(l3); l5 = self.l5(l4); l6 = self.l6(l5)
        outs = []
        for lv in (l3, l4, l5, l6):
            t = self.tower(lv)
            c = self.cls(t).flatten(2)
            r = self.reg(t).flatten(2)
            outs.append(torch.cat([c, r], dim=1).transpose(1, 2))
        return torch.cat(outs, dim=1)


def warm_start_adapter(model, dev):
    ck = torch.load(os.path.join(DTM, "packages/Model/MIKcodec_MCM_model/"
                                      "dtm-V_entropy_vm_1_270000.pth"), map_location=dev)
    fa = ck["feature_adapter"]
    with torch.no_grad():
        model.adapter[0].weight.copy_(fa["adapter.weight"])
        model.adapter[0].bias.copy_(fa["adapter.bias"])


class Shard(torch.utils.data.Dataset):
    def __init__(self, d):
        self.d = d
        self.rows = []
        with open(os.path.join(d, "index.jsonl"), encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    self.rows.append(json.loads(ln))

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        r = self.rows[i]
        res2 = np.load(os.path.join(self.d, r["file"])).astype(np.float32)
        return {"res2": torch.from_numpy(res2),
                "boxes": torch.as_tensor(np.asarray(r["boxes"], np.float32)).reshape(-1, 4),
                "labels": torch.as_tensor(np.asarray(r["labels"], np.int64)).reshape(-1)}


def assign_targets(boxes, labels, levels, dev):
    tg = []
    for (s, h, w), (lo, hi) in zip(levels, RANGES):
        ys, xs = torch.meshgrid(torch.arange(h, device=dev), torch.arange(w, device=dev),
                                indexing="ij")
        cx = (xs.reshape(-1).float() + 0.5) * s
        cy = (ys.reshape(-1).float() + 0.5) * s
        n = h * w
        tcls = torch.full((n,), -1, dtype=torch.long, device=dev)   # 默认 -1 = 背景
        treg = torch.zeros((n, 4), device=dev)
        if boxes.numel() == 0:
            tg.append((tcls, treg, torch.zeros(n, dtype=torch.bool, device=dev)))
            continue
        l = cx[:, None] - boxes[None, :, 0]
        t = cy[:, None] - boxes[None, :, 1]
        r = boxes[None, :, 2] - cx[:, None]
        b = boxes[None, :, 3] - cy[:, None]
        ltrb = torch.stack([l, t, r, b], -1)              # **像素**单位
        inside = ltrb.min(-1).values > 0
        mx = ltrb.max(-1).values                          # 层分配用**像素**长边（不是 /s 后的值）
        inrange = (mx >= lo) & (mx <= hi)
        # FCOS center sampling：只让距物体中心 ≤1.5*stride 的格子当正样本 ⇒ 正样本质量↑
        bcx = (boxes[:, 0] + boxes[:, 2]) * 0.5
        bcy = (boxes[:, 1] + boxes[:, 3]) * 0.5
        rad = 1.5 * s
        inrange = (inrange
                   & (cx[:, None] >= bcx[None, :] - rad) & (cx[:, None] <= bcx[None, :] + rad)
                   & (cy[:, None] >= bcy[None, :] - rad) & (cy[:, None] <= bcy[None, :] + rad))
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        area_b = area[None, :].expand_as(inside)
        area_b = torch.where(inside, area_b, torch.full_like(area_b, 1e12))
        best = area_b.argmin(1)
        has = (inside & inrange).any(1)
        tcls[has] = labels[best[has]]
        treg[has] = ltrb[torch.arange(n, device=dev)[has], best[has]] / s   # 回归目标 = 像素/stride
        tcls[inside.any(1) & ~has] = -2          # 框内但不在本层尺寸区间 ⇒ ignore（不监督分类）
        tg.append((tcls, treg, has))
    return tg


def focal_loss(logits, targets, alpha=0.25, gamma=2.0):
    """targets：-1 = 背景（one-hot 全 0）、-2 = 忽略、>=0 = 类号（detectron2 口径）。"""
    valid = targets != -2
    lg = logits[valid]
    tg = targets[valid]
    if lg.numel() == 0:
        return logits.sum() * 0.0
    pos = tg >= 0
    oh = torch.zeros_like(lg)
    if bool(pos.any()):
        oh[pos] = F.one_hot(tg[pos], NUM_CLS).float()
    ce = F.binary_cross_entropy_with_logits(lg, oh, reduction="none")
    p = torch.sigmoid(lg)
    alpha_t = alpha * oh + (1.0 - alpha) * (1.0 - oh)
    p_t = p * oh + (1.0 - p) * (1.0 - oh)
    w = alpha_t * (1.0 - p_t) ** gamma
    return (ce * w).sum() / max(1, int(pos.sum()))


def giou_loss(pred, target):
    px1, py1, px2, py2 = -pred[:, 0], -pred[:, 1], pred[:, 2], pred[:, 3]
    tx1, ty1, tx2, ty2 = -target[:, 0], -target[:, 1], target[:, 2], target[:, 3]
    ix1 = torch.max(px1, tx1); iy1 = torch.max(py1, ty1)
    ix2 = torch.min(px2, tx2); iy2 = torch.min(py2, ty2)
    inter = (ix2 - ix1).clamp(min=0) * (iy2 - iy1).clamp(min=0)
    ap = (px2 - px1).clamp(min=0) * (py2 - py1).clamp(min=0)
    at = (tx2 - tx1).clamp(min=0) * (ty2 - ty1).clamp(min=0)
    union = ap + at - inter + 1e-6
    iou_ = inter / union
    cw = (torch.max(px2, tx2) - torch.min(px1, tx1)).clamp(min=0)
    ch = (torch.max(py2, ty2) - torch.min(py1, ty1)).clamp(min=0)
    enclose = cw * ch + 1e-6
    return (1 - iou_ + (enclose - union) / enclose).mean()


def decode(pred, levels, conf=0.30):
    out = []
    off = 0
    for (s, h, w) in levels:
        n = h * w
        blk = pred[off:off + n]; off += n
        ys, xs = np.meshgrid(np.arange(h), np.arange(w), indexing="ij")
        cx = (xs.reshape(-1) + 0.5) * s; cy = (ys.reshape(-1) + 0.5) * s
        sc = 1 / (1 + np.exp(-blk[:, :NUM_CLS].astype(np.float64)))
        ci = sc.argmax(1); sm = sc[np.arange(n), ci]
        for j in np.where(sm > conf)[0]:
            l, t, r, b = blk[j, -4:] * s
            out.append((float(sm[j]), int(ci[j]), float(cx[j] - l), float(cy[j] - t),
                        float(cx[j] + r), float(cy[j] + b)))
    return out


def iou(a, b):
    x1 = max(a[0], b[0]); y1 = max(a[1], b[1]); x2 = min(a[2], b[2]); y2 = min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    return inter / (ua + 1e-9)


def nms_boxes(dets, thr=0.5, topk=100):
    """dets: list of (score, cls, x1, y1, x2, y2) ⇒ 每类 NMS 后按分数取前 topk。"""
    dets = sorted(dets, key=lambda d: -d[0])[:5000]
    keep = []
    for c in sorted({d[1] for d in dets}):
        idx = [i for i, d in enumerate(dets) if d[1] == c]
        boxes = np.array([[dets[i][2], dets[i][3], dets[i][4], dets[i][5]] for i in idx], np.float32)
        sc = np.array([dets[i][0] for i in idx], np.float32)
        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        areas = np.maximum(0, x2-x1) * np.maximum(0, y2-y1)
        order = sc.argsort()[::-1]
        while order.size > 0:
            i = order[0]
            keep.append(idx[int(i)])
            if order.size == 1:
                break
            xx1 = np.maximum(x1[i], x1[order[1:]]); yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]]); yy2 = np.minimum(y2[i], y2[order[1:]])
            inter = np.maximum(0.0, xx2-xx1) * np.maximum(0.0, yy2-yy1)
            iou_ = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
            order = order[1:][iou_ <= thr]
    return [dets[i] for i in keep[:topk]]


def evaluate(model, va, dev, n, confs=(0.05, 0.10, 0.30)):
    """多阈值评测 ⇒ (教师框数, {conf: (学生框数, 命中数, recall)}, 诊断字典)。

    诊断（用于区分"分类崩"与"回归崩"）：
      ms   = 每图最高分中位数            —— 分类头的信号强度
      sw   = 学生框长边中位数(conf=0.05) —— 回归是否在长（退化点框 ⇒ sw≈0）
      tw   = 教师框长边中位数            —— 参照
      biou = **忽略类别**的最大 IoU 中位数 —— 定位本身对不对（高 ⇒ 定位对、分类错）
    """
    model.eval()
    gb_all, pb_all = [], {c: [] for c in confs}
    mx, sw, biou, tw = [], [], [], []
    with torch.no_grad():
        for i in range(min(len(va), n)):
            r = va.rows[i]
            res2 = np.load(os.path.join(va.d, r["file"])).astype(np.float32)
            t = torch.from_numpy(res2)[None].to(dev)
            lv = levels_of(t.shape[2], t.shape[3])
            pred = model(t)[0].cpu().numpy()
            gb = [(float(sc), int(c), *b) for sc, c, b in
                  zip(r["scores"], r["labels"], r["boxes"])]
            gb_all.append(gb)
            lg = np.clip(pred[:, :NUM_CLS].astype(np.float64), -60, 60)
            mx.append(float((1.0 / (1.0 + np.exp(-lg))).max()))
            for c in confs:
                pb_all[c].append(nms_boxes(decode(pred, lv, c)))
            pb0 = pb_all[confs[0]][-1]                        # 当前图的框
            sw += [max(d[4] - d[2], d[5] - d[3]) for d in pb0]
            tw += [max(g[4] - g[2], g[5] - g[3]) for g in gb]
            for g in gb:                                  # 忽略类别：教师框的最佳 IoU
                biou.append(max([iou(g[2:], p[2:]) for p in pb0] or [0.0]))
    model.train()
    tot_g = sum(len(g) for g in gb_all)
    res = {}
    for c in confs:
        hit = tot_p = 0
        for gb, pb in zip(gb_all, pb_all[c]):
            tot_p += len(pb)
            for g in gb:
                for p in pb:
                    if p[1] == g[1] and iou(p[2:], g[2:]) >= 0.5:
                        hit += 1
                        break
        res[c] = (tot_p, hit, hit / max(1, tot_g))
    ex = dict(ms=float(np.median(mx)) if mx else 0.0,
              sw=float(np.median(sw)) if sw else 0.0,
              tw=float(np.median(tw)) if tw else 0.0,
              biou=float(np.median(biou)) if biou else 0.0)
    return tot_g, res, ex


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--val", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--steps", type=int, default=0)
    ap.add_argument("--patience", type=int, default=8)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--init", default="", help="已有 ckpt 热启动（域微调；留空 = 仅 warm start adapter）")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    dev = torch.device(args.device)

    tr = Shard(args.train)
    va = Shard(args.val)
    print(f"train={len(tr)} val={len(va)}", flush=True)
    model = FeatureDet().to(dev)
    warm_start_adapter(model, dev)
    if args.init:
        ck0 = torch.load(args.init, map_location=dev)
        model.load_state_dict(ck0["model"])
        print("init from", args.init, "epoch =", ck0.get("epoch"), flush=True)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    tl = torch.utils.data.DataLoader(tr, batch_size=1, shuffle=True, num_workers=2,
                                     collate_fn=lambda b: b[0])
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(
        opt, T_max=max(1, args.epochs * len(tl)), eta_min=args.lr * 0.1)

    gstep = 0
    best_rc, best_ep = -1.0, 0
    t0 = time.time()
    for ep in range(args.epochs):
        model.train()
        acc, acc_c, acc_r = [], [], []
        for bi, s in enumerate(tl):
            res2 = s["res2"][None].to(dev)
            levels = levels_of(res2.shape[2], res2.shape[3])
            pred = model(res2)[0]                                     # (N,84)
            boxes = s["boxes"].to(dev); labels = s["labels"].to(dev)
            off = 0
            lc, lr_ = [], []
            for (tcls, treg, has) in assign_targets(boxes, labels, levels, dev):
                n = tcls.shape[0]
                pblk = pred[off:off + n]; off += n
                lc.append(focal_loss(pblk[:, :NUM_CLS], tcls))
                if bool(has.any()):
                    lr_.append(giou_loss(pblk[has][:, -4:], treg[has]))
            l_cls = sum(lc) / len(lc)
            l_reg = (sum(lr_) / len(lr_)) if lr_ else 0.0
            loss = l_cls + l_reg
            opt.zero_grad(); loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 10.0)
            opt.step(); sched.step()
            acc.append(float(loss)); acc_c.append(float(l_cls)); acc_r.append(float(l_reg))
            gstep += 1
            if bi % 50 == 0:
                print(f"ep{ep} step{bi} loss={np.mean(acc[-50:]):.4f} "
                      f"cls={np.mean(acc_c[-50:]):.4f} reg={np.mean(acc_r[-50:]):.4f} "
                      f"({(time.time()-t0)/max(1,gstep):.2f}s/step)", flush=True)
            if args.steps and gstep >= args.steps:
                break
        print(f"== epoch {ep} done  mean_loss={np.mean(acc):.4f} "
              f"cls={np.mean(acc_c):.4f} reg={np.mean(acc_r):.4f}", flush=True)
        torch.save({"model": model.state_dict(), "epoch": ep}, os.path.join(args.out, "last.pth"))
        g_, res, ex = evaluate(model, va, dev, 50)          # 每轮多阈值 + 诊断 ⇒ 早停依据
        rc = res[0.10][2]      # 早停/选优判据 = R@0.10（预期部署档）；R@0.05 早饱和且实测会误砍仍在变好的轮次
        if rc > best_rc:
            best_rc, best_ep = rc, ep
            torch.save({"model": model.state_dict()}, os.path.join(args.out, "best.pth"))
        tag = " ".join(f"c{c}:P={res[c][0]} R={res[c][2]:.3f}" for c in sorted(res))
        print(f"== EVAL ep{ep} val50: teacher={g_} | {tag} | "
              f"ms={ex['ms']:.3f} 学生框长边={ex['sw']:.0f}(教师{ex['tw']:.0f}) "
              f"biou={ex['biou']:.3f} bestR={best_rc:.3f}@{best_ep}", flush=True)
        if args.steps and gstep >= args.steps:
            break
        if best_rc <= 0.0 and ep + 1 >= args.patience:
            print(f"EARLY_STOP ep{ep}: R@0.10 连续 {ep + 1} 轮为 0 ⇒ 停", flush=True)
            break
        if best_rc > 0.0 and ep - best_ep >= args.patience:
            print(f"EARLY_STOP ep{ep}: {args.patience} 轮无提升（best={best_rc:.3f}@{best_ep}）", flush=True)
            break

    g_, res, ex = evaluate(model, va, dev, 200)
    tag = " ".join(f"conf{c}:P={res[c][0]} hit={res[c][1]} R={res[c][2]:.3f}" for c in sorted(res))
    print(f"EVAL val200: teacher_boxes={g_} | {tag} | ms={ex['ms']:.3f} "
          f"学生框长边={ex['sw']:.0f}(教师{ex['tw']:.0f}) biou={ex['biou']:.3f} "
          f"(bestR={best_rc:.3f}@{best_ep})", flush=True)
    torch.save({"model": model.state_dict()}, os.path.join(args.out, "final.pth"))
    print("B2_DONE", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
