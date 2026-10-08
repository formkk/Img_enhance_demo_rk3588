#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HQ 1080p 输入对照实验 · 第一步：显存可行性探针

目的：在**开发机（RTX 3090 24GB）**上测 RetinexFormer 在若干输入尺寸下的
      **训练显存峰值与是否 OOM**，判断「1920×1080 输入」是否可行。
口径：与仓库外 `~/datasets/imx415_hq60_runs/train_hq60.py` 一致
      （RGB / 仅 /255 / L1(mean) / AMP / Adam / bs=1 / 起点 = LOL_v1.pth）。
性质：**纯离线探针**；不碰板端、不改任何既有脚本/数据/仓库跟踪文件。
效力：非验收依据（规则 10-evidence.md §1 / 00-ssot.md §3.5）。
"""
from __future__ import annotations

import importlib.util
import os
import sys

import torch
import torch.nn.functional as F

REPO = "/home/jenkolux/PKU_WH/Img_enhance_demo"
ARCH = os.path.join(REPO, "refs/pre-research/basicsr/models/archs/RetinexFormer_arch.py")
INIT = os.path.join(REPO, "refs/pre-research/pretrain_model/LOL_v1.pth")

# (W, H, 说明)：H 已含 pad（本任务口径 content 1080 + pad 0 ⇒ 1080；另测 1088 以看对齐影响）
CASES = [
    (960, 544, "现役主档 960x544（content 540 + 2/2 pad）"),
    (1280, 720, "中间档 1280x720"),
    (1920, 1080, "1080p 候选（content 1080 + pad 0）"),
    (1920, 1088, "1080p 候选 1088（32 对齐）"),
]


def _load(mod: str, path: str):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def probe(w: int, h: int, note: str):
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    try:
        RetinexFormer = _load("RetinexFormer_arch", ARCH).RetinexFormer
    except Exception as e:  # noqa: BLE001
        print(f"[{w}x{h}] 架构导入失败: {type(e).__name__}: {e}")
        return None
    try:
        # 构造参数照仓库外 train_hq60.py:68（build()），保证与既有训练一致
        model = RetinexFormer(in_channels=3, out_channels=3, n_feat=40, stage=1, num_blocks=[1, 2, 2]).cuda()
        sd = torch.load(INIT, map_location="cpu")
        if isinstance(sd, dict) and "params" in sd and isinstance(sd["params"], dict):
            sd = sd["params"]
        incompat = model.load_state_dict(sd, strict=False)
        print(f"[{w}x{h}] 权重加载: missing={len(incompat.missing_keys)} unexpected={len(incompat.unexpected_keys)}")
    except Exception as e:  # noqa: BLE001
        print(f"[{w}x{h}] 模型/权重加载失败: {type(e).__name__}: {e}")
        return None

    opt = torch.optim.Adam(model.parameters(), lr=3e-5)
    x = torch.rand(1, 3, h, w, device="cuda")
    t = torch.rand(1, 3, h, w, device="cuda")
    ok, err = True, ""
    t0 = None
    use_amp = os.environ.get("PROBE_AMP", "0") == "1"
    scaler = torch.cuda.amp.GradScaler(enabled=use_amp)
    try:
        model.train()
        opt.zero_grad(set_to_none=True)
        import time as _t

        t0 = _t.time()
        with torch.cuda.amp.autocast(enabled=use_amp):
            y = model(x)
            loss = F.l1_loss(y, t)
        if use_amp:
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
        else:
            loss.backward()
            opt.step()
        torch.cuda.synchronize()
        step_s = _t.time() - t0
    except RuntimeError as e:  # 多为 CUDA OOM
        ok, err, step_s = False, str(e).replace("\n", " ")[:150], float("nan")
    peak = torch.cuda.max_memory_allocated() / 2**30
    print(f"[{w}x{h}] {note}\n    amp={use_amp}  ok={ok}  峰值显存={peak:.2f} GiB  单步耗时={step_s:.2f}s  {err}")
    del model, opt, x, t
    torch.cuda.empty_cache()
    return ok, peak


if __name__ == "__main__":
    print("torch", torch.__version__, "| cuda", torch.version.cuda, "| dev", torch.cuda.get_device_name(0))
    print("arch  =", ARCH, "exists:", os.path.exists(ARCH))
    print("init  =", INIT, "exists:", os.path.exists(INIT))
    print("-" * 78)
    rows = []
    for w, h, note in CASES:
        r = probe(w, h, note)
        rows.append((w, h, note, r))
    print("-" * 78)
    print("汇总：")
    for w, h, note, r in rows:
        if r is None:
            print(f"  {w}x{h}: 未测成（前置失败）")
        else:
            ok, peak = r
            print(f"  {w}x{h}: {'可行' if ok else 'OOM ✗'}  峰值 {peak:.2f} GiB")
    sys.exit(0)