"""第②批验证：单实例多 CRTC —— 槽0(DSI) + 槽1(HDMI) 双屏同时出图 + 各自翻页。"""
import sys
import time

sys.path.insert(0, "/home/cat/starlight")
import _starlight_native as _n
import config as starlight_config
import log as starlog
import display_mipi as dm
import display_hdmi as dh

cfg = starlight_config.load()
logger = starlog.from_config(cfg)

# 槽 0 = TH4（DSI）
mipi = dm.DisplayMipi(cfg, logger)
rc0 = mipi.init(with_osd=False)
print(f"槽0(DSI) configure rc={rc0}")
i0 = _n.drm.info()
print("  slot0:", {k: i0[k] for k in ("connector_id", "crtc_id", "mode_name", "hdisplay", "vdisplay")})

# 槽 1 = TH5（HDMI）
h = dh.DisplayHdmi(cfg, logger, None)
h._inited = True
rc1 = h.init_canvas()
print(f"槽1(HDMI) init_canvas rc={rc1} drm_ok={h._drm_ok}")
i1 = _n.drm.info_slot(1)
print("  slot1:", {k: i1[k] for k in ("connector_id", "crtc_id", "mode_name",
                                      "hdisplay", "vdisplay", "connector_status")})
print(f"  crtc_count = {i0['crtc_count']} (期望 2)")
print(f"  crtc 不同? {i0['crtc_id']} vs {i1['crtc_id']} -> {i0['crtc_id'] != i1['crtc_id']}")

# 两槽各翻 10 帧（交替，验证互不干扰）
ok0 = ok1 = 0
for i in range(10):
    c0 = _n.drm.canvas(0, {"canvas_w": mipi.canvas_w, "canvas_h": mipi.canvas_h})
    c0.write(bytes([(i * 20) % 256]) * (mipi.canvas_w * mipi.canvas_h * 3))
    if _n.drm.compose_flip(0) == 0:
        ok0 += 1
    c1 = _n.drm.canvas_slot(1, 0, {"canvas_w": h.canvas_w, "canvas_h": h.canvas_h})
    c1.write(bytes([(i * 20) % 256]) * (h.canvas_w * h.canvas_h * 3))
    if _n.drm.compose_flip_slot(1, 0) == 0:
        ok1 += 1
    time.sleep(0.02)
print(f"槽0 翻页 {ok0}/10  槽1 翻页 {ok1}/10")
print(f"  slot0 flip_count={_n.drm.info()['flip_count']}  slot1 flip_count={_n.drm.flip_count_slot(1)}")
print(f"  slot0 flip_skipped={_n.drm.info()['flip_skipped']}  slot1 flip_skipped={_n.drm.flip_skipped_slot(1)}")
h.shutdown()
mipi.shutdown()
