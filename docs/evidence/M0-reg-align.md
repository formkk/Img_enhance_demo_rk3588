# M0 回归项①：ONNX↔RKNN 对齐回归（逐档重跑）

| 项 | 内容 |
|---|---|
| 地位 | **记录类子文档（C 层）**，承载 §11.2 的 M0-① 回归项「ONNX↔RKNN 对齐回归」的逐档重跑结果 |
| 效力 | **非验收依据**：验收只认主基准 §13 的复现方式 + §11.2 的回归项 + `bench.py` 报告（规则 `00-ssot.md` §3.5） |
| 对应主基准节 | §10.2（转换流程）、§11.1-2、§11.2（M0 行 ①，**v3.22 裁定 O27 拆两层**）、§15（O27）；规则 `10-evidence.md` §1/§2 |
| 状态 | `draft` |

> **判据引用（不重定义取值）**：**①a** ONNX↔PyTorch（FP32 链路）张量 **< 1e-4**；**①b** 板上输出 vs PyTorch **按精度档给阈**——**FP16** 张量 ≤ 1e-3 且图像域 PSNR ≥ 70 dB；**U8 场（若启用）** ≥ 60 dB；**INT8** 张量 ≤ 3e-2 且图像域 ≥ 50 dB。全部以 **§11.2 的 M0 行**为准（v3.22 裁定 O27）。

---

## 0. 测量条件随行（缺任一项视为未登记——规则 `10-evidence.md` §1）

| 项 | 取值 |
|---|---|
| 板型 / 内核 | **LubanCat-5 V2（RK3588）** / 5.10.160 |
| 板端运行时 | **librknnrt 2.3.2**（`429f97ae6b@2025-04-09`，进程实测加载 `/usr/lib/librknnrt.so`，7,726,232 B）；NPU 驱动 `0.9.8` |
| PC 端工具链 | **rknn-toolkit2 2.3.2**（conda env `rknn232`，`PYTHONNOUSERSITE=1`）；torch 2.0.1+cpu / onnx 1.17.0 / onnxruntime 1.19.2；opset **12** |
| 权重哈希（`model.zde_weight_hash`） | **`ca8855b90df9a80fa4195a831f33d3476b1964f787eb70602797c773067f3b84`**（`models/zde/Epoch99.pth`，sha256） |
| 曲线档 / 精度档 | 逐行给出（256×144 / 480×272 / 960×544；fp16 / int8） |
| 归一化约定 | `mean=[0,0,0]` / `std=[255,255,255]`（[0,255] → [0,1]），输入 NHWC uint8 或等价 float32 |
| 固定代用输入 | `docs/evidence/reports/M0-2/input_18_1080x720.png`（sha256 `6156690b…76efc9`），按各档 resize 到 `(w,h)` |
| config 快照哈希 | `6fd5c587ed7bbd283eabcb3785257f75114f2a357c414eebf6a72e871d41d08f`（`config.yaml`，v3.29 时点；**键值与本轮前一致**） |
| 时钟口径 | 仅用于耗时参考的 `CLOCK_MONOTONIC`（`mono_ns`）；**本轮结论不依赖时序** |
| 探测时点 | 2026-09-18（CST）；全部为**只读推理**，未改动系统文件与演示链路 |

**ONNX 资产哈希（本轮重跑后与重跑前逐字节一致 ⇒ 无资产漂移）**

| 档 | ONNX sha256（重跑前 = 重跑后） |
|---|---|
| 256×144 | `f5e9ed00883a07e286c333445b77e34d3320ce7a4e18d1bc53b10b64cc4857e6` |
| 480×272 | `6fef7434ec7f04d349e94f5af9d810ced5425a7b90cc983697b21314b42b4174` |
| 960×544 | `586569a61efd903a9d863219a1ff3986822e7a546cc4e66fdf5a0e98a19c54d1` |

**板端 `.rknn` 与仓库资产同哈希**（逐档 sha256 比对一致）⇒ 本轮测的就是仓库登记的那批模型：

| 档 / 精度 | `.rknn` sha256 |
|---|---|
| 256×144 fp16 | `00f93c28304ab69e0ab8ab26d92b596c893bde6c2231e6f107493ec537517b27` |
| 256×144 int8 | `4164800f1c9d87e0484bd7f6d6830ce92c921666df560837be2af65c3eb81d5a` |
| 480×272 fp16 | `35bf0a31cbe36d9efa04e566fe21538246ebd53e0f45e004fe46fdaad7ef430d` |
| 480×272 int8 | `ea33bdc68840e257232e5b19c415122e9c25206a51e653b3d8950bdace6ddc15` |
| 960×544 fp16 | `004cf9a0cb03a3877365f9665c0fe4773de1a8867ccd9d250a999f48c259c71b` |
| 256×272 int8（2 条带切片模型） | `98e6f39cfbfd703c4cc1e9bb6270795d3bb0b26467ffe7b98358a959191e4f5d` |

---

## 1. 结论速览

| # | 结论 | 判定 |
|---|---|---|
| 1 | **§11.2 M0-① 本轮重跑 = 过**：①a 三档全过（最大 2.235e-7）、①b **五个"档 × 精度"组合 + 1 个生产口径（2 条带）全过** | ✅ |
| 2 | ①b 的 480×272 两项与历史值**逐位一致**（FP16 4.91e-4 / 76.977 dB；INT8 1.72e-2 / 51.066 dB）⇒ 权重与模型未变，本轮属"重跑留报告"而非"新结果" | ✅ 可复现 |
| 3 | **2 条带生产口径**（core0+core1，480×272 INT8）重跑结果与 M0-2 的落盘输出**逐位一致（max_abs = 0）**，对齐 1.9625e-2（≤3e-2）、图像域 **53.456 dB**（≥50） | ✅ |
| 4 | **余量提示（非未过）**：**256×144 INT8** 的张量对齐 **2.602e-2**，占 INT8 阈（3e-2）的 **87%**（480×272 仅占 57%）⇒ 该档 INT8 的阈值余量偏薄，M1 实拍权重变更后须重点复检 | ⚠️ 记录 |
| 5 | **U8 曲线场未启用**（v3.22 裁定 O23 = 全档 FP16）⇒ ①b 的"U8 场 ≥60 dB"一支**不适用（N/A）** | N/A |
| 6 | **960×544 INT8 不存在**（§10.4 三档无该组合、§4.3 该档为 FP16 单核 A/B 用途）⇒ 不构成缺口，**登记为不适用** | N/A |

---

## 2. ①a：ONNX↔PyTorch（FP32 链路，三档重跑）

| 档 | `pt_estimator_vs_full_max_abs` | **`onnx_vs_pt_max_abs`** | 阈（§11.2 M0-①a） | 判定 | 报告 |
|---|---|---|---|---|---|
| 256×144 | 0.0 | **2.086e-7** | < 1e-4 | ✅ | `docs/evidence/reports/M0-reg-align/export_256x144.json` |
| 480×272 | 0.0 | **1.937e-7** | < 1e-4 | ✅ | `docs/evidence/reports/M0-reg-align/export_480x272.json` |
| 960×544 | 0.0 | **2.235e-7** | < 1e-4 | ✅ | `docs/evidence/reports/M0-reg-align/export_960x544.json` |

- `pt_estimator_vs_full_max_abs = 0.0` 表示导出用的 `CurveEstimator` 包装与官方完整模型**逐位一致**（`tools/convert_models.py` 的内置断言，< 1e-6）。
- ①a 用**随机种子输入**（`torch.manual_seed(0)`，`dummy = rand(1,3,H,W)`）——与板端用的固定代用帧无关，故 ①a 与 ①b 是两条**互相独立**的链路检查。

---

## 3. ①b：板上输出 vs PyTorch（逐档 × 精度档）

| 档 | 精度 | 板端模型 | 输入馈入 | **张量 max_abs** | 阈（§11.2 M0-①b） | **图像域 PSNR** | 图像域阈 | 最大像素差 | 判定 |
|---|---|---|---|---|---|---|---|---|---|
| 256×144 | fp16 | `zde_curve_256x144_fp16_tk232.rknn` | float32 → lite2 | **4.601e-4** | ≤1e-3 | **77.022 dB** | ≥70 dB | 0.198 LSB | ✅ |
| 256×144 | int8 | `zde_curve_256x144_int8_tk232.rknn` | float32 → lite2 | **2.602e-2** | ≤3e-2 | **55.508 dB** | ≥50 dB | 10.820 LSB | ✅ |
| 480×272 | fp16 | `zde_curve_480x272_fp16_tk232.rknn` | float32 → lite2 | **4.910e-4** | ≤1e-3 | **76.977 dB** | ≥70 dB | 0.205 LSB | ✅ |
| 480×272 | int8 | `zde_curve_480x272_int8_tk232.rknn` | float32 → lite2 | **1.716e-2** | ≤3e-2 | **51.066 dB** | ≥50 dB | 7.464 LSB | ✅ |
| 960×544 | fp16 | `zde_curve_960x544_fp16_tk232.rknn` | float32 → lite2 | **5.482e-4** | ≤1e-3 | **76.904 dB** | ≥70 dB | 0.233 LSB | ✅ |
| 480×272 | int8（**2 条带**，core0+core1） | `zde_curve_256x272_int8_tk232.rknn` ×2 | **uint8 raw**（C 形态探针） | **1.9625e-2** | ≤3e-2 | **53.456 dB** | ≥50 dB | 9.602 LSB | ✅ |

**图像域口径说明**：图像域 = **在模型输入分辨率上**用同一帧做 8 次迭代曲线应用后的 PSNR（`tools/enhance_ref.py`，各档独立计算，与 `docs/evidence/M0-8-int8-retry.md` §8 同口径）。**不是** 1080×608 应用窗的 PSNR——后者由 §13 的实时画质验收承担。

**逐档报告与原始输出**

| 档 / 精度 | 板端报告 | 板端输出张量 |
|---|---|---|
| 256×144 fp16 / int8 | `probe_fp16_256x144.json` / `probe_int8_256x144.json` | `out_fp16_256x144.npy` / `out_int8_256x144.npy` |
| 480×272 fp16 / int8 | `probe_fp16_480x272.json` / `probe_int8_480x272.json` | `out_fp16_480x272.npy` / `out_int8_480x272.npy` |
| 960×544 fp16 | `probe_fp16_960x544.json` | `out_fp16_960x544.npy` |
| 图像域闸口 | `image_gate_256x144.json` / `image_gate_480x272.json` / `image_gate_960x544.json` | — |
| 2 条带口径 | `split2_stdout.json` + `split2_verify.json` | `split2__stitched.f32` |

（以上均在 `docs/evidence/reports/M0-reg-align/`）

---

## 4. 生产估计口径（2 条带）与可复现性

**为什么要单列**：480×272 INT8 的**生产口径**不是"单模型整帧"，而是 §4.3/§7 的「**2 条带空间切分 + core0+core1 并行**」（v3.22 裁定 O29 选项 a）。因此本档除单模型外，把该口径一并重跑。

| 项 | 实测 |
|---|---|
| 切分方式 | 2 条带，切片模型输入 256×272，**HALO = 7 px**（= 7 次 3×3 的感受野半径），输出只取中间列拼接 |
| 输入馈入 | **uint8 raw NHWC**（`frame_480x272_u8.raw`，sha256 `bda752ad…11aab5`；**已核对 = 固定代用输入的 uint8 化，逐字节一致**） |
| 拼接输出 vs PT 参考 | **max_abs = 1.9625e-2**（≤3e-2 ✅） |
| 图像域 PSNR | **53.456 dB**（≥50 ✅） |
| **可复现性**（与 M0-2 的落盘 dump 逐位比对） | **max_abs = 0.0**（完全一致） |
| 帧耗时（参考，不作判据） | 中位 **21.66 ms**、p90 24.66 ms（20 次；两条带各 21.61 / 21.66 ms 中位） |

---

## 5. 复现命令

**①a（PC，conda env `rknn232`；`PYTHONNOUSERSITE=1`）**

```
python tools/convert_models.py export \
  --weights models/zde/Epoch99.pth --out models/zde/zde_curve_<W>x<H>.onnx \
  --h <H> --w <W> \
  --report docs/evidence/reports/M0-reg-align/export_<W>x<H>.json \
  --input-png docs/evidence/reports/M0-2/input_18_1080x720.png \
  --dump-input docs/evidence/reports/M0-reg-align/input_<W>x<H>.npy \
  --dump-pt-ref docs/evidence/reports/M0-reg-align/pt_ref_<W>x<H>.npy
```

**①b（板端；`PYTHONPATH=~/rknn_ab/site/232`，逐档把 `--tol` 取该精度档的阈值）**

```
python3 ~/rknn_ab/probe/rknn_probe.py \
  --rknn ~/rknn_ab/models/<model>.rknn \
  --input ~/rknn_ab/data/reg_align/input_<W>x<H>.npy \
  --ref   ~/rknn_ab/data/reg_align/pt_ref_<W>x<H>.npy \
  --tol 1e-3（fp16）/ 3e-2（int8） --runs 20 \
  --dump ~/rknn_ab/data/reg_align/out_<tier>_<W>x<H>.npy \
  --report ~/rknn_ab/data/reg_align/probe_<tier>_<W>x<H>.json \
  --librknnrt /usr/lib/librknnrt.so
```

**2 条带口径（板端）**

```
~/rknn_ab/probe/rknn_split_probe ~/rknn_ab/models/zde_curve_256x272_int8_tk232.rknn \
  ~/rknn_ab/data/frame_480x272_u8.raw 272 480 20 2 0,1 float <dump_prefix>
```

**图像域闸口（PC）**

```
python3 tools/enhance_ref.py --frame <input_*.npy> --ref-curve <pt_ref_*.npy> \
  --curve FP16=<out_fp16_*.npy> [--curve INT8=<out_int8_*.npy>] --out <image_gate_*.json>
```

> **工具变更（本版）**：`tools/rknn_probe.py` 新增 `--tol`（默认仍 **1e-4**，**不静默放宽**）——按 v3.22 裁定 O27，①b 的阈值按精度档参数化；此前该脚本硬编码 1e-4，故 INT8/FP16 的判定恒为 `align_pass=false`，与本裁定冲突。

---

## 6. 与历史记录的比对（为什么本档数值可对账）

| 项 | 历史值（来源） | 本轮值 | 差 |
|---|---|---|---|
| 480×272 FP16 张量对齐 | 4.91e-4（`docs/evidence/M0-2-ab-toolkit-version.md` §7 / §14 v3.22） | **4.910e-4** | 0 |
| 480×272 FP16 图像域 | 76.98 dB（`docs/evidence/M0-2/int8_image_gate.json`） | **76.977 dB** | 0 |
| 480×272 INT8 张量对齐 | 1.72e-2（同上） | **1.716e-2** | 0 |
| 480×272 INT8 图像域 | 51.07 dB（同上） | **51.066 dB** | 0 |
| 480×272 INT8 双条带图像域 | 53.46 dB（同上） | **53.456 dB** | 0 |
| 2 条带拼接输出 | `curve_board_int8_split2_480x272.npy`（M0-2） | `split2__stitched.f32` | **0.0** |
| ONNX 文件字节 | 重跑前的 `models/zde/*.onnx` | 重跑后同路径 | **哈希一致** |

⇒ **①a/①b 的结论在"权重未变、模型未变"的前提下完全可复现**；本轮的实际新增内容是：**三档 × 精度档的完整矩阵**（此前 256×144 / 960×544 只有耗时、无对齐报告）、**逐档落盘输出与图像域报告**、以及**生产 2 条带口径的对齐与图像域留档**。

---

## 7. 与 §11.2 的判定

| 回归项 | 判定 | 说明 |
|---|---|---|
| ① **ONNX↔RKNN 对齐回归**（①a + ①b） | ✅ **过** | ①a 三档全过（最大 2.235e-7）；①b 五组合 + 生产 2 条带全过；阈值按 §11.2（v3.22 裁定 O27）分层取用，**未放宽**。报告与原始输出见 `docs/evidence/reports/M0-reg-align/` |

**对 M0 出口的影响**：M0 三条回归项中 **① 已过**、**③ 已产出**，**② （config 键完整性）仍为「未过」**（4 项空缺，根因 O30 = 暂时挂起，见 `docs/evidence/M0-reg-config.md`）。**按 §11.2 纪律，三者须全过方可出口 ⇒ M0 仍不得出口**；本项通过不改变该结论。

**残留复检义务（M1）**：M1 权重一经变更（含实拍域 fine-tune），按 **O11** 作废并全量重跑；本档的"复检时点"= **M1 权重变更后**，其中 **256×144 INT8 的余量偏薄（87%）须重点复检**。