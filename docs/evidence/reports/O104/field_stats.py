#!/usr/bin/env python3
"""field_stats.py —— 只读：打印曲线场（3ch α）的 min/max/mean 与 α 的符号"""
import os, sys, struct, time
sys.path.insert(0, "/home/cat/starlight")
import _starlight_native as _n
import capture as capm, config as C, curve_estim as ce, log as L

cfg = C.load(); logger = L.from_config(cfg)
cap = capm.Capture(cfg, logger); est = ce.CurveEstimator(cfg, logger)
assert cap.init() == 0 and cap.stream_on_once() == 0 and est.init() == 0
for _ in range(24):
    f = cap.acquire(1, 500)
    if f is None: continue
    est.estimate(f, alpha=est.alpha)
    cap.release(f["lease_id"])
snap = est.publish()
g = est.geom; cw, chh = int(g["eff_w"]), int(g["eff_h"])
b = snap.field.to_bytes()
plane = cw * chh
for ch in range(3):
    vals = struct.unpack_from("<%de" % plane, b, ch * plane * 2)
    mn, mx = min(vals), max(vals)
    mean = sum(vals) / plane
    print("ch%d  min=%.4f  max=%.4f  mean=%+.4f  neg_frac=%.3f" % (
        ch, mn, mx, mean, sum(1 for v in vals if v < 0) / plane))
# 模拟 OSD 的取值（clamp 到 [0,1]）
v = struct.unpack_from("<e", b, ((plane // 2) + cw // 2) * 2)[0]
print("中心像素 ch0 alpha = %+.4f ; OSD 画出的 y = clamp(x,0,1) = %.4f" % (v, max(0.0, min(1.0, v))))
est.shutdown(); cap.shutdown()
