#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HQ 1080p 输入对照实验 · 训练脚本（本目录专用）

来源：`~/datasets/imx415_hq60_runs/train_hq60.py` 的**逐字复制**，
      仅改 6 处（见下方"与 train_hq60.py 的差异"）。既有脚本**保持原样未改**。
目的：做一个「**只变输入尺寸**」的公平对照臂 —— 基座、数据、超参、损失、增强、种子
      全部与权重 E 的配方相同，唯一差异 = 输入几何 960×544 → **1920×1080**。

与 train_hq60.py 的差异（**仅此 6 处**）：
  ① 几何：`W, CONTENT_H, PAD = 960, 540, 2` → **`1920, 1080, 0`**（⇒ `H = 1080`）
  ② `--tag`  默认 `1003A-60pairs-3e-5` → **`1003F-hq1080-3e-5`**
  ③ `--out-dir` 默认 `~/datasets/imx415_hq60_runs` → **`~/datasets/imx415_hq1080_runs`**
  ④ 报告里的 `name`  `"1003A"` → **`"1003F-hq1080"`**
  ⑤ **梯度检查点**：`enable_grad_checkpoint()` 逐个 `IGAB` 包（默认开，`--no-ckpt` 关）
     —— 1080p 激活显存 22.54 GiB → **7.77 GiB**（实测，loss 逐位一致 ⇒ 数学等价）
  ⑥ **周期存盘**：`--save-every`（默认 5）每 5 epoch 存 `{tag}_ep{N}.pth`
     —— 2026-10-08 因 GPU 掉总线丢了 31 epoch 的教训

口径（与 train_hq60.py 逐条一致）：
  · RGB（imread 后 BGR->RGB）· 整幅等比缩至 W×CONTENT_H + 上下各 PAD 行零 pad → W×H → 输出裁回 W×CONTENT_H
  · 不切块 · 仅 /255（无 mean/std）· 仅几何增强 hflip/vflip
  · lr 3e-5 · 40 epoch · bs=1 · AMP · Adam · cosine 至 1e-6 · L1(mean) · seed 100
  · 起点 = LOL_v1.pth（sha 9b4810dc…）· 训练集只读

效力：**非验收依据**（规则 10-evidence.md §1 / 00-ssot.md §3.5）；纯离线训练，不碰板端。
显存前提（本机 RTX 3090 24GB 实测）：**1080p 需开 AMP**（FP32 会 OOM；AMP 峰值 22.54 GiB）✓
"""
import argparse, hashlib, importlib.util, json, os, random, time
import cv2, numpy as np, torch, torch.nn.functional as F
from torch.utils.checkpoint import checkpoint as _ckpt

REPO = "/home/jenkolux/PKU_WH/Img_enhance_demo"
ARCH = os.path.join(REPO, "refs/pre-research/basicsr/models/archs/RetinexFormer_arch.py")
WEIGHTS = os.path.join(REPO, "refs/pre-research/pretrain_model")
PAIRS = os.path.expanduser("~/datasets/imx415_hq_train60/pairs.json")

W, CONTENT_H, PAD = 1920, 1080, 0          # ① 几何：960,540,2 → 1920,1080,0
H = CONTENT_H + 2 * PAD  # 1080


def _load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


RetinexFormer = _load("RetinexFormer_arch", ARCH).RetinexFormer


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


def build(init, device):
    model = RetinexFormer(in_channels=3, out_channels=3, n_feat=40, stage=1, num_blocks=[1, 2, 2])
    sd = torch.load(init, map_location="cpu")
    if isinstance(sd, dict):
        sd = sd.get("params", sd.get("state_dict", sd))
    sd = {k.replace("module.", ""): v for k, v in sd.items()}
    missing, unexpected = model.load_state_dict(sd, strict=False)
    print(f"  init={os.path.basename(init)} missing={len(missing)} unexpected={len(unexpected)}", flush=True)
    return model.to(device), len(missing), len(unexpected)


def enable_grad_checkpoint(model, cls_names=("IGAB",)):
    """**按模块粒度**打梯度检查点：仅对 `cls_names` 列出的子模块（默认 `IGAB` = 注意力块）
    在前向时只存输入/输出、反向时逐块重算 ⇒ 降低激活显存。

    ⚠️ 为什么不包整个 `body`：把整网当**一个**检查点块时，反向重算仍会把全块激活一次性
    物化（实测峰值 22.60 GiB，与不打的 22.54 GiB 无差）。只有切成**多块**才真正省显存。
    ⚠️ 只包一层、不嵌套（重算期再触发检查点会踩 `inner_unpack` 报错）。
    **不改 arch 文件、不改数学、不改超参**（数值等价，仅重算）。
    """
    wrapped = []
    for name, m in model.named_modules():
        if type(m).__name__ in cls_names:
            orig = m.forward

            def forward(*args, _orig=orig, **kw):
                return _ckpt(_orig, *args, use_reentrant=False, **kw)

            m.forward = forward
            wrapped.append(name)
    return wrapped


def psnr(a, b):
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return 100.0 if mse == 0 else 10.0 * np.log10(1.0 / mse)


@torch.inference_mode()
def identity_psnr(pool):
    vals = []
    for lo, hi in pool:
        vals.append(psnr(hi.astype(np.float64) / 255., lo.astype(np.float64) / 255.))
    return float(np.mean(vals))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", default=os.path.join(WEIGHTS, "LOL_v1.pth"))
    ap.add_argument("--tag", default="1003F-hq1080-3e-5")                       # ②
    ap.add_argument("--out-dir", default=os.path.expanduser("~/datasets/imx415_hq1080_runs"))  # ③
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--lr", type=float, default=3e-5)
    ap.add_argument("--min-lr", type=float, default=1e-6)
    ap.add_argument("--bs", type=int, default=1)
    ap.add_argument("--amp", action="store_true")
    ap.add_argument("--no-ckpt", action="store_true", help="关闭梯度检查点（默认开启，省显存）")
    ap.add_argument("--save-every", type=int, default=5, help="每 N 个 epoch 存一次中间权重（0=不存）")
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
    init_sha = sha256(args.init)
    gtB = float(np.mean([hi.astype(np.float64).mean() / 255. for _, hi in pool]))
    ip = identity_psnr(pool)
    print(f"[{args.tag}] n={len(pool)} lr={args.lr} epochs={args.epochs} gtB={gtB:.4f} device={device}", flush=True)

    t0 = time.time()
    model, nm, nu = build(args.init, device)
    if not args.no_ckpt:
        nw = enable_grad_checkpoint(model)
        print(f"  [grad-checkpoint] on：{len(nw)} 个 IGAB 已包（逐块重算）", flush=True)
    model.train()
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(args.epochs, 1), eta_min=args.min_lr)
    rng = np.random.default_rng(args.seed + 1)
    use_amp = args.amp and device == "cuda"
    scaler = torch.cuda.amp.GradScaler(enabled=use_amp)
    print(f"  [identity(上训集)] PSNR={ip:.4f}", flush=True)

    hist, ema, best_ema, wait, stopped = [], None, 1e9, 0, args.epochs
    start_loss = None
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
        ml = float(np.mean(losses)); start_loss = ml if ep == 1 else start_loss
        ema = ml if ema is None else 0.6 * ema + 0.4 * ml
        hist.append({"epoch": ep, "lr": opt.param_groups[0]["lr"], "train_l1": round(ml, 6),
                     "ema": round(ema, 6), "sec": round(time.time() - te, 1)})
        peak_gib = round(torch.cuda.max_memory_allocated() / 2**30, 2)
        print(f"  ep{ep:3d} lr={opt.param_groups[0]['lr']:.2e} L1={ml:.6f} ema={ema:.6f} "
              f"({hist[-1]['sec']}s) peak={peak_gib}GiB", flush=True)
        if args.save_every and ep % args.save_every == 0:
            mid = os.path.join(args.out_dir, f"{args.tag}_ep{ep}.pth")
            torch.save({"params": model.state_dict(), "epoch": ep,
                        "lr": opt.param_groups[0]["lr"], "train_l1": round(ml, 6)}, mid)
            print(f"  [ckpt] {os.path.basename(mid)}", flush=True)
        if ema < best_ema - args.min_delta:
            best_ema, wait = ema, 0
        else:
            wait += 1
            if wait >= args.patience:
                stopped = ep
                print(f"  [stop] 训练损失收敛（EMA 平台 {args.patience} 轮无 >{args.min_delta} 改善）@ ep{ep}", flush=True)
                break
    ckpt = os.path.join(args.out_dir, f"{args.tag}_final.pth")
    torch.save({"params": model.state_dict()}, ckpt)
    ckpt_sha = sha256(ckpt)
    rep = {"tag": args.tag, "name": "1003F-hq1080", "init": args.init, "init_sha256": init_sha,   # ④
           "final_sha256": ckpt_sha, "lr": args.lr, "epochs_cap": args.epochs,
           "epochs_ran": hist[-1]["epoch"], "stopped_early": stopped < args.epochs, "bs": args.bs,
           "train_n": len(pool), "seed": args.seed, "gt_bright_train": round(gtB, 4),
           "identity_train_psnr": round(ip, 4), "order": args.order,
           "geometry": f"{W}x{H} (content {W}x{CONTENT_H} + {PAD} rows zero pad)",
           "normalization": "/255 only", "loss": "L1(mean) no held-out",
           "aug": "hflip/vflip", "amp": bool(use_amp), "min_lr": args.min_lr,
           "grad_checkpoint": ("per-IGAB" if not args.no_ckpt else None),
           "save_every": args.save_every,
           "stop_rule": f"train-loss EMA plateau, patience={args.patience}, min_delta={args.min_delta}",
           "l1_first": hist[0]["train_l1"], "l1_last": hist[-1]["train_l1"], "l1_ema_last": hist[-1]["ema"],
           "history": hist, "ckpt": ckpt, "sha256": ckpt_sha,
           "wall_sec": round(time.time() - t0, 1),
           "train_batches": sorted({p["batch"] for p in pairs}),
           "peak_vram_gib": round(torch.cuda.max_memory_allocated() / 2**30, 2),
           "baseline_note": "对照臂：与权重 E 同基座/同数据/同超参，唯一差异 = 输入几何 1920x1080（E 为 960x544）"}
    with open(os.path.join(args.out_dir, "train_specs.json"), "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f"[{args.tag}] done L1 {hist[0]['train_l1']:.6f} -> {hist[-1]['train_l1']:.6f} "
          f"ep={hist[-1]['epoch']} sha={rep['final_sha256']} {rep['wall_sec']}s", flush=True)


if __name__ == "__main__":
    main()