#!/usr/bin/env python3
# ab_3dnr_analyze.py —— O87 的 3DNR A/B 专项分析（collect 关 vs demo 开）
#
# 背景：`illum_gain_sweep_analyze.py` 用「相邻帧 |Δ| 中位数 / 1.128」估 σ_eff，
#       而 **demo 档（3DNR 开）下该中位数 = 0** ⇒ 除零、且中位数不再是有效估计。
# 本脚本改用**稳健口径**：RMS 差分（对中位数为 0 的情形依然有效）+ 分位数 + 大差异像素占比。
#
# 口径（与 O87 主口径对齐，仅把"中位数"替换为"RMS"）：
#   · σ_temporal_rms = sqrt( mean( Δ² ) / 2 )，Δ = 相邻帧同像素 Y 差（Y 平面）
#     （两帧独立同分布时 Var(Δ)=2σ² ⇒ σ = sqrt(mean(Δ²)/2)）
#   · 另报 |Δ| 的 p50/p90/p99/max 与 |Δ|>8 的像素占比（占比可揭示是否只压了"小噪声"、留下了"大变化"）
#   · 信号 = 暗部块均值（64×64、块均值最小 25%）；SNR_temporal = 信号 / σ_temporal_rms
#
# 用法：python3 ab_3dnr_analyze.py <raw.nv12> <frames> <label>

import json
import sys

import numpy as np

W, H = 1920, 1080
YL = W * H
FL = YL * 3 // 2
BLK = 64
BR, BC = H // BLK, W // BLK


def main():
    raw, n, label = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    mm = np.memmap(raw, dtype=np.uint8, mode="r", shape=(n, FL))
    sig, hp, p50, p90, p99, frac8, mx = [], [], [], [], [], [], []
    sq = []
    prev = None
    for i in range(n):
        y = mm[i, :YL].reshape(H, W)
        b = y[:BR * BLK, :BC * BLK].reshape(BR, BLK, BC, BLK).astype(np.float64)
        m = b.mean(axis=(1, 3))
        order = np.argsort(m, axis=None)[:int(m.size * 0.25)]
        sig.append(float(np.median(m.reshape(-1)[order])))

        yf = y.astype(np.float32)
        k = np.zeros_like(yf)
        k[1:, :] += yf[:-1, :]; k[:-1, :] += yf[1:, :]
        k[:, 1:] += yf[:, :-1]; k[:, :-1] += yf[:, 1:]
        c = np.full_like(yf, 4.0)
        c[1:, :] += 1; c[:-1, :] += 1; c[:, 1:] += 1; c[:, :-1] += 1
        hp.append(float((yf - k / c)[yf <= np.percentile(yf, 25)].std()))

        if prev is not None:
            d = np.abs(y.astype(np.int16) - prev.astype(np.int16)).astype(np.int32)
            p50.append(float(np.median(d))); p90.append(float(np.percentile(d, 90)))
            p99.append(float(np.percentile(d, 99))); mx.append(int(d.max()))
            frac8.append(float((d > 8).mean())); sq.append(float((d.astype(np.float64) ** 2).mean()))
        prev = y

    s = float(np.median(sig)); hp_m = float(np.median(hp))
    sig_rms = float(np.sqrt(np.median(sq) / 2.0))
    rep = dict(label=label, frames=n,
               dark_block_mean=s, highpass_sigma=hp_m,
               dt_p50=float(np.median(p50)), dt_p90=float(np.median(p90)),
               dt_p99=float(np.median(p99)), dt_max=max(mx),
               dt_frac_gt8=float(np.median(frac8)),
               sigma_temporal_rms=sig_rms,
               snr_temporal_rms=(s / sig_rms if sig_rms > 0 else None),
               snr_highpass=s / hp_m)
    print(json.dumps(rep, ensure_ascii=False))


if __name__ == "__main__":
    main()