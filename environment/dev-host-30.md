# 设备档案：开发机 192.168.0.30（x86_64 上位机）

| 项 | 内容 |
|---|---|
| 地位 | 本仓库的**环境记录**（对照规则 `00-ssot.md` §2 的 C 层记录类），承载 `docs/plans/M0.md` 前置条件 P3 所指"PC 端转换环境"的**实际观测值** |
| 效力 | **非验收依据**：不得作为达标依据，也不得替代 `bench.py` 报告（规则 `10-evidence.md` §1） |
| 对应主基准节 | §10.1（软件栈：PC 端转换 / 板上运行时）、§10.2（模型转换流程，PC 端）、§10.3（代码结构）；从属规范 `docs/build.md` §1–§2 |
| 状态 | `draft`（本档为**环境快照**，机器软件栈变更后须重新采集并回写本档；不进验收链） |

> 本档只记录**本机实测到的环境事实**与**缺失项**，不复制任何规范取值、不裁定开放项。取值一律以主基准 §10.4 与 `docs/build.md` 为准。

## 1. 角色与边界

| 项 | 内容 |
|---|---|
| 本机身份 | **开发机 / 上位机（PC 端）**，Trae IDE 在此运行，仓库 `Img_enhance_demo` 位于 `/home/jenkolux/PKU_WH/` |
| 架构 | **x86_64** —— 与目标平台 aarch64 不同架构，**不能**作为板上运行期结论的来源（`docs/build.md` §1） |
| 在本项目中的职责 | ① 代码编辑与版本管理；② `docs/build.md` §4 的 pybind11 模块**交叉编译备选路径**；③ §10.2 的**模型转换链**（`torch.onnx.export` → `onnxsim` → `rknn-toolkit2`）；④ 代用数据准备与离线评测 |
| 明确不在本机 | V4L2 / DMABUF / RGA / NPU / DRM 双 CRTC 等**板上专属**链路——本机无 `/dev/video*`、无 NPU、非目标平台，相关验证只能在演示板 192.168.0.104 上做 |

## 2. 硬件清单（采集时点：2026-09-17 16:54 CST；GPU 行 17:0x 复采修正）

| 类别 | 实测值 |
|---|---|
| 整机 | ASUS `All Series`（主机名 `jenkolux-All-Series`），主板 ASUSTeK **B85-PRO GAMER**，BIOS 2203（2015-04-27） |
| CPU | Intel **Core i7-4790 @ 3.60 GHz**，4 核 8 线程（2 线程/核），L3 8 MiB，x86_64 |
| 内存 | **15 GiB** 物理（采集时空闲 ≈10 GiB，可用 ≈13 GiB），Swap 2 GiB（未使用） |
| GPU | NVIDIA **GeForce RTX 3090**（GA102，PCI `10de:2204`，子系统 Gigabyte `4042`，Video BIOS 94.02.26.08.54）—— 驱动 **535.230.02 已加载**；详见 §3.4 |
| 系统盘 | `/dev/sda` KingDian S280 **240 GB**（`/dev/sda5` 挂 `/` 219 G，已用 60 G，**可用 148 G**） |
| 数据盘 | `/dev/sdb` Great Wall GW600 **512 GB** ext4，**当前未挂载**（无挂载点） |
| 移动盘 | `/dev/sdc` SanDisk Cruzer Blade 28.7 GB，挂 `/media/jenkolux/WIN`，可用 8.1 G |
| 网卡 | `eno1` Intel I217-V，**192.168.0.30/24**，网关 192.168.0.1（metric 100） |
| 外设 | Cherry 机械键盘、TESORO 鼠标；**无 USB 摄像头**（代用数据源 P4 尚未接入本机） |
| 传感器 | `sensors` 不可用（lm-sensors 未安装）—— 非本机必需项 |

## 3. 软件环境

### 3.1 操作系统与运行时

| 项 | 实测值 |
|---|---|
| 发行版 | Ubuntu **20.04.6 LTS**（Focal） |
| 内核 | `5.15.0-139-generic`（x86_64） |
| systemd 默认目标 | `graphical.target`；采集时无图形会话（`XDG_SESSION_TYPE=tty`） |
| SSH 服务 | `sshd` **active**（`systemctl is-active ssh`）= active |
| 时间 | CST（Asia/Shanghai）；开机 2026-09-17 16:03:53，采集时已运行 50 分钟 |

### 3.2 编译工具链（对照 `docs/build.md` §2 / §3）

| 工具 | 实测版本 | 备注 |
|---|---|---|
| gcc / g++ | **9.4.0**（Ubuntu 9.4.0-1ubuntu1~20.04.2） | C11 + C++17 支持满足 `docs/build.md` §1 的语言标准要求在**本机**成立；dpkg 登记（`build-essential 12.8ubuntu1.1`） |
| cmake | **3.27.1** | 高于 `docs/build.md` §2 的 `[待复核]` 下限；**来源 = pip 用户级安装**（`/home/jenkolux/.local/bin/cmake`），**dpkg 未登记** —— 换用户或换机器即不存在，构建脚本不得假定它在系统路径里 |
| make | GNU Make 4.2.1 | dpkg 登记（`make 4.2.1-1.2`） |
| ninja | 1.10.2 | **来源 = conda base**（`/home/jenkolux/anaconda3/bin/ninja`），**dpkg 未登记**；同上，不属系统组件 |
| git | 2.50.1 | 仓库已初始化（main 分支，主基准 §14 v3.18） |
| aarch64-linux-gnu-gcc | **未安装** | 交叉编译备选路径当前**不可用**（`docs/build.md` §1 的备选路径） |
| clang | 未安装 | — |

### 3.3 Python 环境

| 项 | 实测值 |
|---|---|
| 默认 `python3`（采集时点 16:54） | **`/home/jenkolux/anaconda3/bin/python3` = Python 3.11.3**（conda base 在 PATH 首位，**非** venv） |
| 默认 `python3`（**现状**，2026-09-17 变更后） | **`/usr/bin/python3` = Python 3.8.10** —— 已执行 `conda config --set auto_activate_base false`（写入 `~/.condarc`）：**新开终端**不再自动激活 base、`anaconda3/bin` 不再进 PATH；**已开的会话不受影响**；需 conda 时显式 `conda activate base`（或指定环境）；**恢复**：`conda config --set auto_activate_base true` |
| 系统 `python3` | `/usr/bin/python3` = Python **3.8.10** |
| conda 根 | `/home/jenkolux/anaconda3`（占 21 G）；`CONDA_PREFIX=/home/jenkolux/anaconda3` |
| 已有 conda 环境 | `base`(3.11.3)、`hugging`、`llama`、`qwen`、**`rknn`(3.8.20，2026-09-17 新建，见下)** |
| 扩展后缀 | `python3-config --extension-suffix` = **`.cpython-311-x86_64-linux-gnu.so`**（x86_64，**与目标 aarch64 不一致**） |
| pip | 23.1.2（conda base）；系统 pip 20.0.2 |
| pip 镜像 | **未配置**（无 `~/.pip/pip.conf`、`~/.config/pip/pip.conf`、`/etc/pip.conf`） |
| apt 源 | `cn.archive.ubuntu.com`（focal 全套） |

**本项目解释器口径（v3.20 按厂家资料修正为"三类用途"）**：本工程有**三个互不相同的解释器用途**，不可混为一谈——

| 用途 | 用哪个解释器 | 依据 |
|---|---|---|
| 工程脚本 / MCP（`board-ops` 等） | 系统 **`/usr/bin/python3`**（3.8.10），**零依赖、不装包** | 不依赖 PATH 解析：**IDE 以 GUI 会话启动子进程时不读 `~/.bashrc`**，PATH 结果不可控 |
| **模型转换（rknn-toolkit2）** | **专用 conda 环境**（不装进 `base`、不装进系统 python） | 需按 toolkit2 的 `requirements` 装 torch/onnx 等大包，与系统 python 的 apt 包隔离；**官方 V2.3.0 文档示例 py3.8，野火 2026-09 教程实测 py3.11，两者均可用**（按对应 `requirements_cpXX-<ver>.txt` 与 wheel 选取） |
| 板上运行期 / pybind11 扩展 | **板上镜像自带 python3**（版本随镜像：Debian11=3.9、Ubuntu 20.04=3.8、Debian10=3.7） | `docs/build.md` §4 要求扩展与**运行期** Python 同 ABI；板上 python 版本由 M0-1 探板定案（主基准 §10.1） |

> 上一版本档写的"板上与开发侧工具链一律以系统 `python3` 为准"**对"模型转换"一栏是错的**（会把 torch/onnx 装进系统 python，与 apt 管理的包冲突），已在 v3.20 修正。本节"扩展后缀"行的 conda `.cpython-311-x86_64-linux-gnu.so` 与目标 aarch64 及板上 Python 均不一致，故**交叉编译路径当前不可用**（§3.2 / §6-C3）。

**依赖对照（对照 rknn-toolkit2 2.3.2 的 `requirements_cpXX-2.3.2.txt`；cp38 与 cp311 两版 pin 一致）**：专用环境 **conda `rknn`（py3.8.20）已建、目前为空**（见 §6-C1），下列依赖**须全部装入该环境**；本表作用是判断"本机已有版本与 pin 是否相容、哪些必须新装"，**不代表专用环境已具备**。

| pin 项 | 本机实测（2026-09-17） | 判定 |
|---|---|---|
| `numpy <=1.26.4` | conda base **1.24.3** ✓ / 系统 python3 1.24.4 ✓ | 满足 |
| `torch >=1.10.1,<=2.4.0` | conda base **2.0.1** ✓ | 满足 |
| `setuptools <81`（`pkg_resources` 约束） | conda base **67.8.0** ✓ | 满足 |
| `scipy >=1.9.3` | conda base **1.10.1** ✓ | 满足 |
| `psutil >=5.9.0` | conda base **5.9.0** ✓ | 满足（刚好下限） |
| `ruamel.yaml >=0.17.21` | conda base **0.17.21** ✓ | 满足（刚好下限） |
| `protobuf >=4.21.6,<=4.25.4` | conda base **未安装**；系统 python3 **4.25.8（超上限）** | **须在专用环境按 pin 安装**——系统 python3 的现状正是"混装"隐患 |
| `onnx >=1.16.1` / `onnxruntime >=1.10.0` | 两处均**未安装** | 需安装（另注意 `onnx` 过新会报 `'onnx' has no attribute 'mapping'`） |
| `opencv-python >=4.5.5.64` / `fast-histogram >=0.11` | 均**未安装** | 需安装 |

> 依赖一律以该版本 toolkit2 的 `requirements` 文件为准安装，**不得手改版本顶替**（主基准 §10.2"版本与依赖"段）；缺失清单参见 §3.3 上表与 §6-C1。

**conda base 已装（与本项目相关的子集）**：numpy 1.24.3、torch **2.0.1（CPU 版，`torch.cuda.is_available()=False`）**、scipy 1.10.1、pillow 9.4.0、matplotlib 3.7.1、pyyaml 6.0、pytest 7.3.1、ruamel.yaml 0.17.21、tqdm 4.65.0、pandas 1.5.3。

**核验为缺失**——逐环境查过 `base` / `hugging` / `llama` / `qwen` / `rknn` / `/usr/bin/python3`，**六处全无**：

| 缺失包 | 关联 | 影响 |
|---|---|---|
| `onnx` / `onnxsim` | §10.2 步骤 1–2 | ONNX 导出与简化链**无法执行** |
| `rknn-toolkit2` | §10.1 PC 端转换、§10.2 步骤 3–4 | RKNN 转换**无法执行** |
| `onnxruntime` | 对齐校验 | ONNX 侧推理校验不可用 |
| `pybind11` | `docs/build.md` §4 | 扩展模块构建不可用 |
| `python-drm` / `evdev` | §10.1 显示 / 触控 | 板上路径，本机非必需 |

**PC 端转换环境（2026-09-17 新建，包体尚未安装）**

| 项 | 实测值 |
|---|---|
| 环境 | conda 环境 **`rknn`**，路径 `/home/jenkolux/anaconda3/envs/rknn`，体积 188 M |
| 解释器 | **Python 3.8.20**；pip **24.2** |
| 扩展后缀 | `.cpython-38-x86_64-linux-gnu.so` |
| 选型依据 | **Rockchip 官方对 Ubuntu 20.04 仅支持 Python 3.8 / 3.9**，且 `rknn-toolkit2` 的 wheel **按 Python 版本分档**（`cp38` 等）；conda base 的 3.11.3 不在该矩阵内 → 故不装进 base |
| 隔离性（与 §3.3 口径段的关系） | 该环境**只承载 PC 端模型转换**（§10.2 步骤 1–4），**不参与** pybind 扩展构建、不参与板上路径——后两者仍以**系统 `python3`** 为准（口径段见上）；两处 Python 均为 3.8 系列，故不引入版本分歧 |
| 状态 | **仅建环境，未装任何包**；`onnx` / `onnxsim` / `rknn-toolkit2` 等仍缺（见上表） |

```bash
conda create -n rknn python=3.8 -y     # 已执行；恢复/重建用同一条命令
conda activate rknn && python --version  # Python 3.8.20
```

### 3.4 GPU 与深度学习算力

> **修正记录**：本档 16:54 采集时曾判"未安装驱动"，**依据仅为 `nvidia-smi` 不在 PATH**——该判据不成立（`nvidia-smi` 由 `nvidia-utils-*` 提供，与内核驱动是两回事）。17:0x 复采后按内核侧证据修正如下。

| 项 | 实测值 |
|---|---|
| 硬件 | NVIDIA **GeForce RTX 3090**（GA102，PCI `10de:2204`，`01:00.0`） |
| 内核驱动 | **已安装且已加载** —— `Kernel driver in use: nvidia`；模块位于 `/lib/modules/5.15.0-139-generic/kernel/nvidia-535/`（`nvidia.ko` / `nvidia-uvm.ko` / `nvidia-drm.ko` / `nvidia-modeset.ko` / `nvidia-peermem.ko`） |
| 驱动版本 | **535.230.02**（`/proc/driver/nvidia/version`；GCC version 字段为空） |
| 已装相关包 | `linux-modules-nvidia-535-5.15.0-139-generic`、`linux-objects-nvidia-535-5.15.0-139-generic`、`linux-signatures-nvidia-5.15.0-139-generic`、`nvidia-kernel-common-535`、`nvidia-firmware-535-535.230.02`、`libnvidia-compute-535`、`nvidia-prime`（另有 `-5.15.0-134` 一套与 `linux-modules-nvidia-525-*` 遗留） |
| 设备节点 | `/dev/nvidia0`、`/dev/nvidiactl`、`/dev/nvidia-modeset`、`/dev/nvidia-uvm`、`/dev/nvidia-uvm-tools` —— **均存在**，权限 `crw-rw-rw-` |
| CUDA 用户态库 | `libcuda.so.1`、`libnvidia-ml.so.1` **存在**（`/lib/x86_64-linux-gnu/`）；`libcudart.so` 缺失（CUDA toolkit 未装，非必需——torch 自带） |
| **`nvidia-smi`** | **已补齐**（2026-09-18 09:02，为死机排查装 `nvidia-utils-535` 535.230.02-0ubuntu0.20.04.1，**`--no-install-recommends`** → 未连带装入 `nvidia-driver-535` 元包，X/GL 驱动件仍未装）；实测输出：RTX 3090 / 42–43 °C / 22 W（370 W 上限）/ P8 / `pcie.link.gen.current=1`（空闲降速，**属正常省电行为，非链路故障**） |
| **仍缺失** | `nvidia-driver-535` 元包未装（故无 X/GL 驱动件）；无 `/usr/local/cuda*`、无 `nvcc` |
| torch | 2.0.1 **CPU 版**（`torch.cuda.is_available()=False`，`torch.version.cuda=None`）—— **驱动已具备，缺的是 CUDA 版 torch** |
| 结论 | **GPU 算力"差一步"**：内核驱动 + `libcuda` + 设备节点 + `nvidia-smi` 齐备；**只剩**把 torch 换成 CUDA 版构建（① 已于 2026-09-18 完成）。当前**仍无可用 GPU 算力**（见 §6 C4） |

### 3.5 网络可达性（只读探测）

| 目标 | 结果 |
|---|---|
| `pypi.org/simple/` | HTTP **200**，1.59–1.72 s |
| `files.pythonhosted.org` | 可达（根路径 404 = 正常响应） |
| `pypi.tuna.tsinghua.edu.cn/simple/` | HTTP **200** |
| `repo.anaconda.com`（`defaults` 源） | **HEAD 200，1.78 s** —— 注意：整包 GET `repodata.json` 会**超时**（文件大），`conda create` 因此偏慢但可用 |
| `mirrors.tuna.tsinghua.edu.cn`（conda-forge / pkgs/main） | HEAD **200**，0.91–0.98 s（可作 conda 镜像备选） |
| `www.baidu.com` | HTTP 200，0.47 s |
| 代理环境变量 | 无 `http_proxy` / `https_proxy`；仅见 Trae 自身的 `ICUBE_PROXY_HOST=127.0.0.1` / `PREVIEW_PROXY_ENABLED=false` |
| 演示板 `192.168.0.104` | ✅ **可达（2026-09-18 复测更正）**：`ssh board` 免密登录成功（实测返回 `host=lubancat user=cat`）。**历史**：2026-09-17 17:0x 曾**不可达**（2 包全丢、`/dev/tcp/192.168.0.104/22` 报"没有到主机的路由"），该记录已被本次复测取代 |

### 3.6 系统组件与驱动审计（2026-09-17 17:0x 复采，只读）

核验方式：`dpkg-query -W` 逐个查安装态 + `lspci -k` / `lsmod` / `/proc/driver/nvidia` 查驱动侧。**除"新建 `rknn` 环境"外，本机未安装任何软件包、未改任何配置。**

**A. 构建与工具链** —— 系统组件齐备

| 组件 | 版本 | 判定 |
|---|---|---|
| `build-essential` | 12.8ubuntu1.1 | 已装（含 `gcc`/`g++` 9.4.0、`make` 4.2.1） |
| `python3-dev` / `python3.8-dev` | 3.8.2 / **3.8.10-0ubuntu1~20.04.18** | 已装（pybind11 构建所需头文件齐备） |
| `python3-pip` | 20.0.2 | 已装（系统侧） |
| `pkg-config` / `autoconf` / `automake` / `libtool` | 0.29.1 / 2.69 / 1.16.1 / 2.4.6 | 已装 |
| `cmake` / `ninja` | 3.27.1 / 1.10.2 | **可用但非系统包**（pip 用户级 / conda base，见 §3.2） |
| `nasm` | — | 未装（本项目非必需） |

**B. 模型转换与图像依赖** —— **大面积缺失**（`rknn-toolkit2` 与 `onnx`/`opencv` 的官方依赖清单）

| 包 | 状态 | 影响 |
|---|---|---|
| `libprotobuf-dev` / `protobuf-compiler` | **未装** | protobuf 头与编译器缺失，ONNX / RKNN 相关 Python 包安装与转换链受影响 |
| `libxslt1-dev` | **未装** | RKNN Toolkit2 官方依赖清单列出 |
| `libglib2.0-dev` | **未装** | 同上 |
| `libgl1` | 1.3.2 已装 | ✅（`libgl1-mesa-glx` 未装，但 `libgl1` 已覆盖运行期） |
| `libsm6` | 2:1.2.3 已装 | ✅ |
| `zlib1g-dev` | 1.2.11 已装 | ✅ |
| `libjpeg-dev` / `libpng-dev` / `libtiff-dev` | **未装** | OpenCV 从源码构建才需要；仅用 pip wheel 则非必需 |
| `libavcodec-dev` / `libavformat-dev` / `libswscale-dev` | **未装** | 同上 |
| `libgstreamer1.0-dev` / `libgtk-3-dev` / `libhdf5-dev` | **未装** | 同上 |

**C. 板上链路开发头文件**（`docs/build.md` §1 交叉编译备选路径所需）

| 包 | 状态 |
|---|---|
| `libdrm-dev` / `libv4l-dev` / `v4l-utils` / `libgbm-dev` | **全部未装** —— 交叉编译板上程序前须补齐；**P1（板可达）已于 2026-09-18 解除**，但**板上 Python 版本与 sysroot 对齐仍未做** ⇒ 现在装仍无从验证 |

**D. 驱动侧** —— 无阻塞项

| 子系统 | 结论 |
|---|---|
| GPU | 见 §3.4：驱动 535.230.02 **已加载**；`nvidia-utils-535` 已于 2026-09-18 补装（`nvidia-smi` 可用） |
| 网卡 | `eno1` Intel I217-V，驱动 **`e1000e` 已加载**；`state UP`，**RX/TX errors = 0、dropped = 0**（采集时累计 43.7 万收包 / 24.0 万发包） |
| 存储 | Intel 8 系列 SATA 控制器，驱动 **`ahci` 已加载** |
| 缺失固件 | 未查到 firmware 加载失败记录（`dmesg` 在沙箱内不可读，`dmesg_restrict=0`；此项**未完全核验**） |

**E. 审计结论**

1. **构建链在本机就绪**（gcc 9.4 + python3.8-dev + make + pkg-config），但 `cmake`/`ninja` 是用户级/conda 安装，**不属系统组件**——构建脚本须显式解析或固定路径。
2. **模型转换链的依赖缺口有两层**：Python 包（§3.3 缺失表）**加上** apt 组件（本表 B 组，尤其 `libprotobuf-dev` / `libxslt1-dev` / `libglib2.0-dev`）。装 Python 包**必须先补 B 组**，且需 sudo（本终端无免密 sudo）。
3. **驱动层无阻塞**：GPU / 网卡 / 存储驱动均已加载，`nvidia-smi` 只影响可读性、不影响算力本体。
4. **C 组（板上开发头文件）**：P1（板可达）已解除，但**仍受 sysroot 未对齐阻塞**，不宜在此之前安装，以免锁定错误版本。

## 4. 通道与访问

| 项 | 状态 |
|---|---|
| 本机自连免密 | **已配置**（2026-09-17）：`~/.ssh/id_ed25519`（ed25519，无口令，指纹 `SHA256:M4te9AEI9Y2RWqIIU38LjLjN8LGWiP02goLCqsHcGxc`）已入 `~/.ssh/authorized_keys`（该文件共 2 行，另有 1 条历史 RSA 公钥；无 `~/.ssh/id_rsa` 私钥）；`ssh -o BatchMode=yes jenkolux@192.168.0.30` 验证通过 |
| `~/.ssh/config` | ✅ **已存在（2026-09-18）**：含 `Host board`（HostName 192.168.0.104 / User cat / IdentityFile `~/.ssh/id_ed25519`）与 `Host dev30`（本机自连别名）。**历史**：2026-09-17 时该文件不存在 |
| 开发机 → 演示板 | ✅ **已打通（2026-09-18）**：`ssh board` 免密可用（ed25519 公钥已入板端 `authorized_keys`）；本档 §7/§9 的板端实测、屏调试与摄像头预览均经此通道执行 |
| `board-ops` MCP | server 代码在 `.trae/mcp/board-ops/server.py`（主基准 §14 v3.18）；**注册入口在 IDE 的 MCP 设置（JSON）**，模板见 **§4.1**。`BOARD_SSH_HOST` / `BOARD_SSH_USER` **仍未在 IDE 侧配置**（C7）→ 8 个只读工具可被列出，但调用时返回配置指引、**不执行任何远程命令**；**板本身已可达**（2026-09-18）⇒ 该配置现在可做且可验证 |
| git 身份 | ✅ **（2026-09-18 已完成全局配置）** `git config --global user.name=formkk` / `user.email=formk@163.com`（用户执行；实测 `git config --global --list` 回读一致）⇒ **此后提交无需再带 `-c`**。**历史沿革**：配置前 `~/.gitconfig` 内 `user.name`/`user.email` 均空（该文件仅有 git-lfs filter 与 `safe.directory=*`），IDE 侧虽配 `formkk <formk@163.com>` 但命令行不读取，故首提以 `git -c user.name=formkk -c user.email=formk@163.com` 单次指定完成（commit `337eb3d`），本仓 `e3fe059`/`dbdaedc` 亦为同样单次指定 |

### 4.1 `board-ops` MCP 注册模板（IDE 侧 JSON）

```json
{
  "mcpServers": {
    "board-ops": {
      "command": "/usr/bin/python3",
      "args": [
        "/home/jenkolux/PKU_WH/Img_enhance_demo/.trae/mcp/board-ops/server.py"
      ],
      "env": {
        "BOARD_SSH_HOST": "192.168.0.104",
        "BOARD_SSH_USER": "<板上用户，本档未核实>",
        "BOARD_SSH_PORT": "22"
      }
    }
  }
}
```

- `command` 用**绝对路径** `/usr/bin/python3`：理由见 §3.3 口径段（GUI 子进程不读 `~/.bashrc`，PATH 解析不可控）；该 server **零依赖**，3.8.10 与 3.11.3 均已实测可跑。
- `env` 现**可填写**（板已可达，见 §3.5）；留空或省略时工具返回配置指引，不会瞎连。
- 若界面校验要求传输类型，按提示补 `"type": "stdio"`。
- 可选项：`BOARD_SSH_KEY`（私钥路径）、`BOARD_SSH_TIMEOUT`（默认 20 s）。

## 5. 与 M0 前置条件对照（`docs/plans/M0.md` §2）

| 前置 | 现状 | 判定 |
|---|---|---|
| P1 演示板可达（SSH） | ✅ **已满足（2026-09-18 更正）**：`ssh board` 免密登录成功，板端命令可执行 | **已满足 —— 不再是 M0 阻塞**。**历史**：2026-09-17 记录为"无路由、端口不可达"，已被取代。**遗留**：`board-ops` MCP 的 `BOARD_SSH_*` 环境变量仍未在 IDE 侧配置（C7），但板本身可达、该配置现在可做且可验证 |
| P2 硬件齐套 | 板、IMX415、MIPI 屏、HDMI、风扇状态**本机无法核验** | 未知（待板上只读探测） |
| P3 模型与工具链 | **部分推进**：转换环境 `rknn`(py3.8) 已建（§3.3）；但 Python 包（`onnx`/`onnxsim`/`rknn-toolkit2`/`pybind11`）与 apt 组件（`libprotobuf-dev` 等，§3.6-B）**双缺** | **未满足** —— 仍有**两层缺口**，且补 apt 组需 sudo |
| P4 代用数据源 | 本机无 USB 摄像头、无 `/dev/video*`，亦未见离线暗光集 | **未满足**（待确认数据来源） |

## 6. 待配置 / 待确认项

> 下表除 C1 的"环境已建"外**均未执行**；标注了阻塞关系与前置条件，执行前须逐项确认（`rknn-toolkit2` 必须与板上 `librknnrt` **同版本号**、`rknpu` 驱动 **≥ 下限**，约束主基准 §2.2；**在探板取得两者版本前不得定版**）。

| # | 事项 | 前置 / 约束 |
|---|---|---|
| C1 | Python 转换环境（`onnx` / `onnxsim` / `onnxruntime` / `rknn-toolkit2`） | **环境已建**（conda `rknn`，py3.8.20，§3.3）；**包体待装**。装前须先补 apt 组件（§3.6-B，需 sudo）；依赖按该版本 toolkit2 的 **`requirements_cp38-<ver>.txt` 全量安装**（py3.8 → 用 cp38 档与对应 wheel），其中 `onnx`/`onnxsim`/`onnxruntime` 不受板端约束、**可先装**；而 `rknn-toolkit2` 必须与板上 `librknnrt` **同版本号**（主基准 §2.2 / §10.1，v3.20 已由"同代"精确化）→ **其版本受 P1（探板）阻塞**；**P1 已解除且探板已完成**（2026-09-17，板上 `librknnrt` = **2.3.2**，见 `environment/board-lubancat5.md` §3）⇒ 该约束**已可落定**。**opset 基线 12 见主基准 §10.2** |
| C1b | apt 组件补齐（`libprotobuf-dev` / `protobuf-compiler` / `libxslt1-dev` / `libglib2.0-dev`，§3.6-B） | **需 sudo**（本终端 `sudo -n` 失败，需密码）→ 须用户执行或提供授权 |
| C2 | `pybind11`（`docs/build.md` §4） | 版本须**锁定**并写入构建日志；ABI 与运行期 Python 一致 |
| C3 | 交叉编译链 `aarch64-linux-gnu-gcc`（`docs/build.md` §1 备选路径） | 须用与板上同代 sysroot；产物 ABI 须与板上 Python 一致 → **P1 已解除（板可达）**，现仅受 **sysroot 未对齐**阻塞。附带的 `libdrm-dev` / `libv4l-dev` / `v4l-utils` / `libgbm-dev`（§3.6-C）同 |
| C4 | GPU 算力补齐（§3.4） | **① `nvidia-utils-535` 已于 2026-09-18 完成**（`nvidia-smi` 可用）；**仅剩 ② CUDA 版 torch**（pip，可装进专用环境）。若 M1 fine-tune 需要 GPU 才需做，否则可缓 |
| C5 | pip 镜像（§3.3） | 可选，仅影响下载速度；PD 直连已 200 |
| ~~C6~~ | ~~git 身份（§4）~~ | ✅ **已于 2026-09-18 完成**：全局 `user.name=formkk` / `user.email=formk@163.com` 已配置并回读核验（详见 §4 该行） |
| C7 | `board-ops` MCP 的 `BOARD_SSH_*` 环境变量（IDE 侧 JSON，见 §4.1） | **P1 已不阻塞**（板可达，2026-09-18）⇒ 现在可配且可验证；`~/.ssh/config` 的 `board` 别名已就绪 |
| C8 | 代用数据源接入（§5 P4） | 需确认用 USB 相机（本机无）还是既有离线暗光集 |

## 7. 采集元信息（可复现命令）

采集时点：**① 2026-09-17 16:54 CST**（硬件 + 软件环境基线）、**② 2026-09-17 17:0x CST**（系统组件与驱动审计复采 —— §3.2 的 cmake/ninja 来源、§3.3 的 `rknn` 环境、§3.4 GPU 修正、§3.6 组件表均出自该次）、**③ 2026-09-18 08:58–09:05 CST**（死机取证与监测配置，见 §9；§3.4 的 `nvidia-smi` 行出自该次）。
采集方式：**① ② 两次为只读探测**（规则 `30-board-safety.md` §4 口径），唯一写入动作是新建 conda 环境 `rknn`；**③ 次含系统写入**（装 4 个监控包 + 3 处配置变更 + 1 个 systemd 服务启用），逐项见 §9.4 的写入清单与回退方式。

```bash
uname -a; cat /etc/os-release
lscpu; free -h; df -h -x tmpfs -x devtmpfs; lsblk -d -o NAME,SIZE,MODEL
cat /sys/class/dmi/id/{sys_vendor,product_name,board_name,bios_version,bios_date}
lspci -nn | grep -Ei "vga|ethernet"; ip -brief addr show; ip route
for c in gcc g++ cmake make ninja git python3; do $c --version | head -1; done
python3-config --extension-suffix; python3 -c "import sys,sysconfig;print(sys.version,sysconfig.get_platform())"
python3 -m pip list            # 取 §3.3 的包版本；缺失项逐环境 import 复核
lsusb; ls /dev/video*          # 代用数据源核验
ssh -o BatchMode=yes jenkolux@192.168.0.30 'hostname; whoami'   # 免密核验
ping -c 2 -W 2 192.168.0.104; timeout 5 bash -c 'cat </dev/null >/dev/tcp/192.168.0.104/22'
```

### 7.1 第二次采集追加的命令（§3.2 / §3.3 / §3.4 / §3.6）

```bash
# —— cmake / ninja 来源（dpkg 未登记）——
command -v cmake ninja                 # → ~/.local/bin/cmake（pip 用户级）、~/anaconda3/bin/ninja（conda base）

# —— conda 环境 ——
conda create -n rknn python=3.8 -y
conda activate rknn && python --version && python -m pip --version && du -sh $CONDA_PREFIX
python -c "import sysconfig;print(sysconfig.get_config_var('EXT_SUFFIX'))"

# —— 系统组件安装态（§3.6 A/B/C 三组的核验口径）——
for p in build-essential python3-dev python3.8-dev python3-pip pkg-config autoconf automake libtool nasm \
         libprotobuf-dev protobuf-compiler libxslt1-dev libglib2.0-dev libsm6 libgl1-mesa-glx libgl1 zlib1g-dev \
         libjpeg-dev libpng-dev libtiff-dev libavcodec-dev libavformat-dev libswscale-dev \
         libgstreamer1.0-dev libgtk-3-dev libhdf5-dev libdrm-dev libv4l-dev v4l-utils libgbm-dev; do
  dpkg-query -W -f='${Status} ${Version}\n' "$p" 2>/dev/null | grep -q "^install ok installed" \
    && echo "$p 已装" || echo "$p 未装"
done

# —— 驱动侧（§3.4 的关键修正依据：按内核侧证据判，而非 nvidia-smi 是否存在）——
lspci -k -s 01:00.0; lsmod | grep -E "nvidia|nouveau"
cat /proc/driver/nvidia/version; cat /proc/driver/nvidia/gpus/*/information
modinfo nvidia | grep -E "^filename|^version"
ls /dev/nvidia*; ldconfig -p | grep -E "libcuda|libnvidia-ml"
dpkg -l | awk '$1=="ii" && /nvidia/ {print $2, $3}'
apt-cache policy nvidia-utils-535
lspci -k | grep -A2 -iE "ethernet|sata"        # 网卡/存储驱动
sudo -n true || echo "sudo 需密码"
```
## 8. 转换环境与隔离纪律（v3.22 追加，实测）

### 8.1 用户级 site-packages 污染（**必须隔离**）

| 项 | 实测 |
|---|---|
| 用户级 site | `~/.local/lib/python3.8/site-packages`，**244 个包**（含 `torch 2.0.1`（CUDA 版，带 `nvidia-*`）、`transformers`、`jupyter`、`accelerate` 等） |
| 影响 1 | 任何 py3.8 环境（含新建 conda 环境）执行 `pip list` 都会**列出不属于该环境**的包——曾据此误判"环境已有 torch/transformers" |
| 影响 2 | `pip install -r requirements.txt` 会把这些包判为**已满足**并跳过安装 ⇒ 该环境的实际依赖**不可复现**（依赖用户目录，换用户/换机即失效） |
| 处置（铁律） | **所有转换 / 探测 / 校验命令一律加 `PYTHONNOUSERSITE=1`**（pip 与 python 均生效）；环境自检用 `python -c "import site;print(site.ENABLE_USER_SITE)"` 应为 `True`（该值由解释器默认给出，**不能靠它判断是否被污染**，只能靠命令级环境变量） |

### 8.2 三套转换环境（M0-2 A/B 用，py3.8.20，均建在 `/home/jenkolux/.conda/envs/`）

| 环境 | toolkit2 | 依赖要点 | 用途 / 状态 |
|---|---|---|---|
| `rknn232` | **2.3.2**（PyPI 正式版） | `torch 2.0.1+cpu`、`onnx 1.17.0`、`onnxruntime 1.19.2`、`onnxsim 0.5.0`、`numpy 1.24.4`、`protobuf 4.25.4`、`opencv-python 5.0.0.93`、`scipy 1.10.1`、`setuptools 75.1.0` | **ONNX 导出 + B 臂转换（现行基线）**；`from rknn.api import RKNN` 实测 OK |
| `rknn242` | 2.4.2a12（预研归档离线 wheel） | 同上（依赖 pin 与 2.3.2 一致，`onnxruntime ≥1.17.0`） | C 臂转换；**待 M0-4 需要时启用** |
| `rknn150` | 1.5.0+1fa95b5c（官方仓库树 wheel） | **精确旧 pin**：`torch 1.10.1+cpu`、`numpy 1.19.5`、`onnx 1.10.0`、`protobuf 3.12.2`、`tensorflow 2.6.2`、`scipy 1.5.4`、`onnxruntime 1.10.0` | A 臂转换（已判定不采用）；环境体积与脆弱性显著高于 B/C |

> `torch` 一律取 **CPU 版**（`--index-url https://download.pytorch.org/whl/cpu`，在 pin 允许区间内）——避免三套环境各装一份 CUDA 版（约 2 GiB/环境）。

### 8.3 工具链离线缓存与板端包

| 项 | 位置 / 说明 |
|---|---|
| 转换 wheel 与官方 requirements | `refs/pre-research/toolchain/`（`rknn_toolkit2-1.5.0+…whl`、`requirements_cp38-2.3.2.txt`；2.4.2a12 在 `refs/pre-research/2.4.2/`） |
| 板端 lite2（cp39，匹配板上 py3.9.2） | `rknn_toolkit_lite2-1.5.0-cp39-cp39-linux_aarch64.whl`（官方仓库树）、`rknn_toolkit_lite2-2.3.2-cp39-…manylinux…whl`（PyPI） |
| 板端 `librknnrt` 目标版本 | `2.3.2`（预研归档 `m0_results/librknnrt_v232.so`）、`2.4.2a2`（其 `2.4.2/a12/runtime/…`）；1.5.0 = 板上现装 |
| 板端无 sudo 加载指定版本 | `unshare -Ur -m sh -c 'mount --bind <so> /usr/lib/librknnrt.so && …'`（lite2 扩展按绝对路径加载，`LD_LIBRARY_PATH`/`LD_PRELOAD`/`LIBRKNNRT_PATH` 均无效）；详见 `docs/evidence/M0-2-ab-toolkit-version.md` §5.2 |

### 8.4 复现命令

```bash
# 单档转换（在 rknn232 环境）
PYTHONNOUSERSITE=1 /home/jenkolux/.conda/envs/rknn232/bin/python tools/convert_models.py export \
  --weights models/zde/Epoch99.pth --h 272 --w 480 --out models/zde/zde_curve_480x272.onnx \
  --input-png docs/evidence/reports/M0-2/input_18_1080x720.png \
  --dump-input docs/evidence/reports/M0-2/input_480x272.npy \
  --dump-pt-ref docs/evidence/reports/M0-2/pt_ref_480x272.npy \
  --report docs/evidence/reports/M0-2/export_onnx.json
PYTHONNOUSERSITE=1 /home/jenkolux/.conda/envs/rknn232/bin/python tools/convert_models.py build \
  --onnx models/zde/zde_curve_480x272.onnx --out models/zde/zde_curve_480x272_fp16_tk232.rknn \
  --quant fp16 --target rk3588 --report docs/evidence/reports/M0-2/build_B_tk232.json

# 板上探测（板端脚本在 ~/rknn_ab/，可一键删除回退）
ssh board 'cd ~/rknn_ab && ./run_arm.sh arm232 232 zde_curve_480x272_fp16_tk232.rknn B_tk232'
```

## 9. 死机事件与取证配置（2026-09-18）

> 本节是**环境记录**，不是项目结论：不进验收链（§头部效力条款），也不构成主基准任何 §15 开放项的裁定。凡标「推断」者为**未证实**，不得当作原因；凡标「已排除」者均附核验来源。

### 9.1 事件：2026-09-18 一次硬死机

| 项 | 实测 |
|---|---|
| 崩溃那次启动 | boot -1，`Fri 2026-09-18 07:51:10 CST` 启动 |
| 最后一条内核日志 | `08:47:18`（`[UFW BLOCK]` mDNS 拦截，来自 `192.168.0.104`） |
| 最后一条用户态日志 | `08:48:37`（`/var/log/syslog`，NetworkManager `CONNECTED_SITE`） |
| 下一次启动 | boot 0，`Fri 2026-09-18 08:51:19 CST` |
| 关机记录 | **无** —— `last -x` 中该次 boot 无 `shutdown`/`Power-Off` 行，journal 亦无 `Shutting down`/`Reached target Power-Off`（对照：boot -2 于 01:07:55 **有**完整关机序列）⇒ **非有序关机**。**判据边界**：boot -3（仅 52 s）结尾只有两次 `Power key pressed` 且同样无关机序列 —— 说明"无关机日志"也可由**长按电源键硬断电**造成，故该判据本身只支持「非有序关机」；**本次归类依据用户报告**，见下一行 |
| 现场现象（**用户确认**，2026-09-18 14:00） | **"画面卡住不动，需按复位或长按电源键才重启"** ⇒ 属**冻结（hang）**，**不是掉电**（掉电会直接黑屏且不会停在同一画面）。据此：① 三种"非有序结束"（有序关机 / 冻结后强制复位 / 长按电源硬断电）中归入**第二类**；② **排除"市电中断 / PSU 瞬间掉电导致黑屏"这一支**，但**不排除**"供电电压跌落引起的系统挂死"（两者表现不同：跌落可致 CPU 停止推进而不复位） |
| 死机窗口 | 可确证的下界 `08:47:18`，上界 `08:51:19`。**推断**可收紧到约 `08:46:40` 之后：网关 mDNS（`SRC=192.168.0.1 DST=224.0.0.1`）在 08:31:44 / 08:33:52 / … / 08:46:40 呈严格 `2 min 08 s` 节奏，下一次（约 08:48:48）缺失。**该收紧为推断，不作为结论。** |

**已排除项**（逐条有核验来源，全部落在崩溃那次启动内）：

| 排除对象 | 核验来源与结果 |
|---|---|
| OOM / 内存耗尽 | `journalctl -b -1`：0 命中 `out of memory`/`oom-kill`；崩溃后 `free` 亦为 15 GiB 仅用 1.3 GiB |
| panic / oops / BUG | 内核日志 0 命中；`panic=0` 本身也不产生自动重启 |
| soft lockup / hard lockup / hung task | 0 命中（`nmi_watchdog=1` 已启用，若为硬 LOCKUP 本应留下记录） |
| MCE / EDAC / Hardware Error | 0 命中 |
| 热击穿 | 0 命中 thermal trip/shutdown；复位后空闲 CPU 包 43 °C、acpitz 27.8/29.8 °C |
| suspend / hibernate | 0 命中 |
| GPU 驱动故障 | `kern.log` 全库 `NVRM`/`Xid` 仅 5 条且均为 "loading"；Xorg 日志（当前与 `.old` 两次启动）仅有既存无害项（`drm ... -2`、`glamor initialization failed`） |
| SATA / 磁盘错误 | `kern.log` 全库仅 link-up 行，0 命中 `failed command`/`exception Emask`/`I/O error`/`hard resetting` |
| 磁盘本体 | SMART **两盘均 PASSED**：sda KingDian S280 240GB（`Power_On_Hours 16495`、`Power_Cycle_Count 279`）、sdb Great Wall GW600 512GB；重分配/待定/离线不可校正/`UDMA_CRC_Error_Count` **全为 0** |
| PCIe 链路错误 | `lspci -vv`：`DevSta` 无 `CorrErr/NonFatalErr/FatalErr`，`CESta` 全无 |
| GPU PCIe 降速 | `LnkSta: Speed 2.5GT/s (downgraded)` 与 `nvidia-smi` 的 `pcie.link.gen.current=1` **是空闲省电行为**（`LnkCap` 为 8GT/s / x16，`Width x16 (ok)`），**不是链路故障**——此项为对先前疑似项的正名 |
| nouveau 混用 | **不成立**：`lspci -k` 为 `Kernel driver in use: nvidia`，`/proc/driver/nvidia/version` = 535.230.02（§3.4 已修正） |

**关键推断边界（必须随结论保留）**：以上排除项合起来说明**没有抓到任何软件侧致死证据**——NMI watchdog 已开却无 hard LOCKUP、且从最后一条日志到复位约有 2–3 分钟可供落盘，与"内核仍在跑、只是用户态卡住"不符，**倾向于系统级瞬时停止**（供电 / 内存-CPU 稳定性 / 总线或器件电气层）。**根因未定论**；现有证据不足以在上述候选之间裁决。这不是"已定位原因"，只是"已划定范围"。

### 9.2 已取证的硬件异常：USB 端口 3-7（CHERRY 键盘）

| 项 | 实测 |
|---|---|
| 该端口设备身份 | `idVendor=046a idProduct=00cb`，`Product: Mechanical Keyboard`，`Manufacturer: CHERRY`（**全速** USB 设备） |
| 故障起点 | **2026-09-17 19:47:51**（boot -2 内，uptime 13438 s）：`reset full-speed USB device number 2 using xhci_hcd` → 反复 `device descriptor read/64, error -71`（EPROTO）→ `usb3-port7: attempt power cycle` → `19:48:02 unable to enumerate USB device` |
| 现状 | 2026-09-18 07:51 与 08:51 两次启动**均复现失败**；`lsusb -t` 中 `Bus 03 Port 7` 为空、`/sys/bus/usb/devices/` 下无 `3-7` ⇒ **该键盘自 9-17 19:47 起已不可用** |
| 同日同控制器另一端口 | 端口 3-8（TESORO 鼠标）于 2026-09-17 11:10 出现**一次性**同类失败（含 1 次 `error -110`），随后恢复并在当前启动正常工作 |
| 历史频次 | 仅能查到 2023-03-02（该次启动时钟错乱）另有一组 `3-8` 的 `-71` — 但 `kern.log` 只覆盖 **9-17 11:08 起**，`journalctl -b -4` 的早期记录时钟不可信 ⇒ **"是否新出现"不可判定，不得表述为"新故障"** |
| 与死机的关系 | **未证实因果**：死机前 13 小时的日志中无任何 USB 事件，崩溃那次启动的最后 60 分钟也无 USB 记录。列为**已确认存在的独立硬件异常**与**待消除项**，不作为死机原因 |
| **归属定位（2026-09-18 13:58:53 实测，已结案）** | 用户把同一把 CHERRY 键盘改插到 **端口 3-6**：**一次性枚举成功**（`usb 3-6: New USB device found, idVendor=046a, idProduct=00cb` → `input: CHERRY Mechanical Keyboard` → `usbhid` 绑定，`/proc/bus/input/devices` 出现 `Handlers=sysrq kbd event21 leds`）。⇒ **键盘本体正常，故障在端口 3-7 一侧**（端口/插座/前面板排线），**不是键盘坏**。该项由"待消除"转为**已定位的端口缺陷** |

### 9.3 取证能力缺口（"为什么没有日志"）

| 缺口 | 实测 |
|---|---|
| 无崩溃转储 | **无 EFI**（`/sys/firmware/efi` 不存在）⇒ 无 pstore/efi_pstore；`kdump-tools` 未装、`cmdline` 无 `crashkernel=` |
| 不自动重启 | `panic=0`、`softlockup_panic=0`、`hung_task_panic=0`、`hung_task_timeout_secs=120` |
| SysRq 转储被禁 | `kernel.sysrq=176`（Ubuntu 默认）= 仅 sync/remount/reboot；**进程转储 `t`/阻塞任务 `w`/内存 `m` 均被禁用** |
| 无硬件看门狗 | `/dev/watchdog*` 不存在（`iTCO_wdt` 未加载）；`systemd` 侧 `#RuntimeWatchdogSec=0` |
| 无死机前遥测 | sysstat 未启用、lm-sensors 未装 ⇒ **死机当场的 CPU/内存/IO/温度完全无记录**（这正是 9.4 要补的） |

### 9.4 本次配置变更（2026-09-18 08:58–09:05 CST，均经 sudo）

| # | 变更 | 目的 | 回退方式 |
|---|---|---|---|
| 1 | 安装 `smartmontools` / `sysstat` / `lm-sensors`（apt，3 个新包） | 磁盘健康 + 系统活动 + 温度采集 | `apt-get remove --purge smartmontools sysstat lm-sensors` |
| 2 | 安装 `nvidia-utils-535`（`--no-install-recommends`，未连带 X/GL 驱动件） | 取得 `nvidia-smi`（GPU 温度/功耗） | `apt-get remove nvidia-utils-535` |
| 3 | `/etc/default/sysstat`：`ENABLED="false"` → `"true"` | 启用 sar 采集 | 改回 `"false"` |
| 4 | `/etc/cron.d/sysstat`：`5-55/10 * * * *` → `* * * * *` | 采样粒度 **10 min → 1 min**，使死机后可回溯最后时刻 | 恢复原行 |
| 5 | 新增 `/etc/sysctl.d/99-crash-forensics.conf`：`kernel.sysrq = 1` | 下次卡死时可用 `Alt+SysRq+t/w/m` 抓任务栈与内存 | 删除该文件 |
| 6 | `smartd` 服务随包启用（`enabled` + `active`） | 磁盘健康告警；当前状态 `Next check of 2 devices will start at ...` | `systemctl disable --now smartd` |

```bash
# 校验（均已执行并得到预期输出）
sar -u -r -f /var/log/sysstat/sa18        # 可读出样本 → 采集生效
nvidia-smi                                 # RTX 3090 / 42–43 °C / 22 W/370 W / P8
cat /proc/sys/kernel/sysrq                 # 1
systemctl status smartd --no-pager         # active (running)，监控 2 盘
sensors                                    # coretemp：Package 43 °C / 四核 38–43 °C
```

### 9.5 待用户决策的后续动作（本档不代为执行）

| 项 | 内容 | 需要的前提 |
|---|---|---|
| A | **已完成**（2026-09-18 13:58）：键盘改插端口 3-6 后恢复正常，见 §9.2 末行。**遗留**：端口 3-7 缺陷本身未修（该端口及该口所在前面板排线不宜再插设备；如需修复须查插座/排线） | — |
| B | 内存稳定性测试（memtest86+）：**无需安装** —— `memtest86+ 5.01-3.1ubuntu2.1` **本就已装**，`/boot/memtest86+.elf` 与 `/etc/grub.d/20_memtest86+` 均在，`grub.cfg` 顶层菜单第 **2** 项即 `Memory test (memtest86+)`（第 3 项为 serial console 变体）。**已安排一次性引导**：`grub-reboot 2` 已执行，`grub-editenv list` = `next_entry=2`；`/etc/default/grub` **未改动**（仍 `GRUB_DEFAULT=0` / `TIMEOUT_STYLE=hidden` / `TIMEOUT=0`），故这是一次性生效、下次重启后自动恢复默认。硬件前提：MBR 分区（`part_msdos`）+ **无 EFI** ⇒ 该 5.01 实模式版本可用 | **待执行**：用户自行选择停机时段重启（见下方测试口径） |
| C | 受控负载复现试验（`stress-ng` + 分钟级采集；死机前的可疑负载为多线程编译，如 08:37 `librknnc.so` 段错误那次编译） | **可能再次死机**，需用户确认 |
| D | 无人值守自愈：`kernel.panic=10` + `softlockup_panic=1`，并在确认 `iTCO_wdt` 可用后设 `RuntimeWatchdogSec` | 会**牺牲现场证据**，须用户权衡 |

**下次死机时的现场取证步骤（条件已具备）**：键盘已于 2026-09-18 13:58 恢复（端口 3-6，`/proc/bus/input/devices` 中 `CHERRY Mechanical Keyboard` 绑 `Handlers=sysrq kbd event21`），`kernel.sysrq=1` 亦已生效 ⇒ 卡住时按 `Alt+SysRq+t`（任务栈）→ `w`（阻塞任务）→ `m`（内存）→ 拍照或抄录屏幕 → `b` 重启。若连 SysRq 也无响应，则说明内核已停止推进（与 `nmi_watchdog=1` 下仍无 LOCKUP 记录一致），此时应转向**硬件层**取证（供电、内存、温度）。事后：`sar -f /var/log/sysstat/sa<DD>` 回溯死机前数分钟的 CPU/内存/IO，`journalctl -b -1` 与 `last -x` 确认是否为硬复位，`nvidia-smi` 与 `sensors` 取复位后基线。

**memtest 测试口径（§9.5-B 的执行约定）**：

1. **进入方式**：直接 `sudo reboot` 即可，**不需要**按 Shift/Esc 选菜单 —— `grubenv` 中 `next_entry=2` 会被 GRUB 一次性消费，之后自动恢复默认引导（已核验 `/etc/default/grub` 未被改动）。若届时未进 memtest，可用 `sudo grub-editenv /boot/grub/grubenv list` 复核 `next_entry`。
2. **时长**：16 GB 单遍约 40–60 分钟；**至少跑满 2 遍**（约 2 小时），最稳妥为过夜 4–8 遍。只跑半遍**不足以排除**间歇性错误。
3. **判读**：看屏幕下方 `Pass` 与 **`Errors`** 计数及失败地址。非 ECC 内存，错误不会被纠错掩盖，出现任何一位错误即判**内存或内存控制器不稳定**。
4. **留存**：拍照记录 `Pass` 数、`Errors` 数、失败地址，以及 memtest 识别到的内存频率/时序（用于与 BIOS 现状对照：当前为 DDR3-1600 配置值、XMP 未启用、两槽 `ChannelA-DIMM0` / `ChannelB-DIMM0`）。
5. **异常处置**：该版本为 5.01（2013 年上游、2020 年打包），个别平台会卡在 `Loading...` 或黑屏无输出。此时**长按电源键重启即可**（`next_entry` 已被消费，会正常回到 Ubuntu）；若确需更新版本，可用 U 盘写 memtest86+ 6.x（本机 `/dev/sdc` Cruzer Blade 可腾用）。
6. **回写义务**：跑完后把结论回填本节 B 行；若出现错误，则据此把 §9.1 的候选排序改为"内存/内存控制器不稳"为**直接证据**，并相应重估"供电"候选的权重。

### 9.6 诊断状态：**挂起**（2026-09-18 14:20 CST）

> **本次死机排查到此挂起，不结案**。根因仍未定论；下列未决项与基线保留，供**下次死机时**继续推断。按用户决定，不主动加压复现（§9.5-C 不做），memtest（§9.5-B）等用户择期重启执行。

**挂起时点的系统基线**（采集于 `2026-09-18 14:10 CST`，uptime 5:18，仅作日后对比基准，**不构成"问题不存在"的依据**）：

| 维度 | 实测 |
|---|---|
| 负载 / CPU | load `0.23 / 0.28 / 0.25`（8 线程）；空闲 **96%**，`wa`=0% |
| 热降频 | 8 个逻辑核 `core_throttle_count` 与 `package_throttle_count` **全为 0**（本次启动从未降频） |
| CPU 频率 | governor `ondemand`，空闲 800 MHz |
| 内存 / Swap | 15 GiB 中用 1.6 GiB、可用 13 GiB；**Swap 0B 使用** |
| PSI 压力 | memory `avg10/60/300` 全 `0.00`；io `some avg300=0.06%`、`full avg300=0.05%` |
| 进程 | 266 任务（1 running / 265 sleeping / 0 stopped / **0 zombie**）；**D 状态进程 0** |
| 磁盘 | `/` 用 46%（95 G / 219 G）；sda `%util` 0.3–1.6%、`r_await`<2 ms；sdb/sdc 近零活动 |
| 文件系统错误 | **0**（无 EXT4-fs error / I/O error / journal abort） |
| 温度 | CPU 包 **39 °C**（核心 35–37 °C）；acpitz 27.8 / 29.8 °C；GPU **44 °C** |
| GPU | 22.4 W（上限 370 W）、**P8**、0% 利用率、显存 6 MiB |
| 失败 systemd 单元 | **0** |
| 硬件错误 | 仅开机横幅 `EDAC MC: Ver: 3.0.0`；**无 MCE / EDAC 报告 / PCIe AER** |
| 磁盘 SMART | 两盘 PASSED（复核于 09:0x） |

**新增一条既存告警（据实记录，未判为故障）**：本次启动日志含 nvidia 资源提示 `resource sanity check: requesting [mem 0x000e0000-0x000fffff], which spans more than PCI Bus 0000:00 [mem 0x000d0000-0x000e7fff window]` 与 `caller os_map_kernel_space+0xa0/0x100 [nvidia] mapping multiple BARs`；另 `hid-generic 0003:195D:1022.0002: ignoring exceeding usage max`（TESORO 鼠标 HID 描述符）与 `at24 0-0050 / 0-0052: supply vcc not found, using dummy regulator`（SMBus SPD EEPROM）。三项均属既存无害，**列此仅为避免日后误读**。

**误读警示**：`journalctl` 中 2 条 `I/O error` 命中来自**排查过程中执行的 grep 命令行**（被 sudo 记入日志），**不是真实 IO 错误**。

**挂起时的未决项**：

| # | 项 | 状态 |
|---|---|---|
| 1 | `grubenv` 的 `next_entry=2` **仍生效** | **任何原因导致的下一次重启都会直接进 memtest86+**（含万一再冻结后按复位）。跑完或按 §9.5-B 步骤 5 处置后自动恢复默认 |
| 2 | §9.5-B 内存测试 | 待用户择期重启执行 |
| 3 | §9.5-C 受控负载复现 | 用户决定不做，保留为可选 |
| 4 | §9.5-D 自动重启自愈 | 未启用（为保留现场证据） |
| 5 | 端口 3-7 缺陷本体 | 未修（该口暂不使用即可） |

**恢复推断的触发条件与第一步**：下次死机发生时，按 §9.5 末段的取证流程采集（`Alt+SysRq+t/w/m` → 拍照 → 复位），随后用 `sar -f /var/log/sysstat/sa<DD>` 取死机前数分钟曲线并与本节基线对比，再用 `journalctl -b -1` + `last -x` 判定是否硬复位。若 memtest 先跑出结果，则以该结果优先重排 §9.1 的候选权重。
