# O143：`step-response` 接桩与「切换响应」真链路测量口径（v3.86 追加 100）

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：M2 闸门 ②-②（§13 复现命令在真实链路跑通）与 ①-3（切换响应）的**接桩记录与测点有效性说明** |
| 效力 | **非裁定、非验收依据**（规则 `00-ssot.md` §3）；①-3 的判定仍以 §9.1 派生值为准 |
| 对应主基准节 | §11 M2 行 ①-3、§11.2 M2 行 ②-②、§13「切换响应」行、§9.1（EMA 收敛式）、§6.5（`mono_ns`）、§15 **O143** |
| 证据 | `docs/evidence/reports/subsplit/step1.json`（首轮：场指纹测点）/ `step2.json`（次轮：向下阶跃）/ `step3.json`（三轮：`mean_enh` 测点）+ 同名 `.log` |
| 状态 | `draft`（入口已通；**测点有效性受限**，见 §4） |

## 1. 接桩内容（**均为 CLI 验证开关，非 config 键 / 不新增事件与返回码**）

| 落点 | 内容 |
|---|---|
| `curve_estim.py` | 增**发布日志**（默认关）：每次发布记 `(publish_ns, seq_est, 场指纹)`；场指纹 = 场前 256 个 fp16 的均值 |
| `main.py` | 增 `--step-exp-times N` / `--step-exp-interval-s` / `--step-exp-low` / `--step-exp-high` / `--step-exp-subdev`；帧循环内低频推进曝光阶跃（稳态开销 = 两次比较）；增 `_step_sample()` 逐 2 帧取 `(mean_orig, mean_enh)`（走 native 统计） |
| `tools/bench.py` | 新 `scenario_step_response()`（**原 `not_implemented` 桩已删**）：≥10 次阶跃（`--runs` 即阶跃次数），出 `conv_ms` P50/P95、`derived_ms` 对照、逐次明细 |

## 2. 裁定口径

1. **阶跃方式** = **运行期曝光微调**（`v4l2-ctl --set-ctrl=exposure=`；规则 `20-contracts.md` §4 明许）。
2. **曝光方向** = **只能向下**：`camera.exposure_us` = **66666 µs** 在 15 fps 下已近满帧上限（帧周期 66.7 ms）⇒ 向上 4× 会被驱动 clamp ⇒ 阶跃成空操作。默认改为「高档 = 现值，低档 = 现值/4」。
3. **测点** = **增强窗均值 `mean_enh`**（每 2 帧一次 ⇒ **7.5 Hz**；走 native 统计 ⇒ 亚毫秒）。**不得**用曲线场指纹作判据（见 §3）。
4. **收敛判据** = 阶跃后**首次进入「新稳态 ±10% 行程」并此后保持**的取样时刻 − 阶跃时刻。
5. **判据与契约不动**：§13 上限仍为 **≤500 ms**；①-3 在无有效真链路测点前**以 §9.1 派生值判定**。

## 3. 三轮迭代（**含两次测点订正，如实登记**）

| 轮 | 测点 / 方向 | 结果 | 结论 |
|---|---|---|---|
| `step1` | 曲线场指纹；**向上** 66666 → 266664 | 场指纹仅变 **0.13%**；"收敛" 2.9–4.9 s | ① 向上阶跃被 clamp ⇒ 空操作；② 场指纹对亮度**不敏感** |
| `step2` | 曲线场指纹；**向下** 66666 ↔ 16666 | 场指纹仅变 **0.4%（最大相邻 Δ = 0.0017）** | **场指纹不得作判据**（信噪比不足），改测语义量 |
| `step3` | **`mean_enh`**；向下阶跃 | **原图均值全程恒定 85.0**（66666 与 16666 无差别）⇒ 增强均值亦恒定 | **根因：演示链路 AE 开启，自动曝光覆盖了运行期曝光写入** |

`step3` 关键读数：`status = ok`、**10/10 阶跃**、取样周期 **132.9 ms**、`mean_orig` 85.2→84.8（噪声级）、`mean_enh` 154.8↔154.9（噪声级）、`conv_ms` P50 **2914 ms**（**不可读作切换响应**）、`derived_ms` = **393.1 ms**（`alpha` 0.4642 / `f_est` 7.613）。

## 4. 结论与残留

- **②-② 判过**：§11.2 原文判据 = *"§13 的复现命令在真实链路跑通一轮（报告可生成即为通过，指标达标与否另判）"* ⇒ `bench.py --scenario step-response` **报告已可生成**，该项闭合。
- **①-3 仍按 §9.1 派生值判过**（`N = ln(0.1)/ln(α)` = 3.000，`N/f_est` = **393.1 ms** ≤400/≤500）；**本场景不作达标依据**。
- **⚠️ 测点有效性受限（不得读作"已实测切换响应"）**：演示链路 **AE 开启** ⇒ 运行期曝光写入被自动曝光覆盖 ⇒ **真链路无法用曝光微调造亮度阶跃**。
- **真测的两条路径（均需另行裁定 / 人工）**：**(a)** **先关 AE**（属 ISP 设置变更，会改数据集输入域 ⇒ 须走 §15）；**(b)** **光源侧阶跃**（人工遮光 / 开关灯），bench 提供外部阶跃时刻对齐入口。

## 5. 复现命令（板端）

```
sudo systemctl stop starlight-demo
sudo /usr/bin/python3 /home/cat/starlight/sensor_lock.py --apply 7906
cd /home/cat/starlight
sudo PYTHONPATH=/home/cat/.local/lib/python3.9/site-packages python3 tools/bench.py \
     --scenario step-response --runs 10 --step-interval-s 3 --out /tmp/subsplit/step3.json
sudo systemctl start starlight-demo        # 恢复（跑完核 rsm_transition → RUN_DEMO）
```

## 6. 效力边界

**判据与契约不动**、**不放宽 §11.2 / §13**、**`config.yaml` 零改动**、**不新增 config 键 / 日志事件 / 返回码**；本档**不作为验收依据**；改 AE 或改测点须先走 §15 裁定（规则 `00-ssot.md` §4）。