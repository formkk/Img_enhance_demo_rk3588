# 附件规范：构建与编译（裁定 O14）

| 项 | 内容 |
|---|---|
| 地位 | 本文档是《RK3588 极暗光增强实时演示系统 —— 项目基准文档》的**从属规范**（v3.12 裁定 O14 拆出），承载主文档 §10.1 的构建与 toolchain 规格 |
| 效力 | 与主基准冲突时**以主基准为准**；本文档变更须同步主基准 §14 变更记录 |
| 主基准对应节 | §10.1（软件栈）、§10.3（代码结构）、§5（接口与数据结构契约）、§11（里程碑）、§15（O55 / O56） |
| 状态 | 标注 **`[待复核]`** 的取值属基线假设，须在 M0-1 以板上实测 toolchain 复核并记录；**不得据未复核取值做性能结论**（证据纪律同主基准 §6.5）。**（v3.66）§2/§3/§4 的"已实测"条目来自 M2 板上构建**（板 LubanCat-5，`cat@192.168.0.104`），构建命令与零告警结论见主基准 §14 v3.66 |

## 1. 目标平台与编译路径

| 项 | 取值 |
|---|---|
| 目标 | aarch64（RK3588 / RK3588S），野火官方镜像（Debian/Ubuntu，内核 5.10 系） |
| 主路径 | **板上原生编译**（M2 起用于 `realtime_apply` 的 C/NEON 与 pybind 模块） |
| 备选 | 交叉编译（aarch64-linux-gnu-gcc，版本 `[待复核]` 随镜像）；须使用与板上同代 sysroot，产物 ABI 须与板上 Python 一致 |
| 语言标准 | C11 + C++17（`[待复核]` 随镜像 toolchain 复核） |
| Python | 镜像自带 `python3`；原型期 M0~M1 为纯 Python |

## 2. 构建系统

| 项 | 取值 |
|---|---|
| 构建工具 | CMake（板上实测 **3.18.4**） |
| 构建目录 | `build/` |
| 目标产物 | **两个 pybind11 扩展模块**（**v3.66 板上实测定稿**）：① **`_realtime_apply`** —— §5 契约模块（`apply_window` / `apply_full`，C/NEON 8 iter 单遍融合）；② **`_starlight_native`** —— 含子模块 `capture` / `rga` / `rknn` / `drm` / `osd` 五个内核。**命名偏离说明**：本节原文写「`realtime_apply`」，实际产物名为 **`_realtime_apply`**（下划线前缀）——因 §5-C 的模块名 `realtime_apply` 已被同名 **Python 包装模块 `realtime_apply.py`** 占用，C 扩展须另名以免遮蔽（与 `_starlight_native` 同策） |
| 依赖 | librga（RGA）、librknnrt（NPU 运行时）、libdrm、pybind11 |
| 版本记录 | 构建时打印并落日志：toolchain 版本、CMake 版本、pybind11 版本、librknnrt 版本（后两项进入 §10.4 的 `runtime.*` 三元组校验） |

**（v3.66）板上实测构建命令**（零告警；`-j4` 约 20 s）：

```
cmake -S . -B build \
  -DPython3_EXECUTABLE=/usr/bin/python3 \
  -DPython3_INCLUDE_DIR=<pyheaders>/usr/include/python3.9 \
  -DSTARLIGHT_PY_HEADERS_ROOT=<pyheaders>/usr/include \
  -Dpybind11_DIR=$(python3 -c "import pybind11;print(pybind11.get_cmake_dir())")
cmake --build build -j4
```

**Python 开发头来源（v3.66 实测手法）**：板上**未安装** `python3-dev` 系统包；改为把对应 `.deb` **解包到用户目录**（如 `~/pyheaders/`）并以 `Python3_INCLUDE_DIR` / `STARLIGHT_PY_HEADERS_ROOT` 指向，**零系统改动**（不写系统路径、不动 apt 状态）。该手法的适用前提 = 解包头的 Python 版本与 `/usr/bin/python3` **同版**（板上 3.9.2）。

## 3. 编译选项（NEON）

| 用途 | 选项 |
|---|---|
| 基线优化 | `-O3` |
| 架构 / NEON | `-march=armv8.2-a+fp16 -mcpu=cortex-a76.cortex-a55` `[待复核]` |
| 浮点 | `-ffast-math` **允许**用于曲线应用热路径；**ONNX↔RKNN 对齐相关路径禁止**（§10.2 的 <1e-4 阈值须可解释） |
| 浮点收缩 | **必须显式 `-ffp-contract=off`（v3.66 板上实测）**——GCC 默认 `-ffp-contract=fast` 会把标量参考的 `t*t-t` 收缩为单次 FMA，导致**同一份 C 代码的 NEON 路径与标量参考差 1 字节**，使 §11.2 的"NEON vs 标量逐位一致"回归项**假性失败**。该开关是 §11.2 数值一致性检查的**前提**，不是可选优化 |
| 告警 | `-Wall -Wextra`，构建须**零告警**（新增告警即视为回归） |
| 调试 | `-g` 仅限 bench / 调试构建；演示构建不带 `-g` |

**硬门槛**：8 次迭代必须在**单遍遍历内融合**（主基准 §3.3，非优化项）。构建产物须通过 §11.2 的回归项与 §13 的 DDR 流量检查；**未融合即判未达标**，不得以"编译器会自动优化"免检。

## 4. pybind11 版本与 ABI

| 项 | 取值 |
|---|---|
| 版本 | **`2.11.1`（v3.66 板上实测定稿并锁定）**；构建日志打印实际版本 |
| ABI 一致性 | 扩展模块与运行期 Python 必须同 ABI。**（v3.66 修正）后缀取值改用 `sysconfig`**：`python3 -c "import sysconfig;print(sysconfig.get_config_var('EXT_SUFFIX'))"` —— 原文写的 `python3-config --extension-suffix` 在**板上不可用**（镜像无 `python3-config` 可执行文件）。后缀仍**不得手工命名**；板上实测值 = `.cpython-39-aarch64-linux-gnu.so` |
| 头文件来源 | pybind11 头与运行期同批；禁止头文件与运行期版本混用。**板上安装方式（v3.66）**：`pip3 install --user pybind11==2.11.1`（用户级，恢复 = `pip3 uninstall pybind11`） |
| Python 解释器 | **必须用系统 `/usr/bin/python3`**（板上 3.9.2），以保证 pybind11 的 ABI 与目标 aarch64 环境一致；**不得用 conda/venv 解释器构建**（见 §1 的"板上原生编译"） |
| 签名一致性 | 导出函数签名必须与主基准 §5 契约 C 节逐字一致（`apply_window` / `apply_full`），返回码取自 §5 契约 A 节。**（v3.66 裁定 O55）曲线场几何不在逐帧参数里传**，由 `configure_from_config` 启动期一次性注入 C 层（§5-C 注） |
| GIL | 热路径是否释放 GIL 与 TH2b 的调用方式一并 `[待复核]` 定稿 |

**回归**：每次构建后跑 U8 / FP16 两档的 `realtime_apply` 数值一致性检查（§11.2 的 ONNX↔RKNN 对齐回归子项）。

## 5. 构建验证清单（每次构建）

1. 构建零告警（**含 `-ffp-contract=off`**）；
2. `python3 -c "import _realtime_apply, _starlight_native"` 成功，模块后缀与 ABI 一致（`sysconfig` 取值，**不得手工命名**）；
3. `apply_window` / `apply_full` 签名与 §5 契约 C 节一致；
4. 版本三元组打印并落日志（§10.4 `runtime.*`）；
5. 8 iter 单遍融合检查（主基准 §11.1-13 口径）；
6. **（v3.66）NEON vs 标量逐位对账**：FP16 与 U8 两档各跑一次，**mismatch 必须为 0**（`bench.py apply-micro --impl native` 报告内 `selfcheck_neon_vs_scalar`）。板上实测 @512×288×8 iter p50 **4.234 ms**（≤ §6.1 的 20 ms）。

**运行期前置（v3.66 板上实测，属部署步骤非构建步骤）**：`drmSetMaster` 需桌面释放 ⇒ 运行前 `sudo systemctl isolate multi-user.target`，跑完**必须**恢复 `sudo systemctl isolate graphical.target`；运行需 `sudo`，并带 `PYTHONPATH=<pybind11 用户级 site-packages>`。
