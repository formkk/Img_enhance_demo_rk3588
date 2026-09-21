#!/usr/bin/env python3
"""visibility_prior.py —— 「增益换可见性」的零副作用先验（用已有原始帧，不动板子）

口径（**可见性类**，区别于 SNR 判据 —— 见证据档 §12.15/§12.17）：
  · dark_mean  : 16×16 块中「块均值最小 25%」的块均值**中位数**  → "暗部有多亮"
  · dark_std   : 同一批暗块的**块内 std 中位数**                    → "暗部细节/纹理能量"
  · y_p10/p50/p90                                                  → 亮度分布
  · g_over_r   : 全帧 G/R（BT.601 limited）                         → 色偏
  · 可选 --png : **原样直出** PNG（无 γ、无任何非线性后处理；规则 10-evidence §2）

用法：python3 visibility_prior.py <raw.nv12> [frame] [label] [--png out.png]
"""

import struct
import sys
import zlib

import numpy as np

W, H = 1920, 1080
YL = W * H
FL = YL * 3 // 2


def png_write(path, rgb):
    """8-bit RGB PNG 直出（无 γ）。"""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[i].tobytes() for i in range(h))

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    png = None
    if "--png" in sys.argv:
        png = sys.argv[sys.argv.index("--png") + 1]
        args = [a for a in args if a != png]
    raw, idx, label = args[0], int(args[1]), args[2]
    mm = np.memmap(raw, dtype=np.uint8, mode="r", shape=(300, FL))
    fr = mm[idx]
    y = fr[:YL].reshape(H, W).astype(np.float64)
    uv = fr[YL:].reshape(H // 2, W // 2, 2)
    u = uv[:, :, 0].astype(np.float64)
    v = uv[:, :, 1].astype(np.float64)

    bs = 16
    yb = y[:H // bs * bs, :W // bs * bs].reshape(H // bs, bs, W // bs, bs).transpose(0, 2, 1, 3)
    bm = yb.mean(axis=(2, 3))
    bstd = yb.std(axis=(2, 3))
    flat = bm.ravel()
    thr = np.percentile(flat, 25)
    sel = flat <= thr
    dark_mean = float(np.median(flat[sel]))
    dark_std = float(np.median(bstd.ravel()[sel]))
    p10, p50, p90 = [float(x) for x in np.percentile(y, [10, 50, 90])]
    ym, um, vm = y.mean(), u.mean(), v.mean()
    yy = 1.164 * (ym - 16.0)
    r = yy + 1.596 * (vm - 128.0)
    g = yy - 0.392 * (um - 128.0) - 0.813 * (vm - 128.0)
    b = yy + 2.017 * (um - 128.0)
    nd = 1024.0 * 1024.0
    print("%-20s Ymean=%6.2f  p10/p50/p90=%5.1f/%5.1f/%5.1f  "
          "dark_mean=%6.2f  dark_std=%5.2f  G/R=%5.2f  B/R=%5.2f"
          % (label, ym, p10, p50, p90, dark_mean, dark_std,
             (g / r if r > 0 else float('nan')), (b / r if r > 0 else float('nan'))), end="")
    if png:
        uu = np.repeat(np.repeat(u, 2, 0), 2, 1)
        vv = np.repeat(np.repeat(v, 2, 0), 2, 1)
        yy2 = 1.164 * (y - 16.0)
        R = np.clip(yy2 + 1.596 * (vv - 128.0), 0, 255)
        G = np.clip(yy2 - 0.392 * (uu - 128.0) - 0.813 * (vv - 128.0), 0, 255)
        B = np.clip(yy2 + 2.017 * (uu - 128.0), 0, 255)
        rgb = np.stack([R, G, B], -1).astype(np.uint8)
        png_write(png, rgb)
        import os
        print("  PNG=%s (%d B)" % (png.split("/")[-1], os.path.getsize(png)))
    else:
        print()


if __name__ == "__main__":
    main()