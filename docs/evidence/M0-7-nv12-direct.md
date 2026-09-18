# 证据：M0-7 NV12 直喂 NPU 实验

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：一次实验的实测结果留档，对应 `docs/plans/M0.md` §3 的 **M0-7** 项 |
| 效力 | **非验收依据**（规则 `00-ssot.md` §3.5）；验收只认主基准 §13 复现方式 + §11.2 回归项 + `bench.py` 报告 |
| 对应主基准节 | §3.1（G1 曲线场几何与采样）、§3.3（RGA → RGB 主链路）、§9.5（CSC/range 指纹）、§11.1-7 |
| 实验时点 | **2026-09-18 19:50–20:06 CST**（Ar 组补档重跑于 20:06） |
| 实验方式 | **只读测试**（编译模型 + 板端推理 + 只读对比；无停流/复位/改 systemd） |
| 状态 | `draft` |

> 本档只记录实测事实与判定，不复制规范取值。NV12 直喂的口径见主基准 §3.1（"仅作实验，主线保持 RGA → RGB"）。

## 1. 实验目的（§11.1-7）

验证 RKNPU 的 **`inputs_yuv_fmt=nv12`**（模型顶部插入 YUV→RGB 预处理）能否作为主链"核对路径"**NV12 → RGA → RGB888** 的替代。**可行记优化项，不可行关闭。**

## 2. 机制调研（PC 转换侧）

RKNN Toolkit2 `rknn.config(inputs_yuv_fmt=...)` 支持在模型顶部插入 YUV 预处理。硬约束（来自 `rknn_base.so` 内建字符串）：

| 约束 | 值 | 影响 |
|---|---|---|
| 支持格式 | 仅 `nv12` / `nv21` | 符合 NV12 |
| **量化** | **仅 INT8**（`only available when do quantization is True`） | FP16 主路线**不可用** |
| 输入 shape | 须 **NCHW 4 维、channel=3** | 与模型一致 |
| 输入 H/W | **须被 4 整除** | §3.1 的 480×272 ✓ / 960×544 ✓ / 256×144 ✓ |
| 内置 CSC | `inputyuv2rgb_wBT601_l`（BT.601 **limited**） | 与 RGA 的 CSC 同族（见 §4.2） |

实测构建日志：`After insert yuv2rgb block. Model input info has changed.`，模型输入由 **1 个拆成 2 个** 张量。

## 3. 复现命令

```bash
# ① 编译带 nv12 预处理的 INT8 模型（PC，转换环境 rknn232）
PYTHONNOUSERSITE=1 ~/.conda/envs/rknn232/bin/python tools/convert_models.py build \
  --onnx models/zde/zde_curve_480x272.onnx \
  --out models/zde/zde_curve_480x272_int8_nv12_tk232.rknn \
  --quant int8 --target rk3588 --calib models/zde/calib/dataset.txt \
  --inputs-yuv-fmt nv12 --report docs/evidence/reports/M0-7/build_nv12_int8_480x272.json

# ② 生成测试数据（PC）：NV12（BT.601 limited，UV 2×2 BOX 下采样）+ RGB888 留档
python3 /tmp/m0-7-make-input.py   # 产物见 reports/M0-7/nv12_480x272.raw + rgb888_480x272.raw

# ③ 板端：RGA 同尺寸 NV12→RGB888（生产口径 CSC 对照）
gcc -I/usr/include/rga -o rga_csc_probe rga_csc_probe.c -lrga
rga_csc_probe nv12_480x272.raw 480 272 rga_rgb888.raw

# ④ 板端：三组推理（rknn_c_probe rawu8 模式）
#   A : 标准 RGB INT8 模型 + PC 生成 RGB888
rknn_c_probe zde_curve_480x272_int8_tk232.rknn rgb888_480x272.raw 20 nhwc float nosram rawu8 noperf out_rgbA.f32 0
#   Ar: 标准 RGB INT8 模型 + RGA 解码 RGB888（主线路径）
rknn_c_probe zde_curve_480x272_int8_tk232.rknn rga_rgb888.raw 20 nhwc float nosram rawu8 noperf out_rgarga.f32 0
#   B : NV12 直喂（nv12 预处理模型 + NV12 原始）
rknn_c_probe zde_curve_480x272_int8_nv12_tk232.rknn nv12_480x272.raw 20 nhwc float nosram rawu8 noperf out_nv12.f32 0
```

## 4. 结果

### 4.1 板端推理（librknnrt 2.3.2 / core_mask=AUTO，20 runs）

| 组 | 输入 | 模型 | NPU 中位 (ms) | 输入张量 |
|---|---|---|---|---|
| A | PC生成 RGB888 | 标准 RGB INT8 | **29.63** | 1×[1,272,480,3] |
| Ar | RGA解码 RGB888 | 标准 RGB INT8 | **29.65** | 1×[1,272,480,3] |
| B | NV12 原始 | nv12 预处理 INT8 | **44.35** | 2×[Y:1,272,480,1] + [UV:1,136,480,1] |

数值口径 = 对应报告 JSON 的 `npu_run_ms.median`。原始报告：`reports/M0-7/A_rgb.json` / `reports/M0-7/Ar_rgarga.json`（**Ar 于补档时重跑**，2026-09-18 20:06）/ `reports/M0-7/B_nv12.json`。

> **运行间波动说明**：NPU 耗时呈双峰（清洁态 ≈29–30 ms / 争用态 ≈44–47 ms）。A 的 `p90=30.55`、B 的 `min=42.99` ⇒ **B 的下界高于 A 的 p90**，故"B 慢于 A"的判定不受波动影响。原表记 A=30.08 / Ar=30.14 为当时终端读数，本次统一以入库 JSON 中位为准（差异 ≤0.5 ms，属波动）。

### 4.2 曲线场差异（INT8 量化噪声基线 = 1.72e-2）

| 对照 | max_abs | mean_abs | >0.01 | 含义 |
|---|---|---|---|---|
| A vs Ar | 0.0111 | 0.00154 | 0.00% | 编码往返 + RGA CSC（基线噪声） |
| **B vs Ar** | **0.0125** | **0.00292** | 0.01% | **★ NV12 直喂 vs 主线 RGA→RGB** |
| B vs A | 0.0139 | 0.00236 | 0.01% | NV12 直喂 vs 无处理 |

全部差异与 INT8 量化噪声**同量级**，`>0.01` 占比 ≤0.01% ⇒ **色彩域一致，无显著曲线漂移**。

> 上表三行由 `out_rgbA.f32` / `out_rgarga.f32` / `out_nv12.f32` 逐元素复算得出（**Ar 输出 `out_rgarga.f32` 已补档**）。

### 4.3 RGA 与 RKNN 内置 CSC 的一致性

| 项 | 值 |
|---|---|
| RGA 解码 RGB vs 原始 RGB（image 域） | PSNR **37.92 dB**（mean_abs 0.959 / max_diff 114 边缘点） |
| 定性 | 与 M0-13 §1.5 实测的 RGA-CSC PSNR 31–38 dB **同区间** ⇒ RGA 的 BT.601-limited 与 RKNN 内置 `BT601_l` 解码**同族等价** |

**结论**：NV12 直喂与"NV12 → RGA → RGB888"在色彩域上**等价**，不存在 CV 域漂移。

## 5. 判定（§11.1-7）

**NV12 直喂 = 功能可行，但不构成优化项，建议「关闭」；主线保持 RGA → RGB。**

| 判据 | 结论 |
|---|---|
| 技术可行（能加载/能推理/无漂移） | ✅ 曲线场差异在 INT8 噪声内（max 1.25e-2） |
| **耗时** | ❌ **更慢**：NPU 中位 44.4 ms vs 主线 29.6 ms（**+14.7 ms**，NPU 需做 YUV→RGB 预处理） |
| FP16 主路线可用性 | ❌ `inputs_yuv_fmt` **仅 INT8**，与 §4.1/§6.1 的 FP16 正式路线冲突 |
| 输入对齐 | ⚠️ H/W 须被 4 整除；与 §3.1 契约可兼容但增加约束 |
| §9.5 指纹可回放性 | ❌ 颜色转换沉入模型内部，失去 RGA 侧 CSC/range 指纹的可核对性 |

**依据**：NV12 直喂在 NPU 内做 YUV→RGB 的开销（+14 ms）**远大于**主线 RGA→RGB 的 2.10 ms（§3.3 / M0-13），且牺牲 FP16 与指纹可回放性 ⇒ **无收益，关闭**。

## 6. 工具改动

- `tools/convert_models.py`：新增 `--inputs-yuv-fmt {nv12,nv21}`（build 段，仅 INT8；报告含该字段）。
- `tools/rknn_c_probe.c`：新增 `rawu8` 输入模式（直读 uint8，不做 float 转换），并支持多输入模型（NV12 折成 2 张量 Y/UV）。
- `tools/rga_csc_probe.c`：新增，同尺寸 NV12→RGB888 CSC 对照（VA 路径，一次 blit）。

## 7. 报告原档

`docs/evidence/reports/M0-7/`：`build_nv12_int8_480x272.json`（构建报告）、`A_rgb.json` / `Ar_rgarga.json` / `B_nv12.json`（路径 A / Ar / B 推理报告）、`nv12_480x272.raw` / `rgb888_480x272.raw` / `rga_rgb888.raw`（同源输入 + RGA 解码输入）、`out_rgbA.f32` / `out_rgarga.f32` / `out_nv12.f32`（曲线场输出，float32 CHW）。

> `.raw` / `.f32` 中间产物按 `.gitignore` 约定**不入库**（可由 §3 复现命令重生成）；入库的是三份推理报告 JSON 与构建报告。