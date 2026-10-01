#!/usr/bin/env python3
"""IMX415 成对数据集采集（日期戳+场景号+帧序号落盘 + **无损裸 NV12** + sidecar 元数据）。

**与 `tools/cam_preview.py` 的关系**：本文件是**独立自包含**的扩展版，**不 import、不修改**
原对焦应用（原文件保持可独立使用）。取流与 multipart 解析逻辑沿用其做法，见下"继承的实测坑"。

用途：M1 阶段的**实拍采集**，服务于 `docs/design/dataset.md` 的 U1（INT8 校准）、
U2（ZDE++ 零参考训练）与 U4（评测）。**不是交付链路**——交付显示路径见主基准 §3.3/§3.4。

**采集范式（依 `docs/design/dataset.md` §4.5 的 (a) 改良版）**：固定 ISP 参数与曝光，
**改光源亮度**而非改曝光 ⇒ 同一取景下暗帧与亮帧的 ISP 行为完全一致、几何天然对齐、
3DNR 不错配。故成对采集的操作序为：摆好取景 → 调亮光源 → 按【采集亮帧 GT】→ 调暗光源 →
按【采集暗帧】→ 按【更换场景】进入下一取景。**U1 校准集为单边图**：全程只按【采集暗帧】，
同一取景内改光照后再按一次（帧序号递增），**无 GT**。

**术语（主基准 v3.69 裁定 O59）**：本工具的"**场景**"= **取景**（狭义），**2 位编号**
（00–99，由【更换场景】+1）；它**仅作编号与 dark-gt 配对键**，**不承担表达场景内容**。

**采集前置（硬约束，主基准 §15 O62 ④(ii)）**：**当前 IQ 必须是"采集档"**（3DNR 关；O40）。
本工具**启动时校验**（比对 `/etc/iqfiles_sets/manifest.json` 与 `/etc/iqfiles/` 当前文件的 sha256），
**不通过则拒绝采集**（预览/对焦仍可用，页面顶部显示原因）。档位由 `tools/collect/iq_sets.py` 维护：

    sudo python3 tools/collect/iq_sets.py status        # 当前是哪一档
    sudo python3 tools/collect/iq_sets.py switch collect

**采集前置之二（硬约束，主基准 §9.5 ④）**：**1080p@30 锁定必须已施加**。60 fps 的 VMAX
（1080+1154 = 2234 行）把 manual 曝光钳到 2242 行 ⇒ 1/30 s（4485 行）不可达（O63），且 VMAX
改变即改变噪声统计 ⇒ 与演示链路不同构。本工具**在启动 `v4l2src` 之前**调用共享实现
`sensor_lock.py`（仓库根，§10.3 登记）写 sensor subdev 的 `vertical_blanking`，并**回读校验**；
**不通过则拒绝采集**（预览/对焦仍可用）。**停流即复位** ⇒ 每次启动都要重施。

用法（板上）：
  python3 tools/collect/capture_app.py --outdir ~/capture_data [--port 8091]
      [--device /dev/video11] [--w 1920] [--h 1080] [--batch B001]
      [--iq-dir /etc/iqfiles] [--iq-manifest /etc/iqfiles_sets/manifest.json]
      [--no-iq-guard]        # **调试用**：绕过防呆（会记入 sidecar 以便审计）
      [--vblank-lines 3405]  # = config `camera.vblank_lines`（§10.4）
      [--sensor-subdev /dev/v4l-subdev2]   # 调试用；默认运行时解析
手机/PC 浏览器打开： http://<板地址>:8091/

落盘约定（`--outdir` 下，命名见主基准 v3.69 / `docs/design/dataset.md` §5.2.8）：
  YYMMDD_<场景2位>_<帧序号>_dark.nv12   暗帧（增强链路输入域；**无损裸 NV12**）
  YYMMDD_<场景2位>_<帧序号>_gt.nv12     亮帧（配对 GT；**无损裸 NV12**）
  YYMMDD_<场景2位>_<帧序号>_dark.json   该帧的 sidecar（**逐帧一份**，字段分组见
  YYMMDD_<场景2位>_<帧序号>_gt.json     `docs/design/dataset.md` §6.1）
  .capture_state.json                   采集进度（下一场景号 / 各场景帧槽），**重启后不丢**

**帧序号语义**：同场景内的帧槽号，从 0001 起；**成对采集时 dark / gt 同号**（构成一对）。
同一帧槽内**重复按同一档位**即开新的一帧槽（U1 靠此在一取景内采多张）。

**无损落盘（v3.69 落地）**：预览仍走 JPEG（浏览器需可解码格式），**落盘走独立的裸 NV12 分支**
（gst `tee` 分叉 + 双 `fdsink`），JPEG **不进数据集**。裸 NV12 无自描述，故 sidecar 记录
`stride` / `uv_offset` / `size_bytes`——否则开发机侧按 `W×H×1.5` 解析会错位。

依赖：板上 `gst-launch-1.0`（含 `v4l2src` / `tee` / `queue` / `mppjpegenc` / `multipartmux` /
`fdsink`），python3 仅用标准库（与原对焦应用一致，无第三方依赖）。

继承的实测坑（勿改）：
  1. `/dev/video11` 是 **Multiplanar** 节点，`ffmpeg/ffplay/mpv` 打不开，必须走 GStreamer。
  2. `v4l2src` **直连** `mppjpegenc` 会反复编码同一块陈旧缓冲 ⇒ 画面"冻住"（帧序号仍在涨）。
     必须前置 `videoconvert` 做一次真实拷贝。`dup_run` 指标用于监视这类故障。
  3. `/dev/video11` 实测为 NV12 1920×1080 **单平面、continuous**、`bytesperline` 1920、
     `sizeimage` 3110400（见 `environment/board-lubancat5.md` §7）⇒ 裸帧**定长**，
     路② 按定长读、**无 multipart 分帧**；`--raw-frame-size` 可覆盖以防驱动改值。
"""

import argparse
import hashlib
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import zlib
from datetime import datetime, timezone

# ── 1080p@30 锁定的**共享实现**（主基准 §9.5 ④ / §10.4 `camera.vblank_lines`）──────
# 机制只有一份 SSOT（`sensor_lock.py`，登记于 §10.3），**不在本文件复制**：
# 本工具部署于 `<repo>/tools/collect/`，故按 `__file__` 反推仓库根并置入 `sys.path`。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
try:
    import sensor_lock
except ImportError as _e:                                     # 部署缺件 ⇒ 采集 fail-closed
    sensor_lock = None
    _SENSOR_LOCK_IMPORT_ERR = str(_e)
else:
    _SENSOR_LOCK_IMPORT_ERR = ''

# 采集档位。命名见主基准 v3.69：<日期>_<场景2位>_<帧序号>_<档位>.<后缀>
KINDS = ('dark', 'gt')          # dark = 暗帧（输入域）/ gt = 亮帧（配对 GT）

# 无损裸 NV12 的几何（由 main() 按 --w/--h/--raw-frame-size 落值）
RAW = {'stride': 0, 'uv_offset': 0, 'frame_bytes': 0}

# gst 命令行（main() 里落值；模块级先置空，避免引用未定义）
GST_CMD = []

# ── ISP IQ 档位防呆（主基准 §15 **O62 ④(ii)**）────────────────────────────
# 采集**必须发生在"采集档"下**（3DNR 关；O40）。档位由 `tools/collect/iq_sets.py` 维护，
# 其产物 `manifest.json` 是本模块与换档工具之间的**唯一接口**（schema 见下）。
# `manifest.json` 的位置是**部署约定**（两工具须一致；约定本身见 `tools/collect/README.md`）。
IQ_MANIFEST_SCHEMA = 'iqsets/v1'
IQ_COLLECT_TAG = 'collect'
IQ = {'tag': 'unknown', 'sha256': None, 'file': None, 'guard': 'blocked', 'why': '尚未校验'}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def iq_guard(iq_dir, manifest_path, bypass=False):
    """校验「当前 IQ = 采集档」（O62 ④(ii)）。

    **fail-closed**：manifest 缺失 / schema 不符 / 当前 IQ 无法判定 ⇒ 一律**不通过**
    （宁可拒采，也不静默污染数据集——O62 ④(ii) 的原话是"防'忘切'导致静默污染数据集"）。
    `bypass=True` 仅供调试，**会记入 sidecar**（`isp_fingerprint.iq_guard = 'bypassed'`）以便审计。
    """
    IQ['guard'] = 'blocked'
    try:
        with open(manifest_path) as f:
            m = json.load(f)
    except Exception as e:
        IQ.update(tag='unknown', sha256=None, file=None,
                  why='无法读取 manifest（%s）：%s' % (manifest_path, e))
        return False
    if m.get('schema') != IQ_MANIFEST_SCHEMA:
        IQ.update(tag='unknown', sha256=None, file=None,
                  why='manifest schema 不符（期望 %r，实得 %r）'
                      % (IQ_MANIFEST_SCHEMA, m.get('schema')))
        return False
    name = m.get('sensor_file')
    cur = os.path.join(iq_dir, name or '')
    if not name or not os.path.exists(cur):
        IQ.update(tag='unknown', sha256=None, file=name, why='当前 IQ 文件不存在：%s' % cur)
        return False
    h = _sha256_file(cur)
    tag = 'unknown'
    if h == (m.get('stock') or {}).get('sha256'):
        tag = 'stock'
    for s, e in (m.get('sets') or {}).items():
        if h == (e or {}).get('sha256'):
            tag = s
    IQ.update(file=name, sha256=h, tag=tag)
    if tag == IQ_COLLECT_TAG:
        IQ.update(guard='ok', why='')
        return True
    IQ['why'] = ('当前 IQ = %s 档，**不是采集档**（采集须在 collect 档下进行；'
                 '换档 = `sudo python3 tools/collect/iq_sets.py switch collect`）' % tag)
    if bypass:
        IQ['guard'] = 'bypassed'
        return True
    return False

# ── ISP 指纹（主基准 §9.5 / 设计档 §6.1；能力缺口 **G5**）─────────────────────
# 记录口径 = **只记「IQ 内路径 + 可观测状态 + 规范化子树哈希前 16 位」，不复制取值**
# （SSOT = IQ 文件，规则 `00-ssot.md` §3.1）。子树边界**已板端实测确认**（与 config.yaml
# §isp.fingerprint 已归档的 16 位逐位一致）⇒ **不照文档推测**。失败**不得静默**：解析不到
# 即 `ok=False` 并把该项留在 `_pending`（规则 `10-evidence.md` §2，禁填占位假值）。
IQFP = {'ok': False, 'why': '尚未解析', 'scenes': [], 'ae': {}, 'src': None}

# 最近一次 `sensor_ctrl` 回读（供页面回显；逐帧随 sidecar 落盘）
SENSOR = {'ok': False, 'why': '尚未回读', 'device': None, 'readback': {}}

# 光照/光源（G3）：批次级设定 + 最近一帧的 Y 均值（供页面回显）
LIGHT = {'lux_anchor': None, 'lux_anchor_note': None, 'y_mean': None,
         'type': None, 'cct_k': None, 'dimming': None, 'distance_m': None, 'note': None}

# 子树跳径 → 已归档哈希（板端实测命中，作自检用；不符即在页面告警）
_FP_EXPECT = {
    'agamma_calib_v11': '881b76242a6f2650',
    'ccm_calib/TuningPara': '85e5727b30c11272',
    'ablc_calib/BlcTuningPara': 'a511de7cb4e7c348',
    'bayer2dnr_v2/TuningPara': '16ee259977494109',
    'adpcc_calib': '79da0940d59eea65',
}


def _sha16(obj):
    """规范化子树哈希前 16 位（口径同 config.yaml §isp.fingerprint 注释）。"""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()[:16]


def _iq_fp_scene(isp, idx, src):
    """由单个 `scene_isp30` 子树构造 8 子项指纹（**值来自 IQ 实测，不硬编码**）。"""
    gt = isp['agamma_calib_v11']['GammaTuningPara']
    dg = (isp.get('adegamma_calib') or {}).get('DegammaTuningPara') or {}
    tp = isp['ccm_calib']['TuningPara']
    blc = isp['ablc_calib']
    wb = isp['wb_v21']
    return {
        'scene_index': idx, 'src': src,
        'gamma_table': {
            'src': 'agamma_calib_v11/GammaTuningPara',
            'gamma_en': gt.get('Gamma_en'),
            'curve_points': len(gt.get('Gamma_curve') or []),
            'gamma_out_offset': gt.get('Gamma_out_offset'),
            'curve_sha256_16': _sha16(gt['Gamma_curve']),
            'subtree_sha256_16': _sha16(isp['agamma_calib_v11']),
            'degamma_en': dg.get('degamma_en'),
        },
        'ccm': {
            'src': 'ccm_calib/TuningPara',
            'enable': (isp['ccm_calib'].get('control') or {}).get('enable'),
            'matrices': len(tp.get('matrixAll') or []),
            'illuminants': len(tp.get('aCcmCof') or []),
            'damp_enable': tp.get('damp_enable'),
            'interp_enable': (tp.get('illu_estim') or {}).get('interp_enable'),
            'default_illu': (tp.get('illu_estim') or {}).get('default_illu'),
            'subtree_sha256_16': _sha16(tp),
        },
        'black_level': {
            'src': 'ablc_calib/BlcTuningPara',
            'enable': blc['BlcTuningPara'].get('enable'),
            'iso_points': len((blc['BlcTuningPara'].get('BLC_Data') or {}).get('ISO') or []),
            'enable_set2': (blc.get('Blc1TuningPara') or {}).get('enable'),
            'subtree_sha256_16': _sha16(blc['BlcTuningPara']),
        },
        'awb_lock': {
            'src': 'wb_v21.control.mode',
            'mode': (wb.get('control') or {}).get('mode'),
            'manual_mode': (wb.get('manualPara') or {}).get('mode'),
            'mwbGain': ((wb.get('manualPara') or {}).get('cfg') or {}).get('mwbGain'),
        },
        'nr_2d': {
            'src': 'bayer2dnr_v2/TuningPara.enable',
            'enable': isp['bayer2dnr_v2']['TuningPara'].get('enable'),
            'subtree_sha256_16': _sha16(isp['bayer2dnr_v2']['TuningPara']),
        },
        'nr_3d': {
            'src': 'bayertnr_v2/TuningPara.enable',
            'enable': isp['bayertnr_v2']['TuningPara'].get('enable'),
            'subtree_sha256_16': _sha16(isp['bayertnr_v2']['TuningPara']),
        },
        'dpc': {
            'src': 'adpcc_calib/DpccTuningPara/Enable',
            'enable': isp['adpcc_calib']['DpccTuningPara'].get('Enable'),
            'subtree_sha256_16': _sha16(isp['adpcc_calib']),
        },
    }


def iq_fp_build(iq_dir):
    """解析当前 IQ → 静态指纹（8 子项 × 每 `scene_isp30`）+ AE 手动档期望值。**失败不静默**。"""
    IQFP.update(ok=False, scenes=[], ae={}, why='', src=None)
    if not IQ['file']:
        IQFP['why'] = 'IQ 文件名未知（iq_guard 未通过？）'
        return False
    path = os.path.join(iq_dir, IQ['file'])
    try:
        with open(path) as f:
            d = json.load(f)
        scenes = [_iq_fp_scene(ms['sub_scene'][0]['scene_isp30'], i,
                               'main_scene[%d].sub_scene[0].scene_isp30' % i)
                  for i, ms in enumerate(d['main_scene'])]
        isp0 = d['main_scene'][0]['sub_scene'][0]['scene_isp30']
        ae = isp0.get('ae_calib', {})
        cc = ae.get('CommCtrl', {})
        mc = cc.get('AecManualCtrl', {}).get('LinearAE', {})
        fm = cc.get('AecFrameRateMode', {})
        IQFP['ae'] = {
            'aec_op_mode': cc.get('AecOpType'),
            'time_value_s': mc.get('TimeValue'), 'gain_value': mc.get('GainValue'),
            'isp_dgain_value': mc.get('IspDGainValue'),
            'manual_en': {k: mc.get(k) for k in
                          ('ManualTimeEn', 'ManualGainEn', 'ManualIspDgainEn')},
            'is_fps_fix': fm.get('isFpsFix'), 'fps_value': fm.get('FpsValue'),
        }
    except Exception as e:
        IQFP['why'] = 'IQ 解析失败（%s）：%s' % (path, e)
        return False
    if not scenes:
        IQFP['why'] = 'IQ 中未找到 main_scene[*].sub_scene[0].scene_isp30'
        return False
    # 自检：子树哈希须与 config.yaml 已归档值一致，否则页面告警（防解析错位）
    bad = [(k, v) for k, v in _FP_EXPECT.items() if
           _sha16(_dig(d['main_scene'][0]['sub_scene'][0]['scene_isp30'],
                       tuple(k.split('/')))) != v]
    IQFP.update(ok=True, scenes=scenes,
                src='%s ×%d scene_isp30' % (path, len(scenes)),
                why=('⚠ 子树哈希自检不符（解析可能错位）：%s' % bad) if bad else '')
    return True


def _dig(node, path):
    cur = node
    for k in path:
        cur = cur[k]
    return cur


def _sensor_ctrl(a):
    """采集时刻只读回读 sensor subdev 三控制（G5 的「运行期指纹」）。失败**不静默** → (None, 原因)。"""
    node = LOCK.get('node') or a.sensor_subdev
    if not node:
        return None, 'sensor subdev 未解析（30 fps 锁定未施加？）'
    ctrls = ('exposure', 'analogue_gain', 'vertical_blanking')
    try:
        r = subprocess.run(['v4l2-ctl', '-d', node, '--get-ctrl', ','.join(ctrls)],
                           capture_output=True, text=True, timeout=5)
    except Exception as e:
        return None, 'v4l2-ctl 调用异常：%s' % e
    vals = {}
    for line in ((r.stdout or '') + (r.stderr or '')).splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            if k.strip() in ctrls:
                try:
                    vals[k.strip()] = int(v.strip())
                except ValueError:
                    vals[k.strip()] = v.strip()
    if r.returncode != 0 or not vals:
        return None, 'v4l2-ctl 回读失败（rc=%d）' % r.returncode
    return {
        'device': node,
        'readback': vals,                       # 采集时刻**实测生效值**
        'expected': {                           # 期望值（来自锁定与 IQ 手动档，非设定回读）
            'vblank_lines': LOCK.get('vblank_lines'),
            'aec_op_mode': IQFP['ae'].get('aec_op_mode'),
            'time_value_s': IQFP['ae'].get('time_value_s'),
            'gain_value': IQFP['ae'].get('gain_value'),
            'is_fps_fix': IQFP['ae'].get('is_fps_fix'),
            'fps_value': IQFP['ae'].get('fps_value'),
        },
        '_note': 'readback = 采集时刻实测；expected = 锁定/IQ 手动档的期望值。二者不一致即漂移信号。',
    }, ''


def _y_mean(raw, w, h, stride, step=4):
    """逐帧 **Y 平面均值**（相对光照指标；设计档 §12.3）。

    口径 = **隔行隔列取 1/step**（与 §3.4 OSD 的降采样同族），零额外取数成本
    （数据本就在手的无损 NV12）。⚠️ **诚实的局限**：帧均值与场景反射率混淆 ⇒
    **跨场景不可直接比**；跨场景光照比较只能靠批次级照度锚点（§12.4）。
    """
    s = 0
    n = 0
    for y in range(0, h, step):
        row = raw[y * stride:y * stride + w]
        sub = row[::step]
        s += sum(sub)
        n += len(sub)
    return (s / n if n else 0.0), n

# ── 1080p@30 锁定防呆（主基准 §9.5 ④ / §10.4 `camera.vblank_lines`）─────────────
# 采集**必须发生在 30 fps 锁定下**：60 fps 的 VMAX（1080+1154 = 2234 行）把 manual 曝光钳到
# 2242 行 ⇒ 1/30 s（4485 行）不可达（O63）；且 VMAX 改变即改变噪声统计 ⇒ 与演示链路不同构。
LOCK = {'ok': False, 'node': None, 'vblank_lines': None, 'readback': None, 'why': '尚未施加'}


def lock_guard(vblank_lines, node=None):
    """施加 1080p@30 锁定（共享实现 `sensor_lock.apply`）——**必须在 `v4l2src` 开流之前**。

    **fail-closed**：缺件 / subdev 解析失败 / 回读校验不符 ⇒ 一律**不通过**（拒采）。
    理由与 IQ 防呆同构：宁可拒采，也不静默产出与演示链路不同构的数据集。
    """
    if sensor_lock is None:
        LOCK.update(ok=False, why='无法导入 sensor_lock（%s）——部署须含仓库根的 `sensor_lock.py`'
                                 % _SENSOR_LOCK_IMPORT_ERR)
        return False
    try:
        dev = sensor_lock.resolve(node)
    except sensor_lock.SensorLockError as e:
        LOCK.update(ok=False, why=str(e))
        return False
    if sensor_lock.apply(vblank_lines, dev) != 0:
        LOCK.update(ok=False, node=dev,
                    why='sensor_lock.apply 返回 E_V4L2（见 stdout 的 [sensor_lock] 行；'
                        '需对该 subdev 有写权限 = 板上 `video` 组成员）')
        return False
    LOCK.update(ok=True, node=dev, vblank_lines=vblank_lines,
                readback=sensor_lock.read_vblank(dev), why='')
    return True


STATE = {
    'jpg': None, 'seq': 0, 'bytes': 0, 'err': '', 'crc': 0, 'dup_run': 0,
    'raw': None, 'raw_seq': 0, 'raw_t': 0, 'raw_err': '',   # 路② 最新一帧裸 NV12
    'lock': threading.Lock(),
    # 采集侧状态（由 _state_load 初始化）
    'outdir': '', 'batch': '', 'next_scene': 1,
    'last': '',            # 最近一次操作的结果文案，回显到页面
    # 逐场景帧槽：{scene_id: {'slot': int, 'has': {'dark':bool,'gt':bool},
    #                        'n': {'dark':int,'gt':int}}}
    'counts': {},
}


# ---------------------------------------------------------------- 取流（沿用对焦应用做法）

def reader(proc):
    """解析 gst 的 multipart 输出，保留最新一张 JPEG。

    multipartmux 的帧结构（实测）：`--frame\\r\\nContent-Type: image/jpeg\\r\\n
    Content-Length: N\\r\\n\\r\\n<jpeg>\\r\\n`
    """
    sep = b'--frame'
    buf = b''
    while True:
        chunk = proc.stdout.read(1 << 16)
        if not chunk:
            break
        buf += chunk
        while True:
            i = buf.find(sep)
            if i < 0:
                break
            nxt = buf.find(sep, i + len(sep))
            if nxt < 0:
                break
            part = buf[i + len(sep):nxt]
            buf = buf[nxt:]
            hdr_end = part.find(b'\r\n\r\n')
            if hdr_end < 0:
                continue
            jpg = part[hdr_end + 4:].rstrip(b'\r\n')
            if jpg[:2] == b'\xff\xd8' and jpg[-2:] == b'\xff\xd9':
                crc = zlib.crc32(jpg)          # 用来监视"画面冻住"这一类故障
                with STATE['lock']:
                    STATE['dup_run'] = STATE['dup_run'] + 1 if crc == STATE['crc'] else 0
                    STATE['crc'] = crc
                    STATE['jpg'] = jpg
                    STATE['seq'] += 1
                    STATE['bytes'] = len(jpg)
    STATE['err'] = 'gst exited (rc=%s)' % proc.poll()


def raw_reader(fd):
    """路②：读**裸 NV12**（定长、无 multipart 分帧），只保留**最新一帧**。

    裸流无帧标记，故按 `RAW['frame_bytes']` 定长切分；不足一帧时继续累积。
    每收满一帧就整体替换 `STATE['raw']`（旧帧自动丢弃），并记到达时刻用于陈旧判定。
    """
    n = RAW['frame_bytes']
    if n <= 0:
        STATE['raw_err'] = 'frame_bytes 未设置（应 > 0）'
        return
    buf = bytearray()
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        buf += chunk
        k = len(buf) // n
        if k:
            # 流从帧边界开始且逐帧定长 ⇒ 第 k 帧 = buf[(k-1)*n : k*n]。
            # **必须按整帧相位取**（不能写 len(buf)-n，管道分块不保证对齐）。
            start = (k - 1) * n
            with STATE['lock']:
                STATE['raw'] = bytes(buf[start:start + n])
                STATE['raw_seq'] += 1
                STATE['raw_t'] = time.monotonic_ns()
            del buf[:k * n]                      # 只留不足一帧的尾巴，buf 不会无界增长
    STATE['raw_err'] = 'raw branch exited'


def err_reader(proc):
    """持续抽干 gst 的 stderr（保留尾部），避免管道写满把 gst 阻塞住。"""
    tail = b''
    while True:
        chunk = proc.stderr.read(1 << 12)
        if not chunk:
            break
        tail = (tail + chunk)[-4096:]
    STATE['err'] = (STATE['err'] + ' | ' if STATE['err'] else '') + \
        tail.decode('utf-8', 'replace').strip()


# ---------------------------------------------------------------- 采集进度持久化

def _state_path():
    return os.path.join(STATE['outdir'], '.capture_state.json')


def _state_load():
    """读回采集进度。**重启不重置场景号**——否则会覆盖已采数据。"""
    p = _state_path()
    if not os.path.exists(p):
        return
    try:
        with open(p) as f:
            d = json.load(f)
        STATE['next_scene'] = int(d.get('next_scene', 1))
        if d.get('batch'):
            STATE['batch'] = d['batch']
        STATE['counts'] = d.get('counts', {}) or {}
    except Exception as e:
        print('[capture] 进度文件读取失败（忽略，场景号从 01 起）：%s' % e)


def _state_save():
    tmp = _state_path() + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'next_scene': STATE['next_scene'], 'batch': STATE['batch'],
                   'counts': STATE['counts']}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _state_path())          # 原子替换，避免断电写坏


def scene_id(n=None):
    """场景号：**2 位**（00–99）。仅作编号与 dark-gt 配对键，不承担表达场景内容。"""
    return '%02d' % (STATE['next_scene'] if n is None else n)


def _date_tag():
    """采集日期戳 YYMMDD（本地时区，逐帧取）。"""
    return datetime.now().strftime('%y%m%d')


def frame_name(scene, slot, kind):
    """`<日期>_<场景2位>_<帧序号4位>_<档位>.nv12`（主基准 v3.69 / dataset.md §5.2.8）。"""
    return '%s_%s_%04d_%s.nv12' % (_date_tag(), scene, slot, kind)


# ---------------------------------------------------------------- 落盘

def _sidecar(scene, slot, kind, path, raw_bytes, a):
    """写**该帧**的 sidecar（**逐帧一份**，不再每场景一份 ⇒ 天然消除覆盖）。

    **只写本应用能确知的字段**；取不到的项一律置 `null` 并列入 `_pending`，
    **不得填占位假值**（规则 `10-evidence.md` §2）。

    **G5（v3.86 本轮补齐）**：`isp_fingerprint` 由「仅 IQ 名+哈希」扩为 **8 子项 × 每 scene_isp30**
    （静态，来自 IQ 实测解析），`sensor_ctrl` 由 `null` 改为 **v4l2-ctl 采集时刻回读**。
    `csc` 仍为 `null`，但**理由入 `_not_applicable`**（采集链路 v4l2 直出、未过 RGA，无 CSC 可记）。

    字段分组依 `docs/design/dataset.md` §6.1。
    """
    mono = time.monotonic_ns()                       # 计量域，唯一可比较的时钟
    utc = datetime.now(timezone.utc).isoformat()     # 仅供人读
    sc, sc_why = _sensor_ctrl(a)
    SENSOR.update(ok=bool(sc), why=sc_why, device=(sc or {}).get('device'),
                  readback=(sc or {}).get('readback', {}))
    ymean, nsamp = _y_mean(raw_bytes, a.w, a.h, RAW['stride'])   # 逐帧相对光照指标（§12.3）
    LIGHT.update(lux_anchor=a.lux_anchor, lux_anchor_note=(a.lux_anchor_note or None),
                 y_mean=round(ymean, 4), type=(a.light_type or None), cct_k=a.light_cct_k,
                 dimming=(a.light_dimming or None), distance_m=a.light_distance_m,
                 note=(a.light_note or None))
    light_pending = [k for k, v in (('type', a.light_type), ('cct_k', a.light_cct_k),
                                    ('dimming', a.light_dimming),
                                    ('distance_m', a.light_distance_m)) if v in (None, '')]
    pending = [
        'optics',               # 镜头型号 + 对焦判据实测值 —— O31 关闭后才有
        'split',                # train/val/test 标签 —— 采集期不定，划分时回填
        'dataset_version',      # 数据集版本哈希 —— 打包时回填
    ]
    if not sc:
        pending.append('sensor_ctrl')                # 回读失败 ⇒ 留在 _pending，不填假值
    if not IQFP['ok']:
        pending.append('isp_fingerprint_static')
    stem = os.path.splitext(os.path.basename(path))[0]
    other = 'gt' if kind == 'dark' else 'dark'
    return {
        'schema': 'capture/v2',                          # v2 = 逐帧 sidecar + 无损裸 NV12 + 帧槽
        'scene_id': scene,                               # 2 位场景号（= 取景编号）
        'slot': slot,                                    # 同场景内的帧槽号（成对时 dark/gt 同号）
        'frame_id': stem,                                # = 文件主干名（含日期戳与帧序号）
        'pair_id': scene,                                # **配对键 = 场景号**（v3.69 裁定 O59）
        'capture_batch': STATE['batch'],
        'kind': kind,                                    # dark = 暗帧（输入域）/ gt = 亮帧（配对 GT）
        'time': {'mono_ns': mono, 'utc_iso8601': utc},
        'capture_condition': {
            'device': a.device,
            'resolution': '%dx%d' % (a.w, a.h),
            'pixel_format': 'NV12',
            'gst_pipeline': ' '.join(GST_CMD),
            # 裸 NV12 无自描述：缺这三项，开发机侧按 W×H×1.5 解析会错位
            'stride': RAW['stride'],                     # Y 平面字节/行（板端实测 1920）
            'uv_offset': RAW['uv_offset'],               # UV 平面起始字节偏移（= stride*h）
            'size_bytes': RAW['frame_bytes'],            # 单帧字节数（板端实测 3110400）
        },
        'pair': {
            'pair_frame_id': '%s_%04d_%s' % (scene, slot, other),
            'gt_source': 'light_source_modulation' if kind == 'gt' else None,
            'geometry_alignment': 'same_frame_no_motion',   # 同机位、仅改光源 ⇒ 天然对齐
        },
        'file': {'name': os.path.basename(path), 'bytes': len(raw_bytes)},
        # ISP 指纹（§9.5 全项，G5 补齐）：**静态 8 子项 × 每 scene_isp30**（口径 = 只记路径 +
        # 可观测状态 + 规范化子树哈希前 16 位，**不复制取值**，SSOT = IQ 文件）+ IQ 名/哈希/档位。
        'isp_fingerprint': {
            'iq_file': IQ['file'],                   # 当前 /etc/iqfiles/ 下的 IQ 文件名
            'iq_hash': IQ['sha256'],                 # 该文件 sha256（= §9.5 指纹的 iq_hash）
            'iq_set': IQ['tag'],                     # collect / demo / stock / unknown
            'iq_guard': IQ['guard'],                 # ok（= 采集档）/ bypassed（人工绕过，须审计）
            'src': IQFP.get('src'),                  # 静态指纹来源（IQ 路径 × scene 数）
            'scenes': IQFP['scenes'],                # 8 子项 × 每 scene_isp30（解析失败则空）
            '_pending': [] if IQFP['ok'] else ['isp_fingerprint_static'],
        },
        'sensor_ctrl': sc,                           # 采集时刻 v4l2-ctl 回读（失败则 null）
        'sensor_ctrl_error': (sc_why or None) if not sc else None,   # 回读失败原因（不静默）
        'csc': None,                                 # 采集链路 v4l2 直出、未过 RGA ⇒ 无 CSC 可记
        # 光照（**G3 补齐**，§12.3/§12.4）：**逐帧** Y 均值（零人工）+ **批次**照度锚点（一次录入）。
        # ⚠️ 帧均值与场景反射率混淆 ⇒ 跨场景不可直接比；跨场景光照比较只能靠锚点。
        'illuminance': {
            'method': 'y_mean + batch_anchor',   # 口径声明（§12.3 / §12.4）
            'frame_y_mean': round(ymean, 4),     # 逐帧 Y 平面均值（相对光照指标）
            'y_mean_sampling': '1/4 隔行隔列 → %d 样本' % nsamp,
            'lux_anchor': a.lux_anchor,          # 批次级照度计实测（Lux）；null = 未录
            'lux_anchor_note': (a.lux_anchor_note or None),
            '_pending': [] if a.lux_anchor is not None else ['lux_anchor'],
        },
        # 光源（批次级；**本组为设计选择，不涉 config 键**，见 `dataset.md` §6.1）
        'light_source': {
            'type': (a.light_type or None),
            'cct_k': a.light_cct_k,
            'dimming': (a.light_dimming or None),   # 与色温漂移判别直接相关
            'distance_m': a.light_distance_m,
            'note': (a.light_note or None),
            '_pending': light_pending,
        },
        'optics': None,
        'split': None,
        'dataset_version': None,
        '_not_applicable': {
            'csc': '采集链路为 RKISP mainpath v4l2 直出，未过 RGA ⇒ 无 CSC 矩阵/range 可记；'
                   'O54 的 CSC 显式化只影响交付链路与离线 RGB 转换工具',
        },
        '_pending': pending,
    }


def do_capture(kind, a):
    """抓当前**无损裸 NV12**帧落盘。返回 (ok, 文案)。"""
    # O62 ④(ii) 防呆：**非采集档一律拒采**（fail-closed，防"忘切"静默污染数据集）
    if IQ['guard'] == 'blocked':
        return False, '拒绝采集（O62 ④(ii) IQ 档位防呆）：%s' % IQ['why']
    # 1080p@30 锁定防呆（§9.5 ④）：未锁定即 60 fps ⇒ 曝光域与演示链路不同构
    if not LOCK['ok']:
        return False, '拒绝采集（1080p@30 锁定未生效）：%s' % LOCK['why']
    with STATE['lock']:
        raw = STATE['raw']
        raw_t = STATE['raw_t']
    if raw is None:
        return False, '还没有无损帧（路② 未就绪）：%s' % (STATE['raw_err'] or '检查 gst 是否在跑')
    # 陈旧判定**跟数据源走**：数据取自路② 裸帧，故以"裸帧是否仍在更新"为准。
    # （旧口径看路① JPEG 的 `dup_run`——那是 mppjpegenc 陈旧缓冲的征候，与路② 数据无关，
    #   且静止画面下会误拦。`dup_run` 保留为页面诊断指标，见 README §7。）
    age_ms = (time.monotonic_ns() - raw_t) // 1000000
    if age_ms > 1000:
        return False, '无损帧已陈旧（%.0f ms 未更新），拒绝采集' % age_ms

    scene = scene_id()
    st = STATE['counts'].setdefault(
        scene, {'slot': 1, 'has': {'dark': False, 'gt': False}, 'n': {'dark': 0, 'gt': 0}})
    if st['has'].get(kind):
        # 本帧槽该档位已有 ⇒ 开新的一帧槽（U1 靠此在同一取景内采多张）
        st['slot'] += 1
        st['has'] = {'dark': False, 'gt': False}
    slot = st['slot']

    name = frame_name(scene, slot, kind)
    path = os.path.join(STATE['outdir'], name)
    existed = os.path.exists(path)
    with open(path, 'wb') as f:                     # 裸 NV12，定长、无容器
        f.write(raw)
    # sidecar **逐帧一份**（与图像同名同主干）⇒ 后采的档位不再覆盖前一份
    with open(os.path.join(STATE['outdir'], os.path.splitext(name)[0] + '.json'), 'w') as f:
        json.dump(_sidecar(scene, slot, kind, path, raw, a), f, indent=2, ensure_ascii=False)

    st['has'][kind] = True
    st['n'][kind] += 1
    _state_save()
    msg = '%s 已保存（无损 NV12, %d KB；场景 %s 帧槽 %04d）%s' % (
        name, len(raw) // 1024, scene, slot, '［覆盖原有文件］' if existed else '')
    STATE['last'] = msg
    return True, msg


def do_nextscene():
    scene = scene_id()
    st = STATE['counts'].get(scene) or {}
    has = st.get('has') or {}
    n = st.get('n') or {}
    missing = [k for k in KINDS if not has.get(k)]
    STATE['next_scene'] += 1
    _state_save()
    msg = '%s 结束（帧槽 %04d 缺 %s；累计 暗 %d / 亮 %d）→ 进入 %s' % (
        scene, st.get('slot', 1), '/'.join(missing) if missing else '无',
        n.get('dark', 0), n.get('gt', 0), scene_id())
    STATE['last'] = msg
    return True, msg


# ---------------------------------------------------------------- 页面

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IMX415 成对采集</title>
<style>
 body{margin:0;background:#111;color:#ddd;font:14px/1.6 monospace}
 .bar{padding:8px 10px;background:#1d1d1d;position:sticky;top:0;z-index:2}
 .ctl{display:flex;gap:8px;padding:10px;flex-wrap:wrap;align-items:center}
 button{font:16px/1 monospace;padding:16px 18px;border:0;border-radius:6px;
        background:#2a4a6a;color:#dff;min-width:120px}
 button:active{background:#3a6a9a}
 button.gt{background:#2a5a2a;color:#dfd}
 button.nx{background:#5a3a2a;color:#fed}
 .st{font-size:15px}
 .ok{color:#8f8}.no{color:#f88}
 .msg{padding:8px 10px;background:#161616;color:#7fd1ff;min-height:22px;white-space:pre-wrap}
 .iq{padding:8px 10px;background:#3a1d1d;color:#fbb;white-space:pre-wrap;display:none}
 .iq.ok{background:#1d3a1d;color:#bfb}
 .lk{padding:8px 10px;background:#3a2a1d;color:#fdb;white-space:pre-wrap;display:none}
 .lk.ok{background:#1d3a2d;color:#bfb}
 .fp{padding:8px 10px;background:#2a1d3a;color:#dcb;white-space:pre-wrap;display:none}
 .fp.ok{background:#1d2a3a;color:#bdf}
 .lg{padding:8px 10px;background:#1d2a2a;color:#bdd;white-space:pre-wrap}
 .lg.warn{background:#3a2a1d;color:#fdb}
 .wrap{display:flex;flex-wrap:wrap;gap:8px;padding:8px}
 .pane{flex:1 1 46%;min-width:300px}
 canvas{width:100%;background:#000;display:block}
 b{color:#7fd1ff}
</style></head><body>
<div class="bar">
 <span class="st">场景 <b id="sc">-</b> 帧槽 <b id="sl">-</b> &nbsp;
  本槽暗帧 <span id="dk" class="no">-</span> &nbsp;
  本槽亮帧GT <span id="gt" class="no">-</span></span>
 &nbsp; 批次 <b id="ba">-</b> &nbsp; JPEG 帧序 <b id="q">-</b> &nbsp; 裸帧序 <b id="rq">-</b>
 &nbsp; JPEG <b id="kb">-</b> KB &nbsp; 重复帧 <b id="d">-</b> &nbsp; 刷新 <b id="f">-</b> fps
 &nbsp; 清晰度 <b id="s">-</b> &nbsp; 本次最大 <b id="mx">-</b>
</div>
<div class="iq" id="iq"></div>
<div class="lk" id="lk"></div>
<div class="fp" id="fp"></div>
<div class="lg" id="lg"></div>
<div class="ctl">
 <button class="gt" id="bgt">采集亮帧 GT</button>
 <button id="bdk">采集暗帧</button>
 <button class="nx" id="bnx">更换场景</button>
</div>
<div class="msg" id="msg">就绪。成对采集：摆取景 → 调亮光源 →【采集亮帧 GT】→ 调暗光源 →【采集暗帧】→【更换场景】。U1 单边图：全程只按【采集暗帧】，同一取景内改光照后再按一次（帧槽 +1）。落盘为无损裸 NV12。</div>
<div class="wrap">
 <div class="pane"><div>全幅</div><canvas id="c1" width="960" height="540"></canvas></div>
 <div class="pane"><div>中心 4× 放大（对焦核对）</div><canvas id="c2" width="1920" height="1080"></canvas></div>
</div>
<canvas id="c3" width="480" height="270" style="display:none"></canvas>
<img id="im" style="display:none">
<script>
const im=document.getElementById('im'),c1=document.getElementById('c1'),c2=document.getElementById('c2'),
      c3=document.getElementById('c3'),s=document.getElementById('s'),mx=document.getElementById('mx'),
      q=document.getElementById('q'),f=document.getElementById('f'),kb=document.getElementById('kb'),
      d=document.getElementById('d'),sc=document.getElementById('sc'),dk=document.getElementById('dk'),
      gt=document.getElementById('gt'),ba=document.getElementById('ba'),msg=document.getElementById('msg'),
      sl=document.getElementById('sl'),rq=document.getElementById('rq'),iq=document.getElementById('iq'),
      lk=document.getElementById('lk'),fp=document.getElementById('fp'),lg=document.getElementById('lg');
let best=0,n=0,t0=performance.now();

function mark(el,on){ el.textContent = on?'已采':'未采'; el.className = on?'ok':'no'; }

im.onload=()=>{
  c1.getContext('2d').drawImage(im,0,0,960,540);
  const ox=(im.naturalWidth-480)/2|0, oy=(im.naturalHeight-270)/2|0;
  c2.getContext('2d').drawImage(im,ox,oy,480,270,0,0,1920,1080);
  const x3=c3.getContext('2d'); x3.drawImage(im,ox,oy,480,270,0,0,480,270);
  const dd=x3.getImageData(0,0,480,270).data, g=new Float32Array(480*270);
  for(let i=0,j=0;i<dd.length;i+=4,j++) g[j]=0.299*dd[i]+0.587*dd[i+1]+0.114*dd[i+2];
  let sum=0,sum2=0,m=0;
  for(let y=1;y<269;y++)for(let x=1;x<479;x++){
    const i=y*480+x, l=4*g[i]-g[i-1]-g[i+1]-g[i-480]-g[i+480];
    sum+=l; sum2+=l*l; m++;
  }
  const v=sum2/m-(sum/m)*(sum/m);
  if(v>best)best=v;
  s.textContent=v.toFixed(1); mx.textContent=best.toFixed(1);
};

async function act(url){
  try{
    const r=await fetch(url,{method:'POST'});
    const j=await r.json();
    msg.textContent=j.msg;
  }catch(e){ msg.textContent='请求失败：'+e; }
}
document.getElementById('bdk').onclick=()=>act('/capture?kind=dark');
document.getElementById('bgt').onclick=()=>act('/capture?kind=gt');
document.getElementById('bnx').onclick=()=>act('/nextscene');

async function loop(){
  im.src='/snap.jpg?ts='+Date.now();
  try{
    const r=await fetch('/stat'); const j=await r.json();
    q.textContent=j.seq; rq.textContent=j.raw_seq; kb.textContent=(j.bytes/1024|0);
    d.textContent=j.dup_run;
    sc.textContent=j.scene_id; sl.textContent=('0000'+j.slot).slice(-4); ba.textContent=j.batch;
    mark(dk, j.cur.dark); mark(gt, j.cur.gt);
    if(j.iq){
      iq.style.display='block';
      iq.className='iq'+(j.iq.guard==='ok'?' ok':'');
      iq.textContent='IQ 档位：'+(j.iq.set||'?')+'　'
        +(j.iq.guard==='ok' ? '✓ 采集档，可采集'
          : j.iq.guard==='bypassed' ? '⚠ 已人工绕过防呆（--no-iq-guard），采集将留痕'
          : '✗ 拒绝采集')+(j.iq.why?'　—　'+j.iq.why:'');
    }
    if(j.lock){
      lk.style.display='block';
      lk.className='lk'+(j.lock.ok?' ok':'');
      lk.textContent='帧率锁定：'+(j.lock.ok
          ? '✓ 已施加（'+j.lock.node+' vertical_blanking='+j.lock.readback+' ⇒ VMAX '+(j.lock.readback+1080)+' 行），可采集'
          : '✗ 未生效，拒绝采集')+(j.lock.why?'　—　'+j.lock.why:'');
    }
    if(j.fp){
      fp.style.display='block';
      fp.className='fp'+((j.fp.ok&&j.sensor&&j.sensor.ok)?' ok':'');
      const rb=j.sensor&&j.sensor.readback||{};
      fp.textContent='ISP 指纹（G5）：'+(j.fp.ok
            ? '✓ 静态 8 子项 × '+j.fp.scenes+' 场景已解析'
            : '✗ 解析失败'+(j.fp.why?'（'+j.fp.why+'）':''))
        +'　|　传感器回读：'+((j.sensor&&j.sensor.ok)
            ? '✓ '+j.sensor.device+' exposure='+rb.exposure+' analogue_gain='+rb.analogue_gain
              +' vertical_blanking='+rb.vertical_blanking
            : '✗ '+(j.sensor&&j.sensor.why||'未回读'));
    }
    if(j.light){
      const L=j.light, miss=[];
      if(L.lux_anchor==null) miss.push('照度锚点');
      if(!L.type) miss.push('光源类型');
      if(L.cct_k==null) miss.push('色温');
      if(!L.dimming) miss.push('调光方式');
      lg.className='lg'+(miss.length?' warn':'');
      lg.textContent='光照/光源（G3）：照度锚点='+(L.lux_anchor==null?'未录':L.lux_anchor+' Lux')
        +'　光源='+(L.type||'-')+' / '+(L.cct_k==null?'-':L.cct_k+'K')+' / 调光='+(L.dimming||'-')
        +' / '+(L.distance_m==null?'-':L.distance_m+' m')
        +(L.note?'（'+L.note+'）':'')
        +'　本帧 Y 均值='+(L.y_mean==null?'（尚未采集）':L.y_mean)
        +(miss.length?'　⚠ 缺：'+miss.join('、'):'');
    }
  }catch(e){}
  n++; const el=(performance.now()-t0)/1000;
  if(el>1){ f.textContent=(n/el).toFixed(1); n=0; t0=performance.now(); }
  setTimeout(loop,200);
}
loop();
</script></body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    ARGS = None                      # 由 main() 注入

    def log_message(self, fmt, *a):
        pass

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode(), 'application/json', code)

    def do_GET(self):
        if self.path.startswith('/snap.jpg'):
            with STATE['lock']:
                jpg = STATE['jpg']
            if jpg is None:
                self.send_error(503, 'no frame yet')
                return
            self._send(jpg, 'image/jpeg')
        elif self.path.startswith('/stat'):
            scene = scene_id()
            st = STATE['counts'].get(scene) or {}
            has = st.get('has') or {}
            self._json({'seq': STATE['seq'], 'raw_seq': STATE['raw_seq'],
                        'bytes': STATE['bytes'],
                        'dup_run': STATE['dup_run'], 'err': STATE['err'],
                        'raw_err': STATE['raw_err'],
                        'scene_id': scene, 'slot': st.get('slot', 1),
                        'batch': STATE['batch'],
                        'cur': {'dark': bool(has.get('dark')), 'gt': bool(has.get('gt'))},
                        'iq': {'set': IQ['tag'], 'guard': IQ['guard'],
                               'file': IQ['file'], 'sha256': IQ['sha256'], 'why': IQ['why']},
                        'lock': {'ok': LOCK['ok'], 'node': LOCK['node'],
                                 'vblank_lines': LOCK['vblank_lines'], 'readback': LOCK['readback'],
                                 'why': LOCK['why']},
                        'fp': {'ok': IQFP['ok'], 'scenes': len(IQFP['scenes']),
                               'src': IQFP['src'], 'why': IQFP['why']},
                        'sensor': {'ok': SENSOR['ok'], 'device': SENSOR['device'],
                                   'readback': SENSOR['readback'], 'why': SENSOR['why']},
                        'light': dict(LIGHT),
                        'last': STATE['last']})
        else:
            self._send(PAGE.encode(), 'text/html; charset=utf-8')

    def do_POST(self):
        # 采集/换场景**有副作用**，故用 POST（GET 只读，见 cam_preview 的做法）
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == '/capture':
            kind = (qs.get('kind') or [''])[0]
            if kind not in KINDS:
                self._json({'ok': False, 'msg': 'kind 非法：%s' % kind}, 400)
                return
            ok, m = do_capture(kind, self.ARGS)
            self._json({'ok': ok, 'msg': m}, 200 if ok else 409)
        elif u.path == '/nextscene':
            ok, m = do_nextscene()
            self._json({'ok': ok, 'msg': m})
        else:
            self.send_error(404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--outdir', default=os.path.expanduser('~/capture_data'),
                    help='落盘目录（**v3.86 裁定 O102 后 `storage.dir` 已取消**），不入 git')
    ap.add_argument('--port', type=int, default=8091)
    ap.add_argument('--device', default='/dev/video11')
    ap.add_argument('--w', type=int, default=1920)
    ap.add_argument('--h', type=int, default=1080)
    ap.add_argument('--batch', default='', help='采集批次号，留空自动生成')
    ap.add_argument('--raw-frame-size', type=int, default=0,
                    help='裸 NV12 单帧字节数；0 = 按 w*h*3/2 推算（默认；板端 1920x1080 实测 3110400）')
    ap.add_argument('--iq-dir', default='/etc/iqfiles',
                    help='3A 的 IQ 加载目录（**不可配置**，仅为便于离线演练而留）')
    ap.add_argument('--iq-manifest', default='/etc/iqfiles_sets/manifest.json',
                    help='IQ 档位 manifest（由 `tools/collect/iq_sets.py` 生成）')
    ap.add_argument('--no-iq-guard', action='store_true',
                    help='**调试用**：绕过 O62 ④(ii) 的"当前 IQ = 采集档"防呆。'
                         '绕过会记入 sidecar（isp_fingerprint.iq_guard = bypassed）以便审计')
    ap.add_argument('--vblank-lines', type=int, default=7906,
                    help='帧率锁定的 `vertical_blanking` 值 = config `camera.vblank_lines`'
                         '（§10.4；**O91 定稿 = 7906 ⇒ VMAX 8986 行 ≈ 66.8 ms ≈ 15 fps**；'
                         '历史 v3.79 的 3405 = 30 fps 值，已作废）。**须在取流前施加**')
    ap.add_argument('--sensor-subdev', default=None,
                    help='显式指定 sensor subdev（调试用；默认按"唯一暴露 V4L2_CID_VBLANK"运行时解析）')
    # ── 光照与光源（**G3 补齐**；设计档 §12.3/§12.4）──────────────────────────
    # **批次级**：逐帧只算 Y 均值（零人工），照度与光源信息**每批次录一次**（§12.4）。
    # 光源条件若在批次中途变化 ⇒ 应换 `--batch` 重开一批（否则记录与事实不符）。
    ap.add_argument('--lux-anchor', type=float, default=None,
                    help='批次级**照度计实测**（Lux，一次）。写入每帧 sidecar 的 illuminance.lux_anchor；'
                         '不填 = null（**不臆造**）。⚠️ 系统本身仍需手动档 + Y 均值作相对指标')
    ap.add_argument('--lux-anchor-note', default='',
                    help='照度锚点的测量方式/照度计型号/测点位置（人读）')
    ap.add_argument('--light-type', default='',
                    help='光源类型（如 LED / 白炽 / 荧光 / 自然光）')
    ap.add_argument('--light-cct-k', type=int, default=None,
                    help='光源标称色温（K）；未测则不填')
    ap.add_argument('--light-dimming', default='',
                    help='调光方式（none / pwm / current / 换灯 / 遮挡 …）——'
                         '**与色温漂移判别直接相关**，建议填写')
    ap.add_argument('--light-distance-m', type=float, default=None,
                    help='光源到被摄物距离（m）')
    ap.add_argument('--light-note', default='',
                    help='光源自由说明（数量 / 布光方式 / 位置 …）')
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    STATE['outdir'] = a.outdir
    STATE['batch'] = a.batch or datetime.now().strftime('B%Y%m%d-%H%M')
    _state_load()
    Handler.ARGS = a

    # O62 ④(ii) 防呆：**启动即校验**「当前 IQ = 采集档」；不通过则**拒绝采集**（预览/对焦仍可用）
    ok_iq = iq_guard(a.iq_dir, a.iq_manifest, bypass=a.no_iq_guard)
    if ok_iq and IQ['guard'] == 'ok':
        print('[capture] IQ 防呆：**通过**（档位 = %s，sha256 = %s）' % (IQ['tag'], IQ['sha256']))
    elif ok_iq:
        print('[capture] ⚠ IQ 防呆：**已人工绕过**（--no-iq-guard）——%s\n'
              '[capture] ⚠ 本会话采集的 sidecar 会记 iq_guard=bypassed，须在证据中说明'
              % IQ['why'])
    else:
        print('[capture] ✗ IQ 防呆：**拒绝采集**——%s\n'
              '[capture]   预览/对焦仍可用；换档后重启本工具即可解除' % IQ['why'])
    sys.stdout.flush()

    # 1080p 帧率锁定（§9.5 ④ / §10.4 `camera.vblank_lines`）：**必须在 v4l2src 开流之前**施加
    # （停流即复位 ⇒ 每次启动都要重施）。O91 定稿 = 15 fps ⇒ 默认 `--vblank-lines 7906`。
    if lock_guard(a.vblank_lines, a.sensor_subdev):
        print('[capture] 帧率锁定：**已施加**（%s vertical_blanking=%d，回读 %s ⇒ VMAX %d 行）'
              % (LOCK['node'], LOCK['vblank_lines'], LOCK['readback'],
                 LOCK['readback'] + a.h))
    else:
        print('[capture] ✗ 帧率锁定：**未生效**——%s\n'
              '[capture]   预览/对焦仍可用；**采集会被拒绝**（见页面提示）' % LOCK['why'])
    sys.stdout.flush()

    # G5：解析当前 IQ 的静态指纹（8 子项 × 每 scene_isp30）+ AE 手动档期望值。
    # 失败**不静默**：页面/日志告警，sidecar 相应字段留 `_pending`。
    if IQ['file'] and not iq_fp_build(a.iq_dir):
        print('[capture] ⚠ ISP 指纹解析失败（G5）：%s\n'
              '[capture]   采集仍可进行，但 sidecar 的 isp_fingerprint 仅含 iq_file/iq_hash'
              % IQFP['why'])
    elif IQFP['ok']:
        print('[capture] ISP 指纹（G5）：已解析 %d 个 scene_isp30 子项（%s）%s'
              % (len(IQFP['scenes']), IQFP['src'], IQFP['why'] or ''))
    sys.stdout.flush()

    # G3：光照/光源记录状态（**批次级**，缺项不阻断，但启动时讲清楚）
    # 先把**批次设定**灌进 LIGHT ⇒ 页面横幅在**首帧之前**就能显示它们
    # （否则 `LIGHT` 只在 `_sidecar` 里更新，首帧前会误示"照度锚点=未录"）
    LIGHT.update(lux_anchor=a.lux_anchor, lux_anchor_note=(a.lux_anchor_note or None),
                 type=(a.light_type or None), cct_k=a.light_cct_k,
                 dimming=(a.light_dimming or None), distance_m=a.light_distance_m,
                 note=(a.light_note or None))
    _lm = [k for k, v in (('--light-type', a.light_type), ('--light-cct-k', a.light_cct_k),
                          ('--light-dimming', a.light_dimming),
                          ('--light-distance-m', a.light_distance_m)) if v in (None, '')]
    if a.lux_anchor is None or _lm:
        print('[capture] ⚠ 光照/光源记录**不完整**：照度锚点=%s；光源缺项=%s\n'
              '[capture]   逐帧 Y 均值仍照常写入；缺项在 sidecar 记 null + _pending（不臆造）'
              % ('未录' if a.lux_anchor is None else '%g Lux' % a.lux_anchor,
                 '、'.join(_lm) or '无'))
    else:
        print('[capture] 光照/光源：照度锚点 %g Lux；光源 %s / %sK / 调光 %s / %.2f m'
              % (a.lux_anchor, a.light_type, a.light_cct_k, a.light_dimming, a.light_distance_m))
    sys.stdout.flush()

    # 裸 NV12 几何：rkisp mainpath 实测单平面 continuous、bytesperline = w、sizeimage = w*h*3/2
    # （`environment/board-lubancat5.md` §7）。侧记入 sidecar，供开发机侧正确解析。
    RAW['stride'] = a.w
    RAW['uv_offset'] = a.w * a.h
    RAW['frame_bytes'] = a.raw_frame_size or (a.w * a.h * 3 // 2)

    r_raw, w_raw = os.pipe()                     # 路② 的管道：写端交给 gst，读端本进程读
    global GST_CMD
    GST_CMD = ['gst-launch-1.0', '-q', 'v4l2src', 'device=' + a.device,
               '!', 'video/x-raw,format=NV12,width=%d,height=%d' % (a.w, a.h),
               # 必须先经过 videoconvert 做一次真实拷贝：实测若把 v4l2src 的缓冲直连 mppjpegenc，
               # 硬编会反复编码同一块陈旧缓冲 ⇒ 页面"画面冻住"（帧序号仍在涨）。见文件头"继承的实测坑"。
               '!', 'videoconvert', '!', 'video/x-raw,format=NV12',
               '!', 'tee', 'name=t',
               # 路① JPEG：仅供浏览器预览，**不进数据集**
               't.', '!', 'queue', '!', 'mppjpegenc', '!',
               'multipartmux', 'boundary=frame', '!', 'fdsink', 'fd=1',
               # 路② 裸 NV12：落盘用。leaky=downstream = 满时丢**旧**缓冲（GstQueueLeaky 语义：
               # upstream 丢新、downstream 丢旧）⇒ 只保留最新帧，且慢读者不会把主链路堵住。
               't.', '!', 'queue', 'leaky=downstream', 'max-size-buffers=2', 'silent=true',
               '!', 'fdsink', 'fd=%d' % w_raw]
    proc = subprocess.Popen(GST_CMD, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            pass_fds=(w_raw,))
    os.close(w_raw)                              # 父进程关写端，读端才能在 gst 退出时收到 EOF
    threading.Thread(target=reader, args=(proc,), daemon=True).start()
    threading.Thread(target=raw_reader, args=(r_raw,), daemon=True).start()
    threading.Thread(target=err_reader, args=(proc,), daemon=True).start()

    srv = http.server.ThreadingHTTPServer(('0.0.0.0', a.port), Handler)
    print('[capture] http://0.0.0.0:%d/  device=%s %dx%d  outdir=%s  batch=%s  下一场景=%s\n'
          '[capture] 无损裸 NV12: stride=%d uv_offset=%d frame_bytes=%d（路② fd=%d）'
          % (a.port, a.device, a.w, a.h, a.outdir, STATE['batch'], scene_id(),
             RAW['stride'], RAW['uv_offset'], RAW['frame_bytes'], r_raw))
    sys.stdout.flush()

    def stop(*_):
        proc.send_signal(signal.SIGINT)
        # ⚠️ `srv.shutdown()` **必须在另一线程调用**：`serve_forever()` 跑在主线程，
        # 而信号处理函数也在主线程 ⇒ 原地直调会**自锁**（shutdown 阻塞等 serve_forever 返回，
        # 后者又永远不返回）⇒ 进程退出后仍占着端口、下次启动报 `Address already in use`
        # （2026-09-29 实测：gst 已退、`/dev/video11` 已释放，但 python 仍 LISTEN 8091）。
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proc.kill()


if __name__ == '__main__':
    main()
