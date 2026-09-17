# 证据：M0-4 —— HQ（Retinexformer）迁移与 R9 机械闸口

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：对应 `docs/plans/M0.md` §3 的 **M0-4**（HQ 三档 R9 机械闸口复测 + HQ 模型/active shape 定版 + 保底 CNN 上板）与 **M0-12**（HQ 降级梯度 + 保底上板） |
| 效力 | **非验收依据**（规则 `00-ssot.md` §3.5）；结论若用于定稿须走 §15 裁定后回写正文与 §10.4 |
| 对应主基准节 | §2.2（toolkit2 确认门）、§3.1（HQ shape 梯度与 pad/crop）、§4.1（模型选型与权重域）、§6.2（关键帧预算）、§9.3（R9 重影机械闸口）、§10.2（转换流程）、§11.1-4 / -5 / -12、§11.2 M0-①b、§15（O30） |
| 时点 | **2026-09-17 21:0x–21:3x CST**（HQ 侧首次迁移与编译） |
| 状态 | **`占位`** —— 用户 2026-09-17 明确指示"先做摄像头调试、HQ 只留占位"；本轮只完成"**可导出 + 可编译 + 资产盘点**"，**未做 R9 闸口与耗时** |

> 纪律：不引用预研归档中"M0 Retinexformer FP16 残影"等**历史结论**（规则 `10-evidence.md` §2、`environment/pre-research-inventory.md` §3）；本档只记录本轮自测。

## 1. 目的（原计划）

按 §9.3 / §11.1-4：对 HQ 三档（960×544 / 768×432 / 512×288）逐一复测 **PT FP32 vs 板上 RKNN FP16** 的重影（row/col_diff 方法），并当天定版 `hq.model_id` 与 `hq.active_shape`；按 §11.1-12 让保底 CNN 上板作为现货。

## 2. 已完成（本轮自测）

### 2.1 工具与流程

| 项 | 实现 |
|---|---|
| 架构直载 | `refs/pre-research/basicsr/models/archs/RetinexFormer_arch.py`（`importlib.util` 直载，绕开 `basicsr/__init__` 依赖链），sha256 `1567c89d…87631` |
| 转换入口 | `tools/convert_models.py export --arch hq`（新增；含 `--arch-path` / `--n-feat` / `--stage` / `--num-blocks`；报告 JSON 记权重与产物哈希、missing/unexpected 键、PT↔ONNX 对齐） |
| 评估入口 | `tools/hq_eval.py`（新增）：`weights`（上游权重横比）/ `prep-input`（三档闸口输入）/ `gate`（PT vs RKNN：张量差、图像域 PSNR、**row/col_diff 结构指标**、"仅 FP16 舍入"对照基线） |
| 板端计时 | `tools/rknn_c_probe.c` 增 **`core_mask`** 参数（§4.1 的 core1+2 契约用） |

### 2.2 模型与导出（`LOL_v1.pth`）

| 项 | 实测 |
|---|---|
| 模型 | `Retinexformer(in=3, out=3, n_feat=40, stage=1, num_blocks=[1,2,2])` —— **1,605,701 参数**，权重载入 **零 missing / 零 unexpected** |
| ONNX 512×288 | opset 12、固定输入；**ONNXPyTorch（FP32）max abs = 3.16e-6 < 1e-4 ✓**（`reports/M0-4/export_hq_512x288_lolv1.json`） |
| ONNX 768×432 | 导出成功（`export_hq_768x432_lolv1.json`） |
| ONNX 960×544 | **失败**（输出日志为空，疑 onnxsim/内存被杀；**未定位**，见 §4 待办） |
| RKNN FP16 512×288 | `rknn-toolkit2 2.3.2` / `target=rk3588` / `do_quantization=False` → **成功**，`models/hq/hq_512x288_fp16_tk232.rknn`（21,132,982 B）；日志含 `exMatMul` / `exNorm` / `exSoftmax`；内部内存 366,048 KB / 权重 3,209.94 KB |

**⇒ §2.2 的确认门通过（2.3.2 可编译注意力算子，不回退 2.4.2a12）**，明细见 `docs/evidence/M0-2-ab-toolkit-version.md` §11。

### 2.3 上游权重横比（代用数据，**非裁定**）

设置：LOLv2 `Real_captured/Test` 前 20 对、PT FP32、512×288、`INTER_AREA`、RGB `[0,1]`；PSNR 全通道 / SSIM 灰度 11×11 高斯。报告 `reports/M0-4/hq_weight_selection.json`。

| 权重 | PSNR (dB) | SSIM |
|---|---|---|
| **`LOL_v1`** | **23.98** | **0.8851** |
| `LOL_v2_real` | 23.79 | 0.8848 |
| `NTIRE` | 19.67 | 0.7660 |
| `SID` | 14.98 | 0.7160 |
| `SDSD_indoor` | 14.64 | 0.6742 |
| `SDSD_outdoor` | 14.55 | 0.6762 |
| `FiveK` | 10.38 | 0.3657 |
| `LOL_v2_synthetic` | 10.35 | 0.4446 |
| `SMID` | 10.03 | 0.4193 |
| no-op 基线（Low vs Normal） | 7.61 | — |

⇒ **支持"LOL_v1 起步"（§4.1）**，但属代用数据结论（O7 规则）⇒ **`hq.model_id` 仍为未定稿**。

### 2.4 闸口输入（三档，已就绪）

`tools/hq_eval.py prep-input` 由真实暗场景帧（`refs/pre-research/m0_results/18.png`）裁 1080×608 后按 §3.1 的 `hq_shape_ladder` 生成，主档带**上下各 2 行零 pad**：

| 档 | 内容区 | pad | 输入 RGB 均值 | 文件 |
|---|---|---|---|---|
| 960×544（主） | 960×540 | 上下各 2 | 9.53 | `gate_input_960x544.png` |
| 768×432（降级 1） | 768×432 | 0 | 9.60 | `gate_input_768x432.png` |
| 512×288（降级 2） | 512×288 | 0 | 9.59 | `gate_input_512x288.png` |

清单 `gate_inputs.json`；PT 参考输出 `pt_ref_512x288.npy` / `pt_ref_768x432.npy`（`.npy` 按 `.gitignore` 不入库，复现见 §5）。

### 2.5 保底 CNN 候选可得性（实测）

| 候选 | 实测结论 |
|---|---|
| KinD++（`zhangyhuaee/KinD_plus`） | 权重托管 **Google Drive（本机不可达）**；仓库仅 `checkpoint/` 的 **TF1** ckpt 且**缺 reflectance-restoration 子网** ⇒ 无法转换 |
| RetinexNet（`weichen582/RetinexNet`） | 权重托管 **Google Drive（不可达）**；仓库 `model/` 仅 `Decom` / `Relight`，**缺 `Denoise`** ⇒ 无法转换 |
| SCI（`vis-opt-group/SCI`，**MIT**） | 权重 `TPAMI/weights/weights_1_3500.pt`（104,050 B，sha256 `067d41fd…5a4d8`）**可由 GitHub API 直取**（`raw.githubusercontent.com` 本机不可达，须用 `Accept: application/vnd.github.raw`）；纯卷积（22,898 参数），官方推理路径 = 单模块 + `r = clamp(input/i,0,1)`；**已下载待评估**（`refs/pre-research/fallback/sci/`，不入库） |

⇒ 保底选型与获取路径**未定**，登记为 **O30**（主基准 §15）。

## 3. 未完成（占位范围）

| 项 | 状态 | 阻塞 |
|---|---|---|
| 三档 **R9 机械闸口**（PT FP32 vs 板上 FP16，含 row/col_diff 与目视） | **未做** | 需三档 RKNN 齐备；**960×544 ONNX 导出失败待查** |
| 三档 **耗时实测**（core1+2，落 §6.2 预算） | **未做** | 属 M0-5；依赖三档 RKNN |
| **保底 CNN 上板**（§11.1-12 现货） | **未做** | 候选权重不可得（**O30** 待裁定） |
| `hq.model_id` / `hq.active_shape` / `hq.pad_crop_policy` 定版 | **未定稿** | 同上；`model_id` 另需 M1 实拍集复检 |

**⇒ §11.1-4 与 §11.1-12 未落定 ⇒ M0 不得出口**（诚实登记，不以文字说明代替证据）。

## 4. 待办

1. 排查 **960×544 ONNX 导出失败**（无输出即被杀；先看内存/onnxsim 行为，必要时先保存未简化 ONNX 再单独简化）。
2. 三档 RKNN FP16 编译 + 上板计时（`rknn_c_probe … 6`，core1+2）+ 输出落盘。
3. `tools/hq_eval.py gate` 出三档对照（含 `fp16_round_control` 基线）+ **目视盲评（≥2 人）**，并自标定 row/col_diff 阈值（§9.3 要求"当天用 PT 对照和盲评共同标定"）。
4. 保底 CNN：先裁定 **O30**，再按裁定转换 / 对齐 / 上板。

## 5. 复现命令

```bash
PY=/home/jenkolux/.conda/envs/rknn232/bin/python   # py3.8 / toolkit2 2.3.2；一律 PYTHONNOUSERSITE=1

# 1) 权重横比（代用数据）
PYTHONNOUSERSITE=1 $PY tools/hq_eval.py weights \
  --weights $(ls refs/pre-research/pretrain_model/*.pth) \
  --data refs/pre-research/lolv2/extracted/LOLv2/Real_captured/Test --limit 20 --h 288 --w 512 \
  --out docs/evidence/reports/M0-4/hq_weight_selection.json

# 2) 闸口输入（三档；主档上下各 2 行零 pad）
PYTHONNOUSERSITE=1 $PY tools/hq_eval.py prep-input --src refs/pre-research/m0_results/18.png \
  --crop 1080x608 --shape 960x544:2 --shape 768x432:0 --shape 512x288:0 \
  --out-dir docs/evidence/reports/M0-4 --out docs/evidence/reports/M0-4/gate_inputs.json

# 3) ONNX（以 512×288 为例）
PYTHONNOUSERSITE=1 $PY tools/convert_models.py export --arch hq \
  --weights refs/pre-research/pretrain_model/LOL_v1.pth --h 288 --w 512 \
  --out models/hq/hq_512x288.onnx \
  --input-png docs/evidence/reports/M0-4/gate_input_512x288.png \
  --dump-input docs/evidence/reports/M0-4/gate_input_512x288.npy \
  --dump-pt-ref docs/evidence/reports/M0-4/pt_ref_512x288.npy \
  --report docs/evidence/reports/M0-4/export_hq_512x288_lolv1.json

# 4) RKNN FP16（2.3.2）
PYTHONNOUSERSITE=1 $PY tools/convert_models.py build --onnx models/hq/hq_512x288.onnx \
  --out models/hq/hq_512x288_fp16_tk232.rknn \
  --report docs/evidence/reports/M0-4/build_hq_512x288_fp16_tk232.json
```

> **本轮实测注意**：`rknn-toolkit2` 会在**当前工作目录**留下 `check*_ops.onnx` 中间文件（已在 `.gitignore` 排除）。

## 6. 产物与报告索引

| 类型 | 路径 |
|---|---|
| 模型 | `models/hq/hq_512x288.onnx`、`hq_768x432.onnx`、`hq_512x288_fp16_tk232.rknn`（`.gitignore` 不入库） |
| 报告 | `docs/evidence/reports/M0-4/{hq_weight_selection,gate_inputs,export_hq_512x288_lolv1,export_hq_768x432_lolv1,build_hq_512x288_fp16_tk232}.json` + `build_hq_512x288_fp16_tk232.log` |
| 闸口输入 | `docs/evidence/reports/M0-4/gate_input_{512x288,768x432,960x544}.png`（`.npy` 由 §5 重生成） |
| 编译确认门 | `docs/evidence/M0-2-ab-toolkit-version.md` §11 |
| 计划与落位 | `docs/plans/M0.md` §3-4 / §3-12；主基准 §4.1 的 v3.23 补注、§11.1-4 / -12 |
