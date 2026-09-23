# 证据：异常恢复前 2 类的落地与确定性注入实测（O24 §4）

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：一次**确定性注入**实验的实测结果；记录 O24 §4 前 2 类（CSI/V4L2 掉流与接收错误、传感器 I2C/DMA 错误）的落地物与判据/处置验证 |
| 效力 | 本档**本身不是验收依据**（规则 `00-ssot.md` §3.5）；其结论值经 §15 O24 就地裁定（v3.86 追加 70）后回填 `docs/measurements.md` §4 两行三列 |
| 对应主基准节 | §10.5（故障降级矩阵新增两行）、§6.5、§5（TH1 生命周期）、§15（**O24** / O13 / O26 / O80）、`docs/measurements.md` §4、`docs/logging.md` §4/§5 |
| 工具 | `native/v4l2_core.c` 的 `v4l2_inject_error(n, mode)` + `v4l2_error_count()`；`capture.py` 的 `inject_v4l2_error()`；`main.py` 的 `--inject-v4l2-error N --inject-v4l2-at M --inject-v4l2-mode err\|noframe` |
| 实验时点 | **2026-09-22 23:50–23:57 CST**（板 `lubancat` / `192.168.0.104`，SSH 别名 `board`） |
| 状态 | `draft` |

## 1. 裁定值（§15 O24 前 2 类；四问）

| 问 | 裁定 | 理由 / 依据 |
|---|---|---|
| **判定方式** | **连续无成功取帧 ≥1 s**（`mono_ns`）**或** `capture.counters().v4l2_errors` 自上次成功取帧起**连续 ≥15 次** | 只统计 `RUN_DEMO` 且**非冻结 / 非全屏期**——HQ 冻结期本就不取帧，不排除会误判 |
| **是否重试** | **不重试**；阈值内**容忍**（零星单次硬错误只丢本帧并计数，不记日志、不迁移） | O26 禁止停流重开、`stream_on_once` 恒一次 ⇒ 进程内**无自愈手段**（§10.5-6 原文同此） |
| **是否迁移 RSM** | `RUN_DEMO → FAULT_EXIT`；**不迁 `MIPI_PREVIEW_ONLY`**（v3.85 裁定 O80 已作废该态）、**不走软复位**（只清 lease，对已停流无效） | 迁移目标由 O80 后的四态空间决定 |
| **是否停机** | **停机**：退出码 ≠ 0 ⇒ systemd `Restart=on-failure` 限次拉起（burst=5 / 60 s，超限 `failed`，§10.6-A） | 与"无自愈手段"一致；限次策略是既有单元配置 |
| **两类的关系** | **处置无差异**；**唯一差异 = `reason` 归因标签**（`v4l2_stream_lost` / `sensor_i2c_dma`） | 用户空间经 V4L2 API 无法区分两者（都表现为"无新帧"或"硬错误"） |
| **事件与码** | **零新增**：`degrade_action`（`trigger=` / `action=fault_exit`）+ `rsm_transition` + `boot_check_item item=TH1 result=fail code=E_V4L2` + `service_exit exit_code=` | 全部为 `docs/logging.md` §4 既有事件；码取 §5 契约 A 既有 `E_V4L2` |

## 2. 落地物清单

| # | 落地物 | 说明 |
|---|---|---|
| 1 | `native/v4l2_core.{c,h}` | 新增 `uint64_t v4l2_errors`（计数）+ `v4l2_error_count()`（只读访问器）+ `v4l2_inject_error(n, mode)`（**验证用钩子**）；DQBUF 非 `EAGAIN` 错误分支计数 |
| 2 | `native/starlight_native_pybind.cpp` | `capture.counters()` 增 `v4l2_errors`；`capture.inject_error(n, mode=0)` |
| 3 | `capture.py` | 补 `E_V4L2 = -1`（§5 契约 A 的符号名，供"码随日志"用）；`inject_v4l2_error(n, mode=0)` |
| 4 | `main.py` | 越阈判据 + 退避 + `_fault_exit()` + `_dmesg_reason()`；`--inject-v4l2-error/-at/-mode`（**均非 config 键**）；退出码 = `FAULT_EXIT_CODE`(1)；报告新增 `v4l2_errors` / `fault` |

**正常帧路径零新增调用（可核对）**：越阈判据只在**取帧失败分支**里读 `counters()`；取帧成功时**仅在"上次成功取帧之后出现过失败"时才再读一次**（`_v4l2_err_pending` 闸门）⇒ 正常路径既不新增调用、也不新增日志 ⇒ **O19 的空链路基线等既有报告仍可比**（并已按 §3 附注补跑 1 轮交叉核对）。

**构建**：板上增量重建（`cmake --build build -j4`）——`v4l2_core.c` + `starlight_native_pybind.cpp` 重编 + 链接，**零告警**（规则 `40-gate.md` §2）。重建前已留 `_starlight_native…so.bak.o24`。

## 3. 两条判据分支的实测（确定性注入）

前置：`sudo systemctl stop starlight-demo`（TH4 需 DRM master）⇒ 注入进程 ⇒ `systemctl start starlight-demo` 恢复。

| 用例 | 命令要点 | 期望 | 实测 |
|---|---|---|---|
| **① 连续硬错误分支** | `--frames 200 --inject-v4l2-error 20 --inject-v4l2-at 30`（mode = `err`） | 连续 15 次硬错误即越阈 | 退出码 **1**；`连续硬错误=15`；`counters.v4l2_errors=15` / `lease_timeouts=0`；`frames=30`（注入点即停）；报告 `fault={'reason':'v4l2_stream_lost','exit_code':1}` |
| **② 连续无帧分支** | `--frames 400 --inject-v4l2-error 400 --inject-v4l2-mode noframe --inject-v4l2-at 30` | 连续无帧 ≥1 s 即越阈 | 退出码 **1**；`连续硬错误=0`（该模式不计 `v4l2_errors`）；`lease_timeouts=186`（≈186×5.4 ms ≈ 1.0 s ⇒ **退避生效、无忙等**）；报告 `fault={'reason':'v4l2_stream_lost','exit_code':1}` |

**处置链（两用例一致，日志原文见 `docs/evidence/reports/O24/*.log`）**：

```
INFO main boot_check_item item=TH1 result=fail code=E_V4L2          ← 越阈点（先记"失败 + 码"）
INFO main degrade_action trigger=v4l2_stream_lost action=fault_exit rsm=RUN_DEMO fsm=IDLE
INFO main rsm_transition from=RUN_DEMO to=FAULT_EXIT reason=v4l2_stream_lost rsm=FAULT_EXIT fsm=IDLE
INFO main service_exit exit_code=1 uptime_s=3
```

| 检查项 | 结论 |
|---|---|
| 判据两条分支均可触发 | ✅（① 硬错误 15 次；② 无帧 ≈1 s） |
| 迁移合法且**只迁一次** | ✅ `RUN_DEMO → FAULT_EXIT`（`RSM_LEGAL` 内）；无 `MIPI_PREVIEW_ONLY`、无软复位 |
| 码与事件 | ✅ 全部为既有事件；码 = `E_V4L2`（符号名，非数值） |
| **退出码 ≠ 0** | ✅ = 1（⇒ systemd `Restart=on-failure` 的必要条件；单元文件为既有配置，未改） |
| 退避（防忙等） | ✅ ② 用例 1 s 内 186 次失败 ⇒ 平均 ~5.4 ms/次（= `ACQUIRE_FAIL_BACKOFF_S`） |
| 阈值内容忍（不误停机） | ✅ 正常轮 900 帧的 `v4l2_errors` / `lease_timeouts` 均为 0（本轮 O19 报告），未见误停机 |
| 归因两分支 | ✅ 同一 `_dmesg_reason()` 在三组 `since_mono` 下分别返回 `sensor_i2c_dma`（无时间下界时命中尾部 `imx415 … error` 行）/ `v4l2_stream_lost`（加了时间下界后不再命中） |

**归因启发式的收紧（本轮修正）**：板上 `dmesg` 尾部 64 KiB 内 `imx415` 有 **350 行**，其中绝大多数是 `sensor_lock` 的**例行**行（`set vblank` / `set exposure` / `set analog gain`）⇒ 仅按设备标识匹配会**恒判**第 2 类。故判据收紧为**三重条件**：同一行**同时**命中「设备标识（`imx415`/`i2c`/`rkisp`）」与「错误关键词」**且**该行 `[<uptime_s>]` **≥ 最后一次成功取帧时刻**（`dmesg` 行首 uptime 与 `CLOCK_MONOTONIC` 同域）。

### 3.3 真链路注入 + systemd 自启限次（v3.86 追加 75；用户在场时执行）

**注入方式**：`media-ctl -d /dev/media0 -l '"m00_b_imx415 1-001a":0->"rockchip-csi2-dphy0":0[0]'`（断开**传感器链路**）。**须先 `systemctl stop starlight-demo`**——取流中改链路会报 `Unable to parse link: Device or resource busy (16)`（本轮首跑即遇上，如实登记）。

| 观察项 | 实测 |
|---|---|
| 判据分支 | **「连续无帧 ≥1 s」**（`连续硬错误=0`）——真实故障表现为**停帧**而非硬错误 |
| 越阈时延 | 每周期 ~1.5 s（`RUN_DEMO` 后 1.42–1.51 s 内落 `boot_check_item`） |
| 处置链 | `boot_check_item item=TH1 result=fail code=E_V4L2` → `degrade_action trigger=v4l2_stream_lost action=fault_exit` → `rsm_transition RUN_DEMO → FAULT_EXIT` → `service_exit exit_code=1` → systemd `Failed with result 'exit-code'` |
| 归因 | `v4l2_stream_lost`（回落分支正确——时间下界内无「设备标识+错误关键词」同行） |
| **systemd 自启限次** | `NRestarts = 5`（`StartLimitBurst=5 / 60 s` 用尽）后 `Start request repeated too quickly` ⇒ **`ActiveState=failed` 且不再自动拉起** |
| 恢复 | 重使能链路 → `systemctl reset-failed` → `start` ⇒ `active` / `RUN_DEMO`（**已验证**） |

原始取证：`docs/evidence/reports/O24/o24_realinject_journal.txt`（journal 摘录）与 `o24_realinject_state.txt`（注入前后单元状态 + 链路状态回读）。

## 4. 未验残留

| # | 残留 | 处置 |
|---|---|---|
| 1 | ~~真链路注入未做~~ ⇒ **（v3.86 追加 75）已完成**：`media-ctl` 断传感器链路，判据由**连续无帧分支**在真故障下触发（详见 §3.3） | 已闭合 |
| 2 | ~~systemd 侧的自启限次未实测~~ ⇒ **（v3.86 追加 75）已完成**：5 次重启后 `Start request repeated too quickly` ⇒ `failed`（§3.3） | 已闭合 |
| 3 | `_dmesg_reason()` 的 `dmesg` 调用耗时（实测 ≈0.13–2 s，受超时上界约束） | 只发生在**停机路径**（此时链路已静止）；已在代码注释与本档登记，不做优化 |
| 3b | **`sensor_i2c_dma` 归因分支未由真实 I2C/DMA 故障命中**（本轮真故障走 `v4l2_stream_lost` 回落；该分支只在"无时间下界"的人工调用下验证过） | 不阻塞；如后续真出现 I2C/DMA 错误，按 §1 的三重条件复核 |
| 4 | 第 2 类（I2C/DMA）在**真故障**下能否被 `dmesg` 命中 | 同 3b |

---

**报告与日志**：`docs/evidence/reports/O24/o24_err.{json,log}`（硬错误分支）、`o24_noframe.{json,log}`（无帧分支）。两者**落在工程目录内**（规则 `10-evidence.md` §4）。