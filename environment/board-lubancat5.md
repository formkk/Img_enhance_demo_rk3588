# 设备档案：演示板 LubanCat-5（192.168.0.104）

| 项 | 内容 |
|---|---|
| 地位 | 本仓库的**环境记录**（规则 `00-ssot.md` §2 的 C 层记录类），承载**演示板**的环境快照 |
| 效力 | **非验收依据**：不得作为达标依据，也不得替代 `bench.py` 报告（规则 `10-evidence.md` §1） |
| 对应主基准节 | §2.1 / §2.2（硬件与 NPU 运行时）、§3.1 / §3.2（采集链）、§7（NPU 绑核）、§10.1（软件栈）、§10.4（config 键）、§10.5-1、§11.1-1、§15（O21） |
| 状态 | `draft`（板端软件栈变更后须复采并回写本档） |

> 本档只记录**板上实测事实与缺失项**，不复制规范取值、不裁定开放项（取值一律以主基准 §10.4 与 `docs/build.md` 为准）。
> **原始探测输出与完整复现命令见 `docs/evidence/M0-1-hardware-baseline.md`**（探测时点 2026-09-17 17:45–17:50 CST，只读）。

## 1. 身份与访问

| 项 | 实测 |
|---|---|
| 板卡 | **Embedfire LubanCat-5 V2**（设备树 `model`）→ RK3588 |
| 地址 | `192.168.0.104/24`（与开发机 `192.168.0.30` 同网段，网关 `192.168.0.1`） |
| 可达性 | ✅ **可达**：ping 0.753 ms 均值、0% 丢包、TCP 22 开放 |
| SSH | `~/.ssh/config` 别名 `board`（`HostName 192.168.0.104` / `User cat` / `IdentityFile ~/.ssh/id_ed25519`），**ed25519 免密已配** |
| 登录用户 | **`cat`**（uid 1000；组含 `sudo` / `adm` / `video` / `dialout`）；（**v3.22 更正**）`sudo -n -l` 实测为 **`(ALL) NOPASSWD: ALL`**——**免密 sudo 可用**，故有副作用操作**技术上可执行**，但**仍须按规则 `30-board-safety.md` §4 先报备影响面与恢复步骤并取得授权** |
| 主机名 | `lubancat` |

## 2. 硬件

| 项 | 实测 |
|---|---|
| SoC | RK3588（LubanCat-5 V2），内核 **5.10.160** |
| GPU | **Mali-G610**（Valhall）：驱动已加载（`mali fb000000.gpu: Probed as mali0`）+ 固件已载（`0x1010000`）+ 用户态 `libmali.so.1.9.0`（**GBM 版**）；节点 `/dev/mali0`、`/dev/dri/renderD128|129`、`card0|1`；devfreq 当前 **300 MHz** |
| NPU | `fdab0000.npu`；devfreq 节点存在；驱动 **rknpu 0.9.8**（2024-08-28 构建） |
| 内存 | total **7.7 GiB**（available 7.1 GiB） |
| eMMC | `mmcblk0` **58.7 G**（`name=11S000`、`manfid=0x2f`；厂商标识字段未得）；根分区 `/dev/mmcblk0p3` 58 G，**可用 51 G** |
| 摄像头 | ✅ **（v3.23）CAM0 + 野火 IMX415 已打通**：`imx415 1-001a` 检测到 `id 0000e0`、`csi2-dcphy0` link 完成、`rkisp_mainpath` = `/dev/video11` 出 NV12 1920×1080，帧逐帧对齐；首轮失败系排线接触/方位问题（详见 **§7**）。⚠️ 当前 overlay 为 **1080p@60** 变体，实测 ≈50.6 fps，**非 §3.1/§3.2 的 30 fps** |

## 3. 软件栈（**M0-1 定案项**）

| 层 | 实测 | 影响 |
|---|---|---|
| 发行版 | **Debian GNU/Linux 11 (bullseye)** | 对应厂家"以 Debian11 为主"；`/boot/extlinux`（extboot 方式） |
| 板端 python3 | **3.9.2** | ⇒ `rknn_toolkit_lite2` 用 **cp39** wheel；**pybind11 扩展 ABI = `cpython-39-aarch64`**（`docs/build.md` §4） |
| NPU 运行时 | **（v3.22 已升级）`librknnrt 2.3.2`**（`429f97ae6b@2025-04-09`，sha256 `d31fc19c…38e8`）；原 **1.5.0** 备份于 **`/usr/lib/librknnrt.so.bak.1.5.0_20260917`**（sha256 `c525301f…b84c`） | 升级记录与恢复步骤见 §5 的 B1 行尾部与 §5.1；**实测结论**：① 2.3.2 模型需 2.3.2 运行时（1.5.0 报 `Invalid RKNN model version 6`）；② **反向兼容成立**——2.3.2 运行时**能加载** 1.5.0 编译的模型（实测对齐 5.42e-4） |
| NPU 驱动 | **rknpu 0.9.8** | ✅ 满足官方 V2.3.0 的「≥0.9.2」下限 |
| 图形/桌面 | 未见运行中的图形会话（未探测 systemd 目标，属待补项） | 关桌面两态（§2.3）待演练 |

## 4. 与 M0-1 出口判定对照（§11.1-1）

| 判定项 | 状态 |
|---|---|
| 板型（Cat-4/5） | ✅ **LubanCat-5 V2** |
| 内核 / 发行版 | ✅ 5.10.160 / Debian 11 |
| 软件版本三元组 | ✅ **已定版**（v3.22）：librknnrt **2.3.2** / toolkit2 **2.3.2**（同版本号）/ driver **0.9.8**（≥0.9.2 下限）；确认门 = M0-4 的 HQ 编译结果 |
| overlay / `/dev/videoX` / rkisp 节点 | ✅ **已满足（v3.23）**：`uEnvLubanCat5-V2.txt` 第 63 行启用 cam0 的 imx415 overlay；`/dev/video0…19+`、`/dev/v4l-subdev*`、`/dev/media0|1` 齐备；ISP 主路径 = **`/dev/video11`**（rkisp_mainpath，NV12 1920×1080）；`camera.video_node` / `camera.rkisp_node` 取值以此为准（详见 §7） |
| 丝印 CAM 口 lane 数 | ✅ **CAM0 = 4 lane**（驱动 `detect imx415 lane 4`；原理图 J8 走 D0~D3+CLK）——与 R12"Cat-5 仅 CAM0/CAM1 为 4-lane"一致 |
| 屏插件 / 横竖屏 / connector | ⬜ 未核（待补 `probe_drm`，服务 M0-9） |

## 5. 待办与阻塞

| # | 事项 | 阻塞 / 约束 |
|---|---|---|
| ~~B1~~ | ~~**librknnrt 1.5.0 vs 2.3.2 的 A/B 实测**~~ → **已完成定版**（2026-09-17 19:0x）：三臂（1.5.0 / 2.3.2 / 2.4.2a12）均可转、可加载、可推理，耗时差异落在噪声内；**定版 = 板上 `librknnrt 2.3.2` + PC `rknn-toolkit2 2.3.2`**（同版本号），确认门 = M0-4 的 HQ 编译结果。证据见 `docs/evidence/M0-2-ab-toolkit-version.md` | **剩余动作：板上 `librknnrt` 升级（1.5.0 → 2.3.2）尚未执行**——有副作用，须先备份 `/usr/lib/librknnrt.so`、记录恢复步骤并取得授权（规则 `30-board-safety.md` §4）；A/B 全程以 `unshare -Ur -m` 挂载命名空间隔离，系统文件未改动 |
| B1-b | **板端 lite2 的正式安装**（`rknn_toolkit_lite2` **cp39**，随定版 2.3.2） | 本轮 A/B 用 `pip3 install --target ~/rknn_ab/site/*` 临时隔离；正式部署时再决定装用户级还是系统级（受 B4 授权约束） |
| B1-c | 临时区清理 | 板 `~/rknn_ab/`（约 100 MB）与 `~/rknn_ab/lib/*/librknnrt.so` 可随时 `rm -rf` 完全回退 |
| B2 | 摄像头链路排查（IMX415 接入 / overlay / 设备树插件） | ✅ **已解除（v3.23，2026-09-17 22:05）**：根因 = cam0 overlay 未启用（已启用）+ **排线接触/方位**（重插后传感器 ID 读取成功）；链路与 ISP 出帧已验证（§7）。**遗留（不阻塞 M0）**：① 当前 overlay 为 1080p@60，实测 ≈50.6 fps，**30 fps 锁定**按 §3.2 三步实验在 M1 落定；② 当次画面极暗+强红偏+离焦，画质结论待正常光照场景复采 |
| B3 | 补探 DRM connector 与屏参数（`board-ops` 的 `probe_drm`） | 服务 §11.1-1 / §11.1-9 |
| B4 | 有副作用操作（换板上 `librknnrt`、装 apt 组件、停流/复位）**均需用户授权**并先记录恢复步骤（规则 `30-board-safety.md` §4） | — |

## 6. 待配置（板端）

| # | 事项 | 前置 |
|---|---|---|
| B5 | `rknn_toolkit_lite2`（cp39 wheel）装到板端 python | 版本须与板上 `librknnrt` 一致（= **2.3.2**）；本轮 A/B 已以 `pip3 install --target ~/rknn_ab/site/*` 临时隔离验证（**已满足**，正式安装方式待定，受 B4 授权约束） |
| B6 | 日志目录与存图目录（`logging.dir` / `storage.dir`，§8.3、`docs/logging.md` §2） | 部署阶段（M3 前后） |
| B7 | systemd 单元 `starlight-demo.service`（§10.6-D 手工清单） | M3/M4 |

## 5.1 板上 `librknnrt` 升级记录（v3.22，**已授权执行**）

| 步骤 | 实测 |
|---|---|
| 授权 | 用户于 2026-09-17 明确授权（本轮） |
| 备份 | `sudo cp -a /usr/lib/librknnrt.so /usr/lib/librknnrt.so.bak.1.5.0_20260917`；备份 sha256 = `c525301fd546d50bea2cd3cbb26773d8aec93cdfb80ce71f7e0a9e09a4c8b84c` |
| 安装 | `sudo install -m 644 -o root -g root <2.3.2 源> /usr/lib/librknnrt.so`；新库 sha256 = `d31fc19c85b85f6091b2bd0f6af9d962d5264a4e410bfb536402ec92bac738e8`（**与 A/B 的 B 臂所用 `.so` 同一文件**） |
| 版本核验 | `strings /usr/lib/librknnrt.so \| grep -m1 "librknnrt version"` → `2.3.2 (429f97ae6b@2025-04-09T09:09:27)`；驱动仍 `0.9.8` |
| 功能核验（**系统路径，不再用命名空间**） | FP16 480×272：加载 OK、对齐 **4.91e-4**、中位 74.0 ms；INT8 480×272：加载 OK、对齐 1.72e-2；FP16 256×144：加载 OK、对齐 4.60e-4、中位 27.0 ms；**1.5.0 编译的模型亦可加载**（对齐 5.42e-4） |
| **恢复步骤** | `sudo install -m 644 -o root -g root /usr/lib/librknnrt.so.bak.1.5.0_20260917 /usr/lib/librknnrt.so`，随后 `strings ... | grep -m1 librknnrt.version` 应回到 `1.5.0`；**无需重启**（下一次进程加载即生效） |
| 遗留 | 备份文件保留在 `/usr/lib/`（不删）；`~/rknn_ab/` 内的隔离用 `.so` 已无必要，可随临时区一并 `rm -rf` |

## 7. 摄像头链路（CAM0 / IMX415）

**结论（2026-09-17 22:05，重插排线 + 重启后复测）**：✅ **链路已打通** —— 传感器 ID 读取成功、media 链路 link 完成、ISP mainpath 出帧（NV12 1920×1080）；先前"无 I2C 应答"系**排线接触/方位问题**，重插后自愈。

### 7.0 bring-up 两阶段对照

| 阶段 | dmesg 关键行 | 判定 |
|---|---|---|
| 首轮（21:53，仅启用 overlay + 重启） | `imx415 1-001a: Unexpected sensor id(000000), ret(-5)`；`i2cdetect` 无 `0x1a` | ❌ 传感器无应答（DT/供电/GPIO 均已核到位 ⇒ 疑物理侧） |
| 复测（22:05，**重插排线 + 重启**） | `imx415 1-001a: Detected imx415 id 0000e0`；`rockchip-csi2-dphy csi2-dcphy0: dphy0 matches m00_b_imx415 1-001a:bus type 5`；`capture_mode is 1920x1080` | ✅ 传感器在线、链路 link、1080p 模式 |

### 7.1 配置变更（有副作用，本次已获用户授权执行）

| 项 | 内容 |
|---|---|
| 改动 | `/boot/uEnv/uEnvLubanCat5-V2.txt` **第 63 行**由注释改为 `dtoverlay=/dtb/overlay/rk3588-lubancat-5-cam0-imx415-1920x1080-60fps-overlay.dtbo`（diff 仅此一行） |
| 备份 | `/boot/uEnv/uEnvLubanCat5-V2.txt.bak.20260917_cam0`，sha256 `dd5facae17149a49dada35e7dbfa118d2a6c60723e163180d446f52e092590aa`；改后 sha256 `d00c7e7642789ca12b89f174e02f7915acacced9b159f9004276d50d121fb88d` |
| 生效方式 | `sudo reboot`（21:53，SSH 约 18 s 后恢复） |
| 恢复步骤 | `sudo cp /boot/uEnv/uEnvLubanCat5-V2.txt.bak.20260917_cam0 /boot/uEnv/uEnvLubanCat5-V2.txt && sudo reboot` |
| 同时生效的其它 overlay（未改动） | 第 39 行 `rk3588-lubancat-5-hdmi0-vp0-overlay.dtbo`；第 44 行 `rk3588-lubancat-5-v2-dsi0-vp2-1080x1920-5.5inch-ebf410125-overlay.dtbo`（5.5" 屏 vp2/dsi0）；`enable_uboot_overlays=1` |
| 仍为注释 | cam1~cam5 的 imx415 overlay（未启用） |

### 7.2 启用后的实测（只读）

| 项 | 实测 |
|---|---|
| DT 节点 | `i2c@fea90000/dcphy0-imx415@1a`（`sony,imx415`）**status=okay**；`csi2-dcphy0`、`mipi0-csi2` = okay（重启前均为 disabled） |
| 节点文件 | `/dev/media0`、`/dev/media1`、`/dev/video0…19+`、`/dev/v4l-subdev0…2`；`v4l2-ctl --list-devices` 列出 `rkcif-mipi-lvds` / `rkisp_mainpath` / `rkisp-statistics` |
| 驱动探测 | `imx415 1-001a: driver version 00.01.08`、`Get data lanes failed! 4 lanes default`、`detect imx415 lane 4`、`Failed to get power-gpios`、`no pinctrl`（该 BSP 的该节点本就不带这两项，非致命） |
| **失败点** | `imx415 1-001a: Unexpected sensor id(000000), ret(-5)`；`rkcif: get remote terminal sensor failed!`（-19）⇒ 传感器无应答 |
| I2C（`sudo i2cdetect -y -r 1`） | 0x22 `fusb302`、0x42 `rk8602`、0x5d `gt911`（触摸）**正常应答** ⇒ 总线与上拉无问题；**无 0x1a** |
| 供电轨（`regulator_summary`） | `cam_dvdd 1200 mV`、`cam_dovdd 1800 mV`、`cam_avdd 2800 mV` 均 enabled；`vdd_cam_5v` 3300 mV（4 users） |
| GPIO（DT 解码 ↔ 原理图） | `pwdn-gpios=<gpio4 8 0>`（= GPIO4_B0，高有效）、`reset-gpios=<gpio4 9 1>`（= GPIO4_B1，低有效）↔ 原理图 J8：AK26=`CAM0_PWDN`、AL24=`CAM0_nRST`，**逐条一致** |
| 屏未受影响 | `card0-DSI-1` = connected + enabled（加摄像头未抢 DSI 的 `inno-dsidphy`；组合 PHY `samsung-mipi-dcphy` 与屏无关） |

### 7.3 CAM 口 ↔ DT/I2C 映射（读 vendor overlay 取得，供丝印核对）

| 丝印口 | I2C（DT 标签 / Linux 节点） | PHY 路径 | CSI |
|---|---|---|---|
| CAM0 | i2c1 / `/dev/i2c-1`（`fea90000`） | `csi2-dcphy0` | `mipi0-csi2` |
| CAM1 | i2c5 / `/dev/i2c-5`（`fead0000`） | `csi2-dcphy1` | `mipi1-csi2` |
| CAM2 | i2c3（`feab0000`，**当前 DT disabled**） | `csi2-dphy1`（+ `dphy0_hw`） | `mipi2-csi2` |
| CAM3 | i2c4（`feac0000`，**当前 DT disabled**） | `csi2-dphy2`（+ `dphy0_hw`） | `mipi3-csi2` |
| CAM4 | i2c6 / `/dev/i2c-6`（`fec80000`） | `csi2-dphy4`（+ `dphy1_hw`） | `mipi4-csi2` |
| CAM5 | i2c7 / `/dev/i2c-7`（`fec90000`） | `csi2-dphy5`（+ `dphy1_hw`） | `mipi5-csi2` |

现场已探 i2c-1 / 5 / 6 / 7：**均无 0x1a**；i2c3 / i2c4 因控制器 disabled 不可探（cam2/cam3 若插错口需先启用对应 overlay）。

### 7.4 硬件资料索引（本次核图用）

- 板卡规格书 `…/鲁班猫5/[野火]鲁班猫5开发板硬件规格书_V1.1.4.pdf` **§4.5.15 摄像头（第 33 页）**：6×24Pin FPC，丝印 CAM0~CAM5；**"需搭配排线使用"**，并附**正面(左)/背面(右)连接示意图**
- 原理图 `…/EBF410149V2R2_20250908/EBF410149V2R2_SCH_20250908.pdf`：`MIPI CSI 0 PORT`（J8）/`MIPI CSI 1 PORT`（J9）；J8 pin5/6 = `I2C1_SDA_M2/SCL_M2`（= i2c1 ✓ 与 cam0 overlay 一致）、pin7~21 = 4 lane MIPI
- 模组资料 `refs/野火鲁班猫/野火【IMX415摄像头】模块_20260617/`（EBF410358V1R1 原理图/PCB/尺寸 + IMX415 datasheet）

### 7.5 出图验证与取流参数（22:05–22:06，只读 + 取流）

| 项 | 实测 |
|---|---|
| ISP 节点 | `rkisp_mainpath` = **`/dev/video11`**（Multiplanar；`rkisp-isp-subdev` pad0 sink = `SGBRG10_1X10/1920x1080` ← `rkcif-mipi-lvds:0 [ENABLED]`；pad2 source = `YUYV8_2X8/1920x1080` → mainpath `[ENABLED]`） |
| 可选格式 | UYVY / NV16 / NV61 / NV21 / **NV12** / NM21 / NM12，Stepwise 32×32–1920×1080 step 8（⇒ §3.1 的 1920×1080 NV12 可用） |
| 3A | `rkaiq_3A_server`（PID 387）常驻；IQ 文件 `/etc/iqfiles/imx415_CMK-OT2022-PX1_IR0147-50IRC-8M-F20.json` **与 DT 的 module-name 匹配** ✅ |
| 取流命令 | `sudo v4l2-ctl -d /dev/video11 --set-fmt-video=width=1920,height=1080,pixelformat=NV12 --stream-mmap=4 --stream-count=90 --stream-to=/tmp/cam_long.raw` |
| 帧完整性 | 30 帧文件 93,312,000 B = 30 × (1920×1080×1.5) **逐帧对齐**；90 帧同理 |
| 实际帧率 | 90 帧 **1.778 s ⇒ ≈ 50.6 fps**（**注意**：当前 overlay 是 `…-imx415-1920x1080-**60fps**-overlay`，**不是 §3.1/§3.2 要的 30 fps**，30 fps 锁定留 M1 三步实验） |
| 传感器 ctrl（3A 自动） | `exposure = 1350`（max 2242）、`analogue_gain = 161`（max 240）⇒ AE 已把增益推到 ~67%，与"现场极暗"一致 |
| 图像观感（当次场景） | f0 近全黑（gray mean 0.45）→ 流稳后 f60/f89 gray mean ≈ 37–39、std ≈ 12.8、max ≈ 101，**B/G/R 均值 ≈ 16–22 / 15–17 / 85–88（强红偏）**、画面**离焦**；属场景/光照待确认项，**不作画质结论** |
| 证据图 | `docs/evidence/reports/M0-1/cam0_1080p_frame0.png`（AE 收敛前）、`cam0_1080p_f60.png`、`cam0_1080p_f89.png`；原始 NV12 未入库（大文件） |

### 7.6 复测命令（只读，供后续复现）

```
ssh board 'sudo dmesg | grep -iE "imx415|rkcif.*sensor failed"; \
  sudo i2cdetect -y -r 1 | tail -8; \
  v4l2-ctl --list-devices; \
  sudo v4l2-ctl -d /dev/video11 --list-formats-ext'
```
判据（已全部满足）：关闭 `Unexpected sensor id(000000)`、`i2cdetect` 出现 `1a`、`Detected imx415 id` 出现、`--list-formats-ext` 含 NV12 1920×1080。

## 8. 摄像头光学与成像现状（v3.24，2026-09-17 22:2x–23:0x）

**结论**：链路（sensor→CSI→ISP→mainpath）完全可用、帧实时前进；**光学侧未定型** —— 镜头换装后仍**未达合焦**，且**安装朝向倾斜约 30–40°**。⇒ **画质类结论一律挂起**；**"镜头定型 + 摆正 + 对焦达标"为 M1 前置闸口（主基准 §15 O31）**，未关闭前不得采 M1 实拍集。

### 8.1 镜头换装

| 项 | 实测 |
|---|---|
| 原镜头 | **特莱斯 TRC-20805A6-01**：焦距 **2.02 ± 5% mm**、F/2.5、FOV **D 119.8° / H 105.2° / V 87.2°**、总长 20.35 ± 0.2 mm、机械后焦 3.33 ± 0.2 mm（资料：`refs/野火鲁班猫/野火【IMX415摄像头】模块_20260617/1-硬件资料_参考资料_模块手册/镜头参数_尺寸/`） |
| 现镜头 | 用户现场换为 **8 mm 可调焦镜头**（型号未记录，属 O31 待登记项；视场显著变窄，`1080×608` 窗口取景更"长焦"） |

### 8.2 对焦实测（原生 NV12 帧，非 JPEG）

| 指标 | 换镜头前（120°） | 8 mm 调焦后 | 备注 |
|---|---|---|---|
| 中心 1:1 480×270 的 Laplacian 方差 | 3.0 | **9.6** | 合焦同类场景通常 **>150** |
| 全幅 Laplacian 方差 | 5.0 | 16.8 | — |
| gray std | 28.8 | 66.4 | 细节量增加（场景已可辨识） |
| B/G/R 均值 | 121/112/121 | 120/107/111 | **中性，无红偏** ✅ |

场景内容（换场景后）：书桌场景（黑色显示器/电视、纸箱、线缆、红色物体）——**可辨识但 1:1 边缘糊开约 3–5 px**。
AE 正常自适应（`analogue_gain` 161 → 146、`exposure` 1350/2242）；`rkaiq_3A_server` 常驻。

### 8.3 安装朝向

实测画面**倾斜约 30–40°**（显示器/桌面均呈斜置）⇒ 模组机械朝向与传感器扫描方向不一致。**M1 采图前须摆正**（§3.1 的 `1080×608` 为横向取景）；结论登记列入 §11.1-1。

### 8.4 预览工具（`tools/cam_preview.py`，已入册主基准 §10.3）

- 用途：M0/M1 bring-up 的**调焦/ISP 观感**核对；**非交付链路**（交付显示走 DRM 双 CRTC，§3.3/§3.4）。
- 用法（板上）：`python3 tools/cam_preview.py --device /dev/video11 --port 8090`；浏览器开 `http://<板地址>:8090/`
  （含：全幅 + 中心 4× 放大；**清晰度读数**（中心 1:1 Laplacian 方差）+ 本次最大 + 帧序号 + **重复帧计数**；下载当前原图）。
- 停止：`ssh board "sudo pkill -9 -f 'cam_pre[.]view'"`（注意 `pkill -f` 会匹配到自己的命令行，须用 `[.]`/`[-]` 转义写法）。
- **两个实测坑（本档留痕，避免重复踩）**：
  1. **`/dev/video11` 是 Multiplanar 节点**：`ffmpeg/ffplay/mpv` 的 v4l2 输入直接报 `Not a video capture device`（实测），须用 **GStreamer `v4l2src`**（支持 MPlane）；
  2. **`v4l2src` 直连 `mppjpegenc` 会反复编码同一块陈旧缓冲** ⇒ 页面"画面冻住"而帧序号仍在涨（极易误判为相机故障）；**必须前置 `videoconvert` 做一次真实拷贝**。对照实测：加 videoconvert 后 `19 帧 / 19 唯一`；纯软件 `jpegenc` 亦正常。工具因此加了 `dup_run` 监控。
- 旁证（排除项）：同一时刻用 `v4l2-ctl` 抓 4 帧原生 NV12，**4 个 md5 全不同** ⇒ 冻结在编码侧而非相机侧。

### 8.5 对后续开发的影响（口径）

| 方向 | 影响 |
|---|---|
| 链路契约（节点/格式/取流/时序/`seq` 配对） | 无 —— 已实证 |
| ZDE++ 模型侧（转换/对齐/量化/bench）、RGA/曲线应用、双屏、关桌面、并发压测 | 无 —— 与镜头解耦 |
| **画质类结论**（§4.3 三档 A/B 定档、O23 盲评、§9.5 ISP 调参、§13 画质验收） | **挂起**（不得以当前糊图定稿） |
| M1 实拍校准/评测集（≥200 张） | **前置依赖**：镜头定型 + 摆正 + 对焦达标（O31），否则评测集作废重采 |
