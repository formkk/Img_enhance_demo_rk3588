#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Retinexformer 域适配微调（`docs/design/dataset.md` §5.2.12 的执行体）。

在**数据机**（RTX 3090）上运行；不安装 basicsr —— 用 importlib 直接加载
`basicsr/models/archs/RetinexFormer_arch.py`（只依赖 torch/einops）。

设计取值（§5.2.12，用户 2026-09-24 已定）：
  数据  训练池 = `LOL_V2/Real_captured/Train` 689 场景 → 620 训练 + 69 域适配验证
        切分按**场景**整体（`seed=100`，`np.random.default_rng`，可复现）
        官方 test 100 只用于最终报告、**不参与选型**
  权重  E1 = `LOL_v2_real.pth` / E2 = `LOL_v1.pth`（E0 = 不微调，即 L1 基线）
  配方  L1(mean) · Adam lr→cosine 至 1e-6 · 30 epoch 早停 · 全参微调
        输入几何 = 部署 shape（内容 960×540 + 上下各 2 行零 pad → 960×544）
        数据增强 = 仅几何（hflip / vflip / rot90）
  评测  验证 PSNR 用官方 `Enhancement/utils.py: PSNR`，**GT_mean 关**（主判据口径）

通道序口径：官方 `test_from_dataset.py` 用 `cv2.imread` 直出（BGR）喂模型，`tools/l1_eval.py`
原样复刻该口径 ⇒ 本脚本同样**不做 BGR→RGB 转换**，保证训练 / 评测口径一致。

用法（数据机）：
  python3 l1_finetune.py --dump-split /tmp/split_seed100.json
  python3 l1_finetune.py --init LOL_v2_real.pth --tag E1 --epochs 30 --lr 1e-4
  python3 l1_finetune.py --init LOL_v1.pth --tag E2 --epochs 30 --lr 1e-4
  # LR 小扫描（小池 + 短 epoch）
  python3 l1_finetune.py --init LOL_v2_real.pth --tag sweep_lr5e-5 --lr 5e-5 \
      --pool-size 150 --epochs 10 --no-ckpt
"""
import argparse
import importlib.util
import json
import os
import random
import time

import cv2
import numpy as np
import torch
import torch.nn.functional as F

REPO = os.path.expanduser("~/rk_demo/Retinexformer")
DATA = os.path.expanduser("~/mycode/Dataset_Create/dark/RetinexFormer_TrainHQ")
TRAIN_LOW = os.path.join(DATA, "LOL_V2/Real_captured/Train/Low")
TRAIN_HIGH = os.path.join(DATA, "LOL_V2/Real_captured/Train/Normal")

N_SCENES = 689
N_VAL = 69
SPLIT_SEED = 100
W, CONTENT_H, PAD = 960, 540, 2
H = CONTENT_H + 2 * PAD                      # 544
FACTOR = 4


def _load(mod_name, path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_arch = _load("RetinexFormer_arch", os.path.join(REPO, "basicsr/models/archs/RetinexFormer_arch.py"))
RetinexFormer = _arch.RetinexFormer
_u = _load("rf_utils", os.path.join(REPO, "Enhancement/utils.py"))
PSNR = _u.PSNR


# ---------------------------------------------------------------- 切分
def make_split(seed=SPLIT_SEED):
    """场景级切分：689 → (620 train, 69 val)，按场景整体、固定种子可复现。"""
    ids = np.arange(1, N_SCENES + 1)
    perm = np.random.default_rng(seed).permutation(N_SCENES)
    val = np.sort(perm[:N_VAL] + 1).tolist()
    tr = np.sort(perm[N_VAL:] + 1).tolist()
    assert len(val) == N_VAL and len(tr) == N_SCENES - N_VAL
    assert not (set(val) & set(tr))
    return tr, val


# ---------------------------------------------------------------- 数据
def load_pool(ids):
    """按部署几何（960×540）预载为 uint8，省内存。"""
    out = []
    for sid in ids:
        lo = cv2.imread(os.path.join(TRAIN_LOW, f"low{sid:05d}.png"))
        hi = cv2.imread(os.path.join(TRAIN_HIGH, f"normal{sid:05d}.png"))
        assert lo is not None and hi is not None, sid
        lo = cv2.resize(lo, (W, CONTENT_H), interpolation=cv2.INTER_AREA)
        hi = cv2.resize(hi, (W, CONTENT_H), interpolation=cv2.INTER_AREA)
        out.append((lo, hi))
    return out


def augment(lo, hi, rng):
    """仅几何增强，**保长宽比**子群（恒等 / hflip / vflip / 180°）。

    注：§5.2.12 原写「水平/垂直翻转、90° 旋转」，但 90° 旋转会把内容 960×540 变成
    540×960 ⇒ 与固定部署几何冲突，故只取保长宽比的二面体子群（已在证据档登记偏离）。
    """
    if rng.random() < 0.5:
        lo, hi = lo[:, ::-1], hi[:, ::-1]
    if rng.random() < 0.5:
        lo, hi = lo[::-1, :], hi[::-1, :]
    return np.ascontiguousarray(lo), np.ascontiguousarray(hi)


def to_batch(imgs_u8):
    """uint8 HWC → float32 0..1，内容 960×540 → 上下各 2 行零 pad → 960×544。"""
    a = np.stack(imgs_u8).astype(np.float32) / 255.
    z = np.zeros((a.shape[0], PAD, W, 3), np.float32)
    a = np.concatenate([z, a, z], axis=1)                    # B×544×960×3
    return torch.from_numpy(a.transpose(0, 3, 1, 2))         # B×3×544×960


# ---------------------------------------------------------------- 模型
def build(init, device):
    model = RetinexFormer(in_channels=3, out_channels=3, n_feat=40, stage=1, num_blocks=[1, 2, 2])
    if init:
        sd = torch.load(init, map_location="cpu")
        if isinstance(sd, dict):
            sd = sd.get("params", sd.get("state_dict", sd))
        sd = {k.replace("module.", ""): v for k, v in sd.items()}
        missing, unexpected = model.load_state_dict(sd, strict=False)
        print(f"  init={init} missing={len(missing)} unexpected={len(unexpected)}", flush=True)
    return model.to(device)


@torch.inference_mode()
def eval_psnr(model, pool, device, bs=4):
    """验证 PSNR（官方函数，GT_mean 关，部署几何）。"""
    model.eval()
    vals = []
    for i in range(0, len(pool), bs):
        chunk = pool[i:i + bs]
        x = to_batch([p[0] for p in chunk]).to(device)
        y = model(x)[:, :, PAD:PAD + CONTENT_H].clamp(0, 1)
        for j in range(len(chunk)):
            gt = chunk[j][1].astype(np.float32) / 255.
            vals.append(float(PSNR(gt, y[j].permute(1, 2, 0).cpu().numpy())))
    model.train()
    return float(np.mean(vals))


# ---------------------------------------------------------------- 训练
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", default=None, help="官方权重文件名（相对 pretrained_weights/）或绝对路径")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--out-dir", default="/tmp/l1_finetune")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--min-lr", type=float, default=1e-6)
    ap.add_argument("--bs", type=int, default=1)
    ap.add_argument("--amp", action="store_true", help="torch.cuda.amp 混合精度（仅提速/省显存，不改配方）")
    ap.add_argument("--eval-bs", type=int, default=2)
    ap.add_argument("--patience", type=int, default=10)
    ap.add_argument("--pool-size", type=int, default=0, help=">0 时只取前 N 个训练场景（小扫描用）")
    ap.add_argument("--seed", type=int, default=SPLIT_SEED)
    ap.add_argument("--no-ckpt", action="store_true")
    ap.add_argument("--dump-split", default=None)
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    tr_ids, val_ids = make_split(args.seed)
    if args.dump_split:
        with open(args.dump_split, "w") as f:
            json.dump({"seed": args.seed, "pool": "LOL_V2/Real_captured/Train",
                       "n_train": len(tr_ids), "n_val": len(val_ids),
                       "train_ids": tr_ids, "val_ids": val_ids,
                       "report_ids": list(range(690, 790))}, f, indent=1)
        print(f"wrote split {args.dump_split}  train={len(tr_ids)} val={len(val_ids)}", flush=True)
        return

    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    device = args.device if torch.cuda.is_available() else "cpu"
    os.makedirs(args.out_dir, exist_ok=True)

    init = args.init
    if init and not os.path.isabs(init):
        init = os.path.join(REPO, "pretrained_weights", init)

    t0 = time.time()
    print(f"[{args.tag}] device={device} torch={torch.__version__} lr={args.lr} "
          f"epochs={args.epochs} bs={args.bs} pool={args.pool_size or 'full-620'}", flush=True)
    tr_ids_use = tr_ids
    if args.pool_size:
        sub = np.random.default_rng(args.seed + 2).permutation(len(tr_ids))[:args.pool_size]
        tr_ids_use = sorted(tr_ids[i] for i in sub)
    print(f"  loading {len(tr_ids_use)} train + {len(val_ids)} val @ {W}x{CONTENT_H} ...", flush=True)
    tr_pool = load_pool(tr_ids_use)
    va_pool = load_pool(val_ids)
    print(f"  loaded in {time.time()-t0:.1f}s", flush=True)

    model = build(init, device)
    model.train()
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(args.epochs, 1), eta_min=args.min_lr)
    rng = np.random.default_rng(args.seed + 1)
    use_amp = args.amp and device == "cuda"
    scaler = torch.cuda.amp.GradScaler(enabled=use_amp)

    hist, best = [], {"epoch": -1, "psnr": -1e9}
    if args.epochs == 0:                      # 纯评测：取 E0（未微调）在 69 验证集上的下界
        vp = eval_psnr(model, va_pool, device, args.eval_bs)
        hist.append({"epoch": 0, "lr": args.lr, "train_l1": None,
                     "val_psnr": round(vp, 4), "sec": 0.0})
        best = {"epoch": 0, "psnr": round(vp, 4)}
        print(f"  [E0] valPSNR={vp:.4f}", flush=True)
    for ep in range(1, args.epochs + 1):
        order = rng.permutation(len(tr_pool))
        te = time.time(); losses = []
        for i in range(0, len(order), args.bs):
            idx = order[i:i + args.bs]
            lo, hi = [], []
            for k in idx:
                a, b = augment(tr_pool[k][0], tr_pool[k][1], rng)
                lo.append(a); hi.append(b)
            x = to_batch(lo).to(device)
            gt = torch.from_numpy(np.stack(hi).astype(np.float32) / 255.).permute(0, 3, 1, 2).to(device)
            with torch.autocast("cuda", enabled=use_amp):
                y = model(x)[:, :, PAD:PAD + CONTENT_H]
                loss = F.l1_loss(y, gt)
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            losses.append(float(loss))
        sched.step()
        vp = eval_psnr(model, va_pool, device, args.eval_bs)
        hist.append({"epoch": ep, "lr": opt.param_groups[0]["lr"],
                     "train_l1": round(float(np.mean(losses)), 5), "val_psnr": round(vp, 4),
                     "sec": round(time.time() - te, 1)})
        print(f"  ep{ep:3d} lr={opt.param_groups[0]['lr']:.2e} L1={np.mean(losses):.5f} "
              f"valPSNR={vp:.4f} ({hist[-1]['sec']}s)", flush=True)
        if vp > best["psnr"]:
            best = {"epoch": ep, "psnr": round(vp, 4)}
            if not args.no_ckpt:
                torch.save({"params": model.state_dict()}, os.path.join(args.out_dir, f"{args.tag}_best.pth"))
        if ep - best["epoch"] >= args.patience:
            print(f"  early stop at ep{ep} (best ep{best['epoch']} {best['psnr']:.4f})", flush=True)
            break

    rep = {"tag": args.tag, "init": init, "lr": args.lr, "epochs_run": len(hist),
           "bs": args.bs, "pool_size": len(tr_ids_use), "seed": args.seed,
           "geometry": f"{W}x{H} (content {W}x{CONTENT_H} + {PAD} rows zero pad)",
           "aug": "hflip/vflip (aspect-preserving dihedral subgroup)", "loss": "L1(mean)",
           "amp": bool(args.amp), "best": best, "history": hist,
           "ckpt": None if args.no_ckpt else os.path.join(args.out_dir, f"{args.tag}_best.pth"),
           "wall_sec": round(time.time() - t0, 1)}
    with open(os.path.join(args.out_dir, f"{args.tag}.json"), "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f"[{args.tag}] done best={best}  {rep['wall_sec']}s", flush=True)


if __name__ == "__main__":
    main()
