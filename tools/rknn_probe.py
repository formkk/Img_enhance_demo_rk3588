#!/usr/bin/env python3
"""单模型 RKNN 探测：加载 / 输出 / 耗时 / 对齐。

用途（主基准 §11.1-2 测速、§11.2-① ONNX↔RKNN 对齐回归）：
  - 板上（默认 backend=lite，经 rknn_toolkit_lite2 → librknnrt）或 PC 模拟器（backend=sim）
  - 输出张量与参考 `.npy`（PyTorch 侧）逐元素比对；**阈值是参数**（`--tol`）——
    §11.2-① 按 v3.22 裁定 O27 拆两层：①a FP32 链路 <1e-4；①b 按精度档给阈
    （FP16 ≤1e-3 / INT8 ≤3e-2）。默认仍为 1e-4，不静默放宽。
  - 计时只报推理调用耗时中位数/分位数，不换算端到端（规则 10-evidence §2）

输入约定：`.npy` float32，shape [1,3,H,W]，值域 [0,255]，RGB（与 convert_models.py 的归一化一致）。

用法：
  python3 tools/rknn_probe.py --rknn m.rknn --input in.npy --ref pt.npy --runs 20
      [--tol 1e-3] [--dump board.npy] [--report r.json] [--backend lite|sim]
      [--librknnrt /usr/lib/librknnrt.so]
"""

import argparse
import json
import os
import re
import statistics
import sys
import time

ALIGN_TOL = 1e-4


def librknnrt_version(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        blob = f.read()
    m = re.search(rb'librknnrt version: ([^\x00\n]+)', blob)
    return m.group(1).decode('utf-8', 'replace').strip() if m else None


def loaded_librknnrt():
    """进程实际映射的 librknnrt（证明运行时用的是哪一个 .so）。"""
    found = []
    try:
        with open('/proc/self/maps') as f:
            for line in f:
                if 'librknnrt' in line:
                    p = line.rstrip().split()[-1]
                    if p not in found:
                        found.append(p)
    except OSError:
        pass
    out = []
    for p in found:
        try:
            size = os.path.getsize(p)
        except OSError:
            size = None
        out.append({'path': p, 'size_bytes': size, 'version': librknnrt_version(p)})
    return out


def stats(values):
    values = sorted(values)
    n = len(values)
    return {
        'n': n,
        'min_ms': values[0],
        'median_ms': statistics.median(values),
        'p90_ms': values[min(n - 1, int(0.9 * n))],
        'max_ms': values[-1],
    }


def run_lite(rknn_path, x, runs):
    from rknnlite.api import RKNNLite
    r = RKNNLite()
    if r.load_rknn(rknn_path) != 0:
        raise SystemExit('load_rknn failed')
    if r.init_runtime() != 0:
        raise SystemExit('init_runtime failed')
    try:
        first = r.inference(inputs=[x])[0]
        times = []
        for _ in range(runs):
            t0 = time.perf_counter()
            out = r.inference(inputs=[x])[0]
            times.append((time.perf_counter() - t0) * 1000.0)
        loaded = loaded_librknnrt()   # release() 前采集，证明实际加载的运行时
    finally:
        r.release()
    return first, out, times, loaded


def run_sim(rknn_path, x, runs):
    from rknn.api import RKNN
    r = RKNN(verbose=False)
    if r.load_rknn(rknn_path) != 0:
        raise SystemExit('load_rknn failed')
    if r.init_runtime() != 0:
        raise SystemExit('init_runtime failed')
    try:
        first = r.inference(inputs=[x])[0]
        times = []
        for _ in range(runs):
            t0 = time.perf_counter()
            out = r.inference(inputs=[x])[0]
            times.append((time.perf_counter() - t0) * 1000.0)
        loaded = loaded_librknnrt()
    finally:
        r.release()
    return first, out, times, loaded


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--rknn', required=True)
    p.add_argument('--input', required=True, help='输入 .npy，float32 [1,3,H,W]，[0,255] RGB')
    p.add_argument('--ref', default='', help='参考输出 .npy（PyTorch 侧），用于对齐判定')
    p.add_argument('--tol', type=float, default=ALIGN_TOL,
                   help='对齐阈值（默认 1e-4 = v3.22 裁定 O27 的 ①a FP32 链路口径；'
                        '①b 按精度档取：FP16 1e-3 / INT8 3e-2）')
    p.add_argument('--dump', default='', help='落盘本次输出 .npy')
    p.add_argument('--runs', type=int, default=20)
    p.add_argument('--report', default='')
    p.add_argument('--backend', choices=['lite', 'sim'], default='lite')
    p.add_argument('--librknnrt', default='/usr/lib/librknnrt.so')
    p.add_argument('--input-layout', choices=['nhwc', 'nchw'], default='nhwc',
                   help='板端 rknnlite 约定 NHWC；输入 .npy 为 CHW 时自动转置')
    args = p.parse_args()

    import numpy as np
    x = np.load(args.input)
    if args.input_layout == 'nhwc' and x.ndim == 4 and x.shape[1] == 3:
        x = np.ascontiguousarray(x.transpose(0, 2, 3, 1))

    t_load0 = time.perf_counter()
    runner = run_lite if args.backend == 'lite' else run_sim
    first, out, times, loaded = runner(args.rknn, x, args.runs)
    load_total_ms = (time.perf_counter() - t_load0) * 1000.0 - sum(times)

    report = {
        'rknn': os.path.abspath(args.rknn),
        'rknn_size_bytes': os.path.getsize(args.rknn),
        'backend': args.backend,
        'input': os.path.abspath(args.input),
        'fed_shape': list(x.shape),
        'output_shape': list(np.asarray(out).shape),
        'output_dtype': str(np.asarray(out).dtype),
        'curve_min': float(np.min(out)),
        'curve_max': float(np.max(out)),
        'curve_mean': float(np.mean(out)),
        'latency': stats(times),
        'load_plus_first_ms': load_total_ms,
        'librknnrt': librknnrt_version(args.librknnrt),
        'librknnrt_loaded': loaded,
    }

    if args.dump:
        np.save(args.dump, out)
        report['dump'] = os.path.abspath(args.dump)
    if args.ref:
        ref = np.load(args.ref)
        if ref.shape != np.asarray(out).shape:
            report['align_error'] = f'shape 不一致: ref{list(ref.shape)} vs out{list(np.asarray(out).shape)}'
            report['align_pass'] = False
        else:
            diff = float(np.abs(ref.astype('float32') - np.asarray(out).astype('float32')).max())
            report['ref'] = os.path.abspath(args.ref)
            report['align_max_abs'] = diff
            report['align_tol'] = args.tol
            report['align_pass'] = diff < args.tol

    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.report:
        with open(args.report, 'w') as f:
            f.write(text + '\n')
    return 0 if report.get('align_pass', True) else 1


if __name__ == '__main__':
    sys.exit(main())