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
| 登录用户 | **`cat`**（uid 1000；组含 `sudo` / `adm` / `video` / `dialout`）；**`sudo -n` 需密码** → 有副作用操作须用户授权 |
| 主机名 | `lubancat` |

## 2. 硬件

| 项 | 实测 |
|---|---|
| SoC | RK3588（LubanCat-5 V2），内核 **5.10.160** |
| GPU | **Mali-G610**（Valhall）：驱动已加载（`mali fb000000.gpu: Probed as mali0`）+ 固件已载（`0x1010000`）+ 用户态 `libmali.so.1.9.0`（**GBM 版**）；节点 `/dev/mali0`、`/dev/dri/renderD128|129`、`card0|1`；devfreq 当前 **300 MHz** |
| NPU | `fdab0000.npu`；devfreq 节点存在；驱动 **rknpu 0.9.8**（2024-08-28 构建） |
| 内存 | total **7.7 GiB**（available 7.1 GiB） |
| eMMC | `mmcblk0` **58.7 G**（`name=11S000`、`manfid=0x2f`；厂商标识字段未得）；根分区 `/dev/mmcblk0p3` 58 G，**可用 51 G** |
| 摄像头 | ❌ **未接入/未使能**：`/sys/class/video4linux/` 空、无 `/dev/video0…N` 与 `/dev/v4l-subdev*` / `/dev/media*`、dmesg 无 `imx415`/`rkisp`/`rkcif`（对应风险 R12） |

## 3. 软件栈（**M0-1 定案项**）

| 层 | 实测 | 影响 |
|---|---|---|
| 发行版 | **Debian GNU/Linux 11 (bullseye)** | 对应厂家"以 Debian11 为主"；`/boot/extlinux`（extboot 方式） |
| 板端 python3 | **3.9.2** | ⇒ `rknn_toolkit_lite2` 用 **cp39** wheel；**pybind11 扩展 ABI = `cpython-39-aarch64`**（`docs/build.md` §4） |
| NPU 运行时 | **`librknnrt 1.5.0`**（2023-05-25 构建） | ⚠️ **与主基准 §2.2「与 toolkit2 同版本号」纪律联动**：toolkit2 用 2.3.2 转出的模型在本板会报 `Invalid RKNN model version`；处置已定为**先 A/B 实测再定版**（见 §5） |
| NPU 驱动 | **rknpu 0.9.8** | ✅ 满足官方 V2.3.0 的「≥0.9.2」下限 |
| 图形/桌面 | 未见运行中的图形会话（未探测 systemd 目标，属待补项） | 关桌面两态（§2.3）待演练 |

## 4. 与 M0-1 出口判定对照（§11.1-1）

| 判定项 | 状态 |
|---|---|
| 板型（Cat-4/5） | ✅ **LubanCat-5 V2** |
| 内核 / 发行版 | ✅ 5.10.160 / Debian 11 |
| 软件版本三元组 | 部分：librknnrt **1.5.0**、driver **0.9.8**、toolkit2 **待 A/B 定版** |
| overlay / `/dev/videoX` / rkisp 节点 |  **未满足**（摄像头未接入，见 §2） |
| 丝印 CAM 口 lane 数 |  待摄像头接入后核（Cat-5 仅 CAM0/CAM1 为 4-lane，R12） |
| 屏插件 / 横竖屏 / connector | ⬜ 未核（待补 `probe_drm`，服务 M0-9） |

## 5. 待办与阻塞

| # | 事项 | 阻塞 / 约束 |
|---|---|---|
| B1 | **librknnrt 1.5.0 vs 2.3.2 的 A/B 实测**（项目方已定路线：ZDE++ 单模型各转一次 + 板端加载/对齐/耗时） | **阻塞 M0-2**（转换与测速）；需 PC 端两个转换环境与 ZDE++ 权重 |
| B2 | 摄像头链路排查（IMX415 接入 / overlay / 设备树插件） | 阻塞 M1（不阻塞 M0） |
| B3 | 补探 DRM connector 与屏参数（`board-ops` 的 `probe_drm`） | 服务 §11.1-1 / §11.1-9 |
| B4 | 有副作用操作（换板上 `librknnrt`、装 apt 组件、停流/复位）**均需用户授权**并先记录恢复步骤（规则 `30-board-safety.md` §4） | — |

## 6. 待配置（板端）

| # | 事项 | 前置 |
|---|---|---|
| B5 | `rknn_toolkit_lite2`（cp39 wheel）装到板端 python | 版本须与板上 `librknnrt` 一致（受 B1 阻塞） |
| B6 | 日志目录与存图目录（`logging.dir` / `storage.dir`，§8.3、`docs/logging.md` §2） | 部署阶段（M3 前后） |
| B7 | systemd 单元 `starlight-demo.service`（§10.6-D 手工清单） | M3/M4 |