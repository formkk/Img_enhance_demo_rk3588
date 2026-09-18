#!/usr/bin/env python3
"""M0-3 步骤 1：从 LOLv2 代用暗帧选两类各 6 帧，并按 §3.1 G1 生成三档模型输入。

- 数据源：refs/pre-research/lolv2/extracted/LOLv2/Real_captured/Test/Low（与 U1 校准集 Train/Low 分离）
- 分类：单侧硬光源 = 亮度重心强偏离且照度离散度大；均匀暗光 = 照度离散度小且亮度居中
- 几何（§3.1 G1）：源图等比自 1920×1080 概念有效区缩到各档有效区
    256×144(无pad) / 480×270→480×272(上下各1pad) / 960×540→960×544(上下各2pad)，零填充
- 输出：docs/evidence/reports/M0-3/inputs/<tag>_{256x144,480x272,960x544}.f32  (NHWC uint8? -> float32 [1,H,W,3])
  + report.json：选帧清单与两类统计

本实验输入为模型用的 uniform [0,1] float32；板端 rknn_c_probe 支持 float32 喂入。
"""
import glob, json, os
import numpy as np, cv2

SRC = 'refs/pre-research/lolv2/extracted/LOLv2/Real_captured/Test/Low'
OUT = 'docs/evidence/reports/M0-3/inputs'
N = 6  # 每类取 6 帧

LV = [('256x144', 256, 144), ('480x272', 480, 272), ('960x544', 960, 544)]
EFF = {'256x144': (256, 144), '480x272': (480, 270), '960x544': (960, 540)}
PAD = {'256x144': 0, '480x272': 1, '960x544': 2}
APP = (512, 288)  # §3.1 显示应用域（v3.33）


def crop_169(rgb):
    """中心裁到 16:9（真实系统原生 16:9），避免代用图 3:2 直接缩放带来的畸变。"""
    h, w = rgb.shape[:2]
    if w * 9 > h * 16:      # 过宽 -> 裁左右
        nw = h * 16 // 9
        x0 = (w - nw) // 2
        return rgb[:, x0:x0 + nw]
    nh = w * 9 // 16        # 过高 -> 裁上下
    y0 = (h - nh) // 2
    return rgb[y0:y0 + nh, :]


def classify(f):
    bgr = cv2.imread(f, cv2.IMREAD_COLOR)
    g = cv2.cvtColor(cv2.resize(bgr, (256, 144), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)
    low = cv2.GaussianBlur(g, (35, 35), 0)
    spread = (low.max() - low.min()) / (low.mean() + 1e-6)
    thr = np.percentile(g, 99)
    ys, xs = np.where(g >= thr)
    cx = float(xs.mean()) / g.shape[1] if len(xs) else 0.5
    cy = float(ys.mean()) / g.shape[0] if len(ys) else 0.5
    return {'mean': float(g.mean()), 'spread': float(spread), 'cx': cx, 'cy': cy,
            'lowstd': float(low.std())}


def pick_even(cands, n):
    """从按 spread 排序的候选里跨区间均匀取 n 个，避免相邻近重复帧主导。"""
    if len(cands) <= n:
        return cands
    idx = np.linspace(0, len(cands) - 1, n).round().astype(int)
    return [cands[i] for i in dict.fromkeys(idx)]


def main():
    fs = sorted(glob.glob(os.path.join(SRC, '*.png')))
    info = {os.path.basename(f): classify(f) for f in fs}

    hard = sorted([(os.path.basename(f), d) for f, d in info.items()
                   if d['spread'] > 4.0 and (d['cx'] > 0.75 or d['cx'] < 0.25 or d['cy'] < 0.2)],
                  key=lambda kv: -kv[1]['spread'])
    unif = sorted([(os.path.basename(f), d) for f, d in info.items()
                   if d['spread'] < 1.9 and 0.45 < d['cx'] < 0.58],
                  key=lambda kv: kv[1]['spread'])

    assert len(hard) >= N, f'硬光源候选 {len(hard)} < {N}'
    assert len(unif) >= N, f'均匀候选 {len(unif)} < {N}'
    sel = {'hard': [n for n, _ in pick_even(hard, N)],
           'uniform': [n for n, _ in pick_even(unif, N)]}

    report = {'source': SRC, 'n_sampled': len(fs), 'classes': sel,
              'stats': {k: {n: info[n] for n in lst} for k, lst in sel.items()},
              'levels': LV}
    os.makedirs(OUT, exist_ok=True)

    for cls_name in ('hard', 'uniform'):
        for name in sel[cls_name]:
            tag = f'{cls_name}_{name[:-4]}'
            bgr = cv2.imread(os.path.join(SRC, name))
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            src = crop_169(rgb)  # 代用图为 3:2，先中心裁到 16:9（真实系统原生 16:9），避免缩放畸变
            for lv, W, H in LV:
                ew, eh = EFF[lv]
                eff = cv2.resize(src, (ew, eh), interpolation=cv2.INTER_AREA)
                pad = PAD[lv]
                canvas = np.zeros((H, W, 3), dtype=np.uint8)
                canvas[pad:pad + eh] = eff
                # 模型输入 float32 [1,H,W,3] 值域 0..255（模型 mean=0/std=255；板端以 u8 模式喂入）
                x = np.ascontiguousarray(canvas.astype(np.float32)[None])
                x.tofile(f'{OUT}/{tag}_{lv}.f32')
            # 应用域帧（§3.1 显示域 512×288），供 step2 应用 8 次迭代
            app = cv2.resize(src, APP, interpolation=cv2.INTER_AREA)
            np.save(f'{OUT}/{tag}_app{APP[0]}x{APP[1]}.npy',
                    app.astype(np.float32).transpose(2, 0, 1)[None])
            print(f'[ok] {cls_name}/{name} -> {tag}_*.f32')

    with open(f'{OUT}/../report.json', 'w') as fp:
        json.dump(report, fp, indent=2, ensure_ascii=False)
    print('report:', OUT + '/../report.json')
    print('hard:', sel['hard'])
    print('uniform:', sel['uniform'])


if __name__ == '__main__':
    main()