# 证据：M0-6 双 context 并发压测（NPU 侧）

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：一次压测的实测结果与判定，对应 `docs/plans/M0.md` §3 的 **M0-6** 项 |
| 效力 | **非验收依据**（规则 `00-ssot.md` §3.5）；验收只认主基准 §13 复现方式 + §11.2 回归项 + `bench.py` 报告 |
| 对应主基准节 | §11.1-6、§6.1、§7、§12 R3 / R18、§15（O29 / O36 / O37 / O43 / O51） |
| 实验时点 | §3 NPU 侧：**2026-09-18 21:0x CST**；**§5 MIPI 侧：2026-09-19 01:4x CST（v3.52 补测）** |
| 实验方式 | §3：**只读板端压测**（双进程并发跑 NPU，无停流/复位/改 systemd）。**§5**：双进程并发 + **DRM 出屏**（期间 `isolate multi-user.target` 取 master，结束已 `isolate graphical.target` 完整恢复） |
| 状态 | `draft`（**v3.52 更新**：NPU 侧（§3）+ **MIPI 侧（§5，补测）均已实测 = 无劣化**；双屏待 HDMI；**正式收口按 §15 O51 归入 M2**） |

> 本档只记录实测事实与判定，不复制规范取值。R3 的降级动作见主基准 §10.5。

## 1. 目的（§11.1-6）

§11.1-6 的出口判据是"**MIPI 侧耗时涨幅 ≤20% 判定 R3**"（R3 = 双 context + 双屏抢 DDR/RGA 致 MIPI 掉帧 >20%）。该判据要求**可运行的 M2 链路**（画布合成 / pageflip）与**双屏**（HDMI）。M0 阶段两者均不具备，故本档只做**能测的那一半**：**NPU 侧的双 context 争抢**——即"曲线估计（ZDE++ 2 条带 core0+core1）"与"HQ 推理"并发时，曲线估计的耗时是否劣化。

## 2. 方法

| 项 | 设置 |
|---|---|
| 被测（A） | `tools/rknn_split_probe`：ZDE++ 480×272 整帧，**2 条带 core0+core1**（切片模型 `zde_curve_256x272_int8_tk232.rknn`，HALO 7），`nofloat`，**30 次** |
| 干扰（B） | `tools/rknn_c_probe`：HQ `hq_512x288_fp16_tk232.rknn`（FP16、**core_mask=AUTO**，见 §5），3 次（单次 ≈2.7–3.6 s） |
| 轮次 | 3 轮；每轮先测 A 单独（`alone`），再后台起 B（`+HQ`，`sleep 1.5` 后启动 A，确保 A 落在 B 的推理窗口内） |
| 口径 | 与 M0-2/M0-8 一致：C-API、uint8 输入、`nofloat`、关闭 perf 采集；取 `frame_wall_ms` 的 min/median/p90 |

## 3. 结果（`frame_wall_ms`，ms）

| 轮 | 场景 | min | median | p90 | max |
|---|---|---|---|---|---|
| 1 | alone | 17.30 | **18.02** | 18.44 | 18.92 |
| 1 | +HQ | 16.27 | **16.78** | 20.44 | 31.60 |
| 2 | alone | 17.63 | **17.96** | 18.07 | 19.11 |
| 2 | +HQ | 16.18 | **16.51** | 20.04 | 20.48 |
| 3 | alone | 16.26 | **18.00** | 18.09 | 18.86 |
| 3 | +HQ | 16.57 | **17.19** | 19.72 | 20.39 |

## 4. 判定

1. **中位数无劣化，反而略降**：alone 18.02 / 17.96 / 18.00 ms → +HQ 16.78 / 16.51 / 17.19 ms（**−5%~−8%**）。⇒ **HQ 并发不拖慢曲线估计**。
2. **尾部分位涨幅在判据内**：p90 由 18.07~18.44 → 19.72~20.44 ms，**涨幅 ≈ +9%**（≤20%）。⇒ 按 §11.1-6 的"≤20%"口径，**NPU 侧并发可接受**。
3. **解释（不作结论、仅记观察）**：median 略降与 min 更低（16.2~16.6 ms）一致，符合"并发负载使 NPU/devfreq 进入更高频点"的形态；p90 上升为偶发抖动（轮 1 出现 max 31.60 ms）。
4. **R18 不触发**：曲线应用侧（8 iter 融合）与本次压测无关，R18 的带宽计数仍以 M0-13 的 118.2 vs 945.6 MB/s 为准。

## 5. MIPI 侧耗时涨幅（§11.1-6 的出口判据）—— **v3.52 补测**

**背景**：§11.1-6 的出口判据是"**双 context 并发下 MIPI 侧耗时涨幅 ≤20%**"（R3）。上表 §3 只测了 **NPU 侧**；MIPI 侧当时无链路可测。**v3.47 打通 MIPI 链路后**（`tools/pipeline_probe.c`），本档补出该半。

**方法**（沿用 §2 的双进程并发口径）：

| 项 | 设置 |
|---|---|
| 被测 | `tools/pipeline_probe --display --frames 600`（约 11.5 s），**取 TH4 的两段打点**：`canvas_ms`（画布合成，2 次 RGA blit）与 `pageflip_ms`；另记录 `frame_ms` / FPS / `flip_skipped` |
| 干扰 | `tools/rknn_c_probe` 跑 HQ `hq_512x288_fp16_tk232.rknn`（**FP16 / `core_mask=0` AUTO**），4 次（≈13 s），后台启动 + `sleep 1.5` 后起被测 |
| 轮次 | 3 轮；每轮先 `alone` 再 `+HQ` |
| 运行条件 | 无桌面（`isolate multi-user.target` 取 DRM master）；结束后 `isolate graphical.target` 完整恢复 |

**结果**（`canvas_ms`，ms）：

| 轮 | 场景 | min | median | p90 | max |
|---|---|---|---|---|---|
| 1 | alone | 0.995 | **1.462** | 2.593 | 3.660 |
| 1 | +HQ | — | **1.373** | 1.974 | — |
| 2 | alone | — | **1.424** | 2.258 | — |
| 2 | +HQ | — | **1.350** | 2.089 | — |
| 3 | alone | — | **1.431** | 2.487 | — |
| 3 | +HQ | — | **1.388** | 1.978 | — |

| 指标（3 轮中位） | alone | +HQ | 涨幅 |
|---|---|---|---|
| **`canvas_ms` 中位** | 1.431 | 1.373 | **−4.1%** |
| **`canvas_ms` p90** | 2.487 | 1.978 | **−20.5%** |
| `frame_ms` 中位 | 16.790 | 16.788 | **±0.0%** |
| FPS | 52.10 / 53.94 / 51.15 | 54.53 / 53.51 / 53.75 | 无劣化（略升） |
| `flip_skipped` | 0 | 0 | — |

**判定**：**MIPI 侧无劣化，涨幅判据（≤20%）满足**——`canvas_ms` 中位 **−4.1%**、p90 **−20.5%**，两项均为**负涨幅**；`frame_ms` 完全不变；3 轮各 600 帧**零丢 flip**。

**口径与限制（必读，否则会误读）**：

1. **`pageflip_ms` 中位不可用作涨幅判据**——它是**双峰量**（前一 flip 已完成时 ≈0.01 ms，未完成时 ≈1 个 vblank ≈16.7 ms），中位对相位极敏感：本轮 alone 三轮为 **1.384 / 0.562 / 0.317**（波动 4×），+HQ 为 1.074 / 1.458 / 1.163。其 **p90 稳定**（5.950 → 6.070，**+2.0%**），故以 p90 为准。
2. **`frame_ms` 是 vsync 锁定值**（60 Hz ⇒ 16.79 ms），**不能**用它判"耗时涨幅"——它不变是"没有掉帧"的证据，不是"计算量未变"的证据。真正的计算量指标是 `canvas_ms`。
3. **干扰侧为 AUTO 口径**（`core_mask=0`），**非契约的 core1+2**——因 **O36**（`RKNN_NPU_CORE_1_2` 被本版运行时拒绝）。该口径限制与 §3 的 NPU 侧测量一致。
4. **本测量只覆盖子项 ①（MIPI 侧涨幅）**；**未含双屏**（HDMI 未接）⇒ 子项 ②（双屏满载画布流量）仍归 **M2-1**。
5. **工具口径差异 —— 已由 §15 裁定 O52 收口**：§15 的 **O51** 把子项 ① 的收口判据写成"**`bench.py` 报告**给出该涨幅"，而本次用的是 **`tools/pipeline_probe.c`**（`bench.py` 的链路类场景 `--scenario realtime` 至今为 `not_implemented`）。**v3.53 裁定 O52 接受 `pipeline_probe` 报告为等效证据**（理由：两者同属「逐帧 JSON 报告 + 函数级打点」，本报告已含该判据所需的全部字段；再跑 `bench.py` 不产生新信息）⇒ **子项 ① 就此正式收口**。**效力边界**：该裁定**仅限本项**；**§13 验收表的复现方式仍只认 `bench.py`**，本裁定不放宽任何 §13 验收项。

## 6. 未做与阻塞（M0-6 未落定）

| 缺口 | 原因 | 处置 |
|---|---|---|
| ~~**MIPI 侧耗时涨幅（§11.1-6 的出口判据）**~~ | **已由 v3.52 补测**（见 §5）：结论 = **无劣化**（`canvas_ms` 中位 −4.1% / p90 −20.5%） | **✅ 已正式收口**（**v3.53 裁定 O52**：接受 `pipeline_probe` 报告为等效证据） |
| **双屏满载画布流量** | **HDMI 未接**（`card0-HDMI-A-1` disconnected） | 接 HDMI 后补（与 M0-9 同前置）→ **M2-1** |
| **HQ 绑 core1+2（契约口径）** | **O36**：`RKNN_NPU_CORE_1_2`（mask=6）被本版运行时拒绝 ⇒ 本次 HQ 为 **AUTO** 口径，非契约口径 | 随 O36 裁定处置 → **M2-2** |
| **RGA 侧争抢（MIPI 合成所用）** | 本轮 `rga_probe` 对 **1920×1080 → 512×288** 的探针几何（eff 512×286 + pad）被 RGA 硬件拒（`dst rect [0,1,512,286]`，y 偏移为奇）；**该尺寸的 RGA 实测属 §15 的 O43**，不在本档范围 | **O43 已回填实测**（中位 **2.079 ms**）；§6.1 该行重排待裁定 |

⇒ **M0-6 判定：NPU 侧无劣化（§3）+ MIPI 侧无劣化（§5）**；出口判据的两个可测半均已给出，**正式收口按 §15 的 O51 归入 M2**（子项 ② 归 M2-1、③ 归 M2-2）。

## 7. 复现命令

```bash
# —— §3 NPU 侧（原口径）——
# 被测 A（单独）
ssh board 'cd ~/rknn_ab && ./probe/rknn_split_probe models/zde_curve_256x272_int8_tk232.rknn \
  data/frame_480x272_u8.raw 272 480 30 2 0,1 nofloat'
# 干扰 B（后台）+ 被测 A（并发）
ssh board 'cd ~/rknn_ab && \
  ./probe/rknn_c_probe models/hq/hq_512x288_fp16_tk232.rknn data/hq/in_512x288_nhwc.f32 3 \
    nhwc nofloat nosram u8 noperf /dev/null >/dev/null 2>&1 & sleep 1.5; \
  ./probe/rknn_split_probe models/zde_curve_256x272_int8_tk232.rknn data/frame_480x272_u8.raw \
    272 480 30 2 0,1 nofloat; wait'

# —— §5 MIPI 侧（v3.52 补测）——
ssh board 'sudo systemctl isolate multi-user.target'          # 取 DRM master（有副作用，须授权）
ssh board 'mkdir -p ~/rknn_ab/data/m0-6-mipi && cd ~/rknn_ab/probe && for r in 1 2 3; do \
  ./pipeline_probe --display --frames 600 --report ~/rknn_ab/data/m0-6-mipi/r${r}_alone.json >/dev/null 2>&1; \
  ./rknn_c_probe ../models/hq/hq_512x288_fp16_tk232.rknn ../data/hq/in_512x288_nhwc.f32 4 \
    nhwc nofloat nosram f32 noperf - 0 > ~/rknn_ab/data/m0-6-mipi/r${r}_hqinf.json 2>&1 & HQPID=$!; \
  sleep 1.5; \
  ./pipeline_probe --display --frames 600 --report ~/rknn_ab/data/m0-6-mipi/r${r}_hq.json >/dev/null 2>&1; \
  wait $HQPID; done'
ssh board 'sudo systemctl isolate graphical.target'           # 恢复桌面
```

## 8. 报告原档

- **§3（NPU 侧）**：本轮为**终端实测**（工具直接打印 JSON），未落盘独立报告文件；上表数字即 `frame_wall_ms` 原文摘录。
- **§5（MIPI 侧，v3.52）**：报告原档落盘于 **`docs/evidence/reports/M0-6/`** —— 每轮 3 份：`r<n>_alone.json`（`pipeline_probe` 无干扰）、`r<n>_hq.json`（+HQ 干扰）、`r<n>_hqinf.json`（`rknn_c_probe` 的干扰侧自证，含 `core_mask` / `mem` / `wall_ms`）。
