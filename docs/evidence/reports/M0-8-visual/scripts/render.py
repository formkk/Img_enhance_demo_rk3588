"""曲线场上采样 → 8 次迭代应用（tools/enhance_ref.py 的参考实现）→ 并排对比 PNG + 统计。"""
import json, sys
import numpy as np, cv2
sys.path.insert(0, 'tools')
from enhance_ref import enhance

OUT = 'docs/evidence/reports/M0-8-visual'
CASES = [('18', '/tmp/vis/src_18.png', (1080, 720), 1.0),
         ('06cr2', '/tmp/vis/src_06cr2.png', (5496, 3670), 0.25)]   # CR2 太大，应用域取 1/4 便于查看
stats = {}
for tag, src_path, native, scale in CASES:
    bgr = cv2.imread(src_path)
    W, H = int(native[0] * scale), int(native[1] * scale)
    img = cv2.resize(bgr, (W, H), interpolation=cv2.INTER_AREA) if scale != 1.0 else bgr
    x = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    stats[tag] = {'apply_domain': f'{W}x{H}', 'scale_vs_native': scale}
    panels = [img]
    for prec in ('int8', 'fp16'):
        c = np.fromfile(f'/tmp/vis/curve_{tag}_{prec}.f32', dtype=np.float32).reshape(3, 272, 480)
        c_up = np.stack([cv2.resize(c[i], (W, H), interpolation=cv2.INTER_LINEAR) for i in range(3)])[None]
        y = enhance(x, c_up)                                   # 8 次迭代（参考实现）
        out = (y[0].transpose(1, 2, 0) * 255.0).round().astype(np.uint8)
        out_bgr = cv2.cvtColor(out, cv2.COLOR_RGB2BGR)
        cv2.imwrite(f'{OUT}/{tag}_enhanced_{prec}.png', out_bgr)
        panels.append(out_bgr)
        g = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)
        stats[tag][prec] = {
            'curve_range': [round(float(c.min()), 4), round(float(c.max()), 4)],
            'mean': round(float(g.mean()), 2),
            'p50': float(np.percentile(g, 50)), 'p90': float(np.percentile(g, 90)),
            'p99': float(np.percentile(g, 99)),
            'dark_ratio_lt30': round(float((g < 30).mean()), 4),
            'laplacian_var': round(float(cv2.Laplacian(g, cv2.CV_64F).var()), 2),
        }
    g0 = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    stats[tag]['original'] = {'mean': round(float(g0.mean()), 2), 'p50': float(np.percentile(g0, 50)),
                              'p90': float(np.percentile(g0, 90)), 'p99': float(np.percentile(g0, 99)),
                              'dark_ratio_lt30': round(float((g0 < 30).mean()), 4),
                              'laplacian_var': round(float(cv2.Laplacian(g0, cv2.CV_64F).var()), 2)}
    # 并排：原图 | INT8 | FP16（等比缩到同一高度便于查看）
    h = min(p.shape[0] for p in panels)
    panels = [cv2.resize(p, (int(p.shape[1] * h / p.shape[0]), h)) for p in panels]
    strip = np.hstack(panels)
    for i, t in enumerate(['原始', 'ZDE++ INT8', 'ZDE++ FP16']):
        x0 = sum(p.shape[1] for p in panels[:i]) + 8
        cv2.putText(strip, t, (x0, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
    cv2.imwrite(f'{OUT}/{tag}_compare.png', strip)
    print(f'{tag}: {OUT}/{tag}_compare.png  ({strip.shape[1]}x{strip.shape[0]})')
json.dump(stats, open(f'{OUT}/stats.json', 'w'), indent=2, ensure_ascii=False)
print(json.dumps(stats, indent=2, ensure_ascii=False)[:1400])
