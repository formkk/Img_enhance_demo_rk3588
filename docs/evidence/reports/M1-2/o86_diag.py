#!/usr/bin/env python3
# o86_diag.py —— O86 基准的**诊断**（判断帧间差分是否被"场景运动 / 光频闪"主导，并给出结构去除后的噪声代理）
#
# 性质：离线诊断，只读原始 NV12，输出统计（不改图、不做非线性后处理）。
# 用法：python3 o86_diag.py <raw.nv12> <frames> [块均值口径同 analyze]

import sys
import numpy as np

W, H = 1920, 1080
Y_LEN = W * H
FRAME_LEN = Y_LEN * 3 // 2
BLK = 64
BLK_ROWS, BLK_COLS = H // BLK, W // BLK


def main():
    raw, n = sys.argv[1], int(sys.argv[2])
    mm = np.memmap(raw, dtype=np.uint8, mode="r", shape=(n, FRAME_LEN))

    d95, d50, ymean, hp_sigma, dark_sigma = [], [], [], [], []
    blk_diff = np.zeros((BLK_ROWS, BLK_COLS), dtype=np.float64)
    blk_sig = np.zeros((BLK_ROWS, BLK_COLS), dtype=np.float64)
    blk_cnt = 0

    prev = None
    for i in range(n):
        y = mm[i, :Y_LEN].reshape(H, W)
        ymean.append(float(y.mean()))

        # 结构去除后的噪声代理：Y − 3×3 均值（高通），取 ≤ 全帧 25% 分位的暗像素
        yf = y.astype(np.float32)
        k = np.zeros_like(yf)
        k[1:, :] += yf[:-1, :]; k[:-1, :] += yf[1:, :]
        k[:, 1:] += yf[:, :-1]; k[:, :-1] += yf[:, 1:]
        cnt = np.full_like(yf, 4.0); cnt[1:, :] += 1; cnt[:-1, :] += 1
        cnt[:, 1:] += 1; cnt[:, :-1] += 1
        hp = yf - k / cnt
        thr = np.percentile(yf, 25)
        hp_sigma.append(float(hp[yf <= thr].std()))

        b = y[:BLK_ROWS * BLK, :BLK_COLS * BLK].reshape(BLK_ROWS, BLK, BLK_COLS, BLK).astype(np.float64)
        m = b.mean(axis=(1, 3)); sd = b.std(axis=(1, 3))
        blk_sig += sd; blk_cnt += 1
        order = np.argsort(m, axis=None)[:int(m.size * 0.25)]
        dark_sigma.append(float(np.median(sd.reshape(-1)[order])))

        if prev is not None:
            d = np.abs(y.astype(np.int16) - prev.astype(np.int16)).astype(np.uint8)
            d95.append(float(np.percentile(d, 95))); d50.append(float(np.percentile(d, 50)))
            db = d[:BLK_ROWS * BLK, :BLK_COLS * BLK].reshape(BLK_ROWS, BLK, BLK_COLS, BLK).astype(np.float64)
            blk_diff += db.mean(axis=(1, 3))
        prev = y

    blk_sig /= blk_cnt
    blk_diff /= max(1, len(d95))

    print("=== 时域（逐帧）===")
    print("帧间 |Δ| p50: 中位 %.2f  范围 %.2f~%.2f" % (np.median(d50), min(d50), max(d50)))
    print("帧间 |Δ| p95: 中位 %.2f  范围 %.2f~%.2f" % (np.median(d95), min(d95), max(d95)))
    # 周期性（光频闪会表现为 30fps 对 50/60Hz 的差频 ≈10/30Hz）
    a = np.asarray(d95, dtype=np.float64) - np.mean(d95)
    f = np.abs(np.fft.rfft(a))[:len(a) // 2]
    top = np.argsort(f)[::-1][:5]
    print("帧间 p95 序列的 FFT 前 5 个峰值 bin:", [(int(t), round(float(f[t]), 1)) for t in top],
          "（bin k ⇒ %.3f Hz）" % (30.0 / len(a)))
    print("全帧 Y mean 漂移: min %.2f max %.2f std %.3f" % (min(ymean), max(ymean), float(np.std(ymean))))
    print("结构去除后噪声代理（暗 25%% 像素的高通 σ）: 中位 %.3f  范围 %.3f~%.3f"
          % (np.median(hp_sigma), min(hp_sigma), max(hp_sigma)))
    print("暗部块 σ（块均值最小 25%%）: 中位 %.3f" % np.median(dark_sigma))

    print("=== 空间（300 帧平均）===")
    print("块 σ 的分布: p10 %.2f p50 %.2f p90 %.2f（最大 %.2f 最小 %.2f）"
          % (np.percentile(blk_sig, 10), np.percentile(blk_sig, 50), np.percentile(blk_sig, 90),
             blk_sig.max(), blk_sig.min()))
    print("块 |Δ| 的分布: p10 %.2f p50 %.2f p90 %.2f（最大 %.2f 最小 %.2f）"
          % (np.percentile(blk_diff, 10), np.percentile(blk_diff, 50), np.percentile(blk_diff, 90),
             blk_diff.max(), blk_diff.min()))
    # 差分是否集中（集中 ⇒ 局部运动；均匀 ⇒ 全局闪烁/噪声）
    share_top10pct = float(np.sort(blk_diff, axis=None)[::-1][:int(blk_diff.size * 0.1)].sum() / blk_diff.sum())
    print("差分能量集中在最强 10%% 块的比例: %.1f%%（均匀分布应为 10%%）" % (share_top10pct * 100))
    np.save("/tmp/o86_blk_diff.npy", blk_diff)
    np.save("/tmp/o86_blk_sigma.npy", blk_sig)


if __name__ == "__main__":
    main()