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
| **缺失** | `nvidia-utils-535` **未安装** → **无 `nvidia-smi`**（apt 候选 `535.230.02-0ubuntu0.20.04.1` 可直接补）；`nvidia-driver-535` 元包未装（故无 X/GL 驱动件）；无 `/usr/local/cuda*`、无 `nvcc` |
| torch | 2.0.1 **CPU 版**（`torch.cuda.is_available()=False`，`torch.version.cuda=None`）—— **驱动已具备，缺的是 CUDA 版 torch** |
| 结论 | **GPU 算力"差一步"**：内核驱动 + `libcuda` + 设备节点齐备；补齐只需 ① 装 `nvidia-utils-535`（拿 `nvidia-smi` 做可用性核查）② 把 torch 换成 CUDA 版构建。当前**仍无可用 GPU 算力**（见 §6 C4） |

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
| 演示板 `192.168.0.104` | **不可达**：2 包全丢，`/dev/tcp/192.168.0.104/22` 报"没有到主机的路由"（17:0x 复测同结论） |

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
| `libdrm-dev` / `libv4l-dev` / `v4l-utils` / `libgbm-dev` | **全部未装** —— 交叉编译板上程序前须补齐；受 P1（板不可达、sysroot 无法对齐）阻塞，**现在装也无从验证** |

**D. 驱动侧** —— 无阻塞项

| 子系统 | 结论 |
|---|---|
| GPU | 见 §3.4：驱动 535.230.02 **已加载**，缺 `nvidia-utils-535` |
| 网卡 | `eno1` Intel I217-V，驱动 **`e1000e` 已加载**；`state UP`，**RX/TX errors = 0、dropped = 0**（采集时累计 43.7 万收包 / 24.0 万发包） |
| 存储 | Intel 8 系列 SATA 控制器，驱动 **`ahci` 已加载** |
| 缺失固件 | 未查到 firmware 加载失败记录（`dmesg` 在沙箱内不可读，`dmesg_restrict=0`；此项**未完全核验**） |

**E. 审计结论**

1. **构建链在本机就绪**（gcc 9.4 + python3.8-dev + make + pkg-config），但 `cmake`/`ninja` 是用户级/conda 安装，**不属系统组件**——构建脚本须显式解析或固定路径。
2. **模型转换链的依赖缺口有两层**：Python 包（§3.3 缺失表）**加上** apt 组件（本表 B 组，尤其 `libprotobuf-dev` / `libxslt1-dev` / `libglib2.0-dev`）。装 Python 包**必须先补 B 组**，且需 sudo（本终端无免密 sudo）。
3. **驱动层无阻塞**：GPU / 网卡 / 存储驱动均已加载，`nvidia-smi` 只影响可读性、不影响算力本体。
4. **C 组（板上开发头文件）受 P1 阻塞**，不宜在 sysroot 未对齐前安装，以免锁定错误版本。

## 4. 通道与访问

| 项 | 状态 |
|---|---|
| 本机自连免密 | **已配置**（2026-09-17）：`~/.ssh/id_ed25519`（ed25519，无口令，指纹 `SHA256:M4te9AEI9Y2RWqIIU38LjLjN8LGWiP02goLCqsHcGxc`）已入 `~/.ssh/authorized_keys`（该文件共 2 行，另有 1 条历史 RSA 公钥；无 `~/.ssh/id_rsa` 私钥）；`ssh -o BatchMode=yes jenkolux@192.168.0.30` 验证通过 |
| `~/.ssh/config` | **不存在**（无 `board` 等主机别名） |
| 开发机 → 演示板 | **未打通**：板不可达（§3.5），`ssh-copy-id` 无从执行 |
| `board-ops` MCP | server 代码在 `.trae/mcp/board-ops/server.py`（主基准 §14 v3.18）；**注册入口在 IDE 的 MCP 设置（JSON）**，模板见 **§4.1**。`BOARD_SSH_HOST` / `BOARD_SSH_USER` **未配置** → 8 个只读工具可被列出，但调用时返回配置指引、**不执行任何远程命令** |
| git 身份 | `~/.gitconfig` 内 `user.name` / `user.email` **均未配置**（该文件仅有 git-lfs filter 与 `safe.directory=*`）；**IDE 侧**已配 `formkk <formk@163.com>`，但**命令行不读取**——首提以 `git -c user.name=formkk -c user.email=formk@163.com` 单次指定完成（commit `337eb3d`）；**C6 仍待全局配置** |

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
- `env` 仅在**板可达后**填写（演示板 `192.168.0.104` 现**不可达**，见 §3.5）；留空或省略时工具返回配置指引，不会瞎连。
- 若界面校验要求传输类型，按提示补 `"type": "stdio"`。
- 可选项：`BOARD_SSH_KEY`（私钥路径）、`BOARD_SSH_TIMEOUT`（默认 20 s）。

## 5. 与 M0 前置条件对照（`docs/plans/M0.md` §2）

| 前置 | 现状 | 判定 |
|---|---|---|
| P1 演示板可达（SSH） | 板 `192.168.0.104` 无路由、端口不可达 | **未满足** —— 仍为 M0 第一道阻塞 |
| P2 硬件齐套 | 板、IMX415、MIPI 屏、HDMI、风扇状态**本机无法核验** | 未知（待板上只读探测） |
| P3 模型与工具链 | **部分推进**：转换环境 `rknn`(py3.8) 已建（§3.3）；但 Python 包（`onnx`/`onnxsim`/`rknn-toolkit2`/`pybind11`）与 apt 组件（`libprotobuf-dev` 等，§3.6-B）**双缺** | **未满足** —— 仍有**两层缺口**，且补 apt 组需 sudo |
| P4 代用数据源 | 本机无 USB 摄像头、无 `/dev/video*`，亦未见离线暗光集 | **未满足**（待确认数据来源） |

## 6. 待配置 / 待确认项

> 下表除 C1 的"环境已建"外**均未执行**；标注了阻塞关系与前置条件，执行前须逐项确认（`rknn-toolkit2` 必须与板上 `librknnrt` **同版本号**、`rknpu` 驱动 **≥ 下限**，约束主基准 §2.2；**在探板取得两者版本前不得定版**）。

| # | 事项 | 前置 / 约束 |
|---|---|---|
| C1 | Python 转换环境（`onnx` / `onnxsim` / `onnxruntime` / `rknn-toolkit2`） | **环境已建**（conda `rknn`，py3.8.20，§3.3）；**包体待装**。装前须先补 apt 组件（§3.6-B，需 sudo）；依赖按该版本 toolkit2 的 **`requirements_cp38-<ver>.txt` 全量安装**（py3.8 → 用 cp38 档与对应 wheel），其中 `onnx`/`onnxsim`/`onnxruntime` 不受板端约束、**可先装**；而 `rknn-toolkit2` 必须与板上 `librknnrt` **同版本号**（主基准 §2.2 / §10.1，v3.20 已由"同代"精确化）→ **其版本受 P1（探板）阻塞**。**opset 基线 12 见主基准 §10.2** |
| C1b | apt 组件补齐（`libprotobuf-dev` / `protobuf-compiler` / `libxslt1-dev` / `libglib2.0-dev`，§3.6-B） | **需 sudo**（本终端 `sudo -n` 失败，需密码）→ 须用户执行或提供授权 |
| C2 | `pybind11`（`docs/build.md` §4） | 版本须**锁定**并写入构建日志；ABI 与运行期 Python 一致 |
| C3 | 交叉编译链 `aarch64-linux-gnu-gcc`（`docs/build.md` §1 备选路径） | 须用与板上同代 sysroot；产物 ABI 须与板上 Python 一致 → 受 P1 阻塞。附带的 `libdrm-dev` / `libv4l-dev` / `v4l-utils` / `libgbm-dev`（§3.6-C）同受阻塞 |
| C4 | GPU 算力补齐（§3.4） | **修正后**：驱动已在，只差 ① `nvidia-utils-535`（apt，需 sudo）② CUDA 版 torch（pip，可装进专用环境）。若 M1 fine-tune 需要 GPU 才需做，否则可缓 |
| C5 | pip 镜像（§3.3） | 可选，仅影响下载速度；PD 直连已 200 |
| C6 | git 身份（§4） | 需用户提供 `user.name` / `user.email` |
| C7 | `~/.ssh/config` 主机别名 `board` / `board-ops` 的 `BOARD_SSH_*`（§4） | 受 P1 阻塞（板不可达，配了也无从验证） |
| C8 | 代用数据源接入（§5 P4） | 需确认用 USB 相机（本机无）还是既有离线暗光集 |

## 7. 采集元信息（可复现命令）

采集时点：**① 2026-09-17 16:54 CST**（硬件 + 软件环境基线）、**② 2026-09-17 17:0x CST**（系统组件与驱动审计复采 —— §3.2 的 cmake/ninja 来源、§3.3 的 `rknn` 环境、§3.4 GPU 修正、§3.6 组件表均出自该次）。
采集方式：**只读探测**（规则 `30-board-safety.md` §4 口径）。**唯一的写入动作**是新建 conda 环境 `rknn`（`conda create -n rknn python=3.8 -y`），未安装任何系统软件包、未改任何系统配置。

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
