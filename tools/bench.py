#!/usr/bin/env python3
"""bench.py —— 计量与验收复现入口（M0 骨架；主基准 §13 各行「复现方式」列指定本工具）。

契约来源（**本文件不重定义任何取值**）：
  - 报告字段清单：主基准 §6.5（`seq` 覆盖数 / 有效配对率 / 丢弃帧数（HQ 单列）/ FPS P50·P95 /
    帧延迟 P50·P95 / 曲线应用·合成·RGA 函数级耗时 / `f_est` 与 `α` / SoC 温度 /
    `discarded_triggers` / 冻结窗口标记；**配对率与丢弃帧数必须随报告给出**；延迟须拆
    「采集→提交」与「提交→vblank」两段 + 实测面板刷新周期 + 空链路基线）
  - 场景与开关：§13「复现方式」列（`--scenario realtime|hq|step-response`、`--capture-mipi`、
    `--seq-log`）与附件规范 `docs/measurements.md`（`--scenario idle-link --breakdown`、
    `--scenario apply-micro`、`--curve-diff-fp16`）
  - 时钟与配对纪律：只用 `mono_ns`（`CLOCK_MONOTONIC`）比对；**延迟按 `seq` 回填**，不得时间就近配对；
    ≥300 帧滑窗；HQ 与冻结窗口单独开窗（§6.5）
  - 调度随行：§6.1（v3.9 裁定 O9）要求报告中记录当时的调度与亲和设置
  - 运行方式：§10.6-B —— bench **SSH 前台运行、不进 systemd**（避免生命周期污染计量窗）

成熟度（诚实标注，勿误用）：
  ✅ 可跑：`model-infer`（模型级测速，包装 C 探针）、`curve-diff-fp16`（曲线场逐像素闸口）、
          `apply-micro --impl py-ref`（numpy 参考实现，**非交付路径**）、`validate-report`（字段校验）
  待链路：`realtime` / `hq` / `step-response` / `idle-link`（M1 起；当前无采集/显示链路，
          调用即明确报错，**不产出占位数字**）
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

SCHEMA_VERSION = 'bench/1.0'

# §6.5 报告字段：帧级场景必须齐全（validate-report 据此校验）
REQUIRED_FIELDS_FRAME = [
    'seq_coverage', 'pairing_rate', 'discarded_frames', 'fps', 'latency',
    'func_timing', 'f_est', 'alpha', 'soc_temp_c', 'discarded_triggers', 'freeze_window',
    'latency_segments', 'panel_refresh_ms', 'idle_link_baseline', 'scheduling',
]
CHAIN_SCENARIOS = ['realtime', 'hq', 'step-response', 'idle-link']


def mono_ns():
    """唯一的时序比对时钟（§6.5：只用 CLOCK_MONOTONIC）。"""
    return time.monotonic_ns()


def pct(values, q):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, int(round(q / 100.0 * (len(v) - 1)))))]


def window_stats(samples_ns):
    if not samples_ns:
        return {'n': 0}
    ms = [s / 1e6 for s in samples_ns]
    return {'n': len(ms), 'p50_ms': pct(ms, 50), 'p95_ms': pct(ms, 95),
            'max_ms': max(ms), 'mean_ms': statistics.fmean(ms)}


def soc_temp_c():
    for p in ('/sys/class/thermal/thermal_zone0/temp', '/sys/class/thermal/thermal_zone1/temp'):
        try:
            with open(p) as f:
                return int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return None


def scheduling_snapshot():
    """§6.1（O9）：报告须随带调度与亲和设置。"""
    snap = {'cpu_count': os.cpu_count()}
    try:
        snap['affinity'] = sorted(os.sched_getaffinity(0))
    except AttributeError:
        pass
    try:
        snap['loadavg'] = list(os.getloadavg())
    except OSError:
        pass
    return snap


def runtime_snapshot():
    lib = '/usr/lib/librknnrt.so'
    ver = None
    if os.path.exists(lib):
        with open(lib, 'rb') as f:
            blob = f.read()
        i = blob.find(b'librknnrt version: ')
        if i >= 0:
            ver = blob[i + 19:i + 80].split(b'\x00')[0].decode('utf-8', 'replace').strip()
    return {'librknnrt': ver, 'python': platform.python_version(),
            'host': platform.node(), 'machine': platform.machine()}


def base_report(args, scenario, scope):
    return {
        'schema_version': SCHEMA_VERSION,
        'tool': 'tools/bench.py',
        'scenario': scenario,
        'scope': scope,                     # frame | model | offline
        'started_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'host': platform.node(),
        'runtime': runtime_snapshot(),
        'clocks': {'compare': 'mono_ns', 'source': 'CLOCK_MONOTONIC'},
        'measurement_conditions': {
            'weight_hash': args.weight_hash or None,
            'curve_level': args.curve_level or None,
            'precision': args.precision or None,
            'config_snapshot_hash': args.config_hash or None,
        },
        'scheduling': scheduling_snapshot(),
        'soc_temp_c': soc_temp_c(),
        'notes': [],
    }


def emit(report, out):
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, 'w') as f:
            f.write(text + '\n')
        print(f'[report] {out}')
    return 0


def scenario_chain(args):
    """链路类场景（M1/M2 起）：当前无采集/显示链路 —— 明确报错，不产出占位数字。"""
    rep = base_report(args, args.scenario, 'frame')
    rep.update({
        'status': 'not_implemented',
        'reason': '链路尚未到位：本场景需 TH1 采集 + TH4/TH5 显示（MIPI 双窗 / HDMI 状态机）。'
                  '按纪律不产出任何占位数字（规则 10-evidence §2）。',
        'requires': 'M1（采集链路）/ M2（实时闭环）之后；复现命令见主基准 §13 对应行',
    })
    return emit(rep, args.out)


def scenario_model_infer(args):
    """模型级（NPU）测速：包装 tools/rknn_c_probe（C API，逐层剖析 + run vs wall）。

    scope=model：不产出 §6.5 的帧级指标，**不得作为 §13 验收依据**。
    """
    bin_path = args.probe_bin or os.path.expanduser('~/rknn_ab/probe/rknn_c_probe')
    rep = base_report(args, 'model-infer', 'model')
    if not os.path.exists(bin_path):
        rep.update({'status': 'missing_probe',
                    'reason': f'未找到 C 探针 {bin_path}（编译见 tools/rknn_c_probe.c 文件头）'})
        return emit(rep, args.out)
    if not (args.model and args.input):
        raise SystemExit('model-infer 需要 --model <rknn> 与 --input <raw float32 NHWC>')
    cmd = [bin_path, args.model, args.input, str(args.runs), args.input_layout,
           'float' if args.want_float else 'nofloat', 'nosram',
           'u8' if args.input_u8 else 'f32', 'noperf']
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        rep.update({'status': 'probe_failed', 'cmd': cmd, 'stderr': proc.stderr.strip()[-2000:]})
        return emit(rep, args.out)
    # C 探针的 stdout 会混入 RKNN 日志行，故按首行 JSON 锚点 + 结束标记提取
    out = proc.stdout
    i = out.find('{"api"')
    j = out.find('---PERF_DETAIL---', i) if i >= 0 else -1
    body = out[i:j if j > 0 else len(out)] if i >= 0 else ''
    body = body[:body.rfind('}') + 1] if '}' in body else ''
    try:
        probe = json.loads(body)
    except ValueError:
        probe = {'raw_tail': out[-2000:]}
    rep.update({'status': 'ok', 'cmd': cmd, 'probe': probe,
                'notes': ['scope=model：不满足 §6.5 帧级字段要求，不得作为 §13 验收依据']})
    return emit(rep, args.out)


def scenario_apply_micro(args):
    """曲线应用函数级打点（`docs/measurements.md` §3 第一行：各档耗时，含双线性采样）。

    `--impl py-ref` 用 tools/enhance_ref.py 的 numpy 参考实现，**只作上限参考、非交付路径**；
    交付路径是 C/NEON 单遍融合（§3.3「非优化项」），实现后以 `--impl native` 接入。
    """
    if args.impl != 'py-ref':
        rep = base_report(args, 'apply-micro', 'model')
        rep.update({'status': 'not_implemented',
                    'reason': 'native（C/NEON 单遍融合）属 M2 交付物（realtime_apply），尚未提供；'
                              '按 §3.3 不得以未融合实现替代判定'})
        return emit(rep, args.out)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import numpy as np
        from enhance_ref import enhance
    except ImportError as e:
        raise SystemExit(f'apply-micro --impl py-ref 需要 numpy 与 enhance_ref：{e}')
    shape = (1, 3, args.h, args.w)
    rng = np.random.default_rng(0)
    x = rng.random(shape, dtype=np.float32)
    x_r = (rng.random(shape, dtype=np.float32) - 0.5) * 0.6
    enhance(x, x_r)                                    # 预热
    samples = []
    for _ in range(args.runs):
        t0 = mono_ns()
        enhance(x, x_r)
        samples.append(mono_ns() - t0)
    rep = base_report(args, 'apply-micro', 'model')
    rep.update({'status': 'ok', 'impl': 'py-ref(numpy)',
                'apply_domain': f'{args.w}x{args.h}',
                'apply': window_stats(samples),
                'notes': ['结果来自 numpy 参考实现，**不代表交付路径（C/NEON 单遍融合）**，'
                          '不得用于 §6.1「曲线应用 ≤2 ms」的达标判定']})
    return emit(rep, args.out)


def scenario_curve_diff(args):
    """`docs/measurements.md` §3 第三行：曲线场逐像素 diff（盲评需真人，另计）。"""
    if not (args.ref and args.cand):
        raise SystemExit('curve-diff-fp16 需要 --ref 与 --cand')
    try:
        import numpy as np
    except ImportError as e:
        raise SystemExit(f'curve-diff-fp16 需要 numpy：{e}')

    def load(p):
        a = np.load(p) if p.endswith('.npy') else np.fromfile(p, dtype=np.float32)
        return np.asarray(a, dtype=np.float32)

    ref, cand = load(args.ref), load(args.cand)
    if ref.shape != cand.shape:
        raise SystemExit(f'shape 不一致：{ref.shape} vs {cand.shape}')
    d = np.abs(ref - cand)
    rep = base_report(args, 'curve-diff-fp16', 'offline')
    rep.update({'status': 'ok', 'ref': os.path.abspath(args.ref), 'cand': os.path.abspath(args.cand),
                'shape': list(ref.shape),
                'diff': {'max_abs': float(d.max()), 'mean_abs': float(d.mean()),
                         'tol': args.tol,
                         'gt_tol_ratio': float((d > args.tol).mean()) if args.tol is not None else None},
                'notes': ['本场景只覆盖 O23 闸口的「逐像素 diff」部分；「盲评（≥2 人）」须另行执行',
                          '阈值是参数：未给 --tol 时只报数不判定（阈值口径须先在 §15 登记）']})
    return emit(rep, args.out)


def scenario_validate(args):
    """§13「计量口径」行：报告字段齐全性 + 配对率校验。"""
    if not args.validate_report:
        raise SystemExit('validate-report 需 --validate-report <json>')
    with open(args.validate_report) as f:
        r = json.load(f)
    missing = [k for k in REQUIRED_FIELDS_FRAME if k not in r] if r.get('scope') == 'frame' else []
    pr = r.get('pairing_rate')
    v = {'report': os.path.abspath(args.validate_report), 'scope': r.get('scope'),
         'missing_fields': missing, 'pairing_rate': pr, 'min_pairing_rate': args.min_pairing_rate}
    if args.min_pairing_rate is None:
        v['pairing_ok'] = None
        v['note'] = '未给 --min-pairing-rate：只报数不判定（配对率下限口径须先在 §15 登记）'
    else:
        v['pairing_ok'] = (pr is not None and pr >= args.min_pairing_rate)
        if pr is None:
            v['note'] = '报告未给 pairing_rate，按 §6.5 视为不合格'
    v['pass'] = (not missing) and (v['pairing_ok'] is not False)
    v['status'] = 'ok'
    rep = base_report(args, 'validate-report', 'offline')
    rep.update(v)
    return emit(rep, args.out)


def main():
    p = argparse.ArgumentParser(description='计量与验收复现入口（M0 骨架）',
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--scenario', default='',
                   choices=['realtime', 'hq', 'step-response', 'idle-link', 'model-infer', 'apply-micro'])
    p.add_argument('--out', default='', help='报告输出路径（默认只打印）')
    p.add_argument('--runs', type=int, default=30)
    p.add_argument('--breakdown', action='store_true', help='延迟分两段上报（§6.5）')
    p.add_argument('--seq-log', action='store_true', help='记 seq 级明细（HQ 响应）')
    p.add_argument('--capture-mipi', action='store_true', help='抓 MIPI 截图（常态 + HQ）')
    p.add_argument('--curve-diff-fp16', action='store_true', help='曲线场逐像素闸口（O23）')
    p.add_argument('--validate-report', default='', metavar='JSON')
    p.add_argument('--min-pairing-rate', type=float, default=None,
                   help='配对率下限（未设则只报数；口径须先在 §15 登记）')
    p.add_argument('--weight-hash', default='')
    p.add_argument('--curve-level', default='')
    p.add_argument('--precision', default='')
    p.add_argument('--config-hash', default='')
    p.add_argument('--impl', default='py-ref', choices=['py-ref', 'native'])
    p.add_argument('--h', type=int, default=608, help='apply-micro 的应用域高')
    p.add_argument('--w', type=int, default=1080, help='apply-micro 的应用域宽')
    p.add_argument('--ref', default='', help='curve-diff 参考曲线场（FP16）')
    p.add_argument('--cand', default='', help='curve-diff 待判曲线场')
    p.add_argument('--tol', type=float, default=None)
    p.add_argument('--probe-bin', default='')
    p.add_argument('--model', default='')
    p.add_argument('--input', default='', help='raw float32 NHWC 输入')
    p.add_argument('--input-layout', default='nhwc', choices=['nhwc', 'nchw'])
    p.add_argument('--input-u8', action='store_true')
    p.add_argument('--want-float', action='store_true')
    args = p.parse_args()

    if args.validate_report:
        return scenario_validate(args)
    if args.curve_diff_fp16:
        return scenario_curve_diff(args)
    if not args.scenario:
        p.error('需指定 --scenario（或 --curve-diff-fp16 / --validate-report）')
    if args.scenario in CHAIN_SCENARIOS:
        return scenario_chain(args)
    if args.scenario == 'model-infer':
        return scenario_model_infer(args)
    if args.scenario == 'apply-micro':
        return scenario_apply_micro(args)
    p.error('未实现的场景')


if __name__ == '__main__':
    sys.exit(main())
