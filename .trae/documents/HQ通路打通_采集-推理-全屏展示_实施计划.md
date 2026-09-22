# HQ 通路打通：HQ 采集 → HQ 推理 → 全屏展示（实施计划）

> **性质**：实施计划（B 层，非契约）。**权威顺序**：主基准 §3.1 / §3.4 / §3.5 / §5 / §6.x / §10.3 / §10.4 / §13 / §15 > 本文件。
> 本文件**不引入任何新取值**；遇到需取值 / 需裁定的点，一律**先裁定后回写**（规则 `00-ssot.md` §3.2、§4）。

## 1. Context（为什么做、要做到什么）

现状（已勘察）：TH3 `keyframe_infer` 推理已能跑（core_mask=4、960×544 wall 中位 ~10.0 s），但**HQ 结果槽无人消费**（`pop_result()` 从未被调用）；**触发源未接线**（只有 `--trigger-at` 帧号钩子）；**全屏 HQ 视图零实现**（全仓无 fullscreen 代码）；**常规态 HDMI 镜像未接**（HDMI 只画"未触发"黑底）；**O58 几何硬编码**仍在 `display_mipi._geom()`。

本轮目标（用户已选定范围）：**打通 HQ 采集 → HQ 推理 → 全屏展示**，且**同轮落地常规态 HDMI 1.875× 镜像**（两屏同画面架构闭合）、**同轮解掉 O58**；**不含**集合 D 存图 / `save_set_abcd`（O72）与「存图」键接线。

**铁律**：不新增 config 键 / 日志事件 / 返回码；不改 §13；不放宽 §11.2；构建零告警；契约签名逐字不变（`display_hdmi.publish` 已随 O67 **取消**，不得实现）；链路输出**原样归档、零色调后处理**（规则 `10-evidence.md` §2）。

已登记可用事件（`docs/logging.md` §4，**不得自造**）：`capture/pointer_device`（O81）、`display_mipi/freeze_enter|freeze_exit`（`seq=`）、`display_mipi/fullscreen_enter|fullscreen_exit`（`seq=`、`reason=click|timeout`）、`keyframe_infer/hq_start|hq_done`、`display_hdmi/hotplug`、`rsm_transition`。

## 2. 分步实施（每步可独立验证；串行依赖见"依赖"列）

| 步 | 目标 | 主要改动 | 依赖 | 自测 |
|---|---|---|---|---|
| **S1** | **几何单源（解 O58）** | 扩展 `curve_estim.resolve_geometry()`（v3.86 追加 35 已声明其为几何单源）增加**视图段**：两窗、OSD 区、按钮区、全屏 HQ 粘贴矩形、HDMI 镜像/全屏缩放矩形；`display_mipi._geom()`、`osd.py` 的 `OSD_Y/OSD_H`、`ui_buttons.py` 的 `BUTTON_*` 全部改为读该单源，删除硬编码 | — | 开发机：坐标断言（见 §3 表）；构建零告警 |
| **S2** | **指针输入（O81）** | 新 `pointer.py`（复用 `capture.TouchReader` 作底层 evdev 非阻塞读取）：触控（绝对，`EVIOCGABS` 折算到 1024×600）+ 鼠标（`REL` 累加 + `BTN_LEFT` 边沿，初值画布中心、clamp）；统一输出 `(x, y, click_edge)`；启动各发一次 `capture/pointer_device` | — | 板端：只记日志不触发，核对坐标域/`present` |
| **S3** | **RSM 收敛（O80）** | `capture.py` 删 `MIPI_PREVIEW_ONLY`（四态：BOOT_CHECK/RUN_DEMO/SHUTDOWN/FAULT_EXIT）；`display_hdmi.on_hotplug()` 只发日志不迁移；`RUN_DEMO` 由 `main.py` 显式置 | — | 板端 600 帧内拔插 3 次：无迁移、无异常 |
| **S4** | **RGA RGB 缩放原语 + 常规态镜像（O78/O85）** | `native/rga_core.{h,c}` 新增 `rga_scale_rgb888(src_va, sw, sh, dst_va, dw, dh)` + `rga_last_scale_ns()`（口径同 `docs/evidence/reports/display-arch-change/rga_mirror_probe.c`：`improcess` + srect/drect）；`native/starlight_native_pybind.cpp` 暴露为 `_n.rga.scale_rgb888`；`display_hdmi.py` 新增 `mirror()`（读槽 0 画布 VA → 放大写槽 1 → `drm.compose_flip_slot(1, seq)`，写前 `wait_flip_slot(1)` 上限 1 vblank，超时计 `flip_skipped_slot` 且**不阻塞 MIPI**）；`main.py` 常规态每帧调用 | S1 | ① fd/VA 与 probe 逐像素一致；② 镜像 P50 ≤3 ms（§6.1 O85 行）；③ 300 帧 fps vs §11.2 锚（fps 22.3 / P50 91.9 ms，劣化 ≤20%） |
| **S5** | **`pop_result` 消费 + 全屏 HQ 视图（O79）** | `main.py` 在 `kf.busy()` 转闲分支内 `pop_result()` → NCHW f32 → RGB888 常驻缓冲（`clip(0,1)*255`，口径同 `tools/hq_eval.py`）→ 置视图态 `fullscreen_hq` → 发 `fullscreen_enter(seq, reason='click')`（reason 值域只有 `click|timeout`，进入也取 `click` = "由那次点击触发的 HQ"）；`display_mipi.compose_flip` 内按 `state['view']` 走全屏分支（HQ 960×540 **1:1 居中**贴 (32,30)，左右 32/上下 30 黑边，**OSD 与按钮区不画**）；`display_hdmi` 全屏态走 `show_hq()`（HQ→1920×1080 **2.0×**）；退出 = 内容区任意点击或 `ui.fullscreen_timeout_ms=30000`，发 `fullscreen_exit(reason)` | S1,S4 | 板端 1 次点击：触发→两屏全屏 ≤12 s；PNG 证据见 §7 |
| **S6** | **O90 冻结 + 计量口径** | 触发被受理起冻结：跳过 `cap.acquire`/`est.estimate`/`ra.apply_window`/`osd.update_stats`，只保留指针轮询、`hdmi.poll()`、TH3 完成检查；冻结首帧用最后画布**重画一次**（叠加 OSD 次行"处理中"）并 flip ⇒ 满足"触发→OSD ≤50 ms"；发 `freeze_enter/freeze_exit(seq)`；**fps 改活动窗口径**（`frames / (wall − Σ冻结窗)`，冻结窗从延迟/配对窗排除，沿用 §8.1 O5 冻结窗先例）；`hq_flips_delta` 这类"HQ 在途仍翻页"的证据口径**随 O90 作废**，改记 `freeze_flips`；`--trigger-at` 由**帧号**改**时间/次数**注入（冻结期帧号不推进） | S3,S5 | 板端 10 次触发：`freeze_enter/exit` 成对、fps 不被冻结拉低、配对率不变 |
| **S7** | **验证工具与证据** | `tools/bench.py` 的 `--scenario hq` 落地（现为 `not_implemented` 空壳）：时间注入 ≥10 次触发 + `--seq-log` + 触发→OSD/触发→全屏耗时 + `discarded_triggers` + 双屏 `mono_ns`/`seq` 配对 + `fullscreen_enter/exit` 的 `reason`；`main.py` 增 `--dump-canvas-dir`（**复用 `tools/realtime_frame_review.py` 的 `write_png`**，stdlib zlib）：常规态与全屏态各导 MIPI 1024×600 + HDMI 1920×1080 原样 PNG | S4,S5 | 见 §7 |
| **S8** | **文档回写** | 见 §8 | S1–S7 | 闸门自检（规则 `40-gate.md` §4） |

## 3. 几何单源（O58 就地裁定口径）

单源 = **`curve_estim.resolve_geometry()`（扩展视图段）**；`acquire/render` 侧只读该单源。**不新增 config 键**（全部由既有键推导）。

| 对象 | 坐标（MIPI 1024×600 域） | 推导来源 |
|---|---|---|
| 原图窗 / 增强窗 | (0,0,512,288) / (512,0,512,288) | `curve_estim.WIN_W/WIN_H` |
| 增强窗命中区 | x 512–1023, y 0–287 | §3.4 |
| OSD 指标区 | (0,288,1024,216) | `display.mipi_canvas` − `ui.osd_metrics_px` |
| 按钮区（3 键各 341） | (0,504,1024,96)，x=0/341/682 | `ui.button_layout` + `ui.hit_area_min_px` |
| 全屏 HQ 贴图 | 960×540 @ (32,30) | `hq_w` / `hq_content_h` 与画布差整除 |
| HDMI 常规镜像 | 1024×600 → (0,0,1920,1080)（1.875×） | `display.hdmi_canvas` |
| HDMI 全屏 HQ | 960×540 → (0,0,1920,1080)（2.0×） | 同上 |

> O58 原登记只指 `display_mipi._geom()`；本次**扩到 `osd.py` / `ui_buttons.py`**，属就地裁定，须在 §15 O58 行写明。

## 4. 视图状态机 / 契约

- 两态 `normal` / `fullscreen_hq`；**不另立第三套状态机**（§3.5）；`ControlState` 只**加只读字段** `view`、`cursor_pos`（签名逐字不变，第三参仍为 state dict）。
- 光标：画进 MIPI 画布**最上层**（`osd.render_into` 之后、`flip` 之前），原生小方块/十字；镜像下两屏都出现（O81 预期）。
- OSD：常规态照常；全屏态**整块不画**（OSD + 按钮区以黑边呈现）。
- 触发 FSM 与 `discarded_triggers` 由 `capture.py` 既有实现承担；指针侧**不再另加防抖**（`trigger.debounce_ms=50` 由 FSM 承担）。
- 「复位·退出」：短按软复位、长按 ≥ `ui.reset_hold_ms` 退出（`ui_buttons.classify_reset`）；「存图」键**本轮不接线**（登记残留）。

## 5. 指针输入

- 节点：触控 = 板端 `event2`（Goodix，模块常量 + `--pointer-touch-node` 调试覆盖，先例 `--trigger-at`）；鼠标 = `ui.pointer_mouse_node` 落值 **`/dev/input/event7`**（板端实测 TESORO H3E；同名 `event11` 事实记入证据档作为定稿依据）。
- 无鼠标/节点打不开 ⇒ 该源停用（触控仍可用）；两者皆无 ⇒ 仅记 `capture/pointer_device present=false`，触发依赖 `--trigger-*` 调试注入。
- 坐标域统一 = **MIPI 画布 1024×600**；全屏态下**内容区任意点击即退出**（不再判命中区）。

## 6. TH3 结果消费与冻结（关键点）

- 消费点：`main.py` 现有 `kf.busy()` 转闲分支；**槽空即跳过**，TH4 不因结果槽空锁帧（§5 原文 + O90 限定注）。
- 冻结窗内**不取帧**（O90）⇒ 帧号钩子失效，故 S6 把注入改为时间/次数；`--frames` 仍按帧数（冻结期不推进）——须在文档写明"`--frames` 统计口径 = 活动帧"。
- 内存：新增 HQ RGB 常驻 960×540×3 ≈1.6 MB；HQ 模型 2.7 GB 常驻不变（R16 残留随行）。

## 7. 验证方案（可复现 + 可贴图）

| 项 | 命令 / 方法 | 记录 |
|---|---|---|
| 常规镜像 | `python3 main.py --frames 300 --dump-canvas-dir storage/hq_path/` | 镜像 P50/P90（≤3 ms）、fps/P50 vs §11.2 锚 |
| HQ 响应 | `tools/bench.py --scenario hq --seq-log`（≥10 次触发） | 触发→OSD ≤50 ms、触发→两屏全屏 ≤12 s、`seq` 配对、`discarded_triggers` |
| HQ 展示 / 交互 | 点击 ≥3 + 全屏进出各 ≥3 + 30 s 超时 1 次 | `fullscreen_enter/exit` 的 `reason`、双屏 `mono_ns`、`seq` |
| **可贴图证据** | `--dump-canvas-dir` 导出：常规态 {MIPI 1024×600, HDMI 1920×1080}、全屏态 {MIPI, HDMI} | **链路原样**、零色调后处理；**先贴图给你目视**，再出量化与结论（用户纪律） |
| 双屏时序 | 每帧 `drm.last_flip_ns_slot(0/1)` + `last_flip_seq_slot` | 两屏 vblank 时间戳差、`flip_skipped_slot` |
| OSD 隐藏 | 全屏态导出图（OSD/按钮区不在） | §13「OSD 全屏下隐藏」 |
| 开发机自检 | 键表零改动、`view_geom` 坐标断言、构建零告警、扩展后缀取自 `python3-config --extension-suffix` | 规则 `40-gate.md` §2 |

## 8. 回写清单（改完即办，规则 `00-ssot.md` §4）

- **§15**：**O58 就地裁定**（单源 = `curve_estim.resolve_geometry()` 视图段；扩展至 osd/ui_buttons）；**O90 落地**（冻结 + 活动窗 fps + `freeze_flips`）；O78/O79/O80/O81/O85 状态推进；**O67 补注**（未实现 `publish`，镜像/全屏以 `mirror()`/`show_hq()` 承担，**非 publish 语义**）；新登记"`bench.py --scenario hq` 空壳""触发注入钩子由帧号改时间"。
- **§10.3**：新增 `pointer.py`，`curve_estim` 视图段，`native/rga_core` 新原语与绑定，`tools/bench.py` 场景。
- **§10.4**：`ui.pointer_mouse_node` 落值 + 依据（evdev 枚举证据）；**键表零增删**。
- **§14**：本批变更记录（含"§13 零改动、§11.2 零放宽"声明）。
- `docs/plans/M2.md`：勾选新子项 + 证据链接；`docs/evidence/M2-2-hq-align.md`（或新档）新增章节。
- **残留登记**：`hq.pad_crop_policy` 仍空（临时口径 = `resolve_geometry` 的 `hq_content_h/hq_pad_top`，O94 允许）、`hq.model_id` / `model.hq_fallback_id` 空（O30/O72）、集合 D 存图未做（O72）、R16 内存、镜像 fps 余量。

## 9. 风险与取舍

| 风险 | 处置 |
|---|---|
| 镜像后 fps 余量小（测算 ≈21.3 fps，floor ≥14.99） | 实测优先；§6.1 该行**单列**不计入帧路径合计；超标先查口径再议裁定 |
| 冻结使 fps/延迟统计失真 | 冻结窗从 fps/延迟/配对窗**排除**，报告随带冻结起止 |
| RGA fd/VA 语义差异 | 新原语必须与 `rga_mirror_probe.c` **逐像素比对一次**，否则镜像可能静默错误 |
| HDMI 缺席 | `_drm_ok=False` ⇒ 只记日志不出图；全屏在 MIPI 侧照常（O80 任一单显） |
| `pad_crop_policy` / `model_id` 空缺 | 沿用临时口径（O94/O30），登记残留，不阻塞本轮 |
| 单线程主循环被 `wait_flip_slot` 拖住 | 上限 1 vblank，超时跳过并计数 |

## 10. 关键文件

`main.py`（编排/冻结/消费/导出）、`display_mipi.py`（视图态/光标/几何）、`display_hdmi.py`（镜像/全屏）、`curve_estim.py`（几何单源扩展）、`osd.py` / `ui_buttons.py`（几何接入）、`pointer.py`（新）、`native/rga_core.{h,c}` + `native/starlight_native_pybind.cpp`（缩放原语）、`tools/bench.py`（hq 场景）、`config.yaml`（仅 `ui.pointer_mouse_node` 落值）。
## 11. 执行状态（v3.86 追加 37 收尾）

| 步 | 状态 | 说明 |
|---|---|---|
| S1 几何单源（解 O58） | ✅ 完成 | `curve_estim.resolve_geometry()` 增视图段；`display_mipi` / `osd` / `ui_buttons` 改读单源、硬编码已删 |
| S2 指针输入（O81） | ✅ 完成 | 新增 `pointer.py`；`ui.pointer_mouse_node` 落值 `/dev/input/event7`（依据 = §10.4 / 证据档 §8.9）；**真实点击未实机验证**（O96(f)） |
| S3 RSM 收敛（O80） | ✅ 完成 | 四态；`on_hotplug()` 只记日志；**热插拔物理拔插未跑**（O96(h)） |
| S4 RGA 缩放原语 + 常规态镜像（O78/O85） | ✅ 完成 | 新增 `rga_scale_rgb888` **与 `rga_scale_rgb888_fd`**；**镜像路径走 `fd`**（A/B 定因，证据档 §8.5）；链内 P50/P90 **待重测**（O96(b)） |
| S5 `pop_result` 消费 + 全屏 HQ 视图（O79） | ✅ 完成 | 触发→全屏 **8.01 / 8.03 s ≤12 s**；`click` 退出路径待复验 |
| S6 O90 冻结 + 计量口径 | ✅ 完成 | 冻结窗 38.04 s 已从 fps 分母排除；`hq_flips_delta` 作废、改记 `freeze_flips` |
| S7 验证工具与证据 | ✅ 完成 | `tools/bench.py --scenario hq` 落地（≥10 次触发 + `--seq-log` + seq 级明细）；`main.py --dump-canvas-dir` 原样导图；**≥10 次触发验收跑未做**（O96(g)） |
| S8 文档回写 | ✅ 完成 | §6.1 / §10.3 / §10.4 / §14（追加 37）/ §15（六处状态 + 新增 O96）/ 头部 / `docs/plans/M2.md` / `docs/evidence/M2-2-hq-align.md` §8 |

**未过项（本轮实测）**：无 HQ 对照轮 **fps 14.3 < §13 判据 ≥14.99**（O96(a)）；镜像 **P90 4.18~4.20 ms > §6.1 该行 ≤3 ms**（VA 口径，O96(b)）。
**未做**：再停桌面重测（用户选定"先不动板子，先回写文档"）⇒ 见 O96(a)(b)(c) 的待办。
