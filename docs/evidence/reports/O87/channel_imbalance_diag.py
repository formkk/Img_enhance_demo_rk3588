#!/usr/bin/env python3
"""channel_imbalance_diag.py —— 偏红根因分辨：**AWB/CCM（全局）** vs **量化/低位深（随亮度加剧）**

判定逻辑（关键）：
  · 色偏若来自 **AWB 增益 / CCM 矩阵** ⇒ 是**全局**处理 ⇒ **各亮度层的 R:G:B 应基本一致**（都偏红）；
  · 色偏若来自 **量化 / 低位深截断 + 暗部压缩** ⇒ **随 Y 降低而加剧**：**亮层应接近正常（G/R ≈0.6~0.7）**，
    暗层才塌到 0.1 以下。

方法：NV12 取 Y 与 UV（UV 为 2×2 下采样）⇒ 按 **UV 采样点对应的 Y** 分层（5 档分位）⇒
      每层算 U/V 均值 ⇒ BT.601 limited 转 RGB ⇒ 报每层 R:G:B（归一 R=1）与像素占比。
      **不做任何后处理**（只做统计；规则 `.trae/rules/10-evidence.md` §2）。

用法：python3 channel_imbalance_diag.py <raw.nv12> [frame_index] [label]
"""

import sys

import numpy as np

W, H = 1920, 1080
YL = W * H
FL = YL * 3 // 2
LAYERS = [(0, 10), (10, 25), (25, 50), (50, 75), (75, 100)]


def yuv2rgb(y, u, v):
    """BT.601 limited（与既有口径一致：YRangeMode=FULL 但按同一矩阵换算，便于横向比较）。"""
    yy = 1.164 * (y - 16.0)
    r = yy + 1.596 * (v - 128.0)
    g = yy - 0.392 * (u - 128.0) - 0.813 * (v - 128.0)
    b = yy + 2.017 * (u - 128.0)
    return r, g, b


def main():
    raw = sys.argv[1]
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    label = sys.argv[3] if len(sys.argv) > 3 else raw
    mm = np.memmap(raw, dtype=np.uint8, mode="r", shape=(300, FL))
    fr = mm[idx]
    y = fr[:YL].reshape(H, W)
    uv = fr[YL:].reshape(H // 2, W // 2, 2)
    ys = y[0::2, 0::2].astype(np.float64)          # 与 UV 采样点对齐
    u = uv[:, :, 0].astype(np.float64)
    v = uv[:, :, 1].astype(np.float64)

    print("# %s  帧 %d" % (label, idx))
    print("#   层(Y 分位)   像素占比   Y均   U均   V均  |  R    G    B   |  G/R   B/R")
    qs = np.percentile(ys, [10, 25, 50, 75])
    masks = [ys <= qs[0], (ys > qs[0]) & (ys <= qs[1]), (ys > qs[1]) & (ys <= qs[2]),
             (ys > qs[2]) & (ys <= qs[3]), ys > qs[3]]
    for (lo, hi), m in zip(LAYERS, masks):
        n = int(m.sum())
        if n == 0:
            continue
        ym, um, vm = ys[m].mean(), u[m].mean(), v[m].mean()
        r, g, b = yuv2rgb(ym, um, vm)
        print("%-8s %5.1f%%   %5.1f %5.1f %5.1f | %5.1f %5.1f %5.1f | %5.2f %5.2f"
              % ("%d-%d%%" % (lo, hi), 100.0 * n / ys.size, ym, um, vm, r, g, b,
                 (g / r if r > 0 else float('nan')), (b / r if r > 0 else float('nan'))))


if __name__ == "__main__":
    main()