#!/usr/bin/env python3
# illum_gain_sweep_analyze.py —— 照度–SNR / 增益扫描 的离线分析（开发机跑；输入 = 板端裸 NV12）
#
# 性质：**离线统计**（只读原始链路输出，不做任何非线性后处理）。
# 口径（本次登记，可复现）：
#   · 只用 Y 平面；帧长 = 3,110,400 B（NV12 1920×1080）
#   · 暗部块 = 64×64 块（前 1024 行 × 全宽 = 480 块）中**块均值最小的 25%**
#   · 信号 = 这些暗部块的**块均值中位数**（"暗部有多少码值"）
#   · 噪声底 = `Y − 3×3 均值` 高通在**暗 25% 像素**上的 σ（去结构）
#   · 时域噪声 = 相邻帧同像素 |ΔY| 的中位数（纯随机噪声时 ≈1.128·σ_eff，且与 lag 无关）
#   · **SNR 代理口径 = 暗部块均值中位数 / 噪声底 σ**（标明为代理，不是 ITU/ISO 定义）
#
# 用法：python3 illum_gain_sweep_analyze.py <raw.nv12> <frames> [label]

import json
import sys

import numpy as np

W, H = 1920, 1080
YL = W * H
FL = YL * 3 // 2
BLK = 64
BR, BC = H // BLK, W // BLK          # 16 × 30


def main():
    raw, n = sys.argv[1], int(sys.argv[2])
    label = sys.argv[3] if len(sys.argv) > 3 else raw
    mm = np.memmap(raw, dtype=np.uint8, mode="r", shape=(n, FL))

    ymean, dark_blk, hp_sig, dmed = [], [], [], []
    prev = None
    for i in range(n):
        y = mm[i, :YL].reshape(H, W)
        yf = y.astype(np.float32)
        ymean.append(float(yf.mean()))

        b = y[:BR * BLK, :BC * BLK].reshape(BR, BLK, BC, BLK).astype(np.float64)
        m = b.mean(axis=(1, 3))
        order = np.argsort(m, axis=None)[:int(m.size * 0.25)]
        dark_blk.append(float(np.median(m.reshape(-1)[order])))

        k = np.zeros_like(yf)
        k[1:, :] += yf[:-1, :]; k[:-1, :] += yf[1:, :]
        k[:, 1:] += yf[:, :-1]; k[:, :-1] += yf[:, 1:]
        c = np.full_like(yf, 4.0)
        c[1:, :] += 1; c[:-1, :] += 1; c[:, 1:] += 1; c[:, :-1] += 1
        hp = yf - k / c
        thr = np.percentile(yf, 25)
        hp_sig.append(float(hp[yf <= thr].std()))

        if prev is not None:
            d = np.abs(y.astype(np.int16) - prev.astype(np.int16))
            dmed.append(float(np.median(d)))
        prev = y

    sig = float(np.median(dark_blk))
    noi = float(np.median(hp_sig))
    yf_last = mm[n // 2, :YL]
    rep = dict(
        label=label, frames=n,
        y_mean=float(np.median(ymean)), y_mean_min=min(ymean), y_mean_max=max(ymean),
        y_p50=float(np.percentile(yf_last, 50)), y_p99=float(np.percentile(yf_last, 99)),
        y_max=int(yf_last.max()),
        dark_block_mean=sig,
        noise_sigma=noi,                       # 噪声底（高通 σ，暗 25% 像素）
        temporal_absdiff_median=float(np.median(dmed)),
        sigma_eff_from_temporal=float(np.median(dmed) / 1.128),
        # 两个 SNR 口径都给（**极弱信号下以 snr_temporal 为准**）：
        #   snr_highpass  = 暗部块均值 / 高通 σ  —— 会**高估**（ISP 空间降噪把空间高频抹平，n=3 实测差 3×）
        #   snr_temporal  = 暗部块均值 / 时域 σ_eff —— **可信口径**（3DNR 关闭时帧间噪声未被压）
        snr_highpass=sig / noi,
        snr_temporal=sig / (float(np.median(dmed)) / 1.128),
    )
    print(json.dumps(rep, ensure_ascii=False))


if __name__ == "__main__":
    main()