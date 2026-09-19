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

用法（板上）：
  python3 tools/collect/capture_app.py --outdir ~/capture_data [--port 8091]
      [--device /dev/video11] [--w 1920] [--h 1080] [--batch B001]
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

# 采集档位。命名见主基准 v3.69：<日期>_<场景2位>_<帧序号>_<档位>.<后缀>
KINDS = ('dark', 'gt')          # dark = 暗帧（输入域）/ gt = 亮帧（配对 GT）

# 无损裸 NV12 的几何（由 main() 按 --w/--h/--raw-frame-size 落值）
RAW = {'stride': 0, 'uv_offset': 0, 'frame_bytes': 0}

# gst 命令行（main() 里落值；模块级先置空，避免引用未定义）
GST_CMD = []

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

    **只写本应用能确知的字段**；ISP 指纹/传感器 ctrl/照度等需另行取数，
    一律置 null 并列入 `_pending`，**不得填占位假值**（规则 `10-evidence.md` §2）。

    字段分组依 `docs/design/dataset.md` §6.1。
    """
    mono = time.monotonic_ns()                       # 计量域，唯一可比较的时钟
    utc = datetime.now(timezone.utc).isoformat()     # 仅供人读
    pending = [
        'isp_fingerprint',      # §6.1「ISP 指纹」全项：gamma 表 / CCM / black level / IQ 文件+哈希 /
                                #   AWB 锁定 / NR·3DNR 档 —— 需板上 rkisp 侧取数（未实现）
        'csc',                  # RGA CSC 矩阵 + range —— 本链路未过 RGA，取流为 v4l2 直出
        'sensor_ctrl',          # 曝光 / 模拟增益 / 数字增益 实测读回值 —— 需 v4l2-ctl 取数（未实现）
        'illuminance',          # 实测照度 + 测量方式（照度计型号/位置）—— 需人工录入（未实现）
        'optics',               # 镜头型号 + 对焦判据实测值 —— O31 关闭后才有
        'split',                # train/val/test 标签 —— 采集期不定，划分时回填
        'dataset_version',      # 数据集版本哈希 —— 打包时回填
    ]
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
        'isp_fingerprint': None,
        'csc': None,
        'sensor_ctrl': None,
        'illuminance': None,
        'optics': None,
        'split': None,
        'dataset_version': None,
        '_pending': pending,
    }


def do_capture(kind, a):
    """抓当前**无损裸 NV12**帧落盘。返回 (ok, 文案)。"""
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
      sl=document.getElementById('sl'),rq=document.getElementById('rq');
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
                    help='落盘目录。**不得指向 storage.dir**（受 §8.3 环形淘汰），也不入 git')
    ap.add_argument('--port', type=int, default=8091)
    ap.add_argument('--device', default='/dev/video11')
    ap.add_argument('--w', type=int, default=1920)
    ap.add_argument('--h', type=int, default=1080)
    ap.add_argument('--batch', default='', help='采集批次号，留空自动生成')
    ap.add_argument('--raw-frame-size', type=int, default=0,
                    help='裸 NV12 单帧字节数；0 = 按 w*h*3/2 推算（默认；板端 1920x1080 实测 3110400）')
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    STATE['outdir'] = a.outdir
    STATE['batch'] = a.batch or datetime.now().strftime('B%Y%m%d-%H%M')
    _state_load()
    Handler.ARGS = a

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
        srv.shutdown()
    signal.signal(signal.SIGTERM, stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proc.kill()


if __name__ == '__main__':
    main()
