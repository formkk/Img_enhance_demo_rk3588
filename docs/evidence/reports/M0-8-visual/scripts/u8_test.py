"""② U8 曲线场 vs FP16 曲线场的图像域差异（离线合成量化，无需 C 实现）。
两种 U8 编码：E1 = 全域 [-1,1]（步长 2/255）；E2 = 观测域 [-0.5,0]（步长 0.5/255，含裁剪计数）。"""
import json, sys
import numpy as np
sys.path.insert(0, 'tools')
from enhance_ref import enhance

D = 'docs/evidence/reports/M0-2/'
x = np.load(D + 'input_480x272.npy').astype(np.float32) / 255.0
fp16c = np.load(D + 'curve_board_fp16_480x272.npy').astype(np.float32)
ref_img = enhance(x, fp16c)

def q_full(c):                      # E1：全域 [-1,1]
    q = np.clip(np.round((c + 1.0) / 2.0 * 255.0), 0, 255).astype(np.uint8)
    return q, (q.astype(np.float32) / 255.0) * 2.0 - 1.0

def q_tuned(c, lo=-0.5, hi=0.0):    # E2：观测域 [lo,hi]
    q = np.clip(np.round((c - lo) / (hi - lo) * 255.0), 0, 255).astype(np.uint8)
    return q, q.astype(np.float32) / 255.0 * (hi - lo) + lo

out = {'curve_field_fp16_range': [float(fp16c.min()), float(fp16c.max())], 'encodings': {}}
for name, fn, step in [('E1_fullrange_-1_1', q_full, 2.0/255.0), ('E2_tuned_-0.5_0', q_tuned, 0.5/255.0)]:
    q, dec = fn(fp16c)
    clipped = int(((fp16c < (-0.5 if 'tuned' in name else -1.0)) | (fp16c > (0.0 if 'tuned' in name else 1.0))).sum())
    img = enhance(x, dec)
    d = np.abs(ref_img - img)
    out['encodings'][name] = {
        'quant_step': step, 'clipped_values': clipped,
        'curve_max_abs_vs_fp16': float(np.abs(dec - fp16c).max()),
        'curve_mean_abs_vs_fp16': float(np.abs(dec - fp16c).mean()),
        'image_psnr_db': float(10*np.log10(1.0/max(np.mean((ref_img-img)**2), 1e-20))),
        'image_max_abs_lsb': float(d.max()*255), 'image_mean_abs_lsb': float(d.mean()*255),
        'gt_1lsb_ratio': float((d > 1/255).mean()), 'gt_2lsb_ratio': float((d > 2/255).mean()),
    }
json.dump(out, open('docs/evidence/reports/M0-2/u8_field_gate.json','w'), indent=2, ensure_ascii=False)
for k, v in out['encodings'].items():
    print('%-20s 步长=%.5f 裁剪=%d | 曲线max=%.2e | 图像PSNR=%.2f dB 最大像素差=%.2f LSB >1LSB=%.2f%% >2LSB=%.2f%%' % (
        k, v['quant_step'], v['clipped_values'], v['curve_max_abs_vs_fp16'], v['image_psnr_db'],
        v['image_max_abs_lsb'], v['gt_1lsb_ratio']*100, v['gt_2lsb_ratio']*100))
print('FP16 场范围 %.4f ~ %.4f' % tuple(out['curve_field_fp16_range']))
