#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HQ 对照臂 · 离线推理 + A/B 出图（本任务专用，开发机 GPU）

三臂（唯一差异 = 输入几何 / 训练集，见各自 note）：
  E     = 板端**现役** HQ 权重 `1002E-61pairs-3e-5`（960×544，61 对）
  A60   = **同配方 960 严格对照** `1003A-60pairs-3e-5`（960×544，60 对）
  F1080 = **新权重** `1003F-hq1080-3e-5`（1920×1080，60 对）

输入口径（与部署 §3.1 一致，逐条）：
  RGB（imread 后 BGR→RGB）· 整幅等比缩至 W×CONTENT_H · 上下各 PAD 行零 pad → W×H
  · 仅 /255（无 mean/std）· 推理 · 输出裁回 W×CONTENT_H
  ⇒ E/A60：1920×1080 → 960×540 + 2/2 pad = 960×544；F1080：1920×1080 + 0 pad = 1920×1080

**纪律**：
  · 输出 PNG **原样归档**，不做任何非线性后处理（规则 `10-evidence.md` §2）；
    一切非线性后处理副本（γ / 对比度 / 直方图均衡）**不生成**。
  · 出对比图时**只做声明过的线性重采样**：`×2 线性上采样`（= 部署把 960×540 拉到
    1920×1080 画布的实际做法）或 `INTER_AREA 降采样`。二者均在报告里显式登记。
  · 指标函数**直接 import 交付侧实现**（`tools/realtime_frame_review.py`），保证口径逐字一致。

**效力**：非验收依据（规则 `10-evidence.md` §1 / `00-ssot.md` §3.5）；纯离线，不碰板端。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time

import cv2
import numpy as np
import torch

REPO = "/home/jenkolux/PKU_WH/Img_enhance_demo"
ARCH = os.path.join(REPO, "refs/pre-research/basicsr/models/archs/RetinexFormer_arch.py")
UNET_ARCH = os.path.join(REPO, "hq_optimize/code/unet_arch.py")
sys.path.insert(0, os.path.join(REPO, "tools"))
from realtime_frame_review import (  # noqa: E402  口径与交付侧逐字一致
    dark_block_stats,
    gr_balance,
    max_rgb,
    mean_rgb,
)

ARMS = {
    "E": dict(
        pth="/home/jenkolux/datasets/imx415_u4b_0p55/runs/1002E-61pairs-3e-5_final.pth",
        W=960, CH=540, PAD=2, arch="retinexformer",
        note="板端现役 HQ（1002E-61pairs-3e-5，61 对，960×544）"),
    "UNet": dict(
        pth=os.path.expanduser("~/datasets/imx415_unet960_runs/1003H-unet960-lr1e-4_final.pth"),
        W=960, CH=540, PAD=2, arch="unet",
        note="轻量 UNet 对照臂（1003H-unet960-lr1e-4，60 对，960×544，随机初始化，ep94 早停）"),
    "A60": dict(
        pth="/home/jenkolux/datasets/imx415_hq60_runs/1003A-60pairs-3e-5/1003A-60pairs-3e-5_final.pth",
        W=960, CH=540, PAD=2, arch="retinexformer", stage=1,
        note="同配方 960 严格对照（1003A-60pairs-3e-5，60 对，960×544，stage 1）"),
    "RF_s2": dict(
        pth=os.path.expanduser("~/datasets/imx415_rf_s2_runs/1003H-rf40-s2-3e-5_final.pth"),
        W=960, CH=540, PAD=2, arch="retinexformer", stage=2,
        note="**Retinexformer 放大臂**（1003H-rf40-s2，stage 1→2，3.211M，n_feat 仍 40 ⇒ 预训练可装）"),
    "F1080": dict(
        pth="/home/jenkolux/datasets/imx415_hq1080_runs/1003F-hq1080-3e-5_final.pth",
        W=1920, CH=1080, PAD=0, arch="retinexformer",
        note="新权重（1003F-hq1080-3e-5，60 对，1920×1080）"),
}


def _load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


RetinexFormer = _load("RetinexFormer_arch", ARCH).RetinexFormer
UNet = _load("unet_arch", UNET_ARCH).UNet


def build(pth, device, arch="retinexformer", stage=1):
    model = UNet(in_ch=3, out_ch=3) if arch == "unet" else \
        RetinexFormer(in_channels=3, out_channels=3, n_feat=40, stage=stage, num_blocks=[1, 2, 2])
    sd = torch.load(pth, map_location="cpu")
    if isinstance(sd, dict):
        sd = sd.get("params", sd.get("state_dict", sd))
    sd = {k.replace("module.", ""): v for k, v in sd.items()}
    missing, unexpected = model.load_state_dict(sd, strict=False)
    assert not missing and not unexpected, (len(missing), len(unexpected))
    return model.to(device).eval()


def resize_rgb(rgb, W, CH):
    """整幅等比缩至 W×CH（= 部署 RGA 那一步的离线等价；INTER_AREA）。

    **拆出本函数的原因（2026-10-16）**：部署的 MFA 是「**先缩放、后平均**」
    （`main.py` L214-215），故离线批处理若要复刻同一口径，必须把平均插在
    **缩放之后、送网络之前** ⇒ 需要单独可调用的缩放步。
    """
    return cv2.resize(rgb, (W, CH), interpolation=cv2.INTER_AREA)


@torch.inference_mode()
def forward(model, lo, W, CH, PAD, device):
    """lo: (CH,W,3) uint8（**已缩放到 HQ 目标几何**的 RGB）→ 输出 (CH,W,3) uint8。

    即 `infer` 中「缩放之后」的那一半（/255 → 上下 PAD → 推理 → 裁回）。
    """
    a = lo.astype(np.float32)[None] / 255.
    if PAD:
        z = np.zeros((1, PAD, W, 3), np.float32)
        a = np.concatenate([z, a, z], axis=1)
    x = torch.from_numpy(a.transpose(0, 3, 1, 2)).to(device)
    y = model(x)[0, :, PAD:PAD + CH]
    return np.clip(y.permute(1, 2, 0).float().cpu().numpy() * 255., 0, 255).round().astype(np.uint8)


@torch.inference_mode()
def infer(model, rgb, W, CH, PAD, device):
    """rgb: HxWx3 uint8 (RGB) → 输出 (CH,W,3) uint8；口径见模块 docstring。

    = `forward(model, resize_rgb(rgb, W, CH), …)`（**拆步后行为逐位不变**）。
    """
    return forward(model, resize_rgb(rgb, W, CH), W, CH, PAD, device)


def to_buf(rgb):
    return rgb.tobytes()


def load_nv12_bt601_full(path, w, h, stride):
    """NV12（stride 可 > w）→ RGB uint8，**BT.601 full range**。

    口径出处：`config.yaml:178-183` 定稿 —— `csc_matrix: bt601` + `rgb_range: full`，
    与 `native/rga_core.c` 的 `IM_YUV_TO_RGB_BT601_FULL` 一致 ⇒ 与交付链路同口径。
    系数（整数化前的浮点 BT.601 full range）：
        R = Y + 1.402 (V−128)
        G = Y − 0.344136 (U−128) − 0.714136 (V−128)
        B = Y + 1.772 (U−128)
    """
    raw = np.fromfile(path, dtype=np.uint8)
    need = stride * h + stride * (h // 2)
    assert raw.size >= need, f"NV12 文件不足：{raw.size} < {need}（sizeimage 见 base_report.json）"
    y = raw[:stride * h].reshape(h, stride)[:, :w].astype(np.float32)
    uv = raw[stride * h:stride * h + stride * (h // 2)].reshape(h // 2, stride)[:, :w]
    u = uv[:, 0::2].astype(np.float32).repeat(2, axis=0).repeat(2, axis=1)
    v = uv[:, 1::2].astype(np.float32).repeat(2, axis=0).repeat(2, axis=1)
    u -= 128.0
    v -= 128.0
    r = y + 1.402 * v
    g = y - 0.344136 * u - 0.714136 * v
    b = y + 1.772 * u
    return np.clip(np.stack([r, g, b], -1), 0, 255).round().astype(np.uint8)


def metrics(rgb, w, h, gt_rgb=None):
    buf = to_buf(rgb)
    dk = dark_block_stats(buf, w, h)
    m = {
        "size": f"{w}x{h}",
        "mean_rgb": mean_rgb(buf),
        "max_rgb": max_rgb(buf),
        "dark_block_sigma_median": dk["sigma_median"],
        "dark_block_mean": dk["r_mean"],
        "gr_balance": gr_balance(buf, w, h, dk),
    }
    if gt_rgb is not None:
        d = rgb.astype(np.float64) - gt_rgb.astype(np.float64)
        mse = float(np.mean(d * d))
        m["psnr_vs_gt"] = round(100.0 if mse == 0 else 10.0 * np.log10(255.0 ** 2 / mse), 4)
    return m


def montage(imgs, col_w=480, col_h=270):
    """等权横向拼图；每列按 INTER_AREA 缩到 col_w×col_h。**仅供目视，不入量化**。"""
    tiles = []
    for im in imgs:
        tiles.append(cv2.resize(np.ascontiguousarray(im[..., ::-1]), (col_w, col_h),
                                interpolation=cv2.INTER_AREA))
    return np.hstack(tiles)


def save_png(path, rgb):
    cv2.imwrite(path, np.ascontiguousarray(rgb[..., ::-1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default=os.path.expanduser("~/datasets/imx415_public17/pairs.json"))
    ap.add_argument("--nv12", default=None, help="单帧 NV12 输入（替代 --pairs）；现场帧用")
    ap.add_argument("--nv12-w", type=int, default=1920)
    ap.add_argument("--nv12-h", type=int, default=1080)
    ap.add_argument("--nv12-stride", type=int, default=1920)
    ap.add_argument("--frame-id", default="field-0.35lux")
    ap.add_argument("--col-w", type=int, default=480, help="拼图每列宽（高 = 宽×9/16）")
    ap.add_argument("--out-dir", default=os.path.join(REPO, "hq_optimize/captures/ab_1008"))
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 对（0=全部）")
    ap.add_argument("--montage", default="0,8,16", help="出对比拼图的 pair 序号（逗号分隔）")
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    device = args.device if torch.cuda.is_available() else "cpu"
    os.makedirs(args.out_dir, exist_ok=True)
    mont_idx = {int(x) for x in args.montage.split(",") if x.strip() != ""}

    if args.nv12:                                      # 单帧现场 NV12：无 GT
        items = [(args.frame_id,
                  load_nv12_bt601_full(args.nv12, args.nv12_w, args.nv12_h, args.nv12_stride),
                  None)]
        mont_idx = {0}
    else:
        pairs = json.load(open(args.pairs))["pairs"]
        if args.limit:
            pairs = pairs[:args.limit]
        items = [(p.get("frame_id"), cv2.imread(p["dark"])[..., ::-1].copy(),
                  cv2.imread(p["gt"])[..., ::-1].copy()) for p in pairs]

    print(f"items={len(items)} device={device} out={args.out_dir}", flush=True)
    models = {}
    for name, a in ARMS.items():
        models[name] = build(a["pth"], device, a.get("arch", "retinexformer"), a.get("stage", 1))
        print(f"  arm {name}: {a['note']}", flush=True)

    rep = {"source": (args.nv12 or args.pairs), "n": len(items), "device": device,
           "nv12_csc": ("bt601 full range（config.yaml:178-183 定稿）" if args.nv12 else None),
           "arms": {k: {kk: vv for kk, vv in v.items()} for k, v in ARMS.items()},
           "policy": "输出原样归档；对比图仅线性重采样（×2 线性 / INTER_AREA）",
           "grids": {"A_1080": "部署口径 1920×1080：960 臂用 ×2 线性上采样（= 部署上屏做法）",
                     "B_960": "960×540 口径：1920 臂用 INTER_AREA 降采样"},
           "eff": "非验收依据", "rows": []}

    t0 = time.time()
    for i, (fid, dark, gt) in enumerate(items):
        h, w = dark.shape[:2]
        outs = {}
        for name, a in ARMS.items():
            outs[name] = infer(models[name], dark, a["W"], a["CH"], a["PAD"], device)
            save_png(os.path.join(args.out_dir, f"p{i:02d}_{name}_{a['W']}x{a['CH']}.png"), outs[name])

        # 网格 A：1920×1080（960 臂 ×2 线性上采样）；网格 B：960×540（1920 臂 INTER_AREA 降采样）
        gA = {"gt": gt}
        gB = {"gt": (cv2.resize(gt, (960, 540), interpolation=cv2.INTER_AREA) if gt is not None else None)}
        for name, a in ARMS.items():
            o = outs[name]
            gA[name] = cv2.resize(o, (w, h), interpolation=cv2.INTER_LINEAR) if o.shape[1] != w else o
            gB[name] = o if (o.shape[1], o.shape[0]) == (960, 540) else \
                cv2.resize(o, (960, 540), interpolation=cv2.INTER_AREA)

        row = {"i": i, "frame_id": fid,
               "dark": metrics(dark, w, h),
               "native": {n: metrics(outs[n], ARMS[n]["W"], ARMS[n]["CH"]) for n in ARMS},
               "gridA_1920x1080": {n: metrics(gA[n], w, h, gA["gt"]) for n in ARMS},
               "gridB_960x540": {n: metrics(gB[n], 960, 540, gB["gt"]) for n in ARMS},
               "gt": (metrics(gt, w, h) if gt is not None else None)}
        rep["rows"].append(row)

        if i in mont_idx:
            cw = args.col_w
            mg = montage([dark] + [gA[n] for n in ARMS], cw, cw * 9 // 16)
            mp = os.path.join(args.out_dir, f"montage_p{i:02d}.png")
            save_png(mp, mg)
            row["montage"] = os.path.basename(mp)
            print(f"  [montage] {mp}", flush=True)
        print(f"  p{i:02d} {fid} done ({time.time()-t0:.0f}s)", flush=True)

    # 汇总（各臂均值）
    def agg(grid, key):
        vals = [r[grid][n][key] for r in rep["rows"] for n in ARMS if key in r[grid][n]]
        return (round(float(np.mean(vals)), 4) if vals else None)

    rep["summary"] = {}
    for grid in ("gridA_1920x1080", "gridB_960x540"):
        rep["summary"][grid] = {
            "psnr_vs_gt": ({n: round(float(np.mean([r[grid][n]["psnr_vs_gt"] for r in rep["rows"]])), 4) for n in ARMS}
                           if any("psnr_vs_gt" in r[grid][na] for r in rep["rows"] for na in ARMS) else None),
            "mean_rgb": {n: [round(float(np.mean([r[grid][n]["mean_rgb"][c] for r in rep["rows"]])), 2) for c in range(3)] for n in ARMS},
            "max_rgb": {n: [int(max(r[grid][n]["max_rgb"][c] for r in rep["rows"])) for c in range(3)] for n in ARMS},
            "dark_block_sigma_median": {n: round(float(np.mean([r[grid][n]["dark_block_sigma_median"] for r in rep["rows"]])), 4) for n in ARMS},
            "dark_block_mean": {n: round(float(np.mean([r[grid][n]["dark_block_mean"] for r in rep["rows"]])), 3) for n in ARMS},
            "gr_balance": {n: {k: round(float(np.mean([r[grid][n]["gr_balance"][k] for r in rep["rows"]])), 4)
                               for k in ("whole_g_over_r", "dark_g_over_r")} for n in ARMS},
        }
    rep["dark_native_summary"] = {
        "mean_rgb": {n: [round(float(np.mean([r["native"][n]["mean_rgb"][c] for r in rep["rows"]])), 2) for c in range(3)] for n in ARMS},
        "dark_block_sigma_median": {n: round(float(np.mean([r["native"][n]["dark_block_sigma_median"] for r in rep["rows"]])), 4) for n in ARMS},
    }
    rep["wall_sec"] = round(time.time() - t0, 1)

    with open(os.path.join(args.out_dir, "ab_report.json"), "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)

    print(f"\n=== 汇总（{len(rep['rows'])} 帧均值；与 GT 的对比一律在同一网格上算） ===")
    print("原图(暗):", json.dumps(rep["rows"][0]["dark"]["mean_rgb"]) if rep["rows"] else None,
          "| 暗块σ", rep["rows"][0]["dark"]["dark_block_sigma_median"] if rep["rows"] else None,
          "| 暗部G/R", rep["rows"][0]["dark"]["gr_balance"]["dark_g_over_r"] if rep["rows"] else None)
    for grid in ("gridA_1920x1080", "gridB_960x540"):
        s = rep["summary"][grid]
        print(f"[{grid}] mean_rgb " + "  ".join(f"{n}={s['mean_rgb'][n]}" for n in ARMS))
        print(f"            暗块σ  " + "  ".join(f"{n}={s['dark_block_sigma_median'][n]:.3f}" for n in ARMS)
              + " | 暗部G/R " + "  ".join(f"{n}={s['gr_balance'][n]['dark_g_over_r']:.4f}" for n in ARMS))
        if s["psnr_vs_gt"]:
            print(f"            PSNR(vs GT) " + "  ".join(f"{n}={s['psnr_vs_gt'][n]:.3f}" for n in ARMS))
    print("原生尺寸:", json.dumps(rep["dark_native_summary"], ensure_ascii=False))
    print(f"done {rep['wall_sec']}s → {args.out_dir}/ab_report.json")


if __name__ == "__main__":
    main()