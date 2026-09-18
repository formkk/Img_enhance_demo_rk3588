#!/usr/bin/env python3
"""M0-3 步骤 2：应用域 512×288 增强 + PSNR/晕边指标 + 盲评材料。

- 曲线场：板端 FP16 三档输出（NCHW [1,3,H,W]），按 §3.1 G1 丢弃 pad 行取有效区
- 应用：曲线场双线性上采样到显示域 512×288（§3.1 I1），复用 tools/enhance_ref.py 的 8 次迭代参考实现
- 参考档：960×544（§4.3「升档」对照）；候选：256×144 / 480×272
- 判定：两类 PSNR 均 >35 dB ⇒ 锁 480×272（provisional）
- 盲评材料：每帧 A|B 并排（480 vs 960 随机化）+ 亮区 ROI 放大 + 答案键
"""
import glob, json, os, sys
import numpy as np, cv2
sys.path.insert(0, 'tools')
from enhance_ref import enhance, image_metrics

BASE = 'docs/evidence/reports/M0-3'
LV = [('256x144', 256, 144, 0), ('480x272', 480, 272, 1), ('960x544', 960, 544, 2)]
EFF_H = {'256x144': 144, '480x272': 270, '960x544': 540}
APP = (512, 288)
REF = '960x544'
TAGS = ['hard_00768', 'hard_00769', 'hard_00699', 'hard_00780', 'hard_00786', 'hard_00785',
        'uniform_00726', 'uniform_00728', 'uniform_00753', 'uniform_00754', 'uniform_00695', 'uniform_00756']


def load_curve(tag, lv, W, H, pad):
    a = np.fromfile(f'{BASE}/curves/{tag}_{lv}.f32', dtype=np.float32).reshape(1, 3, H, W)
    eh = EFF_H[lv]
    c = a[:, :, pad:pad + eh, :]                      # 丢弃 pad 行（§3.1 G1）
    c_up = np.stack([cv2.resize(c[0, i], APP, interpolation=cv2.INTER_LINEAR) for i in range(3)])[None]
    return c, c_up


def lowfreq_std(gray):
    k = 2 * (min(gray.shape) // 8) + 1
    return float(cv2.GaussianBlur(gray, (k, k), 0).std())


def main():
    os.makedirs(f'{BASE}/blind', exist_ok=True)
    report = {'apply_domain': f'{APP[0]}x{APP[1]}', 'reference_level': REF,
              'levels': [l[0] for l in LV], 'frames': {}}
    rng = np.random.RandomState(20260918)

    for tag in TAGS:
        x = np.load(f'{BASE}/inputs/{tag}_app{APP[0]}x{APP[1]}.npy').astype(np.float32) / 255.0
        E, C = {}, {}
        for lv, W, H, pad in LV:
            c, c_up = load_curve(tag, lv, W, H, pad)
            E[lv] = enhance(x, c_up)
            C[lv] = c
        ref = E[REF]
        entry = {'curve_range': {lv: [round(float(C[lv].min()), 4), round(float(C[lv].max()), 4)] for lv, _, _, _ in LV},
                 'vs_ref': {}}
        for lv, _, _, _ in LV:
            if lv == REF:
                continue
            m = image_metrics(ref, E[lv])
            g = cv2.cvtColor((E[lv][0].transpose(1, 2, 0) * 255).round().astype(np.uint8), cv2.COLOR_RGB2GRAY)
            entry['vs_ref'][lv] = {**m, 'low_freq_std': round(lowfreq_std(g), 3)}
        entry['ref_low_freq_std'] = round(lowfreq_std(
            cv2.cvtColor((ref[0].transpose(1, 2, 0) * 255).round().astype(np.uint8), cv2.COLOR_RGB2GRAY)), 3)
        report['frames'][tag] = entry

        # ---- 盲评材料：A|B 并排（480 vs 960 随机化）+ 亮区 ROI 4× 放大 ----
        swap = bool(rng.randint(0, 2))
        img480 = (E['480x272'][0].transpose(1, 2, 0) * 255).round().astype(np.uint8)
        img960 = (E[REF][0].transpose(1, 2, 0) * 255).round().astype(np.uint8)
        left, right = (img960, img480) if swap else (img480, img960)
        # 亮区 ROI：在参考档增强图上取最亮 64×64 块位置
        g = cv2.cvtColor(img960, cv2.COLOR_RGB2GRAY)
        _, _, _, maxloc = cv2.minMaxLoc(cv2.GaussianBlur(g, (33, 33), 0))
        cx = int(np.clip(maxloc[0], 64, APP[0] - 64)); cy = int(np.clip(maxloc[1], 64, APP[1] - 64))
        r = 64
        roi = lambda im: cv2.resize(im[cy - r:cy + r, cx - r:cx + r], (512, 512), interpolation=cv2.INTER_NEAREST)
        top = np.hstack([left, right])
        bot = np.hstack([roi(left), roi(right)])
        canvas = np.vstack([top, bot])
        canvas = cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR)
        cv2.putText(canvas, 'A', (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
        cv2.putText(canvas, 'B', (APP[0] + 10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
        cv2.putText(canvas, 'A x8', (10, APP[1] + 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)
        cv2.putText(canvas, 'B x8', (512 + 10, APP[1] + 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)
        cv2.imwrite(f'{BASE}/blind/{tag}_AB.png', canvas)
        report['frames'][tag]['blind'] = {'A': REF if swap else '480x272', 'B': '480x272' if swap else REF}

    # ---- 聚合 ----
    agg = {}
    for cls in ('hard', 'uniform'):
        tags = [t for t in TAGS if t.startswith(cls)]
        for lv in ('256x144', '480x272'):
            ps = [report['frames'][t]['vs_ref'][lv]['psnr_db'] for t in tags]
            mx = [report['frames'][t]['vs_ref'][lv]['max_abs_lsb'] for t in tags]
            agg[f'{cls}/{lv}'] = {'psnr_mean': round(float(np.mean(ps)), 2), 'psnr_min': round(float(np.min(ps)), 2),
                                  'psnr_max': round(float(np.max(ps)), 2), 'max_abs_lsb_max': round(float(np.max(mx)), 2),
                                  'n': len(tags)}
    report['aggregate'] = agg
    with open(f'{BASE}/apply_report.json', 'w') as fp:
        json.dump(report, fp, indent=2, ensure_ascii=False)

    print('%-18s %9s %9s %9s %9s' % ('class/level', 'PSNR均', 'PSNR最小', 'PSNR最大', 'maxLSB'))
    for k, v in agg.items():
        print('%-18s %9.2f %9.2f %9.2f %9.2f' % (k, v['psnr_mean'], v['psnr_min'], v['psnr_max'], v['max_abs_lsb_max']))


if __name__ == '__main__':
    main()