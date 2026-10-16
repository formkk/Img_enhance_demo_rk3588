#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板上批量伪标签：对 det_pseudo/hq_in/*.png 逐张跑 dcm_demo.py 检测，收集 report.json 的框。

**用途** = 为 DCM 检测训练准备「伪标签」（labelme 格式），供人工修正。
**口径**：
  · 每张独立 outdir（det_pseudo/out/<stem>/），产物 report.json 保留原始证据；
  · score 阈 0.30 = 演示链现役值（dcm_demo.py 默认），不另设；
  · q31 = 演示链现役档；
  · 串行执行（NPU 共享设备，规则 30 §1：RGA/NPU 为共享设备，不得并发抢占）；
  · 单张失败不中断（记 failed 列表），便于断点续跑（--skip-existing）。

**纪律**：本脚本只产伪标签，**不作为验收依据**（规则 10-evidence.md §1）；
  伪标签最终由人工修正后才入训练。

用法（板上）：
  python3 pseudo_batch.py [--in-dir ...] [--out-root ...] [--score 0.30] [--limit N]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

DEMO = "/home/cat/dcm_x101/demo/dcm_demo.py"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-dir", default="/home/cat/dcm_x101/det_pseudo/hq_in")
    ap.add_argument("--out-root", default="/home/cat/dcm_x101/det_pseudo/out")
    ap.add_argument("--score", type=float, default=0.30)
    ap.add_argument("--q", type=int, default=31)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-existing", action="store_true", default=True)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.in_dir, "*.png")))
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"!! {args.in_dir} 无 png", file=sys.stderr)
        return 2
    os.makedirs(args.out_root, exist_ok=True)

    ok, fail, skip = [], [], []
    t0 = time.time()
    for i, f in enumerate(files):
        stem = os.path.splitext(os.path.basename(f))[0]
        od = os.path.join(args.out_root, stem)
        rep = os.path.join(od, "report.json")
        if args.skip_existing and os.path.isfile(rep):
            skip.append(stem)
            continue
        os.makedirs(od, exist_ok=True)
        cmd = ["/usr/bin/python3", DEMO, "--input", f, "--outdir", od,
               "--q", str(args.q), "--score", str(args.score),
               "--timeout", str(args.timeout)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and os.path.isfile(rep):
            ok.append(stem)
        else:
            fail.append({"stem": stem, "rc": r.returncode,
                         "err": (r.stderr or r.stdout)[-300:]})
        el = time.time() - t0
        n = len(ok) + len(fail)
        print(f"[{i+1}/{len(files)}] {stem} rc={r.returncode} "
              f"ok={len(ok)} fail={len(fail)} ({el:.0f}s)", flush=True)

    with open(os.path.join(args.out_root, "pseudo_summary.json"), "w") as fh:
        json.dump({"n_in": len(files), "ok": len(ok), "fail": len(fail),
                   "skip": len(skip), "score": args.score, "q": args.q,
                   "wall_sec": round(time.time() - t0, 1),
                   "ok_list": ok, "fail_list": fail, "skip_list": skip},
                  fh, ensure_ascii=False, indent=1)
    print(f"\ndone ok={len(ok)} fail={len(fail)} skip={len(skip)} "
          f"({time.time()-t0:.0f}s) → {args.out_root}", flush=True)
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
