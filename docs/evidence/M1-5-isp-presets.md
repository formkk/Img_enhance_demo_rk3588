# M1#5：ISP 分场景调参（暗房 / 夜景 / 弱光人脸预设 + ISP 指纹）

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：`isp.presets` / `isp.fingerprint` / `isp.csc_matrix` / `isp.rgb_range` / `isp.nr_3d_ref_frames` **5 键定稿**的一次作业记录（含 S1~S5 全过程、量化随行与未验项） |
| 效力 | 本档**本身不是验收依据**（规则 `00-ssot.md` §3.5）；其**结论值**经 §15 **O145** 就地裁定后回填 `config.yaml` 的该 5 键，并作为 §11 M1 出口标准 #5 与 §11.2 M2-③ / §10.4 配置闸门的证据路径 |
| 对应主基准节 | §9.5（含 O91 / O94 块）、§9.2、§10.3、§10.4、§11 M1 #5、§15（**O145** / O64 / R23） |
| 工具 | `tools/collect/iq_sets.py`（`status` / `switch`）、`tools/realtime_frame_review.py`（O94 (a) 实时线转正）、`tools/hq_frame_review.py`（O94 (b) HQ 线）、`tools/bench.py --scenario idle-link`、只读 IQ 探针（`/tmp/iq_fp*.py`，**未入库**） |
| 实验时点 | **2026-09-23 23:2x–2026-09-24 0x CST**（板 `lubancat` / `192.168.0.103`，SSH 别名 `board`） |
| 状态 | `draft` |

## 1. 范围与判据口径

用户 2026-09-23 指示：**「只做 S1~S5（5 键定稿），三套预设的曝光/增益起点就用 O91 的 66 ms / 20×」**；随后就两项分叉选定：**三套预设全用 66 ms / 20×**、**NR 档（3DNR）本轮只回读、不改**。

判据口径 = §9.5 的 **O94**：**离线单帧双通路出图 + 量化随行 + 人工目视**，**不以「演示画面观感」为判据**；**链路输出原样归档、零后处理**（规则 `10-evidence.md` §2）。

| 步 | 内容 | 关键产出 |
|---|---|---|
| S1 | 档位与取值回读（`iq_sets.py status` + sensor subdev 回读） | §2 |
| S2 | 双通路单帧出图（实时线 + HQ 线）与量化随行 | §3 |
| S3 | ISP 指纹 8 子项全量回读（三套 IQ 对照）+ 3DNR A/B | §4、§5 |
| S4 | **人工目视终审（用户）** | §6（**待**） |
| S5 | 落 5 键 + 回写 | §7 |

## 2. S1：档位与取值回读

`python3 tools/collect/iq_sets.py status`（板端，只读）与 `v4l2-ctl -d /dev/v4l-subdev2 --get-ctrl exposure,analogue_gain`：

| 项 | stock（出厂） | collect（采集档） | demo（演示档，**现役**） |
|---|---|---|---|
| `sha256` | `4536f96a0bd594ed…` | `ff7fb3e92865eabe…` | `2ed58401f2fc8b0b…` |
| `nr3d_enable` | `[1]` | **`[0]`** | **`[1]`**（= stock 逐位一致） |
| `exp_us` / `gain` / `fps`（IQ 生成参数） | — | `66666` / `20.0` / `15` | `66666` / `20.0` / `15` |
| `built_utc` | — | `2026-09-21T04:03:32Z` | `2026-09-21T04:03:34Z` |

**sensor subdev 回读（§9.5 ③ 的唯一有效口径）**：`exposure = 8974`（× 7.433 µs/行 = **66.5 ms**）、`analogue_gain = 87`（0.3 dB 步进 ⇒ 26 dB ⇒ **20×**）；`vertical_blanking = 7906`（流中由 `sensor_lock.py` 在 `STREAMON` 前施加；VMAX = 1080 + 7906 = 8986 行 ≈ 66.8 ms ⇒ ≈15 fps）。

⇒ **与 O91 定稿值换算后完全吻合**（§9.5 的 O91 块：`TimeValue = 0.066666` / `GainValue = 20.0` / `FpsValue = 15`；`camera.vblank_lines = 7906`）⇒ **两套 IQ 无需重建**（重建会改变已定稿的 sha256，并使已归档报告的测量条件失效）。

## 3. S2：双通路单帧出图（O94 (a)(b)(c)）

命令（板端，演示服务先停）：

```bash
python3 tools/realtime_frame_review.py --frames 300 --snr-frames 100 --scene darkroom_demo --prefix /tmp/m1_5/rt
python3 tools/hq_frame_review.py                                                     --prefix /tmp/m1_5/hq
```

产物（已取回工程目录，**链路原样、零后处理**）：`docs/evidence/reports/M1-5/{rt_orig.png, rt_enh.png, hq_orig.png, hq_hq.png}` + 两份 `*_report.json`。

| 项 | 实时线（增强窗 512×288） | HQ 线（输出 960×540） |
|---|---|---|
| 原图窗 `mean_rgb` | 25.64 / 24.12 / 25.47 | 26.38 / 24.78 / 26.24 |
| 输出 `mean_rgb` | **93.87 / 88.76 / 103.55** | **116.86 / 111.21 / 106.35** |
| `dark_block_sigma_median` | 2.403 | 9.89（HQ 输出） |
| 推理域 SNR（§13 口径 ≥4） | **35.146** | **46.668** |
| 通道平衡 G/R（全窗 / 最暗 25% 块） | 0.9406 / **0.8119** | 0.9517 / 0.9046（HQ 输出） |
| 曲线频率 / 模型耗时 | `f_est = 2.501 Hz`（该工具 300 帧 50 次估计的口径） | `run_ns = 8 369 101 843`（≈8.37 s） |

**测量条件随行**：`iq.set = demo` / `sha256 = 2ed58401…` / `nr3d = [1]` / `exp_us = 66666` / `gain = 20.0` / `fps = 15`；subdev 回读 `8974 / 87 / 7906`；`config sha256 = 0b99a46c5ab84990…`；`labels.scene = darkroom_demo`、`labels.lux = null`（**未录入照度计读数** ⇒ 本批不构成"某照度下"的合格判据，见 §8）。

**可读数事实（不作结论）**：暗部 G/R = 0.81 在**原图窗与增强窗同源**（0.9406/0.8119 为原图窗；增强窗 0.9456/0.8379）⇒ 该偏色来自**采集侧**，非 8-iter 应用引入；HQ 输出 `mean_rgb` 的 R/B 差（116.86 vs 106.35）与暗部 σ 9.89 亦在**输出侧**首次出现。

## 4. S3：ISP 指纹 8 子项全量回读（三套 IQ 对照）

场景树路径 = `main_scene[i].sub_scene[0].scene_isp30`（i = 0 / 1）。**取值不复制进 config**，只记「IQ 内路径 + 可观测状态 + 规范化子树哈希（前 16 位十六进制）」。

| 子项 | IQ 内路径 | stock | collect | demo |
|---|---|---|---|---|
| `iq_file` / `iq_hash` | 文件本身 | `4536f96a…` | `ff7fb3e9…` | `2ed58401…` |
| `gamma_table` | `agamma_calib_v11/GammaTuningPara` | `Gamma_en = 1`、曲线 **49 点**、`out_offset = 0`、曲线 sha `6c8b16cfbcc298d9`、子树 `881b76242a6f2650` | 同 stock | 同 stock |
| （旁证）`degamma` | `adegamma_calib/DegammaTuningPara` | `degamma_en = 0`（表全 0） | 同 | 同 |
| `ccm` | `ccm_calib` | `control.enable = 1`；`matrixAll` **14** 条 / `aCcmCof` **7** 个光源；`damp_enable = 1`、`illu_estim.interp_enable = 0`、`default_illu = "A"`；`TuningPara` 子树 `85e5727b30c11272` | 同 stock | 同 stock |
| `black_level` | `ablc_calib` | `BlcTuningPara.enable = 1`（`BLC_Data` = **13** 个 ISO 点 × R/Gr/Gb/B）、`Blc1TuningPara.enable = 0`；子树 `a511de7cb4e7c348` | 同 stock | 同 stock |
| `awb_lock` | `wb_v21.control.mode` | **`CALIB_WB_MODE_AUTO` ⇒ 未锁定**；`manualPara.cfg.mwbGain = [1.32703, 1, 1, 3.31432]`（留档，未启用） | 同 | 同 |
| `nr_2d` | `bayer2dnr_v2/TuningPara.enable` | `1`；2 组 Setting（LSNR/lcg、HSNR/hcg）× 13 ISO 点；子树 `16ee259977494109` | 同 stock | 同 stock |
| `nr_3d` | `bayertnr_v2/TuningPara.enable` | `1`；子树 `be05bac5d36d4eca` / `8868e41f867ce167` | **`0`**；子树 `3231321eaddd45e7` / `31be6ead629da008` | `1`；**与 stock 逐位一致** |
| `dpc` | `adpcc_calib/DpccTuningPara/Enable` | `1`；子树 `79da0940d59eea65` / `534d9be12f2cc92c` | 同 stock | 同 stock |

**两条结论（可复现）**：

1. **collect 与 demo 的全部差异只有 3DNR 使能 + AE 手动档**（其余 7 子项在 stock / collect / demo 三者间**逐位相同**）⇒ §9.5 的 O91 ⑤「域差收敛（两档同曝光同增益，只差 3DNR）」成立；亦说明「改 IQ 没改名」的风险在本轮为零。
2. **CSP / range 侧取值 = `bt601` + `full`**：依据 = ① ISP 侧实测 `YRangeMode = CAM_YRANGEV2_MODE_FULL` / `full_range = 1` / `cgc_yuv_limit = 0`（§9.5 ⑨ v3.83 排除项）；② 交付链路 `native/rga_core.c` 已显式化 `IM_YUV_TO_RGB_BT601_FULL`（v3.86 追加 39 / O54 就地裁定）。**两处一致** ⇒ 非猜测值。

## 5. 3DNR 的开 / 关 A/B 实测（本档的核心新实测）

**目的**：§9.5 v3.4 追加② 要求「3DNR 参考帧数与实测帧级延迟（时域滤波固有延迟须计入 §6.1 空链路基线）」。**前置事实**：**「参考帧数」不是可达取值对象**——`bayertnr_v2` 只暴露 `enable` + 2 组 `Setting` × 13 ISO 点（17 字段，无任何 ref/frame 字段），厂商头文件（`/usr/include/rkaiq/iq_parser_v2/bayertnr_uapi_head_v2.h`、`algos/abayertnr2/*`）与 `strings /usr/lib/librkaiq.so` **均无**参考帧数 ⇒ **不以猜测值填充**，改测「其帧级固有延迟」。

**方法与条件**：两档唯一差异 = 3DNR 使能（§4 结论 1）⇒ 用 `bench.py --scenario idle-link`（§6.1 空链路口径：关 TH2a / TH2b / OSD，保留 采集 → RGA 原图窗 → 画布合成 → pageflip）各 3 轮 × 300 帧；换档 = `iq_sets.py switch`（重启 `rkaiq_3A`）+ sha256 回读校验；同一 config 快照（`0b99a46c…`）、同一权重 / 曲线档 / 精度档。

```bash
sudo python3 tools/collect/iq_sets.py switch collect   # 3DNR 关
sudo PYTHONPATH=/home/cat/.local/lib/python3.9/site-packages python3 tools/bench.py \
    --scenario idle-link --breakdown --rounds 3 --frames 300 \
    --weight-hash ca8855b9… --curve-level 480x272 --precision fp16 --config-hash 0b99a46c… \
    --out /tmp/idle_3dnr_off.json
sudo python3 tools/collect/iq_sets.py switch demo      # 3DNR 开（现役档，跑完须恢复）
# 同命令 → /tmp/idle_3dnr_on.json；OFF 复跑 → /tmp/idle_3dnr_off2.json（乱序排除）
```

| 报告 | 3DNR | 端到端 P50 / P95 | 采集→提交 P50 / P95 | 提交→vblank P50 / P95 |
|---|---|---|---|---|
| `reports/M1-5/idle_3dnr_on.json` | **开** | 13.482 / 20.992 | 4.643 / 4.932 | **8.887 / 16.156** |
| `reports/M1-5/idle_3dnr_off.json` | **关** | 16.278 / 24.574 | 7.366 / 9.639 | **8.980 / 16.156** |
| `reports/M1-5/idle_3dnr_off2.json` | **关**（复跑） | 12.661 / 20.306 | 3.558 / 4.868 | **8.698 / 16.215** |

三份报告的公共随行：配对率 **1.0**（900/900）、`discarded_frames` 四项全 **0**、`stream_on_count = 1`、`estimates = 0`（空链路口径自证）、`ts_source = EOF`、面板刷新 **16.667 ms**。

**结论（可证伪）**：

1. **未检出可归因于 3DNR 时域滤波的显示侧帧级固有延迟** ——「提交→vblank」段三份报告**几乎逐位一致**（p50 8.70–8.98 ms、p95 16.156–16.215 ms；极差 ≤0.3 ms），且 p95 恒被面板刷新周期（16.667 ms）支配。
2. **「采集→提交」段的差异不可归因于 3DNR** —— 首轮 OFF 曾出现 +2.7 ms（p50 7.366），但**同条件复跑不复现**（OFF#2 = 3.558，反而**低于** ON 的 4.643）⇒ 该段跨运行抖动（±2 ms）**大于**两档差异；其锚点 = 驱动 EOF 事件，时序随 ISP 处理次序移动 ⇒ **不得由延迟差反推参考帧数**。
3. ⇒ 本项的落地方式改为：**`ref_frames = "IQ 未暴露（不得猜值）"` + 上述 A/B 读数与结论入 config**（键仍定稿，见 §7）。**这不是"测到了 0 帧参考"，而是"该量不可达、且其可观测后果（延迟）未检出"**——两种说法不得混用。

## 6. S4：目视判定（**已完成**，v3.86 追加 103）

用户 2026-09-24 指示「**执行 S4 目视判定**」⇒ **由实现者代行目视**（4 张链路原样图已内联贴入对话；用户既定要求"先给图、只给路径无效"）。**⚠️ 效力随行**：O94 (d) 原文的目视判据主体 = **用户**；本次为**用户指示代行**，**用户可随时覆写**。

### 6.1 实拍所见（逐图，链路原样、零后处理）

| 图 | 所见 |
|---|---|
| `rt_orig` / `hq_orig`（512×288 原图窗） | 近黑场景：左侧门板上一块漫射高光斑（**已饱和**）、右上置物架瓶罐轮廓可辨、右下深色板面；**原图窗本身即偏品红**（与量化暗部 G/R = 0.8119 / 0.8059 一致） |
| `rt_enh`（增强窗 512×288） | 亮度大幅提升，门框 / 插座与走线 / 吊挂物 / 瓶罐 / 纸堆**结构可辨**；① **整体品红调**；② **暗部抬升带来灰雾感**（黑电平上移、对比度低）；③ 颗粒噪明显；④ 左下高光斑仍饱和 |
| `hq_hq`（960×540） | 细节最充分（瓶标文字轮廓可辨）；① **整体偏绿 / 偏灰**；② **颗粒噪声大**；③ **左侧门板出现块状"云雾"斑块**（近黑区放大伪影 / 疑似幻觉）；④ 左下光斑被压成饱和黄块、右下白块过曝 |

量化随行见 §3（推理域 SNR：实时 **35.146** / HQ **46.668**，§13 判据 ≥4）。

### 6.2 判定 = 「可放行（有保留）」——核心是**把缺陷与 ISP 5 键解耦**

| 所见缺陷 | 归因（实测支撑） | 是否属 ISP 5 键 |
|---|---|---|
| 品红偏色 | **原图窗同源**（暗部 G/R 0.81）⇒ 采集侧，正对 §9.5 ⑤ 的「AWB 锁定」未落地（**O64**） | 是（但 O64 独立登记，**不阻塞 M1#5**） |
| HQ 发绿 / 颗粒噪 / 门板云雾斑 | **首次出现于 HQ 输出侧** ⇒ 预训练权重与模型行为 | **否**（与 §13「实时画质」行同源） |
| 高光饱和 | 原图窗即 clip（曝光已由 O91 定稿） | 否 |

**支撑放行的四项**：① 曝光 / 增益 / 帧率与 O91 定稿换算吻合、subdev 回读一致；② 两档唯一 ISP 差异 = 3DNR（域差收敛）；③ 增强链路可工作，SNR 远超判据；④ 缺陷根因均不在 5 键的作用面。

### 6.3 保留（不阻塞，须随行）

1. **若要改善采集侧偏色，须先做 AWB 锁定对照**（改 IQ）——但那会**改两套 IQ 的 sha256** ⇒ 作废 O91 已登记哈希与已归档报告的测量条件 ⇒ **须另行走 §15 裁定**，本轮不做。
2. **本批无照度标注**（`lux = null`）⇒ 判定条件不完备，只作**同条件对照**。
3. **门板云雾斑与颗粒噪登记给模型 / 训练线**（不在 M1#5 范围）。

### 6.4 结论

**M1#5 的 ISP 侧（三档预设 + 指纹 + `csc_matrix` / `rgb_range`）判「可放行」** ⇒ O145 的 S4 末步闭合（回写 = §9.5 补记 + §14 追加 103 + `docs/plans/M1.md` §3#5 + `docs/plans/M2.md` §8.7）。

## 7. S5：落 5 键与回写清单

| 键 | 落值（`config.yaml`） | 依据 |
|---|---|---|
| `isp.presets` | 三档（暗房 / 夜景 / 弱光人脸）：`exposure_us = 66666`、`gain = 20.0`、`fps = 15`、`iq_set = "demo"`、`nr_3d = IQ 现值`；随行 `note` 声明**三档同值**与 SSOT | 用户 2026-09-23 选定；O91（66 ms / 20×） |
| `isp.fingerprint` | 8 子项 + `iq_file` / `iq_hash`（三档 sha256）；**只记路径 + 状态 + 子树哈希** | §9.5 ISP 指纹（v3.2 / v3.4）；§4 实测 |
| `isp.csc_matrix` | `"bt601"` | §9.5 ⑨（ISP 侧 full）+ `native/rga_core.c` 的 `IM_YUV_TO_RGB_BT601_FULL` |
| `isp.rgb_range` | `"full"` | 同上 |
| `isp.nr_3d_ref_frames` | `src` / `enable` / `ref_frames = "IQ 未暴露"` / `ab`（三份报告路径 + 两段读数 + 结论） | §5；**取值对象经 O145 修订** |

**唯一代码改动** = `config.py` 的 `OPAQUE_SUBTREES` 增列 **`isp.presets` / `isp.fingerprint` / `isp.nr_3d_ref_frames`**（把 §10.4 已明列的三张子表声明为不下探，机制与 `pipeline.curve_geometry` 同）；否则 `config.load()` 会以「表外键」拒绝加载（已实测复现）。**不新增 config 键 / 日志事件 / 返回码**。

**回写**：`config.yaml` / 主基准 §9.5（O145 块）/ §10.3（本档登记）/ §10.4（三行）/ §14（追加 102）/ §15（**O145** + 索引行）/ 头部计数 / `docs/evidence/baseline-changelog.md`（§v3.86-追加102）/ `docs/plans/M1.md`（§3 #5、§5 键表）/ `docs/plans/M2.md`（§8.1 ①-5、§8.2 ②-③、§8.3、§8.6-§8.7）。

**配置快照变更**：`config.snapshot_hash()` 由 `0b99a46c5ab84990…` → **`8cf2fdbfafde1db9…`**；`pending_keys()` **8 → 3**（`hq.model_id` / `hq.pad_crop_policy` / `model.hq_fallback_id`，**全属 M2-2**）⇒ §13 形式报告已按新快照重跑（沿用 v3.86 追加 95 的先例），**并补齐 §6.5 的测量条件四项**（o95 报告该项为 `null`）：

| 报告 | `fps.window_*`（中位） | 端到端 P50 / P95 | 采集→提交 P95 | 提交→vblank P95 | 配对率 / 丢弃 |
|---|---|---|---|---|---|
| `M2-gate/bench_rt_o102.json` | 15.097 / 15.082 / 15.065（**15.082 ≥ 14.99**） | **34.72 / 51.14** ms（≤80，余量 28.9） | 42.25 | 16.197 | **1.0** / 四项全 0 |
| `M2-gate/bench_hq_o102.json` | —（该场景不判 fps） | 见下「HQ 响应」两判据 | — | 15.934（P95） | **1.0** / `discarded_triggers = 0` |

`bench_rt_o102` 另：`f_est = 7.583 Hz` / `alpha = 0.4642` ⇒ 收敛 **395.6 ms**（≤400 / ≤500）；`stream_on_count` 三轮均 1、`freeze_window = 0`、`metrics_policy_ok = true`；`max = 954.4 ms`（单次长尾仍在）。**HQ 轮（`bench_hq_o102.json`，`status = ok`、10/10 触发、`discarded_triggers = 0`、冻结窗 378.52 s）**：**§13「HQ 响应」两判据全过** —— **触发 → MIPI OSD 翻转** = 13.05 / **14.79** / 29.80 ms（min / 中位 / max；判据 **≤50 ms**）；**触发 → 全屏 HQ 视图出图** = 7 753 / **7 830** / 7 954 ms（判据 **≤12 s**）；`hq_active_shape = 960x544`。**⚠️ 随行**：10 次的 `exit_reason` **恒为 `timeout`**（非点击退出）⇒ §13「交互」行的"全屏点击退出（`reason=click`）"**仍未证**（与 `O96(f)` 同源）；`fs_hdmi_flip_ns` 恒为 0（HDMI 未接）。

## 8. 残留与未验（**不属阻断项**，不得读作"已达标"）

1. **AWB 未锁定**：`awb_lock = auto`（§4）⇒ §9.5 ⑤ 的「AWB 锁定」未落地、§15 **O64 仍未闭合**；本轮**不重建 IQ**（重建会改两套 sha256 ⇒ 使 O91 与已归档报告的测量条件失效）。
2. **R23 分场景缓解未落地**：三档同值 ⇒ §9.5 的 O91 残留① （高照度过曝）**未缓解**；按 O94 的「两段式」留待 M2 通路可用后的**观感终审**。
3. **`isp_fingerprint` 事件的启动打印未实现**：`log.py` 已登记该事件（字段 `iq_hash` / `gamma` / `ccm` / `black_level` / `csc_matrix` / `rgb_range` / `awb_lock`），但**全仓无发出点**（`main.py` 未 emit）⇒ §9.5 的"启动时打印进日志"**尚未落地**（原"随存图落盘"载体已随 O102 取消）。
4. **O94 的四项盲区不变**：3DNR 时域拖尾 / 闪烁、实时链帧率与延迟、全屏 HQ 视图观感、双屏镜像效果 ⇒ 随 M2 通路复验。
5. **本批无照度标注**（`labels.lux = null`）⇒ S2 的量化只作**同条件对照**，不得读作"某照度下"的合格值。
6. **IQ 探针未入库**（`/tmp/iq_fp*.py`）⇒ `isp.fingerprint` 的**机械复核**当前无仓内工具（子项哈希口径已在本档 §4 写死，可据此重建）。

## 9. 复现命令汇总

```bash
# S1 档位与回读
sudo python3 tools/collect/iq_sets.py status
sudo v4l2-ctl -d /dev/v4l-subdev2 --get-ctrl exposure,analogue_gain

# S2 双通路单帧出图（演示服务先停）
sudo systemctl stop starlight-demo
python3 tools/realtime_frame_review.py --frames 300 --snr-frames 100 --scene darkroom_demo --prefix /tmp/m1_5/rt
python3 tools/hq_frame_review.py --prefix /tmp/m1_5/hq
sudo systemctl start starlight-demo          # 恢复

# S3 3DNR A/B（两档唯一差异 = 3DNR；跑完须切回 demo 档）
sudo python3 tools/collect/iq_sets.py switch collect
sudo PYTHONPATH=/home/cat/.local/lib/python3.9/site-packages python3 tools/bench.py \
    --scenario idle-link --breakdown --rounds 3 --frames 300 \
    --weight-hash ca8855b90df9a80fa4195a831f33d3476b1964f787eb70602797c773067f3b84 \
    --curve-level 480x272 --precision fp16 --config-hash 0b99a46c5ab849907610472b853efeb3dbf9fe65286bdb2f47e15ceb626aa5db \
    --out /tmp/idle_3dnr_off.json
sudo python3 tools/collect/iq_sets.py switch demo    # 同命令 → /tmp/idle_3dnr_on.json

# S5 键表自检
python3 config.py                                    # pending_keys() 应为 3（全部属 M2-2）
```