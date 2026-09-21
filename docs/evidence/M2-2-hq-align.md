# M2-2 ② ONNX↔RKNN 对齐回归：HQ 三档（O35 模型族阈值判定）

## 1. 结论

**HQ（Retinexformer）三档 ONNX↔RKNN 对齐回归 = 全部通过**（按 O35 / v3.55 的 HQ 模型族阈值：张量 max ≤1.5e-2 **且** 图像域 PSNR ≥64 dB）。

| 档 | 张量 max | HQ 阈判 | PSNR (dB) | HQ 阈判 | 张量余量 | 图像余量 |
|---|---|---|---|---|---|---|
| 512×288 | 4.604e-3 | ✓ | 67.35 | ✓ | 3.26× | 1.05× |
| 768×432 | 6.465e-3 | ✓ | 69.87 | ✓ | 2.32× | 1.09× |
| 960×544 | 8.929e-3 | ✓ | 68.23 | ✓ | **1.68×** | 1.07× |

## 2. 口径与判定修正

### 2.1 纠正历史误解（重要）

M0-4 既有报告 `docs/evidence/reports/M0-4/gate_board_{512x288,768x432,960x544}.json` 的 `pass=false` 是**按 ZDE++ 族阈值**（tensor ≤1e-3 / PSNR ≥70 dB）判的——**该阈值对 HQ/Retinexformer 族不适配**。

O35（v3.55）已裁定：FP16 档阈值**按「模型族 × 精度档」细分**——
- **ZDE++ 族** = 张量 max ≤1e-3 且 PSNR ≥70 dB（实测 4.91e-4 / 76.98 dB）；
- **HQ（Retinexformer）族** = 张量 max ≤1.5e-2 且 PSNR ≥64 dB。

按正确阈值，**HQ 三档本就该判过**；且 960×544 的张量余量 **1.68×** 与 O35 裁定时核算值逐位吻合（"以最差档 960×544 为基准…张量余量 1.68×"），交叉佐证口径一致。

### 2.2 实现变更

`tools/hq_eval.py` 新增 **`--family {zde,hq}`**（默认 `zde` 保持既有行为不变）：
- `gate` 阈值表按族选取（HQ 族 = 1.5e-2 / 64 dB；ZDE++ 族 = 1e-3 / 70 dB）；
- cv2 惰性获取（`_cv2_or_none`）：可视化（diff/预览 PNG）缺 cv2 时优雅跳过，**判定链不依赖绘图库**。

报告 = `docs/evidence/reports/M2-2/gate_{512x288,768x432,960x544}_hqfamily.json`。

## 3. 可复现性

- **板端引擎**：`tools/rknn_c_probe.c`（RKNPU2 C API）已存在于板 `~/rknn_ab/probe/rknn_c_probe`。
- **模型**：`~/rknn_ab/models/hq/hq_{3shape}_fp16_tk232.rknn`。
- **输入**：`~/rknn_ab/data/hq/in_{shape}_nhwc.f32`。
- **本次重跑命令**（`core_mask=4` = O36 裁定的 `RKNN_NPU_CORE_2`）：

```bash
./rknn_c_probe models/hq/hq_<S>_fp16_tk232.rknn data/hq/in_<S>_nhwc.f32 1 nhwc float nosram f32 noperf /tmp/out_hq_<S>_c4.f32 4
```

- **本次重跑输出 → 开发机**：`docs/evidence/reports/M2-2/out_hq_{shape}_c4.f32`。
- **可复现性佐证**：三档本次输出与板端历史 `~/rknn_ab/data/hq/out_hq_{shape}.f32` **md5 逐字节一致**：

| 档 | 本次 md5 | 历史 md5 |
|---|---|---|
| 512×288 | `f0213708e567d388dd462508bd798170` | 同 |
| 768×432 | `af3f8d8801c90fbbf3664443ecf49289` | 同 |
| 960×544 | `2870a7a7e0a10d412eb8ad6ecddeba07` | 同 |

## 4. 耗时（core1, core_mask=4，单次含 perf 关闭；仅供参考，非 §6.2 预算判定）

| 档 | npu_run median |
|---|---|
| 512×288 | 2647.6 ms |
| 768×432 | 5117.3 ms |
| 960×544 | 7899.9 ms |

（§6.2 的分档预算 ≤12 / ≤7 / ≤4 s 已由 O37 落定，与 HQ 推理非同一 API 口径的独立判定；本表只作本档记录的补充。）

## 5. 效力边界

- **本项只判 §11.2 M2-2-②**（ONNX↔RKNN 对齐回归 + `hq_eval` 工具加 `--family`）。
- **不放宽任何阈值**；仅把已存在的数值用正确族阈值判定。
- **O30（保底 CNN）仍挂起**，`hq.model_id` / `hq.pad_crop_policy` / `model.hq_fallback_id` 仍空缺。
- **TH3 关键帧链路（keyframe_infer）未在本项实现**；本项只交付对齐回归证据。
- `config.yaml` 零改动。

## 6. 未验（登记，不得写成结论）

- **§6.2 预算判定**非本项范围（O37 已另落定）。
- TH3 引擎的**板载端到端耗时**（2647/5117/7899 ms 是 probe 单次逐层耗时口径，非交付链路的触发→HDMI ≤2 s 口径；后者需 TH3 实现后测）。

## 7. 追加（v3.84 ⑳）——TH3 真线程触发验证（**已归档**）

**性质：已归档证据**——首轮（GIL 未释放）的 stdout **未归档**，仅作**背景**登记（按 `10-evidence.md` §4 与 §14 v3.84 ⑨ 先例，**不作证据**）；**第 7.1 节的复跑批次已落盘归档**（`docs/evidence/reports/M2-2/th3_r{1,2,3}_*.out`），**可作 §11.2 M2-2-④ 的证据**。

### 7.1 复跑归档批次（本节的证据本体）

**测量条件（随行，`10-evidence.md` §1）**：

| 项 | 取值 |
|---|---|
| 板型 | `Embedfire LubanCat-5 V2` |
| 权重哈希 | HQ `67bc708ad50893d7a2ddb78715dbb01096897b5dc303a13cf518fde4f99b73c7`；ZDE++ `98e6f39cfbcbfd703c4cc1e9bb6270795d3bb0b26467ffe7b98358a959191e4f5d` |
| 曲线档 / 精度档 | `480x272` / `fp16` |
| IQ 档 | **demo**（`32f95bdf…`，`nr3d=[1]`、`exp_us=33333`、`gain=8.0`） |
| config 快照哈希 | `/tmp/th3_verify.yaml` = `9d71c99c644e4ad3a7fa2ac7c33e5012ac3232bb66b3bf8f7212ad001595e08f`（与 `config.yaml`（`18abee63…`）**仅差 `logging.level` 一行**，键值零改动） |
| 版本三元组 | toolkit2 `2.3.2` / librknnrt `2.3.2` / rknpu driver `0.9.8` |
| 扩展模块 | `_starlight_native*.so` 构建于 `2026-09-20 10:34:29`（**含 GIL 修复**） |
| 时钟口径 | `mono_ns`（`CLOCK_MONOTONIC`） |

**运行命令**（停桌面取 DRM master 后，板上前台，**不加 sudo**——`cat` 在 `video` 组）：

```bash
python3 main.py --frames 400 --trigger-at 100 --config /tmp/th3_verify.yaml   # R1 触发轮
python3 main.py --frames 400              --config /tmp/th3_verify.yaml   # R2/R3 对照轮
```

**结果**：

| 轮 | 触发 | `hq_started`/`hq_done` | **`hq_flips_delta`** | `slot_full` | `last_run_ns` | `fps` | 端到端 `P50` |
|---|---|---|---|---|---|---|---|
| R1 | `--trigger-at 100` | 1 / 1 | **229** | True | 8084.8 ms | 24.5 | **63.2 ms** |
| R2 | — | 0 / 0 | 0 | — | — | 22.1 | 91.0 ms |
| R3 | — | 0 / 0 | 0 | — | — | 22.6 | 92.7 ms |

**三轮公共项（全过）**：`frames=400` / `flips=400` / `flip_skipped=0` / `dropped_stale=0` / `lease_timeouts=0` / `discarded_triggers=0` / **配对率 1.0000（400/400）** / `stream_on_count=1` / **exit=0**。

**归档件（md5 逐位一致，板 `/tmp` ⇄ 仓库）**：

| 文件 | md5 |
|---|---|
| `docs/evidence/reports/M2-2/th3_r1_trigger.out` | `2cc06c25264b6ba23bb7c458bb24d834` |
| `docs/evidence/reports/M2-2/th3_r2_ctrl.out` | `9b8350cb5a96970173f2a2ef9ea6ddd0` |
| `docs/evidence/reports/M2-2/th3_r3_ctrl.out` | `d7ce9a1e92cc21a87a001af2909b15b1` |

### 7.2 §5 关键约束判定

**`hq_flips_delta=229 > 0`** ⇒ **HQ 推理在途（8.08 s）期间 TH4 仍翻页 229 帧** ⇒ §5「TH4 不得因结果槽空而锁帧」**成立**（该结论**不依赖** P50 数值，见 §7.3）。

### 7.3 首轮暴露的缺陷（**我实现中的 bug，非既有缺陷**）

**背景（未归档，不作证据）**：首轮跑得 `hq_flips_delta=1`（8.3 s 内 MIPI 仅翻 1 帧）、`fps=15.1`、`P50=106.1 ms`。根因 = **pybind11 默认持有 GIL**，`rknn_hq.run` / `pack_rgb888_nhwc_f32` 的**长耗时 C 调用阻塞主 Python 线程**（TH4 合成主循环）⇒ TH3 worker 线程被 GIL 架空，"真线程"方案失效。

**修复** = `native/starlight_native_pybind.cpp` 在取 `buffer_info.ptr` 之后、C 调用前后加 `py::gil_scoped_release`（取指针须持 GIL，故先取再释放、调用完收回）。

修复后复跑（§7.1）：`hq_flips_delta` **1 → 229**。

### 7.4 ⚠️ P50 反常（现象 + 待查，**不强行归因**）

**两批独立复现，方向一致**：

| 批次 | 触发轮 `P50` | 对照轮 `P50` | 触发轮 `fps` | 对照轮 `fps` |
|---|---|---|---|---|
| 首批（未归档） | 72.6 ms | 94.9 / 91.1 ms | 24.3 | 21.7 / 22.4 |
| **归档批次** | **63.2 ms** | **91.0 / 92.7 ms** | **24.5** | **22.1 / 22.6** |

**现象**：**触发轮（注入 HQ、额外占用内存与结果槽）的 `P50` 反而显著低于对照轮**，`fps` 亦更高；触发轮 P50 亦低于 v3.80 基线 74.6 ms。

**⚠️ 边界（不得写成结论）**：
- **机制未知**——"占用更多资源却更快"与⑲ 的"HQ 常驻使 P50 微升"方向相反；
- **n 仍小**（触发 n=2 / 对照 n=4，且**均为 400 帧短跑**，非 `bench.py` 完整报告口径）；
- **可疑方向（未验证）**：触发轮的后半段（8 s 推理在途 + 结果槽被填）与对照轮的负载分布不同，可能与**低频估计（TH2a）与 TH3 的时序交错**或**帧率差异本身**有关——**此为假设，非结论**；
- ⇒ **登记 O74**（阻塞里程碑 = M2-2 出口前），由用户裁定方向。

### 7.5 效力边界

- 本项**只判**「§5 TH4 不被 HQ 冻结」（§7.2）与登记 P50 反常（§7.4）；**不放宽 §11.2 任何回归判据**（20% 未动）；**§13 全表不动**；`config.yaml` 键值零改动。
- **副作用与恢复（已按规则 `30-board-safety.md` §4 执行）**：`isolate multi-user.target` 停桌面 → IQ 切 **demo** → 三轮 → **IQ 还原 collect（sha `5cc97e06…` 回读一致 ✓）→ `isolate graphical.target`**；复核 `graphical=active`、**真实 oops = 0**。