#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L1 论文口径基线 harness（O132 / O133 / O134 裁定的执行体）。

在**数据机**（RTX 3090）上运行；依赖 = 官方 Retinexformer 仓库 + 9 个官方权重 + 官方验证集。
不安装 basicsr —— 用 importlib 直接加载 `basicsr/models/archs/RetinexFormer_arch.py` 与
`Enhancement/utils.py`（前者只依赖 torch/einops；后者只依赖 cv2/numpy/math）。

口径（v3.86 追加 77 裁定）：
  ① 论文可比：数据集**原生分辨率** + 官方 pad/4（reflect）
  ② 部署 shape：等比缩放到**内容 960×540** + 上下各 **2 行零 pad** → 960×544 → 推理 → 裁回 960×540；
     GT 同步缩放到 960×540
  GT_mean：**开 / 关两列**（复刻官方 `test_from_dataset.py --GT_mean` 语义，含其 BGR2GRAY 通道序原样）

指标（**位一致**，直接调用官方函数）：
  PSNR = `utils.PSNR`            （[0,1] 域、全通道 RGB）
  SSIM = `utils.calculate_ssim`  （8-bit、逐通道 RGB、11×11 高斯 σ=1.5）

用法：
  python3 l1_eval.py --out /tmp/l1_report.json
"""
import argparse
import importlib.util
import json
import os
import sys
import time

import cv2
import numpy as np
import torch
import torch.nn.functional as F

REPO = os.path.expanduser("~/rk_demo/Retinexformer")
DATA = os.path.expanduser("~/mycode/Dataset_Create/dark/RetinexFormer_TrainHQ")
FACTOR = 4
DEPLOY_W, DEPLOY_CONTENT_H, DEPLOY_PAD = 960, 540, 2


def _load(mod_name, path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_arch = _load("RetinexFormer_arch", os.path.join(REPO, "basicsr/models/archs/RetinexFormer_arch.py"))
RetinexFormer = _arch.RetinexFormer
_u = _load("rf_utils", os.path.join(REPO, "Enhancement/utils.py"))
load_img, PSNR, calculate_ssim = _u.load_img, _u.PSNR, _u.calculate_ssim
from skimage import img_as_ubyte  # noqa: E402

# ---- 9 个官方上游权重（O133 裁定的对照臂）----
ARMS = {
    "LOL_v1": "pretrained_weights/LOL_v1.pth",
    "LOL_v2_real": "pretrained_weights/LOL_v2_real.pth",
    "LOL_v2_synthetic": "pretrained_weights/LOL_v2_synthetic.pth",
    "NTIRE": "pretrained_weights/NTIRE.pth",
    "SID": "pretrained_weights/SID.pth",
    "SDSD_indoor": "pretrained_weights/SDSD_indoor.pth",
    "SDSD_outdoor": "pretrained_weights/SDSD_outdoor.pth",
    "FiveK": "pretrained_weights/FiveK.pth",
    "SMID": "pretrained_weights/SMID.pth",
    "no-op": None,
}

# ---- 官方验证集（low, high）----
DATASETS = {
    "LOL_V1": ("LOL_V1/test_low", "LOL_V1/test_high"),
    "LOL_V2_Real": ("LOL_V2/Real_captured/Test/Low", "LOL_V2/Real_captured/Test/Normal"),
    "LOL_V2_Syn": ("LOL_V2/Synthetic/Test/Low", "LOL_V2/Synthetic/Test/Normal"),
    "VE_LOL_Cap": ("VE_LOL_M/extracted/VE-LOL-L-Cap-Full/VE-LOL-L-Cap-Low_test",
                   "VE_LOL_M/extracted/VE-LOL-L-Cap-Full/VE-LOL-L-Cap-Normal_test"),
}


def pairs_of(ds):
    lo, hi = DATASETS[ds]
    a = sorted(os.path.join(DATA, lo, f) for f in os.listdir(os.path.join(DATA, lo))
               if f.lower().endswith((".png", ".jpg")))
    b = sorted(os.path.join(DATA, hi, f) for f in os.listdir(os.path.join(DATA, hi))
               if f.lower().endswith((".png", ".jpg")))
    assert len(a) == len(b), (ds, len(a), len(b))
    return list(zip(a, b))


def build(weights, device):
    model = RetinexFormer(in_channels=3, out_channels=3, n_feat=40, stage=1, num_blocks=[1, 2, 2])
    sd = torch.load(weights, map_location="cpu")
    if isinstance(sd, dict):
        sd = sd.get("params", sd.get("state_dict", sd))
    sd = {k.replace("module.", ""): v for k, v in sd.items()}
    missing, unexpected = model.load_state_dict(sd, strict=False)
    model.eval().to(device)
    return model, len(missing), len(unexpected)


@torch.inference_mode()
def infer(model, x):
    b, c, h, w = x.shape
    ph, pw = (-h) % FACTOR, (-w) % FACTOR
    if ph or pw:
        x = F.pad(x, (0, pw, 0, ph), "reflect")
    y = model(x)
    return y[:, :, :h, :w].clamp(0, 1)


def to_tensor(rgb01, device):
    return torch.from_numpy(rgb01.transpose(2, 0, 1)).unsqueeze(0).to(device)


def deploy_prep(rgb01):
    """等比缩放到内容 960×540，上下各 2 行零 pad → 960×544"""
    c = cv2.resize(rgb01, (DEPLOY_W, DEPLOY_CONTENT_H), interpolation=cv2.INTER_AREA)
    z = np.zeros((DEPLOY_PAD, DEPLOY_W, 3), np.float32)
    return np.concatenate([z, c, z], axis=0)          # 960×544


def deploy_crop(rgb01):
    return rgb01[DEPLOY_PAD:DEPLOY_PAD + DEPLOY_CONTENT_H]   # → 960×540


def gt_mean_rect(restored, target):
    """复刻官方 --GT_mean（含其 BGR2GRAY 通道序原样）"""
    mr = cv2.cvtColor(restored.astype(np.float32), cv2.COLOR_BGR2GRAY).mean()
    mt = cv2.cvtColor(target.astype(np.float32), cv2.COLOR_BGR2GRAY).mean()
    return np.clip(restored * (mt / mr), 0, 1)


def metrics(restored, target):
    """返回 (gt_mean 关, gt_mean 开) 两组 (psnr, ssim)"""
    out = []
    for r in (restored, gt_mean_rect(restored, target)):
        out.append((float(PSNR(target, r)),
                    float(calculate_ssim(img_as_ubyte(target), img_as_ubyte(r)))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", nargs="*", default=list(ARMS))
    ap.add_argument("--datasets", nargs="*", default=list(DATASETS))
    ap.add_argument("--out", default="/tmp/l1_report.json")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--extra", nargs="*", default=[],
                    help="额外臂，格式 `NAME=PATH`（PATH 可为绝对路径，如微调后检查点）")
    args = ap.parse_args()

    for kv in args.extra:                       # 微调后权重等临时臂，不写死进 ARMS
        name, path = kv.split("=", 1)
        ARMS[name] = path

    device = args.device if torch.cuda.is_available() else "cpu"
    print(f"device={device}  torch={torch.__version__}", flush=True)

    cache = {}
    for ds in args.datasets:
        cache[ds] = pairs_of(ds)
        print(f"  {ds}: n={len(cache[ds])}", flush=True)

    rep = {"meta": {"repo": REPO, "data": DATA, "device": device,
                    "deploy": {"content": f"{DEPLOY_W}x{DEPLOY_CONTENT_H}", "pad_rows": DEPLOY_PAD},
                    "metric": "official Enhancement/utils.py: PSNR([0,1] all-channel) + calculate_ssim(8bit per-channel 11x11 gauss sigma=1.5)"},
           "results": {}}

    for arm in args.arms:
        wrel = ARMS[arm]
        model = None
        if wrel is not None:
            wp = os.path.join(REPO, wrel)
            t0 = time.time()
            model, nm, nu = build(wp, device)
            print(f"\n[{arm}] loaded {wrel}  missing={nm} unexpected={nu}  ({time.time()-t0:.1f}s)", flush=True)
        else:
            print("\n[no-op] identity", flush=True)
        rep["results"][arm] = {}

        for ds in args.datasets:
            rec = {}
            for geo in ("paper", "deploy"):
                acc = {"off": [[], []], "on": [[], []]}
                for lp, hp in cache[ds]:
                    low = np.float32(load_img(lp)) / 255.
                    high = np.float32(load_img(hp)) / 255.
                    if geo == "paper":
                        if model is None:
                            restored = low
                        else:
                            y = infer(model, to_tensor(low, device))
                            restored = y.squeeze(0).permute(1, 2, 0).cpu().numpy()
                        target = high
                    else:
                        lin = deploy_prep(low)
                        if model is None:
                            restored = deploy_crop(lin)
                        else:
                            y = infer(model, to_tensor(lin, device))
                            restored = deploy_crop(y.squeeze(0).permute(1, 2, 0).cpu().numpy())
                        target = cv2.resize(high, (DEPLOY_W, DEPLOY_CONTENT_H), interpolation=cv2.INTER_AREA)
                    (p0, s0), (p1, s1) = metrics(restored, target)
                    acc["off"][0].append(p0); acc["off"][1].append(s0)
                    acc["on"][0].append(p1); acc["on"][1].append(s1)
                rec[geo] = {
                    "gt_mean_off": {"psnr": round(float(np.mean(acc["off"][0])), 4),
                                    "ssim": round(float(np.mean(acc["off"][1])), 4)},
                    "gt_mean_on": {"psnr": round(float(np.mean(acc["on"][0])), 4),
                                   "ssim": round(float(np.mean(acc["on"][1])), 4)},
                    "n": len(acc["off"][0]),
                }
            rep["results"][arm][ds] = rec
            print(f"  {ds:12s} paper(off) PSNR={rec['paper']['gt_mean_off']['psnr']:.4f} "
                  f"SSIM={rec['paper']['gt_mean_off']['ssim']:.4f} | "
                  f"paper(on) {rec['paper']['gt_mean_on']['psnr']:.4f}/{rec['paper']['gt_mean_on']['ssim']:.4f} | "
                  f"deploy(off) {rec['deploy']['gt_mean_off']['psnr']:.4f}/{rec['deploy']['gt_mean_off']['ssim']:.4f}",
                  flush=True)
        del model
        torch.cuda.empty_cache()

    with open(args.out, "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print("\nwrote " + args.out, flush=True)


if __name__ == "__main__":
    main()
