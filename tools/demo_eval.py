#!/usr/bin/env python3
"""演示质量人工评估素材生成器（06.CR2 → 1080p 源/目标 + 曲线估计速度）。

目的：以 06.CR2 为测试图，按**现方案**（§3.1 G1 几何 + 480×272 曲线估计 + 8 iter 单遍融合在
**1920×1080 存盘域**应用）产出一对 **真 1080p** 的「源 / 目标」图，供人工判断输出分辨率是否
真正落到 1080p（而非低分辨率增强后放大），并统计各段速度。

两段式（中间夹板上推理，故拆成两个子命令）：

  # ① 本机（需 cv2 + rawpy，用 ~/.conda/envs/rknn232/bin/python）：解 CR2 → 1080p 源图 + 估计输入
  python3 tools/demo_eval.py prep --out docs/evidence/reports/M0-demo-quality

  # ② 板端（在板上执行，产物 scp 回本机）：
  #   INT8 双条带（现方案主路径）：rknn_split_probe <切片模型> est_in_480x272_u8.raw 272 480 40 2 0,1 \
  #                                        float <板上输出前缀>
  #   FP16 单模型（对照）：       rknn_c_probe <480x272_fp16 模型> est_in_480x272_nhwc.f32 40 nhwc float \
  #                                        nosram f32 noperf <输出.f32>

  # ③ 本机：曲线场取有效区 → 上采样到 1920×1080 → 8 iter 应用 → 1080p 目标图
  python3 tools/demo_eval.py render --out docs/evidence/reports/M0-demo-quality \
      --curve int8_split2=<stitched.f32> [--curve fp16=<dump.f32>] [--apply-json <probe.json>]

纪律说明：本脚本产出的 `laplacian_var` 等属**画质类指标**，按 O31（镜头定型）挂起，仅作人工评估
素材，**不作验收依据**；应用侧耗时是 numpy **参考实现**（`tools/enhance_ref.py`），正式达标值须由
M0-13 的 C/NEON native 实现给出（§11.1-13）。
"""

import argparse
import hashlib
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from enhance_ref import enhance  # noqa: E402

CR2_DEFAULT = 'refs/pre-research/06.CR2'
EFF_W, EFF_H = 480, 270          # §3.1 G1 有效区
EST_W, EST_H = 480, 272          # 上/下各 1 行零 pad 后的估计输入
OUT_W, OUT_H = 1920, 1080        # §3.1 存盘域（真 1080p）


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def decode_cr2(path):
    """口径同预研 §12.9.4：no_auto_bright=True → 未增强的原始暗帧。"""
    import rawpy
    with rawpy.imread(path) as raw:
        rgb16 = raw.postprocess(no_auto_bright=True, output_bps=16)
    return (rgb16 / 257.0).round().astype(np.uint8)     # 16bit→8bit 线性映射


def gray_stats(g):
    import cv2
    return {
        'mean': round(float(g.mean()), 2),
        'p50': float(np.percentile(g, 50)),
        'p90': float(np.percentile(g, 90)),
        'p99': float(np.percentile(g, 99)),
        'dark_ratio_lt30': round(float((g < 30).mean()), 4),
        'laplacian_var': round(float(cv2.Laplacian(g, cv2.CV_64F).var()), 2),
    }


def do_prep(a):
    import cv2
    os.makedirs(a.out, exist_ok=True)
    src = decode_cr2(a.cr2)                                   # RGB uint8, 原生 5496×3670
    src1080 = cv2.resize(src, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
    cv2.imwrite(os.path.join(a.out, 'src_1080p.png'),
                cv2.cvtColor(src1080, cv2.COLOR_RGB2BGR))

    # §3.1 G1：1080p → 等比 480×270 → 上下各 1 行零 pad → 480×272
    small = cv2.resize(src1080, (EFF_W, EFF_H), interpolation=cv2.INTER_AREA)
    est = np.zeros((EST_H, EST_W, 3), dtype=np.uint8)
    est[1:1 + EFF_H] = small
    est.tofile(os.path.join(a.out, 'est_in_480x272_u8.raw'))              # NHWC uint8（切片探针）
    np.ascontiguousarray(est.astype(np.float32)).tofile(
        os.path.join(a.out, 'est_in_480x272_nhwc.f32'))                    # NHWC float32 值域 [0,255]

    x = src1080.astype(np.float32).transpose(2, 0, 1)[None] / 255.0        # [1,3,1080,1920]
    np.save(os.path.join(a.out, 'frame_1080p_f32.npy'), x)

    rep = {
        'cr2': os.path.abspath(a.cr2), 'cr2_sha256': sha256(a.cr2),
        'cr2_native': list(src.shape), 'src_1080p': [OUT_H, OUT_W],
        'geom': {'eff': [EFF_H, EFF_W], 'est': [EST_H, EST_W], 'pad_rows': [1, 1],
                 'apply_domain': [OUT_H, OUT_W], 'upsample': 'bilinear 4x/4x'},
        'src_stats': gray_stats(cv2.cvtColor(src1080, cv2.COLOR_RGB2GRAY)),
        'est_input_stats': {'mean': round(float(est.mean()), 2),
                            'pad_row_top_nonzero': int(np.count_nonzero(est[0])),
                            'pad_row_bot_nonzero': int(np.count_nonzero(est[-1]))},
        'files': {},
    }
    for f in ('src_1080p.png', 'est_in_480x272_u8.raw', 'est_in_480x272_nhwc.f32', 'frame_1080p_f32.npy'):
        p = os.path.join(a.out, f)
        rep['files'][f] = {'bytes': os.path.getsize(p), 'sha256': sha256(p)[:16]}
    with open(os.path.join(a.out, 'prep.json'), 'w') as fh:
        json.dump(rep, fh, indent=2, ensure_ascii=False)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    print('[prep] 板上送：est_in_480x272_u8.raw（切片探针）/ est_in_480x272_nhwc.f32（单模型探针）')
    return 0


def do_render(a):
    import cv2
    x = np.load(os.path.join(a.out, 'frame_1080p_f32.npy'))                # [1,3,1080,1920]
    src_bgr = cv2.cvtColor((x[0].transpose(1, 2, 0) * 255).round().astype(np.uint8),
                           cv2.COLOR_RGB2BGR)
    rep = {'apply_domain': [OUT_H, OUT_W], 'iters': 8, 'candidates': {}}
    if a.apply_json:
        rep['probe'] = json.load(open(a.apply_json))

    panels = [src_bgr]
    for spec in a.curve:
        if '=' not in spec:
            raise SystemExit(f'--curve 需为 名称=路径，收到 {spec}')
        name, path = spec.split('=', 1)
        c = np.fromfile(path, dtype=np.float32).reshape(3, EST_H, EST_W)    # NCHW 3×272×480
        c_eff = c[:, 1:1 + EFF_H, :]                                        # 只取有效区（裁掉 pad 行）
        c_up = np.stack([cv2.resize(c_eff[i], (OUT_W, OUT_H), interpolation=cv2.INTER_LINEAR)
                         for i in range(3)])[None]

        t = []
        for _ in range(3):                                                 # 参考实现耗时（非达标依据）
            t0 = time.perf_counter()
            y = enhance(x, c_up)
            t.append(time.perf_counter() - t0)
        out = (y[0].transpose(1, 2, 0) * 255.0).round().astype(np.uint8)
        out_bgr = cv2.cvtColor(out, cv2.COLOR_RGB2BGR)
        cv2.imwrite(os.path.join(a.out, f'tgt_1080p_{name}.png'), out_bgr)
        panels.append(out_bgr)
        rep['candidates'][name] = {
            'curve_src': os.path.abspath(path),
            'curve_eff_range': [round(float(c_eff.min()), 4), round(float(c_eff.max()), 4)],
            'apply_ms_ref_median': round(sorted(t)[1] * 1000.0, 1),
            'stats': gray_stats(cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)),
        }
        print(f'[{name}] tgt_1080p_{name}.png  apply(ref) {sorted(t)[1]*1000:.1f} ms')

    rep['src_stats'] = gray_stats(cv2.cvtColor(src_bgr, cv2.COLOR_BGR2GRAY))
    # 并排（各缩到 960 宽，便于整屏查看；全分辨率单图已分别落盘）
    sc = 0.5
    half = [cv2.resize(p, (int(p.shape[1] * sc), int(p.shape[0] * sc)), interpolation=cv2.INTER_AREA)
            for p in panels]
    strip = np.hstack(half)
    labels = ['SRC(1080p)'] + [s.split('=')[0] for s in a.curve]
    x0 = 0
    for i, lab in enumerate(labels):
        if i:
            cv2.line(strip, (x0, 0), (x0, strip.shape[0]), (0, 255, 0), 2)
        cv2.putText(strip, lab, (x0 + 10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
        x0 += half[i].shape[1]
    cv2.imwrite(os.path.join(a.out, 'compare_1080p.png'), strip)
    print(f'[compare] {os.path.join(a.out, "compare_1080p.png")} ({strip.shape[1]}x{strip.shape[0]})')

    with open(os.path.join(a.out, 'render.json'), 'w') as fh:
        json.dump(rep, fh, indent=2, ensure_ascii=False)
    print(json.dumps(rep, indent=2, ensure_ascii=False)[:1800])
    return 0


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd', required=True)

    pp = sub.add_parser('prep', help='解 CR2 → 1080p 源图 + 估计输入')
    pp.add_argument('--cr2', default=CR2_DEFAULT)
    pp.add_argument('--out', required=True)
    pp.set_defaults(func=do_prep)

    pr = sub.add_parser('render', help='曲线场 → 1080p 目标图 + 统计')
    pr.add_argument('--out', required=True)
    pr.add_argument('--curve', action='append', default=[], metavar='名称=路径')
    pr.add_argument('--apply-json', default='', help='板上探针 JSON（速度随行）')
    pr.set_defaults(func=do_render)

    a = p.parse_args()
    return a.func(a)


if __name__ == '__main__':
    sys.exit(main())