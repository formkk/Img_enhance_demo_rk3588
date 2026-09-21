#!/usr/bin/env python3
# dark_sigma_smear_analyze.py —— O86「同模式基准」离线分析（暗部 σ + 拖影代理口径）
#
# 性质：**离线分析**（开发机跑），输入 = 板端 `v4l2-ctl --stream-to` 落的**裸 NV12**（逐帧原始链路输出）。
#       **不做任何非线性后处理**（规则 `.trae/rules/10-evidence.md` §2）：只做几何切片与统计。
#
# 口径（可复现；本口径由本次执行定义，随证据档 §8 登记）：
#   · 帧 = NV12 1920×1080 单平面布局：Y 平面在前 2,073,600 B，UV 交错在后 1,036,800 B；**只用 Y 平面**。
#   · 块 = 64×64，取前 1024 行（16 块行）× 全宽 1920（30 块列）= 480 块（尾部 56 行不参与）。
#   · **暗部块** = 该帧**块均值最小的 25%**（120 块）——用"块均值排序"而不是固定阈值，避免绑定具体照度。
#   · 暗部 σ = 该帧暗部块的**块内标准差**取中位数；跨帧再取中位 / p90。
#   · 拖影（**代理口径**）= 相邻帧 Y 的**同像素绝对差** |Δ|，在全帧与"上帧暗部块"两处各报 p50/p95/p99 与 |Δ|>8 占比。
#     ⚠️ 它度量的是**静态场景下的时域不干净度**（噪声底 + 任何残影）；**真实运动拖尾需运动靶标 ⇒ 本项未做**。
#   · 有效性检查：每帧全帧 Y mean 的漂移（AE 是否真固定 / 场景是否变了）。
#
# 用法：
#   python3 dark_sigma_smear_analyze.py <raw.nv12> <frames> <out.json> [--png <out.png>] [--png-frame N]

import json
import sys

import numpy as np

W, H = 1920, 1080
Y_LEN = W * H                      # 2,073,600
FRAME_LEN = Y_LEN * 3 // 2         # NV12: Y + UV(420) ⇒ 3,110,400
BLK = 64
BLK_ROWS = (H // BLK)              # 16（末 56 行不参与）
BLK_COLS = (W // BLK)              # 30
N_BLK = BLK_ROWS * BLK_COLS        # 480
DARK_FRAC = 0.25
DARK_N = int(N_BLK * DARK_FRAC)    # 120
CHUNK = 20                         # 分批处理，控制内存


def block_stats(y):
    """y: (H,W) uint8 → 块均值 (16,30) float64、块标准差 (16,30) float64。"""
    b = y[:BLK_ROWS * BLK, :BLK_COLS * BLK].reshape(BLK_ROWS, BLK, BLK_COLS, BLK).astype(np.float64)
    m = b.mean(axis=(1, 3))
    var = (b * b).mean(axis=(1, 3)) - m * m
    return m, np.sqrt(np.maximum(var, 0.0))


def main():
    raw, n_frames, out_json = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    png = None
    png_frame = 0
    if "--png" in sys.argv:
        png = sys.argv[sys.argv.index("--png") + 1]
    if "--png-frame" in sys.argv:
        png_frame = int(sys.argv[sys.argv.index("--png-frame") + 1])

    mm = np.memmap(raw, dtype=np.uint8, mode="r", shape=(n_frames, FRAME_LEN))
    assert mm.shape[0] == n_frames, "帧数与文件大小不符"

    per_frame = []          # 每帧: dict(ymean, ymax, yp1, yp50, yp99, dark_sigma, dark_mean_lo, dark_mean_hi, blkmean_p10/50/90)
    diff_all = []           # 逐帧差分的分位数（全帧）
    diff_dark = []          # 逐帧差分的分位数（上帧暗部块）
    frac_gt8_all, frac_gt8_dark = [], []

    for s in range(0, n_frames, CHUNK):
        e = min(s + CHUNK, n_frames)
        for i in range(s, e):
            y = mm[i, :Y_LEN].reshape(H, W)

            if png is not None and i == png_frame:
                # 直出转换（BT.601 limited 的 Y + UV→RGB），**无 γ / 无对比度处理**
                uv = mm[i, Y_LEN:].reshape(H // 2, W // 2, 2).astype(np.float64)
                u = np.repeat(np.repeat(uv[:, :, 0], 2, axis=0), 2, axis=1)
                v = np.repeat(np.repeat(uv[:, :, 1], 2, axis=0), 2, axis=1)
                yy = y.astype(np.float64)
                r = 1.164 * (yy - 16) + 1.596 * (v - 128)
                g = 1.164 * (yy - 16) - 0.392 * (u - 128) - 0.813 * (v - 128)
                b = 1.164 * (yy - 16) + 2.017 * (u - 128)
                rgb = np.stack([r, g, b], axis=-1).clip(0, 255).astype(np.uint8)
                try:
                    from PIL import Image
                    Image.fromarray(rgb).save(png)
                    print("PNG(直出，无 γ) =", png)
                except Exception as ex:      # PIL 缺失不阻塞数值分析
                    print("PNG 跳过（%s）" % ex)

            m, sd = block_stats(y)
            order = np.argsort(m, axis=None)
            dark_idx = order[:DARK_N]
            dark_sig = np.median(sd.reshape(-1)[dark_idx])
            dark_m = m.reshape(-1)[dark_idx]

            yf = y.reshape(-1).astype(np.float64)
            per_frame.append(dict(
                ymean=float(yf.mean()),
                yp1=float(np.percentile(yf, 1)), yp50=float(np.percentile(yf, 50)),
                yp99=float(np.percentile(yf, 99)), ymax=int(yf.max()),
                blkmean_p10=float(np.percentile(m, 10)), blkmean_p50=float(np.percentile(m, 50)),
                blkmean_p90=float(np.percentile(m, 90)),
                dark_sigma=float(dark_sig),
                dark_mean_lo=float(dark_m.min()), dark_mean_hi=float(dark_m.max()),
            ))

            if i > s or s > 0:                       # 需要前一帧
                prev = mm[i - 1, :Y_LEN].reshape(H, W)
                d = np.abs(y.astype(np.int16) - prev.astype(np.int16)).astype(np.uint8)
                df = d.reshape(-1)
                diff_all.append([float(np.percentile(df, q)) for q in (50, 95, 99)] + [int(df.max())])
                frac_gt8_all.append(float((df > 8).mean()))
                dm = np.zeros((H, W), dtype=bool)
                for r_, c_ in zip(dark_idx // BLK_COLS, dark_idx % BLK_COLS):
                    dm[r_ * BLK:(r_ + 1) * BLK, c_ * BLK:(c_ + 1) * BLK] = True
                dd = d[dm]
                diff_dark.append([float(np.percentile(dd, q)) for q in (50, 95, 99)] + [int(dd.max())])
                frac_gt8_dark.append(float((dd > 8).mean()))

    def q(v, p):
        return float(np.percentile(np.asarray(v, dtype=np.float64), p))

    rep = dict(
        input=dict(raw=raw, frames=n_frames, frame_len=FRAME_LEN, layout="NV12 1920x1080, 只用 Y 平面"),
        criteria=dict(blk="64x64", grid=[BLK_ROWS, BLK_COLS], n_blk=N_BLK,
                       dark_rule="块均值最小的 25%", dark_n=DARK_N,
                       smear_rule="相邻帧同像素 |ΔY|（代理口径；真实运动拖尾需运动靶标，未做）"),
        y_mean=dict(median=q([f["ymean"] for f in per_frame], 50),
                    min=min(f["ymean"] for f in per_frame), max=max(f["ymean"] for f in per_frame),
                    std=float(np.std([f["ymean"] for f in per_frame])),
                    p1=q([f["yp1"] for f in per_frame], 50), p50=q([f["yp50"] for f in per_frame], 50),
                    p99=q([f["yp99"] for f in per_frame], 50), ymax=max(f["ymax"] for f in per_frame)),
        block_mean=dict(p10=q([f["blkmean_p10"] for f in per_frame], 50),
                        p50=q([f["blkmean_p50"] for f in per_frame], 50),
                        p90=q([f["blkmean_p90"] for f in per_frame], 50)),
        dark_roi=dict(sigma_median=q([f["dark_sigma"] for f in per_frame], 50),
                      sigma_p90=q([f["dark_sigma"] for f in per_frame], 90),
                      sigma_min=min(f["dark_sigma"] for f in per_frame),
                      sigma_max=max(f["dark_sigma"] for f in per_frame),
                      mean_lo_median=q([f["dark_mean_lo"] for f in per_frame], 50),
                      mean_hi_median=q([f["dark_mean_hi"] for f in per_frame], 50)),
        diff_all=dict(p50=q([d[0] for d in diff_all], 50), p95=q([d[1] for d in diff_all], 50),
                      p99=q([d[2] for d in diff_all], 50), max=max(d[3] for d in diff_all),
                      frac_gt8_median=q(frac_gt8_all, 50)),
        diff_dark=dict(p50=q([d[0] for d in diff_dark], 50), p95=q([d[1] for d in diff_dark], 50),
                       p99=q([d[2] for d in diff_dark], 50), max=max(d[3] for d in diff_dark),
                       frac_gt8_median=q(frac_gt8_dark, 50)),
        n_diff_pairs=len(diff_all),
    )
    with open(out_json, "w") as fh:
        json.dump(rep, fh, indent=2, ensure_ascii=False)
    print(json.dumps(rep, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()