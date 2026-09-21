#!/usr/bin/env python3
# multiframe_average_analyze.py —— 多帧时域平均（C′）的离线收益评估
#
# 目的：在**不动 sensor 时序、不改硬件**的前提下，量化「N 帧平均」能把 SNR 提到多少。
# 口径（与 illum_gain_sweep_analyze.py 对齐，可复现）：
#   · N ∈ {1,2,4,8,16,32}：按**不重叠**分组对 Y 平面求平均（float），得到 300/N 个「平均帧」
#   · 信号 = 平均帧的**暗部块均值**（64×64 块、块均值最小的 25%，与单帧同口径）
#   · 噪声 = **相邻平均帧**同像素 |ΔY| 的中位数 / 1.128（时域 σ_eff）
#   · SNR = 信号 / 噪声
#   · ⚠️ 输出帧率代价 = 30/N（N=1 → 30 fps；N=16 → 1.9 fps）
#
# 用法：python3 multiframe_average_analyze.py <raw.nv12> <frames> [label]

import json
import sys

import numpy as np

W, H = 1920, 1080
YL = W * H
FL = YL * 3 // 2
BLK = 64
BR, BC = H // BLK, W // BLK


def dark_block_mean(y8):
    b = y8[:BR * BLK, :BC * BLK].reshape(BR, BLK, BC, BLK).astype(np.float64)
    m = b.mean(axis=(1, 3))
    order = np.argsort(m, axis=None)[:int(m.size * 0.25)]
    return float(np.median(m.reshape(-1)[order]))


def main():
    raw, n = sys.argv[1], int(sys.argv[2])
    label = sys.argv[3] if len(sys.argv) > 3 else raw
    mm = np.memmap(raw, dtype=np.uint8, mode="r", shape=(n, FL))

    out = []
    for N in (1, 2, 4, 8, 16, 32):
        groups = n // N
        dmed, sig = [], []
        prev = None
        for gidx in range(groups):
            acc = np.zeros((H, W), dtype=np.float32)
            for k in range(N):
                acc += mm[gidx * N + k, :YL].reshape(H, W).astype(np.float32)
            acc /= N
            sig.append(dark_block_mean(acc))
            if prev is not None:
                d = np.abs(acc - prev)
                dmed.append(float(np.median(d)))
            prev = acc
        s = float(np.median(sig))
        if dmed:
            noi = float(np.median(dmed)) / 1.128
            out.append(dict(N=N, out_fps=round(30.0 / N, 2), groups=groups,
                            dark_block_mean=s, sigma_eff=noi, snr=s / noi))
        else:
            out.append(dict(N=N, out_fps=round(30.0 / N, 2), groups=groups,
                            dark_block_mean=s, sigma_eff=None, snr=None))
    rep = dict(label=label, frames=n, sweep=out)
    print(json.dumps(rep, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()