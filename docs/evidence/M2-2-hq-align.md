

## 8.29 桌面双屏可操作性与桌面图标（GNOME 3.38 扩展载体）（v3.86 追加 57；O119 就地裁定）

**性质**：B/C 层**记录类**（**非验收依据**）。

### 8.29.1 用户报告与两条根因

用户 2026-09-22：「**退出返回桌面后，HDMI 屏是正常桌面，但 MIPI 屏只有背景，没有菜单，另外，HDMI 和 MIPI 屏都没看到演示应用的 shotcut**」。

| # | 现象 | 根因（实测） | 性质 |
|---|---|---|---|
| ① | MIPI（DSI）只有壁纸、无菜单 | 本板 Xorg = **2944×1080 扩展桌面**（`HDMI-1` = **primary** `1920x1080+0+0`、`DSI-1` = `1024x600+1920+0` 副屏），而 **GNOME 3.38 的顶部栏只画在主屏** | **设计行为，不是故障** |
| ② | 两屏都无桌面快捷方式 | **Debian 11 的 nautilus 3.38 不提供 `nautilus-desktop`**（`/usr/bin/nautilus-desktop` 不存在、无任何 nautilus 进程）⇒ `org.gnome.desktop.background show-desktop-icons` **无实现方**（该键虽为 `true`，本身不产生图标） | **载体缺失** |

### 8.29.2 裁定与落地

口径二选一（用户选定）：① 副屏菜单 = **装副屏顶栏扩展**；② 桌面图标 = **装桌面图标扩展**。

```
sudo apt-get install -y gnome-shell-extension-desktop-icons gnome-shell-extension-multi-monitors
# 附带装上：xdg-desktop-portal 1.8.1-1、libfuse2 2.9.9-5
gsettings set org.gnome.shell enabled-extensions \
    "['desktop-icons@csoriano', 'multi-monitors-add-on@spin83']"
```

| 包 | 扩展 UUID | `shell-version` | 作用 |
|---|---|---|---|
| `gnome-shell-extension-multi-monitors` 23-1 | `multi-monitors-add-on@spin83` | `3.38` | **给副屏也画顶部栏** |
| `gnome-shell-extension-desktop-icons` 20.04.0+git20200908-5 | `desktop-icons@csoriano` | `3.38.0` | **桌面图标（真正的实现方）** |

板端 GNOME Shell = **3.38.6** ⇒ 两者版本匹配。**恢复** = `apt purge` 两包 + 清空 `enabled-extensions`。

### 8.29.3 ⚠️ 会话重建口径（实测，必须随行）

**扩展只在会话启动时扫描** ⇒ 装/改后须过一次会话。本轮实测两条路径：

| 操作 | 结果 |
|---|---|
| **`sudo loginctl terminate-session <id>`** | ✘ **不能重建会话**：执行后**无 `Xorg` / `gnome-shell`**，gdm 停在登录界面（`Xorg.0.log` 以 `Server terminated successfully (0)` 收尾，之后**无新 Xorg**），会话号反复变化（`c29`→`c30`→`c31`），面板全黑 |
| **`sudo systemctl restart gdm3`** | ✔ **恢复**：`Xorg` + `gnome-session-binary` + `gnome-shell` 全部起来（gdm 配了 `AutomaticLoginEnable=true` / `AutomaticLogin=cat`，无需输密码），**显示布局不变**（`Screen 0: 2944×1080`） |

⇒ **正确口径 = `systemctl restart gdm3`**（已回写 §10.6-D 步骤 6）。这与 v3.72 记录的 `isolate graphical.target` + `gdm restart` 属**同一类风险**（拉不回 GNOME 会话），本次的 `terminate-session` 是该类风险的又一实例。

### 8.29.4 复核（截图为证）

`org.gnome.Shell.Screenshot` 抓整屏（2944×1080，左 HDMI + 右 MIPI）：

| 图档 | 读数 |
|---|---|
| `docs/evidence/reports/O119/desktop_dual_monitor.png` | 整屏 2944×1080；两屏顶部栏均有时钟 `9月22日 10:47` |
| `docs/evidence/reports/O119/desktop_mipi_panel.png` | **MIPI 区顶部栏已出现**（左「活动」+ 中时钟） |
| `docs/evidence/reports/O119/desktop_hdmi_icons_2x.png` | **HDMI 桌面左上出现「启动极暗光增强演示」启动器图标**（`camera-video` 图标 + 启动角标；同屏另有「主文件夹」「回收站」） |

扩展状态（`gnome-extensions info`）：`desktop-icons@csoriano` ⇒ **`状态: ENABLED`**、`multi-monitors-add-on@spin83` ⇒ **`状态: ENABLED`**；`journalctl` 内**无扩展 JS 报错**（仅有 bolt / GeoClue / polkit 等无关噪声），gnome-shell 存活。

### 8.29.5 未验

1. **桌面图标只验证了「主屏（HDMI）有图标」** —— `desktop-icons@csoriano` 在 3.38 只画**主屏**，**MIPI 副屏不会有桌面图标**（那边靠顶栏的「活动」/收藏进入，属预期）。
2. **两个扩展的兼容性只跑了 1 次会话**（n=1）；长时运行是否稳定未测。
3. **图标点击走的是 O116 的 `launch-demo.sh` 路径**（已在 §8.28 验证过同一脚本 + 同一条命令），**本轮未再点一次**。

## 8.30 两块屏都放可点的演示入口（`dash-to-dock` 多屏 dock）（v3.86 追加 58；O120 就地裁定）

**性质**：B/C 层**记录类**（**非验收依据**）。

### 8.30.1 用户报告与一条硬限制

用户 2026-09-22：「**现在 HDMI 有 shotcut，MIPI 屏有菜单栏，但菜单栏下无内容，MIPI 屏也没有 shotcut**」。

O119 只解决了「副屏有顶栏」，但**副屏顶栏下仍无任何可点的演示入口**。硬限制（`gsettings list-recursively` 实测）：

```
org.gnome.shell.extensions.desktop-icons show-mount true
org.gnome.shell.extensions.desktop-icons icon-size 'standard'
org.gnome.shell.extensions.desktop-icons show-home true
org.gnome.shell.extensions.desktop-icons show-trash true
```

⇒ `desktop-icons@csoriano` **只有这四项**、**无任何「副屏 / 所有显示器」开关** ⇒ 它在 GNOME 3.38 上**只画主屏**（当前主屏 = `HDMI-1`）⇒ **MIPI 上永远不会有桌面图标**（**硬限制，不是配置问题**）。

### 8.30.2 裁定与落地（`dash-to-dock` 多屏 dock）

口径二选一（用户选定）：**装 dock 扩展，两屏都有图标栏**。

```
sudo apt-get install -y gnome-shell-extension-dashtodock     # 69-1，shell-version = 3.36 / 3.38
gsettings set org.gnome.shell enabled-extensions \
    "['desktop-icons@csoriano', 'multi-monitors-add-on@spin83', 'dash-to-dock@micxgx.gmail.com']"
D=org.gnome.shell.extensions.dash-to-dock
gsettings set $D multi-monitor true          # ★ 关键：两块屏都显示 dock
gsettings set $D dock-position 'BOTTOM'
gsettings set $D autohide false
gsettings set $D dock-fixed true             # 常显（避免找不到入口）
gsettings set $D dash-max-icon-size 32
gsettings set $D icon-size-fixed true        # MIPI 只有 600 px 高，48 px 偏大
gsettings set $D show-apps-at-top true
```

**为什么能解决**：dock 的内容 = GNOME 收藏（`org.gnome.shell favorite-apps`），而 **`starlight-demo.desktop` 已在收藏首位** ⇒ **两块屏的 dock 里第一个应用图标就是演示启动器**，点一下即走 O116 的 `launch-demo.sh` 路径。三个扩展**互不替换**：顶栏 = `multi-monitors-add-on`、桌面图标 = `desktop-icons@csoriano`（主屏）、dock = `dash-to-dock`（两屏）。

**恢复** = `apt purge gnome-shell-extension-dashtodock` + 从 `enabled-extensions` 移除该 UUID。

### 8.30.3 复核（截图为证）

`org.gnome.Shell.Screenshot` 抓整屏（2944×1080）：

| 图档 | 读数 |
|---|---|
| `docs/evidence/reports/O120/desktop_mipi_dock.png` | **MIPI：顶部栏（「活动」+ 时钟）+ 底部 dock**；dock 首个应用图标 = **演示启动器**（`camera-video` 图标，位于应用网格按钮之后） |
| `docs/evidence/reports/O120/desktop_hdmi_dock.png` | **HDMI 底部同样有 dock**，同一组图标顺序 |
| `docs/evidence/reports/O120/desktop_dock_dual.png` | 整屏 2944×1080 原档（左 HDMI + 右 MIPI） |

扩展状态：三者均 **`状态: ENABLED`**；`xrandr` 布局仍 `Screen 0: 2944×1080`（`HDMI-1` primary + `DSI-1`）；`journalctl` 内无扩展 JS 报错。会话重建仍用 `sudo systemctl restart gdm3`（O119 口径，**本次再次复现成功**、布局不变）。

### 8.30.4 随行修正与未验

**随行修正**：O119 的证据报告目录由误建的 `docs/evidence/reports/O116/` 更正为 **`docs/evidence/reports/O119/`**（§10.3、§15 O119 行、§14 追加 57、本节 §8.29 的引用已同步改；`O116/` 目录已删除）。

**未验**：
1. **dock 图标未经点击实测** —— 截图证明图标**在位**，但「从 MIPI 的 dock 点它能否启动演示」**未点过**（该路径的脚本与命令已在 §8.28 端到端验证过，本轮沿用同一入口）。
2. **三个扩展同时加载只跑了本次一次会话**（n=1）；长时稳定未测。
3. **MIPI 的 dock 宽度**（1024 px 上 9 个图标 + 应用网格按钮）已接近满宽，**再加收藏项可能溢出**（未测）。

## 8.31 「点图标要等好几秒」的根因与修法：取 DRM master 的有界重试（v3.86 追加 59；O121 就地裁定）

**性质**：B/C 层**记录类**（**非验收依据**）。

### 8.31.1 用户报告与「单击无效」的反证

用户 2026-09-22：「**MIPI 屏上的底部有 dock，但单击演示图标并没有反应，需反复点击，或长按-新窗口，才能启动**」。

| 证据 | 读数 |
|---|---|
| `/home/cat/starlight_logs/launch.log` | **4 次点击**：`14:57:00` / `14:58:49` / `14:59:18` / `14:59:51` |
| sudo 日志 | **4 次** `COMMAND=/usr/bin/systemctl isolate --no-block multi-user.target` ⇒ **免密每次都成功** |
| 单元日志 | 前 **3 次**均**首拍** `main/boot_check_item item=TH4 result=fail code=E_DRM` ⇒ 进程退出 ⇒ `Restart=on-failure` 隔 **2~4 s** 重试才成功 |

⇒ **不是「点击无效」，也不是权限问题**：命令每次都执行了，只是**首拍取不到 DRM master**、靠重启补上，**屏上出现 2~4 s 黑档** ⇒ 用户误判为「没反应」。

### 8.31.2 根因（实测坐实）

桌面（`gnome-shell` / `Xorg`）与 `starlight-demo.service` **互斥**占用 `card0` 的 DRM master。而 `systemctl isolate multi-user.target` 的排序**只能保证 `gdm.service` 停完**：

- `display-manager.service` = `gdm.service`，`Type=dbus`（`systemctl show` 实测，**无 cgroup**）；
- **Xorg 不在其 cgroup 内** —— Xorg 是 gdm 经 logind 拉起的**用户会话进程**（`gdm-x-session` / `session-*.scope`）；
- ⇒ `graphical.target` 的 stop **不等 Xorg 退出** ⇒ 本单元首拍**抢在 master 释放之前**。

**同秒级证据**：`/var/log/Xorg.0.log` —— `Time: Tue Sep 22 15:15:43` 启动、`Server terminated successfully (0)` 收尾、文件 mtime **15:16:30**；而 demo 首拍失败 `2026-09-22T07:16:30.600Z`（= **15:16:30.600 CST**）。

### 8.31.3 被证伪的方案（如实留档，不得重复尝试）

先按「加排序」实现：`[Unit]` 增 `Conflicts=graphical.target` + `After=graphical.target`。**实测无效**：

```
15:16:30  sudo /usr/bin/systemctl isolate --no-block multi-user.target
15:16:30.600  INFO main boot_check_item item=TH4 result=fail code=E_DRM   ← 仍是首拍失败
TH4 失败总数 10 → 11
```

反向亦不成立：`isolate graphical.target` 时本单元**并未**被 `Conflicts` 停掉（demo 与桌面并存）。⇒ **两行已撤销**（`deploy/starlight-demo.service` 内保留说明性注释，不再写这两行）。

### 8.31.4 修法（交付链路有界重试）与实测

`display_mipi.py`：`_n.drm.configure()` 返回 **`E_DRM`** 时**有界重试** —— `DRM_MASTER_RETRY_N = 8` × `DRM_MASTER_RETRY_MS = 500` ms（上限 ≈4 s）；**只对 `E_DRM` 重试**、其余错误立即返回、全部失败仍返回 `E_DRM`（**返回码语义不变**）。`main.py` 在重试成功时记一行 `boot_check_item item=TH4 result=degraded code=E_DRM`（**不新增事件 / 字段**）。

**干净复现（先 `stop` 单元 → `isolate graphical.target` → 等桌面就绪 → `isolate --no-block multi-user.target`）**：

| 项 | 读数 |
|---|---|
| TH4 失败总数 | **11 = 基数 ⇒ 零新增失败** |
| 新增日志行 | `main boot_check_item item=TH4 result=degraded code=E_DRM`（15:21:20.582） |
| 进入 `RUN_DEMO` | `rsm_transition from=BOOT_CHECK to=RUN_DEMO reason=boot_ok` |
| **自 isolate 到单元 active** | **1.21 s**（此前 = 「首拍失败 + 2 s 重启」的 2~4 s） |
| 单元约束回读 | `Conflicts` / `After` 中**无 graphical 相关项**（两行已撤）✓ |

### 8.31.5 O117 收口（同轮，实测）

`log.py` 的 `FORCED_INFO_EVENTS` 增列 `service_exit` 与 `reset_key`，并同步 `docs/logging.md` §1。**实测**：`systemctl restart starlight-demo`（SIGTERM → `on_sig`）后，journal 与 `logging.dir` 中出现 **`main service_exit exit_code=0 uptime_s=0`** —— 该行此前在 `logging.level = "WARN"` 下**被级别过滤**（所以 2026-09-22 排查「demo 为何自行退出」时**只能靠 Xorg 启停反证**）。

### 8.31.6 O118 的覆盖与残留

开机首拍（`09:59:26` 的 `TH4 result=fail code=E_DRM`）属**同一类「master 未就绪」竞态** ⇒ **由同一有界重试吸收**。**残留 = 重启复验未做**（n=0）；**未裁定前不改单元其它取值**。

### 8.31.7 未验

1. **重试窗口的边界未压测**：若桌面退出耗时 > 4 s（重试上限），仍会退化为首拍失败 + `Restart`（**未构造该场景**）。
2. **`Restart` 上限交互未验**：连续失败 5 次仍会触发 `StartLimitBurst`（§10.6-A 契约行为）。
3. **`result=degraded` 行的频率未统计**：正常开机 / 点击路径下预期各 1 行，未做多轮统计（n=1）。

## 8.32 显示翻页失联的 `crtc_recover` 自愈：落地、正常路径复验与「触发路径复现受限」（v3.86 追加 61；O122 收口）

**性质**：B/C 层**记录类**（**非验收依据**）。契约面见主基准 §15 O122 与 §10.5（降级矩阵 `crtc_recover` 行）。

### 8.32.1 落地物（代码面，零新增键 / 事件 / 返回码）

| 落点 | 内容 |
|---|---|
| `native/drm_core.c` | 新增 `drm_recover_slot(int slot)` / `drm_recover()`：以**当前画布 fb** 重发 `drmModeSetCrtc`（重建 CRTC → 恢复 plane 绑定），成功置 `g_flip_done[slot]=1`；失败写 `g_err="RecoverSetCrtc"` |
| `native/drm_core.c` | `drm_compose_flip_slot` **两个失败点补写** `g_err` / `g_errno`：`drmModePageFlip < 0` ⇒ `"PageFlip"`；未收到 pageflip 完成事件 ⇒ `"PageFlipTimeout"`（经**既有** `drm.info()` 读出，**不新增接口**） |
| `native/starlight_native_pybind.cpp` | 暴露 `drm.recover()` / `drm.recover_slot(slot)` |
| `display_mipi.py` | `_flip()` **有界自愈**：返回负值时先 `recover()` 再重试本帧翻页，上限 `DRM_DISP_RECOVER_N = 2`（**机制值、非 §10.4 config 键**）；**只对负值自愈**（`1` = 过期帧属正常语义）；三处调用点（`compose_flip` / `compose_fullscreen` / `mark_busy`）统一走 `_flip`；**`compose_flip` 返回码语义不变** |
| `main.py` | `counters["pageflip_fail"]`（O122 前该分支**不计数**、负值被静默吞掉）+ `lost_edges` / `recover_edges` **边沿**登记 ⇒ `degrade_action`（`trigger=pageflip_fail` / `pageflip_recover`、`action=crtc_recover`，带 `rsm=` / `fsm=`）；失联态内**不刷屏** |

### 8.32.2 板端构建与部署（零告警）

```
Built target _realtime_apply
[ 50%] Building C object CMakeFiles/_starlight_native.dir/drm_core.c.o
[100%] Building CXX object CMakeFiles/_starlight_native.dir/starlight_native_pybind.cpp.o
Linking CXX shared library _starlight_native.cpython-39-aarch64-linux-gnu.so
```

增量构建**零告警**（无新增告警）。回读：`hasattr(n.drm, "recover") == True`；`drm.info()` **键表未变**（仍 `last_error` / `last_errno`）；`python3 -m py_compile` 通过。

### 8.32.3 正常路径复验（重启演示后）

| 项 | 读数 |
|---|---|
| TH4 启动 | `boot_check_item item=TH4 result=degraded code=E_DRM`（O121 有界重试）→ `rsm_transition from=BOOT_CHECK to=RUN_DEMO reason=boot_ok` |
| plane 绑定 | `dri/0/state` 两 plane 均 `allocated by = python3`（两块屏均已恢复为演示画面） |
| 停机摘要（旧进程） | `pageflip_fail=0` ⇒ **Python 侧改动确已部署** |
| `degrade_action` | **零行**（自愈未被触发，符合正常路径预期） |
| 单元状态 | `starlight-demo.service = active`、`graphical.target = inactive`、pid 53820、`RUN_DEMO` @ 18:00:59 CST；18:06:09 复核仍 active |

### 8.32.4 触发路径**复现受限**（n=0，如实登记）

演示**持 master** 时，外部注入**三条路线全部无效**：

| 注入手段 | 结果 |
|---|---|
| `chvt 3` / `chvt 1`（切 VT） | 无效 —— 内核 `fbcon` 的 mode restore 走 `drm_fb_helper_restore_fbdev_mode_unlocked` → `drm_master_internal_acquire`，被 `dev->master` 守卫挡掉（`EBUSY`）⇒ **不重做 `SetCrtc`** |
| `vtcon1` unbind / rebind | 同上，无效 |
| 新建外部 DRM 客户端调 `drmSetMaster` | 直接返回 `EBUSY`（`drm.info()` 实测 `last_error="drmSetMaster"`、`last_errno=16`） |

⇒ **结论**：只要本单元进程活着并持 master，**没有外部者能改写 CRTC** ⇒ 自愈路径在现网条件下**无法现场构造**，本项**注入路线整体作废**。自愈分支保持**防御性存在**（对任何外部 modeset 有效，不依赖 VT 归属），但**未经实机触发验证**。

### 8.32.5 用户所报现象的成因订正（重要）

用户 2026-09-22 17:58–18:00 所报「**两个屏都显示字符的 [机器时间] android work: did not sent uevent**」的真实成因链：

1. `reset_key press=long` → `main service_exit exit_code=0`（日志中当日 `service_exit` 共 **12** 次）；
2. 本单元退出序 `close(card0)` ⇒ **释放 DRM master**；
3. 内核 `fbcon` 拿回两个 CRTC ⇒ **两屏显示 console 文本**（`android_work: did not sent uevent` 即 console 输出）。

经用户确认：该长按**系其本人**在 MIPI 按钮区按压「EXIT (长按3秒)」测试**退回桌面**，**属设计行为**（O114 / O98），**不是缺陷**。

⇒ **与 15:22:08 的 debugfs 取证区分**：后者（两 plane `fb=225`、`allocated by = [fbcon]`，而本单元 `master=y`；旧进程 `wall=2925.98 s / frames=43786 / flips=5 / flip_skipped=0` ⇒ 49 min 静默）**仍然成立**，即**演示存活期间被外部 modeset 夺走 plane** 这一缺陷真实存在 ⇒ O122 与 §10.5 降级行**按防御性保留**，不因本次「复现受限」而撤销。

### 8.32.6 未验（残留，n=0）

1. **自愈路径实机未触发**：`recover_edges > 0` 的场景**未构造**，`DRM_DISP_RECOVER_N=2` 的重试上限**未压测**。
2. **HDMI 槽（slot 1 / TH5）未纳入自愈**：`display_hdmi.py` 仍只走 `compose_flip_slot(1, …)`；HDMI 缺席属**可降级项**（规则 `30-board-safety.md` §2），故本轮不扩面。
3. **`PageFlipTimeout` 分支未构造**：`g_err="PageFlipTimeout"` 的路径与 `drmModePageFlip` 返回负值路径**不共用同一构造法**，未单独验证。
4. **边沿登记的日志频率未统计**：正常路径为 0 行（本次），失联/恢复**成对**出现**未实测**（n=0）。

## 8.33 端到端延时翻倍的根因：TH2a 曲线估计内联在显示循环（v3.86 追加 62；O123 就地裁定）

**性质**：B/C 层**记录类**（**非验收依据**）。契约面见主基准 §15 O123 与 §6.1 第 413 / 415 行加注。

### 8.33.1 用户报告与时间线（从各步骤留的稳态实拍图上读出的屏上值）

用户 2026-09-22：「**原来的端到端延时为 60 ms 左右，现在为 100~200 ms**」。

| 时点 | 步骤 | 屏上端延时 | 帧率 |
|---|---|---|---|
| 9-21 22:49 | 追加 50（O110） | 060 ms | 15.0 |
| 9-21 22:57 / 23:08 / 23:15 | 追加 51 / 52 / 53 | 050 / 025 / 027 ms | 15.0 / 15.0 / 15.2 |
| **9-22 08:51** | **追加 54（O114，EXIT 长按）** | **049 ms** | 15.1 |
| **9-22 10:02** | **追加 55（O115，端延时改 1 s 滑窗 P50）** | **114 ms** | 15.5 |
| 9-22 15:15 / 17:58 | O121 / O122 期间与之后（单元长跑） | 144.5 / 136.9 ms | 13.8~14.3 |

⇒ 上跳钉在 **08:51 → 10:02** 之间；**"排除 MIPI 屏按钮故障"（O121 / O122）发生在 15:00 之后，不是该跳变的原因**（那时已是 114 ms）。

### 8.33.2 逐句 diff（`HEAD` = 9-21 13:01 快照 vs 现状）

1. **延时记账零变化**：`display_mipi.compose_flip` 的配对四行（`_lat_submit_ns` / `_lat_flip_ns` / `_lat_flip_seq` / `_lat_t_capture_ns`）与 `lat_pair()` 的四个字段**逐字节相同**；`main.py._lat_pct` 亦逐字节相同（`git show HEAD:main.py` 比对）⇒ **不是配对或口径把数字算大**。
2. **主循环每帧新增仅 5 项**，均 ≤ 毫秒级：`_pump_pointer` / `_pump_keys` / `_pump_injection` / `_pump_hq_lifecycle`（常规态空转）、**每帧 `hdmi.mirror()`**（RGA 1.875× P50 1.88 ms + 一次 HDMI pageflip 等待）、`osd.draw_cursor()`、`pageflip_fail` 计数与两条边沿比较。**没有任何一步是 70 ms 量级**。
3. **唯一超契约项 = 曲线估计内联**：§6.1 第 413 / 415 行要求它是"独立循环、不在帧路径、不参与 ≤32 ms 合计"；实现为 `main.py` 每 `est_every` 帧**同步**调 `CurveEstimator.estimate()`（RGA 缩放 + NPU 双条带 + EMA）。

### 8.33.3 被排除的解释（如实留档，不得重复尝试）

| 解释 | 否证 |
|---|---|
| 帧率档 30→15 fps（O91） | `O103`（9-21）**已在 15 fps 档**下实测端到端 P50 **63.8 ms** |
| 图形栈残留抢资源 | 只读核对：`graphical.target` / `gdm3` 均 `inactive`，**无任何 Xorg / gnome-shell / gdm 进程**；演示进程 61.6% CPU、loadavg 1.8 |
| HDMI 侧 vblank 拖慢 | 两路 CRTC 实测均 **60 Hz**（HDMI `1920x1080@60` / DSI `1024x600@60`）⇒ 等待上界 16.7 ms |
| 应用侧"跟不上" | **独立于延时代码的只读测量**：220 Hz 采样 `dri/0/state` 的 plane `fb=` 翻转 ⇒ **翻页率 14.96 Hz**（与 15 fps 档一致） |
| 曝光参与该数字 | `capture.py` 自检 `counters` 实测 **`ts_source='EOF'`**（`ts_source_changes=0`）⇒ 口径 = **EOF→vblank、不含曝光**；且 9-21 的 060 ms **小于**曝光 66.7 ms，本不可能含曝光 |
| 曝光时长变化（A/B 实测） | 现场把 `exposure` 8974 → 4487（33.3 ms）后屏上读数仍在 **88~210 ms** 区间（与改前 100~200 重叠）⇒ **1 s 滑窗 P50 的自身抖动（跨约 2 个帧周期）大于 33 ms 的预期位移**，该对照**不具判别力**，已按用户指示停止该路线 |

### 8.33.4 决定性证据（受控 A/B，同条件 600 帧 × 2 轮）

`main.py --frames 600 --report-json`；板 `LubanCat-5 V2`；`camera.fps=15` / `vblank_lines=7906`（VMAX 8986 ⇒ **帧周期 66.68 ms**）；`exposure=8974`；`ts_source=EOF`；无触发。

| 项 | `est_every=2`（现网值） | `est_every=6` | Δ |
|---|---|---|---|
| 端到端 P50 / P95（ms） | **152.5 / 227.2** | **80.7 / 166.3** | **−71.8 / −60.9** |
| 采集→提交 P50（ms） | 144.1 | 68.9 | −75.2 |
| 提交→vblank P50（ms） | 9.0 | 9.2 | ~0 |
| 单轮迭代（1 / `fps_active`，ms） | 76.9 | 71.6 | **−5.3** |
| **推算队列驻留**（采集→提交 − 单轮） | **+67.2（≈1 个帧周期）** | **−2.7（≈0）** | |
| `f_est`（Hz） | 7.15 | 2.47 | −4.68 |
| 配对率 / `dropped_stale` / `lease_timeouts` | 1.0 / 0 / 0 | 1.0 / 0 / 0 | — |

⇒ **单轮只差 5.3 ms，端到端却差 71.8 ms**：机制 = `端到端 = 队列驻留 + 应用处理 + pageflip 排队`，**单轮耗时跨过帧周期（66.68 ms）⇒ 队列恒定驻留一帧（+66.8 ms）**；未跨过 ⇒ 驻留 0。**是阶跃而非线性**——这正是"几毫秒改动换来一个帧周期延时"、以及用户"不可能变化这么大"的观感来源。

### 8.33.5 native 关键取证（决定线程化是否有效）

`native/starlight_native_pybind.cpp`：`hq.run` / `pack_rgb888_nhwc_f32` / `to_rgb888` 三条**均已** `py::gil_scoped_release`，且注释记明理由——"pybind 默认持 GIL ⇒ 会把主 Python 线程（TH4 合成主循环）冻住…使『独立线程』方案被 GIL 架空（实测：8.3 s 内 MIPI 仅翻 1 帧）"；而 **`rknn.estimate` 绑定未释放 GIL**。⇒ **TH2a 线程化必须同步补 `py::gil_scoped_release`，否则收益归零**（该结论已写入 O123 裁定值）。

### 8.33.6 同期随行事实（不得只报优点）

1. **现网实际 13.0 fps（`fps_steady` 13.35），未达 O93 的"15 fps 档"**：`dropped_stale=0` 且 `frames==flips` ⇒ 消费速率 = 产出速率 ⇒ **传感器被应用节流**；与 `O96(a)` 的「14.3 < 14.99」同族但更低。
2. 提交→vblank **P50 9.0 / P95 16.2 ms**（≤ 面板周期 16.67）⇒ 显示侧无问题。
3. 镜像链内 **P50 1.88 / P90 2.08 / max 2.34 ms** ⇒ §6.1 O85 行 ≤3 ms **达标**，非瓶颈。
4. `O103` 记的 63.8 ms 当时**同为 `est_every=2`**（`fps_active` 14.24）⇒ 今日同档 152.5 ms、单轮 76.9 ms 与之仅差 6.7 ms 却换来 89 ms 延时，**台阶的精确位置未钉死**（见 8.33.7）。

### 8.33.7 未验（残留，不得写成结论）

1. **阈值精确位置未钉死**：需 `est_every` 2/3/4/6 × 帧率档**扫档**才能画出该台阶曲线；现有数据只能给出"跨过即 +1 帧周期"的定性结论。
2. ~~**线程化实现与板端复验未做**~~ —— **（v3.86 追加 62 落地点）已实现并复验通过**，见 §8.33.9；本节的 A/B 是**用 `est_every` 做的代理实验**，其结论由落地点复核。
3. **`O76` 的空链路基线仍缺**：按规则 `30-board-safety.md` §3，归因未闭合前**不得据此重定任何判据**；本节两个报告仅作 O19 采集方式所需的「**常态链路对照**」证据。
4. **本轮未测**：`est_every` 对 `f_est` / 收敛时间的独立复算（沿用 O103 的 7.527 Hz / 398.6 ms）。

### 8.33.8 归档物

`docs/evidence/reports/lat-inline-est/probe_est2_600f.json` / `probe_est6_600f.json` —— 均为 `main.py --report-json` 的**原样输出**（含 `lat_*_all_ms` 原始数组、`counters`、`th5`、`boot_s`），**未做任何加工**（纪律见规则 `10-evidence.md`）。

### 8.33.9 落地与板端复验（v3.86 追加 62 落地点；O123 已实现）

**改动清单（三处，均不新增 config 键 / 日志事件 / 返回码）**：

| 文件 | 改动 |
|---|---|
| `curve_estim.py` | 拆出 `request(frame)`（**同步只做 RGA 缩放**，持帧时长 = 一次 RGA，lease 契约不变）、`_worker()`（NPU 双条带 + EMA + 发布；**不触 RGA** ⇒ RGA 单调用方 = 主线程）、`latest()`（非阻塞取值）、`wait_first()`（**仅首拍**有界等一次发布）；`publish()` 拆出 `_publish_locked()` 供 worker / 自检共用；`counters()` 增两个诊断量 `req_skipped` / `est_fail` |
| `main.py` | 每 `est_every` 帧只调 `request()`；曲线场第 0 帧用 `wait_first()`、此后一律 `latest()`（**帧路径不再等 worker**） |
| `native/starlight_native_pybind.cpp` | `rknn.estimate` 绑定补 `py::gil_scoped_release`（**前提项**：不释放则线程被 GIL 架空、收益归零——先例见 §8.33.5） |

**板端构建**：`cmake --build build -j4` ⇒ `Building CXX object …/native/starlight_native_pybind.cpp.o` + `Linking CXX shared module ../_starlight_native.cpython-39-aarch64-linux-gnu.so`，**零新增告警**（§11.2 构建零告警）；旧产物已按惯例备份为 `.so.bak.20260922a`。

**复验（两组 600 帧，`main.py --frames 600 --report-json`，条件同 §8.33.4）**：

| 项 | 落地前（`est_every=2`） | 落地后 R1 / R2 | 判据 |
|---|---|---|---|
| 端到端 P50 / P95（ms） | 152.5 / 227.2 | **48.7 / 96.0** · **54.4 / 128.9** | 预期"~80 ms 量级" ⇒ **达成（且更优）** |
| 采集→提交 P50（ms） | 144.1 | 38.7 / 47.6 | vs §6.1 帧路径 32 + 镜像 1.9 = 33.9 ⇒ 余量转正 |
| 提交→vblank P50（ms） | 9.0 | 8.9 / 8.5 | 不变（显示侧非瓶颈） |
| `f_est`（Hz） | 7.15 | **7.64 / 7.66** | 不得退到 2.47 ⇒ **达成（略升）** |
| `estimates`（600 帧） | 300 | 300 / 300 | = 600/2 ⇒ worker **零跳过** |
| `fps_active` / `fps_steady` | 13.01 / 13.35 | 14.16 / 14.65 | 改善 |
| 配对率 | 1.0 | 1.0 / 1.0 | 不回归 |
| `dropped_stale` / `lease_timeouts` / `flip_skipped` / `pageflip_fail` | 全 0 | **全 0** | 不回归 |
| `stream_on_count` | 1 | 1 | 契约 |

**过程中发现并修掉一处缺陷（如实登记）**：首版线程化后**第 0 帧** `latest()` 仍为 `None` ⇒ `main.py` 构造 `st["curve_field"]` 时直接 `AttributeError: 'NoneType' object has no attribute 'field'`（实测崩溃一次）。根因 = O123 之前**首帧是"同步估计 + 立即发布"**，故"曲线场从第 0 帧起有效"这一前提被帧路径隐式依赖，而线程化后发布变成异步 ⇒ **该脆弱点首次暴露**。处置 = 首拍用有界 `wait_first(2.0 s)` 把旧行为等回来（**仅启动期一次**；稳态帧路径仍不等待）；未采用"让帧路径容忍 `None`"的做法，因为那会改变 TH4/融合的输入契约（属另一件事）。

**复验后的口径边界（未变）**：屏上「端延时」仍只作现场判读（O115）；验收仍只认 `bench.py` 报告；`O76` 的空链路基线仍缺 ⇒ 不据此重定任何判据。
