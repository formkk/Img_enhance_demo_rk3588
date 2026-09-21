#!/usr/bin/env python3
"""ISP IQ **两套档位**（采集档 / 演示档）的生成、校验与切换 —— 主基准 §15 **O62** 的实现。

**为什么需要它**：`rkaiq_3A_server` 按 **sensor 名**匹配 IQ，而它**硬编码 `/etc/iqfiles/`**
且**仅支持 `--silent`**（板上实测，见 `docs/evidence/M1-collect-tool-board.md` §10.7）
⇒ **同一 sensor 名下只能存在一份 IQ** ⇒ 采集档与演示档**必须换文件 + 重启 3A**。

**两套档位的差异**（主基准 v3.75 裁定 O62 = 选项 (b)）：

| 档位 | 3DNR（`bayertnr_v2/TuningPara/enable`） | 用途 |
|---|---|---|
| **采集档** | **0（关）** | U1/U2/U3/U4 取"纯净底"（O40） |
| **演示档** | **保持出厂值（1，开）** | 运行期观感/降噪（§9.2、§3.1 v3.1 ⑧） |

**两档共同点**（主基准 v3.77 裁定 = 纯 IQ 路径，§9.5 ⑥）：均把
`ae_calib/CommCtrl/AecOpType` 置 `RK_AIQ_OP_MODE_MANUAL`，并把
`AecManualCtrl/LinearAE/{TimeValue, GainValue, IspDGainValue}` 设为目标值
⇒ **`rkaiq_3A_server` 启动即固定曝光/增益**，**不动服务、零新增进程**。

**取值纪律（硬约束）**：`build` **强制显式给出** `--exp-us` / `--gain` / `--fps`
（本工具**不臆造取值**，规则 `10-evidence.md` §2；与 `isp_owner.c` 的 `--gain` 同口径）。
**固定曝光/增益的最终取值 = M1 定稿项**（主基准 §10.4 `camera.*`、§15 **O63**：1/30 s 须待
1080p@30 锁定）。**取值 SSOT = IQ 文件**（§9.5 ⑦）⇒ 值只写在本工具生成的 IQ 里，
`config.yaml` 不再单独存值。

板上目录布局（`--sets-dir`，默认 `/etc/iqfiles_sets`）：

    /etc/iqfiles_sets/
    ├── stock/<sensor>.json     出厂基线（`import-stock` 落盘；`build` 的输入、`switch stock` 的还原源）
    ├── collect/<sensor>.json   采集档（3DNR 关）
    ├── demo/<sensor>.json      演示档（3DNR 开）
    └── manifest.json           两套的文件名 + sha256 + 生成参数（**防呆与指纹的唯一依据**）

用法（板上；`switch` 需 root）：

    sudo python3 tools/collect/iq_sets.py import-stock            # 首次：固化出厂基线
    sudo python3 tools/collect/iq_sets.py build --exp-us 33333 --gain 4.0 --fps 30
    python3 tools/collect/iq_sets.py status                       # 只读：当前 IQ 是哪一档
    sudo python3 tools/collect/iq_sets.py switch collect          # 换档 + 重启 3A + 校验
    sudo python3 tools/collect/iq_sets.py switch stock            # 还原出厂

依赖：python3 标准库（与采集工具同口径，无第三方依赖）。
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

# ── 契约常量（不得自造；改动须先回写主基准）─────────────────────────────
IQ_DIR = '/etc/iqfiles'                      # `rkaiq_3A_server` 硬编码的加载目录（不可配置）
SETS_DIR = '/etc/iqfiles_sets'               # 两套档位的存放目录
SENSOR_GLOB = 'imx415_'                      # 本板 sensor = IMX415（文件名前缀）
SCHEMA = 'iqsets/v1'
SVC = 'rkaiq_3A.service'

# 纯 IQ 路径要写的三个手动档位（主基准 §9.5 ⑥；单位见 `aec_head.h`）
MANUAL_OP = 'RK_AIQ_OP_MODE_MANUAL'          # `RK_AIQ_OP_MODE_MANUAL = 2`
P_OPMODE = ('AecOpType',)
P_TIME = ('AecManualCtrl', 'LinearAE', 'TimeValue')          # f32，**秒**
P_GAIN = ('AecManualCtrl', 'LinearAE', 'GainValue')          # f32，**线性倍数**
P_DGAIN = ('AecManualCtrl', 'LinearAE', 'IspDGainValue')     # f32，**线性倍数**
P_EN = [('AecManualCtrl', 'LinearAE', k)
        for k in ('ManualTimeEn', 'ManualGainEn', 'ManualIspDgainEn')]
P_NR3D = ('bayertnr_v2', 'TuningPara', 'enable')             # Bayer 时域 NR（O40 / O62）
# AE 的帧率档（§9.5 ④ v3.79）：**rkaiq 的曝光上界按它算**，与 sensor 的 `exposure` max 无关
# ⇒ 不设 30 时，即便物理 VMAX 已抬到 4485 行，`TimeValue=0.033333` 仍被钳到 ≈2224 行（板上实测）。
P_FPSFIX = ('AecFrameRateMode', 'isFpsFix')                  # bool：是否固定帧率
P_FPSVAL = ('AecFrameRateMode', 'FpsValue')                  # u8：帧率值（0 = 未指定 ⇒ 退到 sensor 默认）

# **手动 WB 锁定（O64）**：`wb_v21.control.mode` 由 AUTO 切到 MANUAL，并由
# `manualPara.cfg.mwbGain` 固定 [R, Gr, Gb, B]（线性倍数）。§9.5 早就要求「AWB 锁定」，
# 此前因 uAPI 路径（`awb_Lock`）不可验证而挂 O64；走 IQ 是**可复现、可审计**的做法。
# 枚举名取自 librkaiq（`strings /usr/lib/librkaiq.so | grep CALIB_WB_MODE_`）。
P_WBMODE = ('wb_v21', 'control', 'mode')                     # CALIB_WB_MODE_AUTO | _MANUAL
P_MWBMODE = ('wb_v21', 'manualPara', 'mode')                 # 手动 WB 子模式
P_WBGAIN = ('wb_v21', 'manualPara', 'cfg', 'mwbGain')        # f32[4]：R / Gr / Gb / B
WB_AUTO = 'CALIB_WB_MODE_AUTO'
WB_MANUAL = 'CALIB_WB_MODE_MANUAL'
MWB_WBGAIN = 'CALIB_MWB_MODE_WBGAIN'

SETS = ('collect', 'demo')


# ---------------------------------------------------------------- 通用：按路径后缀查/改

def find_paths(node, tail, prefix=()):
    """递归找出所有以 `tail` 结尾的路径 → [(path_tuple, value)]（不依赖具体 scene 下标）。"""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = prefix + (k,)
            if len(p) >= len(tail) and p[-len(tail):] == tail:
                out.append((p, v))
            out.extend(find_paths(v, tail, p))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(find_paths(v, tail, prefix + (i,)))
    return out


def get_by_path(node, path):
    cur = node
    for k in path:
        cur = cur[k]
    return cur


def set_by_path(node, path, value):
    cur = node
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = value


def find_sensor_file(iq_dir):
    """在 `iq_dir` 下按前缀找本板 sensor 的 IQ 文件（唯一）。"""
    if not os.path.isdir(iq_dir):
        raise SystemExit('ERROR: 目录不存在：%s' % iq_dir)
    hits = sorted(f for f in os.listdir(iq_dir)
                  if f.startswith(SENSOR_GLOB) and f.endswith('.json'))
    if len(hits) != 1:
        raise SystemExit('ERROR: %s 下前缀 %r 的 IQ 文件应恰有 1 个，实得 %d 个：%s'
                         % (iq_dir, SENSOR_GLOB, len(hits), hits))
    return hits[0]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def manifest_path(sets_dir):
    return os.path.join(sets_dir, 'manifest.json')


def load_manifest(sets_dir):
    p = manifest_path(sets_dir)
    if not os.path.exists(p):
        raise SystemExit('ERROR: 未找到 %s —— 请先执行 `import-stock` 与 `build`' % p)
    with open(p) as f:
        return json.load(f)


def save_manifest(sets_dir, m):
    os.makedirs(sets_dir, exist_ok=True)
    with open(manifest_path(sets_dir), 'w') as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
        f.write('\n')


# ---------------------------------------------------------------- 子命令

def cmd_import_stock(a):
    """把当前 `/etc/iqfiles/` 的 IQ 固化为**出厂基线**（`build` 的输入、`switch stock` 的还原源）。"""
    src_name = find_sensor_file(a.iq_dir)
    src = os.path.join(a.iq_dir, src_name)
    dst_dir = os.path.join(a.sets_dir, 'stock')
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, src_name)
    if os.path.exists(dst) and not a.force:
        raise SystemExit('ERROR: %s 已存在（出厂基线不可随意覆盖）。'
                         '确认要重设请加 --force' % dst)
    shutil.copy2(src, dst)
    m = {'schema': SCHEMA, 'sensor_file': src_name,
         'stock': {'sha256': sha256_file(dst), 'imported_utc': now_utc()},
         'sets': {}}
    save_manifest(a.sets_dir, m)
    print('[stock] %s → %s\n[stock] sha256 = %s'
          % (src, dst, m['stock']['sha256']))
    print('[stock] 已写入 %s（注意：这会清空原有 sets 记录，须重新 build）'
          % manifest_path(a.sets_dir))


def resolve(doc):
    """把各目标**后缀**解析为本文档内的**绝对路径**列表（不依赖具体 scene 下标）。

    返回 {tail: [abs_path, ...]}；`P_EN` 逐项、`P_NR3D` 等按各自后缀查。
    """
    tails = [P_OPMODE, P_TIME, P_GAIN, P_DGAIN, P_NR3D, P_FPSFIX, P_FPSVAL,
             P_WBMODE, P_MWBMODE, P_WBGAIN] + list(P_EN)
    return {t: [p for p, _ in find_paths(doc, t)] for t in tails}


def require(doc, tails_what):
    """校验关键路径存在（缺任一即中止）——避免 `set_by_path` 改到错误位置或静默失效。"""
    tg = resolve(doc)
    for t, what in tails_what:
        if not tg[t]:
            raise SystemExit('ERROR: IQ 中未找到 %s（%s）—— schema 与预期不符，已中止'
                             % ('/'.join(str(x) for x in t), what))
    return tg


def _build_one(src_json, out_dir, name, exp_us, gain, isp_dgain, nr3d, fps,
               wb_lock=None, wb_gain=None):
    """由出厂基线派生一套档位；返回 (落盘路径, 文档)。

    `nr3d=None` = 保持原值；`wb_lock=None` = WB 保持原值（`'manual'` 锁到 `mwbGain`，
    `'auto'` 强制自动）；`wb_gain=None` = 用 IQ 里既有的 `mwbGain`（不臆造取值）。
    """
    d = json.loads(json.dumps(src_json))          # 深拷贝
    tg = require(d, [(P_OPMODE, 'AecOpType'), (P_TIME, 'TimeValue'),
                     (P_GAIN, 'GainValue'), (P_DGAIN, 'IspDGainValue'),
                     (P_NR3D, '3DNR 使能'), (P_FPSFIX, 'AE 帧率固定位'),
                     (P_FPSVAL, 'AE 帧率值')] + [(t, t[-1]) for t in P_EN])

    # ① 纯 IQ 路径：启动即固定曝光/增益（§9.5 ⑥）——按**解析出的绝对路径**逐个写入
    for p in tg[P_OPMODE]:
        set_by_path(d, p, MANUAL_OP)
    for p in tg[P_TIME]:
        set_by_path(d, p, exp_us / 1e6)           # TimeValue 单位 = 秒
    for p in tg[P_GAIN]:
        set_by_path(d, p, gain)                   # 线性倍数
    for p in tg[P_DGAIN]:
        set_by_path(d, p, isp_dgain)              # 线性倍数（ISP 数字增益）
    for t in P_EN:                                # 三个使能位（出厂本就是 1，此处确保）
        for p in tg[t]:
            set_by_path(d, p, 1)
    # ①' AE 帧率档（§9.5 ④ v3.79）：**rkaiq 的曝光上界按帧率算**——不设 30 则 1/30 s 被钳到 ≈2224 行
    for p in tg[P_FPSFIX]:
        set_by_path(d, p, 1)
    for p in tg[P_FPSVAL]:
        set_by_path(d, p, fps)

    # ② 3DNR（O40：采集期关 / O62：演示档保持出厂值）
    if nr3d is not None:
        for p in tg[P_NR3D]:
            set_by_path(d, p, nr3d)

    # ③ 手动 WB 锁定（O64）：切 `control.mode`，手动档下再固定 `mwbGain`
    if wb_lock is not None:
        wb_tg = {t: [p for p, _ in find_paths(d, t)] for t in (P_WBMODE, P_MWBMODE, P_WBGAIN)}
        for t in (P_WBMODE, P_MWBMODE, P_WBGAIN):
            if not wb_tg[t]:
                raise SystemExit('ERROR: IQ 中未找到 %s —— 无法锁 WB（schema 与预期不符，已中止）'
                                 % '/'.join(str(x) for x in t))
        for p in wb_tg[P_WBMODE]:
            set_by_path(d, p, WB_MANUAL if wb_lock == 'manual' else WB_AUTO)
        if wb_lock == 'manual':
            for p in wb_tg[P_MWBMODE]:
                set_by_path(d, p, MWB_WBGAIN)
            if wb_gain is not None:
                for p in wb_tg[P_WBGAIN]:
                    set_by_path(d, p, list(wb_gain))

    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, name)
    with open(out, 'w') as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.write('\n')
    return out, d


def cmd_build(a):
    """由出厂基线生成两套档位，并写 manifest（文件名 + sha256 + 生成参数）。"""
    m = load_manifest(a.sets_dir)
    name = m['sensor_file']
    stock = os.path.join(a.sets_dir, 'stock', name)
    if not os.path.exists(stock):
        raise SystemExit('ERROR: 未找到出厂基线 %s —— 请先执行 `import-stock`' % stock)

    with open(stock) as f:
        src = json.load(f)

    # 生成前先核对出厂基线里**关键路径存在**（否则写入会落到错误位置或静默失效）
    wb_lock = None if a.wb_lock == 'keep' else a.wb_lock
    wb_gain = None
    if a.wb_gain:
        try:
            wb_gain = [float(v) for v in a.wb_gain.split(',')]
        except ValueError:
            raise SystemExit('ERROR: --wb-gain 须为形如 "R,Gr,Gb,B" 的四个数')
        if len(wb_gain) != 4:
            raise SystemExit('ERROR: --wb-gain 须为 4 个线性倍数（R,Gr,Gb,B），实得 %d 个' % len(wb_gain))
    if wb_gain is not None and wb_lock != 'manual':
        raise SystemExit('ERROR: --wb-gain 仅在 --wb-lock manual 下有意义')

    req = [(P_OPMODE, 'AecOpType'), (P_TIME, 'TimeValue'),
           (P_GAIN, 'GainValue'), (P_DGAIN, 'IspDGainValue'),
           (P_NR3D, '3DNR 使能（O40「采集期关 3DNR」的前提）'),
           (P_FPSFIX, 'AE 帧率固定位（§9.5 ④：曝光上界按它算）'),
           (P_FPSVAL, 'AE 帧率值')] + [(t, t[-1]) for t in P_EN]
    if wb_lock is not None:
        req += [(P_WBMODE, 'WB 模式（O64 锁 WB 的前提）'),
                (P_MWBMODE, '手动 WB 子模式'),
                (P_WBGAIN, '手动 WB 增益 mwbGain')]
    tg = require(src, req)
    nr_stock = sorted({v for _, v in find_paths(src, P_NR3D)})
    if len(nr_stock) != 1:
        print('[build] WARN: 出厂基线的 %s 取值不唯一：%s（演示档将保持各自原值）'
              % ('/'.join(P_NR3D), nr_stock), file=sys.stderr)

    sets = {}
    for s in SETS:
        nr3d = 0 if s == 'collect' else None      # 采集档关 3DNR；演示档保持出厂
        out, d = _build_one(src, os.path.join(a.sets_dir, s), name,
                            a.exp_us, a.gain, a.isp_dgain, nr3d, a.fps, wb_lock, wb_gain)
        tg2 = resolve(d)                          # 用**绝对路径**回读，确认写入生效
        sets[s] = {
            'sha256': sha256_file(out),
            'file': name,
            'exp_us': a.exp_us,
            'gain': a.gain,
            'isp_dgain': a.isp_dgain,
            'fps': a.fps,
            'nr3d_enable': sorted({v for _, v in find_paths(d, P_NR3D)}),
            'aec_op_mode': sorted({get_by_path(d, p) for p in tg2[P_OPMODE]}),
            'aec_frame_rate_mode': sorted({(get_by_path(d, p1), get_by_path(d, p2))
                                           for p1 in tg2[P_FPSFIX] for p2 in tg2[P_FPSVAL]}),
            'time_value_s': sorted({get_by_path(d, p) for p in tg2[P_TIME]}),
            'gain_value': sorted({get_by_path(d, p) for p in tg2[P_GAIN]}),
            'built_utc': now_utc(),
        }
        if tg2[P_WBMODE]:                          # O64：WB 模式与手动增益随行入 manifest
            sets[s]['wb_mode'] = sorted({get_by_path(d, p) for p in tg2[P_WBMODE]})
        if tg2[P_WBGAIN]:
            sets[s]['wb_gain'] = [list(get_by_path(d, p)) for p in tg2[P_WBGAIN]]
        print('[build] %-7s → %s\n[build]   sha256=%s  nr3d=%s  wb=%s %s  AecOpType=%s  '
              'TimeValue=%s s  GainValue=%s  (isFpsFix,FpsValue)=%s'
              % (s, out, sets[s]['sha256'], sets[s]['nr3d_enable'],
                 sets[s].get('wb_mode'), sets[s].get('wb_gain'),
                 sets[s]['aec_op_mode'], sets[s]['time_value_s'], sets[s]['gain_value'],
                 sets[s]['aec_frame_rate_mode']))

    m['sets'] = sets
    save_manifest(a.sets_dir, m)
    print('[build] manifest = %s' % manifest_path(a.sets_dir))
    print('[build] 注意：固定曝光/增益的**最终取值 = M1 定稿项**（O63）；'
          '`--fps` 与 `--exp-us` 必须一致（30 fps ⇒ 1/30 s），否则 rkaiq 会把曝光钳回上界。')


def cmd_status(a):
    """只读：报告 `/etc/iqfiles/` 当前 IQ 与哪一档匹配（**防呆的唯一依据**）。"""
    m = load_manifest(a.sets_dir)
    name = m['sensor_file']
    cur = os.path.join(a.iq_dir, name)
    if not os.path.exists(cur):
        print('cur:  MISSING (%s)' % cur)
        print('set:  unknown')
        return 2
    h = sha256_file(cur)
    print('cur:  %s\n      sha256 = %s' % (cur, h))
    tag = 'unknown'
    if h == m.get('stock', {}).get('sha256'):
        tag = 'stock'
    for s in SETS:
        if h == (m.get('sets', {}).get(s) or {}).get('sha256'):
            tag = s
    for s in SETS:
        e = m.get('sets', {}).get(s)
        if e:
            print('%-7s sha256 = %s  nr3d=%s  wb=%s  exp_us=%s  gain=%s  built=%s'
                  % (s, e['sha256'], e['nr3d_enable'], e.get('wb_mode'), e['exp_us'],
                     e['gain'], e['built_utc']))
    print('stock   sha256 = %s' % m.get('stock', {}).get('sha256'))
    print('set:  %s' % tag)
    return 0 if tag != 'unknown' else 2


def _is_active(svc=SVC):
    return subprocess.run(['systemctl', 'is-active', svc],
                          capture_output=True, text=True).stdout.strip() == 'active'


def _restart_3a(wait_s=10.0):
    """重启厂商 3A 服务，并**校验它真的起来了**。

    ⚠️ 实测教训（2026-09-19）：`systemctl restart` **返回 0 并不等于服务在跑**——
    本板观察到 `rkaiq_3A_server` 启动后随即自行退出（日志止于 `MessageParser process loop exit!`），
    而 `restart` 仍报成功 ⇒ **必须回读 `is-active`**，否则 `switch` 会误报"已重启 ✓"。
    根因**未定**（疑与"紧接上一次重启 / 刚结束取流"有关）⇒ 只做校验 + 大声报错，**不做自动重试**
    （重试会掩盖问题）。
    """
    r = subprocess.run(['systemctl', 'restart', SVC], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit('ERROR: systemctl restart %s 失败（rc=%d）：%s'
                         % (SVC, r.returncode, (r.stderr or '').strip()))
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if _is_active():
            return True
        time.sleep(0.5)
    print('[switch] ✗ **重启后 %s 未处于 active**（等待 %.0f s）——新 IQ 未生效！\n'
          '[switch]   诊断：`systemctl status %s --no-pager` / '
          '`journalctl -u %s --no-pager -n 30`\n'
          '[switch]   恢复：`sudo python3 tools/collect/iq_sets.py switch stock`，'
          '或直接 `sudo systemctl start %s`'
          % (SVC, wait_s, SVC, SVC, SVC), file=sys.stderr)
    return False


def cmd_switch(a):
    """换档：备份当前 → 复制目标档 → 重启 3A → **回读校验 sha256**（O62 ⑤）。"""
    m = load_manifest(a.sets_dir)
    name = m['sensor_file']
    dst = os.path.join(a.iq_dir, name)

    if a.set_name == 'stock':
        src = os.path.join(a.sets_dir, 'stock', name)
        want = m['stock']['sha256']
    else:
        src = os.path.join(a.sets_dir, a.set_name, name)
        want = m['sets'][a.set_name]['sha256']
    if not os.path.exists(src):
        raise SystemExit('ERROR: 目标档不存在：%s（请先 `build`）' % src)

    cur_h = sha256_file(dst) if os.path.exists(dst) else None
    if cur_h == want:
        print('[switch] 当前已是 %s 档（sha256 一致），无需换档' % a.set_name)
        return 0

    # 前置提示：换档要重启 3A，正在取流时换会打断取流（不阻断，仅告警）
    busy = subprocess.run(['fuser', a.video_node], capture_output=True, text=True).stdout.strip()
    if busy:
        print('[switch] ⚠ %s 正被占用（pid: %s）——换档会重启 3A 并打断取流，建议先停取流'
              % (a.video_node, busy), file=sys.stderr)

    # 备份**放到 sets_dir 下**：绝不落在 IQ 加载目录里（旧版曾写到 <iq>.prev ⇒ 污染 /etc/iqfiles/）
    if cur_h is not None:
        bak_dir = os.path.join(a.sets_dir, '.prev')
        os.makedirs(bak_dir, exist_ok=True)
        bak = os.path.join(bak_dir, name)
        shutil.copy2(dst, bak)
        print('[switch] 已备份当前 → %s（sha256=%s）' % (bak, cur_h))

    # 防御：清掉历史版本误落在 IQ 目录内的 <name>.json.prev
    stray = dst + '.prev'
    if os.path.exists(stray):
        os.remove(stray)
        print('[switch] 已清理 IQ 加载目录内的历史残留：%s' % stray)

    shutil.copy2(src, dst)
    got = sha256_file(dst)
    if got != want:
        raise SystemExit('ERROR: 复制后 sha256 不符（want=%s got=%s）—— 已中止' % (want, got))
    print('[switch] %s → %s（sha256=%s ✓）' % (src, dst, got))

    if a.no_restart:
        print('[switch] --no-restart：**未重启 %s**，新 IQ 尚未生效（须手工重启）' % SVC)
        return 0

    if not _restart_3a():                        # 服务没起来 ⇒ 换档**未生效**，须当失败返回
        return 3
    got2 = sha256_file(dst)
    print('[switch] 已重启 %s 且服务 active；回读 sha256=%s（%s）'
          % (SVC, got2, '一致 ✓' if got2 == want else '**不一致 ✗**'))
    print('[switch] 效度检查（**只认 sensor subdev 回读**，§9.5 ③）：\n'
          '  v4l2-ctl -d %s --get-ctrl exposure,analogue_gain' % a.subdev)
    return 0


def main():
    ap = argparse.ArgumentParser(description='ISP IQ 两套档位管理（O62 实现；见文件头）')
    ap.add_argument('--sets-dir', default=SETS_DIR, help='两套档位存放目录（默认 %s）' % SETS_DIR)
    ap.add_argument('--iq-dir', default=IQ_DIR,
                    help='3A 的 IQ 加载目录（默认 %s；**不可配置**，仅为便于离线演练而留）' % IQ_DIR)
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('import-stock', help='把当前 /etc/iqfiles 的 IQ 固化为出厂基线')
    p.add_argument('--force', action='store_true', help='覆盖已有出厂基线（慎用）')
    p.set_defaults(fn=cmd_import_stock)

    p = sub.add_parser('build', help='由出厂基线生成采集档 / 演示档（值须显式给出）')
    p.add_argument('--exp-us', type=int, required=True,
                   help='固定曝光时间（微秒）。**必填**——本工具不臆造取值；'
                        'M1 定稿前只作测点（O63 阻塞到 1080p@30 锁定）')
    p.add_argument('--gain', type=float, required=True,
                   help='固定总增益（**线性倍数**，写 sensor 模拟段 GainValue）。**必填**')
    p.add_argument('--isp-dgain', type=float, default=1.0,
                   help='ISP 数字增益 IspDGainValue（线性倍数，默认 1.0）')
    p.add_argument('--fps', type=int, required=True,
                   help='AE 帧率档（写 `AecFrameRateMode.FpsValue`）。**必填**——rkaiq 的曝光上界'
                        '按它算（不设 30 则 1/30 s 被钳到 ≈2224 行）；须与 `--exp-us` 自洽'
                        '（30 ⇒ --exp-us 33333）。1080p@30 锁定后固定 30（§9.5 ④）')
    p.add_argument('--wb-lock', choices=('keep', 'auto', 'manual'), default='keep',
                   help='WB 模式（O64）：keep=保持出厂；manual=锁到 manualPara.cfg.mwbGain'
                        '（消除 AWB 在低照度下的落点漂移）；auto=强制自动')
    p.add_argument('--wb-gain', default='',
                   help='手动 WB 增益 "R,Gr,Gb,B"（线性倍数，4 个数；**仅在 --wb-lock manual 下**）。'
                        '留空 = 用 IQ 里既有的 mwbGain（不臆造取值）')
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser('status', help='只读：当前 IQ 是哪一档（防呆依据）')
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser('switch', help='换档 + 重启 3A + 校验')
    p.add_argument('set_name', choices=list(SETS) + ['stock'])
    p.add_argument('--no-restart', action='store_true', help='只换文件、不重启 3A（调试用）')
    p.add_argument('--video-node', default='/dev/video11',
                   help='取流占用检测用（默认 /dev/video11）')
    p.add_argument('--subdev', default='/dev/v4l-subdev2',
                   help='效度检查命令中使用的 sensor subdev（默认 /dev/v4l-subdev2）')
    p.set_defaults(fn=cmd_switch)

    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == '__main__':
    main()
