#!/usr/bin/env python3
"""CAM 实时预览（调焦/调 ISP 用）：板上 GStreamer 编 MJPEG → 本机浏览器看。

用途：M0/M1 bring-up 阶段的对焦与 ISP 观感核对。**不是交付链路**（TH4/TH5 的显示路径
见主基准 §3.3/§3.4，走 DRM 双 CRTC + DMABUF lease，与本脚本无关）。

为什么要绕一层：摄像头节点 `/dev/video11` 是 **Multiplanar** 设备，`ffmpeg/ffplay/mpv`
的 v4l2 输入打不开它（实测 `Not a video capture device`）；GStreamer 的 `v4l2src` 支持
MPlane，故编码侧用 gst，分发侧用 stdlib HTTP（GStreamer 1.18 的 `tcpserversink` 只有
GDP 模式，浏览器不认）。

用法（板上）：
  python3 tools/cam_preview.py [--device /dev/video11] [--port 8090] [--w 1920] [--h 1080]
本机浏览器打开： http://<板地址>:8090/
页面提供：全幅预览 + 中心 4× 放大 + 实时清晰度读数（Laplacian 方差，浏览器端算）。

依赖：板上 `gst-launch-1.0`（含 `v4l2src` / `mppjpegenc` / `multipartmux` / `fdsink`），
python3 仅用标准库。
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
import zlib

STATE = {'jpg': None, 'seq': 0, 'bytes': 0, 'err': '', 'crc': 0, 'dup_run': 0,
         'lock': threading.Lock()}
GST_CMD = []


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


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>CAM0 调焦预览</title>
<style>
 body{margin:0;background:#111;color:#ddd;font:13px/1.5 monospace}
 .bar{padding:6px 10px;background:#1d1d1d;position:sticky;top:0}
 .wrap{display:flex;flex-wrap:wrap;gap:8px;padding:8px}
 .pane{flex:1 1 46%;min-width:320px}
 canvas{width:100%;background:#000;display:block}
 b{color:#7fd1ff}
 .num{font-size:18px;color:#9f9}
</style></head><body>
<div class="bar">清晰度(中心1:1 Laplacian 方差) <span id="s" class="num">-</span>
 &nbsp; 本次最大 <b id="mx">-</b> &nbsp; 帧序号 <b id="q">-</b> &nbsp; 刷新 <b id="f">-</b> fps
 &nbsp; JPEG <b id="kb">-</b> KB &nbsp; 重复帧 <b id="d">-</b> &nbsp; <button id="p">暂停/继续</button>
 &nbsp; <a id="dl" href="/snap.jpg" style="color:#7fd1ff">下载当前原图</a></div>
<div class="wrap">
 <div class="pane"><div>全幅（1920×1080 → 960×540）</div>
   <canvas id="c1" width="960" height="540"></canvas></div>
 <div class="pane"><div>中心 4× 放大（取中心 480×270）</div>
   <canvas id="c2" width="1920" height="1080"></canvas></div>
</div>
<canvas id="c3" width="480" height="270" style="display:none"></canvas>
<img id="im" style="display:none">
<script>
const im=document.getElementById('im'),c1=document.getElementById('c1'),c2=document.getElementById('c2'),
      c3=document.getElementById('c3'),s=document.getElementById('s'),mx=document.getElementById('mx'),
      q=document.getElementById('q'),f=document.getElementById('f'),kb=document.getElementById('kb'),
      d=document.getElementById('d');
let paused=false,best=0,n=0,t0=performance.now(),seq=0;
document.getElementById('p').onclick=()=>{paused=!paused};
im.onload=()=>{
  c1.getContext('2d').drawImage(im,0,0,960,540);
  const ox=(im.naturalWidth-480)/2|0, oy=(im.naturalHeight-270)/2|0;
  c2.getContext('2d').drawImage(im,ox,oy,480,270,0,0,1920,1080);   // 4× 放大
  const x3=c3.getContext('2d'); x3.drawImage(im,ox,oy,480,270,0,0,480,270);
  const d=x3.getImageData(0,0,480,270).data, g=new Float32Array(480*270);
  for(let i=0,j=0;i<d.length;i+=4,j++) g[j]=0.299*d[i]+0.587*d[i+1]+0.114*d[i+2];
  let sum=0,sum2=0,m=0;
  for(let y=1;y<269;y++)for(let x=1;x<479;x++){
    const i=y*480+x, l=4*g[i]-g[i-1]-g[i+1]-g[i-480]-g[i+480];
    sum+=l; sum2+=l*l; m++;
  }
  const v=sum2/m-(sum/m)*(sum/m);
  if(v>best)best=v;
  s.textContent=v.toFixed(1); mx.textContent=best.toFixed(1);
  document.getElementById('dl').href='/snap.jpg?ts='+Date.now();
};
async function loop(){
  if(!paused){ im.src='/snap.jpg?ts='+Date.now(); }
  try{const r=await fetch('/stat');const j=await r.json();q.textContent=j.seq;kb.textContent=(j.bytes/1024|0);d.textContent=j.dup_run;}catch(e){}
  n++; const el=(performance.now()-t0)/1000; if(el>1){f.textContent=(n/el).toFixed(1); n=0; t0=performance.now();}
  setTimeout(loop,150);
}
loop();
</script></body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/snap.jpg'):
            with STATE['lock']:
                jpg = STATE['jpg']
            if jpg is None:
                self.send_error(503, 'no frame yet')
                return
            self._send(jpg, 'image/jpeg')
        elif self.path.startswith('/stat'):
            self._send(json.dumps({'seq': STATE['seq'], 'bytes': STATE['bytes'],
                                   'dup_run': STATE['dup_run'],
                                   'err': STATE['err']}).encode(), 'application/json')
        else:
            self._send(PAGE.encode(), 'text/html; charset=utf-8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', default='/dev/video11')
    ap.add_argument('--port', type=int, default=8090)
    ap.add_argument('--w', type=int, default=1920)
    ap.add_argument('--h', type=int, default=1080)
    a = ap.parse_args()

    cmd = ['gst-launch-1.0', '-q', 'v4l2src', 'device=' + a.device,
           '!', 'video/x-raw,format=NV12,width=%d,height=%d' % (a.w, a.h),
           # 必须先经过 videoconvert 做一次真实拷贝：实测若把 v4l2src 的缓冲直连 mppjpegenc，
           # 硬编会反复编码同一块陈旧缓冲 ⇒ 页面"画面冻住"（帧序号仍在涨）。见本文件头"实测坑"。
           '!', 'videoconvert', '!', 'video/x-raw,format=NV12',
           '!', 'mppjpegenc', '!', 'multipartmux', 'boundary=frame', '!', 'fdsink', 'fd=1']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    threading.Thread(target=reader, args=(proc,), daemon=True).start()

    srv = http.server.ThreadingHTTPServer(('0.0.0.0', a.port), Handler)
    print('[cam_preview] http://0.0.0.0:%d/  device=%s %dx%d' % (a.port, a.device, a.w, a.h))
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