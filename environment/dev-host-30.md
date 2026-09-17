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

## 2. 硬件清单（采集时点：2026-09-17 16:54 CST）

| 类别 | 实测值 |
|---|---|
| 整机 | ASUS `All Series`（主机名 `jenkolux-All-Series`），主板 ASUSTeK **B85-PRO GAMER**，BIOS 2203（2015-04-27） |
| CPU | Intel **Core i7-4790 @ 3.60 GHz**，4 核 8 线程（2 线程/核），L3 8 MiB，x86_64 |
| 内存 | **15 GiB** 物理（采集时空闲 ≈10 GiB，可用 ≈13 GiB），Swap 2 GiB（未使用） |
| GPU | NVIDIA **GA102**（PCI `10de:2204`）—— **未安装驱动**（`nvidia-smi` 不存在）；详见 §3.4 |
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
| gcc / g++ | **9.4.0**（Ubuntu 9.4.0-1ubuntu1~20.04.2） | C11 + C++17 支持满足 `docs/build.md` §1 的语言标准要求在**本机**成立 |
| cmake | **3.27.1** | 高于 `docs/build.md` §2 的 `[待复核]` 下限 |
| make | GNU Make 4.2.1 | — |
| ninja | 1.10.2 | — |
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
| 已有 conda 环境 | `base`(3.11.3, 当前激活)、`hugging`、`llama`、`qwen` |
| 扩展后缀 | `python3-config --extension-suffix` = **`.cpython-311-x86_64-linux-gnu.so`**（x86_64，**与目标 aarch64 不一致**） |
| pip | 23.1.2（conda base）；系统 pip 20.0.2 |
| pip 镜像 | **未配置**（无 `~/.pip/pip.conf`、`~/.config/pip/pip.conf`、`/etc/pip.conf`） |
| apt 源 | `cn.archive.ubuntu.com`（focal 全套） |

**本项目解释器口径（本档固化）**：板上与开发侧工具链**一律以系统 `python3` 为准**——依据主基准 §10.1「镜像自带 `python3`」与 `docs/build.md` §1 / §4（pybind11 扩展必须与**运行期 Python 同 ABI**；上表"扩展后缀"行的 conda `.cpython-311-x86_64-linux-gnu.so` 与目标 aarch64 及板上 Python 均不一致，用 conda 解释器做构建会引入 ABI 偏差）。**推论**：`board-ops` MCP 与后续工程脚本的解释器**写绝对路径 `/usr/bin/python3`**，不依赖 PATH 解析——**IDE 以 GUI 会话启动子进程时不读 `~/.bashrc`**，PATH 解析结果不可控（`conda init` 只影响交互式 shell）。

**conda base 已装（与本项目相关的子集）**：numpy 1.24.3、torch **2.0.1（CPU 版，`torch.cuda.is_available()=False`）**、scipy 1.10.1、pillow 9.4.0、matplotlib 3.7.1、pyyaml 6.0、pytest 7.3.1、ruamel.yaml 0.17.21、tqdm 4.65.0、pandas 1.5.3。

**核验为缺失（逐环境查过 `base` / `hugging` / `llama` / `qwen` / `/usr/bin/python3`，五处全无）**：

| 缺失包 | 关联 | 影响 |
|---|---|---|
| `onnx` / `onnxsim` | §10.2 步骤 1–2 | ONNX 导出与简化链**无法执行** |
| `rknn-toolkit2` | §10.1 PC 端转换、§10.2 步骤 3–4 | RKNN 转换**无法执行** |
| `onnxruntime` | 对齐校验 | ONNX 侧推理校验不可用 |
| `pybind11` | `docs/build.md` §4 | 扩展模块构建不可用 |
| `python-drm` / `evdev` | §10.1 显示 / 触控 | 板上路径，本机非必需 |

### 3.4 GPU 与深度学习算力

| 项 | 实测值 |
|---|---|
| 硬件 | NVIDIA GA102（`10de:2204`）已插在 `01:00.0` |
| 驱动 | **未安装**（`nvidia-smi` 不存在，无 `nvcc`） |
| torch | 2.0.1 **CPU 版**（`torch.version.cuda = None`） |
| 结论 | 本机当前**无可用 GPU 算力**；若 M1 的实拍域 fine-tune 需 GPU，须先装驱动（**待确认项，见 §6**） |

### 3.5 网络可达性（只读探测）

| 目标 | 结果 |
|---|---|
| `pypi.org/simple/` | HTTP **200**，1.72 s |
| `pypi.tuna.tsinghua.edu.cn/simple/` | HTTP **200**，可达 |
| 演示板 `192.168.0.104` | **不可达**：2 包全丢，`/dev/tcp/192.168.0.104/22` 报"没有到主机的路由" |

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
| P3 模型与工具链 | `onnx` / `onnxsim` / `rknn-toolkit2` / `pybind11` 全缺（§3.3） | **未满足** —— 本档主要缺口 |
| P4 代用数据源 | 本机无 USB 摄像头、无 `/dev/video*`，亦未见离线暗光集 | **未满足**（待确认数据来源） |

## 6. 待配置 / 待确认项

> 下列各项目前**均未执行**；标注了阻塞关系与前置条件，执行前须逐项确认（`rknn-toolkit2` 的版本受 §10.1"与板上同代"纪律约束，**在板型与板上版本三元组未探测前不得定版**）。

| # | 事项 | 前置 / 约束 |
|---|---|---|
| C1 | Python 转换环境（`onnx` / `onnxsim` / `onnxruntime` / `rknn-toolkit2`） | 须先确定：装进 conda base 还是新建专用环境；且 `rknn-toolkit2` 版本须与板上 `librknnrt` **同代**（§10.1）→ 受 P1 阻塞 |
| C2 | `pybind11`（`docs/build.md` §4） | 版本须**锁定**并写入构建日志；ABI 与运行期 Python 一致 |
| C3 | 交叉编译链 `aarch64-linux-gnu-gcc`（`docs/build.md` §1 备选路径） | 须用与板上同代 sysroot；产物 ABI 须与板上 Python 一致 → 受 P1 阻塞 |
| C4 | GPU 驱动（§3.4） | 若 M1 fine-tune 需 GPU 则须装；属系统级改动 |
| C5 | pip 镜像（§3.3） | 可选，仅影响下载速度；PD 直连已 200 |
| C6 | git 身份（§4） | 需用户提供 `user.name` / `user.email` |
| C7 | `~/.ssh/config` 主机别名 `board` / `board-ops` 的 `BOARD_SSH_*`（§4） | 受 P1 阻塞（板不可达，配了也无从验证） |
| C8 | 代用数据源接入（§5 P4） | 需确认用 USB 相机（本机无）还是既有离线暗光集 |

## 7. 采集元信息（可复现命令）

采集时点：**2026-09-17 16:54 CST**。采集方式：**只读探测**（规则 `30-board-safety.md` §4 口径），未做任何安装、未改任何配置。

```bash
uname -a; cat /etc/os-release
lscpu; free -h; df -h -x tmpfs -x devtmpfs; lsblk -d -o NAME,SIZE,MODEL
cat /sys/class/dmi/id/{sys_vendor,product_name,board_name,bios_version,bios_date}
lspci -nn | grep -Ei "vga|ethernet"; ip -brief addr show; ip route
for c in gcc g++ cmake make ninja git python3; do $c --version | head -1; done
python3-config --extension-suffix; python3 -c "import sys,sysconfig;print(sys.version,sysconfig.get_platform())"
python3 -m pip list            # 取 §3.3 的包版本；缺失项逐环境 import 复核
nvidia-smi; python3 -c "import torch;print(torch.__version__,torch.cuda.is_available())"
lsusb; ls /dev/video*          # 代用数据源核验
ssh -o BatchMode=yes jenkolux@192.168.0.30 'hostname; whoami'   # 免密核验
ping -c 2 -W 2 192.168.0.104; timeout 5 bash -c 'cat </dev/null >/dev/tcp/192.168.0.104/22'
```