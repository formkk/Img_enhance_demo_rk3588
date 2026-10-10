# 立项草案：DCM 演示路（板端单帧 DCM 压缩画面 + 检测框）

> **地位**：**草案**（C 层记录类）。**未立项、未裁定、未启动**。
> **状态**：2026-10-10 用户指示「先出立项草案」⇒ 本档即该草案。
> **纪律**：本档**非验收依据**；**启动前须按 `00-ssot.md` 走裁定并新开 O 项**，且**不得从本草案直接推进实现**。

---

## 1. 缘起与目标

**用户诉求（2026-10-10）**：在**原有视频流输出**的基础上展示 DCM 技术——**点击实时路暗图 ⇒ 切入「DCM 演示路」**（交互仿现有 HQ），**展示一幅实时帧经 DCM 压缩后的画面，并叠加检测框**。

**前提（用户给定）**：
1. 跑 DCM 演示时 **HQ 不跑** ⇒ **NPU 有多核空闲**；
2. **允许照 HQ 的做法**：点击 → 冻结 → 计算 → 全屏展示（**单帧**，不要求实时）。

**已核实（本草案依据）**：

| 项 | 事实 | 出处 |
|---|---|---|
| 实时路 ZDE++ 分核 | `CORE_MASK_SLICE = (1, 2)` = **core0+core1（2 核）** | `curve_estim.py:39`（O29） |
| HQ 分核 | `HQ_CORE_MASK = 4` = **core2（1 核）** | `keyframe_infer.py:68`（O36） |
| 冻结语义 | HQ 期间实时链 `continue` **冻结**（O90） | `main.py` `_pump_hq_lifecycle` |
| ⇒ 可用核 | HQ 不跑 ⇒ 至少空 core2；若**沿用冻结**（实时链停）⇒ **core0+1 亦空（合计 3 核）** | 由上三条推得 |

⇒ **算力不再是阻塞项**；单帧背景预算可参照 HQ 的触发预算（§11 的 HQ 行）。

---

## 2. 方案总览（目标形态）

```
实时路（常规视图，暗图窗）
   │ 点击「增强窗/触发键」
   ▼
冻结（复用 HQ 的冻结/全屏语义，O90）
   ▼
取 1 帧（TH1 dmabuf，与 HQ 同源取得方式）
   ▼
板端 DCM 单帧链路：
   ① g_a + h_a（RKNN）→ 潜变量 y + 超先验 z
   ② 熵编码（CPU，真码流）→ 得 **真实字节数/码率**
   ③ 熵解码（CPU）→ ④ g_s（RKNN）→ 重建特征 x_hat
   ⑤ （可选）REC（RKNN）→ 重建像素图
   ⑥ 检测（板端轻量检测器 RKNN）→ 检测框
   ▼
全屏展示：**重建图 + 检测框 + 码率(bpp/kbps) + 档位**（仿 HQ 全屏）
   ▼
点击/超时退出（复用 `ui.fullscreen_timeout_ms` 语义）
```

**演示内容（建议）**：**同码率 A/B** —— 左「**暗光原图**经 DCM」vs 右「**我们的增强后**经 DCM」，各叠检测框 ⇒ 直观呈现"极暗光下先增强再编码，机器才看得懂"。

---

## 3. 技术路线（分阶段，S0 最便宜先行）

| 阶段 | 内容 | 动板子 | 产出 | 判据（可证伪） |
|---|---|---|---|---|
| **S0** ⭐ | 把 `g_a/h_a/g_s/h_s（+REC）` **导出 ONNX**，清点**算子清单** | ❌ | 算子清单 + 可导出性结论 | 全部算子落在 rknn-toolkit2 支持面内 ⇒ 才可进 S1 |
| **S1** | rknn-toolkit2 转换 + **与 PyTorch 数值对拍**（同 ZDE++ 的移植方法） | ❌ | 各网 RKNN + 对拍报告（最大偏差/逐位一致率） | 对拍偏差在可接受带内（阈值待裁） |
| **S2** | 板上**单帧**：编码 → **真码率** → 解码 → 重建图 | ✅ | 板上单帧跑通日志 + 重建 PNG | 重建图非空、码率为真实字节数 |
| **S3** | 接**板端轻量检测器**（RKNN）→ 叠框 | ✅ | 叠框图 | 框落位合理（目视） |
| **S4** | 接进 demo：点击 → 冻结 → DCM 演示路 → 全屏 | ✅ | 可点验的演示路 | 与 HQ 交互一致；§3.5 契约已裁定 |

**S0 先行的理由**：全案的**唯一"可能做不成"**在**算子可转换性/量化可行性**，而它最便宜验证（无需设备）。**S0 不过则本方案终止**，不投入后续。

---

## 4. 关键技术风险与对策

| # | 风险 | 说明 | 对策 |
|---|---|---|---|
| R1 | **RKNN 算子不支持**（最高） | DTM 网络含 **RNAB/CAB/TAM 注意力、ResAU 分组卷积、GDN、PixelShuffle、高斯混合概率模型** | **S0 先证伪**；不支持时的退路 = 等价替换（**可能需重训**，成本剧增 ⇒ 届时应重新裁定） |
| R2 | **量化精度损失** | 熵模型参数对量化敏感（ZDE++ 移植已有同类经验可循） | S1 对拍 + 必要时 FP16 档 |
| R3 | **熵编码需移植** | 主路径依赖 `models/entropy_models` + 高斯混合概率模型（torch 生态）；**rANS 是串行数据相关步骤，不能上 NPU** | 在 **CPU** 用 C++/纯 Python 重写**区间编码**；**必须产出真码流**（否则码率造假 ⇒ 违反 `10-evidence.md` §2 精神） |
| R4 | **检测器降级** | DCM 规范检测器 = detectron2 Faster R-CNN（RPN/ROIAlign/NMS，**转 RKNN 很难**） | 换**板端轻量检测器**（如 rknn_model_zoo 的 YOLO 系）；**界面/讲解须如实标注"检测器为板端轻量模型，非 DCM 规范检测器"** |
| R5 | **与 DTM RM 非逐位一致** | 板端为**移植版**（量化 + 简化熵编码） | 明确标注为"**板端移植版**"，**不得宣称与 DCM RM 一致**；如需对比，RM 结果另行离线产出（147） |
| R6 | **核与实时性冲突** | 演示占用 NPU；若**不冻结**实时链，则与 core0+1 冲突 | **沿用冻结语义**（实时链停）；不冻结方案不采纳 |

---

## 5. 契约与纪律影响（启动前必须处理）

| 面 | 影响 | 处置 |
|---|---|---|
| **§3.5 视图态** | 现为**契约两态**（`normal` \| `fullscreen_hq`）；新增「DCM 演示路」**可能构成新视态** | **须裁定**：或新增第三态，或设计为 `fullscreen_hq` 的"内容模式"以**免改契约**（优选后者，待评估） |
| **§5 契约** | 若新增视图态/新处理级 ⇒ 契约 A/B/C 节须同步 | 裁定后回写 |
| **§10.4 键表** | 预计涉及：档位（码率档）、超时（可复用 `ui.fullscreen_timeout_ms`）、检测阈值（可复用/新增） | **新增键须先入 §10.4**；能复用则复用 |
| **§11.2 实时线** | **不动**（实时路帧路径不涉） | — |
| **§13 判据** | 本项为**演示级功能**，**预计不改 §13** | 若定稿为验收项再议 |
| **立项** | 需**新开 O 项**（含风险 R1~R6、"如实标注"义务、S0 的终止判据） | 启动前完成 |

---

## 6. 待裁定的开放项（本草案不代拟）

1. **视图态形态** —— ✅ **已裁定（用户 2026-10-10）= 新增第三视图态「DCM 演示路」**（**不做成** `fullscreen_hq` 的内容模式）；**其名称 / 进入与退出 / 超时 / 画布布局等细则随 S4 定稿**（登记 = 主基准 §15 **O166**）。
2. **检测器选型**：具体用哪个板端轻量模型（精度/速度/许可）？
3. **档位**：DCM 质量档（对标 `--quality 0~31` 的哪几档）？演示默认档？
4. **A/B 是否同屏**：MIPI 放增强后、HDMI 放暗光原图（两根 HDMI 输出各一路）？
5. **一致性口径**：板端移植版与 DTM RM 的偏差允许多大（S1 对拍阈值）？
6. **S0 终止判据**：算子不支持到何种程度即终止本方案？

---

## 7. 环境与依赖前置

| 项 | 状态 |
|---|---|
| 离线导出/转换环境（S0/S1） | **开发机（本机）实测已具备**：Python **3.8.10** + **RTX 3090** + torch 2.0.1（DTM 要求 1.12.1 ⇒ **需对齐**）；详见 §8.1。数据机 `147_data` 亦为 3090，**但 FRP 仅 0.455 MB/s ⇒ 只适合离线** |
| rknn 工具链（S1/S2） | 板上/工具机需 `rknn-toolkit2` 与板上 `rknn-toolkit-lite`（**版本待查**） |
| DTM 缺件 | `packages/Environment/`（detectron2-0.5 / CompressAI）与 `Detector_model/model_final_68b088.pkl` **均缺**（见 `DTM/README.md` §4） |
| DTM 构建产物 | `build/*.so` 为作者 **py3.7 预编译**，须按本机 python 重编 |

---

## 8. 移植上板可行性评估（2026-10-10）

> 依据 = **离线代码清点**（只读，未改任何文件）+ **本机环境勘查**。**尚未做 ONNX 导出实测** ⇒ 结论**分级标注**（已确定 / 未确认），未闭合项见 §8.7。
> 清点对象 = **单帧编解码链路所必需的子网**：`g_a`（含 `E0ToE3CompressModel.g_a` + `CodingNet.g_a`，合计 16× 下采样）、`h_a`、`h_s`、`g_s`、`Context/MCM`、`hyper_scale_decoder`、`REC`。

### 8.1 环境（本机实测）

| 项 | 实测 | 对照 DTM 要求 |
|---|---|---|
| Python | **3.8.10** | ✅ 与 DTM 要求（3.8）一致 |
| GPU | **RTX 3090 / 24 GB** | ✅ 与 DTM 开发环境同类 |
| torch | **2.0.1+cu117** | ⚠️ DTM 要求 **1.12.1+cu116**（大版本差异 ⇒ 权重加载/导出算子行为可能不同） |
| 缺包 | cv2 / einops / bitstring / torchac / pycocotools / timm / detectron2 / compressai / onnx / rknn-toolkit2 | 需安装（多数为轻量包；detectron2、compressai、rknn-toolkit2 较重） |
| 磁盘 | `/` 可用 **95 G** | ✅ |
| ⚠️ 沙箱 | 本机执行受 **TRAE 沙箱限制**（部分路径不可写） | **装包/建环境可能需在沙箱外执行** |

⇒ **离线步骤（S0/S1）的环境基本具备**；唯一硬缺口 = **装包 + torch 版本对齐**。

### 8.2 算子清点（已确定）

**关键结论：主链路不含任何注意力 / 矩阵乘 / Softmax。** 注意力类（`TAM`/`RNAB`/`CAB`、`LayerNorm`、`MatMul`、`Softmax`）**只存在于未被主链路实例化的分支**（`src/codec/components/autoencoder_data/` 的 `EncoderBasic` / `DecoderHigh`）。主链路算子面：

| 类 | 算子 | 备注 |
|---|---|---|
| 卷积 | `Conv2d` / **Grouped Conv（groups=64 / 128）** / **Depthwise 分离卷积** / `ConvTranspose2d`(4×4 s2) | 占绝对多数 |
| 逐元素 | Add / Mul / Div / Sub / Abs / Clamp / LeakyReLU / ReLU / Sigmoid（仅瓶颈） | 常规 |
| 形状 | `PixelShuffle` / `Concat` / `chunk`(split) / `reshape` / `view` / `stack` / `Pad(replicate)` | ⚠️ 见 §8.6 |
| **整数定点** | **int32 卷积 + `bitwise_right_shift` + 逐通道 shift**（`hyper_scale_decoder`） | ⚠️ 最高风险 |
| 应剥离 | `torch.erfc`（`GaussianConditional` / `GMProbModel`）、`torch.cumsum`（CDF 构建） | **整体移到 CPU**，不进 NPU 图 |

### 8.3 形状与动态性（960×544 输入，已确定）

`pad → 960×576`；特征 **128×144×240**（1/4）→ `y` **128×36×60**（1/16）→ `z` **128×9×15**；`h_s` 输出 256×36×60；`g_s` 回 128×144×240；`REC` 出 **3×576×960**。
**固定输入下全静态**（无数据相关分支/循环）⇒ **ONNX 导出无动态 shape 障碍**。

### 8.4 熵编码链（已确定）

- 主链路用 **compressai 的 rANS**（`models/entropy_models/entropy_models.py` 内 `ans.RansEncoder/RansDecoder` + C++ 扩展 `compressai._CXX.pmf_to_quantized_cdf`）；**不是 torchac**（torchac 仅 CLIC 变体）。
- **可整体剥离到 CPU**：概率建模与索引生成全是 tensor 运算（可 CPU 重算），压缩产物 = rANS 字节串。
- **关键取舍**：
  - 要与 RM **码流逐字节一致** ⇒ 必须复刻 compressai/ans 的 rANS 字节布局（唯一 C++ 依赖点）；
  - 只要**可编可解 + 真实字节数** ⇒ **任何自写算术/区间编码器都可替代** ⇒ 移植难度**大幅下降**（与本草案 §4 **R5**「板端为移植版、不宣称与 RM 一致」口径一致）。

### 8.5 量化敏感点（已确定，直接影响码率/可解性）

| 量 | 产出环节 | 后果 |
|---|---|---|
| **`mean`** | `Context/MCM` 输出 | `round(y − mean)` 的均值；误差 **>0.5 直接翻转符号**；且**编解码两侧须逐元素一致** |
| **`scale`** | `hyper_scale_decoder`（**整数定点**） | 直接决定 CDF 索引 ⇒ **码率** |
| **`medians`** | `EntropyBottleneck.quantiles` | `round(z − medians)` ⇒ `z` 的符号与码率 |

⇒ RKNN 量化噪声**破坏三者任一** ⇒ **码率跳变或解码失败**。这是 S1 对拍**必须锁住**的部分。

### 8.6 风险排序与结论

| 序 | 风险点 | 依据 | 判定 |
|---|---|---|---|
| 1 | **整数定点 `hyper_scale_decoder`**（int32 卷积 + 位右移 + per-channel shift） | `models/Fix_point_layer.py` `IntConv2d_sigma`；ONNX/RKNN 对 int32 位运算支持差、量化会破坏整数语义 | ⚠️ **最高**；退路 = 改浮点等价实现（需对拍） |
| 2 | **`Context/MCM` 的 reshape/split/stack 记忆序** | `src/codec/components/contexts/utils.py` `down_shuffle/up_shuffle` | ⚠️ 高 |
| 3 | **`PixelShuffle` / `ConvTranspose2d`** | `subpel_conv3x3` 等 | 中高 |
| 4 | **`replicate` Pad + `math.ceil` 尺寸**（`base_layers/utils.py`） | 导出前须固化 h/w | 中 |
| 5 | `where` / STE clamp、`erfc` 误入图 | `models/rec/common.py`、`entropy_models.py` | 低-中；裁剪务必剥离 |

**结论（分置信度）**：

| 命题 | 判定 | 置信度 |
|---|---|---|
| 单帧 DCM 的**神经网络部分**可拆成可导出子模块 | **很可能可行** | 中-高（算子面干净、无动态 shape；**未实测导出**） |
| **熵编码**可在板上 CPU 实现（**真码流**） | **可行** | 高（自写区间编码即可，不强求与 RM 一致） |
| **重建像素 + 检测框**可在板上呈现 | **可行** | 高（REC 为常规卷积；检测换板端轻量模型） |
| 端到端**单帧**板端跑通 | **有望**，但取决于 §8.6 序 1/2 的转换与对拍结果 | 中 |
| **实时**（连续帧） | **不在本方案目标内** | — |

> **⇒ 评估结论**：方案**值得做、且大概率能做**；但 **序 1（整数定点子树）与序 2（MCM 的 reshape 链）是真正的关口**。**在 §8.7 实测完成前，本评估不构成"能做"的承诺。**

### 8.7 闭合本评估必须跑的实测（= S0/S1 的具体判据）

| 编号 | 实测 | 判据 |
|---|---|---|
| **S0-1** | 在 py3.8 + **torch 版本对齐** 的环境**逐模块导出 ONNX** | 记录**失败的算子/节点** |
| **S0-2** | `onnxruntime` 数值对拍（PyTorch vs ONNX） | **逐模块最大偏差** |
| **S0-3** | **单列 `hyper_scale_decoder` 的 int32 路径**能否导出 | **此项决定要不要改算法** |
| **S1-1** | rknn-toolkit2 转换 + 板端 `rknn-toolkit-lite` 推理 | **逐模块对拍** |
| **S1-2** | **三处量化敏感量（`mean` / `scale` / `medians`）** 的板端偏差 | 是否导致**码率跳变 / 解码失败** |

### 8.8 S0 实测结果（2026-10-10 已跑）—— **取代 §8.6 的推测性置信度**

- **脚本** = `DTM/code/s0_export_onnx.py`；**产物** = `DTM/out_s0/`（10 个 `.onnx` + `s0_report.json`）
- **环境** = `~/anaconda3/envs/dtm`（**克隆自 `rknn232`**，未污染原 env）：py3.8.20 + torch 2.0.1+cu117 + onnx 1.17 + onnxruntime 1.19.2 + **rknn-toolkit2 2.3.2**
- **口径** = 导出 opset 12 / batch=1 / **CPU 语义** / 固定 960×576 输入；对拍 = onnxruntime vs PyTorch

| # | 子网 | 导出 | 数值对拍（max abs） | 主要算子 |
|---|---|---|---|---|
| 01 | feature_extractor | ✅ | 4.9e-06 | Conv / LeakyRelu / Add / Mul |
| 02 | g_a | ✅ | 1.3e-03 | Conv / LeakyRelu / Add / Mul |
| 03 | g_a_sft | ✅ | 3.0e-08 | Conv / Relu / Mul / Add |
| 04 | hyper_encoder | ✅ | 1.7e-04 | Conv / **Pad / Slice / Transpose / Reshape** / Abs / Cast |
| 05 | hyper_decoder | ✅ | 3.3e-06 | Conv / **ConvTranspose×2** / LeakyRelu |
| **06** | **hyper_scale_decoder** | ❌ **FAIL** | — | **`aten::bitwise_right_shift` 导出不支持** ⇒ **关口 1 实测命中** |
| **07** | **context_mcm** | ⚠️ 导出成功但**模型非法**（ORT 加载 Type Error） | 无法对拍 | **Shape×51 / Gather×44 / Unsqueeze×182 / Reshape×59 / Concat×79 / Slice×14 / Split×18 / Cast×49** ⇒ **关口 2 实测命中** |
| 08 | g_s_sft | ✅ | 6.0e-08 | Conv / Relu / Mul / Sub |
| **09** | **g_s** | ✅（**原报"11.5"经查为假象**，见 §8.10） | **1.2e-04** | Conv / **DepthToSpace×2** / LeakyRelu |
| **10** | **rec** | ⚠️ 导出成功但**模型非法**（`Where` 类型绑定冲突） | 无法对拍 | Conv / **DepthToSpace×2** / Where / Greater / Less |

**结论（实测，取代推测）**：

1. **关口 1 与关口 2 双双实测命中**——且二者**恰是熵模型参数（`scale` / `mean`）的产出链**，即**编解码最核心的部分** ⇒ **"原封不动移植"走不通，必须改实现**。
2. **干净、可直接进 S1 的**：`01 / 02 / 03 / 05 / 08`（`04` 数值也对得上，但有 Pad/Slice/Transpose，需在 RKNN 侧观察）。
3. ~~两项待查~~ ⇒ **均已查清**：`09_g_s` 的"11.5"是 **`randn` 输入假象**（真实分布下 = 1.2e-04）；`10_rec` 的 `Where` 类型冲突**已修**（见 §8.10）。

### 8.9 **A-① 实测：`hyper_scale_decoder` 浮点等价（2026-10-10 已成）—— 关口 1 打通**

- **脚本** = `DTM/code/s0b_hsd_float_equiv.py`；**产物** = `DTM/out_s0/06b_hyper_scale_decoder_float.onnx` + `s0b_report.json`
- **做法**：原实现 = `clamp(±2^15) → int32 卷积 → 逐通道算术右移 >> s_c`（`s_c = clamp(per_channel_shift_c + (in_precision − out_precision), 0, 31)`）。**等价改写** = `y = floor( conv(x, W) / 2^{s_c} )`（**算术右移 ≡ 向下取整除法**），只用 `Conv / Div / Pow / Floor / Ceil / Where / Relu / Abs / Clip / DepthToSpace`，**无任何位运算**。

| 对比 | max abs diff | 逐位一致率 |
|---|---|---|
| PyTorch 定点 **vs** PyTorch 浮点等价 | **0.0** | **1.0000** |
| ONNX 浮点等价 **vs** PyTorch 定点 | **0.0** | **1.0000** |
| ONNX **vs** PyTorch 浮点等价 | **0.0** | — |

- **ONNX 算子表**：`Conv×3 / DepthToSpace×1 / Relu×2 / Abs / Clip×4 / Div×3 / Pow×3 / Floor×4 / Ceil / Where / Less / Reshape×3 / Cast×4`（**无位运算**）
- **⇒ 关口 1 打通**：原「导出即失败」的**唯一**一项，现**已可导出且与定点实现逐位一致**。
- ⚠️ **如实 caveat**：一致性在**单个随机输入**（ref 值域 0~366、`shift ∈ {0,1,2,3}`）上验证；float32 的整数精确性在 |值| < 2^24 内成立、本处余量极大，但**多输入 / 极值输入的鲁棒性尚未测**——留待 S1 随逐层对拍一并做。

### 8.10 **A-② 实测：修 `rec` + 查清 `g_s`（2026-10-10 已成）**

- **脚本** = `DTM/code/s0c_fix_rec_and_bisect_gs.py`；**产物** = `DTM/out_s0/10b_rec_clamp.onnx` + `s0c_report.json`

**(1) `rec` 的 `Where` 类型冲突 —— 已修（数学等价）**
- **根因（已定位）**：`models/rec/common.py:16-19` 的 `split_0to255` 写作 `torch.where(input > 255, 255, input)` —— 标量 `255`/`0` 是 **Python int**，与 float 张量在 ONNX `Where` 里**类型绑定冲突**（`Type parameter (T) of Optype (Where) bound to different types`）。
- **修法**：末级改为 **`clamp(0, 255)`**（`RecFixed`）⇒ ONNX 里 `Where/Greater/Less` 全部换成 `Clip`。
- **实测**：**PyTorch 原版 vs clamp 修版 = `0.0`（完全等价）**；导出 ONNX 后 ORT 可正常加载；ONNX vs PyTorch **max abs diff 1.9（rel 0.75%）**，出现在 0..255 的裁剪边界附近（20 层卷积的 float32 累积差异，**非结构问题**）。

**(2) `g_s` 的"11.5 偏差" —— 假象，已澄清**
- **根因**：S0 首版给 `g_s` 喂 `torch.randn`（**非真实分布**）。`ResAU` 的 `x·(1+mask)` 对 OOD 输入**剧烈放大** ⇒ 输出达 **±1.6e7**，此时绝对偏差 12 其实只有 **rel 7.3e-07**（纯 float32 噪声）。
- **实测（真实分布输入）**：输出值域 **[-31.7, 70.5]**、max abs diff **1.2e-04**（rel 2.2e-06）⇒ **`g_s` 无问题**。
- **处置**：`s0_export_onnx.py` 已改用真实分布输入（`randn` 假象消失）；逐子模块二分（7 段全过，0 ~ 2.9e-06）亦佐证。

**(3) 方法论副产品（影响**所有** ONNX 对拍，务必沿用）**
- DTM 多处 `nn.LeakyReLU(inplace=True)` ⇒ **调用与 `torch.onnx.export` 的 trace 都会就地改输入张量**；若 `ref` 与 ORT 复用同一批张量 ⇒ **假偏差**（实测曾把 `rec` 误报为差 **255**）。
- **处置**：**每个调用点各喂独立副本**（`s0_export_onnx.py` 与 `s0c` 均已修）。上表与本节数字均为修正后的**可信口径**。

**(4) 修正后的 S0 汇总（可信）**

| # | 子网 | 结果 | max abs diff |
|---|---|---|---|
| 01 | feature_extractor | ✅ | 6.8e-06 |
| 02 | g_a | ✅ | 1.5e-03 |
| 03 | g_a_sft | ✅ | 4.5e-08 |
| 04 | hyper_encoder | ✅ | 1.3e-04 |
| 05 | hyper_decoder | ✅ | 1.7e-06 |
| **06** | hyper_scale_decoder | **✅ 已修**（§8.9 float 等价，**0 偏差**） | — |
| **07** | **context_mcm** | ❌ **仍非法**（动态形状 / 记忆序） | — |
| 08 | g_s_sft | ✅ | 8.9e-08 |
| 09 | g_s | ✅（原 11.5 = 假象） | 1.2e-04 |
| **10** | **rec** | **✅ 已修**（clamp） | 1.9（rel 0.75%） |

> **⇒ 10 个里 9 个已可导出且数值可信；唯一剩下的硬骨头 = `07 context_mcm`。**

### 8.11 **A-③ 实测：`context_mcm` 静态化（2026-10-10 已成）—— 关口 2 打通（导出层）**

- **脚本** = `DTM/code/s0d_context_static.py`；**产物** = `DTM/out_s0/07b_context_mcm_static.onnx` + `s0d_report.json`
- **根因**：`ContextUtils.down_shuffle / up_shuffle` 用 `y.numel() >> 1`、`k = m // W` 与 `shape` 派生做 reshape/split/stack ⇒ 导出成一大片动态形状算子，且 ORT 加载即报 **Type Error**；`Context.forward` 另有 `F.pad(..., w%2, h%2)` 与**全 1 掩码写 0** —— 后者在本尺寸下是**空操作**。
- **静态化做法**（固定 `B=1, C=128, H=36, W=60`，**不改算法语义**）：
  1. 所有 reshape/split 尺寸改用 **Python int 常量**（`m = B*C*H*W//2`、`k = m//W`），**不用** `numel()`/`shape()`；
  2. 去掉两处**空操作**（`pad(0)` 与全 1 掩码）；
  3. `quantize` 等价为 **`round(clamp(x, int32min, int32max))`**（STE 的 `detach` 在推理/导出下等价于 `round`）。
- **实测**：

| 对比 | max abs diff |
|---|---|
| PyTorch 原 `Context` **vs** PyTorch 静态化（`mu`） | **0.0** |
| PyTorch 原 `Context` **vs** 静态化（`y_rec`） | **0.0** |
| ONNX 静态化 **vs** 原 `Context`（`mu` / `y_rec`） | **7.2e-07** |

- **算子对比（大幅收敛）**：

| | 原导出 | 静态化后 |
|---|---|---|
| 动态形状类 | `Shape×51 / Gather×44 / Unsqueeze×182 / ConstantOfShape×12 / Expand×7 / Tile×7 / ReduceProd×6 / Cast×49` | `Shape×1 / Gather×1 / Unsqueeze×24 / ConstantOfShape×8 / Expand×8 / Tile×8`（**ReduceProd / Cast 清零**） |
| 计算类 | `Conv×33 / Relu×18 / Concat×79 / Reshape×59 / Slice×14 / Split×18` | `Conv×33 / Relu×18 / Concat×29 / Reshape×46 / Slice×10 / Split×15` |
| **ORT 加载** | ❌ **Type Error（模型非法）** | ✅ **可加载并推理** |

> **⇒ 关口 2 在"导出层"打通**：模型合法、数值与原实现一致（7e-7）。
> ⚠️ **残留少量动态形状算子**（`Shape×1 / Gather×1 / Unsqueeze×24 / ConstantOfShape×8 / Expand×8 / Tile×8`）——**是否被 `rknn-toolkit2` 接受 = S1 的判据，尚未测**。

### 8.12 **S1 实测：`rknn-toolkit2` 2.3.2 转换（2026-10-10 已成）—— 10/10 通过（FP16）**

- **脚本** = `DTM/code/s1_convert_rknn.py`；**产物** = `DTM/out_s1/*.rknn` + `s1_report.json`
- **口径** = `rknn.config(target_platform="rk3588")` → `load_onnx` → **`build(do_quantization=False)`（FP16）** → `export_rknn`。**第一轮只看算子支持，不涉及量化**。工具链 = 本机 `rknn-toolkit2 2.3.2`（**与板端 `librknnrt 2.3.2` 同版本**）。

| # | 子网 | 转换 | `.rknn` 大小 |
|---|---|---|---|
| 01 | feature_extractor | ✅ | 1.89 MB |
| 02 | g_a | ✅ | 1.54 MB |
| 03 | g_a_sft | ✅ | 0.16 MB |
| 04 | hyper_encoder | ✅ | 1.62 MB |
| 05 | hyper_decoder | ✅ | 2.03 MB |
| 06b | hyper_scale_decoder（float 等价） | ✅ | 9.38 MB |
| 07b | context_mcm（静态化） | ✅ | 5.95 MB |
| 08 | g_s_sft | ✅ | 0.16 MB |
| 09 | g_s | ✅ | 4.29 MB |
| 10b | rec（clamp 修版） | ✅ | 7.91 MB |
| | **合计** | **10/10** | **≈ 34.9 MB** |

- **首轮曾报 `Ceil` 无 lowering**（`use CustomOperatorLower`）—— 出自 `06b` 里为实现 `trunc` 等价写的 `where + ceil/floor`。**已改为纯 `Floor`**（该网输入恒为 `round(z)` 后的整数值 ⇒ trunc / floor / 恒等三者等价），**重出后 0 偏差保持、告警消失**。

> **⇒ S1 结论：10 个子网全部可转 RKNN（FP16），无算子降级告警。**

⚠️ **必须点明的边界（这不算"过关口"）**：
1. **只验证了"转换成功"，未验证"板上能跑"**（**S2 未做**）：需在板上用 `librknnrt 2.3.2` 实际推理并与 PyTorch **对拍**；
2. **只做了 FP16**：**INT8 量化未测**——而 `mean / scale / medians` 三处是量化敏感量（§8.5）；
3. 板端未装 `rknnlite` ⇒ 推理需走 **RKNPU2 C API**（或先装 `rknn_toolkit_lite2`，cp39）；
4. 板端存储 44 GB 可用 ⇒ 35 MB 模型无压力；**实时性未测**。

### 8.13 **S2 实测：板上跑 + 逐网对拍（2026-10-10 已成）—— 7/10 数值正常，3 个需处理**

- **板端环境**：板 python3.9 **无 venv 能力**（缺 `ensurepip`）⇒ 改用 **`pip3 install --target /home/cat/dcm_s2/pylibs`**（numpy 2.0.2 + `rknn_toolkit_lite2 2.3.2`(cp39) + `ruamel.yaml` + `psutil`）。
- **侵入面**：全部落在 **`/home/cat/dcm_s2/`** 一个目录内 ⇒ **不影响 `starlight-demo`、不碰视频流、不改 systemd**；**回退 = `rm -rf /home/cat/dcm_s2`**（零外部残留）。
- **运行日志回显 `librknnrt 2.3.2 (429f97ae6b@2025-04-09)`** ⇒ **与 PC 端 toolkit2 同版本** ✓。
- **流程**：`s2_prepare_io.py`（本机：固化同一份输入 + PyTorch 参考）→ scp（**只传模型与输入，不传参考**）→ `s2_board_infer.py`（板端 `rknnlite`）→ 回传 → `s2_compare.py`。

| # | 子网 | max abs | **corr** | 判定 |
|---|---|---|---|---|
| 01 | feature_extractor | 6.6e-03 | **1.0000** | ✅ |
| 02 | g_a | 1.14 | — | ✅（rel 7.2e-04） |
| 03 | g_a_sft | 3.7e-04 | — | ✅ |
| 04 | hyper_encoder | 7.1e-02 | — | ✅（rel 4.5e-04） |
| 05 | hyper_decoder | 2.0e-03 | — | ✅ |
| **06b** | hyper_scale_decoder | 6.0 | **0.9999** | ⚠️ **可用但偏大**（max rel **1.6%**，其余网仅 ~1e-3） |
| **07b** | **context_mcm** | 1.21 / 0.89 | **0.9248 / 0.9585** | ❌ **降质**（误差与信号同量级；`mu` 偏差 >0.5 即翻转熵符号） |
| 08 | g_s_sft | 5.0e-04 | — | ✅ |
| 09 | g_s | 0.115 | — | ✅（rel 1.8e-03） |
| **10b** | **rec** | 255.0 | **0.1367** | ⚠️ **本行已作废**（**夹具口径错误**：漏解 `g_s_sft` ⇒ 输入 OOD）⇒ **纠正见 §8.15** |

> **⇒ S2 结论**
> 1. **"转换成功 ≠ 板上正确"**：实测 **7/10 数值正常**（corr≈1，rel ~1e-3，符合 FP16 预期）；
> 2. **坏的恰是"熵模型参数链"与像素重建**：`07b`（产出 `mu`）降质、`10b`（产像素）**结构错乱**；
> 3. `10b` **corr=0.14 而形状正确**，~~最像 RKNN 内部 NHWC 布局下 reshape / DepthToSpace 的记忆序漂移~~ → **该推测已被 §8.14 实测证伪**（真因 = **FP16 动态范围溢出**）；
> 4. `06b` 的 max rel 1.6% 显著高于另 9 网，需关注（`Div/Pow/Floor` 在 FP16 下的整数语义退化）。

⚠️ **未决（如实）**：以上为**单输入一次性实测**；**根因未定位**（**尚未做板上子图二分**），也**未试 INT8**。

### 8.14 **S3 实测：板上子图二分定位根因（2026-10-10 已成）**（⚠️ 其中 **10b 部分已作废** ⇒ 见 §8.15）

- **脚本** = `s3_bisect_prepare.py`（本机：切段 → 导出 ONNX → 转 RKNN → 落 IO）/ `s3_board_infer.py`（板端）/ `s3_compare.py`；**产物** = `DTM/out_s3/`
- **切法**：`10b`（rec）按 `head/body/tail` 的**每个子模块**切；`07b`（context）**直击三个原语**（`down_shuffle` / `up_shuffle` / `MCM_phase1`）+ 整段 `slice0` + `full`。共 17 段，**16 段板上跑通**（`07b_prim_phase1_stage0` 因 **3 输入**取张量属性失败，未跑成）。

**(1) `10b`（rec）根因 = **FP16 动态范围溢出**（**不是**记忆序/布局）**

| 段 | PyTorch 参考值域 | 板上 RKNN | 判定 |
|---|---|---|---|
| `body0_DSConv` | −2.64e4 ~ **5.19e4**（< 65504） | 同范围，**无 inf** | ✅ rel 5.0e-4 |
| `body1_DSConv` | −1.89e4 ~ **6.98e4**（**越界**） | max=**inf**（4 个） | ❌ **溢出起点** |
| `body2_ResAU64` | −5.6e8 ~ 3.8e8 | **inf × 7,600,621** | ❌ |
| `tail0` / `tail2` | ±1.8e9 / ±1.2e9 | 大面积 inf | ❌ |
| `tail9_clamp` | 0 ~ 255 | 0 ~ 255（**inf→clamp→255**） | 表面正常 |

- **FP16 上限 = 65504**；rec 内部激活达 **1e8 ~ 1e9** ⇒ **板上溢出为 inf** ⇒ 末级 `clamp(0,255)` 把 inf 变 255 ⇒ **输出"无 NaN 但结构错乱"** —— 与 §8.13 中 `10b` **corr=0.1367** 的现象**完全吻合**。
- ⇒ **10b 的根因不是"记忆序"，而是 FP16 数值范围**（该网条件数差、内部量级巨大）。

**(2) `07b`（context）：**"记忆序"假设被证伪**

| 段 | rel | corr |
|---|---|---|
| `07b_prim_down_shuffle_y`（4 路输出） | 3.4e-4 ~ 4.0e-4 | **0.999999977 ~ 0.999999979** |
| `07b_prim_up_shuffle` | 3.4e-4 | **0.999999978** |
| `07b_full` | 1.03 / 1.21 | **0.9506 / 0.9732** |

- ⇒ **RKNN 对这套 `reshape/split/stack` 的处理是正确的**（原语级 corr 0.99999998）——**我此前"NHWC 记忆序漂移"的推测被实测证伪**（这正是二分法的价值）。
- `07b_full` 仍降质 ⇒ **发散在 phases 或"逐级增长"路径**，**尚未定位**（`phase1_stage0` 未跑成）。

**⇒ S3 结论**
1. **两个根的"坏"是两种不同病**：`10b` = **FP16 溢出**（已定性、证据充分）；`07b` = **降质但未定位**（原语清白 ⇒ 嫌疑在 phase 网或其输入增长路径）；
2. **两者都不是"算子不支持"或"记忆序"** ⇒ **修复方向随之明确**：
   - `10b`：**per-layer / per-channel 缩放**（把内部张量压回 FP16 范围，数学等价）或改结构；亦可试 INT8；
   - `07b`：继续二分（把 3 输入包成 1 个输入以便 RKNN 属性查询）。

### 8.15 **S4 纠正：`10b`(rec) 的"FP16 溢出"是夹具错误，板上实际完全正常**

**(一) 纠错经过**
1. 追查"rec 输出饱和 88%、内部量级 1e9"时发现**参考输出本身就不合理**（均值 225）⇒ 先查输入域 → 读 `tools/main.py` 解码链：
   `feature_out = decode_model(...)`，其中 `x_hat = g_s( g_s_sft( y_hat, q_map_decoder ) )`
   ⇒ **解码端有一步反 SFT `g_s_sft`**，而我在 S0/S2/S3 里用的是 **`g_s(y)`（漏了 `g_s_sft`）** ⇒ **输入不在模型训练域内**。
2. 逐项排除其它可能：`model_rec.pt` 的 **22 个键全部形状一致、全部载入**（含 `tail.*`）⇒ **权重无问题**。

**(二) 纠正后实测（本机，`s4_rec_domain_probe.py`）**

| rec 内部 | 错误口径 `g_s(y)` | **正确口径 `g_s(g_s_sft(y))`** |
|---|---|---|
| `x_hat` 值域 | ±70 | **[-0.31, 0.78]** |
| `head1` / `body2` / `tail0` | 7.8e3 / **5.9e8** / **2.5e9** | 0.72 / 13.1 / 105.7 |
| 末级 `clamp(0,255)` | 均值 225.5、**饱和 88%** | 均值 **141.6**、**饱和 0%** |
| 判定 | ✗ 全链路**远超 FP16 上限 65504** | ✓ **全链路 ≤ ±200 ⇒ 完全在 FP16 范围内** |

**(三) 板上复测（正确口径；`s4_10b_recheck.py` 重写 IO 后重跑）**

| | ref (PyTorch) | board (RKNN) |
|---|---|---|
| 值域 | 112.39 ~ 160.58 | 112.44 ~ 160.62 |
| 均值 | 139.53 | 139.54 |
| **inf 数** | 0 | **0** |
| **max_abs / rel / corr** | — | **0.122 / 7.6e-04 / 0.99999095** |

> **⇒ `10b`(rec) 在板上完全正常**（FP16 误差 ~7.6e-04，与其它网同量级）。**"FP16 溢出"根本不存在、无需修复**；**§8.13 与 §8.14 中关于 `10b` 的失败与"根因"全部作废**。

**(四) 教训（如实归档）**
- **对拍前必须先确认"输入在该模型的训练域内"**；本次错在**照搬编码端链路去喂解码端模块**（漏 `g_s_sft`）；
- **"参考输出本身不合理"（饱和 88%）本应是第一预警**，我却先去怀疑板子与量化 —— **排查顺序错了**；
- ⇒ **S2 结论应修正为：板上 **9/10** 数值正常，待查仅剩 `07b`**（`07b` 输入取自**编码端** `y=g_a_sft(g_a(x))`、`psi=hyper_decoder(round(z))`，与 `compress()` 一致 ⇒ 其降质结论**暂时成立**，但**同理仍需复核口径**）。

### 8.16 **S5/S6：`07b`(context/MCM) 长尾根因定位 = FP16 下 `round()` 的平局跳变（tie-breaking）**

> 承 §8.15：板上 9/10 网正常，待查仅剩 `07b`（整网 corr 0.958、`mu` 偏差 >0.5 占 **0.040%**）。本轮按"**先复核口径、再谈根因**"的顺序做（S4 教训）。

**(一) S5 口径复核 —— 结论：口径无问题**

① **调用链逐项对照**（源码 = `tools/main.py` + `models/MIK_MCM_codecs.py`）：

| 环节 | 源码 | 本套喂法 | 一致 |
|---|---|---|---|
| `compress` 收到的 `x` | `MIKImageEncoder.forward`: `features = feature_extractor(image)` → `feature_coding.compress(features, …)` | `fe(img)` | ✅ |
| 输入1 `y` | `y = g_a(x)`；`y = g_a_sft(y, q_map_encoder)` | 同 | ✅ |
| `z_hat` / `psi` | `z_hat = round(z)`；`psi = hyper_decoder(z_hat, h=576, w=960)` | 同 | ✅ |
| 输入2 | `context(y=y, hyper_params=psi)`（**只在编码端如此调用**；解码端走 `context.decompress`） | 同 | ✅ |
| `quantize_func` | `partial(self.quantize)`（**无** `gvae_params`） | 同 | ✅ |

② **数值域**（两种输入都在域内，**无 10b 那种爆炸**）：

| 输入来源 | `y` | `psi` | `mu`(输出) |
|---|---|---|---|
| **真实图**（`logs/samples/000000481386.jpg`，/255 + 补零） | −2.63 ~ 2.50 | −0.0089 ~ 0.378 | −2.91 ~ 2.13 |
| 随机图（S2/S3 所用） | −0.81 ~ 0.72 | −0.0063 ~ 0.311 | −0.80 ~ 0.80 |

③ **导出图是否忠实**（本机 ORT vs PyTorch）：

| 输入 | 静态化 vs 原 PyTorch | **ONNX(ORT) vs 原 PyTorch** |
|---|---|---|
| 真实图 | `max_abs = 0` | **rel 8.2e-07，corr 1.00000000** |
| 随机图 | `max_abs = 0` | **rel 1.2e-06，corr 1.00000000** |

⇒ **口径与导出都清白**；差异**只出现在板上 RKNN**。

**(二) S6 phase 级二分**（把 3 输入打包成 1 个输入以绕过 RKNN 3 输入属性查询失败；脚本 `s6_07b_phase_bisect.py`）

| 段 | max_abs | rel | corr | 判定 |
|---|---|---|---|---|
| `07b_p0_phase` ~ `p3_phase` | 4.5e-04 ~ 7.3e-04 | 5.2e-04 ~ 8.4e-04 | **1.000000** | ✅ phase 网干净 |
| `07b_round_only` | **0** | — | — | ✅ 该输入下 **0/69120 跳变** |
| **`07b_cum1`**（跑到 stage0） | **1.000** | 1.15 | 0.978 | ❌ **恰好 ±1** |
| `07b_cum2` | 1.000 | ~1.0 | 0.992 / 0.985 | ❌ |
| `07b_cum3` | 1.103 | 1.02 | 0.984 | ❌ |

**(三) 推理链（根因）**

1. 4 个 phase 单独跑**全部 `corr = 1.000000`**、相对误差 ~5e-4（正常 FP16）⇒ **phase 网本身没问题**；
2. `round` 单测**零跳变** ⇒ `round` 算子本身也不发散；
3. 但 **`cum1` = "phase0 + round + 加法" 一跑就出 `max_abs = 恰好 1.000`** —— **±1 的整数级跳变**；连续的 FP16 误差只可能给 ~5e-4 量级，**不可能直接产生 1.0** ⇒ 只可能源自 **`round()` 在 .5 边界上的平局判定被 FP16 微小差推到另一侧**；
4. 发生率极低：**69120 个元素里 1 个（0.0014%）**，与 `mu` 越阈值点占比 **0.040%** 同量级 ✓ 自洽；
5. 该 ±1 误差经 `spatial` 拼接进入后续 stage，被卷积平滑后放大为 max 1.103 ⇒ **就是那条 0.04% 的长尾**。

**(四) 结论**

**根因 = FP16 精度下 `round()` 的固有平局效应（tie-breaking），单一机制**：
- **不可通过改图消除**（只能提高该处精度，FP16 NPU 无此选项）；
- **无害**：编解码两侧跑**同一张网 ⇒ 结果逐位一致 ⇒ 不会解码失败**；对码率影响 = **0.04% 符号翻转**。

⇒ **`07b` 判定从"降质待查"正式改为「可用」**。

**(五) 附：输入顺序交换测试（假设 1 被排除）**

`07b` 是 2 输入模型 ⇒ 先排除"rknnlite 取输入顺序错"。实测：`normal`（`y`,`psi`）✅ 跑通并**复现 S2 数字**（`y_rec` corr 0.924839 / `mu` corr 0.958498）；`swapped`（`psi`,`y`）❌ **被 RKNN 拒绝** `RKNN_ERR_PARAM_INVALID`（通道数 **128 ≠ 256**，形状不符 ⇒ 不可能被静默交换）。⇒ **输入顺序不是原因**。

**(六) 产物与脚本**

脚本 = `DTM/code/s5_07b_domain_check.py` / `s6_07b_phase_bisect.py` + 复用 `s3_board_infer.py`；产物 = `DTM/out_s6/`（8 段 onnx/rknn/IO，**已 gitignore**）；板端 = `/home/cat/dcm_s4/`（**试验用，可 `rm -rf` 回退**）。

---

### 8.17 **S7/S8/S9：真码流 + 熵编码搬板 + 板端单帧端到端（2026-10-10 已成）**

> 承 §8.16：`07b` 已不构成阻塞 ⇒ 本轮做三件事：**① 离线真码流 + 端到端单帧；② 熵编码搬到板上；③ 11 个网 + 熵编码在板上跑通单帧**。
> 口径声明：与 RM 的对比基线 = **本机 PyTorch FP32**（`torch 2.0.1`）；板上 = **RKNN FP16**（`rknn-toolkit2`/`librknnrt` **2.3.2**）。

**(一) S7（离线，开发机）：真码流 + 端到端单帧贯通**

- 熵编码走 **DTM 自带 rANS**（`compressai.ans`，**非** torchac）⇒ **真码流**：**y = 1612 B + z = 100 B = 1712 B**；真码率 **0.02623 bpp**（960×544）/ **0.02642**（960×540）/ 0.00660（1920×1080）。
- 端到端（含 REC）单帧 PC 侧 ≈ **1.5 s**；真码流往返一致性 **max_abs = 0**（熵编解码无损）。
- 产物 = [out_s7](file:///home/jenkolux/PKU_WH/Img_enhance_demo/DTM/out_s7)（`input.png` / `rec_from_bitstream.png` / `y_strings.npy` / `z_strings.npy`）；脚本 = `s7a_entropy_roundtrip.py` / `s7b_e2e_oneframe.py`。

**(二) S8（板上）：熵编码搬板 = 逐字节复现 RM**

- DTM 自带 rANS 的 **C++ 源码**（`data_compression/cpp_exts/rans/rans_interface.{cpp,hpp}` + `ryg_rans/*.h`）用板上 **g++ 10.2.1** 编成 **`rans.cpython-39-aarch64-linux-gnu.so`**（板 python **3.9.2**；头文件取自板上已解包的 `~/pyheaders/root/usr/include`，**未装系统 dev 包**）。
- 板上用上一轮捕获的编码器入参跑：**v0 = 1612 B / v1 = 100 B，两者 `byte_equal=True`、`roundtrip=True`** ⇒ **与 RM 的 `compressai.ans` 逐字节一致**。
- 两处口径坑（上一轮捕获遗留，本轮修）：
  1. `encode_with_indexes` 的 `cdfs` 必须是**逐行二维** list（上一轮落盘时被**扁平化**成一维 ⇒ 直接喂会被 pybind11 判 `TypeError`）；行宽 = `cdf.size / len(cdf_length)`（v0 = 3133、v1 = 23）。
  2. `decode_with_indexes` 是 **5 参**（`encoded, indexes, cdfs, cdfs_sizes, offsets`），**无 `shape`**。
- 速度：y 流 **编码 0.06 s / 解码 0.04 s**；z 流 <0.01 s。

**(三) S9（板上）：11 个网 + CPU rANS 的单帧端到端**

- **网数 10 → 11**：新增 **`07d`**（`context.decompress` 的**解码分支**静态版；`forward` 的 `07b` 不是它）与 **`07e`**（只输出 `mu` 的单输出网）；11 个 **FP16** 网全部在板上跑通。
- 单帧耗时（960×576 模型输入，**单帧、不要求实时**）= fe 0.116 + (g_a+g_a_sft) 0.141 + h_a 0.008 + h_s 0.007 + hsd 0.033 + 07e 0.051 + rANS 编 0.088 + rANS 解 0.123 + (07d+08+09+10b) 0.561 ≈ **1.13 s**。
- **z 流与 RM 逐字节一致**（100 B；`sym_z` 不符 **0** 处）；**y 流 3512 B vs RM 1612 B（2.18×）**，`sym_y` 不符 **3517 / 276480 = 1.27%**。
- 重建图 vs RM(FP32)：**`mean|Δpix| ≈ 18.95/255`**、整体**偏亮 +10.8**、corr 0.717（目视 = 同景、更亮更糊）。
- 产物 = [out_s9/board_out](file:///home/jenkolux/PKU_WH/Img_enhance_demo/DTM/out_s9/board_out)（`input.png` / `pc_ref_rec.png` / `board_rec.png` / `board_report.json`）；脚本 = `s9a_prepare_assets.py` / `s9b_export_07d.py` / `s9c_export_07e_means.py` / `s9_board_e2e.py`；板端 = `/home/cat/dcm_s8/`（**试验用，`rm -rf` 全回退**）。

**(四) 差异根因（已定位；**非**接线错误、**非**转换错误）**

1. **`07b` 的 2 输出在 RKNN 上退化为同一份** —— ORT 下 `y_rec`/`mu` 的 `maxdiff = 1.0`，但板上 `out0 == out1`（**逐位相等，都是 `mu`**）⇒ `y_rec` 输出丢失。⚠️ 因此 **§8.16(五) 与 S2/S3 用 `out_1` 当 `mu` 的对照，实际取到的仍是 `mu`**（本轮复核：`out_0`/`out_1` 对 `ref_mu` 均 corr **0.958498**、对 `ref_y_rec` 均 0.924839）⇒ **板端 `mu` 一直是对的**，该退化对本链路**无影响**；已另导单输出 `07e`/`07d` 规避（结果与用 `07b` **完全一致**）。
2. **FP16 误差被 MCM 内 8 级级联 `round()` 放大（真正的根因）** —— 把**与 PC 完全相同**的 `(y, psi)` 喂板上 `07e`：`mu` 仍差 **`mean|Δ| = 0.031`**、`|Δ| > 0.5` 占 **0.84%**、`|Δ| ≤ 0.01` 占 50.9%、**逐位相等 = 0%**（= FP16 噪声 everywhere + 平局翻转 few）。这是 §8.16 机制的**放大版**：§8.16 的 **0.04%** 是"同输入、单点"估计；**全链级联 8 次 `round` 后升到 ~1.3%**（1.27% 的 `y` 符号翻转与 0.84%~1.3% 同量级，自洽）。
3. **该误差经 `g_s → REC` 放大，但下游本身是干净的** —— 把**板端 `y_hat`** 喂回 **PC 的 FP32 下游**（08 → 09 → REC），**复现出同一张板端重建图**（`mean|Δ| = 0.035`、corr **0.999999**），且**同样复现对 RM 的 18.95 偏差** ⇒ **板上 08/09/10b 与熵编码数值都正确**，偏差**全部来自 `y_hat` 的误差**（REC 内部动态范围极大 ⇒ 对 `y_hat` 的微小差高度敏感；该敏感性在 FP32 下**同样存在**）。
4. ⇒ **结论**：**"跑通"成立**（真码流 + 真重建 + 板上 CPU 熵编码逐字节复现）；但**板上 FP16 移植版与 RM 的一致性有明显差距**（码率 **2.18×**、重建**可见偏差**），属 §4 **R5** 已登记的"**移植版 ≠ RM**"，并新增一条**机制性事实**：**MCM context 的 8 级级联 `round()` 对 FP16 误差高度敏感**（§8.16 的"无害"结论只对**单点同输入**成立，**不可外推到全链**）。

**(五) 待裁定（新增，随 O166）**

- 是否接受"板端 FP16 移植版"的一致性差距（码率 2.18× / 重建可见偏差）？
- 若要求更接近 RM，可选项 = **把 MCM context（`07e` / `07d`）改在板端 CPU 以 FP32 运行**（单帧可承受，代价未评估）；**须先裁定再动手**。

---

## 9. 结论

**本草案仅定义方案与风险，不构成启动。** 启动前置条件：
1. 用户对 §6 的开放项作出裁定；
2. 按 `00-ssot.md` **正式立项**（新开 O 项 + 回写 §14/§15 + §10.3 若新增代码模块）；
3. 从 **S0** 开始，S0 不过即终止。

**风险提示（如实）**：本方案的技术可行性**尚**未经任何实测；**R1（算子可转换性）是唯一可能"做不成"的点**，故以最便宜的 S0 先证伪，**不作乐观承诺**。
