#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量 UNet 对照臂 · 训练脚本（本目录专用）

目的：与**现役 HQ（RetinexFormer，960×544）**做「**只换架构**」的对照 —— 几何锁死现役档
      960×544，看 UNet 的噪声表现是否更好。

配方与 `~/datasets/imx415_hq60_runs/train_hq60.py`（= 现役配方来源）逐条一致：
  · RGB（imread 后 BGR→RGB）· 整幅等比缩至 960×540 + 上下各 2 行零 pad → 960×544 → 输出裁回 960×540
  · 不切块 · 仅 /255（无 mean/std）· 仅几何增强 hflip/vflip · 训练集只读
  · lr 3e-5 · 40 epoch · bs=1 · AMP · Adam · cosine 至 1e-6 · L1(mean) · seed 100
  · 判停 = 训练损失 EMA 平台（无 held-out）· 取 final 权重

与 train_hq60.py 的差异（**仅此 3 处**）：
  ① **架构**：`RetinexFormer` → **轻量 `UNet`**（`hq_optimize/code/unet_arch.py`；
     1.448 M 参数，**比现役 1.606 M 更轻**）
  ② **初始化**：`LOL_v1.pth` → **随机初始化**（UNet 无对应预训练权重可用 ⇒ **此处不对等，
     必须随结论登记**：RetinexFormer 臂享受了预训练起点，UNet 臂没有）
  ③ tag / out-dir / 报告名改成本轮专用

效力：**非验收依据**（规则 `10-evidence.md` §1 / `00-ssot.md` §3.5）；纯离线训练，不碰板端。
"""
import argparse, hashlib, importlib.util, json, os, random, time
import cv2, numpy as np, torch, torch.nn.functional as F

REPO = "/home/jenkolux/PKU_WH/Img_enhance_demo"
ARCH = os.path.join(REPO, "hq_optimize/code/unet_arch.py")     # ①
PAIRS = os.path.expanduser("~/datasets/imx415_hq_train60/pairs.json")

W, CONTENT_H, PAD = 960, 540, 2                                # 锁死现役档
H = CONTENT_H + 2 * PAD                                        # 544


def _load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


UNet = _load("unet_arch", ARCH).UNet


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()


def load_pool(pairs, order="rgb"):
    out = []
    for p in pairs:
        lo = cv2.imread(p["dark"]); hi = cv2.imread(p["gt"])
        assert lo is not None and hi is not None, p["dark"]
        if order == "rgb":
            lo, hi = lo[..., ::-1].copy(), hi[..., ::-1].copy()
        lo = cv2.resize(lo, (W, CONTENT_H), interpolation=cv2.INTER_AREA)
        hi = cv2.resize(hi, (W, CONTENT_H), interpolation=cv2.INTER_AREA)
        out.append((lo, hi))
    return out


def augment(lo, hi, rng):
    if rng.random() < 0.5:
        lo, hi = lo[:, ::-1], hi[:, ::-1]
    if rng.random() < 0.5:
        lo, hi = lo[::-1, :], hi[::-1, :]
    return np.ascontiguousarray(lo), np.ascontiguousarray(hi)


def to_batch(imgs):
    a = np.stack(imgs).astype(np.float32) / 255.
    z = np.zeros((a.shape[0], PAD, W, 3), np.float32)
    a = np.concatenate([z, a, z], axis=1)
    return torch.from_numpy(a.transpose(0, 3, 1, 2))


def build(device):
    model = UNet(in_ch=3, out_ch=3)                            # ① 架构
    n = sum(p.numel() for p in model.parameters())
    print(f"  arch=UNet params={n/1e6:.3f}M init=random（无预训练）", flush=True)   # ②
    return model.to(device), n


def psnr(a, b):
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return 100.0 if mse == 0 else 10.0 * np.log10(1.0 / mse)


@torch.inference_mode()
def identity_psnr(pool):
    return float(np.mean([psnr(hi.astype(np.float64) / 255., lo.astype(np.float64) / 255.)
                          for lo, hi in pool]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="1003G-unet960-3e-5")                                  # ③
    ap.add_argument("--out-dir", default=os.path.expanduser("~/datasets/imx415_unet960_runs"))  # ③
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--lr", type=float, default=3e-5)
    ap.add_argument("--min-lr", type=float, default=1e-6)
    ap.add_argument("--bs", type=int, default=1)
    ap.add_argument("--amp", action="store_true")
    ap.add_argument("--save-every", type=int, default=5)
    ap.add_argument("--seed", type=int, default=100)
    ap.add_argument("--patience", type=int, default=6)
    ap.add_argument("--min-delta", type=float, default=5e-5)
    ap.add_argument("--order", choices=["bgr", "rgb"], default="rgb")
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    device = args.device if torch.cuda.is_available() else "cpu"
    os.makedirs(args.out_dir, exist_ok=True)

    pairs = json.load(open(PAIRS))["pairs"]
    pool = load_pool(pairs, args.order)
    gtB = float(np.mean([hi.astype(np.float64).mean() / 255. for _, hi in pool]))
    ip = identity_psnr(pool)
    print(f"[{args.tag}] n={len(pool)} lr={args.lr} epochs={args.epochs} gtB={gtB:.4f} device={device}", flush=True)

    t0 = time.time()
    model, nparam = build(device)
    model.train()
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(args.epochs, 1), eta_min=args.min_lr)
    rng = np.random.default_rng(args.seed + 1)
    use_amp = args.amp and device == "cuda"
    scaler = torch.cuda.amp.GradScaler(enabled=use_amp)
    print(f"  [identity(上训集)] PSNR={ip:.4f}", flush=True)

    hist, ema, best_ema, wait, stopped = [], None, 1e9, 0, args.epochs
    for ep in range(1, args.epochs + 1):
        order = rng.permutation(len(pool)); te = time.time(); losses = []
        for i in range(0, len(order), args.bs):
            idx = order[i:i + args.bs]
            lo, hi = [], []
            for k in idx:
                a, b = augment(pool[k][0], pool[k][1], rng); lo.append(a); hi.append(b)
            x = to_batch(lo).to(device)
            gt = torch.from_numpy(np.stack(hi).astype(np.float32) / 255.).permute(0, 3, 1, 2).to(device)
            with torch.autocast("cuda", enabled=use_amp):
                y = model(x)[:, :, PAD:PAD + CONTENT_H]
                loss = F.l1_loss(y, gt)
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward(); scaler.step(opt); scaler.update()
            losses.append(float(loss))
        sched.step()
        ml = float(np.mean(losses))
        ema = ml if ema is None else 0.6 * ema + 0.4 * ml
        hist.append({"epoch": ep, "lr": opt.param_groups[0]["lr"], "train_l1": round(ml, 6),
                     "ema": round(ema, 6), "sec": round(time.time() - te, 1)})
        peak = round(torch.cuda.max_memory_allocated() / 2**30, 2)
        print(f"  ep{ep:3d} lr={opt.param_groups[0]['lr']:.2e} L1={ml:.6f} ema={ema:.6f} "
              f"({hist[-1]['sec']}s) peak={peak}GiB", flush=True)
        if args.save_every and ep % args.save_every == 0:
            mid = os.path.join(args.out_dir, f"{args.tag}_ep{ep}.pth")
            torch.save({"params": model.state_dict(), "epoch": ep}, mid)
            print(f"  [ckpt] {os.path.basename(mid)}", flush=True)
        if ema < best_ema - args.min_delta:
            best_ema, wait = ema, 0
        else:
            wait += 1
            if wait >= args.patience:
                stopped = ep
                print(f"  [stop] 训练损失收敛 @ ep{ep}", flush=True)
                break

    ckpt = os.path.join(args.out_dir, f"{args.tag}_final.pth")
    torch.save({"params": model.state_dict()}, ckpt)
    ckpt_sha = sha256(ckpt)
    rep = {"tag": args.tag, "name": "1003G-unet960", "arch": "UNet", "init": "random",
           "params_M": round(nparam / 1e6, 3), "final_sha256": ckpt_sha, "lr": args.lr,
           "epochs_cap": args.epochs, "epochs_ran": hist[-1]["epoch"],
           "stopped_early": stopped < args.epochs, "bs": args.bs, "train_n": len(pool),
           "seed": args.seed, "gt_bright_train": round(gtB, 4), "identity_train_psnr": round(ip, 4),
           "order": args.order, "geometry": f"{W}x{H} (content {W}x{CONTENT_H} + {PAD} rows zero pad)",
           "normalization": "/255 only", "loss": "L1(mean) no held-out", "aug": "hflip/vflip",
           "amp": bool(use_amp), "min_lr": args.min_lr, "save_every": args.save_every,
           "stop_rule": f"train-loss EMA plateau, patience={args.patience}, min_delta={args.min_delta}",
           "l1_first": hist[0]["train_l1"], "l1_last": hist[-1]["train_l1"],
           "l1_ema_last": hist[-1]["ema"], "history": hist, "ckpt": ckpt, "sha256": ckpt_sha,
           "wall_sec": round(time.time() - t0, 1),
           "train_batches": sorted({p["batch"] for p in pairs}),
           "peak_vram_gib": round(torch.cuda.max_memory_allocated() / 2**30, 2),
           "baseline_note": "对照臂：与现役同几何 960×544 / 同数据 / 同超参，唯一差异 = 架构"
                            "（UNet 1.448M vs RetinexFormer 1.606M），但**初始化不对等**"
                            "（现役有 LOL_v1 预训练起点，本臂随机初始化）"}
    with open(os.path.join(args.out_dir, "train_specs.json"), "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f"[{args.tag}] done L1 {hist[0]['train_l1']:.6f} -> {hist[-1]['train_l1']:.6f} "
          f"ep={hist[-1]['epoch']} sha={rep['final_sha256']} {rep['wall_sec']}s", flush=True)


if __name__ == "__main__":
    main()