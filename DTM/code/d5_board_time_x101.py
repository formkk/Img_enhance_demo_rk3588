"""d5：板上测 X-101 backbone+FPN 的耗时与输出 shape（判别实验，无精度结论）。

环境：板上 python3.9 + /home/cat/rknn_ab/site/232/（rknnlite 2.3.2）
输入：随机 (1,128,192,336) fp32（真实 res2 由 DCM 解码产生，本脚本只测速度/形状）

用法（板上）：
  PYTHONPATH=/home/cat/rknn_ab/site/232 python3 d5_board_time_x101.py <rknn路径> [重复次数]
"""

import sys
import time

import numpy as np
from rknnlite.api import RKNNLite

H, W = 192, 336  # res2: 768x1344 / 4


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "/home/cat/dcm_x101/x101_backbone_fpn.rknn"
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    rk = RKNNLite(verbose=False)
    print("load_rknn:", rk.load_rknn(path))
    t0 = time.perf_counter()
    print("init_runtime:", rk.init_runtime())
    t1 = time.perf_counter()
    print(f"init_runtime 耗时: {t1 - t0:.3f} s")

    rng = np.random.default_rng(0)
    feat = rng.standard_normal((1, 128, H, W)).astype(np.float32) * 0.05

    # 预热 1 次（不计时）
    try:
        rk.inference(inputs=[feat], data_format="nchw")
    except Exception as e:  # noqa: BLE001
        print("warmup FAILED:", repr(e))
        return 2

    ts = []
    out = None
    for i in range(reps):
        t0 = time.perf_counter()
        out = rk.inference(inputs=[feat], data_format="nchw")
        t1 = time.perf_counter()
        ts.append(t1 - t0)
        print(f"run {i}: {t1 - t0:.3f} s")

    ts_sorted = sorted(ts)
    n = len(ts_sorted)
    p50 = ts_sorted[n // 2]
    print(f"\n=== 耗时（{n} 次）: min={ts_sorted[0]:.3f} p50={p50:.3f} max={ts_sorted[-1]:.3f} s ===")

    print("\n=== 输出 shape/dtype ===")
    if isinstance(out, (list, tuple)):
        for i, o in enumerate(out):
            a = np.asarray(o)
            print(f"  out[{i}]: shape={a.shape} dtype={a.dtype} min={a.min():.4f} max={a.max():.4f}")
    else:
        a = np.asarray(out)
        print(f"  out: shape={a.shape} dtype={a.dtype}")

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
