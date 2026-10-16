#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同场景去重：结构签名（下采样灰度 + 相关）+ 亮度分层（均值分箱）。

**去重判据（三条同时满足才判「重复」）**：
  1. 同一 scene（同前缀，如 raw_seqmv）——跨场景永不判重；
  2. 亮度差小：|mean_Y(a) − mean_Y(b)| ≤ LUX_TOL（同场景不同照度**不**判重，保留）；
  3. 结构相似：8×8 下采样灰度图的归一化互相关 ≥ CORR_TOL
     （内容几乎不动；低分辨压掉噪声，使判据对曝光差稳健）。

**保留策略**：贪心扫描（按亮度降序），每帧与**已保留**集合比对，重复则丢弃。
  好处：保留的一帧一定与所有其他保留帧都不同，且天然覆盖从亮到暗的照度谱。

**用途** = 为 DCM 检测训练准备数据：同场景连续帧高度相关，且同一场景同一照度
  的多帧对训练无增益，只稀释有效样本。

**纪律**：本脚本**只判定、只移动**（被去重者移入 `_dup/` 子目录），不删除、
不改内容、不写后处理副本。输出清单 JSON 供人工复核。

用法：
  python3 dedup_same_scene.py --src <dir> [--pattern '*.jpg'] [--lux-tol 6]
      [--corr-tol 0.985] [--out-json <path>] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

import cv2
import numpy as np

# 8×8 下采样：把噪声与细纹理压掉，只留「内容布局」。经验上这个尺寸对
# 「同一场景同一曝光下的相邻帧」区分度足够，对「轻微手持抖动」又足够稳健。
SIG_HW = 8
LUX_TOL_DEF = 6      # mean_Y 差 ≤6 视为「同照度」；>6 判为不同照度，保留
CORR_TOL_DEF = 0.985


def _sig(img_bgr):
    """8×8 下采样灰度 → 零均值单位方差签名（用于互相关）。"""
    g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    s = cv2.resize(g, (SIG_HW, SIG_HW), interpolation=cv2.INTER_AREA).astype(np.float32)
    s = s - s.mean()
    n = float(np.sqrt((s * s).sum()))
    return s / n if n > 1e-6 else s


def _mean_y(img_bgr):
    g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    return float(g.mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="待去重目录")
    ap.add_argument("--pattern", default="*.jpg")
    ap.add_argument("--lux-tol", type=float, default=LUX_TOL_DEF)
    ap.add_argument("--corr-tol", type=float, default=CORR_TOL_DEF)
    ap.add_argument("--out-json", default="")
    ap.add_argument("--dry-run", action="store_true", help="只判定，不移动")
    ap.add_argument("--scene-prefix", default="",
                    help="显式指定 scene 名（如 hq_e_seqmv）；给定时所有文件视为同一 scene，"
                         "不再从文件名推断。用于文件名尾部不是 _fNNNN/_NNNN 的批次"
                         "（如 f145_hqE_960x540.png）")
    ap.add_argument("--scene-from-prefix", action="store_true",
                    help="scene 取文件名第一个 '_' 前的前缀（如 hq60_xxx → hq60）。"
                         "用于多个数据集混在同一目录、需按批次独立去重的场合")
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(args.src)
                   if f.endswith(args.pattern[1:]) and os.path.isfile(os.path.join(args.src, f)))
    if not files:
        print(f"!! {args.src} 无 {args.pattern}", file=sys.stderr)
        return 2

    recs = []
    for fn in files:
        img = cv2.imread(os.path.join(args.src, fn))
        if img is None:
            print(f"!! 读不到 {fn}，跳过", file=sys.stderr)
            continue
        recs.append({"file": fn, "mean_y": _mean_y(img), "sig": _sig(img)})

    # scene 优先级：--scene-prefix > --scene-from-prefix > 从文件名推断
    if args.scene_prefix:
        def scene_of(fn):
            return args.scene_prefix
    elif args.scene_from_prefix:
        def scene_of(fn):
            return fn.split("_", 1)[0]
    else:
        def scene_of(fn):
            # scene = 去掉尾部 _fNNNN / _NNNNN 后的前缀（与命名约定一致）。
            # ⚠️ 先查 "_f"+数字 再查 "_"+数字：若顺序反了，"_f0183" 会被剥成 "_f" 前缀误判。
            base = os.path.splitext(fn)[0]
            for suf in ("_f", "_"):
                i = base.rfind(suf)
                if i > 0 and base[i + 1 + len(suf):].isdigit():
                    return base[:i]
            return base

    # 按 scene 分组，组内按亮度降序贪心
    kept, dropped = [], []
    by_scene = {}
    for r in recs:
        by_scene.setdefault(scene_of(r["file"]), []).append(r)

    for scene in sorted(by_scene):
        grp = sorted(by_scene[scene], key=lambda r: -r["mean_y"])
        g_kept = []
        for r in grp:
            dup_of = None
            for k in g_kept:
                if abs(k["mean_y"] - r["mean_y"]) > args.lux_tol:
                    continue  # 不同照度 ⇒ 保留
                c = float((k["sig"] * r["sig"]).sum())
                if c >= args.corr_tol:
                    dup_of = k["file"]
                    break
            if dup_of is None:
                g_kept.append(r)
                kept.append(r)
            else:
                r["dup_of"] = dup_of
                dropped.append(r)
        print(f"[{scene}] 保留 {len(g_kept)} / 去重 {len(grp) - len(g_kept)}"
              f"（共 {len(grp)}）", flush=True)

    print(f"\n总计：保留 {len(kept)}，去重 {len(dropped)}"
          f"（lux_tol={args.lux_tol} corr_tol={args.corr_tol}）")

    if not args.dry_run and dropped:
        dup_dir = os.path.join(args.src, "_dup")
        os.makedirs(dup_dir, exist_ok=True)
        for r in dropped:
            shutil.move(os.path.join(args.src, r["file"]),
                        os.path.join(dup_dir, r["file"]))

    rep = {"src": os.path.abspath(args.src), "pattern": args.pattern,
           "lux_tol": args.lux_tol, "corr_tol": args.corr_tol,
           "n_in": len(recs), "n_kept": len(kept), "n_dup": len(dropped),
           "dry_run": args.dry_run,
           "by_scene": {s: {"in": len(by_scene[s]),
                            "kept": sum(1 for k in kept if scene_of(k["file"]) == s),
                            "dup": sum(1 for d in dropped if scene_of(d["file"]) == s)}
                        for s in sorted(by_scene)},
           "kept_files": [k["file"] for k in kept],
           "dup_pairs": [{"file": d["file"], "dup_of": d["dup_of"]} for d in dropped]}
    if args.out_json:
        with open(args.out_json, "w") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        print(f"清单 → {args.out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
