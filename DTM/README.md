# DTM（DCM 参考软件）资源归档

> **地位**：本目录 = **上游第三方参考软件资源 + 理解笔记**（**非本工程资产**）。
> **状态**：**已归档，暂不开展**（2026-10-10 用户裁定）。**结论性取舍见 §7**。
> **纪律**：本笔记**非验收依据**；引用 DTM 的任何取值一律回其上游文档/代码，本笔记只作索引。

## 0. 来源与校验

| 项 | 值 |
|---|---|
| 文件 | `DTM/DTM-V-1.5.zip` + 展开树 `DTM/DTM-V-1.5/` |
| 上游 | 数据机 SSH 别名 **`147_data`**（`MQZX3090`）`/home/jiangfeng/jf_app/DTM-V-1.5.zip` |
| 拉取 | 2026-10-10，经公网 FRP `117.72.39.181:2223`，**145 s**（≈0.46 MB/s） |
| 校验 | 66,632,346 B；md5 `f29ed4de17efb4c44617e543f1df19dc`（**源 ≡ 本机**）✓ |
| 入库 | **整个上游包不入库**（`.gitignore`：`DTM/*.zip` + `DTM/DTM-V-1.5/`）；**本目录的自建文件入库** |

## 1. 它是什么

**DTM-V 1.5 = DCM 的参考软件（RM）**；DCM 是**国内对标 MPEG-VCM（Video Coding for Machines）**的工作。
范式是**编码的用户从"人"换成"机器"**：

| | 传统视频编码 | DTM-V（面向机器） |
|---|---|---|
| 优化目标 | 人眼观感（PSNR/SSIM/VMAF） | **任务性能/码率**（mAP–bpp、准确率–bpp） |
| 保真对象 | 像素 | **特征/语义**（主路径保 **res2 级、stride 4、128 通道**特征） |
| 双出口 | 只有像素 | **特征**（喂任务网）＋（可选）**像素重建**（REC） |
| 内容级取舍 | 无 | **语义结构化**：只编前景块（detectron2 检出 bbox） |

版本史：v1.0→v1.5 逐版并入 **DCM-I-122/123/124、148/149/150、162、170、179、189** 提案（见上游 `README.md`）。

## 2. 数据流（要点）

**编码**（上游 `tools/main.py` `SemanticStructuredFeatureCoding_MCM.compress`）
`读图/缩放/pad 到 64 对齐 → 特征提取器 E0ToE3（3ch→128ch, stride4）→ CodingNet_Mean_MCM_Scale_hqq_v2（g_a 降到 stride16 得 y；超先验得 z；熵编码）→ 写码流`

**解码**
`读头 → 解 y/z → 重建特征 x_hat(128ch) → 存 res2_*.pt 供任务网用 →（可选）REC 重建像素`

## 3. 关键结构速查（上游路径）

| 关注点 | 位置 |
|---|---|
| 入口（`--mode compress\|decompress`） | `tools/main.py` |
| 主用特征编解码器 | `models/MIK_MCM_codecs.py`（`CodingNet_Mean_MCM_Scale_hqq_v2`） |
| 图像 VBR 编解码（旁路） | `models/CLIC_based_codecs.py` |
| 像素重建 | `models/rec/rec.py` |
| 激活（ResAU/GDN/ResA） | `models/resau.py`、`src/codec/components/activations/` |
| **推理期定点卷积**（为部署铺路） | `models/Fix_point_layer.py` |
| **码流语法** | `tools/main.py`（`bsFormatIH1Enc` / `IF1` / `IRE`） |
| 熵编码栈 1（**MCM 主路径**） | `models/entropy_models/` + `src/codec/components/entropy_coding/prob_models/gm.py` |
| 熵编码栈 2（图像旁路，**源自 CompressAI**，C++ rANS） | `data_compression/`（`cpp_exts/ryg_rans`） |
| 任务推理（特征域 / 像素域） | `scripts/DCM_Datasets_Feature_Inference_Scripts/`（detectron2, mAP）、`.../Image_Inference_Scripts/Hyperlpr/`（车牌, 准确率） |

**码流要点（易错）**：`IH1`(start `0x00000180`) →〔可选 `IH2`+结构数据〕→ `IF1`(**`rate_control_q_id` uint5 + [z 长度]z[y 长度]y**，**写序 z→y**、每段 uint32 显式记比特长) →〔可选 `IRE`〕。

**质量档**：`--quality 0~31` **不是质量等级**，是 SFT 量化因子索引（线性映射 **0.200→0.870**）；与代码里硬编码的 `build_feature_coding_Mean_MCM_Scale(quality=3)`（**结构档**）**不是一回事**。

## 4. 权重与依赖（⚠️ 有缺件）

| 项 | 状态 |
|---|---|
| `packages/Model/MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth` | ✅ 主特征编解码器 |
| `packages/Model/REC_model/model_rec.pt` | ✅ 像素重建 |
| `packages/Model/Rate_Control_model/rate_control.pth` | ✅ 率控 |
| `packages/Model/Detector_model/` | ❌ **空占位**（需外下 `model_final_68b088.pkl`） |
| `packages/Environment/` | ❌ **空**（detectron2-0.5 / CompressAI 需自行下载） |
| 依赖 | **python 3.8**、`torch==1.12.1+cu116`、`torchac`、`opencv-python`、`pycocotools` |

## 5. 可移植性坑（已确认）

1. **硬编码路径遍地**：`requirements.txt`（`/data1/fengry/...`）、`evaluation/*.sh`（`/work/Users/jiake/...`）、`scripts/**`（`/Your/Path/...`）。
2. **强假设 CUDA**（`--device gpu` 默认、多处 `torch.device("cuda")`）。
3. **python 版本混杂**：`build/` 里 `.so` 是**作者机器 py3.7 预编译**（`/data3/fengry/DCM/imcl_decoder/...`）、`.pyc` 混 38/39 ⇒ **换解释器必须重编**。
4. **离线失效风险**：HyperLPR 会尝试联网拉模型。
5. 噪音文件：`.idea/`、`logs/samples/*.jpg`、`dtmV_output/` 样例、`tools/ssic.py`（早期重复版）、`scripts/.../124/`（历史脚本）。

## 6. 与本演示项目的互补性

| 我们的资产 | DTM 的缺口 |
|---|---|
| 极暗光（0.5~0.75 Lux）增强链路 + 现场帧 + U1~U4 数据集 | 官方测试集为**正常光照** COCO/CCPD |
| RK3588 **端侧** + 双屏演示 | 需要"端→云"的落地场景 |
| **缺失**：给机器看的出口（现判据 = SNR ≥ 4，是**给人看的代理指标**） | 提供**任务指标**（mAP / 准确率）口径 |

## 7. 候选结合角度与结论

| 角度 | 做什么 | 价值 | 代价/风险 |
|---|---|---|---|
| **A. 极暗光 VCM 评测** | 同一现场帧走"**未增强暗光原图** vs **我们 HQ 增强后**"两条，各过 `--quality 0~31`，出 **mAP–bpp 对比曲线** | DTM 官方没有极暗光数据 ⇒ 回答"极暗光下 VCM 值不值得先增强"，是**差异化贡献** | 需搭 DTM 环境；离线评测 |
| **B. 端–云协同演示** | 板出图 → DTM 编码 → 147 数据机解码+检测 | 演示叙事强 | ⚠️ FRP **0.455 MB/s** ⇒ 仅离线/演示级 |
| **C. 极暗光补充测试集** | 按 DCM 测试集规范组织我们的现场帧 | 数据资产标准化 | 需与 DCM 规范对齐 |
| **D. 端侧部署 DTM 子模块** | 把 g_a/g_s 搬上 RK3588 NPU | 端侧闭环 | ⚠️ **NPU 三核已占满**（实时 core0+1 / HQ core2）；DTM 为 x86+cu116 栈 ⇒ **成本最高** |
| **E. 判据对齐** | 把"可用性"论证从 SNR（代理）升级为 mAP（任务） | 论证更硬 | 低风险 |

> **结论（2026-10-10 用户裁定）**：**本目录仅归档理解，暂不开展任何 DTM 相关工作**；不新建里程碑、不改主基准判据、不引入新依赖、不动板子。后续若要开展，须按 `00-ssot.md` **另行立项**（新开 O 项 + 回写 §14/§15），**不得从本笔记直接推进**。
