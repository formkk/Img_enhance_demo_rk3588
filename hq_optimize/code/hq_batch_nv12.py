#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量：目录下无损裸 NV12 -> HQ E 臂（板端现役 1002E）离线推理 -> PNG 原样归档。

**用途** = 为 DCM 检测训练准备「部署域 HQ 输出图」（输入侧口径与部署一致）。

**口径（逐字复用 `hq_ab_infer.py`，不重新实现）**：
  · NV12 -> RGB = `load_nv12_bt601_full`（BT.601 full range，config.yaml:178-183 定稿）
  · 缩放 = `resize_rgb`（整幅等比缩至 W×CONTENT_H，INTER_AREA）
  · 前向 = `forward`（/255 -> 上下各 PAD 行零 pad -> 推理 -> 裁回 W×CONTENT_H）
  · E 臂：960×544（CONTENT 540 + PAD 2/2）=> 输出裁回 960×540
  · 模型只加载一次（脚本级循环），不逐帧重建

**MFA 多帧平均（2026-10-16，用户裁定「方案 A」）**：
采集侧（`tools/collect/capture_app.py`）每次采集连拍 **8 帧**（对齐部署 `--hq-mfa-frames` 现役默认 8）。
本脚本按**部署的同一顺序**复刻平均：**逐帧缩放 -> uint16 无饱和累加 -> 取均（四舍五入）-> 送网络**。
  · 口径出处 = `native/mfa_core.{h,c}`：`acc` 为 16bit 无饱和累加，取均 = `(acc + nframes/2) / nframes`（整数除法）。
  · 部署累加的是**含 PAD 行**的缓冲；PAD 行恒 0 =>「先 PAD 后平均」≡「先平均后 PAD」
    => 此处缩到 (W, CONTENT_H) 后平均即可（等价，且省内存）。
  · 组 = 采集端的**一次采集**（同 `<stem>`，成员为 `<stem>_n<NN>.nv12`；成员序优先取 sidecar 的 `burst.names`）。
    无 `_nNN` 的文件**自成一组**（**兼容旧单帧数据** => 逐帧独立推理，行为与改前逐位一致）。
  · **原始 8 帧保留不删** => 可复算 / 可改 N（用户裁定：省那几 GB 不值得丢掉可复算性）。
  · 几何优先取该组 sidecar 的 `capture_condition`（`stride` / `resolution`）=> 免手填 `--nv12-h` 填错导致静默错位。

**纪律**：
  · 输出 PNG **原样归档**，不做任何非线性后处理（规则 `10-evidence.md` §2）；
    一切非线性后处理副本（γ / 对比度 / 直方图均衡）**不生成**。
  · 效力 = 非验收依据（规则 `10-evidence.md` §1）。

用法：
  python3 hq_batch_nv12.py --src-dir hq_optimize/captures/seqmv \
      --out-dir hq_optimize/captures/hq_e_seqmv [--arm E] [--nv12-h 1080] [--limit N]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))          # hq_optimize/code/ -> 仓库根
sys.path.insert(0, HERE)


def _load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ab = _load("hq_ab_infer", os.path.join(HERE, "hq_ab_infer.py"))

_NN = re.compile(r"_n(\d{2})$")


def _list_groups(src_dir):
    # 把 *.nv12 归并为「采集组」：
    #   · `<stem>_n<NN>.nv12` -> 组键 `<stem>`（一次采集 = 一组）；成员序优先取 sidecar 的
    #     `burst.names`（采集端权威记录），否则按文件名序。
    #   · 无 `_nNN` 的 -> 自成一组（兼容旧单帧数据 => 逐帧独立推理，行为与改前一致）。
    #   返回 [(组键, [成员文件名, ...]), ...]（组键按文件名序）。
    bucket = {}
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".nv12"):
            continue
        stem = os.path.splitext(fn)[0]
        m = _NN.search(stem)
        bucket.setdefault(stem[:m.start()] if m else stem, []).append(fn)
    out = []
    for key in sorted(bucket):
        members = bucket[key]
        side = os.path.join(src_dir, key + ".json")
        names = None
        if os.path.exists(side):
            try:
                names = ((json.load(open(side)) or {}).get("burst") or {}).get("names")
            except Exception:
                names = None
        if names:
            have = set(members)
            members = [n for n in names if n in have]
        out.append((key, members))
    return out


def _group_geometry(src_dir, key, args):
    # 几何：优先取该组 sidecar 的 `capture_condition`（采集端实测），否则回落 CLI 参数。
    # 动机 = 免手填 --nv12-h 填错（本批采集实为 1920x1080，CLI 历史默认是 1088）导致静默错位。
    # 返回 (w, h, stride, 来源)。
    side = os.path.join(src_dir, key + ".json")
    if os.path.exists(side):
        try:
            cc = (json.load(open(side)) or {}).get("capture_condition") or {}
            res = str(cc.get("resolution") or "").lower()
            st = cc.get("stride")
            if "x" in res and st:
                w, h = (int(x) for x in res.split("x")[:2])
                return w, h, int(st), "sidecar.capture_condition"
        except Exception:
            pass
    return args.nv12_w, args.nv12_h, args.nv12_stride, "cli"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-dir", required=True, help="含 *.nv12 的目录")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--arm", default="E", choices=list(ab.ARMS),
                    help="HQ 权重臂（默认 E = 板端现役 1002E-61pairs-3e-5）")
    ap.add_argument("--nv12-w", type=int, default=1920)
    ap.add_argument("--nv12-h", type=int, default=1088,
                    help="**仅在该组无 sidecar 时生效**（有 sidecar 则取其 capture_condition）；"
                         "历史默认 1088 系 seqmv 实测（stride=1920/h=1088），"
                         "本批采集为 1080（stride=1920）= 3,110,400 B")
    ap.add_argument("--nv12-stride", type=int, default=1920)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 组（0=全部）")
    args = ap.parse_args()

    a = ab.ARMS[args.arm]
    device = args.device if __import__("torch").cuda.is_available() else "cpu"
    os.makedirs(args.out_dir, exist_ok=True)

    groups = _list_groups(args.src_dir)
    if args.limit:
        groups = groups[:args.limit]
    if not groups:
        print(f"!! {args.src_dir} 下没有 .nv12", file=sys.stderr)
        return 2
    n_frame = sum(len(m) for _, m in groups)
    n_multi = sum(1 for _, m in groups if len(m) > 1)
    print(f"组 = {len(groups)}（其中连拍组 {n_multi}）；共 {n_frame} 个 nv12", flush=True)

    print(f"arm={args.arm} ({a['note']})  W={a['W']} CH={a['CH']} PAD={a['PAD']}  "
          f"device={device}  nv12 CLI 回落 = {args.nv12_w}x{args.nv12_h} stride={args.nv12_stride}",
          flush=True)
    model = ab.build(a["pth"], device, a.get("arch", "retinexformer"), a.get("stage", 1))
    print(f"out={args.out_dir}", flush=True)

    t0 = time.time()
    rows = []
    for i, (key, members) in enumerate(groups):
        w, h, stride, geo_src = _group_geometry(args.src_dir, key, args)
        need = stride * h * 3 // 2
        sz0 = os.path.getsize(os.path.join(args.src_dir, members[0]))
        if sz0 < need:
            print(f"!! {members[0]} = {sz0} B < 需要 {need} B"
                  f"（stride={stride} h={h}，几何来源={geo_src}）——请核对 sidecar / --nv12-h",
                  file=sys.stderr)
            return 2

        # **部署顺序**：逐帧「缩放」到 HQ 目标几何 -> uint16 无饱和累加 -> 取均（四舍五入）
        los = [ab.resize_rgb(ab.load_nv12_bt601_full(os.path.join(args.src_dir, fn), w, h, stride),
                             a["W"], a["CH"]) for fn in members]
        if len(los) == 1:
            avg, mfa_n = los[0], 1
        else:
            acc = np.zeros_like(los[0], dtype=np.uint16)
            for lo in los:
                acc += lo.astype(np.uint16)                      # 无饱和（N <= 8 => <= 2040）
            mfa_n = len(los)
            avg = ((acc.astype(np.uint32) + (mfa_n // 2)) // mfa_n).astype(np.uint8)

        out = ab.forward(model, avg, a["W"], a["CH"], a["PAD"], device)
        dst = os.path.join(args.out_dir, f"{key}_hqE_{a['W']}x{a['CH']}.png")
        ab.save_png(dst, out)
        rows.append({"i": i, "src": key, "members": members, "mfa_n": mfa_n,
                     "geom": {"w": w, "h": h, "stride": stride, "src": geo_src},
                     "dst": os.path.basename(dst), "bytes": os.path.getsize(dst)})
        if i % 25 == 0 or i == len(groups) - 1:
            el = time.time() - t0
            print(f"  [{i+1}/{len(groups)}] {key} (mfa_n={mfa_n}) -> "
                  f"{os.path.basename(dst)}  ({el:.0f}s, {el/(i+1):.2f}s/组)", flush=True)

    rep = {"src_dir": os.path.abspath(args.src_dir), "out_dir": os.path.abspath(args.out_dir),
           "arm": args.arm, "arm_spec": a, "device": device,
           "nv12_cli_fallback": {"w": args.nv12_w, "h": args.nv12_h, "stride": args.nv12_stride},
           "n_groups": len(rows), "n_frames_in": n_frame, "wall_sec": round(time.time() - t0, 1),
           "mfa": {"order": "逐帧缩放 -> uint16 无饱和累加 -> 取均(四舍五入) -> 送网络",
                   "rounding": "dst = (acc + nframes/2) / nframes（整数除法）",
                   "cite": "native/mfa_core.{h,c}（与部署同一内核）",
                   "pad_note": "部署累加含 PAD 行的缓冲；PAD 恒 0 => 先 PAD 后平均 ≡ 先平均后 PAD",
                   "n_per_group": {r["src"]: r["mfa_n"] for r in rows}},
           "policy": "输出原样归档，无任何非线性后处理（规则 10-evidence.md §2）；"
                     "连拍组按部署口径先缩放后平均；原始 8 帧保留不删",
           "eff": "非验收依据（规则 10-evidence.md §1）",
           "rows": rows}
    with open(os.path.join(args.out_dir, "hq_batch_report.json"), "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f"done {rep['wall_sec']}s -> {args.out_dir}  "
          f"(组={rep['n_groups']} / 入帧={rep['n_frames_in']})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
