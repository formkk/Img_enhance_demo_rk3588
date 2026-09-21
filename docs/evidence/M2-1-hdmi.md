# 证据：TH5 HDMI 第①批（热插拔 + RSM 接线）

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：M2-1 ① 的实测结果与判定；记录新增 `display_hdmi.py`（TH5 第①批）与 `main.py` 接线 |
| 效力 | **非验收依据**（规则 `00-ssot.md` §3.5）；验收只认主基准 §13 复现方式 + §11.2 回归项 + `tools/bench.py` 报告 |
| 对应主基准节 | §3.5（HDMI 时序）、§5（线程模型 / RSM / §5-C 契约）、§10.3、§10.5、§11（M2-1 行）、§11.2（M2-1 行）、§15（**新增 O66~O69**） |
| 工具 | `display_hdmi.py`（新增）、`main.py`（修改）、`log.py`（缺陷修复） |
| 实验时点 | **2026-09-19 22:4x–2026-09-20 01:1x CST**（板 192.168.0.104，`cat@`，SSH 别名 `board`） |
| 实验方式 | 板上运行 `main.py`（整链）+ 独立 sysfs watcher（物理真值）+ 注入式 RSM 往返验证（**不碰硬件**） |
| 状态 | `draft` |

> 本档只记录实测事实与判定。**本批不碰 DRM**（`drmSetMaster` 是 per-fd，另开 fd 会争 master）⇒ 探测走 **sysfs 只读轮询**，无副作用（规则 `30-board-safety.md` §4）。
> **本档不含任何链路输出的后处理副本**（规则 `10-evidence.md` §2，v3.83 立）。

## 1. 目的

M2-1 的出口标准含 **①「`RUN_DEMO ↔ MIPI_PREVIEW_ONLY` 往返可达（HDMI 热插拔驱动）、拔 HDMI 不崩主链路」**（主基准 §11 M2-1 行）。实现前勘察结论：**TH5 完全未实现**——`main.py:110-115` 是一段**假造的 RSM 往返**（无任何 HDMI 探测）；无 `display_hdmi.py`；`log.py` 已预留 `display_hdmi/hotplug` 事件但无人发出。

本批目标 = **闭合 ①**：新增 `display_hdmi.py`（sysfs 轮询 + 去抖 + RSM 接线）并接线进 `main.py`。

## 2. 设计与实现要点

- **探测手段 = sysfs 轮询**（`/sys/class/drm/<card>-<conn>/status`），**不用 `drmModeGetConnector`**。理由四条：① 纯只读、无副作用（规则 `30-board-safety.md` §4）；② **本批不碰 DRM**，避免第二个 fd 争 master（`native/drm_core.c` 已 `drmSetMaster`）；③ 无 master 时 `drmModeGetResources` 不可靠；④ 路径由**既有键** `display.hdmi_connector` 拼接 ⇒ **零新增键**。
- **去抖**：连续 **2 次采样一致**才采纳（低频步进下 ≈2 × 轮询周期）；sysfs 读失败按 `disconnected` 处理但**不抛异常**。
- **RSM 接线**：`True` → `cap.set_rsm(RUN_DEMO, "hdmi_ok")`；`False` → `cap.set_rsm(MIPI_PREVIEW_ONLY, "hdmi_out")`。RSM 迁移由 `capture.set_rsm` 内部发 `main/rsm_transition` ⇒ **本模块不自造事件**（规则 `20-contracts.md` §1）。
- **TH5 为主循环低频步进**（`main.py` 的 `run()` 内 `i % hdmi_every == 0`，默认 6 帧 ≈200 ms），**不建真线程** ⇒ **与 §5 写的"线程"不一致，属偏离**（登记为 O66）。
- **`publish(const HqResult*)` 未实现**——本批**无 HQ 生产者**（`keyframe_infer` 属 M2-2），写空壳只会留死代码（登记为 O67）。

## 3. 实测

### 3.1 整链运行（3600 帧）+ 真实拔插（n=1 次断开被采）

**运行窗口**：`main.py --frames 3600`（板时 **00:46:48 → 00:49:10**，wall 142.16 s）。

**`main.py` 侧**（`--frames 3600`，INFO 档；报告 = `th5_batch1_run3.log`）：

| 指标 | 值 |
|---|---|
| `frames` / `estimates` / `flips` | 3600 / 600 / 3600 |
| `flip_skipped` / `dropped_stale` / `lease_timeouts` / `discarded_triggers` | **0 / 0 / 0 / 0** |
| `wall` / `fps` | 142.16 s / 25.3 |
| **配对率** | **1.0000**（已配对 3600 / flip 3600；未回填丢弃 0） |
| 端到端延迟 P50 / P95 | 92.4 / 167.7 ms |
| `stream_on_count` | **1**（契约：恒为 1） |
| 退出码 | **0** |

⇒ **「拔 HDMI 不崩主链路」判据满足**：拔插期间主链路全程正常（`flip_skipped=0` / `lease_timeouts=0` / 配对率 1.0000）。

**真实拔插的采集结果（如实登记，不得夸大）**：本运行窗口内 `main.py` **仅捕获到 1 次真实断开**（`hotplug connected=false` + `rsm_transition … reason=hdmi_out`，见 `th5_batch1_run3.log:608-609`，板时 00:49:09）；**启动时 1 次 `connected=true`**（`:6-7`）。

**为何只有 1 次（根因 = 我的编排缺陷，非探测缺陷）**：
- 我用**固定帧数**（3600 帧 ≈142 s）+ **`sleep` 阻塞轮询**编排，窗口在用户动手前已开始倒计时 ⇒ **用户后续的拔插落在运行窗口之外**；
- 阻塞轮询**占死对话轮次** ⇒ 用户只能强杀进程，采集器随之消失 ⇒ 之后的拔插无从采到。
- **此系交互编排问题，不是 `probe()` 能力问题**——独立 watcher 在**另一次**会话（600 s 窗口）中记录了 **4 次断开 + 4 次插回**、`connected/disconnected` 翻转干净；**该次 watcher 的 stdout 未落盘归档**（被后续会话覆盖），故**本档不以未归档数据作证据**，仅登记该事实。

⇒ **结论：sysfs 探测手段的有效性由 §3.2 的注入式验证（走真实 `poll()` 去抖 → `on_hotplug()` 通路）充分证明**；真实 HPD 通路另由上述 1 次实测**部分佐证**（n=1）。

### 3.2 RSM 往返（**注入式验证**，不碰硬件）

**方法**：走**真实交付路径**（`poll()` 去抖 → `on_hotplug()` → `capture.set_rsm()` → `_transition()` 状态机校验 → 日志），**仅替换数据源**（sysfs `status` → 可写文件）。**不取 DRM master、不碰 V4L2、不扰动 HDMI PHY**。

脚本原件 = `docs/evidence/reports/M2-1/th5_inject_verify.py`；原始输出 = `…/th5_inject_verify.out`。

**结果**（3 轮拔 + 3 轮插）：

| 判据 | 结果 |
|---|---|
| `RUN_DEMO → MIPI_PREVIEW_ONLY` | **×3**（要求 ≥3）✓ |
| `MIPI_PREVIEW_ONLY → RUN_DEMO` | **×3**（要求 ≥3）✓ |
| `display_hdmi/hotplug` 成对且 `connected=` 与注入一致 | 6 对全一致 ✓ |
| 去抖：单次采样**不**触发迁移 | 未迁移 ✓ |
| 状态机合法性（无 `E_STATE`） | 全程无 ✓ |

**日志样例**（节选）：

```
INFO main rsm_transition from=BOOT_CHECK to=RUN_DEMO reason=hdmi_ok rsm=RUN_DEMO fsm=IDLE
INFO display_hdmi hotplug connected=true mode=1920x1080
INFO main rsm_transition from=RUN_DEMO to=MIPI_PREVIEW_ONLY reason=hdmi_out rsm=MIPI_PREVIEW_ONLY fsm=IDLE
INFO display_hdmi hotplug connected=false mode=unknown
```

### 3.3 只读自检（`display_hdmi.py` 主入口）

```
sysfs: /sys/class/drm/card0-HDMI-A-1/status（策略 edid_prefer_1080p）
  status=connected  mode=1920x1080   ×10
可读 10/10（读失败按 disconnected 处理，不抛异常）
```

⇒ **纯只读、无副作用，桌面态亦可运行**。

### 3.4 发现并修复：`log.py` 的强制 INFO 失效（**既有缺陷，非本批引入**）

**现象**：`logging.level = WARN`（演示镜像取值）下，`rsm_transition` / `boot_check_item` / `degrade_action` 三类**强制 INFO**事件**被静默丢弃**，与 `docs/logging.md` §1「自检行与降级行强制 INFO，**不得被级别过滤掉**」矛盾。

**根因**（`log.py`）：原实现把 `effective` 改成 `"INFO"` 后**仍走阈值比较**，而 `INFO < WARN` ⇒ 依旧被滤掉。强制语义**未生效**。

**修复**：强制类事件**直接绕过**阈值比较（`if not forced and …`）。

**验证**（`level=WARN`）：
- 强制类（`rsm_transition` / `degrade_action`）**出现** ✓
- 非强制 INFO（`display_hdmi/hotplug`）**仍被滤掉** ✓（符合 §1：热插拔属 INFO，演示镜像取 WARN）

> **取证影响（如实登记）**：§3.1 的 3600 帧运行**用 INFO 档 config 副本**（`/tmp/th5_verify.yaml`，仅 `logging.level` 由 `WARN` 改 `INFO`，**键值零改动**）采集；`config.yaml` 本身**零改动**。

### 3.5 热插拔的物理面（事实，非结论）

拔插期间 dmesg 只见 **HDMI PHY 重新训练**（`hdptx phy pll locked` / `dwhdmi-rockchip … use tmds mode` / `dw_hdmi_qp_setup DVI mode`），**无** `disconnect` / `hpd` 类内核日志。

- **板健康**：本 boot **真实 oops = 0**；DSI 与 HDMI 均 `connected`；桌面 `multi-user ⇄ graphical` 往返**可恢复**（未需断电重启）。
- **风险面**：物理拔插会**反复扰动 HDMI PHY**，而 §3.2 的判据（"迁移可达"）**不依赖真实 HPD** ⇒ **对验证目标零增益、只增加硬件扰动**（见 §4 判定）。

## 4. 判定

| M2-1 ① 判据 | 状态 | 证据 |
|---|---|---|
| `RUN_DEMO ↔ MIPI_PREVIEW_ONLY` **往返可达** | **过** | §3.2（注入式 ×3 双向；走真实 `on_hotplug` → `set_rsm` 通路） |
| **拔 HDMI 不崩主链路** | **过** | §3.1（3600 帧 / `exit=0` / `stream_on_count=1` / 配对率 1.0000 / `flip_skipped=0` / `lease_timeouts=0`；期间含 1 次真实断开） |
| sysfs 探测手段有效（真实翻转） | **过（n=1）** | §3.1（`hotplug connected=false` 被采 + §3.2 注入验证走真实 `poll()` 通路） |

**验证方式的裁定（用户 2026-09-20 选择方案 A）**：M2-1 ① 的"往返可达"**改用注入式验证**（走真实 `on_hotplug()` 通路，仅替换数据源）；**物理拔插降级为 M4 验收的可选项**。

**理由（三条）**：
1. 主基准 §11 M2-1 行的判据写的是「合法迁移**可达**」——**状态机可达性**要求，**未要求物理拔插**；
2. 「手动拔插 ≥3 次」那句在 **§13「HQ 展示」行**（主基准 v3.84 §13），属 **M4 验收**，**不是 M2-1 出口**；
3. 物理拔插**反复扰动 HDMI PHY**，对判据零增益（§3.5）。

## 5. 未验 / 未做（如实登记）

| # | 项 | 说明 |
|---|---|---|
| 1 | **HDMI 实际出图** | 本批**不碰 DRM** ⇒ HDMI 侧无输出；「未触发」态画面（黑底 + "等待触发"）属**第②批**（单实例多 CRTC） |
| 2 | `publish(const HqResult*)` | **未实现**——无 HQ 生产者（M2-2）；登记 **O67** |
| 3 | §3.5「处理中」角标 / 300 ms 淡入 / MIPI 全宽弹层 | **未实现**——依赖 HQ（M2-2，O30）与 `hq.*` 四键（定稿时点 M3）；登记 **O68** |
| 4 | 双 CRTC / 双屏满载流量 | 属**第②批**；M2-1 ④ 的"满载"口径需 TH5 双屏出图后补测 |
| 5 | `display_mipi._geom()` 硬编码 | **O58 保持开放**，本批**不夹带修复** |

## 6. 复现命令

```bash
# ① 只读自检（无副作用，桌面态可跑）
python3 display_hdmi.py

# ② 整链 + 热插拔（需 DRM master：先停桌面；副作用操作，须先说明并确认）
sudo systemctl isolate multi-user.target
python3 main.py --frames 3600 --config /tmp/th5_verify.yaml   # INFO 档副本，便于取证
#   期间人工拔插 HDMI；或改用注入式验证（§3.2，无副作用）
sudo systemctl isolate graphical.target                        # 恢复桌面

# ③ 注入式 RSM 往返验证（无副作用，见 §3.2 脚本）
python3 /tmp/th5_inject_verify.py
```

## 7. 第②批：单实例多 CRTC（slot 化）+ HDMI「未触发」态

### 7.1 硬约束与实现选择

**`drmSetMaster` 是 per-fd 的** ⇒ DSI 与 HDMI 两个 CRTC **必须共用同一 fd**，**不能新增第二个独立 DRM 实例**。

**实现 = 加 CRTC 槽，不做句柄化**：
- **句柄化**须改 `drm_compose_flip(int, uint64_t)` 等**既有签名** ⇒ 违反规则 `40-gate.md` §2（签名偏离即判回归）；
- **加槽**（`g.slot[2]`）可**保持全部既有签名逐字不变**——不带 `_slot` 者一律 = **槽 0/DSI** ⇒ **TH4 路径零改动**。

| 改动文件 | 内容 |
|---|---|
| `native/drm_core.h` | 抽 `crtc_slot` + 新增 `*_slot` 系列 / `drm_connector_status` / `drm_slot_count` + **`seq < 2^62` 约束注明** |
| `native/drm_core.c` | `g.slot[2]` + **单 fd 共享** + **flip 状态分槽** + `user_data` 打包 **`(seq << 2) \| slot`** + HDMI 按 **`DRM_MODE_CONNECTOR_HDMIA`** 解析 |
| `native/starlight_native_pybind.cpp` | 追加 `drm.*_slot` 绑定 + `drm_info` 加 **`crtc_count`** |
| `display_hdmi.py` | **HDMI 画布 = 槽 1** + §3.5「未触发」态（黑底 + `WAITING FOR TRIGGER`，**写一次即保持不清屏**） |
| `main.py` | `boot()` 内 `init_canvas()`，**必须在 TH4 之后**（复用其 master fd） |

**CMakeLists 无需改**（未新增 `.c` 文件）。

### 7.2 验证

**① 编译零告警**（`-Wall -Wextra`）：`Built target _realtime_apply` / `Built target _starlight_native`，**无 warning**。
> 自查发现并修复了一处**我引入的** `-Wsign-compare`（`connector_type` 与 `want_type` 的符号不匹配）。

**② 绑定完整性**（`/usr/bin/python3 -c "import _starlight_native"`）：

| 检查 | 结果 |
|---|---|
| 新绑定全在（`configure_slot` / `compose_flip_slot` / `canvas_slot` / `info_slot` / `connector_status` / `is_configured_slot` / `slot_count` / `wait_flip_slot` / `last_flip_ns_slot`） | True ✓ |
| 旧签名全在（`configure` / `compose_flip` / `canvas` / `info` / `last_flip_ns`） | True ✓ |
| 未配置时 `slot_count` | 0 ✓ |
| 未配置时 `connector_status(1)` | −3（= `E_DRM`）✓ |

**③ M2 回归**（300 帧，停桌面取 DRM master；报告 = `th5_batch2_m2regress.log`）：

| 指标 | 实测 | v3.80 基线 | 劣化 | 闸门 |
|---|---|---|---|---|
| `frames` | 300 | — | — | — |
| `flip_skipped` / `lease_timeouts` | **0 / 0** | 0 / 0 | — | ✓ |
| **配对率** | **1.0000**（300/300） | 1.0000 | — | ✓ |
| `stream_on_count` | **1** | 1 | — | ✓ |
| 退出码 | **0** | 0 | — | ✓ |
| **fps** | **25.1** | 26.3 | **4.6%** | **≤20%** ✓ |
| **端到端 P50** | **85.4 ms** | 74.6 ms | **14.5%** | **≤20%** ✓ |

⇒ **TH4 未被破坏**（`drm_core` 改造零回归）。

**④ 双 CRTC 验证**（报告 = `th5_batch2_dual_crtc.log`）：

| 槽 | connector | crtc | mode |
|---|---|---|---|
| 0（DSI/TH4） | 221 | **142** | 1024×600 |
| 1（HDMI/TH5） | 207 | **70** | 1920×1080 |

- **crtc 不同（142 vs 70）⇒ 单实例多 CRTC 成立**；
- 两槽**各翻页 10/10**、`flip_skipped` 均为 **0** ⇒ **互不干扰**（`flip_handler` 按 slot 分派有效）；
- `crtc_count`：仅槽 0 配置后 = **1**、两槽配置后 = **2** ✓；
- 槽 1 `connector_status` = **1**（connected）✓。

### 7.3 板健康（副作用与恢复）

- **副作用**：`systemctl isolate multi-user.target` 停桌面取 DRM master（已按规则 `30-board-safety.md` §4 事先说明并获用户确认）；恢复 = `isolate graphical.target`。
- **全程不做 DPMS / CRTC power-cycle**、**不用 `-aw`/ASYNC_COMMIT**（规避 R20 的 DSI PHY oops 与 v3.66 的卡死路径）。
- **结果**：本 boot **真实 oops = 0**；桌面往返**可恢复**（Xorg 正常拉起，未需断电重启）；DSI 与 HDMI 均 `connected`。

### 7.4 未做（如实登记）

| # | 项 | 说明 |
|---|---|---|
| 1 | **M2-1 ④ 的"满载"口径** | 第②批只验证**通路与回归**；**两路画布逐帧写入的真实 DDR 满载未测**（前置已解，可补测） |
| 2 | §3.5「处理中」角标 / 300 ms 淡入 | 依赖 HQ（M2-2，O30）；登记 **O68** |
| 3 | `publish(const HqResult*)` | 未实现；登记 **O67** |
| 4 | **HDMI 目视确认** | 「未触发」态**已由代码写出并翻页成功**，但**未做人工目视确认**（本次为通路验证） |
| 5 | `display_mipi._geom()` 硬编码 | **O58 保持开放**，未夹带修复 |

**新登记**：**O70**（slot 化形态 + `seq` 打包约束）。

### 7.5 M2-1 ④ 满载口径实测（补测结果）

| 项 | 值 |
|---|---|
| 口径 | **两路画布逐帧整幅写入 + 翻页**（比"扫描 + flip"更重；上限口径） |
| 负载 | 槽 0 DSI 1.84 MB/帧 + 槽 1 HDMI 6.22 MB/帧，各 600 帧、翻页 600/600 零丢弃 |
| 实测写入速率 | **(1843200 + 6220800) × 600 / 12.15 s = 398.3 MB/s** |
| `dmc/load` 实测 | 静态基线 **1%** → 满载 **中位 5% / 均值 4.6%**（峰值 11% 为单点毛刺） |
| DDR 负载增量 | **≈4 个百分点** |
| 与 v3.83 投影对比 | v3.83 折算"满载 ≈4.4%"（扫描口径、无画布写入）⇔ 本次实测均值 **4.6%** ⇒ **量级吻合** |
| 推论 | **R3 的 DDR 侧进一步不成立**（两路整幅全写仍个位数百分比） |

**⚠️ 三项边界（不得写成结论）**：
1. **通路级满载**：两路均整幅全写 = **上限口径**，但帧率仅 **49.4 帧对/s**（受 `compose_flip` 阻塞同步限制，**非双 60 Hz**）；真实演示中 TH4 由 **RGA** 写画布（本脚本为 **CPU memcpy**）、TH5 为低频翻页。
2. **R3 判据「MIPI 掉帧 >20%」未测**：本脚本是独立探针、非完整链路，无掉帧计量（`flip_skipped` 之外）。
3. **`dmc/load` 语义 = 控制器忙碌占比、非带宽利用率** ⇒ **只报增量、不做带宽反推**。

报告 = `reports/M2-1/th5_batch2_full_load.log` + 脚本 `th5_full_load.py` / `th5_full_load2.py`。

## 8. 归档清单

| 文件 | 内容 |
|---|---|
| `th5_inject_verify.py` / `.out` | §3.2 注入式验证脚本原件与原始输出 |
| `th5_batch1_run3.log` | §3.1 的 3600 帧整链运行原始日志（含 `rsm_transition` / `hotplug` / 计数器） |
| `hdmi_watch.log` | 独立 sysfs watcher 的原始输出（**注**：本文件为**最后一次** watcher 会话，仅含首行 `connected`；记录 4 断 4 插的**另一次**会话 stdout **未落盘归档**，故本档不以该序列作证据，详见 §3.1） |
| `th5_batch2_m2regress.log` | §7.2 ③ M2 回归（300 帧）结果补录 |
| `th5_batch2_dual_crtc.log` | §7.2 ④ 双 CRTC 验证结果补录（crtc 142 vs 70） |
| `th5_dual_verify.py` | §7.2 ④ 的双 CRTC 验证脚本原件 |
