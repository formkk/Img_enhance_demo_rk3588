#!/usr/bin/env python3
"""8 次迭代曲线应用的参考实现 + 图像域精度闸口指标（M0-8 / O23 用）。

数学取自官方 Zero-DCE++（`enhance_net_nopool.enhance`）：`y ← y + x_r·(y² − y)` 连续 8 次，
末端一次性裁剪到 [0,1]。契约与单遍融合要求见主基准 §3.3（本文件是**参考实现**，非交付运行时）。

用途：把不同来源的曲线场（PyTorch 真值 / 板上 FP16 / 板上 INT8 / 切分拼接）分别应用同一帧，
给出**图像域**指标（PSNR、最大与平均像素差、越界像素占比），作为"模型量化是否可进主线"的判据。

用法：
  python3 tools/enhance_ref.py --frame <npy> [0,255] float32 [1,3,H,W] RGB
      --ref-curve <npy|f32> 参考曲线场（3ch，范围 [-1,1]，NCHW 展平）
      --curve 名称=路径 [--curve 名称2=路径2 ...] [--out 报告.json]
"""

import argparse
import json
import os
import struct
import sys

import numpy as np


def load_curve(path, shape):
    """支持 .npy 与裸 float32；统一返回 [1,3,H,W]。"""
    if path.endswith('.npy'):
        a = np.load(path)
    else:
        a = np.fromfile(path, dtype=np.float32)
    a = np.asarray(a, dtype=np.float32)
    if a.size != int(np.prod(shape)):
        raise SystemExit(f'{path}: 元素数 {a.size} != 期望 {int(np.prod(shape))}')
    return a.reshape(shape)


def enhance(x, x_r, iters=8):
    """官方 8 次迭代（同一 x_r 复用 8 次），末端裁剪。x/x_r 与图像同分辨率。"""
    y = np.array(x, dtype=np.float32, copy=True)
    for _ in range(iters):
        y = y + x_r * (y * y - y)
    return np.clip(y, 0.0, 1.0)


def image_metrics(ref, cand):
    d = np.abs(ref - cand)
    mse = float(np.mean((ref - cand) ** 2))
    psnr = float('inf') if mse == 0 else 10.0 * np.log10(1.0 / mse)
    return {
        'psnr_db': psnr,
        'max_abs_lsb': float(d.max() * 255.0),          # 以 8 bit 为单位的最大像素差
        'mean_abs_lsb': float(d.mean() * 255.0),
        'gt_1lsb_ratio': float((d > 1.0 / 255).mean()),
        'gt_2lsb_ratio': float((d > 2.0 / 255).mean()),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--frame', required=True, help='uint8 值域 float32 的 [1,3,H,W] RGB 帧')
    p.add_argument('--ref-curve', required=True)
    p.add_argument('--curve', action='append', default=[], metavar='名称=路径')
    p.add_argument('--out', default='')
    args = p.parse_args()

    x = np.load(args.frame).astype(np.float32) / 255.0
    shape = x.shape
    ref_c = load_curve(args.ref_curve, shape)
    ref_img = enhance(x, ref_c)

    report = {
        'frame': os.path.abspath(args.frame),
        'shape': list(shape),
        'ref_curve': os.path.abspath(args.ref_curve),
        'ref_curve_range': [float(ref_c.min()), float(ref_c.max())],
        'candidates': {},
    }
    for spec in args.curve:
        if '=' not in spec:
            raise SystemExit(f'--curve 需为 名称=路径，收到 {spec}')
        name, path = spec.split('=', 1)
        c = load_curve(path, shape)
        cd = np.abs(c - ref_c)
        img = enhance(x, c)
        report['candidates'][name] = {
            'path': os.path.abspath(path),
            'curve_range': [float(c.min()), float(c.max())],
            'curve_max_abs': float(cd.max()),
            'curve_mean_abs': float(cd.mean()),
            'image': image_metrics(ref_img, img),
        }

    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        with open(args.out, 'w') as f:
            f.write(text + '\n')
        print(f'[report] {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())