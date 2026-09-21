#!/usr/bin/env python3
# inference_domain_snr.py —— O87 评估：把**推理域**（ZDE++ 输入分辨率）的 SNR 算出来
#
# 动机：主链路里 ZDE++ 的曲线估计输入是 **480×272**（`pipeline.curve_level`），即相对 1920×1080
#       是 **4×4 下采样**。⇒ 采集端的"够不够用"**不应按全分辨率判，而应按推理域判**。
#       本脚本用**已有 1 Lux 原始 NV12**离线做 4×4 box 下采样，再按与全分辨率同族的代理口径算 SNR。
#
# 口径（可复现；与 `ab_3dnr_analyze.py` 对齐，仅把域换成 480×270）：
#   · 下采样 = **4×4 box 平均**（1920×1080 → 480×270）
#   · 信号 = 480×270 上 **16×16 块**中「块均值最小的 25%」的块均值中位数（块数 = 480，与全分辨率一致）
#   · 噪声 = **相邻帧**同像素 \|Δ\| 的 RMS / √2（时域口径；中位数在 3DNR 下会为 0，故用 RMS）
#   · **SNR = 信号 / 噪声**（**代理口径**，非 ISO/CPI 标准）
#
# ⚠️ 口径差异（不得掩盖）：真实链路是 RGA 做 NV12→RGB888 缩放（多为**双线性**），
#    双线性的等效平均核与 box 略不同 ⇒ 本估算**略偏乐观**；且只算 Y 平面。
#
# 用法：python3 inference_domain_snr.py <raw.nv12> [frames] [label] [stride]
#   例：python3 inference_domain_snr.py /tmp/o87_ab_collect.nv12 300 "1Lux collect"

import sys

import numpy as np

W, H = 1920, 1080
YL = W * H
FL = YL * 3 // 2
BR, BC = 16, 30          # 480x270 上 16x16 块 ⇒ 30 x 16 = 480 块
DS_W, DS_H = 480, 270


def main():
    raw = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    label = sys.argv[3] if len(sys.argv) > 3 else raw
    stride = int(sys.argv[4]) if len(sys.argv) > 4 else 1

    mm = np.memmap(raw, dtype=np.uint8, mode="r", shape=(300, FL))
    sig, sq, p90, prev = [], [], [], None
    for i in range(0, n, stride):
        y = mm[i, :YL].reshape(H, W)
        ds = y.reshape(DS_H, 4, DS_W, 4).mean(axis=(1, 3))
        b = ds[:BR * 16, :BC * 16].reshape(BR, 16, BC, 16).astype(np.float64)
        m = b.mean(axis=(1, 3))
        order = np.argsort(m, axis=None)[:int(m.size * 0.25)]
        sig.append(float(np.median(m.reshape(-1)[order])))
        if prev is not None:
            d = np.abs(ds - prev)
            sq.append(float((d ** 2).mean()))
            p90.append(float(np.percentile(d, 90)))
        prev = ds

    s = float(np.median(sig))
    se = float(np.sqrt(np.median(sq) / 2.0))
    print("%-24s 推理域(480x270): 暗部块均值=%.2f  时域σ_rms=%.3f  |Δ|p90=%.1f  ⇒ SNR=%.2f"
          % (label, s, se, float(np.median(p90)), s / se))


if __name__ == "__main__":
    main()