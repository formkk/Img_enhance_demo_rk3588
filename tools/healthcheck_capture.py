#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/healthcheck_capture.py —— 采集数据体检：IMX415 成对/单边 NV12 的**只读**量化

用途
----
采集落盘后的"体检"：给定一个采集目录（`tools/collect/capture_app.py` 的 `--outdir`），
自动发现成对帧（命名 `YYMMDD_<场景2位>_<帧序号>_{dark,gt}.nv12`；**配对键 = 场景号
`YYMMDD_<场景2位>`**，与 `capture_app.py` 的 `pair_id` / v3.69 裁定 O59 一致——**同场景的
dark 与 gt 即一对、不要求帧序号相同**，例：场景 03 的 `03_0001_dark` 与 `03_0002_gt` 仍为一对），
逐帧量化 + 逐项判定，
产出**审查用**报告。**本工具不产出任何验收依据**——验收只认 §13 的复现方式 + §11.2 的
回归项 + `bench.py` 报告（规则 `00-ssot.md` §3.5、`10-evidence.md` §1）。方法论见
`docs/design/dataset.md` 的「采集数据体检 SOP」。

跑在哪台机
----------
只需 `numpy` + `PIL`（**不依赖 cv2**）⇒ **开发机（2026-10-02 实测 numpy 1.24.4 + PIL 7.0.0）
与数据机均可跑**。
**回退说明**：若数据机 FRP 通道不通，可直接在开发机跑（先 `scp`/`rsync` 回采集目录），
**两处口径一致**（本文件即口径唯一来源）。

只读纪律
--------
本工具**只读**输入目录（`.nv12` 与同名 `.json`），**不写入、不删改任何采集产物**；
输出只落在 `--json-out` / `--png-out` / `--contact-sheet` 指定的路径（默认在输入目录下）。

量化口径（逐条写死，供复用与复现）
----------------------------------
1. NV12 布局：`1920x1080, stride 1920, uv_offset 2073600, frame_bytes 3110400`
   （第三批口径 = `--w/--h/--stride/--uv-offset/--frame-bytes` 的默认值）。
   Y 平面 = `h × stride` 取前 `w` 列；UV 平面自 `uv_offset` 起、`(h/2) × (w/2) × 2`（Cb,Cr 交错），
   2× 上采样对齐到 Y。
2. NV12→RGB：**BT.601 / full range (JFIF)**——`R = Y + 1.402·Cr'`、`G = Y − 0.344136·Cb' − 0.714136·Cr'`、
   `B = Y + 1.772·Cb'`（`Cb'/Cr' = Cb/Cr − 128`），`clip(0,255)`。
3. Y 统计：`mean / p50 / p95 / p99`；**近黑** = `Y ≤ 16` 占比；**饱和** = `Y == 255` 占比与 `Y ≥ 250` 占比。
   —— **①GT 饱和判定用单阈值**：`=255` 占比 `≤ 1.0 %` ⇒ 过；`> 1.0 %` ⇒ 不过（**2026-10-02 与 SOP ① 同步**，
   **取消"警戒"档**）；`≥250` 占比仅随行报数、不单独定判。
4. **暗部 SNR 代理**：32×32 块（忽略不足一块的边缘），取**块均值最低 25 %** 的块，报 `(块均值 / 块σ)` 的**中位**
   （块σ = 0 的块剔除）。**注意**：该代理与 1 Lux 的时域 `σ_eff` 口径**不同、不可并列**，只作方向性参照。
5. **全帧 `R/G`、`B/G`** 由 RGB 三通道均值给出——**只作档位分离的旁证，不得作色彩判据横比**
   （全帧均值比随画面内容变化；曾据此误判"严重偏色"，已撤回）。
6. **几何**：Y 平面 z-score 后 4× 均值降采样 → 归一化互相关 NCC；比较**零位移**与
   **±12 px（降采样域 = ±48 全分辨率 px）内最优**。`best_shift_full = best_shift_down × 4`。
   **判定口径（2026-10-02 修订）**：**零位移即最优**；或**次优（非零位移）相对零位移的 NCC
   增益 ≤ `geom_gain_ignore`（`0.02`）⇒ 增益可忽略 ⇒ 仍判"对齐"**。输出 `NCC(0,0)` / 最优位移 /
   增益 / 判定。**初版把"非零位移一律判不过"过严、与既有口径不符，已废**（2026-10-02 补记）。
7. **配对质量**：同对 NCC（dark vs gt，零位移）与**跨对 NCC**（不同 pair 之间）并列给出。
   **跨对基准口径（2026-10-02 补记②）**：跨对 NCC **只算"成对之间"**（**排除孤立帧**）——孤立帧
   若与某对同场景，其 NCC 语义上是"同对"、会**虚假抬高**跨对基准（前例：孤立 `03_0001 vs 03_0002`
   的 `0.662` 被误当跨对基准）。
   **判定口径（2026-10-02 修订）**：同对 `NCC(0,0) ≥ ncc_same_pass`（`0.5`）**且** > 同批跨对
   `max|NCC|` ⇒ 过；否则**配对异常 ⇒ 硬失败**（计入总体"不可用"）。初版第④项**未计入硬失败**
   （同对 NCC 异常不阻止"可用"结论），已修订（2026-10-02 补记）。
8. **sidecar 对账**：`illuminance.frame_y_mean` 与离线 `Y[::2, ::2].mean()`（1/4 抽样）之差。
9. **一致性**：各帧 `sensor_ctrl.readback` 的 `exposure / analogue_gain / vertical_blanking` 是否**逐帧相同**。
10. **sidecar 字段核查**：`illuminance.*` / `light_source.*` / `isp_fingerprint`（子项 + sha256 + 档位）/
    `sensor_ctrl` 是否存在且自洽。**为空 + `_pending` 不算问题**（照实报）。

非线性后处理纪律（**硬约束**）
-----------------------------
本工具产出的 PNG **只做规范的 CSC（BT.601/full）**，**不做任何 γ / 对比度 / 直方图均衡等非线性后处理**；
链路输出必须**原样归档、原样量化**（规则 `10-evidence.md` §2，v3.83 裁定 `_view` γ 副本已废弃）。
若暗画面看不清，正确做法是修 O54（显式 CSC/range）让链路输出本身正确，而非后处理。

用法
----
    python3 tools/healthcheck_capture.py --dir <采集目录>
    python3 tools/healthcheck_capture.py --dir <采集目录> --no-png --json-out /tmp/hc.json
"""
import argparse
import glob
import json
import os
import re
import sys

import numpy as np
from PIL import Image

# ── 第三批口径默认值（可 CLI 覆盖）──────────────────────────────────────────────
DEF_W, DEF_H = 1920, 1080
DEF_STRIDE = 1920
DEF_UV_OFFSET = 2073600
DEF_FRAME_BYTES = 3110400

DOWN = 4          # 几何降采样倍率
SEARCH = 12       # 几何搜索半径（降采样域 px）
BLK = 32          # 暗部块尺寸
NEAR_BLACK = 16   # Y ≤ 16 记近黑

# 命名 = `YYMMDD_<场景2位>_<帧序号4位>_dark|gt`
RE_NAME = re.compile(r'^(\d{6})_(\d{2})_(\d{4})_(dark|gt)$')

# ISP 指纹子项（**实测 7 项**：与 tools/collect/capture_app.py 的 `_iq_fp_scene()` 返回键一致，
# 亦与 config.yaml `isp.fingerprint` 的 7 个子键一致；此前的"8 子项"为计数错误，已在设计档更正）
FP_SUBITEMS = ('gamma_table', 'ccm', 'black_level', 'awb_lock', 'nr_2d', 'nr_3d', 'dpc')

# ── 本轮实取刻度（**可随集重标定**；不得当作永久规格，也不引入 §10.4 键）──────────
TH = {
    'gt_eq255_pass': 1.0,    # GT `Y==255` 占比（%）：≤ pass 过；> pass 不过（单阈值，2026-10-02 与 SOP ① 同步、取消"警戒"档）
    'gt_ge250_info': 2.0,    # GT `Y>=250` 占比（仅报数，不单独定判）
    'dark_snr_warn': 10.0,   # 暗部 SNR 代理中位（越低越差）：>= warn 过
    'dark_snr_fail': 5.0,    #                              < fail 不过
    'ncc_same_pass': 0.5,    # 同对零位移 NCC 下限（④）
    'ncc_cross_max': 0.3,    # 跨对零位移 NCC 上限（含符号绝对值；仅旁证）
    'geom_gain_ignore': 0.02,  # ② 非零位移最优相对零位移的 NCC 增益 ≤ 此值 ⇒ 判"对齐"（可忽略）
    'y_mean_diff_max': 1.0,  # sidecar frame_y_mean 与离线复算之差的容忍
}


# ── I/O ──────────────────────────────────────────────────────────────────────
def load_nv12(path, w, h, stride, uv_offset, frame_bytes):
    try:
        raw = np.fromfile(path, dtype=np.uint8)
    except OSError as e:
        return None, None, None, f"读取失败：{e}"
    if frame_bytes and raw.size != frame_bytes:
        return None, None, None, f"字节数 {raw.size} != frame_bytes {frame_bytes}"
    if raw.size < uv_offset + (h // 2) * w:
        return None, None, None, f"字节数 {raw.size} 不足以容纳 Y+UV 平面"
    y = raw[:h * stride].reshape(h, stride)[:, :w].astype(np.float32)
    uv = raw[uv_offset:uv_offset + (h // 2) * w].reshape(h // 2, w // 2, 2).astype(np.float32)
    cb = np.repeat(np.repeat(uv[:, :, 0], 2, axis=0), 2, axis=1)
    cr = np.repeat(np.repeat(uv[:, :, 1], 2, axis=0), 2, axis=1)
    return y, cb, cr, None


def nv12_to_rgb(y, cb, cr):
    """规范 BT.601 / full range（JFIF）。**不做任何非线性后处理**。"""
    cbb, crr = cb - 128.0, cr - 128.0
    r = y + 1.402 * crr
    g = y - 0.344136 * cbb - 0.714136 * crr
    b = y + 1.772 * cbb
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)


# ── 量化 ─────────────────────────────────────────────────────────────────────
def y_stats(y):
    f = y.ravel()
    return {
        'y_mean': float(f.mean()),
        'y_p50': float(np.percentile(f, 50)),
        'y_p95': float(np.percentile(f, 95)),
        'y_p99': float(np.percentile(f, 99)),
        'near_black_le16_pct': float((f <= NEAR_BLACK).mean() * 100.0),
        'eq255_pct': float((f == 255).mean() * 100.0),
        'ge250_pct': float((f >= 250).mean() * 100.0),
        'y_mean_sampled_q1': float(y[::2, ::2].mean()),  # sidecar 对账（1/4 抽样）
    }


def dark_snr_proxy(y, h, w):
    hb, wb = (h // BLK) * BLK, (w // BLK) * BLK
    blocks = y[:hb, :wb].reshape(hb // BLK, BLK, wb // BLK, BLK)
    means = blocks.mean(axis=(1, 3))
    stds = blocks.std(axis=(1, 3))
    thr = float(np.percentile(means, 25))
    mask = (means <= thr) & (stds > 0)
    ratios = means[mask] / stds[mask]
    return {
        'n_blocks': int(means.size),
        'n_dark25': int(mask.sum()),
        'block_mean_thr': thr,
        'snr_proxy_median': float(np.median(ratios)) if ratios.size else None,
        'snr_proxy_mean': float(ratios.mean()) if ratios.size else None,
    }


def rgb_ratio(rgb):
    r = rgb[:, :, 0].astype(np.float64).mean()
    g = rgb[:, :, 1].astype(np.float64).mean()
    b = rgb[:, :, 2].astype(np.float64).mean()
    return {'R_mean': r, 'G_mean': g, 'B_mean': b,
            'R_over_G': r / g if g else None, 'B_over_G': b / g if g else None}


def _ncc(a, b):
    a = a - a.mean()
    b = b - b.mean()
    sa = np.sqrt((a * a).mean())
    sb = np.sqrt((b * b).mean())
    if sa == 0 or sb == 0:
        return 0.0
    return float((a * b).mean() / (sa * sb))


def downsample_z(y, h, w):
    hb, wb = (h // DOWN) * DOWN, (w // DOWN) * DOWN
    d = y[:hb, :wb].reshape(hb // DOWN, DOWN, wb // DOWN, DOWN).mean(axis=(1, 3))
    d = d - d.mean()
    s = d.std()
    return d / s if s > 0 else d


def geometry(a_y, b_y, h, w):
    A, B = downsample_z(a_y, h, w), downsample_z(b_y, h, w)
    ncc0 = _ncc(A, B)
    best, best_shift = ncc0, (0, 0)
    for dy in range(-SEARCH, SEARCH + 1):
        for dx in range(-SEARCH, SEARCH + 1):
            if dy == 0 and dx == 0:
                continue
            ay0, ay1 = max(0, dy), A.shape[0] + min(0, dy)
            by0, by1 = max(0, -dy), B.shape[0] + min(0, -dy)
            ax0, ax1 = max(0, dx), A.shape[1] + min(0, dx)
            bx0, bx1 = max(0, -dx), B.shape[1] + min(0, -dx)
            v = _ncc(A[ay0:ay1, ax0:ax1], B[by0:by1, bx0:bx1])
            if v > best:
                best, best_shift = v, (dy, dx)
    return {
        'ncc_00': ncc0,
        'ncc_best': float(best),
        'best_shift_down': [int(best_shift[0]), int(best_shift[1])],
        'best_shift_full': [int(best_shift[0] * DOWN), int(best_shift[1] * DOWN)],
        'gain_best_minus_00': float(best - ncc0),
    }


# ── sidecar ──────────────────────────────────────────────────────────────────
def load_sidecar(path):
    if not os.path.exists(path):
        return None, 'sidecar 缺失'
    try:
        with open(path) as f:
            return json.load(f), None
    except Exception as e:  # noqa: BLE001
        return None, f'sidecar 解析失败：{e}'


def sidecar_report(sc, offline_sampled_y_mean, expect_fp_subitems=FP_SUBITEMS):
    """核查 sidecar 四组字段的存在性与自洽性。**为空 + `_pending` 不算问题**。"""
    rep = {'ok': True, 'notes': [], 'missing_groups': [], 'fp_subitems': None, 'fp_sha_count': None,
           'pending': [], 'frame_y_mean': None, 'y_mean_diff': None}
    if sc is None:
        rep['ok'] = False
        rep['missing_groups'] = ['<whole sidecar>']
        return rep
    for grp in ('illuminance', 'light_source', 'isp_fingerprint', 'sensor_ctrl'):
        if grp not in sc:
            rep['ok'] = False
            rep['missing_groups'].append(grp)
    ill = sc.get('illuminance') or {}
    if isinstance(ill, dict):
        rep['frame_y_mean'] = ill.get('frame_y_mean')
        if ill.get('frame_y_mean') is not None and offline_sampled_y_mean is not None:
            rep['y_mean_diff'] = abs(float(ill['frame_y_mean']) - offline_sampled_y_mean)
        if ill.get('lux_anchor') is None:
            rep['notes'].append('lux_anchor 为空（批次级锚点未录；`_pending` 中有则非问题）')
    fp = sc.get('isp_fingerprint') or {}
    scenes = fp.get('scenes') or []
    if scenes:
        keys = set(scenes[0].keys()) - {'scene_index', 'src'}
        rep['fp_subitems'] = sorted(keys)
        rep['fp_sha_count'] = sum(
            1 for k, v in scenes[0].items()
            if isinstance(v, dict) and any(s.endswith('sha256_16') for s in v)
        )
        missing = [k for k in expect_fp_subitems if k not in keys]
        extra = [k for k in keys if k not in expect_fp_subitems]
        if missing:
            rep['ok'] = False
            rep['notes'].append(f'isp_fingerprint 子项缺失：{missing}')
        if extra:
            rep['notes'].append(f'isp_fingerprint 子项多出：{extra}')
    else:
        rep['notes'].append('isp_fingerprint.scenes 为空（`_pending` 中有 isp_fingerprint_static 则应属已知故障路径）')
    for grp in ('illuminance', 'light_source', 'isp_fingerprint'):
        pend = (sc.get(grp) or {}).get('_pending') if isinstance(sc.get(grp), dict) else None
        if pend:
            rep['pending'].extend(f'{grp}.{p}' for p in pend)
    return rep


# ── 判定 ─────────────────────────────────────────────────────────────────────
def verdict(label, state, detail):
    return {'项': label, '判定': state, '实测': detail}


def judge_pair(pid, dark_name, gt_name, frames, geo, ncc_same, cross_vals, consist, sc_state):
    items = []
    # ① GT 饱和
    gt = frames[gt_name]
    e = gt['eq255_pct']
    v = '过' if e <= TH['gt_eq255_pass'] else '不过'
    items.append(verdict('① GT 饱和', v,
                         f"=255 {e:.2f}%（≥250 {gt['ge250_pct']:.2f}%）；刻度 ≤{TH['gt_eq255_pass']} 过 / >{TH['gt_eq255_pass']} 不过（2026-10-02 与 SOP ① 同步，单阈值、取消\"警戒\"档）"))
    # ② 几何对齐（口径：零位移即最优；或非零位移最优但增益 ≤ geom_gain_ignore ⇒ 判"对齐"）
    shift = geo['best_shift_full']
    gain = geo['gain_best_minus_00']
    aligned = (shift == [0, 0]) or (gain <= TH['geom_gain_ignore'])
    if shift == [0, 0]:
        why = '零位移最优'
    elif aligned:
        why = f"非零位移最优但增益 {gain:+.4f} ≤ {TH['geom_gain_ignore']} ⇒ 可忽略、判对齐"
    else:
        why = f"非零位移最优且增益 {gain:+.4f} > {TH['geom_gain_ignore']} ⇒ 判不对齐"
    items.append(verdict('② 几何对齐', '过' if aligned else '不过',
                         f"NCC(0,0) {geo['ncc_00']:.3f}；最优位移 {shift}；增益 {gain:+.4f}；{why}"))
    # ③ 暗部信号（**仅描述、不参与淘汰** —— 2026-10-02 依用户裁定改正）
    #   暗光增强的训练目标即「差暗图 → 好 GT」，暗帧 SNR 越低正是要的那类输入（0.75 Lux 就是照度下限的探索），
    #   不是质量缺陷。原 `≥10 过 / ≥5 警戒 / <5 不过` 阈值系本工具自设刻度、**无规格依据**，
    #   曾据此把 11 对低光配对误判为不合格（已撤回）。此数仅作**描述与分层**用。
    s = frames[dark_name]['dark_snr']['snr_proxy_median']
    items.append(verdict('③ 暗部信号（仅描述）', '描述',
                         f"暗帧 SNR 代理中位 {s}（**不作淘汰判据**；越低 = 输入越暗，属训练目标）"))
    # ④ 配对质量（硬失败）：同对 NCC(0,0) ≥ 0.5 且 > 同批跨对 max|NCC|，否则配对异常
    cross_max = max((abs(x) for x in cross_vals), default=0.0)
    if ncc_same >= TH['ncc_same_pass'] and ncc_same > cross_max:
        v = '过'
    else:
        v = '不过'
    items.append(verdict('④ 配对质量', v,
                         f"同对 NCC(0,0) {ncc_same:.3f}；同批跨对 max|NCC| {cross_max:.3f}；"
                         f"刻度 同对≥{TH['ncc_same_pass']} 且 同对>跨对（否则配对异常、硬失败）"))
    # ⑤ 采集一致性
    if consist['ok']:
        v = '过'
    else:
        v = '不过'
    items.append(verdict('⑤ 采集一致性', v, consist['detail']))
    # ⑥ sidecar 字段完整
    v = '过' if sc_state['ok'] else '不过'
    detail = f"isp_fingerprint 子项 {len(sc_state['fp_subitems'] or [])} 项 / 子树 sha 字段 {sc_state['fp_sha_count']} 个" \
             f"；_pending = {sc_state['pending'] or '无'}"
    if sc_state['missing_groups']:
        detail += f"；缺失组 {sc_state['missing_groups']}"
    if sc_state['notes']:
        detail += '；' + '；'.join(sc_state['notes'])
    items.append(verdict('⑥ sidecar 字段完整', v, detail))

    # 总体
    hard_fail = any(i['判定'] == '不过' for i in items
                    if i['项'] in ('② 几何对齐', '④ 配对质量', '⑤ 采集一致性', '⑥ sidecar 字段完整'))
    gt_fail = items[0]['判定'] == '不过'
    any_warn = any(i['判定'] == '警戒' for i in items)
    if hard_fail:
        overall = '不可用'
    elif gt_fail:
        overall = '不可用（GT 报废；dark 可留作无 GT 暗帧）'
    elif any_warn:
        overall = '有条件可用'
    else:
        overall = '可用'
    return {'pair_id': pid, 'dark': dark_name, 'gt': gt_name, 'items': items, 'overall': overall}


# ── 主流程 ───────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(
        description='IMX415 采集体检（NV12 只读量化；**非验收依据**）',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dir', default='.', help='采集目录（默认当前目录）')
    ap.add_argument('--w', type=int, default=DEF_W, help=f'宽（默认 {DEF_W}）')
    ap.add_argument('--h', type=int, default=DEF_H, help=f'高（默认 {DEF_H}）')
    ap.add_argument('--stride', type=int, default=DEF_STRIDE, help=f'Y 行跨距（默认 {DEF_STRIDE}）')
    ap.add_argument('--uv-offset', type=int, default=DEF_UV_OFFSET, help=f'UV 平面偏移（默认 {DEF_UV_OFFSET}）')
    ap.add_argument('--frame-bytes', type=int, default=DEF_FRAME_BYTES, help=f'单帧字节数（默认 {DEF_FRAME_BYTES}）')
    ap.add_argument('--json-out', default=None, help='指标 JSON 输出路径（默认 <dir>/metrics.json）')
    ap.add_argument('--png-out', default=None, help='PNG 输出目录（默认 <dir>/png/）')
    ap.add_argument('--no-png', action='store_true', help='不写任何 PNG（含接触图）')
    ap.add_argument('--contact-sheet', default=None, help='接触图输出路径（默认 <dir>/contact_sheet.png）')
    ap.add_argument('--warn-only', action='store_true', help='只报结论，不因结论非"可用"而返回非零')
    args = ap.parse_args(argv)

    d = os.path.abspath(args.dir)
    if not os.path.isdir(d):
        print(f'[错误] 目录不存在：{d}', file=sys.stderr)
        return 2
    json_out = args.json_out or os.path.join(d, 'metrics.json')
    png_out = args.png_out or os.path.join(d, 'png')
    contact = args.contact_sheet or os.path.join(d, 'contact_sheet.png')

    # 发现帧（**配对键 = 场景号** `YYMMDD_<场景2位>`，与 `tools/collect/capture_app.py` 的
    #   `pair_id` / v3.69 裁定 O59 一致；**同场景的 dark 与 gt 即一对、不要求帧序号相同**——
    #   例：场景 03 的 dark(`03_0001`) 与 gt(`03_0002`) 帧序号不同，仍是同一对。
    #   ⚠️ 历史实现按键含帧序号 ⇒ 03 被误判为两个孤立帧，2026-10-02 补记② 更正）
    dark, gt, unrecognized, multi_slot, frame_index = {}, {}, [], [], []
    for p in sorted(glob.glob(os.path.join(d, '*.nv12'))):
        stem = os.path.basename(p)[:-len('.nv12')]
        m = RE_NAME.match(stem)
        if not m:
            unrecognized.append(os.path.basename(p))
            continue
        date, scene, slot, kind = m.groups()
        key = f'{date}_{scene}'
        bucket = dark if kind == 'dark' else gt
        if key in bucket:  # 同场景同档多槽：如实报告，不静默丢弃
            multi_slot.append(f"{os.path.basename(bucket[key])} + {os.path.basename(p)}")
        bucket[key] = p
        frame_index.append((date, scene, slot, kind, os.path.basename(p)))

    pair_keys = sorted(set(dark) & set(gt))
    orphan_dark = sorted(set(dark) - set(gt))
    orphan_gt = sorted(set(gt) - set(dark))

    metrics = {
        'schema': 'capture-healthcheck/v1',
        'tool': 'tools/healthcheck_capture.py',
        'source_dir': d,
        'params': {'w': args.w, 'h': args.h, 'stride': args.stride,
                   'uv_offset': args.uv_offset, 'frame_bytes': args.frame_bytes, 'csc': 'BT.601/full'},
        'discovery': {
            'pairs': pair_keys, 'orphan_dark': orphan_dark, 'orphan_gt': orphan_gt,
            'unrecognized': unrecognized, 'multi_slot': multi_slot,
        },
        'frames': {}, 'geometry': {}, 'cross_pair_ncc00': {},
        'scene_slots': {}, 'sidecar': {}, 'consistency': {}, 'verdicts': [],
    }

    if not pair_keys and not orphan_dark and not orphan_gt:
        print(f'[警告] 目录内未发现任何 `YYMMDD_<场景2位>_<帧序号>_dark|gt.nv12`：{d}')
        if unrecognized:
            print(f'        未识别文件 {len(unrecognized)} 个：{unrecognized[:5]}…')

    # 逐帧量化
    Y = {}
    rgbs = {}
    errors = {}
    batch = None
    for key, by_kind in [(k, dark[k]) for k in sorted(dark)] + [(k, gt[k]) for k in sorted(gt)]:
        name = os.path.basename(by_kind)[:-len('.nv12')]
        y, cb, cr, err = load_nv12(by_kind, args.w, args.h, args.stride, args.uv_offset, args.frame_bytes)
        if err:
            errors[name] = err
            print(f'[错误] {name}：{err}', file=sys.stderr)
            continue
        rgb = nv12_to_rgb(y, cb, cr)
        Y[name] = y
        if not args.no_png:
            rgbs[name] = rgb
        m = {}
        m.update(y_stats(y))
        m['dark_snr'] = dark_snr_proxy(y, args.h, args.w)
        m.update(rgb_ratio(rgb))
        m['file_bytes'] = os.path.getsize(by_kind)
        sc, sc_err = load_sidecar(os.path.join(d, name + '.json'))
        if sc and batch is None:
            batch = sc.get('capture_batch')
        if sc_err:
            m['sidecar_error'] = sc_err
        m['sidecar'] = sidecar_report(sc, m['y_mean_sampled_q1'])
        m['sensor_ctrl_readback'] = (sc or {}).get('sensor_ctrl', {}).get('readback') if sc else None
        metrics['frames'][name] = m

    metrics['batch'] = batch

    # 场景 / 槽位（如实报告，不删改输入）
    for date, scene, slot, kind, _stem in frame_index:
        buckets = metrics['scene_slots'].setdefault(f'{date}_{scene}', {})
        b = buckets.setdefault(slot, {'slot': slot, 'dark': False, 'gt': False})
        b[kind] = True
    for k, slots in metrics['scene_slots'].items():
        metrics['scene_slots'][k] = [slots[s] for s in sorted(slots)]

    # 几何 / 同对 NCC
    for k in pair_keys:
        dn, gn = os.path.basename(dark[k])[:-5], os.path.basename(gt[k])[:-5]
        if dn in Y and gn in Y:
            metrics['geometry'][k] = geometry(Y[dn], Y[gn], args.h, args.w)
    # 跨对 NCC（**只记不同 pair 之间**；同对 NCC 归 geometry）
    # **（2026-10-02 补记②）跨对基准只算"成对之间"、排除孤立帧**：孤立帧若与某对同场景，其 NCC
    # 语义上是"同对"、会**虚假抬高**基准（前例：孤立 03_0001 vs 03_0002 的 0.662）。
    pair_of = {}
    for k in pair_keys:
        pair_of[os.path.basename(dark[k])[:-5]] = k
        pair_of[os.path.basename(gt[k])[:-5]] = k
    pair_names = sorted(pair_of)          # 仅成对帧进跨对基准；孤立帧不参与
    for i in range(len(pair_names)):
        for j in range(i + 1, len(pair_names)):
            a, b = pair_names[i], pair_names[j]
            if pair_of[a] == pair_of[b]:
                continue  # 同对，不属跨对
            metrics['cross_pair_ncc00'][f'{a}__vs__{b}'] = _ncc(
                downsample_z(Y[a], args.h, args.w), downsample_z(Y[b], args.h, args.w))

    # 一致性（sensor_ctrl 逐帧相同）
    names = sorted(Y)
    readings = {}
    for name in names:
        rb = metrics['frames'][name].get('sensor_ctrl_readback')
        if rb:
            readings[name] = (rb.get('exposure'), rb.get('analogue_gain'), rb.get('vertical_blanking'))
    uniq = set(readings.values())
    consist = {
        'ok': bool(readings) and len(uniq) == 1,
        'detail': (f"exposure/gain/vblank 逐帧一致 {readings[names[0]] if readings else '-'}"
                   if readings and len(uniq) == 1 else
                   (f"逐帧不一致：{sorted(uniq)}" if readings else 'sensor_ctrl 回读缺失')),
        'readings': readings,
    }
    metrics['consistency'] = consist

    # 判定
    for k in pair_keys:
        dn, gn = os.path.basename(dark[k])[:-5], os.path.basename(gt[k])[:-5]
        if dn not in Y or gn not in Y:
            continue
        pid = k.split('_')[-1]  # 场景号（pair_id = 场景号，O59）
        cross_vals = list(metrics['cross_pair_ncc00'].values())  # cross_pair_ncc00 已只含跨对
        v = judge_pair(pid, dn, gn, metrics['frames'], metrics['geometry'][k],
                       metrics['geometry'][k]['ncc_00'], cross_vals,
                       consist, metrics['frames'][dn]['sidecar'])
        metrics['verdicts'].append(v)

    overall = '可用'
    if not (dark or gt):
        overall = '不可用（无输入）'
    elif errors or unrecognized:
        overall = '不可用'
    elif orphan_dark or orphan_gt:
        overall = '有条件可用'
    elif metrics['verdicts']:
        if all(x['overall'] == '可用' for x in metrics['verdicts']):
            overall = '可用'
        elif any(x['overall'] == '不可用' for x in metrics['verdicts']):
            overall = '不可用'
        else:
            overall = '有条件可用'
    metrics['overall'] = overall

    # 汇总（逐对判定 → 可用对数）
    n_pairs = len(metrics['verdicts'])
    n_usable = sum(1 for v in metrics['verdicts'] if v['overall'] == '可用')
    n_cond = sum(1 for v in metrics['verdicts'] if v['overall'].startswith('有条件可用'))
    metrics['summary'] = {'n_pairs': n_pairs, 'usable': n_usable,
                          'conditional': n_cond, 'unusable': n_pairs - n_usable - n_cond}

    # PNG + 接触图（只做 BT.601/full，无任何非线性后处理）
    if not args.no_png:
        os.makedirs(png_out, exist_ok=True)
        for name, rgb in rgbs.items():
            Image.fromarray(rgb).save(os.path.join(png_out, name + '.png'))
        if pair_keys:
            THUMB = 540
            tiles = []
            for k in pair_keys:
                dn, gn = os.path.basename(dark[k])[:-5], os.path.basename(gt[k])[:-5]
                row = []
                for nm in (dn, gn):
                    if nm in rgbs:
                        im = Image.fromarray(rgbs[nm])
                        row.append(im.resize((THUMB * args.w // args.h, THUMB), Image.BILINEAR))
                if len(row) == 2:
                    tiles.append(row)
            if tiles:
                tw = tiles[0][0].size[0]
                sheet = Image.new('RGB', (tw * 2, THUMB * len(tiles)))
                for r, row in enumerate(tiles):
                    for c, im in enumerate(row):
                        sheet.paste(im, (c * tw, r * THUMB))
                os.makedirs(os.path.dirname(os.path.abspath(contact)), exist_ok=True)
                sheet.save(contact)

    os.makedirs(os.path.dirname(os.path.abspath(json_out)), exist_ok=True)
    with open(json_out, 'w') as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)

    # ── 人读报告 ──
    print(f'采集体检：{d}')
    print(f'批次：{batch}｜成对 {len(pair_keys)}｜孤立 dark {len(orphan_dark)}｜孤立 gt {len(orphan_gt)}'
          f'｜未识别 {len(unrecognized)}')
    if orphan_dark:
        print(f'  孤立 dark：{orphan_dark}')
    if orphan_gt:
        print(f'  孤立 gt：{orphan_gt}')
    if multi_slot:
        print(f'  [警告] 同场景同档多槽（如实报告、不静默丢弃）：{multi_slot}')
    if errors:
        for n, e in errors.items():
            print(f'  [错误] {n}：{e}')
    print(f'采集一致性：{"过" if consist["ok"] else "不过"} —— {consist["detail"]}')
    for v in metrics['verdicts']:
        print(f'\n▶ 场景 {v["pair_id"]}（{v["dark"]} | {v["gt"]}）⇒ 总体：{v["overall"]}')
        for it in v['items']:
            print(f'    {it["项"]}：{it["判定"]} —— {it["实测"]}')
    print(f'\n逐对判定：{n_pairs} 对 ⇒ 可用 {metrics["summary"]["usable"]} / '
          f'有条件可用 {metrics["summary"]["conditional"]} / 不可用 {metrics["summary"]["unusable"]}')
    print(f'最终可用对数：{metrics["summary"]["usable"]}/{n_pairs}')
    print(f'总体结论：{overall}（**非验收依据**；量化口径见文件头注释）')
    print(f'指标 JSON：{json_out}' + ('' if args.no_png else f'｜PNG：{png_out}｜接触图：{contact}'))

    return 0 if (args.warn_only or overall == '可用') else 1


if __name__ == '__main__':
    sys.exit(main())