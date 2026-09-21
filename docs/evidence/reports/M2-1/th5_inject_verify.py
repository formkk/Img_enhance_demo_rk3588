"""M2-1 ① 注入式验证：走真实 DisplayHdmi.poll() -> on_hotplug() -> Capture.set_rsm() 通路。

只替换「数据源」（sysfs status -> 可写文件），其余全部为交付代码路径：
  probe() / poll() 去抖 / on_hotplug() / set_rsm() / _transition() 状态机校验 / 日志。
不取 DRM master、不碰 V4L2、不扰动 HDMI PHY（规则 30-board-safety.md §4）。
"""
import sys
sys.path.insert(0, "/home/cat/starlight")
import config as starlight_config
import log as starlog
import capture as capture_mod
import display_hdmi as hdmi_mod

cfg = starlight_config.load("/tmp/th5_verify.yaml")   # INFO 档，能采到 hotplug
logger = starlog.from_config(cfg)

cap = capture_mod.Capture(cfg, logger)                # 构造不打开设备
d = hdmi_mod.DisplayHdmi(cfg, logger, cap)

STATUS = "/tmp/hdmi_inject_status"
MODES  = "/tmp/hdmi_inject_modes"
open(MODES, "w").write("1920x1080\n1280x1024\n1440x900\n")
open(STATUS, "w").write("connected\n")
d.status_path = STATUS                                # 仅替换数据源
d.modes_path  = MODES

def set_phys(s):
    open(STATUS, "w").write(s + "\n")

def poll_debounced():
    d.poll()                                          # 第 1 次：进入待确认
    return d.poll()                                   # 第 2 次：一致 -> 采纳

rc = d.init()
print(f"init: rc={rc} rsm={cap.rsm} (期望 RUN_DEMO)")

for k in range(3):
    set_phys("disconnected")
    poll_debounced()
    print(f"轮{k+1} 拔: rsm={cap.rsm} (期望 MIPI_PREVIEW_ONLY)")
    set_phys("connected")
    poll_debounced()
    print(f"轮{k+1} 插: rsm={cap.rsm} (期望 RUN_DEMO)")

# 抖动测试：单次采样不应触发迁移
before = cap.rsm
set_phys("disconnected"); d.poll()
print(f"抖动(仅1次采样): rsm={cap.rsm} 未迁移={cap.rsm == before} (期望 True)")
set_phys("connected"); d.poll()

print(f"最终 rsm={cap.rsm}")
