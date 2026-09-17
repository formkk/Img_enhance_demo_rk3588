# 前置预研资产盘点（RK3562 预研归档 `refs/Img_Enchance_Demo_explore.zip`）

| 项 | 内容 |
|---|---|
| 地位 | 本仓库的**环境记录**（规则 `00-ssot.md` §2 的 C 层记录类），承载**前置预研（RK3562 阶段）归档**中可用资产的判定与落位 |
| 效力 | **非验收依据**；且本档所列**一切预研实测数字均为历史数据，禁止作为依据或换算基础**（规则 `10-evidence.md` §2、主基准附录 A/C） |
| 对应主基准节 | §2.2（版本纪律）、§4.1 / §4.3（模型与档位）、§10.1 / §10.2（软件栈与转换流程）、§11.1-2 / -8、§15（O7 代用数据） |
| 时点 | 盘点 **2026-09-17 18:3x–19:0x CST**（只读解包 + 实测校验，未改动归档与被引用对象） |
| 状态 | `draft` |

> 预研方（另一台 PC：`192.168.0.147`，conda `rk_demo`；板：RK3562 `192.168.0.114`）已完成"M0 环境 + 模型迁移"级验证，但**其基准文档、方案稿与全部实测数字仅作历史留档**；本档只判定**资产可用性**与**可复用面**。

## 1. 归档概况

| 项 | 实测 |
|---|---|
| 归档 | `refs/Img_Enchance_Demo_explore.zip`（3.73 GiB，4012 条目；含 `.git`（2 commit）与 Windows 版 `.venv`——**后者无用**） |
| 打包性质 | **工作目录打包**（非 `git archive`）：`.gitignore` 排除的 `pretrain_model/`、`m0_results/`、`2.4.2/`、`lolv2/` 均在包内 |
| 解包落位 | `refs/pre-research/`（`refs/` 已被 `.gitignore` 排除；**入包路径按归档原结构保留以便溯源**）；工具链另有 `refs/pre-research/toolchain/` |
| 文件名编码 | 归档内中文名为 **GBK**（zip 无 UTF-8 标志），解包后须 `cp437→gbk` 修正（本档所列路径均已修正后落位） |

## 2. 可直接可用（已纳入工程）

| # | 资产 | 归档内路径 | 落位 / 用法 |
|---|---|---|---|
| 1 | **Zero-DCE++ 官方权重** `Epoch99.pth`（52,395 B） | `zde/extracted/Zero-DCE_extension-main/Zero-DCE++/snapshots_Zero_DCE++/` | → `models/zde/Epoch99.pth`（sha256 `ca8855b9…f3b84`）；本轮 A/B 的转换输入 |
| 2 | **Zero-DCE++ 官方源码**（`model.py` / `Myloss.py` / `lowlight_train.py` / `dataloader.py` + 官方 train/test 数据 2066 文件） | 同上目录 | 架构定义已按官方逐行落为 `tools/zde_model.py`（另加只导出曲线估计的 `CurveEstimator` 包装） |
| 3 | **Retinexformer 上游预训练权重 ×9**（1.606 M 参数，实测键 `body.0.denoiser.*`） | `pretrain_model/`（`LOL_v1` / `LOL_v2_real` / `LOL_v2_synthetic` / `SID` / `SMID` / `SDSD_indoor` / `SDSD_outdoor` / `FiveK` / `NTIRE`） | HQ 链路候选权重（§10.2 / §11.1-4/-5 的输入）；**与归档内 `evaluation` 报告所用的同一批** |
| 4 | **Retinexformer 架构文件** | `basicsr/models/archs/RetinexFormer_arch.py` | HQ 转换用；预研 rules 另记"用 `importlib.util` 直载该文件、绕开 `basicsr/__init__.py` 全量导入"的可行做法（省去 lmdb/torchvision 依赖链） |
| 5 | **toolkit2 2.4.2a12 完整三元组**（离线） | `2.4.2/a12/…`：cp38/cp310/cp312 wheel + `librknnrt.so`（实测 `2.4.2a2 (047a9211e0@2026-07-31)`）+ `rknn_server`(2.4.0) + `versions.txt` | A/B **C 臂**（`docs/evidence/M0-2-ab-toolkit-version.md`）；亦为将来板端升级的成对包 |
| 6 | **librknnrt 2.3.2（aarch64）** + lite2 2.3.2 板端包 | `m0_results/librknnrt_v232.so`（实测 `2.3.2 (429f97ae6b@2025-04-09)`）、`rknn_toolkit_lite2-2.3.2-cp38-aarch64.whl` | B 臂；⚠️ 归档内 lite2 为 **cp38**，与本板 python **3.9.2** 不符 → 已改用 PyPI 的 cp39 正式 wheel |
| 7 | **LOLv2 数据集**（1,047,872,805 B） | `lolv2/LOLv2.zip` | **实测完整**：3593 条目、`testzip → None`；结构 `LOLv2/{Real_captured,Synthetic}/{Train,Test}/{Low,Normal}`。用途：O7 代用数据源（M0-3 / M0-8 校准与评测） |
| 8 | **真实暗场景测试素材** | `m0_results/18.png`（1080×720，均值 8.42/255）、`m0_results/15.ARW`（Sony RAW）、`06.CR2`（21 MB）、`07.DNG` | 本轮 A/B 的**确定性代用输入**即取自 `18.png`；其余可作 O7 代用数据与 RAW 域评估素材 |
| 9 | **转换/评测脚本范式**（参考实现，**不入工程源码树**） | `m0_zde_export_onnx.py`、`m0_zde_convert_rknn.py`、`m0_zde_tile_infer.py`、`m0_zde_realtime.py`、`batch_eval_lolv2.py`、`img_stats.py`、`eval_06cr2_zde/*.py` 等 | 仅作参考；工程侧改写为 `tools/convert_models.py` / `tools/rknn_probe.py`（去 `sys.path` 硬编码、target 改 `rk3588`、shape 可配） |

## 3. 方法/结论可借鉴，但**不得作为证据引用**

| 项 | 内容 | 对本项目的用途 |
|---|---|---|
| INT8 塌缩 | `tanh` 输出在小模型上被对称量化压到近零（ZDE++ 曲线参数全塌缩）；Retinexformer 在极低亮度区同样归零 | 与 §4.2"INT8 不进入主线、M0-8 仅作实验"的判断方向一致（**仅旁证**） |
| Dense tile 等价性 | 512×512 重叠 tile（overlap ≥16 px）+ 距离权重融合，与 PT 全分辨率推理"像素级等价"（理由：ZDE++ 感受野 ~15 px） | 支持 §3.1 的 RGA 零填充 / 曲线场上采样策略的先验判断（**须在 3588 自行复测**） |
| Retinexformer 的 RKNN FP16 残影 | 单 tile、零拼接、同输入下 PT 正常而 RKNN FP16 有多道水平/垂直重影；去掉 RGB2BGR 后输出逐像素一致 ⇒ 判为编译器算子级（`exSoftmax`/`exNorm`/`exMatMul`）数值偏差，非代码/驱动 bug | 对应 R9（HQ 重影）：M0-4 的机械闸口须自行复测，**不得以预研结论免检** |
| 驱动缺陷 | rknpu **0.9.3** 下大 tensor `failed to submit`（社区同款 bug，0.9.6 起修复） | 本板为 **0.9.8**，满足 ≥0.9.2 下限，不受此影响 |
| 8 次迭代曲线数学 | `x ← x + x_r·(x²−x)` 共 8 次（4+4 分组），末尾 `clip(0,1)` | 与 §4.1 / §3.3 一致；工程实现须**单遍融合**（不得照搬其 numpy 逐迭代全帧写法） |
| 微调配方 | `m0_zde_finetune.py`（256 px / L1 / 15 ep）→ `_v2`（512 px / L1+0.2·SSIM / ReduceLROnPlateau / 早停）；`m0_zde_widen_init.py` 的 Net2Net 宽度外推（nf32→96，含 1% 噪声破对称）+ `m0_zde_train_wider.py` | §4.1 已把"LOL-v2 fine-tune v2"列为**候选起点**；配方可用于本项目实拍域微调（M1/M2） |
| 其"扩参无收益"结论 | nf96 在 6 次实验中未稳定超过 nf32 | 省去重复试错；**仍属历史结论，若要写入设计须自行复测** |
| 实时链路脚手架 | USB 摄像头 → RKNNLite → 写 `/dev/fb0` 并排对比（含 `/dev/fb0` 32bpp 直写、`VideoWriter` 尺寸坑） | **仅原型参考**：本项目主路径为 DRM 双 CRTC + RGA + DMABUF lease，且**禁止 CPU 全帧混合** |

## 4. 不可用 / 需剔除

| 项 | 原因 |
|---|---|
| **微调后的 ZDE++ 权重（`best.pth` / `final.pth`，nf32 v1/v2 及 nf96）** | **不在归档内**。依据：① 打包为工作目录、`.gitignore` 排除的 `*.pth` 亦被带出（`pretrain_model/*.pth` 在包内），故若权重在仓库里就会被带出；② 全包 `.pth` 仅两类（官方 `Epoch99.pth` 与 Retinexformer 上游 9 个）；③ `m0_results/zde_512x512_fp16.rknn` 时间戳 **08-12 13:24** 早于微调脚本（08-12 16:57 / 08-13 11:03）与 `results_ft.json`（08-13 10:59），且预研基准 §12.8 明写该 rknn 由 `Epoch99.pth` 转出 ⇒ **现有两个 rknn 均为官方权重产物，不含微调权重**。补救：向预研方索取（其 `out_dir`），或用归档内脚本 + LOLv2（已验完整）自行复现 |
| `m0_results/*.rknn`（ZDE++ / Retinexformer 各档） | 均由 `target_platform='rk3562'` 转出（实测 `zde_512x512_fp16.rknn` 内嵌 `target_platform:["rk3562"]`、`version: 2.4.2a12`、输入固定 512×512）⇒ **不可用于 3588**，且与 §10.4 的档位口径不符；须按 `rk3588` 重转 |
| `2.4.2/` 下 `a1…a11` 各 `.tgz`（合计 **1.92 GiB**） | alpha 冗余，仅 `a12` 的 wheel + runtime 有价值 |
| `.venv/`（Windows 版）、`archive/*.md`（ds / grok / qwen 方案稿）、预研基准文档本体 | 前者无用；中者仅存档、**不得作为实施依据**；后者选型（IMX462/IMX327S、960×540、Ubuntu 20.04、四线程）与本项目（**IMX415、1080p、RGA 480×272、Debian 11**）不同，**不可套用** |
| 全部预研实测数字 | 39.69 dB / 17.96 dB / 7.1 s / 4.9 s / 255 ms / 270 ms / 75.24 dB 等一律**禁止引用**（附录 A/C、规则 `10-evidence.md` §2） |

## 5. 与本项目的口径差异（避免误套用）

| 维度 | 预研（归档） | 本项目（主基准） |
|---|---|---|
| 板 / NPU | RK3562（1 TOPS 单核，`rknn_set_core_mask` 不可用） | **RK3588**（三核，分核：core0 实时 / core1+2 关键帧） |
| 传感器 | IMX462 / IMX327S | **IMX415** |
| 实时链路分辨率链 | 单一 1080p 链，推理 960×540（曲线不做降分辨率） | **曲线估计档 256×144 / 480×272 / 960×544**，应用域 1080×608 |
| 采集与显示 | USB 摄像头 + `/dev/fb0` | **MIPI rkisp mainpath + DMABUF lease + DRM 双 CRTC** |
| 板端运行时 | librknnrt 2.3.2（其后又装 2.4.2a2）+ lite2 2.3.2 | 本板出厂 **1.5.0**；定版见 `M0-2` 证据 |
| 关键帧链路 | 分时抢占（单核） | 分核并行（core1+2）+ MIPI 弹层降级 |

## 6. 回写义务（本次盘点触发）

- 主基准 §10.3（`refs/pre-research/` 与 `environment/pre-research-inventory.md` 登记）、§14（本版变更记录）
- `environment/dev-host-30.md` §3.3（用户级 site-packages 污染与 `PYTHONNOUSERSITE=1` 口径）
- `docs/plans/M0.md`（P3 的模型与工具链来源、B1 阻塞状态）