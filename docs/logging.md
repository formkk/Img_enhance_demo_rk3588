# 附件规范：日志规格（裁定 O14）

| 项 | 内容 |
|---|---|
| 地位 | 本文档是《RK3588 极暗光增强实时演示系统 —— 项目基准文档》的**从属规范**（v3.12 裁定 O14 拆出），承载主文档 §10.5 的日志规格 |
| 效力 | 与主基准冲突时**以主基准为准**；本文档变更须同步主基准 §14 变更记录 |
| 主基准对应节 | §10.5（启动自检与故障降级矩阵）、§5 契约 A（返回码）、§6.5（计量报告字段） |
| 状态 | 标注 **`[待复核]`** 的取值须在 M2 定稿并回写主文档 §10.4 的 `logging.*` 键；**v3.86 裁定 O102**：§4 删 `keyframe_infer/save_set` 与 `keyframe_infer/save_fail` 两事件（「存图」功能整体取消） |

## 1. 级别

| 级别 | 用途 |
|---|---|
| ERROR | 强制项失败、进入 `FAULT_EXIT`、无法降级且返回 `E_*` 的故障 |
| WARN | 可降级项失败，以及每一次降级动作（含 RSM 迁移到 `MIPI_PREVIEW_ONLY`） |
| INFO | 默认级别：启动自检逐项、状态迁移、触发与丢弃计数、热插拔、时间戳异常 |
| DEBUG | 仅 bench / 调试构建；逐帧事件，演示构建禁用 |

- 演示镜像取 WARN 为准，但**自检行与降级行强制 INFO**，不得被级别过滤掉。
  **（v3.86 裁定 O117）强制 INFO 的完整名单**（= `log.py` 的 `FORCED_INFO_EVENTS`）：`boot_check_item` / `degrade_action` / `rsm_transition` / **`service_exit`** / **`reset_key`**。后两条是**退出取证事件**：主文档 §13「部署与自启动」行与 §11.2 M4 ⑤ 的判据要求它们**成对出现**，而在 WARN 档下它们既非自检行也非降级行 ⇒ 会被过滤（2026-09-22 实测：`journalctl -u starlight-demo` 与 `logging.dir` 里**均搜不到**）⇒ 裁定一并列入强制 INFO（**不新增事件、不新增字段**）。
- 级别由 config `logging.level` 控制（主文档 §10.4）。

## 2. 路径与轮转

| 项 | 取值 |
|---|---|
| 目录 | config `logging.dir`；**（v3.86 裁定 O102）** 原「默认 `<storage.dir>/logs`」的回退**已作废**（`storage.*` 六键随存图取消）⇒ 该键缺失时**仅 stdout** |
| 文件名 | `starlight_<utc_date>.log` `[待复核]` |
| 轮转 | 单文件 ≥ `logging.rotate_mb`（基线 8 MiB `[待复核]`）切分；保留 `logging.keep_files`（基线 3 份 `[待复核]`），超限删最旧 |
| 旁路 | 同时输出 stdout；systemd 下由 journal 收集（单元口径见主文档 §10.6-A，v3.14 裁定 O6），`journalctl -u starlight-demo` 为现场首查入口 |
| 写失败 | 日志目录不可写**不得阻塞主链路**：记一次 `E_IO` 后降级为仅 stdout，不再重试写文件 |

**纪律（v3.86 裁定 O102 后）**：日志目录 = `logging.dir`，与任何**影像落盘**无耦合；原「与 §8.3 的存图目录分离 / 日志不受存图环形淘汰影响」的**对象已随存图取消而消失**（`logging.dir` 自身的轮转纪律见上表）。

## 3. 字段化行格式

每行一条事件，字段以空格分隔、`k=v`：

```
<utc_iso8601> <mono_ns> <level> <module> <event> [k=v ...]
```

| 字段 | 说明 |
|---|---|
| `utc_iso8601` | `CLOCK_REALTIME`，人读用（与 §8.3 双时钟入档口径一致） |
| `mono_ns` | `CLOCK_MONOTONIC` 纳秒，**唯一可用于时序比对的时钟**（主文档 §6.5） |
| `level` | §1 的四个级别之一 |
| `module` | 取 §10.3 的模块名：`capture` / `curve_estim` / `realtime_apply` / `keyframe_infer` / `display_mipi` / `display_hdmi` / `isp_ctl` / `main` |
| `event` | 事件名（§4 清单），小写 + 下划线，**不得自造未登记事件** |
| `k=v` | 事件字段；带帧的事件必须带 `seq_src` 或 `seq_est`，带状态的事件必须带 `rsm=` 与 `fsm=` |

## 4. 事件清单与必带字段

| module | event | 必带字段 |
|---|---|---|
| main | `boot_check_item` | `item=<§10.5 编号>` · `result=ok / fail / degraded` · `code=<错误码符号>` |
| main | `rsm_transition` | `from=` · `to=` · `reason=` |
| main | `service_start` / `service_exit` | `exit_code=` · `uptime_s=`（起止均带 `mono_ns`） |
| main | `restart_attempt` | `attempt=` · `window_s=60` · `exit_code=`（主文档 §10.6-A 崩溃记录） |
| main | `start_limit_hit` | `burst=5` · `interval_s=60` · `action=failed`（超限停；此后不再自动拉起） |
| main | `version_triplet` | `toolkit2=` · `librknnrt=` · `rknpu_driver=` |
| main | `isp_fingerprint` | `iq_hash=` · `gamma=` · `ccm=` · `black_level=` · `csc_matrix=` · `rgb_range=` · `awb_lock=` |
| capture | `stream_on_once` | `result=` · `video_node=` |
| capture | `sensor_lock` | `result=` · `node=` · `vblank_lines=` · `readback=`（**v3.79 新增**：IMX415 1080p@30 锁定施加结果，主基准 §9.5 ⑧） |
| capture | `lease_timeout` | `lease_id=` · `seq=` · `buffer_index=` · `generation=` |
| capture | `timestamp_source_changed` | `source=` · `seq=` · `window_invalidated=true`（口径：不停机、该窗口指标作废，主文档 §6.5） |
| capture | `trigger_accept` / `trigger_discard` | `source=`（**v3.85 裁定 O84：唯一源 = 指针点击**） · `seq_src=` · `discarded_triggers=` |
| capture | `pointer_device` | `kind=touch / mouse` · `node=` · `present=`（**v3.85 裁定 O81 新增**：指针输入设备接入情况；触控 + 鼠标） |
| curve_estim | `curve_publish` | `seq_est=` · `f_est=` · `alpha=` · `curve_level=` · `precision=` |
| keyframe_infer | `hq_start` / `hq_done` | `seq_src=` · `model_id=` · `active_shape=` · `done_ns=` |
| ~~keyframe_infer~~ | ~~`save_set`~~ / ~~`save_fail`~~ | **（v3.86 裁定 O102）随「存图」功能整体取消而移除**（原字段 `items=` / `bytes=` / `dir=` / `code=E_IO` / `reason=`） |
| display_hdmi | `hotplug` | `connected=` · `mode=`（EDID 选定结果）。**（v3.85 裁定 O80）只记日志、不再驱动 RSM 迁移** |
| display_mipi | `freeze_enter` / `freeze_exit` | `seq=` |
| display_mipi | `fullscreen_enter` / `fullscreen_exit` | `seq=` · `reason=click / timeout`（**v3.85 裁定 O79 新增**：全屏 HQ 视图进出；`display_hdmi/fade_start` / `fade_done` 两事件**随 O82 取消**） |
| display_mipi | `reset_key` | `press=short / long` · `seq=`（短按软复位 → 触发 `rsm_transition`；长按 → `service_exit`，主文档 §10.6-C）。**（v3.86 裁定 O114）** 长按之后由 `main()` 执行 `systemctl isolate graphical.target` **恢复桌面**——该动作**不新增事件、不新增字段**（成功静默、失败仅 stdout 提示，见主文档 §10.6-C） |
| 任意 | `degrade_action` | `trigger=` · `action=`（动作名同主文档 §10.5 矩阵"降级动作"列） |

## 5. 错误码

- 日志中的错误码**只允许取自主基准 §5 契约 A 节**：`0 = OK`、`-1 E_V4L2`、`-2 E_NPU`、`-3 E_DRM`、`-4 E_IO`、`-5 E_STATE`、`-6 E_TIMEOUT`。
- 打印**符号名**（如 `E_V4L2`），必要时附数值；**不得自造新码**，缺失类别须先走主基准变更再使用。
- 主文档 §15 的 O24（异常恢复补项，5 类故障）实测后填动作时，须**同时在此表登记其 `event` 与 `code`**。

## 6. 与计量报告的关系

- **日志不承载指标**：FPS / 延迟 / 配对率 / 丢弃帧数一律由 `bench.py` 报告给出（字段清单见主基准 §6.5），避免同一事实两处 SSOT。
- 日志只做事件留痕与异常定位；两者通过 `seq_src` / `mono_ns` 关联，可交叉核对但**不得互相替代**。
