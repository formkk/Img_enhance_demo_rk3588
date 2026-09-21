"""M2-1 ④ 满载口径：两路画布**逐帧写入 + 翻页**时的 DDR 负载。

与 v3.83 的"扫描 + flip，无画布写入"不同，本脚本做**真实满载**：
  · 槽 0（DSI 1024×600×3 = 1.84 MB/帧）+ 槽 1（HDMI 1920×1080×3 = 6.22 MB/帧）
  · 每帧：整幅写入画布 → compose_flip_slot
  · 期间采样 /sys/class/devfreq/dmc/load

判据：与"静态基线"对比 DDR load 增量。
副作用：需 DRM master（先停桌面）；不做 DPMS / CRTC power-cycle。
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


def dmc_sample():
    try:
        with open(DMC) as f:
            return f.read().strip()
    except OSError:
        return "unreadable"


cfg = starlight_config.load()
logger = starlog.from_config(cfg)

# 槽 0 = DSI
mipi = dm.DisplayMipi(cfg, logger)
rc0 = mipi.init(with_osd=False)
# 槽 1 = HDMI
h = dh.DisplayHdmi(cfg, logger, None)
h._inited = True
h.init_canvas()
print(f"槽0 rc={rc0}  槽1 rc={h._drm_ok}  crtc_count={_n.drm.info()['crtc_count']}")

w0, h0 = mipi.canvas_w, mipi.canvas_h
w1, h1 = h.canvas_w, h.canvas_h
b0 = w0 * h0 * 3
b1 = w1 * h1 * 3
print(f"DSI 画布 {w0}x{h0} = {b0/1e6:.2f} MB/帧    HDMI 画布 {w1}x{h1} = {b1/1e6:.2f} MB/帧")

# ── 阶段 1：静态基线（两槽已配置，但不写不翻）──────────────
print("\n[阶段1] 静态基线（不写画布、不翻页）")
time.sleep(2)
static = [dmc_sample() for _ in range(10)]
print(f"  dmc/load 10 次采样: {static}")

# ── 阶段 2：满载（两路逐帧写入 + 翻页）──────────────────────
print("\n[阶段2] 满载：两路画布逐帧写入 + 翻页")
N = 300
t0 = time.monotonic_ns()
ok0 = ok1 = 0
payload0 = bytes([0x40]) * b0
payload1 = bytes([0x80]) * b1
for i in range(N):
    c0 = _n.drm.canvas(0, {"canvas_w": w0, "canvas_h": h0})
    c0.write(payload0)
    if _n.drm.compose_flip(0) == 0:
        ok0 += 1
    c1 = _n.drm.canvas_slot(1, 0, {"canvas_w": w1, "canvas_h": h1})
    c1.write(payload1)
    if _n.drm.compose_flip_slot(1, 0) == 0:
        ok1 += 1
elapsed = (time.monotonic_ns() - t0) / 1e9
print(f"  两路各 {N} 帧，用时 {elapsed:.2f}s（{N/elapsed:.1f} 帧对/s）")
print(f"  槽0 翻页 {ok0}/{N}  槽1 翻页 {ok1}/{N}")
print(f"  实测写入速率 = ({b0}+{b1})×{N}/{elapsed:.2f} = {(b0+b1)*N/elapsed/1e6:.1f} MB/s")

busy = [dmc_sample() for _ in range(10)]
print(f"  dmc/load 10 次采样: {busy}")

# ── 阶段 3：恢复静态，再采样 ────────────────────────────────
print("\n[阶段3] 停写后恢复静态")
time.sleep(2)
after = [dmc_sample() for _ in range(10)]
print(f"  dmc/load 10 次采样: {after}")

print(f"\n槽0 flip_count={_n.drm.info()['flip_count']} skipped={_n.drm.info()['flip_skipped']}")
print(f"槽1 flip_count={_n.drm.flip_count_slot(1)} skipped={_n.drm.flip_skipped_slot(1)}")
h.shutdown()
mipi.shutdown()
