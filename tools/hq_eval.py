#!/usr/bin/env python3
"""HQ（Retinexformer）评估工具：上游权重选型（`weights`）与 R9 机械闸口对照（`gate`）。

契约来源：
  - §4.1 模型选型（HQ 权重域：M0 以代用数据评估，M1 实拍集复检）
  - §9.3 R9 重影机械闸口（M0-4 对 HQ 三档逐一复测 vs PT FP32，row/col_diff 方法）
  - §11.2 M0-①b 板上输出 vs PyTorch 的**按精度档阈值**（FP16 张量 ≤1e-3 + 图像域 PSNR ≥70 dB）

两个子命令：
  weights  在代用数据（LOLv2）子集上比较多个上游权重（PT FP32，CPU），给出 PSNR/SSIM 与 no-op 基线
  prep-input  按 §3.1 `hq_shape_ladder` 生成三档固定闸口输入（16:9 窗裁切 + 等比缩放 + 主档上下零 pad）
  gate     PT FP32 输出 vs 板上 RKNN FP16 输出：张量差、图像域 PSNR、**row/col_diff 结构指标**、
           以及"仅 FP16 舍入"的对照基线（把 PT 输出投到 float16 再比），用于区分
           精度效应与编译器算子级的空间伪影（重影/条纹）

用法：
  python3 tools/hq_eval.py weights --weights a.pth b.pth \
      --data refs/pre-research/lolv2/extracted/LOLv2/Real_captured/Test \
      --limit 20 --h 288 --w 512 --out report.json
  python3 tools/hq_eval.py prep-input --src <frame.png> --crop 1080x608 \
      --shape 960x544:2 --shape 768x432:0 --shape 512x288:0 --out-dir dir/ --out manifest.json
  python3 tools/hq_eval.py gate --pt-ref pt.npy --cand board.f32 --cand-layout nchw \
      --shape 1,3,544,960 --tag 960x544 --out-dir docs/evidence/reports/M0-4-visual --out report.json
"""

import argparse
import glob
import importlib.util
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from enhance_ref import image_metrics   # 图像域指标统一定义（LSB / PSNR 口径一致）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HQ_ARCH_DEFAULT = os.path.join(ROOT, 'refs', 'pre-research', 'basicsr',
                               'models', 'archs', 'RetinexFormer_arch.py')


def load_arch(arch_path):
    spec = importlib.util.spec_from_file_location('RetinexFormer_arch', arch_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_weights(weights_path):
    import torch
    sd = torch.load(weights_path, map_location='cpu')
    if isinstance(sd, dict):
        for key in ('params', 'state_dict'):
            if isinstance(sd.get(key), dict):
                sd = sd[key]
                break
    return {k.replace('module.', ''): v for k, v in sd.items()}


def ssim_gray(a, b):
    """标准 11×11 高斯窗 SSIM（灰度，值域 [0,1]）。"""
    import cv2
    C1, C2 = 0.01 ** 2, 0.03 ** 2
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    mu_a = cv2.GaussianBlur(a, (11, 11), 1.5)
    mu_b = cv2.GaussianBlur(b, (11, 11), 1.5)
    saa = cv2.GaussianBlur(a * a, (11, 11), 1.5) - mu_a * mu_a
    sbb = cv2.GaussianBlur(b * b, (11, 11), 1.5) - mu_b * mu_b
    sab = cv2.GaussianBlur(a * b, (11, 11), 1.5) - mu_a * mu_b
    s = ((2 * mu_a * mu_b + C1) * (2 * sab + C2)) / ((mu_a ** 2 + mu_b ** 2 + C1) * (saa + sbb + C2))
    return float(s.mean())


def psnr_gray(a, b):
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return float('inf') if mse == 0 else float(10.0 * np.log10(1.0 / mse))


def row_col_diff(ref, cand):
    """R9 用结构指标：逐行 / 逐列的**平均绝对差**剖面（§9.3 row/col_diff 方法）。

    重影与条纹 = 空间局部化的误差 ⇒ 表现为主峰远高于剖面中位（structure factor 大），
    而均匀的精度噪声只抬高整体基线、不抬高 structure factor。
    """
    d = np.abs(ref.astype(np.float32) - cand.astype(np.float32))     # [C,H,W]
    row = d.mean(axis=(0, 2))
    col = d.mean(axis=(1, 2))

    def prof(v):
        med = float(np.median(v))
        return {
            'max': float(v.max()),
            'median': med,
            'p99': float(np.percentile(v, 99)),
            'argmax': int(np.argmax(v)),
            'structure_factor': float(v.max() / med) if med > 0 else float('inf'),
        }

    return {'row_diff': prof(row), 'col_diff': prof(col)}


def _cv2_or_none():
    """可视化用的 cv2 惰性获取：缺失时返回 None，由调用方**优雅跳过**。

    **理由**：`gate` 的**判定依据是数值**（张量 max / PSNR），与绘图库无关；
    cv2 只用于输出 diff/预览 PNG。缺 cv2 时不应让整条判定链失败。
    """
    try:
        import cv2
        return cv2
    except ImportError:
        return None


def diff_map_png(path, diff, gain=20.0):
    cv2 = _cv2_or_none()
    if cv2 is None:
        print(f'[warn] 缺 cv2，跳过 diff 可视化：{path}')
        return
    d = np.clip(diff * gain, 0.0, 1.0)
    rgb = (np.transpose(d, (1, 2, 0)) * 255.0).astype(np.uint8)
    cv2.imwrite(path, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))


def save_rgb_png(path, img_chw):
    cv2 = _cv2_or_none()
    if cv2 is None:
        print(f'[warn] 缺 cv2，跳过预览可视化：{path}')
        return
    rgb = (np.clip(np.transpose(img_chw, (1, 2, 0)), 0.0, 1.0) * 255.0).astype(np.uint8)
    cv2.imwrite(path, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))


def cmd_weights(args):
    import cv2
    import torch

    mod = load_arch(args.arch_path)
    lows = sorted(glob.glob(os.path.join(args.data, 'Low', '*.png')))
    assert lows, f'找不到 {args.data}/Low/*.png'
    if args.limit:
        lows = lows[:args.limit]

    pairs = []
    for lp in lows:
        gt = os.path.join(os.path.dirname(os.path.dirname(lp)), 'Normal', os.path.basename(lp))
        if not os.path.exists(gt):
            continue
        lo = cv2.cvtColor(cv2.resize(cv2.imread(lp), (args.w, args.h), interpolation=cv2.INTER_AREA),
                          cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        no = cv2.cvtColor(cv2.resize(cv2.imread(gt), (args.w, args.h), interpolation=cv2.INTER_AREA),
                          cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        pairs.append((os.path.basename(lp), lo, no))

    report = {
        'mode': 'weights',
        'data': os.path.abspath(args.data),
        'shape': [args.h, args.w],
        'n_pairs': len(pairs),
        'pairs': [p[0] for p in pairs],
        'interpolation': 'INTER_AREA',
        'metric_domain': '[0,1] RGB；PSNR 全通道，SSIM 灰度 11x11 高斯',
        'noop_baseline': {
            'psnr_db': float(np.mean([psnr_gray(p[1], p[2]) for p in pairs])),
            'psnr_db_std': float(np.std([psnr_gray(p[1], p[2]) for p in pairs])),
        },
        'weights': {},
    }
    for wp in args.weights:
        model = mod.RetinexFormer(in_channels=3, out_channels=3, n_feat=args.n_feat,
                                  stage=args.stage, num_blocks=list(args.num_blocks))
        missing, unexpected = model.load_state_dict(load_weights(wp), strict=False)
        model.eval()
        psnrs, ssims, per_image = [], [], {}
        with torch.no_grad():
            for name, lo, no in pairs:
                out = model(torch.from_numpy(lo).permute(2, 0, 1)[None]).clamp(0, 1)[0].permute(1, 2, 0).numpy()
                p = psnr_gray(out, no)
                s = ssim_gray(out, no)
                psnrs.append(p)
                ssims.append(s)
                per_image[name] = {'psnr_db': p, 'ssim': s}
        report['weights'][os.path.basename(wp)] = {
            'path': os.path.abspath(wp),
            'n_missing_keys': len(missing),
            'n_unexpected_keys': len(unexpected),
            'psnr_db_mean': float(np.mean(psnrs)),
            'psnr_db_std': float(np.std(psnrs)),
            'ssim_mean': float(np.mean(ssims)),
            'per_image': per_image,
        }
        print(f"{os.path.basename(wp)}: PSNR {np.mean(psnrs):.2f} dB  SSIM {np.mean(ssims):.4f}  "
              f"(missing={len(missing)} unexpected={len(unexpected)})")
    print(f"no-op 基线: PSNR {report['noop_baseline']['psnr_db']:.2f} dB")
    return report


def load_tensor(path, shape, layout):
    a = np.load(path) if path.endswith('.npy') else np.fromfile(path, dtype=np.float32)
    a = np.asarray(a, dtype=np.float32).reshape(shape)
    if layout == 'nhwc':
        a = np.transpose(a, (0, 3, 1, 2))
    elif layout == 'auto':
        # [1,3,H,W] 与 [1,H,W,3] 靠第 2 维是否为 3 判定
        a = np.transpose(a, (0, 3, 1, 2)) if a.shape[1] != 3 else a
    return a


def cmd_prep_input(args):
    """按 §3.1 的 hq_shape_ladder 生成固定输入：等比缩到有效区 → 主档上下零 pad（pad 行事后丢弃）。"""
    import cv2
    src = cv2.imread(args.src)
    assert src is not None, f'读不到 {args.src}'
    src_h, src_w = src.shape[:2]
    if args.crop:
        cw, ch = (int(v) for v in args.crop.split('x'))
        y0 = (src_h - ch) // 2
        x0 = (src_w - cw) // 2
        src = src[y0:y0 + ch, x0:x0 + cw]
    manifest = {
        'mode': 'prep-input',
        'src': os.path.abspath(args.src),
        'src_shape_hw': [src_h, src_w],
        'crop_wh': args.crop,
        'interpolation': 'INTER_AREA',
        'pad_fill': 'zero（全黑）',
        'shapes': {},
    }
    os.makedirs(args.out_dir, exist_ok=True)
    for spec in args.shape:
        wh, pad = spec.split(':')
        w, h = (int(v) for v in wh.split('x'))
        pad = int(pad)
        content = cv2.resize(src, (w, h - 2 * pad), interpolation=cv2.INTER_AREA)
        if pad:
            frame = np.zeros((h, w, 3), dtype=np.uint8)
            frame[pad:pad + h - 2 * pad] = content
        else:
            frame = content
        out = os.path.join(args.out_dir, f'gate_input_{w}x{h}.png')
        cv2.imwrite(out, frame)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32)
        manifest['shapes'][f'{w}x{h}'] = {
            'png': os.path.abspath(out),
            'pad_rows_each_side': pad,
            'content_wh': [w, h - 2 * pad],
            'rgb_mean_255': float(rgb.mean()),
            'rgb_std_255': float(rgb.std()),
            'gray_mean_255': float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()),
        }
        print(f'{w}x{h}: pad={pad} content={w}x{h - 2 * pad} mean={rgb.mean():.2f} → {out}')
    return manifest


def cmd_gate(args):
    shape = tuple(int(v) for v in args.shape.split(','))
    ref = load_tensor(args.pt_ref, shape, 'auto').astype(np.float32)
    out_dir = args.out_dir or '.'
    os.makedirs(out_dir, exist_ok=True)

    report = {
        'mode': 'gate',
        'tag': args.tag,
        'pt_ref': os.path.abspath(args.pt_ref),
        'shape': list(shape),
        'candidates': {},
    }
    # 对照基线：把 PT 输出投到 float16 再回来 —— 纯精度效应（不含任何编译器空间伪影）
    control = ref.astype(np.float16).astype(np.float32)
    report['fp16_round_control'] = {
        'tensor_max_abs': float(np.abs(ref - control).max()),
        'image': image_metrics(np.clip(ref, 0, 1), np.clip(control, 0, 1)),
        **row_col_diff(ref, control),
    }
    # FP16 档阈值按「模型族」取（v3.55 裁定 O35）：ZDE++ 族与 HQ/Retinexformer 族各一套。
    # 阈值来源 = 主基准 §11.2 M0-①b；本工具**不重定义阈值**，只按族选用。
    family = getattr(args, 'family', 'zde')
    if family == 'hq':
        thresholds = {'tensor_max_abs_fp16': 1.5e-2, 'image_psnr_db_fp16': 64.0}
    else:
        thresholds = {'tensor_max_abs_fp16': 1e-3, 'image_psnr_db_fp16': 70.0}
    report['family'] = family
    report['thresholds'] = thresholds

    for spec in args.cand:
        name, path = spec.split('=', 1)
        cand = load_tensor(path, shape, args.cand_layout).astype(np.float32)
        entry = {
            'path': os.path.abspath(path),
            'tensor_max_abs': float(np.abs(ref - cand).max()),
            'tensor_mean_abs': float(np.abs(ref - cand).mean()),
            'image': image_metrics(np.clip(ref, 0, 1), np.clip(cand, 0, 1)),
            **row_col_diff(ref, cand),
        }
        # np.float64 与阈值比较会返回 np.bool_，不可 JSON 序列化 ⇒ 显式转 Python bool
        entry['pass_tensor'] = bool(entry['tensor_max_abs'] <= thresholds['tensor_max_abs_fp16'])
        entry['pass_image'] = bool(entry['image']['psnr_db'] >= thresholds['image_psnr_db_fp16'])
        s = args.tag.replace('x', 'x')
        save_rgb_png(os.path.join(out_dir, f'{s}_{name}_out.png'), cand[0])
        diff_map_png(os.path.join(out_dir, f'{s}_{name}_diff20x.png'), np.abs(ref[0] - cand[0]), args.diff_gain)
        report['candidates'][name] = entry
        print(f"{name}: tensor_max_abs {entry['tensor_max_abs']:.3e} (pass={entry['pass_tensor']})  "
              f"PSNR {entry['image']['psnr_db']:.2f} dB (pass={entry['pass_image']})  "
              f"row_sf {entry['row_diff']['structure_factor']:.2f} col_sf {entry['col_diff']['structure_factor']:.2f}")
    save_rgb_png(os.path.join(out_dir, f'{args.tag}_pt_ref.png'), ref[0])
    save_rgb_png(os.path.join(out_dir, f'{args.tag}_fp16_control.png'), control[0])
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='mode', required=True)

    pw = sub.add_parser('weights')
    pw.add_argument('--weights', nargs='+', required=True)
    pw.add_argument('--data', required=True, help='LOLv2 子集目录（含 Low/ 与 Normal/）')
    pw.add_argument('--limit', type=int, default=20)
    pw.add_argument('--h', type=int, default=288)
    pw.add_argument('--w', type=int, default=512)
    pw.add_argument('--arch-path', default=HQ_ARCH_DEFAULT)
    pw.add_argument('--n-feat', type=int, default=40)
    pw.add_argument('--stage', type=int, default=1)
    pw.add_argument('--num-blocks', type=int, nargs=3, default=[1, 2, 2])
    pw.add_argument('--out', default='')

    pp = sub.add_parser('prep-input')
    pp.add_argument('--src', required=True)
    pp.add_argument('--crop', default='', help='如 1080x608（16:9 采集窗），缺省不裁')
    pp.add_argument('--shape', action='append', required=True, metavar='WxH:pad',
                    help='如 960x544:2（2 行零 pad 在上下各 2 行）、768x432:0、512x288:0')
    pp.add_argument('--out-dir', required=True)
    pp.add_argument('--out', default='')

    pg = sub.add_parser('gate')
    pg.add_argument('--pt-ref', required=True, help='PT FP32 输出 .npy（[1,3,H,W]，[0,1]）')
    pg.add_argument('--cand', action='append', default=[], metavar='名称=路径', required=True)
    pg.add_argument('--cand-layout', choices=['auto', 'nchw', 'nhwc'], default='auto')
    pg.add_argument('--shape', required=True, help='如 1,3,544,960')
    pg.add_argument('--tag', required=True)
    pg.add_argument('--out-dir', default='')
    pg.add_argument('--diff-gain', type=float, default=20.0)
    pg.add_argument('--out', default='')
    # 模型族（v3.55 裁定 O35）：FP16 档的阈按「模型族 × 精度档」细分
    #   zde = ZDE++ 族（张量 ≤1e-3 且图像域 PSNR ≥70 dB）
    #   hq  = HQ / Retinexformer 族（张量 ≤1.5e-2 且图像域 PSNR ≥64 dB）
    # 默认 `zde` 以保持既有行为不变（既有报告均为 ZDE++ 族口径）。
    pg.add_argument('--family', choices=['zde', 'hq'], default='zde',
                    help='FP16 档的阈值族（O35）：zde=ZDE++（≤1e-3/≥70dB）；hq=Retinexformer（≤1.5e-2/≥64dB）')

    args = p.parse_args()
    if args.mode == 'weights':
        report = cmd_weights(args)
    elif args.mode == 'prep-input':
        report = cmd_prep_input(args)
    else:
        report = cmd_gate(args)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, 'w') as f:
            f.write(text + '\n')
        print(f'[report] {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())