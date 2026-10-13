#!/usr/bin/env python3
"""B4b（147 上跑）：对指定 ckpt 目录里的权重复核 val200（学生框 vs 教师框）。"""

import os
import sys

import torch

sys.path.insert(0, "/home/jiangfeng/jf_app/dcm_b")
import b2_train as B                                                    # noqa: E402


def main() -> int:
    d = sys.argv[1] if len(sys.argv) > 1 else "/home/jiangfeng/jf_app/dcm_b/ckpt3"
    dev = torch.device("cuda")
    va = B.Shard("/home/jiangfeng/jf_app/dcm_b/pre_val")
    for name in ("best.pth", "final.pth"):
        p = os.path.join(d, name)
        if not os.path.exists(p):
            continue
        m = B.FeatureDet().to(dev)
        m.load_state_dict(torch.load(p, map_location=dev)["model"])
        g, res, ex = B.evaluate(m, va, dev, 200)
        parts = " ".join(f"c{c}:R={res[c][2]:.3f}" for c in sorted(res))
        print(f"{name} val200: teacher={g} | {parts} | "
              f"biou={ex['biou']:.3f} sw={ex['sw']:.0f} (teacher {ex['tw']:.0f})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
