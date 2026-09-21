"""M2-1 ④ 满载口径 v2：**负载期间**密集采样 dmc/load（v1 的采样在循环后才做，只捕到衰减尾巴）。

  · 阶段1 静态基线 → 阶段2 两路逐帧写入+翻页（**循环内每帧采样**）→ 阶段3 恢复
"""
import sys
import time

sys.path.insert(0, "/home/cat/starlight")
import _starlight_native as _n
import config as starlight_config
import log as starlog
import display_mipi as dm
import display_hdmi as dh

DMC = "/sys/class/devfreq/dmc/load"


def dmc():
    try:
        with open(DMC) as f:
            return f.read().strip()
    except OSError:
        return "?"


def pct(s):
    return int(s.split("@")[0]) if s and s[0].isdigit() else -1


cfg = starlight_config.load()
logger = starlog.from_config(cfg)
mipi = dm.DisplayMipi(cfg, logger)
mipi.init(with_osd=False)
h = dh.DisplayHdmi(cfg, logger, None)
h._inited = True
h.init_canvas()
w0, h0 = mipi.canvas_w, mipi.canvas_h
w1, h1 = h.canvas_w, h.canvas_h
b0, b1 = w0 * h0 * 3, w1 * h1 * 3
print(f"槽0 {w0}x{h0}={b0/1e6:.2f}MB  槽1 {w1}x{h1}={b1/1e6:.2f}MB  crtc_count={_n.drm.info()['crtc_count']}")

# 阶段1：静态
time.sleep(3)
st = [dmc() for _ in range(20)]
print(f"\n[阶段1] 静态基线 20 次: {st}")

# 阶段2：满载，**循环内采样**
print("\n[阶段2] 满载（两路逐帧写入+翻页），循环内每 5 帧采样")
N = 600
p0, p1 = bytes([0x40]) * b0, bytes([0x80]) * b1
samples = []
t0 = time.monotonic_ns()
ok0 = ok1 = 0
for i in range(N):
    c0 = _n.drm.canvas(0, {"canvas_w": w0, "canvas_h": h0})
    c0.write(p0)
    if _n.drm.compose_flip(0) == 0:
        ok0 += 1
    c1 = _n.drm.canvas_slot(1, 0, {"canvas_w": w1, "canvas_h": h1})
    c1.write(p1)
    if _n.drm.compose_flip_slot(1, 0) == 0:
        ok1 += 1
    if i % 5 == 0:
        samples.append(dmc())
el = (time.monotonic_ns() - t0) / 1e9
vals = [pct(s) for s in samples if pct(s) >= 0]
print(f"  两路各 {N} 帧，用时 {el:.2f}s（{N/el:.1f} 帧对/s）")
print(f"  槽0 {ok0}/{N}  槽1 {ok1}/{N}")
print(f"  实测写入速率 = {(b0+b1)*N/el/1e6:.1f} MB/s")
print(f"  负载期 dmc/load 采样（{len(vals)} 个）: {samples}")
if vals:
    print(f"  → 峰值 {max(vals)}%  中位 {sorted(vals)[len(vals)//2]}%  均值 {sum(vals)/len(vals):.1f}%")

# 阶段3
time.sleep(3)
print(f"\n[阶段3] 停写后: {[dmc() for _ in range(10)]}")
h.shutdown()
mipi.shutdown()
