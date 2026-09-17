#!/usr/bin/env python3
"""Zero-DCE++ 转换工具：PyTorch → ONNX → RKNN。

契约来源（主基准 §10.2「模型转换流程」）：
  步骤 1  torch.onnx.export，opset 12，固定输入（禁动态 shape）
  步骤 2  onnxsim → 与 PyTorch 对齐（阈 <1e-4）
  步骤 3  rknn.config(target_platform='rk3588')
  步骤 4  rknn.build(do_quantization=False)   # FP16 为正式路线
导出范围：只导出 `CurveEstimator`（3ch `x_r` 曲线场）；8 次迭代在板端后处理（§4.1、§3.3）

输入归一化约定（与训练一致，随报告登记）：
  RGB 顺序、输入值域 [0,255] float32、mean=[0,0,0]、std=[255,255,255]、quant_img_RGB2BGR=False
  —— 等价于训练期 [0,1] 归一化，便于 RGA 输出 uint8 后直接喂入。

用法：
  # 导出 ONNX（在含 torch/onnx/onnxsim 的转换环境执行）
  python3 tools/convert_models.py export --weights models/zde/Epoch99.pth \
      --h 272 --w 480 --out models/zde/zde_curve_480x272.onnx [--report out.json]
  # ONNX → RKNN（在目标 toolkit2 版本的环境执行）
  python3 tools/convert_models.py build --onnx models/zde/zde_curve_480x272.onnx \
      --out models/zde/zde_curve_480x272_fp16.rknn [--quant fp16|int8 --calib calib.txt]
"""

import argparse
import hashlib
import json
import os
import platform
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TARGET_PLATFORM = 'rk3588'
MEAN = [[0, 0, 0]]
STD = [[255, 255, 255]]
OPSET = 12
ALIGN_TOL = 1e-4


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def write_report(report, path):
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if path:
        with open(path, 'w') as f:
            f.write(text + '\n')
        print(f'[report] {path}')


def do_export(args):
    import numpy as np
    import torch
    import onnx
    import onnxruntime as ort
    from onnxsim import simplify
    from zde_model import enhance_net_nopool, CurveEstimator

    base = enhance_net_nopool(scale_factor=1)
    base.load_state_dict(torch.load(args.weights, map_location='cpu'))
    base.eval()
    model = CurveEstimator(base).eval()

    dummy = torch.rand(1, 3, args.h, args.w)
    with torch.no_grad():
        _, x_r_full = base(dummy)
        x_r_est = model(dummy)
    diff_est = (x_r_full - x_r_est).abs().max().item()
    assert diff_est < 1e-6, f'CurveEstimator 与完整模型不一致：{diff_est}'

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    torch.onnx.export(
        model, dummy, args.out,
        input_names=['input'], output_names=['curve'],
        opset_version=OPSET, dynamic_axes={}, do_constant_folding=True,
    )
    onnx.save(simplify(onnx.load(args.out))[0], args.out)

    sess = ort.InferenceSession(args.out, providers=['CPUExecutionProvider'])
    ort_out = sess.run(None, {'input': dummy.numpy()})[0]
    diff_onnx = float(np.abs(x_r_est.numpy() - ort_out).max())

    if args.input_png:
        # 固定代用输入帧：RGB，缩放到目标档位后转 float32 [0,255]，与板端喂入约定一致
        import cv2
        img = cv2.imread(args.input_png)
        assert img is not None, f'读不到输入图 {args.input_png}'
        img = cv2.cvtColor(cv2.resize(img, (args.w, args.h)), cv2.COLOR_BGR2RGB)
        x_in = img.astype(np.float32).transpose(2, 0, 1)[None]   # HWC → [1,3,H,W]
        np.save(args.dump_input, x_in)
        with torch.no_grad():
            # 板端 .rknn 内的 mean/std 会把 [0,255] 归一化到 [0,1]，PT 侧须手动等价
            ref = model(torch.from_numpy(x_in) / 255.0).numpy()
        np.save(args.dump_pt_ref, ref)
        print(f'[input] {args.dump_input}  [pt_ref] {args.dump_pt_ref}')
        report_inputs = {
            'input_png': os.path.abspath(args.input_png),
            'input_png_sha256': sha256(args.input_png),
            'dump_input': os.path.abspath(args.dump_input),
            'dump_pt_ref': os.path.abspath(args.dump_pt_ref),
            'pt_ref_range': [float(ref.min()), float(ref.max())],
        }
    else:
        report_inputs = {}

    report = {
        'stage': 'export',
        'weights': os.path.abspath(args.weights),
        'weights_sha256': sha256(args.weights),
        'onnx': os.path.abspath(args.out),
        'onnx_sha256': sha256(args.out),
        'input_shape': [1, 3, args.h, args.w],
        'opset': OPSET,
        'onnx_normalization': {'rgb2bgr': False, 'mean': MEAN, 'std': STD},
        'pt_estimator_vs_full_max_abs': diff_est,
        'onnx_vs_pt_max_abs': diff_onnx,
        'align_pass': diff_onnx < ALIGN_TOL,
        'env': {
            'python': platform.python_version(),
            'torch': torch.__version__,
            'onnx': onnx.__version__,
            'onnxruntime': ort.__version__,
        },
    }
    report.update(report_inputs)
    write_report(report, args.report)
    return 0 if report['align_pass'] else 1


def do_build(args):
    import rknn
    from rknn.api import RKNN

    quant = 'fp16' if args.quant == 'fp16' else 'int8'
    rknn_ver = getattr(rknn, '__version__', 'unknown')

    r = RKNN(verbose=True)
    r.config(
        mean_values=MEAN,
        std_values=STD,
        target_platform=args.target,
        quant_img_RGB2BGR=False,
    )
    if r.load_onnx(model=args.onnx) != 0:
        raise SystemExit('load_onnx failed')
    if quant == 'fp16':
        ret = r.build(do_quantization=False, rknn_batch_size=1)
    else:
        if not args.calib:
            raise SystemExit('int8 需要 --calib')
        ret = r.build(do_quantization=True, dataset=args.calib, rknn_batch_size=1)
    if ret != 0:
        raise SystemExit('build failed')
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if r.export_rknn(args.out) != 0:
        raise SystemExit('export_rknn failed')
    r.release()

    report = {
        'stage': 'build',
        'onnx': os.path.abspath(args.onnx),
        'onnx_sha256': sha256(args.onnx),
        'rknn': os.path.abspath(args.out),
        'target_platform': args.target,
        'quant': quant,
        'input_normalization': {'rgb2bgr': False, 'mean': MEAN, 'std': STD},
        'env': {'python': platform.python_version(), 'rknn_toolkit2': rknn_ver},
    }
    write_report(report, args.report)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='stage', required=True)

    pe = sub.add_parser('export', help='PyTorch → ONNX（+ onnxsim + 对齐）')
    pe.add_argument('--weights', required=True)
    pe.add_argument('--out', required=True)
    pe.add_argument('--h', type=int, required=True)
    pe.add_argument('--w', type=int, required=True)
    pe.add_argument('--report', default='')
    pe.add_argument('--input-png', default='', help='固定代用输入帧；给定则落盘 .npy 输入与 PyTorch 参考输出')
    pe.add_argument('--dump-input', default='')
    pe.add_argument('--dump-pt-ref', default='')
    pe.set_defaults(func=do_export)

    pb = sub.add_parser('build', help='ONNX → RKNN')
    pb.add_argument('--onnx', required=True)
    pb.add_argument('--out', required=True)
    pb.add_argument('--quant', choices=['fp16', 'int8'], default='fp16')
    pb.add_argument('--calib', default='')
    pb.add_argument('--target', default=TARGET_PLATFORM)
    pb.add_argument('--report', default='')
    pb.set_defaults(func=do_build)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()