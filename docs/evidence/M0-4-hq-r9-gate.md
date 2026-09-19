# 证据：M0-4 —— HQ（Retinexformer）迁移与 R9 机械闸口

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：对应 `docs/plans/M0.md` §3 的 **M0-4**（HQ 三档 R9 机械闸口复测 + HQ 模型/active shape 定版 + 保底 CNN 上板）与 **M0-12**（HQ 降级梯度 + 保底上板） |
| 效力 | **非验收依据**（规则 `00-ssot.md` §3.5）；结论若用于定稿须走 §15 裁定后回写正文与 §10.4 |
| 对应主基准节 | §2.2（toolkit2 确认门）、§3.1（HQ shape 梯度与 pad/crop）、§4.1（模型选型与权重域）、§6.2（关键帧预算）、§9.3（R9 重影机械闸口）、§10.2（转换流程）、§11.1-4 / -5 / -12、§11.2 M0-①b、§15（O30） |
| 时点 | **2026-09-17 21:0x–21:3x CST**（HQ 侧首次迁移与编译） |
| 状态 | **`draft`（部分完成；v3.31 更新）** —— 2026-09-17 曾按用户指示置为"占位"（只做"可导出 + 可编译 + 资产盘点"）；**v3.31 已完成三档转换与编译 + 板端三档推理 + R9/对齐闸口实测**（见 §7/§8）。**保底 CNN 仍按 O30「暂时挂起」不动**；`hq.model_id` / `hq.active_shape` / `hq.pad_crop_policy` 因**闸口不过**而**不予定版**（见 §9） |

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
| 模型 | 三档 ONNX `models/hq/hq_{512x288,768x432,960x544}.onnx` + 三档 FP16 `.rknn`（`hq_<shape>_fp16_tk232.rknn`）——**v3.31 齐备**（`.gitignore` 不入库；哈希见 §7.1） |
| 报告 | 三档 `export_hq_*_lolv1.json`、`build_hq_*_fp16_tk232.{json,log}`（v3.31 补齐 768/960）、`gate_board_*.json`（v3.31）、`hq_weight_selection.json`、`gate_inputs.json` —— 完整索引见 §9 |
| 闸口输入 | `docs/evidence/reports/M0-4/gate_input_{512x288,768x432,960x544}.png`（`.npy` 由 §5 重生成） |
| 编译确认门 | `docs/evidence/M0-2-ab-toolkit-version.md` §11 |
| 计划与落位 | `docs/plans/M0.md` §3-4 / §3-12；主基准 §4.1 的 v3.23 补注、§11.1-4 / -12 |

---

## 7. 本轮（v3.31）：三档核心推理试验

> **测量条件随行**（规则 `10-evidence.md` §1）：板 **LubanCat-5 V2**（内核 5.10.160）/ 板端 **librknnrt 2.3.2**（`429f97ae6b@2025-04-09`，进程实测加载 `/usr/lib/librknnrt.so`）/ 驱动 **0.9.8** / PC **rknn-toolkit2 2.3.2**（env `rknn232`，`PYTHONNOUSERSITE=1`）/ opset **12** / 权重 **`LOL_v1.pth`** sha256 `9b4810dc…32cd1` / 架构 `RetinexFormer_arch.py` sha256 `1567c89d…87631` / 归一化 `mean=0,std=255` / 固定输入 = `gate_input_<shape>.png`（来自 `refs/pre-research/m0_results/18.png` 的 1080×608 裁剪，`pad_fill=zero`）/ 探测时点 **2026-09-18 08:0x–08:2x CST**（安静板）。

### 7.1 转换与编译（三档齐备）

| 档 | ONNX↔PT（FP32） | ONNX sha256 | `.rknn` FP16 | `.rknn` sha256 |
|---|---|---|---|---|
| 512×288 | **3.159e-6** | `855ef58c…43cce` | 21,132,982 B | `b3aa2001…7cbbf4` |
| 768×432 | **1.189e-5** | `4c413134…eac4b3` | 42,184,694 B | `75f4bef7…3fed1aa` |
| 960×544 | **2.518e-5** | `64d2cbb9…a8c4e771` | 60,173,686 B | `67bc708a…f99b73c7` |

- **960×544 的 ONNX 导出失败已消解**：本轮**一次成功**（27 s，日志 `0 NONE 0 NOTE 0 WARNING 0 ERROR`）。**原失败未能复现** —— 如实登记为"**环境性、未定位**"，不编根因（规则 `10-evidence.md` §2）。
- 三档编译均为 `target=rk3588` / `do_quantization=False`（FP16），日志与报告见 §9 索引。

### 7.2 板端推理（core_mask=6 被拒 ⇒ 实际单核 AUTO）

| 档 | wall 中位 | npu_run 中位 | p90 | 模型 internal 内存 | DMA |
|---|---|---|---|---|---|
| 512×288 | **3352 ms** | 3332 ms | 3672 | 374.8 MB | 395.6 MB |
| 768×432 | **6404 ms** | 6373 ms | 7082 | 843.4 MB | 885.1 MB |
| 960×544 | **10014 ms** | 9977 ms | 10813 | **1327.5 MB** | 1387.3 MB |

- 每次 **5 runs**、安静板、`perf_collect=0`；**不作跨档外推**（§6 定锚纪律）。
- **`rknn_set_core_mask(6)`（= §4.1 的 `RKNN_NPU_CORE_1_2`）被运行时拒绝**：`Not support core mask: 6, fallback to single core auto mode`，`ret = −1`。**可接受性扫描（修正版）**：

  | mask | 0 | 1 | 2 | 3 | **6** | 7 |
  |---|---|---|---|---|---|---|
  | HQ 512×288 | ✅0 | ✅0 | ✅0 | ✅0 | **❌ −1** | ✅0 |
  | ZDE 480×272（对照） | ✅0 | — | — | ✅0 | **❌ −1** | ✅0 |

  ⇒ **不是 HQ 模型特有**；本版运行时只接受 0/1/2/3/7。**§4.1/§7 的"HQ 绑 core1+2"因此不可执行** → 登记 **O36**。**（v3.54 修正）** 上表**未测 mask=4 与 5**；补测后正确口径 = **接受 0 / 1 / 2 / 3 / 4 / 7，拒绝 5 / 6**（**mask=4 = core2 单独可用**），据此 **O36 已裁定契约值改为 `RKNN_NPU_CORE_2`**，见 `docs/evidence/O36-core-mask-contract.md`。
- **960×544 编译期另有 22 处核分配失败**：`Failed to config layer: 'Conv:…' using 3Core fallback to single`（分布于 denoiser 的 encoder/decoder/bottleneck 的 `fn/net/net.{2,4}` 卷积）⇒ **核分配是编译期决定的，运行时改不了**，与上面的 mask 被拒自洽。

### 7.3 ①b 与 R9 判定（`tools/hq_eval.py gate`）

| 档 | 纯 FP16 舍入对照（PT→fp16→PT） | 板上 RKNN 张量 max_abs | 对纯舍入的倍率 | 图像域 PSNR（阈 ≥70 dB） | 最大像素差 | `>1 LSB` 占比 | row/col `structure_factor` | 判定 |
|---|---|---|---|---|---|---|---|---|
| 512×288 | 4.882e-4 / 86.15 dB | **4.604e-3** | **9.4×** | **67.35 dB** ❌ | 0.72 LSB | 0.0000 | 2.11 / 1.84（对照 3.23） | ❌ 不过（阈 ≤1e-3） |
| 768×432 | 4.883e-4 / 86.10 dB | **6.465e-3** | **13.2×** | **69.87 dB** ❌ | 0.87 LSB | 0.0000 | 2.17 / 1.81（对照 3.05） | ❌ 不过 |
| 960×544 | 4.883e-4 / 85.99 dB | **8.929e-3** | **18.3×** | **68.23 dB** ❌ | 1.98 LSB | 0.0001 | 2.40 / 2.17（对照 2.93） | ❌ 不过 |

- 阈值取自 §11.2 M0-①b 的 FP16 档（张量 ≤1e-3 且图像域 ≥70 dB），**未放宽**；注意该阈是以 **ZDE++ 的同板实测**（4.91e-4 / 76.98 dB）为基线标定的。
- **R9（重影）一侧无异常证据**：最大像素差 ≤2.0 LSB、`>1 LSB` 占比 ≤0.01%，且**结构因子不高于纯舍入对照** ⇒ OK 的失败是"**数值闸口不过**"，**不是**"画质出现重影/条纹"。
- 差异图已落盘：`docs/evidence/reports/M0-4/<shape>_board_diff20x.png`（另 `_pt_ref` / `_fp16_control` / `_board_out` 三张 PNG）。

### 7.4 复现命令

```
# 转换（PC, env rknn232, PYTHONNOUSERSITE=1）
python tools/convert_models.py export --arch hq \
  --weights refs/pre-research/pretrain_model/LOL_v1.pth \
  --out models/hq/hq_<W>x<H>.onnx --h <H> --w <W> \
  --report docs/evidence/reports/M0-4/export_hq_<W>x<H>_lolv1.json \
  --input-png docs/evidence/reports/M0-4/gate_input_<W>x<H>.png \
  --dump-input docs/evidence/reports/M0-4/gate_input_<W>x<H>.npy \
  --dump-pt-ref docs/evidence/reports/M0-4/pt_ref_<W>x<H>.npy
python tools/convert_models.py build --onnx models/hq/hq_<W>x<H>.onnx \
  --out models/hq/hq_<W>x<H>_fp16_tk232.rknn --quant fp16 \
  --report docs/evidence/reports/M0-4/build_hq_<W>x<H>_fp16_tk232.json

# 板端推理（第 9 参 = dump 路径，第 10 参 = core_mask；core_mask=6 会被拒）
~/rknn_ab/probe/rknn_c_probe ~/rknn_ab/models/hq/hq_<W>x<H>_fp16_tk232.rknn \
  ~/rknn_ab/data/hq/in_<W>x<H>_nhwc.f32 5 nhwc float nosram f32 noperf \
  ~/rknn_ab/data/hq/out_hq_<W>x<H>.f32 6

# 判定（PC）
python tools/hq_eval.py gate --pt-ref docs/evidence/reports/M0-4/pt_ref_<W>x<H>.npy \
  --shape 1,3,<H>,<W> --tag <W>x<H> \
  --cand board=docs/evidence/reports/M0-4/out_hq_<W>x<H>.f32 --cand-layout nchw \
  --out-dir docs/evidence/reports/M0-4 --out docs/evidence/reports/M0-4/gate_board_<W>x<H>.json
```

---

## 8. 根因调查（v3.31，**进行中**）

### 8.1 已确立的三条

| # | 结论 | 证据 |
|---|---|---|
| 1 | **图被切得很碎，且只有注意力部分随分辨率增长**：总算子条目 1300 / 1876 / 2420（512/768/960）；`exMatMul` **332 / 692 / 1032**、`Add` **207 / 423 / 627**（随 H×W 增长），而 `Reshape 204` / `Conv 167` / `exNorm 88` / `Transpose 74` / `Split 48` / `ConvExGelu 72` **三档恒定** | `build_hq_*_fp16_tk232.log`（算子清单） |
| 2 | **"逐算子 FP16 舍入累积"不是误差来源**：在 PyTorch 里对 **118 个 leaf 模块的输出逐一做 FP16 舍入**（权重保持 FP32），误差仅 **max 2.166e-4 / mean 2.887e-5**，**比板端 4.604e-3 小 21 倍** ⇒ 长链累积解释**不成立** | 本档 §8.2 的作废实验同批（脚本见下） |
| 3 | **960×544 编译期有 22 处 `3Core → single` 核分配回落**，且运行时 mask=6 被拒（两模型一致）⇒ 核分配在**编译期**确定、运行时不可改 | `build_hq_960x544_fp16_tk232.log` + §7.2 的 mask 扫描 |

**§8.1-2 的复现脚本（内联，无新增工具）**：加载 arch + `LOL_v1.pth` → `xt = 输入/255` → `ref = net(xt)`（FP32）→ 用 `register_forward_hook` 对每个 leaf 模块输出做 `out.half().float()` → 再算 `max|ref−sim|`。

### 8.2 已排除 / 已作废（**不得作为证据引用**）

| 尝试 | 结果 | 处置 |
|---|---|---|
| "权重也舍入到 FP16"朴素模拟 | `max = 6.449e-1` | **作废**：朴素 `.half()` 会把 LayerNorm/Softmax 的 `eps` 类小常数在 FP16 下冲成 0 或次正规数 ⇒ 结果被假象污染。**RKNN 编译器并不这样做**——编译日志中 134 条 `need parameter qtype, type is set to float` 正说明**这些常数张量被保留在 float32** |
| PC 模拟器对照（`rknn_probe.py --backend sim`） | `load_rknn` + `init_runtime` 不支持模拟器 | **不可用**（toolkit2 要求 `load_onnx` + `build` 才能跑模拟器）；如需此叉须另写流程 |
| 逐层耗时剖析（`PERF_DETAIL`） | 表 383 行仅解析出 154 行 | **本轮不采信**该占比结论（解析噪声未解决），仅留原始表 `reports/M0-4/perf_detail_512.txt` 备查 |

### 8.3 收敛到的待查方向（尚未排除）

1. **算子近似实现**：NPU 对 `ConvExGelu`（GELU）与 Softmax/exp 常用查表或近似 —— 目前**最可疑**（24 个 `ConvExGelu` 层 + 注意力链）；
2. **中间累加精度**：NPU 内部是否 FP16 累加（而非 FP32）；
3. **编译期算子融合/重排**改变数值路径（含 `Reshape/Transpose/Split` 造成的中间物化）。

**下一步（已提议）**：**微模型逐算子归因** —— 把 `Conv3×3` / `Conv+GELU` / `LayerNorm` / `MatMul+Softmax` 各做成微型 ONNX，用**同一 FP16 配置**编译上板，逐算子量误差，以把上面三条分开。**在结果出来前，本档不对根因下结论。**

### 8.4 后续（v3.54 已执行）

上条的归因**已于 2026-09-19 执行完毕**，方法与结果见 **`docs/evidence/O35-hq-fp16-opattr.md`**（工具 `tools/opattr.py`，报告原档 `docs/evidence/reports/M0-4/opattr2/`）。结论摘要：

| §8.3 待查方向 | v3.54 判定 |
|---|---|
| ① 算子近似实现 | **部分成立**：`ConvExGelu` 是**次级**来源（max 超出 3.2×、**mean 超出 19.4×**）；`exSoftmax13` 接近纯舍入底（2.6×） |
| ② 中间累加精度（长归约） | **不成立**：`l2norm` 归约长度放大 16×（9216 → 147456）后**绝对**误差基本不变（1.65e-4 → 1.32e-4），远低于 1e-3 |
| ③ 编译期融合/重排 | **不必要**：主因已由**单个算子的直接测量**定位，无需诉诸不可直接观测的解释 |

**主因 = `exNorm` 的 LayerNorm 形态**（`nn.LayerNorm(40)`）：单算子板上偏差 **1.815e-2**（超出纯 FP16 舍入 **9.4×** / mean 5.8×），**其 9.4× 与整链 @512×288 的 9.4× 完全一致**，且单算子绝对偏差**大于**整链输出偏差（4.604e-3）。**已排除 `eps` / 方差约定不符**（受控对照：板上输出最接近正确约定 eps=1e-5 + 有偏方差）。**仍未定位**：`exMatMul`（68 层）的直接精度、三档 `max` 随分辨率增长的机制。

> **本档 §8.2 的作废项继续有效**（朴素 `.half()` 模拟不得引用）。**①b 阈值口径的处置仍待 §15 裁定**（O35 保持开放）。

---

## 9. 判定与影响（v3.31）

| 项 | 状态 | 说明 |
|---|---|---|
| **M0-4**（HQ 定版 + R9 闸口） | **不予定版** | 三档对齐全过（§7.1），**但板上 ①b 三档全不过**（§7.3）⇒ `hq.model_id` / `hq.active_shape` / `hq.pad_crop_policy` 若照现状定版等于默认接受"数值闸口不过"，**须先裁定**（**O35**） |
| **M0-5**（§6.2 耗时） | **实测已否定 ≤2 s** | 512×288 最小档 3.35 s（1.7×）、960×544 10.01 s（5.0×）；且是单核 AUTO 口径 ⇒ **§6.2 预算须重议**（**O37**） |
| **R9（重影）** | **无异常证据**（暂） | 结构指标不高于纯舍入对照、像素差 ≤2 LSB；**正式阈值标定**（§9.3 要求的"当天用 PT 对照 + 盲评共同标定"）**尚未做** |
| **M3-③**（HQ 三档对齐回归） | **提前预警** | 按现状跑会**不过**（阈取自 §11.2-①b 的 FP16 档） |
| **§6.3 内存 / §10.5 加载策略** | **需重议** | 960×544 单模型 internal+DMA ≈ **2.7 GB** ⇒ **三档不可能同时常驻**（**O37** 第 ② 子句） |
| **保底 CNN（O30）** | **仍挂起** | 未动；HD 路因此**当前没有任何退路**（既无过阈的正式模型，也无保底现货） |

**未决项（本轮登记，须裁定）**：**O35**（HQ FP16 板上误差超 ①b 阈 9~14×）、**O36**（`RKNN_NPU_CORE_1_2` 不可执行）、**O37**（§6.2 时间预算 + 三档常驻内存）。详见主基准 §15。

**报告索引（新增）**：`reports/M0-4/` 下的 `export_hq_960x544_lolv1.{json,log}`、`build_hq_{768x432,960x544}_fp16_tk232.{json,log}`、`gate_board_{512x288,768x432,960x544}.json`、`{512x288,768x432,960x544}_{board_out,board_diff20x,pt_ref,fp16_control}.png`、`out_hq_*.f32`（`.gitignore` 不入库）、`perf_detail_512.txt`。
