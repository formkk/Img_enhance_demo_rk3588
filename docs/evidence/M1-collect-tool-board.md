# 证据：采集工具 v3.70 改造的**板上实测**（命名 + 无损裸 NV12 + 逐帧 sidecar）

| 项 | 内容 |
|---|---|
| 地位 | 记录类（C 层）：**一次实验的原始记录**，非验收依据（规则 `00-ssot.md` §2/§3.5） |
| 效力 | 低于主基准与 A 层附件规范；结论以主基准 §14（v3.71）与 `docs/design/dataset.md` 为准 |
| 对应主基准节 | §10.3（`tools/collect/capture_app.py`）、§14（v3.70 / v3.71）、§15（O59 的采集侧约定）、§9.5（ISP 指纹）、`docs/design/dataset.md` §5.2.8 |
| 状态 | `frozen`（2026-09-19 实测完成；**遗留未验项见 §6**） |

> **测量条件随行**（规则 `10-evidence.md` §1）：板型 **Embedfire LubanCat-5 V2**；内核 **5.10.160 #16**；
> 节点 `/dev/video11`（`rkisp_mainpath`，MPLANE）；Python **3.9.2**；`gst-launch-1.0`（含
> `v4l2src / videoconvert / tee / queue / mppjpegenc / multipartmux / fdsink / rawvideoparse` 全部可用）；
> `rkaiq_3A_server` 运行中（pid 395）；被验对象 = `tools/collect/capture_app.py`（v3.70 版本，
> `grep -c raw_reader` = 1）。
> **权重 / 曲线档 / 精度档 / config 快照哈希：N/A**（本实验不涉模型与 config，工具不读写 `config.yaml`）。

---

## 1. 目的

v3.70 的改造（命名 + 无损裸 NV12 + 逐帧 sidecar）只做过**开发机离线自测**与 `videotestsrc` 代源的
管线结构验证；**真机 `v4l2src` + `mppjpegenc` 下的双 `fdsink` 共存、路② 实际帧长、端到端采集落盘**
均未验证。本实验补齐这三项。

## 2. 方法与复现命令

```bash
# 1) 部署（板上原本无 tools/collect/，仅有交付链路）
mkdir -p /home/cat/starlight/tools/collect
scp tools/collect/capture_app.py board:/home/cat/starlight/tools/collect/capture_app.py

# 2) 只读前置：相机是否被占用 / 格式回读
ssh board 'fuser /dev/video11; pgrep -af "main.py|capture_app|cam_preview|gst-launch"'
ssh board 'v4l2-ctl -d /dev/video11 --get-fmt-video'

# 3) 启动（后台）
ssh board 'cd /home/cat/starlight && nohup python3 tools/collect/capture_app.py \
    --outdir /home/cat/capture_data_test --port 8091 > /tmp/cap.log 2>&1 < /dev/null &'

# 4) 采集动作（UI 三键的等价 HTTP 调用）
curl -s -X POST 'http://127.0.0.1:8091/capture?kind=gt'
curl -s -X POST 'http://127.0.0.1:8091/capture?kind=dark'    # 应落同帧槽
curl -s -X POST 'http://127.0.0.1:8091/capture?kind=dark'    # 应开新帧槽

# 5) 裸帧自检（解码回 JPEG；注意必须给 rawvideoparse 指定 format/width/height）
gst-launch-1.0 -q filesrc location=<f>.nv12 ! rawvideoparse format=nv12 width=1920 height=1080 \
    ! videoconvert ! jpegenc ! filesink location=/tmp/chk.jpg
```

## 3. 结果（实测）

### 3.1 只读前置：格式回读**与实现假设完全一致**

```
Format Video Capture Multiplanar:
        Width/Height      : 1920/1080
        Pixel Format      : 'NV12' (Y/CbCr 4:2:0)
        Number of planes  : 1
        Plane 0 : Bytes per Line : 1920     Size Image : 3110400
```

⇒ 该节点**单平面、continuous、无填充**，`stride = 1920`、`uv_offset = 2073600`、`size_bytes = 3110400`
——与 `capture_app.py` 按 `w*h*3/2` 推算并写入 sidecar 的三项**逐位一致**（`--raw-frame-size` 覆盖口未启用）。

### 3.2 双分支共存：**成立**，且路② 满帧率无丢帧

启动约 8 s 后连续三次 `/stat`：

| 时刻 | 路① JPEG `seq` | 路② 裸流 `raw_seq` | `err` | `raw_err` | `dup_run` |
|---|---|---|---|---|---|
| T+8 s | 298 | 302 | 空 | 空 | 0 |
| T+10 s | 380 | 382 | 空 | 空 | 0 |
| T+12 s | 461 | 464 | 空 | 空 | 0 |

冻结前最后一次采样：`seq = 5556`、`raw_seq = 5557`。
⇒ **`raw_seq` 与 `seq` 全程同步（差值 ≤4）**，即**路② 按满帧率消费、`leaky=downstream` 未触发实质丢帧**；
两路均无错误。实测帧率 ≈ 40 fps（1080p@60 overlay 下的既有现象）。

### 3.3 采集动作与命名：**全部符合裁定**

| 动作 | 返回文案 | 落盘文件 |
|---|---|---|
| `kind=gt` | `260919_01_0001_gt.nv12 已保存（无损 NV12, 3037 KB；场景 01 帧槽 0001）` | `260919_01_0001_gt.nv12` |
| `kind=dark` | `260919_01_0001_dark.nv12 …（场景 01 帧槽 0001）` | `260919_01_0001_dark.nv12`（**与 gt 同帧槽 ⇒ 成对**） |
| `kind=dark`（重复） | `260919_01_0002_dark.nv12 …（场景 01 帧槽 0002）` | `260919_01_0002_dark.nv12`（**开新帧槽**） |
| `/nextscene` | `01 结束（帧槽 0002 缺 gt；累计 暗 2 / 亮 1）→ 进入 02` | `.capture_state.json` 同步 |

目录实测：**每个 `.nv12` 恰为 3,110,400 B**；**sidecar 逐帧一份**（`*_0001_dark.json` / `*_0001_gt.json` /
`*_0002_dark.json` 各一，与图像同主干名）；sidecar 的 `capture_condition` 含
`stride=1920 / uv_offset=2073600 / size_bytes=3110400`，`schema=capture/v2`、`slot`、`frame_id`、`pair_id` 齐备。

### 3.4 裸帧内容自检：**帧对齐正确、无撕裂**

- **平面统计**（`260919_01_0001_dark.nv12`）：
  Y 平面 `len=2073600 mean=101.4 min=4 max=255`；UV 平面 `len=1036800 mean=145.0 min=103 max=207`。
  ⇒ UV 平面**落在偏移 2073600**（若切片错位，该区段会呈现亮度特征：宽范围 0–255、mean ≈100），
  **窄幅色度分布即证帧对齐**。
- **解码回看**：`rawvideoparse → videoconvert → jpegenc` 成功解出 1920×1080 图像（187,906 B），
  **画面完整、无撕裂、无错位**（画面竖置 = O31 残留「摆正」未做，与本次改造无关）。
- **附带观察（不作结论）**：该临时解码命令**未指定 CSC / range**，解出图**偏品红**；
  这正是 **O54**（CSC 矩阵与 range 的显式化）要解决的问题域。**因果未验证，只记现象**。

## 4. 与 v3.70 的对照

| 项 | v3.70 登记的状态 | 本实验结论 |
|---|---|---|
| 双 `fdsink` 共存 | 未验证（仅 `videotestsrc` 代源） | ✅ **真机成立** |
| 路② 实际帧长 | 未验证（假设 3110400） | ✅ **实测 3110400，与假设一致** |
| 端到端采集落盘 / 命名 / sidecar | 未验证（仅离线单测） | ✅ **全部符合裁定** |
| 路② 长跑稳定性 | 未验证 | ⚠️ **见 §5：非工具侧原因中断** |

## 5. 中断事件：内核 oops（**非本工具缺陷**）

首轮长跑约 **2.3 min**（`seq` 达 5556）后，**两路同时冻结**（`seq` / `raw_seq` 均停、无 traceback、
gst 进程仍存活）。同期 `dmesg`：

```
[ 9849.898258] Unable to handle kernel NULL pointer dereference at virtual address 00000000000000f0
[ 9849.902881] Internal error: Oops: 96000005 [#1] SMP
[ 9849.903588] CPU: 0 PID: 27063 Comm: Xorg Not tainted 5.10.160 #16
[ 9849.905109] pc : samsung_mipi_dcphy_power_on+0x6a0/0x8ec
```

**已核实的事实**：① oops 位于 **DSI PHY**（显示侧），**触发进程 = `Xorg`**（PID 27063）；
② 时间戳 9849.9 s 与冻结时刻**吻合**；③ **本工具不触碰 DRM / DSI**（只开 `/dev/video11`）；
④ 与主基准 v3.66 §14 已记录的「DSI 原子提交卡死 → `samsung_mipi_dcphy_power_on`」**同一处**。

**未验证（不得写成结论）**：oops 与相机链路冻结之间的**因果**。本次**无对照实验**
（缺「关桌面 / 停 Xorg 后复跑」一组）。按纪律只记「**现象 + 时间吻合 + 待查**」。

**收尾实测**：杀掉本工具后，`v4l2-ctl -d /dev/video11 --stream-mmap --stream-count=10` **正常出 10 帧**、
ISP 正常（`rkisp0-vir0: first params buf queue`）⇒ **相机链路自愈，本次未需重启**（与 v3.66 记录的
「D 态不可中断、只能重启」情形不同）。残留一个 D 态 `kworker/0:2+events`（oops 遗留）。

## 6. 遗留未验项（如实登记）

1. **长跑稳定性**：因 §5 的中断，**路② 的长时间（>3 min）稳定性未验**；对照实验（关桌面后复跑）未做。
2. **`--raw-frame-size` 覆盖口**：未触发（默认值与实测一致），其正确性仅由离线单测覆盖。
3. **U1 单边图流程**：本次只验了「gt + dark 成对」与「重复 dark 开新槽」，
   **未按 U1 的完整动作序列**（连续多取景、每取景多光照）走一遍。
4. **`--outdir` 指向真实采集目录**：本次用测试目录，未验证 3 MB/帧 × 上百帧的实际落盘与 rsync 流程。

## 7. 复现要点（坑）

1. **`rawvideoparse` 必需**：`filesrc ! video/x-raw,format=NV12,width=…,height=…` **会失败**
   （caps 未 fix，报 `Filter caps do not completely specify the output format`）；
   须用 `rawvideoparse format=nv12 width=1920 height=1080`。
2. **板上原无 `tools/collect/`**：`/home/cat/starlight/` 只是交付链路部署；采集工具须**单独部署**。
3. **`pkill -f "gst-launch-1.0"` 会自杀**：ssh 的 `bash -c` 命令行含该字符串 ⇒ 须写成 `gst-launch-1[.]0`。
4. **工具与主链路 TH1 互斥**：采集期间不得跑 `main.py`（同 `/dev/video11`，见 `tools/collect/README.md` §2）。

---

## 8. DPMS 归因与 A/B 对照（**v3.72 追加**；用户 2026-09-19 提问"是不是锁屏或休眠"派生）

### 8.1 oops 的触发路径 = **DPMS**（调用栈铁证）

```
Unable to handle kernel NULL pointer dereference at 0x00000000000000f0
Internal error: Oops: 96000005 [#1] SMP
CPU: 0 PID: 27063 Comm: Xorg Not tainted 5.10.160 #16
 pc : samsung_mipi_dcphy_power_on+0x6a0/0x8ec
 lr : samsung_mipi_dcphy_power_on+0x5c/0x8ec
      samsung_mipi_dcphy_power_on+0x6a0/0x8ec
      phy_power_on+0x80/0x130
      drm_atomic_connector_commit_dpms+0xf0/0x120     ← 显示电源管理（点亮）路径
```

紧邻前一刻：`vop2_crtc_atomic_enable … Update mode to 1024x600p60`（CRTC 被重新点亮）。

### 8.2 **不是休眠**（已被证据排除）

```
gsd-power: Error calling suspend action:
           GDBus.Error:org.freedesktop.login1.SleepVerbNotSupported: Sleep verb "suspend" not supported
```
⇒ 系统**不支持 suspend**，GNOME 尝试过但被内核拒绝；`sleep-inactive-ac-timeout=1200` 形同虚设。

### 8.3 而是**熄屏/锁屏（DPMS）**，且**高频发生**

桌面设置：`org.gnome.desktop.session idle-delay = 300`、`screensaver lock-enabled = true`。

本 boot 内 CRTC **enable/disable 累计 59 次**（约每 30 s ~ 几分钟一轮，桌面活跃期持续 2.7 h）
⇒ 每一次"点亮 DSI"都是一次抽奖；**命中率 ≈ 1/59 ≈ 1.7%**。13:15:43 那次中了。

**更正（相对本档 §5 的初版登记）**：§5 写的"3 次 oops"是 `grep -c` 数到**同一次 oops 的 3 行**
（pc / lr / trace）⇒ **实际每 boot 只 1 次 oops**（上次 boot 同样 1 次）。§5 的因果表述同时**下调**：
DSI 用 `samsung_mipi_dcphy`、CSI 用 `rockchip-csi2-dphy`，**是两个不同驱动**，
"oops 拖死相机"缺机制证据（候选路径 = oops 留下的 D 态 `kworker` 卡住 dma-heap/CMA 锁）。

### 8.4 A/B 对照（关桌面 vs 桌面在跑）

| 腿 | 条件 | 时长 | 结果 | 新增 oops | 新增 CRTC 事件 |
|---|---|---|---|---|---|
| **1** | 桌面**完全活跃**（gnome-shell + gsd-power，DPMS 频繁） | **2.3 min** | **两路同时冻结** | **1** | — |
| **2** | **桌面完全停止**（`Xorg` 进程数 = 0） | **5.5 min** | **全程正常** | 0 | **0** |
| 3 | 仅 Xorg、无 gnome-shell ⇒ **DPMS 未恢复，判为无效腿** | 5.5 min | 正常 | 0 | 0 |

腿 2 实测：`seq` 297→3298→6898→10498→13501、`raw_seq` 298→3300→6900→10502→13503
（**全程同步、差 ≤4**）、`err`/`raw_err` 空；采集落盘 3,110,400 B。

**结论（有保留）**：**判别变量 = DPMS/CRTC power-cycle 活动**——关桌面后该活动**归零**（新增 0 次），
不再有 `power_on` 调用机会，故 5.5 min 稳定（比腿 1 的 2.3 min 长 2.4×）。
**但"冻结"本身仅 1 次观测（n=1）**，且**未取得反例**（腿 3 因 gnome-shell 未起而失效）
⇒ 登记为「**强支持、非定论**」，**不得写成"已定因果"**。

### 8.5 加固实验（未做，留给后续）

要闭合因果，需补两腿：**(a)** 桌面活跃下跑到 **>3 min 且复现冻结**（提高 n）；
**(b)** 关桌面下跑到 **≥30 min** 仍不冻结。两者都要在 M1 采集的真实时长量级上做。

### 8.6 本次实验的**副作用（如实登记，教训）**

`systemctl isolate multi-user.target` + `stop gdm` + `loginctl terminate-session` 组合**能停掉桌面**，
但 **`isolate graphical.target` + `gdm restart` 拉不回 GNOME 会话**：
autologin 的 PAM session **能开**（`pam_unix(gdm-autologin:session): session opened for user cat`），
但 **gnome-shell 始终不起**、`card0` 无持有者，并留下一个
**卡死的 Xorg 僵尸（`Zl` 态，占 vt2）**——`kill -9` 无效，**很可能就是那次 oops 的进程在内核里没能退出**。

⇒ **停桌面后想恢复，必须重启板子**；"关桌面"这一动作**不应轻率执行**。

**随后 `sudo reboot` 未完成**：约 8 min 后板子 **ping 通但 22/5900/80 全部 refused**，
ARP 显示**本地管理型 MAC（`12:26:...`，早期启动回退 MAC）** ⇒ **需现场断电重启**（远程不可恢复）。
**因果未验证**（可能是僵尸进程挡住关机流程，也可能与该 oops 同源）——**只记现象**。

### 8.7 对 M1 采集的直接含义

1. **M1 采集应在无桌面状态下进行**（本项目 §11.1-10 的"关桌面演练"本就要求此能力），
   这同时把本 oops 的触发机会降为 0；**但关桌面/恢复桌面须按 §8.6 的教训操作（预备重启）**。
2. **采集工具与主链路 TH1 互斥**（§2）——采集期间本就不跑 `main.py`。
3. 该 oops 是**内核/厂商 BSP 缺陷**，不在本项目代码范围；**升级内核或向厂商反馈**是唯一根治路径。
4. 风险项已登记为主基准 **§12 的 R20**。
