# 证据：M0-10 关桌面演练（演示板 LubanCat-5 / 192.168.0.104）

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：一次演练的实测结果留档，对应 `docs/plans/M0.md` §3 的 **M0-10** 项 |
| 效力 | **非验收依据**（规则 `00-ssot.md` §3.5）；验收只认主基准 §13 复现方式 + §11.2 回归项 + `bench.py` 报告 |
| 对应主基准节 | §2.3（关桌面两态）、§3.5（DRM master、R13）、§10.5-1（自检第 4 项 DRM master）、§11.1-10、§12（R13） |
| 演练时点 | **2026-09-18 18:4x–18:5x CST** |
| 演练方式 | **有副作用**（`systemctl isolate/set-default`）——按规则 `30-board-safety.md` §4 已先向用户说明影响面与恢复步骤，并取得执行确认 |
| 状态 | `draft` |

> 本档只记录实测事实。关桌面两态的口径以主基准 §2.3 / §11.1-10 为准，本档不复制、不裁定。
> 演练期间 MIPI 屏黑屏、SSH（sshd 属 multi-user.target）保持可连。

## 1. 演练前只读探测（基线）

| 项 | 实测 |
|---|---|
| default target | `graphical.target` |
| 运行态 | `graphical.target` active + `multi-user.target` active |
| 显示管理器 | `gdm.service` **active/running** |
| DRM 设备 | `card0` = `platform-display-subsystem`（DSI-1 + HDMI-A-1）；`card1` = `platform-fdab0000.npu`（NPU，非显示） |
| DRM master 持有者 | `card0` 被 **Xorg（PID 1548）** 持有为 master，gnome-shell（PID 1791）为 X 客户端，systemd-logind（PID 844）持 fd |
| 会话 | `loginctl` 2 条：seat0 tty2（cat）+ 另一 cat 会话 |
| 工具 | `modetest` 可用；`sudo -n` = `NOPASSWD: ALL` |

结论：当前 `card0` 的 DRM master 被桌面（Xorg）占用，符合 R13 的阻塞形态；这正是关桌面演练的意义所在。

## 2. 态一：`isolate multi-user.target`（当次切换、回退验证）

| 步骤 | 操作 | 结果 |
|---|---|---|
| ① | `sudo systemctl isolate multi-user.target` | `get-default` **未变 = graphical.target**（← 不改默认） |
| ② | 验证桌面停 | `graphical.target` → **inactive**；`multi-user.target` → active；`gdm.service` → **inactive**；无 Xorg / gnome-shell 进程 |
| ③ | 验证 DRM master 可取得 | `sudo fuser /dev/dri/card0` → **(none)**（无持有者） |
| ④ | **modetest set mode 验证 master** | `modetest -M rockchip -s 221@142:1024x600` → 输出 `setting mode 1024x600-60.00Hz on connectors 221, crtc 142`，无权限/失败报错 ⇒ **取得 DRM master 成功** |
| ⑤ | 回退 | `sudo systemctl isolate graphical.target` → `get-default` 仍 graphical；`graphical.target` → **active**；`gdm.service` → active；Xorg 复现（PID 5110）+ gnome-shell（PID 5422）重新持有 `card0` master |

**态一判定 = 过**：当次切换可回退；multi-user 下可取得 DRM master（modetest set mode 成功）；回退后桌面完整恢复。

## 3. 态二：`systemctl set-default multi-user.target`（固化路径、回退验证）

| 步骤 | 操作 | 结果 |
|---|---|---|
| ① | `sudo systemctl set-default multi-user.target` | `Created symlink /etc/systemd/system/default.target → …/multi-user.target`；`get-default` → multi-user.target |
| ② | 验证运行态不变 | 固化只改符号链接、**当前运行态不变**：`graphical.target` 仍 active、`gdm.service` 仍 active |
| ③ | 回退（固化到演示镜像时） | `sudo systemctl set-default graphical.target` → symlink 回 `…/graphical.target`；`get-default` → graphical.target |

**态二判定 = 过**：`set-default` 仅改默认符号链接、对当前运行态无副作用；可回退。

## 4. 演练结论

| 出口判定项（§11.1-10） | 结果 |
|---|---|
| `isolate` 当次切换可回退 | ✅ modetest set mode 成功（取得 master）＋ `isolate graphical.target` 完整恢复桌面 |
| `set-default` 固化路径可回退 | ✅ 符号链接可来回切换；运行态不受影响 |
| multi-user 下取 DRM master | ✅ `isolate` 后 `card0` 无持有者、modetest 对 `DSI-1`(221)/`CRTC 142`(1024×600) set mode 成功 |

**M0-10 两项状态均验证通过。** 当前系统回落到 `default = graphical.target / gdm 运行`（开发态）；演示镜像固化为 `set-default multi-user.target` 的动作留到部署阶段（M3/M4，§2.3、§10.6-D）。

> 注：modetest set mode 仅验证了**单屏（DSI-1）**取 master 并出图；**双 CRTC 双屏异显**属 M0-9（DSI-1 + HDMI-A-1 双色条），需接 HDMI 显示器后另行打通。

## 5. 复现命令

```bash
# 态一（有副作用，演练时授权执行）
sudo systemctl isolate multi-user.target
sudo fuser /dev/dri/card0                                   # → (none)
sudo timeout 8 modetest -M rockchip -s 221@142:1024x600      # → set mode 成功
sudo systemctl isolate graphical.target                      # 回退

# 态二（有副作用，演练时授权执行）
sudo systemctl set-default multi-user.target
sudo systemctl get-default
sudo systemctl set-default graphical.target                  # 回退
```