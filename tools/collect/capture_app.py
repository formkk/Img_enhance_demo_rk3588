#!/usr/bin/env python3
"""IMX415 成对数据集采集（场景号前缀落盘 + sidecar 元数据）：板上取流 → 手机/PC 浏览器操作。

**与 `tools/cam_preview.py` 的关系**：本文件是**独立自包含**的扩展版，**不 import、不修改**
原对焦应用（原文件保持可独立使用）。取流与 multipart 解析逻辑沿用其做法，见下"继承的实测坑"。

用途：M1 阶段的**实拍配对采集**（暗帧 + 亮帧 GT），服务于 `docs/design/dataset.md` 的
U3（HQ 配对训练）与 U4（评测）。**不是交付链路**——交付显示路径见主基准 §3.3/§3.4。

**采集范式（依 `docs/design/dataset.md` §4.5 的 (a) 改良版）**：固定 ISP 参数与曝光，
**改光源亮度**而非改曝光 ⇒ 同一场景下暗帧与亮帧的 ISP 行为完全一致、几何天然对齐、
3DNR 不错配。故操作序为：摆好场景 → 调亮光源 → 按【采集亮帧 GT】→ 调暗光源 → 按【采集暗帧】
→ 按【更换场景】进入下一场景。

用法（板上）：
  python3 tools/collect/capture_app.py --outdir ~/capture_data [--port 8091]
      [--device /dev/video11] [--w 1920] [--h 1080] [--batch B001]
手机/PC 浏览器打开： http://<板地址>:8091/

落盘约定（`--outdir` 下）：
  S001_dark.jpg   暗帧（增强链路的输入域）
  S001_gt.jpg     亮帧（配对 GT）
  S001.json       该场景的 sidecar 元数据（字段分组见 `docs/design/dataset.md` §6.1）
  .capture_state.json   采集进度（下一场景号、批次），**重启后不丢**

依赖：板上 `gst-launch-1.0`（含 `v4l2src` / `mppjpegenc` / `multipartmux` / `fdsink`），
python3 仅用标准库（与原对焦应用一致，无第三方依赖）。

继承的实测坑（勿改）：
  1. `/dev/video11` 是 **Multiplanar** 节点，`ffmpeg/ffplay/mpv` 打不开，必须走 GStreamer。
  2. `v4l2src` **直连** `mppjpegenc` 会反复编码同一块陈旧缓冲 ⇒ 画面"冻住"（帧序号仍在涨）。
     必须前置 `videoconvert` 做一次真实拷贝。`dup_run` 指标用于监视这类故障。
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

# 采集档位 → 文件名后缀。GT 命名依用户 2026-09-19 决定：S001_dark.jpg / S001_gt.jpg
KINDS = {'dark': 'dark', 'gt': 'gt'}

STATE = {
    'jpg': None, 'seq': 0, 'bytes': 0, 'err': '', 'crc': 0, 'dup_run': 0,
    'lock': threading.Lock(),
    # 采集侧状态（由 _state_load 初始化）
    'outdir': '', 'batch': '', 'next_scene': 1,
    'last': '',            # 最近一次操作的结果文案，回显到页面
    'counts': {},          # 已采场景的 {scene_id: {'dark':bool,'gt':bool}}
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
        print('[capture] 进度文件读取失败（忽略，从 S001 起）：%s' % e)


def _state_save():
    tmp = _state_path() + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'next_scene': STATE['next_scene'], 'batch': STATE['batch'],
                   'counts': STATE['counts']}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _state_path())          # 原子替换，避免断电写坏


def scene_id(n=None):
    return 'S%03d' % (STATE['next_scene'] if n is None else n)


# ---------------------------------------------------------------- 落盘

def _sidecar(scene, kind, path, jpg_bytes, a):
    """写 sidecar。**只写本应用能确知的字段**；ISP 指纹/传感器 ctrl/照度等需另行取数，
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
    return {
        'schema': 'capture/v1',
        'scene_id': scene,
        'frame_id': '%s_%s' % (scene, kind),
        'pair_id': scene,
        'capture_batch': STATE['batch'],
        'kind': kind,                                    # dark = 暗帧（输入域）/ gt = 亮帧（配对 GT）
        'time': {'mono_ns': mono, 'utc_iso8601': utc},
        'capture_condition': {
            'device': a.device,
            'resolution': '%dx%d' % (a.w, a.h),
            'pixel_format': 'NV12',
            'gst_pipeline': ' '.join(GST_CMD),
        },
        'pair': {
            'pair_frame_id': '%s_%s' % (scene, 'gt' if kind == 'dark' else 'dark'),
            'gt_source': 'light_source_modulation' if kind == 'gt' else None,
            'geometry_alignment': 'same_frame_no_motion',   # 同机位、仅改光源 ⇒ 天然对齐
        },
        'file': {'name': os.path.basename(path), 'bytes': len(jpg_bytes)},
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
    """抓当前帧落盘。返回 (ok, 文案)。"""
    with STATE['lock']:
        jpg = STATE['jpg']
        seq = STATE['seq']
        dup = STATE['dup_run']
    if jpg is None:
        return False, '还没有帧，检查 gst 是否在跑'
    if dup >= 3:
        # 画面冻住时采到的是陈旧帧，采进数据集会污染，必须拦下
        return False, '画面疑似冻住（重复帧 %d），拒绝采集' % dup

    scene = scene_id()
    path = os.path.join(STATE['outdir'], '%s_%s.jpg' % (scene, kind))
    existed = os.path.exists(path)
    with open(path, 'wb') as f:
        f.write(jpg)
    with open(os.path.join(STATE['outdir'], '%s.json' % scene), 'w') as f:
        json.dump(_sidecar(scene, kind, path, jpg, a), f, indent=2, ensure_ascii=False)

    st = STATE['counts'].setdefault(scene, {'dark': False, 'gt': False})
    st[kind] = True
    _state_save()
    msg = '%s_%s.jpg 已保存（seq=%d, %d KB）%s' % (scene, kind, seq, len(jpg) // 1024,
                                                  '［覆盖原有文件］' if existed else '')
    STATE['last'] = msg
    return True, msg


def do_nextscene():
    scene = scene_id()
    st = STATE['counts'].get(scene) or {}
    missing = [k for k in KINDS if not st.get(k)]
    STATE['next_scene'] += 1
    _state_save()
    msg = '%s 结束%s → 进入 %s' % (scene,
                                 ('（缺 %s）' % '/'.join(missing)) if missing else '（暗/亮齐）',
                                 scene_id())
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
 <span class="st">场景 <b id="sc">-</b> &nbsp;
  暗帧 <span id="dk" class="no">-</span> &nbsp;
  亮帧GT <span id="gt" class="no">-</span></span>
 &nbsp; 批次 <b id="ba">-</b> &nbsp; 帧序号 <b id="q">-</b>
 &nbsp; JPEG <b id="kb">-</b> KB &nbsp; 重复帧 <b id="d">-</b> &nbsp; 刷新 <b id="f">-</b> fps
 &nbsp; 清晰度 <b id="s">-</b> &nbsp; 本次最大 <b id="mx">-</b>
</div>
<div class="ctl">
 <button class="gt" id="bgt">采集亮帧 GT</button>
 <button id="bdk">采集暗帧</button>
 <button class="nx" id="bnx">更换场景</button>
</div>
<div class="msg" id="msg">就绪。操作序：摆场景 → 调亮光源 →【采集亮帧 GT】→ 调暗光源 →【采集暗帧】→【更换场景】</div>
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
      gt=document.getElementById('gt'),ba=document.getElementById('ba'),msg=document.getElementById('msg');
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
    q.textContent=j.seq; kb.textContent=(j.bytes/1024|0); d.textContent=j.dup_run;
    sc.textContent=j.scene_id; ba.textContent=j.batch;
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
            cur = STATE['counts'].get(scene) or {}
            self._json({'seq': STATE['seq'], 'bytes': STATE['bytes'],
                        'dup_run': STATE['dup_run'], 'err': STATE['err'],
                        'scene_id': scene, 'batch': STATE['batch'],
                        'cur': {'dark': bool(cur.get('dark')), 'gt': bool(cur.get('gt'))},
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
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    STATE['outdir'] = a.outdir
    STATE['batch'] = a.batch or datetime.now().strftime('B%Y%m%d-%H%M')
    _state_load()
    Handler.ARGS = a

    global GST_CMD
    GST_CMD = ['gst-launch-1.0', '-q', 'v4l2src', 'device=' + a.device,
               '!', 'video/x-raw,format=NV12,width=%d,height=%d' % (a.w, a.h),
               # 必须先经过 videoconvert 做一次真实拷贝：实测若把 v4l2src 的缓冲直连 mppjpegenc，
               # 硬编会反复编码同一块陈旧缓冲 ⇒ 页面"画面冻住"（帧序号仍在涨）。见文件头"继承的实测坑"。
               '!', 'videoconvert', '!', 'video/x-raw,format=NV12',
               '!', 'mppjpegenc', '!', 'multipartmux', 'boundary=frame', '!', 'fdsink', 'fd=1']
    proc = subprocess.Popen(GST_CMD, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    threading.Thread(target=reader, args=(proc,), daemon=True).start()

    srv = http.server.ThreadingHTTPServer(('0.0.0.0', a.port), Handler)
    print('[capture] http://0.0.0.0:%d/  device=%s %dx%d  outdir=%s  batch=%s  下一场景=%s'
          % (a.port, a.device, a.w, a.h, a.outdir, STATE['batch'], scene_id()))
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
