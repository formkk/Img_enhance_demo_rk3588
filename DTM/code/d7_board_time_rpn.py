"""d7：板上测 RPN 头（方案 B 混合架构第二步：确认可加载、可跑、输出 shape 对）。

环境：板上 python3.9 + /home/cat/rknn_ab/site/232/（rknnlite 2.3.2）
输入：5 级 FPN 特征（随机 fp32），静态 shape 与导出一致。
本脚本只验「可加载 + 可跑 + shape 对 + 耗时」，**不含精度结论**。

用法（板上）：
  PYTHONPATH=/home/cat/rknn_ab/site/232 python3 d7_board_time_rpn.py <rknn路径> [重复次数]
"""

import sys
import time

import numpy as np
from rknnlite.api import RKNNLite

# 与导出/转换一致的 5 级静态 shape
SHAPES = [
    (1, 256, 192, 336),   # p2
    (1, 256, 96, 168),    # p3
    (1, 256, 48, 84),     # p4
    (1, 256, 24, 42),     # p5
    (1, 256, 12, 21),     # p6
]


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "/home/cat/dcm_x101/rpn_head.rknn"
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    rk = RKNNLite(verbose=False)
    print("load_rknn:", rk.load_rknn(path))
    t0 = time.perf_counter()
    print("init_runtime:", rk.init_runtime())
    t1 = time.perf_counter()
    print(f"init_runtime 耗时: {t1 - t0:.3f} s")

    rng = np.random.default_rng(0)
    feats = [(rng.standard_normal(s).astype(np.float32) * 0.5) for s in SHAPES]

    # 预热 1 次（不计时）
    try:
        rk.inference(inputs=feats, data_format="nchw")
    except Exception as e:  # noqa: BLE001
        print("warmup FAILED:", repr(e))
        return 2

    ts = []
    out = None
    for i in range(reps):
        t0 = time.perf_counter()
        out = rk.inference(inputs=feats, data_format="nchw")
        t1 = time.perf_counter()
        ts.append(t1 - t0)

    ts_sorted = sorted(ts)
    n = len(ts_sorted)
    print(f"\n=== 耗时（{n} 次）: min={ts_sorted[0]*1000:.2f} p50={ts_sorted[n//2]*1000:.2f} "
          f"max={ts_sorted[-1]*1000:.2f} ms ===")

    print("\n=== 输出 shape/dtype（应为 10 个：obj2~obj6 (1,3,h,w) + dlt2~dlt6 (1,12,h,w)）===")
    if isinstance(out, (list, tuple)):
        for i, o in enumerate(out):
            a = np.asarray(o)
            kind = "obj" if i < 5 else "dlt"
            lvl = (i % 5) + 2
            print(f"  out[{i}] ({kind}{lvl}): shape={a.shape} dtype={a.dtype} "
                  f"min={a.min():.4f} max={a.max():.4f}")
    else:
        print("  单输出:", np.asarray(out).shape)

    print("\n=== 内存 ===")
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(("MemTotal", "MemAvailable")):
                    print(" ", line.strip())
    except OSError:
        pass

    rk.release()
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
