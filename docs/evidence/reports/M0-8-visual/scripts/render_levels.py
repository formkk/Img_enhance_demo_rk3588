"""三档曲线场 → 8 次迭代应用 → 四联对比图（原始 | 256×144 | 480×272 | 960×544）+ 指标。"""
import json, sys
import numpy as np, cv2
sys.path.insert(0, 'tools')
from enhance_ref import enhance

OUT = 'docs/evidence/reports/M0-8-visual'
LV = [('256x144', 256, 144), ('480x272', 480, 272), ('960x544', 960, 544)]
CASES = [('18', '/tmp/vis/src_18.png', (1080, 720), 1.0),
         ('06cr2', '/tmp/vis/src_06cr2.png', (5496, 3670), 0.25)]
report = {}
for tag, src_path, native, scale in CASES:
    bgr = cv2.imread(src_path)
    W, H = int(native[0] * scale), int(native[1] * scale)
    img = cv2.resize(bgr, (W, H), interpolation=cv2.INTER_AREA) if scale != 1.0 else bgr
    x = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    entry = {'apply_domain': f'{W}x{H}', 'levels': {}}

    def metrics(gray_u8):
        gray = gray_u8
        g = gray.astype(np.float32)
        k = max(5, (min(W, H) // 8) | 1)                    # 低频（照明均匀性）：大核模糊后的 std
        low = cv2.GaussianBlur(g, (k, k), 0)
        return {'mean': round(float(g.mean()), 2), 'p50': float(np.percentile(g, 50)),
                'p90': float(np.percentile(g, 90)), 'p99': float(np.percentile(g, 99)),
                'dark_lt30': round(float((g < 30).mean()), 4),
                'detail_lap': round(float(cv2.Laplacian(gray, cv2.CV_32F).var()), 1),
                'low_freq_std': round(float(low.std()), 2),
                'low_freq_range': [round(float(low.min()), 1), round(float(low.max()), 1)]}

    panels = [img]; labels = ['原始']
    entry['original'] = metrics(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    for name, lw, lh in LV:
        c = np.fromfile(f'/tmp/vis/lvc_{tag}_{name}.f32', dtype=np.float32).reshape(3, lh, lw)
        c_up = np.stack([cv2.resize(c[i], (W, H), interpolation=cv2.INTER_LINEAR) for i in range(3)])[None]
        y = enhance(x, c_up)
        out = (y[0].transpose(1, 2, 0) * 255.0).round().astype(np.uint8)
        bgr_e = cv2.cvtColor(out, cv2.COLOR_RGB2BGR)
        cv2.imwrite(f'{OUT}/{tag}_lv{name}.png', bgr_e)
        panels.append(bgr_e); labels.append(f'ZDE++ {name}')
        entry['levels'][name] = {**metrics(cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)),
                                 'curve_range': [round(float(c.min()), 4), round(float(c.max()), 4)]}
    h = min(p.shape[0] for p in panels)
    panels = [cv2.resize(p, (int(p.shape[1] * h / p.shape[0]), h)) for p in panels]
    strip = np.hstack(panels)
    for i, t in enumerate(labels):
        x0 = sum(p.shape[1] for p in panels[:i]) + 8
        cv2.putText(strip, t, (x0, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (0, 255, 0), 2)
    cv2.imwrite(f'{OUT}/{tag}_levels_compare.png', strip)
    report[tag] = entry
    print(f'{tag}: {OUT}/{tag}_levels_compare.png ({strip.shape[1]}x{strip.shape[0]})')
json.dump(report, open(f'{OUT}/stats_levels.json', 'w'), indent=2, ensure_ascii=False)

for tag in report:
    e = report[tag]; o = e['original']
    print(f"\n== {tag} @ {e['apply_domain']}")
    print("  %-14s %6s %5s %5s %5s %7s %8s %9s" % ('配置', 'mean', 'p50', 'p90', 'p99', '暗部%', '细节lap', '低频std'))
    def row(n, m):
        print("  %-14s %6.1f %5.0f %5.0f %5.0f %6.1f%% %8.1f %9.2f" % (
            n, m['mean'], m['p50'], m['p90'], m['p99'], m['dark_lt30']*100, m['detail_lap'], m['low_freq_std']))
    row('原始', o)
    for name, _, _ in LV:
        row('ZDE++ ' + name, e['levels'][name])
