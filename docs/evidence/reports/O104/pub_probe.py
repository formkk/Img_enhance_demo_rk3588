#!/usr/bin/env python3
"""pub_probe.py —— 只读验证：publish() 返回的是不是"刚估出来的那一块"缓冲"""
import sys, struct
sys.path.insert(0, "/home/cat/starlight")
import _starlight_native as _n
import capture as capm, config as C, curve_estim as ce, log as L

cfg = C.load(); logger = L.from_config(cfg)
cap = capm.Capture(cfg, logger); est = ce.CurveEstimator(cfg, logger)
assert cap.init() == 0 and cap.stream_on_once() == 0 and est.init() == 0
g = est.geom; plane = 3 * int(g["eff_w"]) * int(g["eff_h"])
def sig(b):
    return sum(struct.unpack_from("<e", b, i * 2)[0] for i in range(0, plane, 997))
k = 0
for _ in range(6):
    f = cap.acquire(1, 500)
    if f is None: continue
    est.estimate(f, alpha=est.alpha)
    cap.release(f["lease_id"])
    written = est._buffers[est._cur].to_bytes()      # estimate 写入的那一块
    snap = est.publish()
    pub = snap.field.to_bytes()                      # publish 返回的那一块
    other = est._buffers[est._cur ^ 1].to_bytes()
    k += 1
    print("est#%d  written(cur)=%+.4f  published=%+.4f  other=%+.4f   pub==written? %s"
          % (k, sig(written), sig(pub), sig(other), pub == written))
est.shutdown(); cap.shutdown()
