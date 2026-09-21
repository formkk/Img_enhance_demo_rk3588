# TH5（HDMI 输出）实现方案

## Context（为什么做）

M2-1 的出口标准四项中，**② 双屏通路**与 **③ `display.hdmi_mode_selection` 定稿**已过，**④ 双屏满载流量**已部分完成（DDR 实测 + 折算投影）；**仅剩 ①「`RUN_DEMO ↔ MIPI_PREVIEW_ONLY` 往返可达（HDMI 热插拔驱动）、拔 HDMI 不崩主链路」**。而 **④ 的"满载"口径也依赖 ①**（需要 TH5 双屏出图）。

现状勘察结论：**TH5 完全未实现**——`main.py:110-115` 是一段**假造的 RSM 往返**（无任何 HDMI 探测）；`native/drm_core.c` 只选 DSI connector（`:91`）、单 `crtc_id`、单一 file-static `struct g`（`:63`）；无 `display_hdmi.py`；`log.py:69-71` 已预留 `display_hdmi` 的 `hotplug`/`fade_start`/`fade_done` 三个事件但无人发出；`§10.3` 已登记模块名 `display_hdmi.py`。

**硬约束（已核实）**：`drm_core.c:141` 已 `drmSetMaster(g.fd)`，而 **DRM master 是 per-fd 的** ⇒ **DSI 与 HDMI 两个 CRTC 必须共用同一个 master fd**，**不能新增第二个独立 DRM 实例**。这直接决定第②批必须走"同一 DRM 内核内管理多 CRTC"。

**用户已裁定**：① 分两批落地；② TH5 用**主循环低频步进**而非真线程（§5 写的是"线程" ⇒ **此为偏离，须登记**）。

**预期结果**：闭合 M2-1 ①；为 ④ 的"满载"口径铺路；不触碰已验证的 TH4 路径（第①批零回归风险）。

---

## 第①批：热插拔 + RSM 接线（**不碰 DRM**）

### 新增 `display_hdmi.py`（§10.3 已登记的模块名）

| 方法 | 职责 |
|---|---|
| `__init__(cfg, logger, capture, card="/dev/dri/card0")` | 读 `display.hdmi_connector`（M0 定稿）拼出 sysfs 路径 `/sys/class/drm/<card>-<conn>/status`；持有 `capture` 以触发 RSM |
| `probe() -> Optional[bool]` | 读 sysfs `status`，`connected`→`True`、`disconnected`→`False`、读失败→`None`（**不抛异常**） |
| `poll() -> int` | **低频步进**入口：连续 **2 次采样一致**才采纳（去抖）；状态**变化**时才调 `on_hotplug` |
| `on_hotplug(connected: bool) -> int` | §5-C 签名。`True` → `cap.set_rsm(RSM_RUN_DEMO, "hdmi_ok")`；`False` → `cap.set_rsm(RSM_MIPI_PREVIEW_ONLY, "hdmi_out")`。发 `display_hdmi/hotplug`（字段 `connected`、`mode`）。返回 `0` / `E_DRM` |
| `init()` / `shutdown()` | `init` 探测一次并按结果接线；`shutdown` 幂等清理（第①批无 DRM 资源，仅置位） |

**探测手段 = sysfs 轮询**（不用 `drmModeGetConnector`）。理由：① 纯只读、无副作用（规则 `30-board-safety.md` §4）；② 第①批**不碰 DRM**，避免第二个 fd 争 master（`drm_core.c:141`）；③ 无 master 时 `drmModeGetResources` 不可靠；④ 路径由**既有键** `display.hdmi_connector` 拼接，**零新增键**。

### 修改 `main.py`

- **删除** `:110-115` 的三行假往返，替换为 `self.hdmi = display_hdmi.DisplayHdmi(...)` + `self.hdmi.init()`（`init` 内含首次探测 + `set_rsm`）。
- `run()`（`:119-190`）循环内按**低频**插入 `self.hdmi.poll()`（如 `if i % self.hdmi_every == 0`，约 200 ms 一次）——与既有 `est_every` 同风格。
- `shutdown()`（`:203`）追加 `self.hdmi.shutdown()`。

### 日志

**只发已预留事件**：`display_hdmi/hotplug`（`log.py:69`）。RSM 迁移由 `capture.set_rsm` 内部发 `main/rsm_transition`（`capture.py:189`）。**不自造事件**（规则 `20-contracts.md` §1）。

### 本批**不做**（并登记，见下）

- **`publish(const HqResult*)` 不实现**——第①②批均**无 HQ 生产者**（`keyframe_infer` 不存在），写空壳只会留死代码；**登记为"§5-C 接口未实现（随 HQ 淡入推迟）"**。
- **"HDMI 未接 → MIPI 全宽弹层预览 2.5 s" 不做**——它依赖 HQ 结果与 `hq.mipi_preview_ms`/`hq.mipi_preview_rect`（**定稿时点 = M3**）。本批只做 **RSM 迁移**这一半。

---

## 第②批：单实例 · 单 master fd · 多 CRTC

### `native/drm_core.c` 改造 = **加 CRTC 槽**（不做句柄化）

**理由**：句柄化须改 `drm_compose_flip(int, uint64_t)` 等既有签名 ⇒ 违反规则 `40-gate.md` §2（签名偏离即判回归）。加槽可**保持全部既有签名逐字不变**。

1. **抽结构**：从 `struct g`（`:63`）抽出 `struct crtc_slot { conn_id; crtc_id; mode; fb[DRM_CORE_MAX_BUF]; nbuf; cur; flip_pending; canvas_fd[]; canvas_va[]; last_submit_ns; last_flip_ns; last_flip_seq; flip_skipped; flip_count; }`；`g.slot[2]`，**`g.fd` 仍只开一次、单 master fd 共享**（`:139-141` 不动）。
2. **slot0 = DSI**：既有全部函数（`drm_configure` / `drm_compose_flip` / `drm_last_flip_*` / `drm_canvas_va` …）**内部一律映射到 slot0**，**签名与语义逐字不变**。
3. **flip 状态分槽**：`g_flip_done[2]` / `g_last_flip_ns[2]` / `g_last_flip_seq[2]`；`drm_last_flip_ns()` 等仍返 **slot0**。
4. **事件泵分派**：`user_data` 打包 **`(seq << 2) | slot`**（⇒ 约束：`seq < 2^62`，TH1 单调分配、实际远小于此，**须在 `drm_core.h` 注明**）；`flip_handler`（`:55`）解包写入对应槽；事件泵退出条件改 `g_flip_done[slot]`（`:192-203`、`:210-218`）。
5. **新增函数（不改旧签名）**：`drm_configure_slot(int slot, const drm_cfg_t*)`、`drm_compose_flip_slot(int slot, int buf_idx, uint64_t seq)`、`drm_canvas_va_slot` / `drm_canvas_fd_slot`、`drm_info_slot(int)`、`drm_connector_status(int)`。HDMI 的 connector 解析需**按类型 `DRM_MODE_CONNECTOR_HDMIA`**（现有 `:91` 只匹配 DSI）。
6. **pybind**（`starlight_native_pybind.cpp:263-290`）：追加同名 `drm.*_slot` 绑定；`drm_info()`（`:119`）加 `crtc_count`。
7. **`CMakeLists.txt`**：若新增 `.c` 文件，须加入 `:89-95` 源清单与 `:122` 的 `-ffp-contract=off` 清单。

### HDMI「未触发」态（§3.5 第一态）

黑底 + "等待触发"，**复用既有原语**：`osd_core.h:23` 的 `osd_fill_rect` + `:33` 的 `osd_blit_glyph`（pybind 已绑定，`:294-311`），字形由 `osd.py` 侧提供。**写一次即"保持不清屏"**（§3.5 语义），无需每帧重绘。

### 几何取值：**不制造第二处来源**

`display_hdmi.py` 取几何时**统一走 `curve_estim.resolve_geometry()` 单一入口**（HDMI 只用 `display.hdmi_canvas` 与 RGA 目标尺寸）。
**不修改** `display_mipi._geom()`（`display_mipi.py:134`）——它的问题已登记为 **O58（开放项）**，按 `00-ssot.md` §3.2 **须先裁定再改**，不在本方案内夹带。

### 禁用路径（安全）

全程**无桌面态**运行；**不做 DPMS / 关 CRTC**（R20 的 oops 触发路径）；**禁用 `-aw` / ASYNC_COMMIT**（DSI 原子提交卡死史，见 v3.66）；插拔只改 RSM，**不动 DSI 的提交路径**。

---

## 必须登记的偏离 / 待裁定项（**不得在子文档内自行裁定**）

| # | 项 | 回写位置 |
|---|---|---|
| 1 | **TH5 用低频步进、非线程**（§5 写"线程"） | **§15 新增 O 项** + §10.3 注记 + §14 |
| 2 | **§5-C 的 `publish` 未实现**（随 HQ 淡入推迟） | **§15 新增 O 项** + §14 |
| 3 | **§3.5「处理中」角标 / 300 ms 淡入 / MIPI 弹层推迟**（依赖 HQ〔M2-2，O30〕与 `hq.*` 四键〔M3〕） | **§15 新增 O 项** + §3.5 注记 + §10.4（四键定稿时点**不变**）+ §14 |
| 4 | **§10.5 矩阵「HDMI 未接/拔出」行未给 `code`** | **§15** + §10.5 |
| 5 | **`drm_core` 单实例多 CRTC（slot 化）** | §10.3 native 树 + §5-C 注记 + §14 |
| 6 | **`user_data` 打包 `(seq<<2)\|slot` 的 `seq < 2^62` 约束** | `native/drm_core.h` 注释 + §14 |

**O58 保持开放**，本方案不夹带修复。

---

## 验证

### 第①批（端到端，需物理拔插）
```bash
sudo systemctl isolate multi-user.target      # 取 DRM master（副作用，需先说明并确认）
python3 main.py --frames 300                  # 期间人工拔 HDMI ≥3 次、插回 ≥3 次
```
判据：退出码 0；`stream_on_count = 1`；配对率 **1.0000**；日志中 `rsm_transition` 的 **`RUN_DEMO → MIPI_PREVIEW_ONLY` 与反向各 ≥3 次**（闭合 §11.2 M2-1 ① 的往返判据）；`display_hdmi/hotplug` 成对且 `connected=` 与物理动作一致；**DSI 侧 `flip_skipped` / `lease_timeouts` 无劣化**（"拔 HDMI 不崩主链路"）。

### 第②批
1. `main.py --frames 300` 同屏 MIPI + HDMI（HDMI 显示"未触发"态）。
2. **必跑 M2 回归**（证明未破 TH4）：`frames=300` / `stream_on_count=1` / 配对率 1.0000 / `fps` 与端到端 P50 相对 **v3.80 基线（26.3 fps、74.6 ms）劣化 ≤20%**。
3. 证据原样归档，**禁止任何后处理**（规则 `10-evidence.md` §2，v3.83 新立的禁止项）。

---

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| **改 `drm_core.c` 回归 TH4** | slot0 路径逐字保留；未配 slot1 时行为与今完全一致；以第②批的 M2 回归（300 帧比对）为闸门 |
| **DRM 并发 / 事件泵互相清 pending** | 每槽独立 `flip_done`；`flip_handler` 按 slot 分派；`drmHandleEvent` 单点调用 |
| **热插拔抖动误迁移** | 连续 2 次采样一致才采纳；sysfs 读失败按 `disconnected` 处理但不抛异常 |
| **DPMS / DSI PHY oops（R20）** | 全程无桌面态；不做 CRTC power-cycle；不用 ASYNC_COMMIT；插拔不动 DSI 提交 |
| **`hq.*` 四键未定稿被"顺手"实现** | 方案显式排除；只登记不实现；`config.yaml` 零改动 |

---

## 交付顺序

1. 第①批（`display_hdmi.py` + `main.py`）→ 验证 → **回写 §14/§15 的偏离登记**
2. 第②批（`drm_core` slot 化 + pybind + HDMI 未触发态）→ 验证 + **M2 回归** → 回写 §10.3/§14
3. 两项均完成后，M2-1 ① 可判过；④ 的"满载"口径仍需在 TH5 双屏出图后补测
