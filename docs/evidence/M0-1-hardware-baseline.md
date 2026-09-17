# 证据：M0-1 硬件基线（演示板 LubanCat-5 / 192.168.0.104）

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：一次探测的原始输出留档，对应 `docs/plans/M0.md` §3 的 **M0-1** 项 |
| 效力 | **非验收依据**（规则 `00-ssot.md` §3.5）；验收只认主基准 §13 复现方式 + §11.2 回归项 + `bench.py` 报告 |
| 对应主基准节 | §2.1（IMX415 核查）、§2.2（NPU 与运行时）、§3.1 / §3.2（采集链）、§10.1（软件栈）、§10.5-1（自检第 1 项）、§11.1-1、§15（O21） |
| 探测时点 | **2026-09-17 17:45–17:50 CST** |
| 探测方式 | **只读**（规则 `30-board-safety.md` §4）；`ssh board`（`~/.ssh/config` 别名 → `cat@192.168.0.104`，ed25519 免密）。**未做任何安装、未改任何配置、未停流** |
| 状态 | `draft`（板上软件栈变更后须复采并回写） |

> 本档只记录实测事实。规范取值一律以主基准与 `docs/build.md` 为准，本档不复制、不裁定。

## 1. 复现命令

```bash
# 可达性（开发机 192.168.0.30 侧，只读）
ping -c 2 -W 2 192.168.0.104
timeout 5 bash -c 'cat </dev/null >/dev/tcp/192.168.0.104/22'
ssh -o BatchMode=yes board 'whoami; hostname; cat /proc/device-tree/model | tr -d "\0"'

# 板端环境
ssh board 'head -2 /etc/os-release; uname -r; python3 -V'

# GPU（Mali-G610）
ssh board 'lsmod | grep -iE "mali|kbase"; ls -l /dev/mali* /dev/dri/'
ssh board 'dmesg | grep -iE "mali|kbase" | tail -6'
ssh board 'for d in /sys/class/devfreq/*gpu*; do echo "$(basename $d): $(cat $d/cur_freq) Hz"; done'
ssh board 'ls -l /usr/lib/aarch64-linux-gnu/libmali*'

# NPU 与运行时
ssh board 'strings /usr/lib/librknnrt.so | grep "librknnrt version"'
ssh board 'dmesg | grep -i rknpu | head -8'

# 采集节点（关键：是否有 V4L2 采集设备）
ssh board 'ls /sys/class/video4linux/; ls -1 /dev/video* /dev/v4l-subdev* /dev/media* 2>/dev/null'
ssh board 'dmesg | grep -iE "imx415|rkisp|rkcif" | tail -8'

# 存储与内存（服务 O21）
ssh board 'lsblk -d -o NAME,SIZE,MODEL; df -h / | tail -1; free -h | head -2'
ssh board 'echo "name=$(cat /sys/block/mmcblk0/device/name)"; echo "manfid=$(cat /sys/block/mmcblk0/device/manfid)"'
```

## 2. 实测输出与解读

### 2.1 可达性与板卡身份

| 项 | 实测 |
|---|---|
| 开发机 → 板 | **可达**：ping 2/2 包、0% 丢包、rtt **0.753 ms**；**TCP 22 开放** |
| SSH | `~/.ssh/config` 有 `Host board`（HostName 192.168.0.104 / User **cat** / IdentityFile `~/.ssh/id_ed25519`），**免密登录成功** |
| 登录用户 | `cat`；`hostname` = **lubancat** |
| 板型（设备树） | **`Embedfire LubanCat-5 V2`** → 即 **RK3588**（非 RK3588S），满足主基准 §10.4 的 `board.model` 候选 |
| 内核 | **`5.10.160`** → 符合主基准 §10.1「内核 5.10 系」 |
| 发行版 | **Debian GNU/Linux 11 (bullseye)** → 对应厂家"以 Debian11 为主"的镜像 |
| python3 | **3.9.2** |

**解读（影响后续）**：板上 python = **3.9** ⇒ ① `rknn_toolkit_lite2` 须用 **cp39** wheel；② pybind11 扩展的 ABI 必须是 **`cpython-39-aarch64`**（`docs/build.md` §4）；③ 主基准 §10.1 要求的"镜像与板上 python 由 M0-1 定案"**已定案 = Debian 11 / python 3.9**。

### 2.2 GPU（Mali-G610）—— 本次核实目标

| 项 | 实测 |
|---|---|
| 内核模块 | `lsmod` 未见独立 `mali` 行（驱动以 built-in 形式加载，见 dmesg） |
| 驱动加载 | `[4.218247] mali fb000000.gpu: Probed as mali0`；`Mali device driver loaded` |
| 固件 | `[14.499216] mali fb000000.gpu: Loading Mali firmware 0x1010000`；`git_sha: ee476db42870778306fa8d559a605a73f13e455c` |
| 构建信息 | `rk_ko_ver is '5'`, `built at '17:12:55', on 'Apr 20 2026'`；源码路径显示为 `drivers/gpu/arm/mali400/`（**Rockchip 沿用的目录名**，非实际 GPU 代次） |
| 设备节点 | `/dev/mali0`（`crw-rw---- root video`）；`/dev/dri/`：`card0`、`card1`、**`renderD128`**、`renderD129` |
| 调频 | devfreq **`fb000000.gpu` 当前 300 000 000 Hz**（另有 `dmc`、`fdab0000.npu`） |
| 用户态库 | `libmali.so.1.9.0`（2020-07-29）；符号链 `libmali-valhall-g610-g13p0-x11-gbm.so` → **Mali-G610（Valhall）+ GBM 支持** |
| 相关包 | `libdrm-* 2.4.104-1`（含 `libdrm-dev`）等 |

**结论：GPU 全栈可用**（内核驱动 + 固件 + 用户态 + GBM）。

**对本项目的意义**：本项目主路径**不依赖 GPU**——曲线应用在 CPU（主基准 §6.1）、缩放/合成用 RGA（§10.1）、推理用 NPU（§7）。GBM 可用意味着"若未来需要 EGL/GBM 合成"具备条件，但**不据此改变 §10.1 的显示栈选型**（仍为 DRM 双 CRTC + RGA）。

### 2.3 NPU 与运行时（**关键冲突项**）

| 项 | 实测 |
|---|---|
| `librknnrt` | **`librknnrt version: 1.5.0 (e6fe0c678@2023-05-25T08:09:20)`** |
| `rknpu` 内核驱动 | **`[drm] Initialized rknpu 0.9.8 20240828 for fdab0000.npu on minor 1`** |
| 驱动其他日志 | `rknpu iommu is enabled, using iommu mode`；**3 条** `can't request region for resource [mem 0xfdab0000 / 0xfdac0000 / 0xfdad0000]` 警告（其后初始化成功，属非致命）；`failed to find power_model node` 等 3 条（非致命） |
| debugfs | `/sys/kernel/debug/rknpu/driver_version` **读不到**（需 root；`cat` 在 sudo 组但 `sudo -n` 需密码）→ 版本改由 dmesg 取得 |

**解读（阻塞 M0-2 的关键）**：
- 驱动 **0.9.8 ≥ 0.9.2**，满足官方 V2.3.0 下限 ✓
- 但 `librknnrt` = **1.5.0**，而主基准 §2.2 的纪律是「toolkit2 与 librknnrt **必须同版本号**」⇒ **PC 端 toolkit2 用 2.3.2 转出的模型在此板上会报 `Invalid RKNN model version`**
- ⚠️ 注意区分：主基准 §2.2 引用的「野火实测基线 librknnrt 2.3.2 + driver 0.9.8」是**野火教程所在板**的值；**本项目这块板是镜像预装的 1.5.0**，两者不可混用
- 处置（已由项目方于同日定为**先 A/B 实测再定版**）：见 §4 待办

### 2.4 采集节点（**未满足项**）

| 项 | 实测 |
|---|---|
| `/sys/class/video4linux/` | **空** |
| `/dev/video*` | 仅 `video-dec0`、`video-enc0`（**编解码器 MPP 节点，非 V4L2 采集**） |
| `/dev/v4l-subdev*` | **不存在** |
| `/dev/media*` | **不存在** |
| dmesg（imx415 / rkisp / rkcif） | **无任何相关日志** |

**结论：摄像头链路完全未启用** —— IMX415 未接入或设备树/overlay 未使能（对应风险 **R12**）。
**影响**：主基准 §11.1-1 的「`/dev/videoX` + rkisp 节点确认」项**未满足**；M1（相机链路）前置未就绪。**M0 其余项不受此阻塞**（M0-3/8 用代用数据，见 §4.3 裁定 O7）。

### 2.5 存储（服务 O21）

| 项 | 实测 |
|---|---|
| 设备 | `mmcblk0` **58.7 G**（另有 `mmcblk0boot0/boot1` 各 4 M）→ **eMMC** |
| 介质标识 | `name = 11S000`；`manfid = 0x00002f`；`cid = 2f011131315330303010682b4b758c00`（**MODEL 字段为空**，厂商型号未能从 sysfs 取得） |
| 根分区 | `/dev/mmcblk0p3` **58 G，已用 4.3 G，可用 51 G**（8%） |

**用途**：这是 `docs/measurements.md` §2（O21：存储介质与容量上限）的**实测依据**。

### 2.6 内存与其他

| 项 | 实测 |
|---|---|
| 内存 | total **7.7 GiB**，available 7.1 GiB（满足主基准 §6.3「演示板 ≥8 GB」） |
| 权限 | `cat` 用户 id 1000；组含 **`sudo`**、`adm`、`video`、`dialout` 等；**`sudo -n` 失败**（需密码）→ 有副作用操作须用户授权 |
| 启动 | `/boot/` 下为 `extlinux/` + `Image-5.10.160` + `dtb`（**extboot 方式**，与野火 SDK 文档一致）；`/boot/uEnv.txt` 未见内容 |
| 图形会话 | 未见运行的桌面/图形栈（未探测 `systemctl` 目标，属待补项） |

## 3. 对 M0 出口判定的影响

| M0-1 出口判定项（§11.1-1） | 状态 |
|---|---|
| 板型（Cat-4/5） | ✅ **LubanCat-5 V2 / RK3588** |
| 丝印 CAM 口 lane 数 |  未核（**待摄像头接入后与 overlay 一并核**，受 §2.4 阻塞） |
| overlay / `/dev/videoX` / rkisp 节点 | ❌ **未满足**（§2.4：无采集节点） |
| 屏插件 / 横竖屏确认 | ⬜ 未核（本次未探测 DRM connector 模式，待补 `probe_drm`） |
| 软件版本三元组 | ✅ 部分：`librknnrt` **1.5.0**、`rknpu` 驱动 **0.9.8**、`rknn-toolkit2` **待 A/B 定版** |

## 4. 待办（阻塞关系）

| # | 待办 | 阻塞 |
|---|---|---|
| 1 | **librknnrt 1.5.0 vs 2.3.2 的 A/B 实测**（项目方已定路线）：ZDE++ 单模型各转一次 + 板端加载/对齐/耗时 | **阻塞 M0-2**（模型转换与测速） |
| 2 | **摄像头链路排查**（IMX415 接入 / overlay / 设备树插件） | 阻塞 M1；不阻塞 M0 |
| 3 | 补探 DRM connector 与屏参数（`probe_drm`） | 服务 §11.1-1 与 §11.1-9（M0-9 双屏打通） |
| 4 | 有副作用操作（换 librknnrt、装 `nvidia-utils`、apt 组件）**均需用户授权** | — |

## 5. 回写义务（本次探测触发）

- `environment/` 板卡档案：本档为新证据源
- 主基准 §10.4：`board.model` / `board.cam_lanes`（后者待补）等 M0 键
- 主基准 §11.1-1 出口判定；§2.2 补注"本项目板实测 = librknnrt 1.5.0"
- `docs/measurements.md` §2（O21）三列回填：介质 eMMC / 可用 51 G / 依据本档
## 6. 补测（v3.23，2026-09-17 21:0x–22:06）：摄像头链路 bring-up（**只读探测 + 一次已授权的配置变更/重启**）

> 本节把 §2.4「采集节点未满足」这一项的**补测过程与原始输出**补齐。**当次结论 = 已打通**；完整档案与复现命令见 `environment/board-lubancat5.md` §7（本档只留原始行与判定）。

### 6.1 变更前后的原始输出对照

| 阶段 | 原始行（`sudo dmesg`） | 判定 |
|---|---|---|
| 变更前 | （无任何 `imx415` / `rkisp` / `rkcif` 行）；`/sys/class/video4linux/` 为空 | overlay 未启用（设备树 `dcphy0-imx415@1a` / `csi2-dcphy0` / `mipi0-csi2` 全 `disabled`） |
| 启用 overlay + 重启后（首轮） | `imx415 1-001a: driver version: 00.01.08` / `Get data lanes failed! 4 lanes default` / `detect imx415 lane 4` / `Failed to get power-gpios` / `no pinctrl` / **`Unexpected sensor id(000000), ret(-5)`** | 驱动已绑定，**传感器无 I2C 应答** |
| **重插排线 + 重启后** | **`imx415 1-001a: Detected imx415 id 0000e0`** / `rockchip-csi2-dphy csi2-dcphy0: dphy0 matches m00_b_imx415 1-001a:bus type 5` / `set fmt: cur_mode: 1944x1096, hdr: 0, bpp: 10` / **`capture_mode is 1920x1080`** | ✅ **打通** |

### 6.2 判为"物理侧"的依据（首轮失败时的排除项）

| 检查 | 原始值 |
|---|---|
| 同总线其它器件 | `i2cdetect -y -r 1`：`20: -- -- UU ...`（0x22 `fusb302`）、`40: -- -- UU ...`（0x42 `rk8602`）、0x5d `gt911` **均正常应答** ⇒ 总线/上拉无问题 |
| 供电轨 | `regulator_summary`：`cam_dvdd 1200 mV` / `cam_dovdd 1800 mV` / `cam_avdd 2800 mV` 均 enabled；`vdd_cam_5v 3300 mV`（4 users） |
| GPIO（DT ↔ 原理图） | `pwdn-gpios = <gpio4 8 0>`（GPIO4_B0，高有效）、`reset-gpios = <gpio4 9 1>`（GPIO4_B1，低有效） 原理图 J8：AK26 = `CAM0_PWDN`、AL24 = `CAM0_nRST`，**逐条一致** |
| 屏 | `card0-DSI-1` = connected + enabled（DSI 绑 `inno-dsidphy`，未与 cam0 的 `dcphy0` 抢资源） |

### 6.3 出图验证（重插后）

| 项 | 原始值 |
|---|---|
| ISP 主路径 | `rkisp_mainpath` = **`/dev/video11`**（Multiplanar；`rkisp-isp-subdev` pad0 sink `SGBRG10_1X10/1920x1080` ← `rkcif-mipi-lvds:0 [ENABLED]`，pad2 source → `rkisp_mainpath [ENABLED]`） |
| 可选格式 | UYVY / NV16 / NV61 / NV21 / **NV12** / NM21 / NM12，Stepwise 32×32–1920×1080 step 8 |
| 3A / IQ | `rkaiq_3A_server`（PID 387）常驻；`/etc/iqfiles/imx415_CMK-OT2022-PX1_IR0147-50IRC-8M-F20.json`（与 DT module-name 匹配） |
| 取流命令 | `sudo v4l2-ctl -d /dev/video11 --set-fmt-video=width=1920,height=1080,pixelformat=NV12 --stream-mmap=4 --stream-count=90 --stream-to=/tmp/cam_long.raw` |
| 帧完整性 | 30 帧 = 93,312,000 B = 30 × 1920×1080×1.5（**逐帧对齐**）；90 帧同理 |
| 帧率 | 90 帧 / 1.778 s ≈ **50.6 fps** ⇒ 当前 overlay 为 **1080p@60** 变体，**非 §3.1/§3.2 的 30 fps**（30 fps 锁定按 §3.2 三步实验在 M1 落定） |
| 图像观感 | f0 近全黑（gray mean 0.45）→ 稳态 f60/f89 gray mean ≈37–39、std ≈12.8、max ≈101；B/G/R ≈16–22 / 15–17 / 85–88（**强红偏**）且离焦 ⇒ 经用户确认为**场景本身暗/红**，**不作画质结论** |
| 证据帧 | `docs/evidence/reports/M0-1/cam0_1080p_frame0.png`（AE 收敛前）/ `cam0_1080p_f60.png` / `cam0_1080p_f89.png` |

### 6.4 变更记录（有副作用，已获授权）

- `/boot/uEnv/uEnvLubanCat5-V2.txt` 第 63 行取消注释（cam0 的 IMX415 1080p@60 overlay）；备份 `…bak.20260917_cam0`，sha256 `dd5facae…` → 改后 `d00c7e76…`；`sudo reboot` 生效。
- **恢复步骤**：`sudo cp /boot/uEnv/uEnvLubanCat5-V2.txt.bak.20260917_cam0 /boot/uEnv/uEnvLubanCat5-V2.txt && sudo reboot`。

### 6.5 对 §3 判定的影响

§2.4 的"未满足"项 → **已满足**：`camera.video_node` / `camera.rkisp_node` 的实测值可取（ISP 主路径 `/dev/video11`、media 设备 `/dev/media1` = `rkisp0-vir0`、`/dev/media0` = `rkcif-mipi-lvds`）；CAM0 = 4 lane 已核。**遗留（不阻塞 M0）**：30 fps 锁定（M1）、HDMI 侧尚未接显示器（M0-9 需接）。
