#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量：目录下无损裸 NV12 → HQ E 臂（板端现役 1002E）离线推理 → PNG 原样归档。

**用途** = 为 DCM 检测训练准备「部署域 HQ 输出图」（输入侧口径与部署一致）。

**口径（逐字复用 `hq_ab_infer.py`，不重新实现）**：
  · NV12 → RGB = `load_nv12_bt601_full`（BT.601 full range，config.yaml:178-183 定稿）
  · 输入预处理 + 推理 = `infer`（整幅等比缩至 W×CONTENT_H + 上下各 PAD 行零 pad → W×H）
  · E 臂：960×544（CONTENT 540 + PAD 2/2）⇒ 输出裁回 960×540
  · 模型只加载一次（脚本级循环），不逐帧重建

**纪律**：
  · 输出 PNG **原样归档**，不做任何非线性后处理（规则 `10-evidence.md` §2）；
    一切非线性后处理副本（γ / 对比度 / 直方图均衡）**不生成**。
  · 单帧口径（**已由用户 2026-10-16 确认**）：采集侧 `capture_app.py` 为单帧落盘
    （raw_reader 只保留最新一帧、do_capture 写一帧），本脚本逐帧独立推理。
    ⚠️ 与部署 HQ 路输入侧的「N=6 帧平均」存在**已登记域差**（hq_optimize/README.md §8），
    本脚本不改该口径（用户裁定先出图目视，域差单独登记）。
  · 效力 = 非验收依据（规则 `10-evidence.md` §1）。

用法：
  python3 hq_batch_nv12.py --src-dir hq_optimize/captures/seqmv \
      --out-dir hq_optimize/captures/hq_e_seqmv [--arm E] [--nv12-h 1088] [--limit N]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))          # hq_optimize/code/ → 仓库根
sys.path.insert(0, HERE)


def _load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ab = _load("hq_ab_infer", os.path.join(HERE, "hq_ab_infer.py"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-dir", required=True, help="含 *.nv12 的目录")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--arm", default="E", choices=list(ab.ARMS),
                    help="HQ 权重臂（默认 E = 板端现役 1002E-61pairs-3e-5）")
    ap.add_argument("--nv12-w", type=int, default=1920)
    ap.add_argument("--nv12-h", type=int, default=1088,
                    help="seqmv 实测 stride=1920/h=1088（1080 的 32 对齐补齐）；"
                         "inter_area 缩到 960×540 时多出的 8 行 padding 被缩掉")
    ap.add_argument("--nv12-stride", type=int, default=1920)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 帧（0=全部）")
    args = ap.parse_args()

    a = ab.ARMS[args.arm]
    device = args.device if __import__("torch").cuda.is_available() else "cpu"
    os.makedirs(args.out_dir, exist_ok=True)

    files = sorted(f for f in os.listdir(args.src_dir) if f.endswith(".nv12"))
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"!! {args.src_dir} 下没有 .nv12", file=sys.stderr)
        return 2

    # 尺寸自检：文件字节数须 ≥ stride*h*1.5（否则 load_nv12 的 assert 会失败）
    need = args.nv12_stride * args.nv12_h * 3 // 2
    sz0 = os.path.getsize(os.path.join(args.src_dir, files[0]))
    if sz0 < need:
        print(f"!! 首帧 {files[0]} = {sz0} B < 需要 {need} B"
              f"（stride={args.nv12_stride} h={args.nv12_h}）——请核对 --nv12-h",
              file=sys.stderr)
        return 2

    print(f"arm={args.arm} ({a['note']})  W={a['W']} CH={a['CH']} PAD={a['PAD']}  "
          f"device={device}  nv12={args.nv12_w}x{args.nv12_h} stride={args.nv12_stride}",
          flush=True)
    model = ab.build(a["pth"], device, a.get("arch", "retinexformer"), a.get("stage", 1))
    print(f"frames={len(files)}  out={args.out_dir}", flush=True)

    t0 = time.time()
    rows = []
    for i, fn in enumerate(files):
        src = os.path.join(args.src_dir, fn)
        rgb = ab.load_nv12_bt601_full(src, args.nv12_w, args.nv12_h, args.nv12_stride)
        out = ab.infer(model, rgb, a["W"], a["CH"], a["PAD"], device)
        stem = os.path.splitext(fn)[0]
        dst = os.path.join(args.out_dir, f"{stem}_hqE_{a['W']}x{a['CH']}.png")
        ab.save_png(dst, out)
        rows.append({"i": i, "src": fn, "dst": os.path.basename(dst),
                     "bytes": os.path.getsize(dst)})
        if i % 25 == 0 or i == len(files) - 1:
            el = time.time() - t0
            print(f"  [{i+1}/{len(files)}] {fn} -> {os.path.basename(dst)}  "
                  f"({el:.0f}s, {el/(i+1):.2f}s/帧)", flush=True)

    rep = {"src_dir": os.path.abspath(args.src_dir), "out_dir": os.path.abspath(args.out_dir),
           "arm": args.arm, "arm_spec": a, "device": device,
           "nv12": {"w": args.nv12_w, "h": args.nv12_h, "stride": args.nv12_stride},
           "n": len(rows), "wall_sec": round(time.time() - t0, 1),
           "policy": "输出原样归档，无任何非线性后处理；单帧口径（与部署 N=6 平均的域差已登记）",
           "eff": "非验收依据（规则 10-evidence.md §1）",
           "rows": rows}
    with open(os.path.join(args.out_dir, "hq_batch_report.json"), "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f"done {rep['wall_sec']}s → {args.out_dir}  (n={len(rows)})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
