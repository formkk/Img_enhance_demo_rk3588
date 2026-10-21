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
  ✅ 可跑：`realtime`（§13「实时帧率」/「端到端延迟」：≥300 帧滑窗 × 3 轮，每轮独立进程）、
          `hq`（§13「HQ 响应」：同进程驱动 `main.Demo` + 时间注入 ≥10 次触发）、
          `idle-link`（**空链路基线**，`docs/measurements.md` §1 / O19：`main.py --idle-link` ×
          ≥300 帧 × 3 轮，报告给 `baseline_candidate` 与 `o19_trigger`）、
          `model-infer`（模型级测速，包装 C 探针）、`curve-diff-fp16`（曲线场逐像素闸口）、
          `apply-micro --impl py-ref`（numpy 参考实现，**非交付路径**）、
          `apply-micro --impl native`（交付内核）、`osd-micro`、`validate-report`（字段校验）、
          **`step-response`（v3.86 裁定 O143 已接桩：≥10 次曝光阶跃 + 曲线场指纹测收敛）**
  （原「待链路」项已清零）
"""

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

SCHEMA_VERSION = 'bench/1.0'

# §6.5 报告字段：帧级场景必须齐全（validate-report 据此校验）
REQUIRED_FIELDS_FRAME = [
    'seq_coverage', 'pairing_rate', 'discarded_frames', 'fps', 'latency',
    'func_timing', 'f_est', 'alpha', 'soc_temp_c', 'discarded_triggers', 'freeze_window',
    'latency_segments', 'panel_refresh_ms', 'idle_link_baseline', 'scheduling',
]
CHAIN_SCENARIOS = ['step-response']


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


def window_stats_ms(samples_ms):
    """毫秒样本 → `window_stats` 口径（内部按 ns 存放，此处换算以复用同一分位实现）。"""
    return window_stats([v * 1e6 for v in samples_ms])


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


def metrics_keys(args):
    """§6.5 计量四键（**v3.86 追加 95 定稿**）：判定窗 / 时钟域 / 时间戳来源 / 配对方式。

    取值唯一来源 = `config.yaml`（§10.4 键表）⇒ **键空即报错**（"空值 = 未定稿"），
    不得在本工具内用硬编码顶替。本函数只读不写，**不新增 config 键 / 日志事件 / 返回码**。
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import config as starlight_config
    cfg = starlight_config.load(args.config or None)
    mk = {k: cfg.require_final(f'metrics.{k}') for k in
          ('window_frames', 'timestamp_domain', 'timestamp_source', 'pair_by')}
    # 与实现矛盾即**硬报错**（同 `main.py` 的 `metrics_policy()` 口径）：本工具只有 `mono_ns` 时钟
    # 与按 `seq` 回填两条实现路径 ⇒ 键写别的值属配置写错，**不得出报告**。
    if str(mk['timestamp_domain']) != 'MONOTONIC':
        raise starlight_config.ConfigError(
            f"metrics.timestamp_domain={mk['timestamp_domain']!r} ≠ MONOTONIC（§6.5 唯一合法时钟域）")
    if str(mk['pair_by']) != 'seq':
        raise starlight_config.ConfigError(
            f"metrics.pair_by={mk['pair_by']!r} ≠ seq（§6.5 禁用时间就近配对）")
    return mk


def policy_block(per, mk):
    """把四键 + 逐轮断言结果汇总进报告（§6.5；**v3.86 追加 95**）。"""
    bad = [{'round': i + 1, 'mismatch': p.get('metrics_policy_mismatch') or {}}
           for i, p in enumerate(per) if p.get('metrics_policy_ok') is False]
    return {'metrics_policy': mk, 'metrics_policy_ok': not bad,
            'metrics_policy_mismatch': bad,
            'note': '四键语义 = 期望值 / 断言值（§6.5）：`window_frames` 决定本报告的判定窗长；'
                    '`timestamp_domain` / `pair_by` 与实现矛盾即硬报错；`timestamp_source` 为运行期实测，'
                    '偏离期望只标记并随行注明（窗口作废由 `capture/timestamp_source_changed` 承担）'}


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


def scenario_hq(args):
    """§13「HQ 响应」行：**触发 → MIPI OSD ≤50 ms**；**触发 → 全屏 HQ 视图出图 ≤12 s**。

    复现命令 = `python3 tools/bench.py --scenario hq --seq-log`（**≥10 次触发**，`--runs`）。
    链路 = **同进程驱动 `main.Demo`**（TH1 采集 → TH2 曲线 → TH3 HQ 推理 → TH4 MIPI → TH5 HDMI）；
    触发注入走 `main.py` 的既有验证钩子（与 `--trigger-times` 同源，**不新增 config 键 / 事件**）。

    **前置**：TH4 取 DRM master ⇒ 须先 `sudo systemctl isolate multi-user.target` 停桌面（恢复见 R20）。
    §6.5：时刻比对只用 `mono_ns`；出图时点取 **pageflip 的 vblank**，不取提交时刻；冻结窗单独开窗、
    并从 fps / 延迟 / 配对窗**排除**（O90）。
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo_root)
    triggers = int(args.runs)
    if triggers < 10:
        raise SystemExit('§13「HQ 响应」行要求 ≥10 次触发 ⇒ `--runs`（= 触发次数）须 ≥10')
    try:
        import config as starlight_config
        import log as starlog
        import main as main_mod
    except ImportError as e:
        raise SystemExit(f'hq 场景需要交付链路（main.py + config + log）：{e}')

    cfg = starlight_config.load(args.config or None)
    logger = starlog.from_config(cfg)
    dump_dir = str(args.dump_canvas_dir or '')
    if args.capture_mipi and not dump_dir:
        # §13「OSD」行的 `--capture-mipi`：常态 1 张 + HQ 期 1 张（**链路原样**、零色调后处理）。
        # 证据位置落 `reports/`（规则 10-evidence §4；**v3.86 裁定 O102 后 `storage.dir` 已取消**）。
        dump_dir = os.path.join(repo_root, 'review', 'hq_path_canvas')

    d = main_mod.Demo(cfg, logger, frames=int(args.frames),
                      trigger_times=triggers, trigger_interval_s=float(args.trigger_interval_s),
                      dump_canvas_dir=dump_dir)
    rep = base_report(args, 'hq', 'frame')
    rep.update({'entry': 'bench.py --scenario hq --seq-log（同进程驱动 main.Demo；链路 = TH1…TH5）',
                'hq_active_shape': str(cfg.require_final('hq.active_shape')),
                'triggers_requested': triggers,
                'dump_canvas_dir': dump_dir or None})
    t0 = time.monotonic()
    d._t_start = t0
    rc = d.boot()
    if rc != 0:
        rep.update({'status': 'boot_failed', 'rc': rc,
                    'reason': 'TH4 取 DRM master 失败（先 `sudo systemctl isolate multi-user.target`）'})
        emit(rep, args.out)
        return 1
    try:
        d.run()
    finally:
        # TH3 / TH5 的 `info()` 须在 `shutdown()` **之前**取（shutdown 会 deinit 模型）
        th3 = d.kf.info() if d.kf is not None else {}
        th5 = d.hdmi.info() if d.hdmi is not None else {}
        d.shutdown()

    wall = max(1e-6, time.monotonic() - t0)
    c = d.counters
    ts = d.trig_summary()
    paired_unpaired = c['flips'] - d.pair_ok
    osd_ok = sum(1 for r in d.trig_log if r['osd_flip_seq'] == r['seq_src'])
    fs_ok = sum(1 for r in d.trig_log if r['fs_mipi_seq'] == r['seq_src'])
    osd_p95, fs_p95 = pct(ts['osd_ms'], 95), pct(ts['fullscreen_mipi_ms'], 95)

    rep.update({
        'status': 'ok',
        'cycles_completed': ts['cycles'],
        'trigger_response': {
            'osd': window_stats_ms(ts['osd_ms']),
            'fullscreen_mipi': window_stats_ms(ts['fullscreen_mipi_ms']),
            'fullscreen_hdmi': window_stats_ms(ts['fullscreen_hdmi_ms']),
            'budget': {'osd_ms': 50.0, 'fullscreen_ms': 12000.0},
            'pass': {'osd_p95_le_50ms': None if osd_p95 is None else osd_p95 <= 50.0,
                     'fullscreen_mipi_p95_le_12s': None if fs_p95 is None else fs_p95 <= 12000.0},
            'exit_reasons': ts['exit_reasons'],
        },
        'seq_coverage': {'triggers': ts['cycles'], 'osd_flip_seq_pair_ok': osd_ok,
                         'fullscreen_mipi_seq_pair_ok': fs_ok},
        'pairing_rate': d.pair_ok / max(1, c['flips']),
        'paired_flips': d.pair_ok,
        'discarded_frames': {'flip_skipped': c['flip_skipped'], 'unpaired': paired_unpaired,
                             'dropped_stale': c['dropped_stale']},
        'fps': {'frames': c['frames'], 'wall_s': wall, 'frozen_s': d.freeze_ns / 1e9,
                'active_window_fps': (c['frames'] / max(1e-6, wall - d.freeze_ns / 1e9)),
                'floor_ref': 14.99},
        'latency': window_stats_ms(d.lat),
        'latency_segments': {'capture_to_submit': window_stats_ms(d.lat_app),
                             'submit_to_vblank': window_stats_ms(d.lat_submit)},
        'func_timing': {'hq_last_run_ns': th3.get('last_run_ns'), 'hq_runs': th3.get('runs'),
                        'hq_core_mask_ret': th3.get('core_mask_ret'),
                        'hdmi_mirror_rga_last_ns': th5.get('last_scale_ns'),
                        'hdmi_mirror_rga': th5.get('scale'),      # §6.1 镜像行：n / P50 / P90（≤3 ms）
                        'note': '本场景只含 TH3 推理与 TH5 放大两处函数级打点；'
                                'RGA 合成 / OSD / 曲线应用的逐函数耗时见 realtime / osd-micro 场景'},
        'f_est': d.est.f_est if d.est else None,
        'alpha': d.est.alpha if d.est else None,
        'discarded_triggers': c['discarded_triggers'],
        'freeze_window': {'cycles': len(ts['freeze_ms']), 'total_ms': sum(ts['freeze_ms']),
                          'per_cycle_ms': ts['freeze_ms'],
                          'marker': 'display_mipi/freeze_enter|freeze_exit',
                          'excluded_from': ['fps', 'latency', 'pairing']},
        'panel_refresh_ms': 1000.0 / float(cfg.require_final('display.panel_refresh_hz')),
        'idle_link_baseline': cfg.get('metrics.idle_link_baseline'),
        'counters': c,
        'th3_info': th3,
        'th5_info': th5,
        'notes': [
            '判据 = §13「HQ 响应」行：触发→MIPI OSD ≤50 ms；触发→全屏 HQ 视图出图 ≤12 s'
            '（v3.58 裁定 O37 重排、v3.85 裁定 O83 语义改「两屏同时」）；判定 = 实现者',
            'O90：HQ 期间实时链冻结 ⇒ fps 取**活动窗口径**（冻结窗已从分母排除）；'
            '`hq_flips_delta` 随 O90 作废，改记 `freeze_window`',
            '§6.5：时钟只用 `mono_ns`；出图时点 = pageflip 的 vblank；配对按 `seq` 回填，禁止时间就近配对',
            '本场景报告随带配对率与丢弃帧数；`--seq-log` 给出每周期 seq 级明细',
        ],
    })
    if d.trig_log:
        rep['notes'].append('触发→OSD / 全屏 的 P95 判据见 trigger_response.pass')
    if rep['idle_link_baseline'] is None:
        rep['notes'].append('`metrics.idle_link_baseline` 缺失 ⇒ 超标归因不可闭合（§6.5）')
    if args.seq_log:
        rep['seq_log'] = d.trig_log
    return emit(rep, args.out)


def scenario_realtime(args):
    """§13「实时帧率」/「端到端延迟」行的复现入口（**无 HQ**）：`--scenario realtime`。

    复现方式（§13 该两行）= **`bench.py --scenario realtime`；≥300 帧滑窗 × 3 轮**
    ⇒ 默认 `--rounds 3` / `--frames 300`。判定口径 = §6.5（`mono_ns`、按 `seq` 回填配对、≥300 帧滑窗、
    HQ 与冻结窗单独开窗、配对率与丢弃帧数随报告给出、延迟两段分解 + 空链路基线对比）。

    **每轮起一个独立进程**（`main.py --report-json`）——机房纪律：**全生命周期只 `stream on` 一次**
    （规则 `30-board-safety.md` §1）⇒ **不把 3 轮塞进同一进程**（那会让 `stream_on_count` 变成 3）。
    **前置**：TH4 需 DRM master（先 `sudo systemctl isolate multi-user.target`）。
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo_root)
    rounds = max(1, int(args.rounds))
    # **（v3.86 追加 95）** 判定窗长缺省 = `metrics.window_frames`（§6.5；键空即报错 ⇒ 不再用 300 硬编码）
    mk = metrics_keys(args)
    frames = int(args.frames) or int(mk['window_frames'])
    per = []
    for r in range(rounds):
        tmp = os.path.join(tempfile.gettempdir(), f'bench_rt_r{r + 1}.json')
        cmd = [sys.executable, os.path.join(repo_root, 'main.py'),
               '--frames', str(frames), '--report-json', tmp]
        # **（v3.86 追加 96）** ①-2 子项拆分：把路径开关透传给 `main.py`（**非 config 键**）
        for flag in ('--no-est', '--no-apply', '--no-osd',
                     '--osd-status-only', '--osd-no-stats'):
            if getattr(args, flag.replace('-', '_').lstrip('_'), False):
                cmd.append(flag)
        if int(getattr(args, 'est_first', 0) or 0) > 0:
            cmd += ['--est-first', str(int(args.est_first))]
        # **[A/B 开关] 保比语义**：默认保比；bench 的 `--no-preserve-ratio` → `main.py --no-preserve-ratio`
        # （**非 config 键**；默认走保比语义，行为与本轮一致）
        if not args.preserve_ratio:
            cmd.append('--no-preserve-ratio')
        if args.config:
            cmd += ['--config', args.config]
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.exists(tmp):
            rep = base_report(args, 'realtime', 'frame')
            rep.update({'status': 'round_failed', 'round': r + 1, 'rc': proc.returncode,
                        'cmd': cmd, 'stdout_tail': proc.stdout.strip()[-1500:],
                        'stderr_tail': proc.stderr.strip()[-1500:],
                        'reason': 'TH4 取 DRM master 失败（先 `sudo systemctl isolate multi-user.target`）'
                                  '或链路启动失败'})
            return emit(rep, args.out)
        with open(tmp) as f:
            per.append(json.load(f))

    def col(key):
        return [p.get(key) for p in per]

    def med(vals):
        v = sorted(x for x in vals if x is not None)
        return v[len(v) // 2] if v else None

    lat = [x for p in per for x in (p.get('lat_all_ms') or [])]
    lat_app = [x for p in per for x in (p.get('lat_app_all_ms') or [])]
    lat_sub = [x for p in per for x in (p.get('lat_submit_all_ms') or [])]
    # **（v3.86 追加 96）** `--skip-first N`：每轮剔除**前 N 帧样本**（事先声明的预热前缀，
    # 与 `--est-first N` 配对用于"只测 TH2b"）。**两口径都随报告给出**（`latency` = 剔除后、
    # `latency_full` = 全部样本），不作事后筛选——剔除量与剔除前后样本数均在报告中可见。
    _skip = max(0, int(getattr(args, 'skip_first', 0) or 0))

    def _pool(key):
        out = []
        for p in per:
            out.extend(list(p.get(key) or [])[_skip:])
        return out

    lat_full, lat_app_full, lat_sub_full = lat, lat_app, lat_sub
    lat = _pool('lat_all_ms')
    lat_app = _pool('lat_app_all_ms')
    lat_sub = _pool('lat_submit_all_ms')
    frames_all = sum(int(p.get('frames') or 0) for p in per)
    flips_all = sum(int(p.get('flips') or 0) for p in per)
    paired_all = sum(int(p.get('paired') or 0) for p in per)
    baseline = per[0].get('idle_link_baseline')
    fps_vals = [p.get('fps_active') for p in per]
    rep = base_report(args, 'realtime', 'frame')
    rep.update({
        'status': 'ok',
        'entry': 'bench.py --scenario realtime（每轮一个独立进程跑 `main.py --report-json`；链路 = TH1…TH5）',
        'rounds': rounds, 'frames_per_round': frames,
        # **（v3.86 裁定 O97）** 判定口径唯一化 = **帧窗**（分母扣**进程启动**与**退出序**，§6.5 新增条）；
        # `active_*`（含启动）与 `steady_*`（扣启动、**仍含退出序**）**降为诊断旁证、不得用于判定**。
        'fps': {'primary': 'window（帧窗口径；O97 判定口径）',
                'window_per_round': col('fps_window'), 'window_median': med(col('fps_window')),
                'window_ms_per_round': col('window_ms'),
                'frame_span_ms_per_round': col('frame_span_ms'),
                'n': len(per), 'frames_total': frames_all, 'floor_ref': 14.99,
                # 诊断旁证（保留是为旧报告可比；**不得用于判定**）
                'active_per_round': fps_vals, 'active_median': med(fps_vals),
                'steady_per_round': col('fps_steady'), 'steady_median': med(col('fps_steady')),
                'boot_s_per_round': col('boot_s'),
                'note': '判定用 `window_*` = 帧窗口径（`frames / (t_run_done − t_boot_done − 冻结窗)`，'
                        '§6.5 / O97）；`active_*`（含启动）与 `steady_*`（扣启动、仍含退出序）为诊断旁证'},
        'latency': window_stats_ms(lat),
        'latency_segments': {'capture_to_submit': window_stats_ms(lat_app),
                             'submit_to_vblank': window_stats_ms(lat_sub)},
        # **（v3.86 追加 96）** ①-2 子项拆分：本轮的**路径自述**与**剔除前**的同口径统计。
        'path_variant': {'no_est': bool(args.no_est), 'est_first': int(args.est_first),
                         'no_apply': bool(args.no_apply), 'no_osd': bool(args.no_osd),
                         # [A/B 开关] 曲线应用语义（与 main.py 报告同名字段；默认 True = 保比）
                         'apply_preserve_ratio': bool(args.preserve_ratio),
                         'osd_status_only': bool(args.osd_status_only),
                         'osd_no_stats': bool(args.osd_no_stats)},
        'skip_first': _skip,
        'latency_full': {'end_to_end': window_stats_ms(lat_full),
                         'capture_to_submit': window_stats_ms(lat_app_full),
                         'submit_to_vblank': window_stats_ms(lat_sub_full)},
        'pairing_rate': paired_all / max(1, flips_all), 'paired_flips': paired_all,
        'seq_coverage': {'flips': flips_all, 'paired': paired_all,
                         'unpaired': flips_all - paired_all},
        'discarded_frames': {
            'flip_skipped': sum(int(p['counters'].get('flip_skipped', 0)) for p in per),
            'unpaired': flips_all - paired_all,
            'dropped_stale': sum(int(p['counters'].get('dropped_stale', 0)) for p in per),
            'lease_timeouts': sum(int(p['counters'].get('lease_timeouts', 0)) for p in per)},
        'freeze_window': {'total_ms': sum(int(p.get('freeze_ns') or 0) for p in per) / 1e6,
                          'marker': 'display_mipi/freeze_enter|freeze_exit',
                          'excluded_from': ['fps', 'latency', 'pairing'],
                          'note': '本场景不注入触发 ⇒ 期望恒为 0'},
        'func_timing': {'hdmi_mirror_rga': per[0].get('mirror_scale'),   # §6.1 镜像行：n / P50 / P90（≤3 ms）
                        'stream_on_count': col('stream_on_count'),
                        'note': '本场景（无 HQ）的函数级打点只含 TH5 放大；RGA 合成 / OSD / 曲线应用'
                                '见 osd-micro 等场景'},
        'f_est': med(col('f_est')), 'alpha': med(col('alpha')),
        'discarded_triggers': sum(int(p['counters'].get('discarded_triggers', 0)) for p in per),
        'panel_refresh_ms': med(col('panel_refresh_ms')),
        'idle_link_baseline': baseline,
        'rounds_detail': [{'round': i + 1, 'fps_window': p.get('fps_window'),
                           'window_ms': p.get('window_ms'), 'frame_span_ms': p.get('frame_span_ms'),
                           'fps_active': p.get('fps_active'),
                           'fps_steady': p.get('fps_steady'), 'boot_s': p.get('boot_s'),
                           'wall_s': p.get('wall_s'), 'frames': p.get('frames'),
                           'lat_p50_ms': p.get('lat_p50_ms'), 'lat_p95_ms': p.get('lat_p95_ms'),
                           'pairing_rate': p.get('pairing_rate'),
                           'stream_on_count': p.get('stream_on_count'),
                           'mirror_scale': p.get('mirror_scale')}
                          for i, p in enumerate(per)],
        'notes': ['判据 = §13「实时帧率」行（无 HQ：≥14.99 FPS，O89/O93）与「端到端延迟」行'
                  '（采集→MIPI 显示 P95 ≤80 ms，两段分解随报告给出）；判定 = 实现者 / 验收人',
                  '§6.5：时钟只用 `mono_ns`；配对按 `seq` 回填；≥300 帧滑窗 × 3 轮'
                  '（每轮一个独立进程 ⇒ `stream_on_count` 恒为 1）',
                  '判据口径（O97）= `fps.window_*` 帧窗口径；`active_*` / `steady_*` 只作旁证',
                  '计量四键（v3.86 追加 95）：判定窗长 = `metrics.window_frames`（键值唯一来源）',
                  '杠杆：HDMI 镜像耗时（§6.1 O85 行 ≤3 ms）取 `func_timing.hdmi_mirror_rga` 的 n / P50 / P90'],
        **policy_block(per, mk),
    })
    if baseline is None:
        rep['notes'].append('`metrics.idle_link_baseline` 缺失 ⇒ 超标归因不可闭合（§6.5 / O19）')
    # `--baseline`（§13「端到端延迟」行的复现命令含此开关）：把「端到端 − M1 空链路基线」的
    # **归因差值**随报告给出（规则 `30-board-safety.md` §3：超标归因必须先扣空链路基线）。
    # 基线缺失时**只标注、不产出差值**（不得用邻近数字顶替）。
    if args.baseline:
        # `metrics.idle_link_baseline` 的落盘形态 = **位置化数列（ms）**：
        #   `[端到端 P50, 端到端 P95, 采集→提交 P50, 采集→提交 P95, 提交→vblank P50, 提交→vblank P95]`
        # （§10.4 该行；config 的叶子键不得出现表外子键 ⇒ 不用字典、用位置化数列，次序在该行与
        # `config.yaml` 注释里写明）。只给前两项时，两段差值不产出（不臆造）。
        b = baseline
        if isinstance(baseline, (list, tuple)) and len(baseline) >= 2:
            b = {'p50_ms': baseline[0], 'p95_ms': baseline[1]}
            if len(baseline) >= 6:
                b.update({'capture_to_submit_p50_ms': baseline[2],
                          'capture_to_submit_p95_ms': baseline[3],
                          'submit_to_vblank_p50_ms': baseline[4],
                          'submit_to_vblank_p95_ms': baseline[5]})
        elif not isinstance(baseline, dict):
            b = None
        if not b:
            rep['attribution'] = {'status': 'baseline_missing',
                                  'reason': '`metrics.idle_link_baseline` 尚未回填 ⇒ 归因未闭合'
                                            '（§6.5 / O19；先跑 `--scenario idle-link --breakdown`）'}
        else:
            def _sub(a, b2):
                return None if (a is None or b2 is None) else round(float(a) - float(b2), 3)

            att = {
                'status': 'ok',
                'idle_link_baseline': b,
                'end_to_end_p95_minus_baseline_ms': _sub(rep['latency'].get('p95_ms'), b.get('p95_ms')),
                'end_to_end_p50_minus_baseline_ms': _sub(rep['latency'].get('p50_ms'), b.get('p50_ms')),
                'note': '差值 = 常态链路（含 TH2a/TH2b/OSD）− 空链路基线；'
                        '两值须同口径（§6.1 / §6.5：跨口径差值不得入报告结论）',
            }
            # 两段差值只在基线携带两段取值时才产出（`len >= 6`）；否则**不臆造**。
            if b.get('capture_to_submit_p95_ms') is not None:
                att['capture_to_submit_p95_minus_baseline_ms'] = _sub(
                    rep['latency_segments']['capture_to_submit'].get('p95_ms'),
                    b.get('capture_to_submit_p95_ms'))
                att['submit_to_vblank_p95_minus_baseline_ms'] = _sub(
                    rep['latency_segments']['submit_to_vblank'].get('p95_ms'),
                    b.get('submit_to_vblank_p95_ms'))
            rep['attribution'] = att
    return emit(rep, args.out)


def scenario_step_response(args):
    """§13「切换响应」行 / §11 M2 出口标准 ①-3 的**复现入口**：**≥10 次亮度阶跃**。

    **口径（v3.86 裁定 O143）**：
      · **阶跃** = 运行期**曝光微调**（规则 `20-contracts.md` §4 明许；`v4l2-ctl --set-ctrl=exposure=`），
        由 `main.py --step-exp-times N --step-exp-interval-s S` 在帧循环内按计划施加；
      · **测点** = **曲线场指纹**（场前 256 个 fp16 的均值）**逐次发布**取值（`f_est` ≈ 7.6 Hz ⇒
        时间分辨率约 132 ms，**随报告给出**，不得读作优于该分辨率的结论）；
      · **收敛判据** = 阶跃后首次进入「新稳态 ±10% 行程」并**此后保持**的发布时刻；
      · **报告给出**：逐次 `conv_ms`、P50/P95、以及**派生对照** `N/α`（§9.1：`N = ln(0.1)/ln(α)`）。
    **判据**（§13 / §9.1）= 切换响应 ≤500 ms；**本工具只出数**，判定 = 实现者 / 验收人。
    **前置**：TH4 需 DRM master（先 `sudo systemctl isolate multi-user.target`）。
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo_root)
    n_steps = int(args.runs)
    if n_steps < 10:
        raise SystemExit('§13「切换响应」行要求 ≥10 次阶跃 ⇒ `--runs` 须 ≥10')
    mk = metrics_keys(args)
    interval = float(getattr(args, 'step_interval_s', 3.0) or 3.0)
    tail_s = float(getattr(args, 'step_tail_s', 3.0) or 3.0)
    # 轮长 = 阶跃计划 + 尾段（尾段用于取「新稳态」参考值）
    frames = int(args.frames) or int((n_steps * interval + tail_s) * 15 + 30)
    tmp = os.path.join(tempfile.gettempdir(), 'bench_step_r1.json')
    cmd = [sys.executable, os.path.join(repo_root, 'main.py'),
           '--frames', str(frames), '--report-json', tmp,
           '--step-exp-times', str(n_steps), '--step-exp-interval-s', str(interval)]
    if int(getattr(args, 'step_low', 0) or 0) > 0:
        cmd += ['--step-exp-low', str(int(args.step_low))]
    if int(getattr(args, 'step_high', 0) or 0) > 0:
        cmd += ['--step-exp-high', str(int(args.step_high))]
    if getattr(args, 'step_subdev', ''):
        cmd += ['--step-exp-subdev', str(args.step_subdev)]
    if args.config:
        cmd += ['--config', args.config]
    proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
    rep = base_report(args, 'step-response', 'frame')
    if proc.returncode != 0 or not os.path.exists(tmp):
        rep.update({'status': 'round_failed', 'rc': proc.returncode, 'cmd': cmd,
                    'stdout_tail': proc.stdout.strip()[-1500:],
                    'stderr_tail': proc.stderr.strip()[-1500:],
                    'reason': 'TH4 取 DRM master 失败（先 `sudo systemctl isolate multi-user.target`）'
                              '或链路启动失败'})
        return emit(rep, args.out)
    with open(tmp) as f:
        run = json.load(f)

    steps = run.get('step_log') or []
    series = [(int(t), int(f), float(mo), float(me))
              for t, f, mo, me in (run.get('step_series') or [])]
    pubs = [(int(t), float(v)) for t, _s, v in (run.get('est_publish_log') or [])]
    # **场指纹旁证（v3.86 裁定 O143 复验）**：首轮曾以它为状态量 ⇒ 4× 曝光阶跃仅变 0.1%~0.4%
    # ⇒ **对亮度不敏感、不得作判据**；此处仅作诊断量随报告给出。
    fp_span = (None if not pubs else
               max(abs(pubs[i + 1][1] - pubs[i][1]) for i in range(len(pubs) - 1)))
    rows = []
    for k, st in enumerate(steps):
        t_step = int(st['t_step_ns'])
        t_end = int(steps[k + 1]['t_step_ns']) if k + 1 < len(steps) else None
        seg = [p for p in series if p[0] >= t_step and (t_end is None or p[0] < t_end)]
        pre = [p for p in series if p[0] < t_step]
        if len(seg) < 3 or not pre:
            rows.append({'step': k + 1, 't_step_ms': (t_step - series[0][0]) / 1e6 if series else None,
                         'exposure_from': st.get('from'), 'exposure_to': st.get('to'),
                         'conv_ms': None, 'reason': '段内样本 <3 或无阶跃前参考'})
            continue
        mo0, me0 = pre[-1][2], pre[-1][3]           # 阶跃前最后一个样本（原图 / 增强）
        mo1, me1 = seg[-1][2], seg[-1][3]           # 段内最后一个样本（新稳态）
        span = me1 - me0
        thr = abs(span) * 0.10                      # **新稳态 ±10% 行程**（对增强窗均值）
        conv = None
        if abs(span) > 1e-9:
            for j in range(len(seg)):
                if all(abs(p[3] - me1) <= thr for p in seg[j:]):
                    conv = seg[j][0]
                    break
        rows.append({'step': k + 1, 't_step_ms': (t_step - series[0][0]) / 1e6,
                     'exposure_from': st.get('from'), 'exposure_to': st.get('to'),
                     'mean_orig_start': mo0, 'mean_orig_final': mo1,
                     'mean_enh_start': me0, 'mean_enh_final': me1, 'span_enh': span,
                     'conv_ms': None if conv is None else (conv - t_step) / 1e6,
                     'samples_in_segment': len(seg),
                     'reason': None if conv is not None else '段内未进入 ±10% 行程或行程过小'})
    convs = [r['conv_ms'] for r in rows if r.get('conv_ms') is not None]
    p50 = pct(convs, 50) if convs else None
    p95 = pct(convs, 95) if convs else None
    alpha = run.get('alpha')
    f_est = run.get('f_est')
    derived = (math.log(0.1) / math.log(alpha) / f_est * 1e3
               if alpha and f_est else None)
    rep.update({
        'status': 'ok' if convs else 'no_convergence',
        'entry': 'bench.py --scenario step-response（每轮独立进程跑 `main.py --step-exp-*`）',
        'steps_requested': n_steps, 'steps_executed': len(steps),
        'step_interval_s': interval, 'frames': frames,
        'series_period_ms': (None if len(series) < 2 else
                             statistics.fmean([series[i + 1][0] - series[i][0]
                                               for i in range(len(series) - 1)]) / 1e6),
        'publish_period_ms': (None if len(pubs) < 2 else
                              statistics.fmean([pubs[i + 1][0] - pubs[i][0]
                                                for i in range(len(pubs) - 1)]) / 1e6),
        'field_fingerprint_span_max': fp_span,
        'conv_ms': {'n': len(convs), 'p50_ms': p50, 'p95_ms': p95,
                    'max_ms': max(convs) if convs else None,
                    'mean_ms': statistics.fmean(convs) if convs else None},
        'derived_ms': {'formula': 'N = ln(0.1)/ln(alpha); 收敛 = N / f_est',
                       'alpha': alpha, 'f_est': f_est, 'value_ms': derived},
        'steps': rows,
        'pairing_rate': run.get('pairing_rate'), 'discarded_frames': run.get('counters'),
        'metrics_policy': mk,
        'notes': ['判据 = §13「切换响应」行（场景亮度阶跃 → 增强生效 ≤500 ms）× ≥10 次阶跃；'
                  '判定 = 实现者 / 验收人',
                  '阶跃方式 = **运行期曝光微调**（规则 20 §4 明许）；**测点 = 增强窗均值**'
                  '`mean_enh`（每 2 帧一次，时间分辨率 = `series_period_ms`，**不得读作优于该分辨率**）',
                  '**曝光方向**：`camera.exposure_us`（66666 µs）已近满帧上限 ⇒ **只能向下阶跃**'
                  '（高档 = 现值，低档 = 现值/4）；向上 4× 会被驱动 clamp 成空操作',
                  '`field_fingerprint_span_max` = 曲线场指纹（场前 256 个 fp16 均值）的相邻最大变化，'
                  '**仅作旁证**：实测对该阶跃不敏感（≈0.4%）⇒ **不得作判据**',
                  '收敛判据 = 阶跃后首次进入「新稳态 ±10% 行程」并此后保持',
                  '`derived_ms` 仅作对照（§9.1 的解析式），**不与实测混用**'],
    })
    return emit(rep, args.out)


def scenario_idle_link(args):
    """§6.1（v3.4「必读」段）/ `docs/measurements.md` §1（O19）的**空链路基线**入口。

    §6.1 原文定范围：*"关闭增强，只跑"采集 → RGA 原图窗 → MIPI 显示"，实测 P50/P95 入 config"*。
    本轮按字面口径实现（`main.py --idle-link`，**不是 config 键**）：
      · 关 TH2a（不 `request`）/ 关 TH2b（不 `apply_window`）/ 关 OSD（`with_osd=False`）
      · 保留 TH1 采集与 lease、RGA 原图窗、画布合成、DRM pageflip、`seq` 回填配对
    复现命令 = `bench.py --scenario idle-link --breakdown`（§13「端到端延迟」行与 `docs/measurements.md`
    §1 均指定）；**≥300 帧滑窗 × 3 轮**，每轮一个独立进程（保 `stream_on_count=1`）。

    **前置**：TH4 需 DRM master（先 `sudo systemctl isolate multi-user.target`）。
    判定（O19）：本报告 `baseline_candidate.p95_ms` ≥ 60 ms ⇒ 自动触发 §13 重议（结论须在 M2 出口前）；
    < 60 ms ⇒ 登记「不触发」并关闭 O19。
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo_root)
    rounds = max(1, int(args.rounds))
    # **（v3.86 追加 95）** 同 `realtime`：判定窗长缺省 = `metrics.window_frames`（键空即报错）
    mk = metrics_keys(args)
    frames = int(args.frames) or int(mk['window_frames'])
    per = []
    for r in range(rounds):
        tmp = os.path.join(tempfile.gettempdir(), f'bench_idle_r{r + 1}.json')
        cmd = [sys.executable, os.path.join(repo_root, 'main.py'), '--idle-link',
               '--frames', str(frames), '--report-json', tmp]
        if args.config:
            cmd += ['--config', args.config]
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.exists(tmp):
            rep = base_report(args, 'idle-link', 'frame')
            rep.update({'status': 'round_failed', 'round': r + 1, 'rc': proc.returncode,
                        'cmd': cmd, 'stdout_tail': proc.stdout.strip()[-1500:],
                        'stderr_tail': proc.stderr.strip()[-1500:],
                        'reason': 'TH4 取 DRM master 失败（先 `sudo systemctl isolate multi-user.target`）'
                                  '或链路启动失败'})
            return emit(rep, args.out)
        with open(tmp) as f:
            per.append(json.load(f))

    def col(key):
        return [p.get(key) for p in per]

    def med(vals):
        v = sorted(x for x in vals if x is not None)
        return v[len(v) // 2] if v else None

    if any(not p.get('idle_link') for p in per):
        raise SystemExit('idle-link 场景要求按空链路口径运行（`--idle-link`）——'
                         '报告未标 `idle_link=true`，禁止混入常态轮（§6.1 口径不可混填）')

    lat = [x for p in per for x in (p.get('lat_all_ms') or [])]
    lat_app = [x for p in per for x in (p.get('lat_app_all_ms') or [])]
    lat_sub = [x for p in per for x in (p.get('lat_submit_all_ms') or [])]
    frames_all = sum(int(p.get('frames') or 0) for p in per)
    flips_all = sum(int(p.get('flips') or 0) for p in per)
    paired_all = sum(int(p.get('paired') or 0) for p in per)
    end_to_end = window_stats_ms(lat)
    seg_app = window_stats_ms(lat_app)
    seg_sub = window_stats_ms(lat_sub)
    # O19 的触发判据用**本轮实测**（不是 config 里的既有值）：取三轮端到端 P95 的中位。
    p95_med = med(col('lat_p95_ms'))
    rep = base_report(args, 'idle-link', 'frame')
    rep.update({
        'status': 'ok',
        'entry': 'bench.py --scenario idle-link --breakdown'
                 '（每轮一个独立进程跑 `main.py --idle-link --report-json`）',
        'link': 'idle（关 TH2a / TH2b / OSD；保留 采集 → RGA 原图窗 → 画布合成 → pageflip，'
                'TH5 HDMI 镜像同构保留——它不是"增强"成本）',
        'rounds': rounds, 'frames_per_round': frames,
        'latency': end_to_end,
        'latency_segments': {'capture_to_submit': seg_app, 'submit_to_vblank': seg_sub},
        'fps': {'per_round': col('fps_active'), 'median': med(col('fps_active')),
                'steady_per_round': col('fps_steady'), 'steady_median': med(col('fps_steady')),
                'frames_total': frames_all,
                'note': '空链路轮的 fps **不是** §13「实时帧率」的判据对象（该判据针对常态链路）；'
                        '仅作口径随行的旁证'},
        'pairing_rate': paired_all / max(1, flips_all), 'paired_flips': paired_all,
        'seq_coverage': {'flips': flips_all, 'paired': paired_all,
                         'unpaired': flips_all - paired_all},
        'discarded_frames': {
            'flip_skipped': sum(int(p['counters'].get('flip_skipped', 0)) for p in per),
            'unpaired': flips_all - paired_all,
            'dropped_stale': sum(int(p['counters'].get('dropped_stale', 0)) for p in per),
            'lease_timeouts': sum(int(p['counters'].get('lease_timeouts', 0)) for p in per)},
        'panel_refresh_ms': med(col('panel_refresh_ms')),
        # **O19 登记用的候选值**：`metrics.idle_link_baseline` 由它回填（两段 + 端到端 + 条件随行）。
        'baseline_candidate': {
            'p50_ms': end_to_end.get('p50_ms'), 'p95_ms': end_to_end.get('p95_ms'),
            'n': end_to_end.get('n'),
            'capture_to_submit_p50_ms': seg_app.get('p50_ms'),
            'capture_to_submit_p95_ms': seg_app.get('p95_ms'),
            'submit_to_vblank_p50_ms': seg_sub.get('p50_ms'),
            'submit_to_vblank_p95_ms': seg_sub.get('p95_ms'),
            'per_round_p95_ms': col('lat_p95_ms'), 'per_round_p50_ms': col('lat_p50_ms'),
            'panel_refresh_ms': med(col('panel_refresh_ms')),
            'timestamp_source': 'EOF（§6.5：驱动只给 EOF ⇒ 本基线为 EOF→vblank 口径；'
                                '`metrics.timestamp_source` 定稿属 M1 十键，本场景不擅自定稿）',
            'rounds': rounds, 'frames_per_round': frames,
        },
        # O19 的**触发判据**就地判定（阈值 60 ms = §13 上限 80 ms 的 75%，见 §13 / measurements §1）。
        'o19_trigger': {'threshold_p95_ms': 60.0, 'measured_median_p95_ms': p95_med,
                        'reopen_required': None if p95_med is None else (p95_med >= 60.0)},
        'idle_link_baseline': per[0].get('idle_link_baseline'),
        'rounds_detail': [{'round': i + 1, 'fps_active': p.get('fps_active'),
                           'boot_s': p.get('boot_s'), 'wall_s': p.get('wall_s'),
                           'frames': p.get('frames'), 'flips': p.get('flips'),
                           'lat_p50_ms': p.get('lat_p50_ms'), 'lat_p95_ms': p.get('lat_p95_ms'),
                           'lat_app_p50_ms': p.get('lat_app_p50_ms'),
                           'lat_app_p95_ms': p.get('lat_app_p95_ms'),
                           'lat_submit_p50_ms': p.get('lat_submit_p50_ms'),
                           'pairing_rate': p.get('pairing_rate'),
                           'stream_on_count': p.get('stream_on_count'),
                           'estimates': (p.get('counters') or {}).get('estimates')}
                          for i, p in enumerate(per)],
        'notes': ['口径来源 = §6.1 的 v3.4「必读」段（*关闭增强，只跑"采集 → RGA 原图窗 → MIPI 显示"*）'
                  '与 `docs/measurements.md` §1（O19）；**本口径不得与常态轮混填**',
                  '§6.5：时钟只用 `mono_ns`；配对按 `seq` 回填；延迟两段随报告给出；'
                  '≥300 帧滑窗 × 3 轮（每轮一个独立进程 ⇒ `stream_on_count` 恒为 1）',
                  'O19 判定：`o19_trigger` —— P95 ≥ 60 ms 则自动触发 §13 重议（结论须在 M2 出口前），'
                  '否则登记「不触发」并关闭 O19；回填落点 = `config.yaml` 的 `metrics.idle_link_baseline`'
                  ' + `docs/measurements.md` §1 三列',
                  '`counters.estimates` 应恒为 0（空链路不跑 TH2a）；非 0 即口径被破，须先查因',
                  '计量四键（v3.86 追加 95）：判定窗长 = `metrics.window_frames`（键值唯一来源）'],
        **policy_block(per, mk),
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
    `--impl native` 走交付路径 = `realtime_apply`（C/NEON 单遍融合，§3.3「非优化项」）。
    """
    if args.impl == 'native':
        return _apply_micro_native(args)
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
                          '不得用于 §6.1「曲线应用 ≤20 ms」的达标判定（阈值见 v3.26 裁定 O33）']})
    return emit(rep, args.out)


def _apply_micro_native(args):
    """交付路径（`realtime_apply`，C/NEON 单遍融合）的函数级打点。

    曲线场几何 / 精度取自 §10.4 键表（`pipeline.curve_level` → `curve_geometry.effective`、
    `pipeline.curve_field_precision`）；**迭代次数 8 是 §3.3 契约值，非 config 键**。
    同时做一次 NEON vs 标量的逐位对账（§11.2 的数值一致性检查）。
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo_root)
    try:
        import config as starlight_config
        import realtime_apply as ra
    except ImportError as e:
        raise SystemExit(f'apply-micro --impl native 需要交付链路（realtime_apply + config）：{e}')

    cfg = starlight_config.load()
    precision = cfg.get('pipeline.curve_field_precision')
    level = cfg.get('pipeline.curve_level')
    effective = (cfg.get('pipeline.curve_geometry.effective') or {}).get(level)
    if not effective:
        raise SystemExit(f'config 的 curve_geometry.effective 缺档位 {level}')
    cw, ch = int(effective[0]), int(effective[1])

    ra.shutdown()
    rc = ra.configure_from_config(cfg, preserve_ratio=args.preserve_ratio)
    if rc != 0:
        raise SystemExit(f'realtime_apply.configure_from_config 返回 {rc}')

    rgb = ra.synth_rgb(args.w, args.h, seed=5)
    curve = ra.synth_curve(cw, ch, precision, seed=13)
    dst = bytearray(args.w * args.h * 3)
    ref = bytearray(args.w * args.h * 3)

    for _ in range(10):                                   # 预热
        ra.apply_window(rgb, curve, dst, args.w, args.h)
    samples = []
    for _ in range(args.runs):
        t0 = mono_ns()
        ra.apply_window(rgb, curve, dst, args.w, args.h)
        samples.append(mono_ns() - t0)

    ra.apply_scalar(rgb, curve, ref, args.w, args.h)
    mismatch = sum(1 for i in range(len(dst)) if dst[i] != ref[i])
    max_abs = max((abs(dst[i] - ref[i]) for i in range(len(dst))), default=0)

    rep = base_report(args, 'apply-micro', 'model')
    rep.update({
        'status': 'ok',
        'impl': 'native(C/NEON single-pass fusion)',
        'impl_id': ra.impl_id(),
        'apply_domain': f'{args.w}x{args.h}',
        'curve_geometry': f'{cw}x{ch}',
        'curve_field_precision': precision,
        'curve_iters': ra.CURVE_ITERS,
        'apply': window_stats(samples),
        'selfcheck_neon_vs_scalar': {'mismatch_bytes': mismatch, 'max_abs': max_abs,
                                     'total': len(dst)},
        'notes': ['交付路径实测；单遍融合为 §3.3 硬门槛（未融合即判未达标）',
                  'budget_ref: §6.1 曲线应用 @512×288 ×8 iter ≤20 ms（v3.26 裁定 O33）'],
    })
    return emit(rep, args.out)


def scenario_osd_micro(args):
    """O44：OSD 指标区三项计算成本（**统计 / 曲线图 / 直方图**）的函数级打点。

    §3.4 的统计为逐像素操作，**策略已定稿**（降采样 4× + 降频 1 Hz）；本场景只补**实测耗时**，
    回填落点 = §3.4 + §6.1（O44 裁定：**不新开 `docs/measurements.md` 行**）。
    本场景**不建 DRM/采集链路**：画布与曲线场用 dma-buf 直接分配 ⇒ **不含 pageflip 与 RGA 画布合成**。
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo_root)
    try:
        import struct
        import config as starlight_config
        import curve_estim as curve_mod                 # 几何单源（O58）
        import osd as osd_mod
        import _starlight_native as _n
    except ImportError as e:
        raise SystemExit(f'osd-micro 需要交付链路（osd.py + _starlight_native + config）：{e}')

    cfg = starlight_config.load()
    g = curve_mod.resolve_geometry(cfg)                 # **几何唯一来源**（O58：禁止第二处常量）
    cw, ch = int(g['canvas_w']), int(g['canvas_h'])
    plot_y, plot_h = int(g['plot_y']), int(g['plot_h'])
    win_w, win_h = 512, 288                             # 窗分辨率（§3.4 双窗）
    level = cfg.get('pipeline.curve_level')
    effective = (cfg.get('pipeline.curve_geometry.effective') or {}).get(level)
    if not effective:
        raise SystemExit(f'config 的 curve_geometry.effective 缺档位 {level}')
    eff_w, eff_h = int(effective[0]), int(effective[1])
    precision = cfg.get('pipeline.curve_field_precision')

    r = osd_mod.OsdRenderer(cfg, cw, ch)
    canvas = _n.alloc(cw * ch * 3)
    raw = _n.alloc(win_w * win_h * 3)
    enh = _n.alloc(win_w * win_h * 3)
    raw.write(bytes((i * 7) & 0xFF for i in range(win_w * win_h * 3)))          # 确定性图案
    enh.write(bytes((i * 11 + 40) & 0xFF for i in range(win_w * win_h * 3)))
    n = 3 * eff_w * eff_h
    field = _n.alloc(n * (2 if precision == 'fp16' else 1))
    if precision == 'fp16':
        field.write(struct.pack(f'<{n}e', *[((i % 256) / 255.0) for i in range(n)]))
    else:
        field.write(bytes((i * 13) & 0xFF for i in range(n)))
    st = {'curve_field': field, 'curve_w': eff_w, 'curve_h': eff_h,
          'curve_precision': precision,
          # **（v3.86 裁定 O105）** 实时工作点需要 `mean_orig`（0..255 域）；给出确定性非零值，
          # 使本场景的计时**含**工作点标记的绘制成本。
          'mean_orig': 13.7}

    period = r.stats.period_ns
    raw_b, enh_b = raw.to_bytes(), enh.to_bytes()
    stats_ns = []
    for i in range(args.runs):                          # ① 统计（强制绕过 1 Hz 节流以便逐次计时）
        t0 = mono_ns()
        r.stats.maybe_update(raw_b, enh_b, win_w, win_h, now_ns=(i + 1) * period)
        stats_ns.append(mono_ns() - t0)

    half = cw // 2
    curve_ns, hist_ns = [], []
    for _ in range(args.runs):                          # ② 曲线图 / ③ 直方图
        t0 = mono_ns()
        r._curve_plot(canvas, 0, plot_y, half, plot_h, st)
        curve_ns.append(mono_ns() - t0)
        t0 = mono_ns()
        r._histogram(canvas, half, plot_y, half, plot_h)
        hist_ns.append(mono_ns() - t0)

    rep = base_report(args, 'osd-micro', 'model')
    rep.update({
        'status': 'ok',
        'canvas': f'{cw}x{ch}',
        'stats_domain': f'{win_w}x{win_h}',
        'stats_decimate': r.decimate,
        'stats_hz': r.stats_hz,
        'stats_samples': r.stats.samples,
        'curve_geometry': f'{eff_w}x{eff_h}',
        'curve_field_precision': precision,
        'osd_stats': window_stats(stats_ns),
        'osd_curve_plot': window_stats(curve_ns),
        'osd_histogram': window_stats(hist_ns),
        'notes': ['O44 三项实测（统计 / 曲线图 / 直方图）；落点 = §3.4 + §6.1',
                  'scope=model：仅函数级成本，**不含 pageflip 与 RGA 画布合成** ⇒ '
                  '不得直接作为 §6.1「MIPI 画布合成 + OSD」行的达标依据（该行拆分/重排待裁定）'],
    })
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
                   choices=['realtime', 'hq', 'step-response', 'idle-link', 'model-infer',
                            'apply-micro', 'osd-micro'])
    p.add_argument('--out', default='', help='报告输出路径（默认只打印）')
    p.add_argument('--runs', type=int, default=30,
                   help='apply-micro 的重复次数；hq 场景 = **触发次数**（§13 要求 ≥10）；'
                        'step-response = **阶跃次数**（§13「切换响应」行要求 ≥10）')
    p.add_argument('--config', default='', help='config.yaml 路径（缺省 = 仓库根）')
    p.add_argument('--rounds', type=int, default=3,
                   help='realtime 场景的轮数（§13 复现方式 = ≥300 帧滑窗 × **3 轮**；每轮独立进程）')
    p.add_argument('--frames', type=int, default=0,
                   help='hq / realtime 场景的每轮帧数（0 = hq 注入触发跑完即收尾 / realtime 与 idle-link '
                        '取 `metrics.window_frames`——§6.5 判定窗下界，键值唯一来源）')
    p.add_argument('--trigger-interval-s', type=float, default=15.0,
                   help='hq 场景的触发注入间隔（秒；须 ≥ HQ 单次耗时）')
    p.add_argument('--dump-canvas-dir', default='',
                   help='hq 场景导图目录（链路原样 PNG；缺省 + `--capture-mipi` 时落 review/hq_path_canvas）')
    p.add_argument('--breakdown', action='store_true', help='延迟分两段上报（§6.5）')
    # **[验证用钩子] §8.1 ①-2 的子项拆分（v3.86 追加 96）** —— 透传到 `main.py`（**非 config 键**）。
    # 变体约定（`docs/evidence/subsplit-attribution.md`）：
    #   V0 = `--no-est --no-apply --no-osd`（应与既有 `idle-link` 一致，作交叉校验）
    #   V1 = `--no-apply --no-osd`（只 TH2a）/ V2 = `--est-first N --no-osd` + `--skip-first N`（只 TH2b）
    #   V3 = `--no-osd`（TH2a + TH2b）/ V4 = 无（全开）
    p.add_argument('--no-est', action='store_true', help='V*：透传 main.py（不请求估计）')
    p.add_argument('--est-first', type=int, default=0, help='V*：只前 N 帧请求估计，之后冻结场')
    p.add_argument('--no-apply', action='store_true', help='V*：透传 main.py（跳过 TH2b 应用）')
    p.add_argument('--no-osd', action='store_true', help='V*：透传 main.py（OSD 不初始化）')
    # **[验证用钩子] OSD 瘦身两级（v3.86 追加 97）**：`--osd-status-only` 只画状态行（去两图）；
    # `--osd-no-stats` 连统计也不做。用于把 OSD 的 ①-2 增量拆成「图示绘制」与「统计」。
    p.add_argument('--osd-status-only', action='store_true', help='V*：OSD 只画状态行（去两图与图名）')
    p.add_argument('--osd-no-stats', action='store_true', help='V*：OSD 连统计也不做（均值保持初值）')
    p.add_argument('--skip-first', type=int, default=0,
                   help='V*：每轮**剔除前 N 帧样本**后再统计（与 `--est-first N` 配对；'
                        '两口径都随报告给出，剔除属**事先声明**的预热前缀，非事后筛选）')
    # **[A/B 开关] 保比语义**（交付实现选择，**非 config 键**）：bench 开关 → 两处透传
    #   · `realtime` 场景 → `main.py --no-preserve-ratio`（TH2b 应用语义，默认保比）；
    #   · `apply-micro --impl native` → `configure_from_config(preserve_ratio=...)`。
    # **默认保比语义**；本反向开关切回旧逐通道语义，仅供 A/B。
    p.add_argument('--no-preserve-ratio', action='store_false', dest='preserve_ratio', default=True,
                   help='[A/B 开关] 切回旧逐通道语义（默认走保比语义；本开关仅供 A/B）')
    p.add_argument('--baseline', action='store_true',
                   help='随报告给出「端到端 − `metrics.idle_link_baseline`」的归因差值'
                        '（§13「端到端延迟」行的复现命令；基线缺失时只标注）')
    p.add_argument('--seq-log', action='store_true', help='记 seq 级明细（HQ 响应）')
    # **[step-response 场景专用]（v3.86 裁定 O143）** 透传给 `main.py --step-exp-*`（**非 config 键**）
    p.add_argument('--step-interval-s', type=float, default=3.0,
                   help='step-response：阶跃间隔（秒；须 ≥ 收敛时间 + 余量）')
    p.add_argument('--step-tail-s', type=float, default=3.0,
                   help='step-response：末次阶跃后的尾段（秒；用于取新稳态参考）')
    p.add_argument('--step-low', type=int, default=0, help='step-response：低档曝光（0 = 取 camera.exposure_us）')
    p.add_argument('--step-high', type=int, default=0, help='step-response：高档曝光（0 = 4× 低档）')
    p.add_argument('--step-subdev', default='', help='step-response：曝光控件所在 subdev（缺省自动探测）')
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
    if args.scenario == 'hq':
        return scenario_hq(args)
    if args.scenario == 'realtime':
        return scenario_realtime(args)
    if args.scenario == 'idle-link':
        return scenario_idle_link(args)
    if args.scenario == 'step-response':
        return scenario_step_response(args)          # **（v3.86 裁定 O143）已接桩**
    if args.scenario in CHAIN_SCENARIOS:
        return scenario_chain(args)
    if args.scenario == 'model-infer':
        return scenario_model_infer(args)
    if args.scenario == 'apply-micro':
        return scenario_apply_micro(args)
    if args.scenario == 'osd-micro':
        return scenario_osd_micro(args)
    p.error('未实现的场景')


if __name__ == '__main__':
    sys.exit(main())
