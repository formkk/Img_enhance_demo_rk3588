# 证据：M0-2 工具链版本 A/B（PC 端 rknn-toolkit2 定版）

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：一次实验（A/B）的原始输出与判定留档，对应 `docs/plans/M0.md` §3 的 **M0-2** 前置子项 |
| 效力 | **非验收依据**（规则 `00-ssot.md` §3.5）；验收只认主基准 §13 复现方式 + §11.2 回归项 + `bench.py` 报告 |
| 对应主基准节 | §2.2（NPU 与运行时版本纪律）、§10.1 / §10.2（软件栈与转换流程）、§10.4（`runtime.*` 三元组）、§11.1-2、§11.2 M0-①、§15（O7 代用数据） |
| 时点 | **2026-09-17 18:4x–19:0x CST**（PC 侧转换 + 板端探测） |
| 状态 | `draft` |

> 本档只记录实测事实与由其得出的判定。**所有历史数字（预研 3562 的 4.9 s / 255 ms / 7.1 s / 39.69 dB 等）一律不作为依据或换算基础**（规则 `10-evidence.md` §2）。

## 1. 判据

- **§2.2 版本纪律**：`rknn-toolkit2` 与板上 `librknnrt` **必须同版本号**（不同版报 `Invalid RKNN model version`）；`rknpu` 内核驱动只需 ≥ 0.9.2。
- **§10.2 转换流程**：`torch.onnx.export`(opset 12、固定 shape) → `onnxsim` → PT 对齐 <1e-4 → `rknn.config(target_platform='rk3588')` → `rknn.build(do_quantization=False)`（FP16 正式）→ 板上输出 vs PT 对齐。
- **§4.3 / O7**：M0 用代用数据（本轮 = 预研归档中的真实暗场景帧 `18.png`）。
- A/B 的目的：**用实测定 PC 端 toolkit2 版本**（阻塞 M0-2 的模型转换与测速）。

## 2. 三臂定义（同一 ONNX、同一 shape，仅换工具链）

| 臂 | PC toolkit2 | 板上 librknnrt | 板上 lite2 | 来源 |
|---|---|---|---|---|
| **A** | **1.5.0**（`1.5.0+1fa95b5c`） | 1.5.0（板上镜像预装，**零改动**） | 1.5.0 cp39 | GitHub `airockchip/rknn-toolkit2` tag `v1.5.0`（仓库树内 wheel） |
| **B** | **2.3.2**（PyPI 正式版） | 2.3.2 | 2.3.2 cp39 | PyPI（toolkit2 与 lite2 均有正式 wheel） |
| **C** | **2.4.2a12**（alpha） | 2.4.2a2 | 2.3.2 cp39（借 B 臂；C 无独立 lite2 可得） | 预研归档 `2.4.2/a12/**` + 其 `versions.txt` 声明的匹配三元组 |

- C 臂的 `librknnrt 2.4.2a2` 与 wheel 同级来自预研归档（离线可得），`versions.txt` 明确 toolkit2/rknn_server/librknnrt **必须同版**。
- **A 臂依赖为精确旧 pin**（wheel `Requires-Dist` 实测）：`torch==1.10.1`、`numpy==1.19.5`、`onnx==1.10.0`、`protobuf==3.12.2`、`tensorflow==2.6.2`、`scipy==1.5.4`、`onnxruntime==1.10.0`……（B/C 两臂为宽松 pin：`numpy≤1.26.4`、`torch 1.10.1–2.4.0` 等，见 §10.2）。

## 3. 环境与固定输入

**转换环境（PC，三套独立 conda 环境，py3.8.20）**：`rknn150` / `rknn232` / `rknn242`，路径 `/home/jenkolux/.conda/envs/`。
依赖按各版 requirements / wheel `Requires-Dist` 安装；`torch` 取 **CPU 版 2.0.1**（在允许区间内，避免三套 CUDA 版重复占 2 GiB/环境）；`setuptools 75.1.0`（< 81 ✓）；`rknn.api` 导入实测通过。

> **环境隔离发现（必须随行登记）**：本机存在**用户级 site-packages**（`~/.local/lib/python3.8/site-packages`，实测 **244 个包**，含 torch 2.0.1 CUDA 版、transformers、jupyter 等）。conda 环境默认 `ENABLE_USER_SITE=True`，会导致 ① `pip list` 看到**不属于该环境**的包、② pip 把 `torch` 判为"已满足"而跳过安装。**本轮所有 A/B 命令一律加 `PYTHONNOUSERSITE=1`**；否则环境不可复现。已回写至 `environment/dev-host-30.md` §3.3。

**固定代用输入**（确定性、可复算）：
- 源帧：`docs/evidence/reports/M0-2/input_18_1080x720.png`（sha256 `6156690b…6efc9`）
- 预处理：`cv2.resize` 到目标档位 → BGR→RGB → float32 **[0,255]** → `[1,3,H,W]`
- 板上喂入：由探测脚本按运行时约定转 **NHWC**（模型声明的输入 layout = NHWC）
- PyTorch 参考：同一张量 **÷255** 后过 `CurveEstimator`（板端 `.rknn` 内 mean/std 已完成该归一化）

**权重**：`models/zde/Epoch99.pth`（官方 Zero-DCE++，sha256 `ca8855b9…f3b84`）。
⚠️ 预研方的**微调权重（best/final.pth）不在归档内**（详见 `environment/pre-research-inventory.md` §4），本轮 A/B 用官方权重——A/B 的变量是工具链版本，与权重无关。

## 4. 复现命令

```bash
# ① ONNX 导出（任一转换环境内，需 torch/onnx/onnxsim）
PYTHONNOUSERSITE=1 python3 tools/convert_models.py export \
  --weights models/zde/Epoch99.pth --h 272 --w 480 \
  --out models/zde/zde_curve_480x272.onnx \
  --input-png docs/evidence/reports/M0-2/input_18_1080x720.png \
  --dump-input docs/evidence/reports/M0-2/input_480x272.npy \
  --dump-pt-ref docs/evidence/reports/M0-2/pt_ref_480x272.npy \
  --report docs/evidence/reports/M0-2/export_onnx.json

# ② 各臂 ONNX → RKNN（在对应 conda 环境内；A/B/C 各一次）
PYTHONNOUSERSITE=1 /home/jenkolux/.conda/envs/rknn232/bin/python tools/convert_models.py build \
  --onnx models/zde/zde_curve_480x272.onnx \
  --out models/zde/zde_curve_480x272_fp16_tk232.rknn --quant fp16 --target rk3588 \
  --report docs/evidence/reports/M0-2/build_B_tk232.json   # envs: rknn150 / rknn232 / rknn242

# ③ 板端探测（板侧脚本已置于 ~/rknn_ab/；见 §6 的命名空间方案）
ssh board 'cd ~/rknn_ab && ./run_arm.sh arm232 232 zde_curve_480x272_fp16_tk232.rknn B_tk232'
```

## 5. 结果

### 5.1 转换（PC）

| 臂 | 转换结果 | 构建警告数 | Total Internal Memory | Total Weight Memory | `.rknn` 大小 |
|---|---|---|---|---|---|
| A 1.5.0 | 成功 | **0** | 63,452,160 B | 24,832 B | 2,691,006 B |
| B 2.3.2 | 成功 | **0** | 61,965 KB | 24.25 KB | 1,908,277 B |
| C 2.4.2a12 | 成功 | **0** | 61,965 KB | 24.25 KB | 1,908,280 B |

- **三臂均无 `Unknown op target` / opset 警告**（B/C 的 61.9 MB 内部内存与权重内存一致；A 稍大）。
- B 与 C 的 `.rknn` 大小差 **3 字节**，印证"`.rknn` 非 byte-reproducible、不得用哈希判定一致性"（§10.2）。
- ONNXPyTorch 对齐（FP32）：**max abs = 2.24e-7**（`export_onnx.json`），四档 shape（256×144 / 480×272 / 960×544）均 <1e-4 ✓。

### 5.2 板端（LubanCat-5 V2 / RK3588 / rknpu 0.9.8）

**加载方式（无 sudo、可一键回退）**：板端不支持 `LD_LIBRARY_PATH` 与 `LIBRKNNRT_PATH` 覆盖——lite2 的扩展**按绝对路径 `/usr/lib/librknnrt.so` 加载**（`readelf -d` 无 `librknnrt` 的 NEEDED 项；`strings` 中该路径硬编码）。故改用**非特权挂载命名空间**：

```sh
unshare -Ur -m sh -c 'mount --bind ~/rknn_ab/lib/arm232/librknnrt.so /usr/lib/librknnrt.so && <run probe>'
```

- 命名空间内实测 `strings /usr/lib/librknnrt.so` = 目标版本；**命名空间外仍为 1.5.0**（系统文件未被改动）。

| 臂 | 实际加载运行时（`/proc/self/maps` 实测） | 输出 shape | 曲线范围 | 对齐 max abs（vs PT） | 推理中位 / p90（20 次） |
|---|---|---|---|---|---|
| A | **1.5.0** `(e6fe0c678@2023-05-25)` | [1,3,272,480] | −0.35229 ~ −0.05759 | **5.42e-4** | 67.6 / 126.5 ms |
| B | **2.3.2** `(429f97ae6b@2025-04-09)` | [1,3,272,480] | −0.35205 ~ −0.05759 | **4.91e-4** | 78.0 / 96.3 ms |
| C | **2.4.2a2** `(047a9211e0@2026-07-31)` | [1,3,272,480] | −0.35205 ~ −0.05759 | **4.91e-4** | 63.8 / 80.5 ms |

> 交错复测（每臂 50 次、两轮）显示单臂内 median 波动 63.8–78.0 ms、单次 min 50 / max 127 ms（NPU governor = `rknpu_ondemand`，1.0 GHz）。**三臂耗时差异落在噪声量级内，不能用于版本判定。**

**三档耗时（B 臂，30 次）**：

| 档位 | 输出 shape | 中位 | min | p90 | 对齐 max abs |
|---|---|---|---|---|---|
| 256×144 | [1,3,144,256] | **27.7 ms** | 23.2 | 31.1 | 4.60e-4 |
| 480×272（默认档） | [1,3,272,480] | **70.9 ms** | 65.2 | 100.4 | 4.91e-4 |
| 960×544 | [1,3,544,960] | **225.2 ms** | 220.6 | 231.6 | 5.48e-4 |

耗时与像素数近似线性（256×144→480×272 像素 ×3.54、耗时 ×2.56；480×272→960×544 像素 ×4、耗时 ×3.18），存在约 10 ms 量级的固定开销。

**反例留档（版本纪律的实测确认）**：当板端仍为系统 `librknnrt 1.5.0` 而模型由 2.3.2/2.4.2a12 转出时，实测报

```
E RKNN: Invalid RKNN model version 6
E RKNN: rknn_init, load model failed!   →  RKNN_ERR_MODEL_INVALID
I RKNN: RKNN Runtime Information: librknnrt version: 1.5.0
```

与 §2.2 / R7 的预言一致。

## 6. 判定

1. **三臂对该模型（ZDE++ 曲线估计，纯 Conv+Concat+Tanh，FP16）均可转、可加载、可推理**；B 与 C 输出逐元素差异 0（`−0.35205~−0.05759` 完全一致，对齐值亦相同 4.91e-4），A 略差（5.42e-4）。
2. **耗时不能区分版本**（差异在噪声内，见 §5.2）。
3. **C 臂不具备作为工程基线的条件**：`2.4.2a12` 为 **alpha**，且板端**没有与之配套的 lite2 wheel**（本轮只能借用 2.3.2 的 lite2），依赖链不闭合。
4. **A 臂不具备作为工程基线的条件**：① 依赖为精确旧 pin（含 `tensorflow==2.6.2`、`numpy==1.19.5`、`onnx==1.10.0`），环境脆弱且与 §10.2 的 pin 口径冲突；② 编译产物更大（内部内存 +2.4%、`.rknn` +41%）；③ HQ（Retinexformer，含注意力算子）的算子支持下限高于 1.5.0，**M0-4/M0-5 必然需要更高版本**，一板只能有一套 `librknnrt`，故 A 不可能是终态。
5. **定版建议：B = `rknn-toolkit2 2.3.2` + `librknnrt 2.3.2`**（PC 与板端同版本号）。依据：正式发布（非 alpha）、PyPI 上 wheel 与 **cp39 lite2** 齐备、依赖 pin 宽松且与 §10.2 一致、对 ZDE++ 零警告、对齐与 C 臂等同；并与 §10.2 已登记的"野火实测基线 = 2.3.2"一致。
   **该定版以 M0-4（HQ R9 机械闸口）的编译结果为确认门**：若 2.3.2 无法编译 HQ，则回退 C（2.4.2a12 + 2.4.2a2）并回写本节。

## 7. 偏差与待裁定（不得在本档内自行放宽）

1. **§11.2 M0-① 的阈 <1e-4 在板端 FP16 上不可达**：三臂实测 **4.60e-4 ~ 5.48e-4**。差异来自 **FP16 输出精度**（曲线值 ~0.35，FP16 相对精度 ~2⁻¹¹ → 单项 ~1.7e-4，经 19 个算子累积至 ~5e-4），与工具链版本无关（B/C 完全相同）。
   - ONNX↔PyTorch（FP32）侧仍严格 <1e-4 ✓（2.24e-7）。
   - **处置建议（须走 §15 裁定后回写 §11.2 / §13，不得默认放宽）**：把 §11.2 M0-① 的口径拆为两层——①a ONNX↔PT 张量对齐 <1e-4（FP32 链路）；①b 板上 FP16 输出 vs PT：**张量阈按 FP16 量级给出（建议 ≤1e-3）并另附图像域指标**（8 次迭代后的增强图 PSNR / 最大像素差），因为曲线场的 5e-4 误差经曲线应用后在图像域被压缩。
2. **耗时与预算冲突**：主基准 §7.1 的"推理 ≤10 ms"（沿用预研早期预期）与本轮实测（480×272 = 70.9 ms）相差约 7 倍；§4.3 的估计频率（5~15 Hz）在默认档下 **上限约 14 Hz**（70.9 ms/次），无余量留给同核并发。
   - **处置建议（须走 §15 裁定）**：① §7.1 预算表按实测重排（或明确该行只适用于某档位）；② 明确默认档是否由 480×272 下调至 256×144（27.7 ms，可支撑 ~30 Hz）；③ 若要保留 480×272，须在 M2 落实"估计频率下降 + EMA 时间常数联动"（§3.1 已有联动口径）。

## 8. 本轮未纳入（后续项）

1. **板端 `librknnrt` 的正式升级**（1.5.0 → 2.3.2）为**有副作用操作**：需备份 `/usr/lib/librknnrt.so`、记录恢复步骤并取得授权（规则 `30-board-safety.md` §4）；本轮**未执行**，A/B 全程用命名空间隔离，板端系统文件未变。
2. HQ（Retinexformer）在 2.3.2 下的可编译性 → **M0-4**。
3. 三档 shape 的完整登记（缓存占用 / U8 闸口 / `pipeline.curve_field_precision`）→ M0-2 本体（`docs/measurements.md` §3 / O23）。
4. INT8 复测（M0-8）。

## 9. 产物清单（工程目录内，不进 `storage.dir`）

| 产物 | 路径 |
|---|---|
| 转换报告（A/B/C + 导出） | `docs/evidence/reports/M0-2/{export_onnx,build_A_tk150,build_B_tk232,build_C_tk242}.json` |
| 构建日志（原始 warning 计数依据） | `docs/evidence/reports/M0-2/build_{A,B,C}_*.log` |
| 固定代用输入 / PT 参考 / 板端输出 | `docs/evidence/reports/M0-2/{input,pt_ref}_*.npy`（板端输出 `out_*.npy` 已回拉） |
| 板端报告 | `docs/evidence/reports/M0-2/probe_*.json`（含 `librknnrt_loaded` 实测路径与版本） |
| 模型产物 | `models/zde/zde_curve_480x272_fp16_tk{150,232,242}.rknn`（A/B/C 三臂）+ `models/zde/zde_curve_{256x144,960x544}_fp16_tk232.rknn`（B 臂三档测速；权重本体不入库）；`.rknn` 按 §10.2 不作哈希一致性判定，哈希仅登记于转换报告 |
| 板端临时区（可一键删除） | 板 `~/rknn_ab/`（`rm -rf` 即完全回退） |
---

## 10. 口径更正（v3.22 追加）：本文 §5.2 的耗时数字只作 python 原型参考

上表的三档耗时与三臂耗时均由 **`tools/rknn_probe.py`（`rknnlite`/python 路径）** 测得。后续用 **C API**（`tools/rknn_c_probe.c`）剖析发现：该 python 路径自带 **~20–26 ms 的固定 IO 开销**（`wall − NPU run_duration`），且会把 INT8 的真实收益掩盖掉（详见 `docs/evidence/M0-8-int8-retry.md` §7）。

- **不得用本文的耗时数字做预算**（§7.1 预算与 O28 的判定改以 C-API 数字为准）；
- **仍然有效的结论**：三臂的**加载能力 / 输出对齐值 / 运行时版本实证**（这些与测量路径无关）；
- 三档的**相对关系**仍成立（近似线性、256×144 显著更快），但绝对值的参考基准应为 C-API 口径。

## 11. 确认门结论（v3.23 补记，2026-09-17）

本文 §6 把 **toolkit2 2.3.2 的定版**挂了一个**确认门**："若 2.3.2 无法编译 HQ（注意力算子），则回退 2.4.2a12"。该门在 **M0-4 的首个 HQ 编译**中打开并**已通过**：

| 项 | 实测 |
|---|---|
| 模型 | Retinexformer（`refs/pre-research/basicsr/models/archs/RetinexFormer_arch.py` importlib 直载；`n_feat=40` / `stage=1` / `num_blocks=[1,2,2]`），**1,605,701 参数**，权重 `LOL_v1.pth`（零 missing / unexpected） |
| ONNX | opset 12、固定输入 512×288；**ONNX↔PyTorch（FP32）对齐 3.16e-6 < 1e-4 ✓**（报告 `docs/evidence/reports/M0-4/export_hq_512x288_lolv1.json`） |
| 编译 | `rknn-toolkit2 2.3.2`，`target_platform=rk3588`，`do_quantization=False` → **成功**，产物 `models/hq/hq_512x288_fp16_tk232.rknn`（21,132,982 B） |
| 算子落点 | 构建日志含 **`exMatMul` / `exNorm` / `exSoftmax`**（注意力路径未被拒） |
| 内存 | 内部内存 **366,048 KB** / 权重 **3,209.94 KB**（日志 `Total Internal Memory Size`） |
| 判定 | **2.3.2 通过确认门 ⇒ 不启用 2.4.2a12 回退路径**（§2.2 已回写） |

⚠️ **该结论只覆盖"能否编译"**：M0-4 的其余交付（三档 R9 机械闸口、三档耗时、保底 CNN 上板）**本轮未完成**，按用户 2026-09-17 指示转为"占位"（见 `docs/plans/M0.md` §3-4/-12 与主基准 §4.1 的 v3.23 补注）。**不得由本节推断 HQ 的数值精度或耗时已达标。**
