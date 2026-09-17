# M0 回归项②：config 键完整性（逐键勾选表）

| 项 | 内容 |
|---|---|
| 地位 | **记录类子文档（C 层）**，承载 §11.2 的 M0-② 回归项「config 键完整性」的逐键勾选结果 |
| 效力 | **非验收依据**：验收只认主基准 §13 的复现方式 + §11.2 的回归项 + `bench.py` 报告（规则 `00-ssot.md` §3.5） |
| 对应主基准节 | §10.4（键表，唯一 SSOT）、§11.1-1/-2/-13、§11.2（M0 行 ②）、§15（O30 / O31 / O34）；规则 `40-gate.md` §1.3、`20-contracts.md` §1 |
| 状态 | `draft`（**v3.29 更新**：O34 已裁定收口、O30 已裁定为「暂时挂起」⇒ 本项维持「未过」，M0 出口判定挂起） |

> **判据**（§11.2 M0-②、规则 `40-gate.md` §1.3）：§10.4 中「定稿时点 = M0」的键**零空缺**——**空值即未定稿，禁止留默认值顶替**。
> 本表逐键勾选，**不做取值裁定**：取值一律照抄 §10.4 的「取值来源 / 依据」列，或在 §10.4 未给字面值时取**已落盘证据**（见各行"证据"列）。
> 落盘载体 = 仓库根 `config.yaml`（M0 首次创建）。

---

## 0. 结论速览

| # | 结论 | 判定 |
|---|---|---|
| 1 | §10.4 中「定稿时点 = M0」的键共 **32 个**：**已填 27**（其中 1 个为**推导值、待复核**）+ **`provisional` 1** + **空缺 4** | ❌ **未过** |
| 2 | 4 个空缺**全部**由同一根因阻塞：**O30（保底 CNN 选型与权重获取）⇒ M0-4 未完成 ⇒ `hq.active_shape` / `hq.pad_crop_policy` 无法定版**。**（v3.29）O30 已裁定为「暂时挂起」** ⇒ 该 4 项空缺是**已知的、持续存在的状态**（用户显式接受），**不因本项而解除** | 已定位 |
| 3 | 空缺**不得**用默认值顶替（§11.2 明文）；`hq.shape_ladder` 的取值为 §10.4 字面值，**不等于** `active_shape` 已定 | 已遵守 |
| 4 | §10.4 的 M0 范围内存在 **4 处"键名/口径未定义"**（"横竖屏"、三档 shape 表、`camera.gain_*` 通配键名、`camera.rkisp_node` 口径）。**（v3.29 裁定 O34 后已全部收口）**：①② **不另设键**（由现有键承载）；③ 属 **M1** 定稿键（不参与 M0 判定）；④ 口径 = ISP 的 media 设备 `/dev/media1` ⇒ **判据恢复可判定，不再阻塞本项** | ✅ 已裁定 |

---

## 1. 判定口径

1. **键名范围**：只认 §10.4 表中**以反引号给出的键名**。表外键**禁止**出现在 config（规则 `20-contracts.md` §1）——因此本表不对"横竖屏""三档 shape 表"自造键名（见 §4）。
2. **空缺的定义**：`null` 或缺失。**`null` ≠ 默认值**：本工程以显式 `null` 标"未定稿"，绝不以"看起来合理的默认值"占位（§11.2 明文禁止）。
3. **非 M0 键**：定稿时点晚于 M0 的键在本阶段**允许为空**（`docs/plans/M0.md` §5），一律写 `null`，不参与本表判定；清单见 §5。
4. **状态三态（+1）**：`已填` / `已填（推导值·待复核）` / `provisional` / `空缺`。其中 `provisional` 只可用于 §10.4 明列的代用数据项（O7），清单见 `docs/evidence/M0-reg-provisional.md`。

---

## 2. M0 键逐键勾选（32 键）

> 取值列 = `config.yaml` 中的实际落盘值（点号路径 = §10.4 的键名，本文件写作嵌套）。

### 2.1 硬件（4/4 已填）

| §10.4 键 | 落盘取值 | 收口状态 | 证据 |
|---|---|---|---|
| `board.model` | `"Embedfire LubanCat-5 V2"` | ✅ 已填 | `/proc/device-tree/model`（只读；`docs/evidence/M0-1-hardware-baseline.md`、`environment/board-lubancat5.md` §1） |
| `board.cam_port` | `"CAM0"` | ✅ 已填 | 丝印口 + overlay 名（同上 §4/§7） |
| `board.cam_lanes` | `4` | ✅ 已填 | 驱动 `detect imx415 lane 4`（同上） |
| `board.overlay` | `"rk3588-lubancat-5-cam0-imx415-1920x1080-60fps-overlay.dtbo"` | ✅ 已填 | `environment/board-lubancat5.md` §7.2（含备份 sha 与恢复步骤） |

### 2.2 采集（3/3 已填）

| §10.4 键 | 落盘取值 | 收口状态 | 证据 |
|---|---|---|---|
| `camera.video_node` | `"/dev/video11"` | ✅ 已填 | `rkisp_mainpath`（M0-1 §6.3） |
| `camera.rkisp_node` | `"/dev/media1"` | ✅ 已填（**口径已裁定**，见 §4-④） | `/dev/media1` = `rkisp0-vir0`（ISP 的 media 设备）；`/dev/media0` = `rkcif-mipi-lvds`（M0-1 §6.5）。**v3.29 裁定 O34-④：本键 = ISP 的 media 设备**，采集出口单列于 `camera.video_node` |
| `camera.mainpath_only` | `true` | ✅ 已填 | §3.1（勿开 rawpath） |

### 2.3 图像（1/1 已填）

| §10.4 键 | 落盘取值 | 收口状态 | 证据 |
|---|---|---|---|
| `rga.core_mask` | `3` | ✅ 已填 | M0-13 §1.4（mask 0/1/3 无差、mask 2 略快 2.4%；取 3 为 M2 并发留核） |

### 2.4 显示（9 键：8 已填 + 1 推导待复核）

| §10.4 键 | 落盘取值 | 收口状态 | 证据 |
|---|---|---|---|
| `display.screen_plugin` | `"mipi1080p"` | ✅ 已填（**「横竖屏」已裁定不另设键**，见 §4-①） | overlay `…dsi0-vp2-1080x1920-5.5inch-ebf410125…`；connector 原生 mode 1080×1920（竖屏） |
| `display.dsi_connector` | `"DSI-1"` | ✅ 已填 | `card0-DSI-1` status=**connected** enabled=enabled（本轮只读探测） |
| `display.hdmi_connector` | `"HDMI-A-1"` | ✅ 已填（**显示器未接**） | `card0-HDMI-A-1` status=**disconnected**（无 EDID ⇒ 无 mode list；不影响本键，`hdmi_mode_selection` 定稿时点为 M2） |
| `display.mipi_canvas` | `[1080, 1920]` | ✅ 已填 | §3.4 布局自洽式；与 connector 原生 mode 一致 |
| `display.hdmi_canvas` | `[1920, 1080]` | ✅ 已填 | §3.1（两层 SSOT 的 buffer 尺寸层） |
| `display.panel_refresh_hz` | `60` | ✅ 已填（**实测**） | `real_clk 131376 kHz ÷ (htotal 1120 × vtotal 1955)` = **60.00 Hz**，与 mode 名 `1080x1920p60` 自洽 |
| `display.mode_htotal` | `1120` | ✅ 已填（**实测**） | `H: 1080 1090 1100 1120` |
| `display.mode_vtotal` | `1955` | ✅ 已填（**实测**） | `V: 1920 1930 1935 1955` |
| `display.dsi_lane_rate` | `788`（Mbit/s/lane） | ⚠️ **已填（推导值·待复核）** | 推导 = `131376000 Hz × 24 bit ÷ 4 lane`；输入取自实测（`bus_format RGB888_1X24`、`dsi,lanes=4`、`dsi,format=0`）。**本平台无回读口**（DT 无 `rockchip,lane-rate`；debugfs 未暴露）⇒ 须在 **M0-9** 实现期由驱动侧/PHY 回读复核 |

> 原始输出与复现命令见 `docs/evidence/reports/M0-1/drm_probe.txt`（2026-09-18 只读探测）。

### 2.5 链路（2 键：1 `provisional` + 1 已填）

| §10.4 键 | 落盘取值 | 收口状态 | 证据 |
|---|---|---|---|
| `pipeline.curve_level` | `"480x272"` | 🟡 **`provisional`**（§10.4 明列的代用数据项，O7） | v3.22 裁定 O28-Q2 维持默认档；**M0-3 的 A/B 定档尚未执行** ⇒ 详见 `docs/evidence/M0-reg-provisional.md`。「三档 shape 表」已裁定**不另设键**（见 §4-②） |
| `pipeline.curve_geometry` | 有效区/pad 表 + `pad_fill=zero` + `pad_mechanism=rga_improcess_zero_source_at_pool_init` + `output_take=effective_only` | ✅ 已填 | §3.1 契约 G1（v3.7 裁定 O1）+ v3.27 裁定 O32 修订的机制；M0-13 §1/§2 实测支撑 |

> **注**：`pipeline.curve_field_precision` 的**定稿时点 = M2**（§10.4），故**不属本表判定范围**；其临时期值 `fp16` 已落盘且**不构成 M0 缺口**（v3.22 裁定 O23 明文）。

### 2.6 HQ（4 键：1 已填 + 3 空缺）

| §10.4 键 | 落盘取值 | 收口状态 | 证据 / 依赖 |
|---|---|---|---|
| `hq.shape_ladder` | `["960x544", "768x432", "512x288"]` | ✅ 已填 | §10.4 字面值（§3.1/§6.2/§9.3 的固定梯度） |
| `hq.model_id` | `null` | ❌ **空缺**（v3.29：O30 已裁定为**暂时挂起** ⇒ 保持空缺） | 依赖 **O30**（保底 CNN 选型与权重获取）+ M0-4 重启；上游权重横比结论为代用数据、M1 实拍集复检（§4.1 v3.23 补注） |
| `hq.active_shape` | `null` | ❌ **空缺**（同上） | **M0-4** 三档 R9 闸口当天定版（§9.3、§11.1-4）——现为**占位** |
| `hq.pad_crop_policy` | `null` | ❌ **空缺**（同上） | 随 `active_shape` 一并定版（同上） |

### 2.7 模型（6 键：5 已填 + 1 空缺）

| §10.4 键 | 落盘取值 | 收口状态 | 证据 / 依赖 |
|---|---|---|---|
| `model.zde_weight_hash` | `"ca8855b90d…f3b84"`（Epoch99.pth sha256） | ✅ 已填 | `docs/evidence/reports/M0-2/export_onnx.json` 的 `weights_sha256`。**权重哈希 ≠ `.rknn` 文件哈希**（§10.2：`.rknn` 非 byte-reproducible） |
| `model.zde_output_channels` | `3` | ✅ 已填 | §4.1 / 官方 Zero-DCE++ 契约（同一曲线场复用 8 次） |
| `model.zde_output_layout` | `"NCHW"` | ✅ 已填（**`rknn_query` 实测**） | `docs/evidence/reports/M0-2/rknn_out_attr_query.json`（`out_attr.fmt`）；**未预设** NCHW/NHWC（§10.4 纪律） |
| `model.zde_output_dims` | `[1, 3, 272, 480]` | ✅ 已填（**`rknn_query` 实测**） | 同上（`out_attr.dims`）；同报告含 256×144 `[1,3,144,256]` / 960×544 `[1,3,544,960]` |
| `model.zde_ft_version` | `"none"` | ✅ 已填（**实测事实**） | 当前权重 = 官方 `Epoch99.pth`，**未做实拍域 fine-tune**（预研方的 `best/final.pth` 不在归档内，§2.4/P3）；M1 按 **O11** 计划（权重一经变更即全量重跑） |
| `model.hq_fallback_id` | `null` | ❌ **空缺**（v3.29：O30 已裁定为**暂时挂起** ⇒ 保持空缺） | 依赖 **O30**（KinD++/RetinexNet 权重不可达；SCI 已落位但**本轮不评估**） |

### 2.8 运行时（3/3 已填）

| §10.4 键 | 落盘取值 | 收口状态 | 证据 |
|---|---|---|---|
| `runtime.toolkit2_ver` | `"2.3.2"` | ✅ 已填 | 三臂 A/B（`docs/evidence/M0-2-ab-toolkit-version.md`）+ M0-4 的 HQ FP16 编译确认门通过（§2.2） |
| `runtime.librknnrt_ver` | `"2.3.2"` | ✅ 已填 | 板上实测 `2.3.2 (429f97ae6b@2025-04-09)`（升级已执行，备份 `.bak.1.5.0_20260917`） |
| `runtime.rknpu_driver_ver` | `"0.9.8"` | ✅ 已填 | `/sys/kernel/debug/rknpu/version`（≥0.9.2 下限 ✓） |

---

## 3. 空缺清单与依赖（4 项）

| # | 键 | 阻塞根因 | 解除动作 | 依据 |
|---|---|---|---|---|
| 1 | `hq.model_id` | **O30**：保底 CNN 候选（KinD++/RetinexNet）权重在本机不可达；SCI 未评估 ⇒ 正式 HQ 与保底两条路都未定。**（v3.29）O30 已裁定为「暂时挂起」**（不选型、不预转换、不重启 M0-4）⇒ 本空缺**持续存在** | 重启触发条件：用户提供候选权重 **或** 指示改用 SCI **或** 上述 M0 其余项完成；重启后按原路径裁定并定版 | §4.1 v3.23 补注 + **v3.29 第 4 条**、§15 O30、`docs/plans/M0.md` §3-4/-12 |
| 2 | `hq.active_shape` | M0-4（三档 R9 机械闸口 + 当天定版）未做 | 同上 | §9.3、§11.1-4 |
| 3 | `hq.pad_crop_policy` | 同上（随 active_shape 定版） | 同上 | §10.4、§11.1-4 |
| 4 | `model.hq_fallback_id` | **O30** | 同第 1 项 | §4.1、§15 O30 |

> **不得以默认值顶替**：以上 4 项在 `config.yaml` 中一律显式 `null`。**§11.2-② 因此判为未过**，M0 不得出口（规则 `40-gate.md` §1.3/§5）——**该状态由用户在 v3.29 显式接受**（O30 = 暂时挂起），即：**M0 先做其余可做项，出口判定挂起**。

---

## 4. 键名/口径问题四项（**v3.29 裁定 O34 后已全部收口**）

| # | 问题（登记时） | 裁定结果（v3.29，§15 O34） | 对 `config.yaml` 的影响 |
|---|---|---|---|
| ① | §10.4 显示域该行写「`display.screen_plugin=mipi1080p`（勿 `mipi600p`） / **横竖屏**」，**"横竖屏"未给键名** | **不另设键**：由 `display.screen_plugin` + `display.mipi_canvas=1080×1920` 共同承载（connector 实测原生 mode = **1080×1920 竖屏** 佐证）；**将来若支持横屏插件，须先在 §10.4 该行定义键名再落 config**（不得自造表外键，规则 `20-contracts.md` §1） | 无（键值不变） |
| ② | 链路域该行写「`pipeline.curve_level`（默认 480×272）+ **三档 shape 表**」，**未给键名** | **不另设键**：由 `pipeline.curve_level` 的**取值域**（三档 level 名）+ `pipeline.curve_geometry` 的 `effective`/`pad_to` 子表共同承载 | 无（已在 `curve_geometry` 内落盘） |
| ③ | 采集域 `camera.gain_*` 是**通配键名**（§10.4 自注"实测重标结构"） | **该行定稿时点 = M1** ⇒ **子键名随 M1 的 `v4l2-ctl -L` 实测一并定义**；**不参与 §11.2 M0-② 的判定**（与 `camera.mode_class` 同口径），**不另登记残留 O 项** | 无（M0 阶段仅留注释说明） |
| ④ | `camera.rkisp_node` 的**"rkisp 节点"口径未定义**（video 节点 vs media 设备） | **口径 = ISP 的 media 设备 `/dev/media1`（`rkisp0-vir0`）**；**采集出口单列于 `camera.video_node`（`/dev/video11`）** ⇒ 两键**不得同值** | 无（落盘值早已是 `/dev/media1`） |

> **判定效力**：四项收口后，§11.2 M0-② 的判据（"定稿时点 = M0 的键零空缺"）**恢复可判定**，O34 **不再阻塞该回归项**；**M0-② 仍为「未过」的唯一原因是 §3 的 4 项空缺**（根因 O30 暂挂）。裁定细节见主基准 §10.4 相应行与 §15 的 O34 行。

---

## 5. 非 M0 键清单（本阶段允许为空，不参与判定）

`config.yaml` 中一并落盘为 `null`（**未定稿**，非"取值未知"），供后续阶段逐个转定稿：

| 定稿时点 | 键 |
|---|---|
| M1 | `camera.mode_class` / `camera.fps` / `camera.exposure_us` / `camera.gain.*` / `isp.presets` / `isp.fingerprint.*` / `isp.csc_matrix` / `isp.rgb_range` / `isp.nr_3d_ref_frames` / `trigger.sources` / `trigger.debounce_ms` / `trigger.interval_s` / `metrics.window_frames` / `metrics.timestamp_domain` / `metrics.timestamp_source` / `metrics.pair_by` / `metrics.idle_link_baseline` |
| M2 | `display.hdmi_mode_selection` / `pipeline.apply_scope` / `pipeline.curve_upsample` / `pipeline.est_freq_hz` / `pipeline.ema_alpha` / `pipeline.curve_field_precision`（**已有临时期值 `fp16`**，仅待转 final）/ `ui.*` / `metrics.report_breakdown` / `logging.*` |
| M3 | `hq.hold_policy` / `hq.fade_ms` / `hq.mipi_preview_ms` / `hq.mipi_preview_rect` / `storage.*` |

> `storage.capacity_limit_gb` 另受 **O21**（介质与容量实测）阻塞；`metrics.idle_link_baseline` 是 **O19** 的触发依据。二者均**不阻塞 M0**。

---

## 6. 与 §11.2 的判定

| 回归项 | 判定 | 说明 |
|---|---|---|
| ② config 键完整性（M0 行） | ❌ **未过** | 32 个 M0 键中 **4 项空缺**（§3，根因 = **O30 已裁定为「暂时挂起」**）。键名/口径问题（§4）**已由 O34 裁定收口**，不再影响判定；`display.dsi_lane_rate` 仍为**推导值待复核**（M0-9）。**未过项不得以文字说明代替证据路径**（规则 `40-gate.md` §4） |

**当前处置（v3.29，用户裁定）**：**O30 = 暂时挂起** ⇒ 本项**维持「未过」且不推进解除**；**M0 先做其余可做项**（M0-3 / M0-6 / M0-7 / M0-9 / M0-10 与 §11.2-①），**M0 出口判定挂起**。**O34 已解除**（不再阻塞本项）。**本项未过期间，M0 不得出口**（§11.2 纪律：不得用"下一阶段补跑"顶替），且**空缺不得用默认值顶替**。