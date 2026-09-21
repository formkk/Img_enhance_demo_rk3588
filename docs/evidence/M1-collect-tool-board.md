# 证据：采集工具 v3.70 改造的**板上实测**（命名 + 无损裸 NV12 + 逐帧 sidecar）

| 项 | 内容 |
|---|---|
| 地位 | 记录类（C 层）：**一次实验的原始记录**，非验收依据（规则 `00-ssot.md` §2/§3.5） |
| 效力 | 低于主基准与 A 层附件规范；结论以主基准 §14（v3.71）与 `docs/design/dataset.md` 为准 |
| 对应主基准节 | §10.3（`tools/collect/capture_app.py`、**v3.79 的仓库根 `sensor_lock.py`**）、§14（v3.70 / v3.71 / **v3.77** / **v3.79** / **v3.80** / **v3.81** / **v3.82** / **v3.83**）、§15（O59 的采集侧约定 / **O63 数值更正 / O65** / **v3.79 的 O63 收口** / **v3.81 的 O54 满幅路径阻塞**）、§9.5（ISP 指纹 / **v3.77 的纯 IQ 路径与 IQ-as-SSOT** / **v3.79 的 1080p@30 锁定**）、§12（**v3.79 新登记 R22** / **v3.80 的整链复验：R22 失效模式未复现** / **v3.82 的 R20（DPMS / DSI PHY oops）与 R21（3A 重启纪律）**）、§8.3（**v3.81 的集合 B 满幅应用路径**）、`docs/design/dataset.md` §5.2.8 / **§4.5（固定 ISP 与曝光、只改光源亮度）** |
| 状态 | `frozen`（2026-09-19 实测完成；**遗留未验项见 §6 与 §12.7**；**§10.7 为 v3.77 追加**；**§12 为 v3.79 追加**；**§13 为 v3.80 追加 = `main.py` 整链复验（临时验证运行，非 M2 出口证据）**；**§14 为 v3.81 追加 = 试采（暗/亮对）+ 离线 ZDE++ 增强（供人工审查；非 U1 数据集、非验收证据）**；**§15 为 v3.82 追加 = ISP/3A 异常态事件与恢复（现场登记，非受控实验、无对照）**；**§16 为 v3.82 追加 = 第二次试采（暗/亮对）+ 暗帧离线 ZDE++ 增强（供人工审查；非 U1 数据集、非验收证据）**；**§17 为 v3.83 追加 = 固定曝光/增益取值定稿（1/30 s + 8×）的取证记录 + range 排除项（现场目视判定，非受控实验、无对照）**；**§18 为 v3.83 追加 = HDMI 显示器接入与双 CRTC 异显核对 + modetest 双屏色条打通 + `hdmi_mode_selection` 定稿（只读核对为主；§18.4 为副作用验证，§18.5 为键定稿）**） |

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

---

## 9. 3DNR A/B 实验（**v3.75 追加；实验作废，但产出两条可用结论**）

**目的**：为 O62（IQ 文件全局性）提供依据——即"**演示是否需要 3DNR**"。

**方法**：同一静止场景、同一光照下，**3DNR 开 / 关各录 150 帧裸 NV12**
（`v4l2-ctl -d /dev/video11 --stream-mmap=4 --stream-count=150 --stream-to=<f>.raw`），比较**帧间差**。
**3DNR 开关手段** = 改 IQ 的 `bayertnr_v2/TuningPara/enable` + `systemctl restart rkaiq_3A.service`。

### 9.1 结果（实测）

| 条件 | 帧差 mean | 帧差 std | 单帧 Y 均值 | 单帧 Y std | Laplacian 方差 | 首帧 PNG |
|---|---|---|---|---|---|---|
| **3DNR 开**（Phase A） | 0.164 | 0.416 | **118.21** | 38.9 | **7.4** | 3,018,610 B |
| **3DNR 关**（Phase B） | 5.778 | 4.826 | **54.76** | 53.6 | **84.5** | 1,393,520 B |

### 9.2 ⚠️ 本实验**作废**（混淆变量，如实登记）

两阶段**亮度差 2×（≈1 档曝光）、清晰度差 11×** ⇒ **两阶段的曝光/增益根本不同**，**不是可比的 A/B**。

**根因**：Phase B 前**重启了 `rkaiq_3A.service`**，而 **AE 仍是自动的**
（`config.yaml` 的 `camera.exposure_us` 等仍为 `null`、定稿时点 M1）
⇒ **3A 重启后 AE 重新收敛到另一个曝光**。
⇒ **帧差 0.164 vs 5.778 的 35× 差不可归因于 3DNR**（更暗 ⇒ 更高增益 ⇒ 噪声本来就大）。

**次要现象（未解释，只记）**：3DNR 开的那帧更"平滑"（Laplacian 7.4）却 **PNG 更大**（3.02 MB vs 1.39 MB）；
无损压缩下平滑应当更小 ⇒ **与预期相反，机制未查**。

### 9.3 可用结论 1（**前置阻塞，硬约束**）

**§9.5 的"固定曝光 1/30 s + 手动增益 + 关 AE"未落实** ⇒ **当前板上 AE 是自动的**
⇒ **任何 ISP 参数 A/B 都会被 AE 混淆**。
⇒ 该前置是 **O62（两套 IQ）与 O40（采集期关 NR）实现的先行条件**；未落实前**不得**用 A/B 结论裁定 ISP 参数。

### 9.4 可用结论 2（**程序验证通过**）

**IQ 切换 + `rkaiq_3A.service` 重启这套操作可用**：重启后服务 `active`（新 pid）；
**恢复原 IQ 后 `sha256` 逐位一致**（`4536f96a0bd594ed803c7a611aa928105c5be8903549b9b269a0e2b4764fa6e0`，与原文件相同）。

### 9.5 **未测得**（如实登记，不得据此下结论）

- **3DNR 的降噪收益**——被 AE 混淆，**未测**；
- **拖影长度**——**画面全程静止、未录到运动**（左右两区帧差 mean 0.16 / 0.13），**未测**。
  ⇒ **O62 的裁定依据是 §3.1 v3.1 ⑧ 的既有设计 + 用户决定，不是本实验**。

### 9.6 板面收尾

两段 raw（各 466,560,000 B）、临时件、IQ 备份**均已删**；**IQ 已恢复原状**（哈希逐位一致、`enable = 1`）；
`rkaiq_3A.service` active；相机复核可用（`--stream-count=5` 正常）——**未在板上留残留**。

---

## 10. 「关 AE + 固定曝光/增益」板上落地与实测（**v3.76 追加**；用户 2026-09-19 指示「我们现在处理：关 AE + 固定曝光/增益」，方案 B-2）

> **测量条件随行**（规则 `10-evidence.md` §1）：板型 **Embedfire LubanCat-5 V2**；内核 **5.10.160 #16**；
> 节点 `/dev/video11`（`rkisp_mainpath`，NV12 1920×1080 单平面）；**sensor subdev = `/dev/v4l-subdev2`（`m00_b_imx415 1-001a`）**；
> IQ 目录 `/etc/iqfiles/`（`imx415_CMK-OT2022-PX1_IR0147-50IRC-8M-F20.json`）；`rkaiq` = **AIQ v5.0x1.3 / Calib v1.4.8**；
> 被验对象 = `tools/collect/isp_owner.c`（v3.76 新增，`-Wall -Wextra` **零告警**）。
> **权重 / 曲线档 / 精度档 / config 快照哈希：N/A**（本实验不涉模型与 config，`isp_owner` 不读写 `config.yaml`）。
> **时钟口径**：本档不含时序比对，**不涉 `mono_ns`**。

### 10.1 目的与前置事实

§9.5 要求「固定曝光 1/30 s + 手动增益 + 关 AE + AWB 锁定」，而 **§9（v3.75）已实测 AE 仍自动跑**
⇒ 本次要**真正关掉 AE**。前置事实（**本次逐项复核，全部为只读探测**）：

| 事实 | 实测 |
|---|---|
| 曝光/增益**不在** ISP 节点 | `v4l2-ctl -d /dev/video11 -l` 仅 `pixel_rate`（read-only） |
| sensor subdev 有 ctrl 但**无 AE 开关** | `/dev/v4l-subdev2`：`exposure`（4–2242，值 1350）、`analogue_gain`（0–240，值 67）、`horizontal_flip` / `vertical_flip` / `vertical_blanking` / `horizontal_blanking`（ro）/ `link_frequency` / `pixel_rate`（ro）——**无 `auto_exposure`** ⇒ AE 完全由 rkaiq 驱动 |
| AE 由谁驱动 | `rkaiq_3A.service`（`/usr/bin/rkaiq_3A_server`，pid 5749），**仅支持 `--silent`** |
| 取流者 | `/dev/video11` **无占用**（`fuser` 空）⇒ 可安全停 3A |
| subdev 归属 | `/dev/v4l-subdev2` = `m00_b_imx415 1-001a`（sensor）、`/dev/v4l-subdev3` = `rkisp-isp-subdev` |

**为何走自持进程（方案 B-2）**：rkaiq 的 uAPI2 是**进程内** API（需 `rk_aiq_sys_ctx_t*`），跨进程只有
**未公开的 socket 报文通道**。核查结论：`SocketServer` / `MessageParser` / `ProcessCommand` 均为
`librkaiq.so` 的**本地符号**（`t`），`MessageParser.hpp` **板上未安装**，`__RkAiqSocketPacket_s`
**不在 DWARF**（`.debug_info` 只覆盖部分 TU；`librkaiq.a` **无 debug 段**），魔数仅能从 `.rodata`
`0x194fd6`（`"RK"`）/ `0x194fd8` 读出 ⇒ **须手工逆向报文结构与哈希**，**已否决**。

### 10.2 调用序的来源（**板上反汇编还原**，非猜测）

`/usr/bin/rkaiq_3A_server` 是薄封装：`main()` 只做「探测 camera → 每路起线程」；真正的 ISP 建立序在
`engine_thread(void*)`（`0x1964`），**逐条核对**为：

```
open(media_info+0x40, O_RDWR)                                    # rkisp-isp-subdev 节点
rk_aiq_uapi2_sysctl_init(sns_ent_name, "/etc/iqfiles/", NULL, NULL)
rk_aiq_uapi2_sysctl_prepare(ctx, width, height, RK_AIQ_WORKING_MODE_NORMAL)   # w/h 取自 media 探测结果
ioctl(fd, VIDIOC_SUBSCRIBE_EVENT /*0x4020565a*/, {type=0x08000001})   # STREAM_START
ioctl(fd, VIDIOC_SUBSCRIBE_EVENT,               {type=0x08000002})   # STREAM_STOP
loop { rk_aiq_uapi2_sysctl_start(ctx); wait_stream_event(START); wait_stream_event(STOP); rk_aiq_uapi2_sysctl_stop(ctx,false); }
```

⇒ **厂商同样是在流未启动时先调 `sysctl_start`**（`start` 在 `wait_stream_event` **之前**），
故本工具**不订阅事件**、`start` 后即固定参数并常驻（单次采集会话，无需事件驱动的 start/stop 循环）。
`getBindedSnsEntNmByVd(video_node)` 用于替代厂商的 media 拓扑解析来取得 sensor 实体名。

### 10.3 结果：**「关 AE + 固定曝光/增益」成立**

**(a) 踩坑与正解（本次最关键发现）**

| 做法 | 实测结果 |
|---|---|
| `setExpMode(OP_MANUAL)` 后再设范围 | **失控**——5 点扫描（请求 5000/15000/33333 µs × gain 2/4/16）回读**恒为 `exposure=405 / analogue_gain=0`**，**与请求值完全无关**（AEC 改用 IQ 的 `stManual` 默认值） |
| **只把 auto 范围钉成一点**（`setExpTimeRange` / `setExpGainRange` 传 `min == max`，**不切模式**） | **可控**——见下表，**单调、线性、可复现** |

⇒ **`rk_aiq_user_api2_imgproc.h` 的注释即正解**：「auto 模式 : gain 在 `[min,max]` 之间自动调整；
**manual 模式 : `gain->min == gain->max`**」——**钉死范围本身即 manual 语义**。
（三个 API 的实现均为 `getExpSwAttr → 改 → setExpSwAttr`，**已反汇编核对**。）

**(b) 取值标定（`isp_owner` 请求 → `/dev/v4l-subdev2` 回读）**

| 请求 `--exp-us` | 请求 `--gain` | 回读 `exposure` | 回读 `analogue_gain` |
|---|---|---|---|
| 5000 | 4.0 | **675** | 40 |
| 15000 | 4.0 | **1350** | 40 |
| 33333 | 4.0 | **1350（被钳制）** | 40 |
| 33333 | 1.0 | 1350 | **0** |
| 33333 | 16.0 | 1350 | **80** |

- **曝光**：`exposure(lines) = exp_us × 0.09` ⇒ **单位 = 秒**（`getExpTimeRange` 返回 `max=0.010000 / min=0.000059`）。
- **增益**：`analogue_gain(reg) = 20·log2(gain)/0.3`（**0.3 dB 步进**；gain 1.0→0、4.0→40、16.0→80）
  ⇒ **API 单位 = 线性倍数**（`getExpGainRange` 返回 `max=256.0 / min=1.0`）。
- **⚠️ 曝光上限（→ 新登记 O63）**：请求 33333 µs（= 1/30 s）**被钳制到 1350 lines ≈ 15 ms**
  = 当前 **1080p@60 的帧周期量级** ⇒ **§9.5 的「固定曝光 1/30 s」在 60 fps 下物理不可达**，
  须待 §11 M1 出口标准 #1「暗光 1080p@30 稳定」落实后方可达。

**(c) 固定后的稳定性（主要判据）**

- **取流中回读恒定**：60 帧取流期间 4 次采样 `exposure=1350 / analogue_gain=40`，**完全一致**。
- **帧亮度稳定段极稳**（`--exp-us 15000 --gain 4.0`，60 帧 NV12 的 Y 平面均值）：

```
f00-03 : 12.40 12.90 12.37 11.87      ← 起始 4 帧极暗（ISP warm-up）
f04-05 : 53.61 52.91                  ← 过冲
f06-17 : 52.06 … 47.52                ← 单调衰减
f18-29 : 46.23 … 45.80
f30-59 : 45.79 45.79 45.78 … 45.79    ← 稳定段：45.78 ± 0.01（±0.02%）
```

- **SIGTERM 退出干净**：`sysctl_stop done; deinit`，无残留、无 D 态进程。

**(d) 对照腿（AE auto，厂商 3A）——用于判断瞬态归属**

| 腿 | 条件 | 起始 4 帧 | 收敛过程 | 末段（f54-59） | 回读 |
|---|---|---|---|---|---|
| **固定档** | `isp_owner --exp-us 15000 --gain 4.0` | **12.4 → 11.9**（极暗） | f04 过冲 53.6 → 单调衰减 | **45.79**（±0.02%） | 1350 / 40 |
| **AE auto** | 厂商 `rkaiq_3A.service` | **12.97 → 11.93**（极暗） | f04 起**单调上爬** 15 → 103.5 | **103.5** | 1350 / **93** |

⇒ **两条可用结论**：
1. **起始 4 帧极暗是固有的 ISP warm-up**（两腿**都有**，量级一致）⇒ **非本实现引入**；
   **采集必须丢弃起始帧**（实测需 ~20–30 帧才收敛，具体丢弃数由 M1 采集流程定）。
2. **AE 每次启动都从零重新收敛**——重启后 ~30 帧收敛到 luma 103.5 / **gain 93**，而改动前长期运行
   实例稳态为 **gain 67** ⇒ **AE 的收敛值不可复现**，这正是 §9.5 要固定它的理由。
   **固定档则恒等于指定值**（1350 / 40）。

**(e) 如实登记的「现象 + 待查」（不得写成结论）**

- **固定档 f04 过冲后约 30 帧衰减收敛**（53.6 → 45.78）：**衰减的因果未定**——曝光/增益已钉死且回读恒定，
  故**不是**曝光/增益在变；候选（**均未验证**）= ISP 侧自适应块（如 DRC/ADRC）或模拟/数字增益的**内部分配**
  在收敛。**本次无对照实验** ⇒ 按纪律**只记现象 + 待查**。
- **`getExpTimeRange` / `getExpGainRange` 的回读不反映设定值**：设定 `0.015 s / 4.0×` 后，二者仍返回
  `0.010000 / 0.000059` 与 `256.0 / 1.0` ⇒ **效度检查必须用 sensor subdev 回读**
  （`v4l2-ctl -d /dev/v4l-subdev2 --get-ctrl`），**不得依赖 rkaiq 的 getter**。
- **AWB 锁定**（`rk_aiq_user_api2_awb_Lock`）返回 `rc=0`（API 接受），但**未验证其效果**
  ⇒ **只记"已调用成功"，不记"已生效"**。（**v3.76 追加：验证尝试与"无法判定"的结论见 §10.6**。）

### 10.4 副作用与恢复步骤（规则 `30-board-safety.md` §4）

| 项 | 内容 |
|---|---|
| **影响面** | 停 `rkaiq_3A.service` ⇒ 采集期 3A 由 `isp_owner` 持有；**演示链路（main.py）与任何取流者在此期间失去 3A**（无 AE/AWB、ISP 参数不再更新）。执行前已确认 **`/dev/video11` 无占用** |
| **恢复步骤** | `sudo systemctl start rkaiq_3A.service`（或重启板子）；若 `isp_owner` 挂住，`sudo pkill -9 -f isp_owner` 后再启服务 |
| **实测恢复** | 本次实验后 **3A 服务已恢复 active**（`exposure=1350 / analogue_gain=93`，AE auto 生效） |
| **已知坑** | ① **须以 root 运行**——`librkaiq` 的 `init` 会 `fopen("/tmp/aiq%d.lock","w+")` 后**不检查 NULL** 就 `fileno()`；该文件属 `root:644` ⇒ 以普通用户跑会 **`fileno(NULL)` 段错误**（librkaiq 的缺陷，`rk_aiq_uapi_sysctl_init+0xa4c`）。② **`--probe` 路径**下（**从未取流**）`sysctl_stop` 会**阻塞**，故 `--probe` 已改为打印后直接退出。 |
| **板上收尾** | 两段 raw（各 124 MB / 62 MB）+ 临时件**均已删**；`isp_owner` 进程已清；3A 服务 active；`df` = 11% |

### 10.5 未验项（如实登记）

1. **AWB 锁定的实际效果**——**未验**（**本次已做验证尝试，结论 = 当前场景无判别力，详见 §10.6**）。
2. **固定档 f04→f30 衰减的因果**——**未验**（缺对照实验）。
3. **1/30 s 在 30 fps 档下的可达性**——**未验**（当前 60 fps 无法达到；→ O63）。
4. **与 `capture_app.py` 的集成**（采集流程中自动停/起 3A 服务）——**未做**；本次为手工分步验证。
5. **长时（>10 min）固定档稳定性**——**未验**（本次单轮 60 帧）。

### 10.6 AWB 锁定（`--awb-lock`）的验证尝试与结论（**v3.76 追加；结论 = 无法判定，登记为 O64**）

> **用户 2026-09-19 指示「先验证 `--awb-lock` 的效果」**。本节记录**尝试过程与为何得不出结论**，
> 以及本次**顺带取得的两条可用事实**。**不作"已生效"或"无效"的断言**（规则 `10-evidence.md` §2）。

**(a) 结构证据（`awb_Lock` 不是空壳）**

板上反汇编 `/usr/lib/librkaiq.so`：

```
rk_aiq_user_api2_awb_Lock        (0x7784c)
  └─ RkCam::RkAiqAwbHandleInt::lock()        (0x8ef68)
       └─ rk_aiq_uapiV2_awb_Lock(_RkAiqAlgoContext*)   (0x112fe8)
```

⇒ 走的是 **algo 级专用 lock 方法**（不是"切 manual + 写增益"的包装）。实测调用 `rc=0`，
且调用前后 `getWBMode` **恒为 0（OP_AUTO）** ⇒ **lock 不改变 WB 工作模式**。

**(b) 先验证可观测量是否有效（**这一步是必要的，否则结论不可信**）**

观测口径 = `rk_aiq_user_api2_awb_QueryWBInfo` 的 `gain`（"实际生效增益"）+ `cctGloabl` + `awbConverged`。
**交叉验证（两条独立旁证）**：

| 旁证 | 结果 | 说明 |
|---|---|---|
| `stat_gain_glb`（统计增益）随时间 | 取流前 **`0.0000`**（LP 滤波未起）→ 取流后收敛到与 `gain` **同值** | 该字段是**实时计算**的，非静态预设 |
| IQ 的 manual 预设（`GetMwbAttrib`，`mode=2` WBGAIN） | `r=1.3270 gr=1.0000 gb=1.0000 b=3.3143` | 与实时生效值 `r=1.9086 b=1.9986` **明确不同** ⇒ 读数确实反映实时值 |

⇒ **可观测量有效**：`QueryWBInfo.gain` 是实时生效值。

**(c) 但当前场景没有判别力（结论无法得出）**

| 观测 | 实测 |
|---|---|
| 实时增益随时间（取流中，每 250 ms 采样） | t+0.25 s（`LV=0`、`converged=0`）→ 收敛后（`LV=54`/`57`、`converged=1`）：**`r=1.9086 gr=1.0000 gb=1.0000 b=1.9986`、`CCT=5621.5`、`CCRI=0.212` 逐位不变** |
| 实时增益随**总增益档**（10× vs 16×） | **逐位相同**（仅 `LV` 变：57 vs 54） |
| 图像域 UV 通道（120 帧，gain 16×） | `UV mean 133.0`、spread 仅 **6.39**，且为**单调微移**（首 5 帧 128.8→135.2、末 5 帧 132.38→132.36），**无 AWB 主动修正的痕迹** |

⇒ **该环境下 AWB 的增益本就已处于收敛值且从不移动**（`awbConverged` 0→1 的翻转**并未伴随任何增益变化**）
⇒ **锁与不锁的读数完全一致** ⇒ **`--awb-lock` 既未被证实、也未被否证**。

**(d) 为何不能用软件构造扰动（本路已排除）**

板上反汇编 `rk_aiq_uapi2_setMWBGain`（0x77ee0）：其实现为
`rk_aiq_user_api2_awb_SetWpModeAttrib(...)` + `rk_aiq_user_api2_awb_SetMwbAttrib(...)`
⇒ **注入 MWB 增益必然先把 WB 切到 manual 模式**；而 manual 模式下 AWB 算法**不运行**
⇒ **无法构造"让 AWB 持续追着变"的软件输入**。`setMWBScene` 同属 `RK_AIQ_MWB_MODE_SCENE`（同为 manual 路径）。

⇒ **要判定 lock 是否生效，需要一个能改变 AWB 输入的扰动**，两条可行路径：
**(i) 物理扰动**（推荐）——板前改变光源色温（开/关一盏暖色台灯，或用暖/冷色物体遮部分画面）；
**这正是 M1 采集范式（§4.5「固定 ISP 参数与曝光，只改光源亮度」）会遇到的真实情形**——
采集时**调亮/调暗光源会同时改变色温**，故本项对 M1 采集**有实际影响**；
**(ii) 软件注入**——先证明"注入错值后 AWB 会拉回"（证明可观测且 AWB 在跑），再做 lock 对照；
**风险**：需先验 `setWBMode` 是否会清除 lock 状态（`lock` 与 `op_mode` 在 rkaiq 中是两套枚举，**预期正交但未验证**）。

**(e) 顺带取得的一条可用事实（与 AWB 无关，但强化 §9.5 的必要性）**

**AE 的收敛值每次启动都不同**——本轮三次读数：`1350/67` → `1350/93` → `1350/126`
（同一场景、同一 IQ，仅重启 `rkaiq_3A.service`）⇒ 再次印证 §9.5「固定曝光 + 手动增益」的必要性。

**(f) 板面收尾**

本次仅只读观测 + 多次 `isp_owner` 起停；**未改 IQ、未改 config**。收尾后
`rkaiq_3A.service` **active**、AE 已收敛（`1350/126`）、临时 raw/log **均已删**、`isp_owner` 进程已清、`df` = 11%。

**(g) 复现命令（供后续做 (i)/(ii) 扰动时使用）**

```bash
# 起固定档 + AWB 观测（每 250 ms 打印一次生效增益 / CCT / converged / stat / mwb）
sudo systemctl stop rkaiq_3A.service
sudo nohup /home/cat/starlight/tools/collect/isp_owner /dev/video11 \
     --exp-us 15000 --gain 10.0 --wb-watch --awb-lock --awb-lock-delay 5 > /tmp/wb.log 2>&1 &
sleep 5
# 取流（AWB 只在有帧时更新；锁在 READY 后 5 s 施加）
sudo v4l2-ctl -d /dev/video11 --set-fmt-video=width=1920,height=1080,pixelformat=NV12 \
     --stream-mmap --stream-count=600 --stream-to=/tmp/wb.raw
# 扰动（(i) 物理：此刻改变光源色温；(ii) 软件：注入 MWB 错值）
sudo pkill -TERM -f isp_owner && sudo systemctl start rkaiq_3A.service
```
**判据**：扰动后若生效增益**随之改变** ⇒ 未锁住；**保持不动** ⇒ 锁住。

---

## 10.7 纯 IQ 路径：`AecOpType` 手动档的板上验证（**v3.77 追加**；用户 2026-09-19 同意「先验证纯 IQ 路径 + 查厂家资料」）

> **测量条件随行**（规则 `10-evidence.md` §1）：板型 **Embedfire LubanCat-5 V2**；内核 **5.10.160**；
> 节点 `/dev/video11`（NV12 1920×1080 单平面）；**sensor subdev = `/dev/v4l-subdev2`（`m00_b_imx415 1-001a`）**；
> IQ = `/etc/iqfiles/imx415_CMK-OT2022-PX1_IR0147-50IRC-8M-F20.json`（原哈希 `4536f96a…`）。
> **被验对象 = IQ 文件的 `scene_isp30/ae_calib/CommCtrl/` 段**（**不是** `isp_owner.c`）。
> **权重 / 曲线档 / 精度档 / config 快照哈希：N/A**（不涉模型与 config）。**时钟口径**：不含时序比对，**不涉 `mono_ns`**。

### 10.7.1 目的

v3.76 用**自持进程**（§10.1~§10.5）关 AE。演示链路（M2）若照搬，须**替换 `rkaiq_3A.service`**（触及 §10.6 的单 unit 交付）
且**多一个进程依赖**（它挂了 ISP 就挂）⇒ 对无人值守的演示风险偏高。本实验验证一条**更轻的路径**：
**IQ 文件里本就有 AEC 工作模式开关与手动档** ⇒ **改 IQ 即可让 `rkaiq_3A_server` 启动即固定**，**不动服务、零新增进程**。

### 10.7.2 厂家 schema（一手资料 = 板上头文件；**板上无厂家 PDF**，已核查 `/usr/share/doc` 与库内无厂商文档）

| 项 | 取值 | 出处 |
|---|---|---|
| `AecOpType` | 枚举 `RKAiqOPMode_t`，默认 `RK_AIQ_OP_MODE_AUTO`；**`RK_AIQ_OP_MODE_MANUAL = 2`** | `aec_head.h` 的 `M4_ENUM_DESC`；`rk_aiq_comm.h` |
| `TimeValue` | `f32`，range [0,1]，默认 0.003，**单位 = 秒** | `aec_uapi_head.h` / `aec_head.h` 的 `M4_NUMBER_DESC` 与 `// unit: s` |
| `GainValue` / `IspDGainValue` | `f32`，range [1,4096]，默认 1，**线性倍数** | 同上 |
| 三个使能位 | `AecManualCtrl/LinearAE/{ManualTimeEn, ManualGainEn, ManualIspDgainEn}` **本来就 = 1** | IQ 实测回读 |

⇒ **缺的只是 `AecOpType`**。另：`AecManualCtrl` 含**三个增益级**（sensor 模拟 `GainValue` + ISP 数字 `IspDGainValue`），要完全钉死须三个都设。

### 10.7.3 方法（**含备份与还原**；规则 `30-board-safety.md` §4）

**影响面**：改动 `/etc/iqfiles/` 下 IQ + 重启 `rkaiq_3A.service` ⇒ 该期间**板上任何取流者的 ISP 行为改变**。
**恢复步骤**：`sudo cp /tmp/iq_backup.json $F && sudo systemctl restart rkaiq_3A.service`（本实验**已执行**，见 §10.7.6-5）。

```bash
F=/etc/iqfiles/imx415_CMK-OT2022-PX1_IR0147-50IRC-8M-F20.json
sudo cp $F /tmp/iq_backup.json && sha256sum $F          # 备份 + 记哈希
python3 - <<'PY'                                         # 改副本
import json
d=json.load(open("/tmp/iq_backup.json"))
c=d["main_scene"][0]["sub_scene"][0]["scene_isp30"]["ae_calib"]["CommCtrl"]
c["AecOpType"]="RK_AIQ_OP_MODE_MANUAL"
lin=c["AecManualCtrl"]["LinearAE"]
lin["TimeValue"]=0.015; lin["GainValue"]=4.0; lin["IspDGainValue"]=1.0
json.dump(d,open("/tmp/iq_manual.json","w"),indent=2)
PY
sudo cp /tmp/iq_manual.json $F && sudo systemctl restart rkaiq_3A.service && sleep 6
sudo v4l2-ctl -d /dev/video11 --set-fmt-video=width=1920,height=1080,pixelformat=NV12 \
     --stream-mmap --stream-count=200 --stream-to=/dev/null
sudo v4l2-ctl -d /dev/v4l-subdev2 --get-ctrl exposure,analogue_gain   # 回读
# 还原：sudo cp /tmp/iq_backup.json $F && sudo systemctl restart rkaiq_3A.service
```

### 10.7.4 结果

| 设置 | 回读 `exposure` | 回读 `analogue_gain` |
|---|---|---|
| `AecOpType=MANUAL`, `TimeValue=0.015`, `GainValue=4.0`（第 1 轮） | **2025** | **40** |
| 同上，**重启服务后第 2 轮** | **2025**（逐位一致） | **40** |
| `TimeValue=0.033333`（1/30 s） | **2224**（被 sensor max 2242 钳制） | 40 |
| 还原 IQ 后（AE auto） | 1350 | 132（**另一轮 139**） |

⇒ **纯 IQ 路径成立**：改 `AecOpType` + 手动值 ⇒ **启动即固定**，**重启后两轮逐位一致**，**不动服务、零新增进程**。
`GainValue = 4.0 → analogue_gain = 40` 与 §10.3 独立测得的映射**完全吻合**（旁证）。
**对照**：AE auto 在本档累计 **5 次读数** `gain = 67 / 93 / 126 / 132 / 139`，**每次不同** ⇒ 再次印证 §9.5 固定它的必要性。

### 10.7.5 行时间标定与曝光上限真值（**更正 v3.76 的两处数值**）

| 量 | 真值 | 依据 |
|---|---|---|
| **行时** | **7.41 µs**（行率 **135,000 lines/s**） | 两组独立测点吻合：`5000 µs → 675 lines`（§10.3 钉范围档）、`15000 µs → 2025 lines`（本档 manual）；自洽校验 VMAX = 1080 + 1154 = **2234 lines** × 7.41 µs = **16.55 ms ≈ 60.4 fps** |
| **AE auto** 曝光上限 | **1350 lines = 10.0 ms** | = `getExpTimeRange.max = 0.010000`（§10.3）⇒ 是 **AE 策略自身**的上限 |
| **manual** 曝光上限 | **2242 lines ≈ 16.6 ms** | = sensor `exposure` 的 max；受 **sensor VMAX** 限制（60 fps 帧长 2234 lines） |
| 旧映射 `lines = exp_us × 0.09` | ❌ 应为 **`lines = exp_us / 7.41`** | 同上 |

**成因更正**：v3.76 写的「上限 = 1350 lines ≈ 15 ms（60 fps 帧周期）」**错**——1350 lines = **10 ms**，且是 **AE auto 的策略上限**（**不是** 60 fps 钳制）；**manual 档没有这个限制**，可达 sensor VMAX ≈ **16.6 ms**（比 AE auto **多 66% 进光量**）。
⇒ **O63 结论仍成立**（1/30 s = 33.3 ms 在 60 fps 下不可达，实测被钳到 **2224**），但**数值与成因已更正**（§9.5 ④ / §15 O63）。

### 10.7.6 未验项与边界（如实登记）

1. **落地未做**——本实验只验证**机制**；**两套 IQ 的建置、换档动作固化、防呆校验**均未实施（登记 O62 ④(ii)/⑤ 与 **O65**）。
2. **`rkaiq_3A_server` 的 IQ 目录不可指定**（板上实测：**硬编码 `/etc/iqfiles/`**，unit 走 `/etc/init.d/rkaiq_3A.sh`、**脚本无参数透传**，程序 `strings` 仅见 `--silent`）⇒ **两套 IQ 必须换文件 + 重启 3A**；**换档重启对演示的扰动未验**。
3. **manual 档下 ISP 内其它自适应块是否仍收敛**未验（§10 已记「固定档 f04→f30 约 30 帧衰减、**因果未定**」，本档**未复测**）。
4. **固定曝光/增益的取值未定**（须按演示实际照度区间选取，M1 定稿）——本档的 `0.015` / `4.0` 只作**测点**，**不是取值**（规则 `10-evidence.md` §2：不得把测点当取值）。
5. **收尾实测**：IQ 哈希已**逐位还原**为 `4536f96a0bd594ed803c7a611aa928105c5be8903549b9b269a0e2b4764fa6e0`；`rkaiq_3A.service` = **active**；`AecOpType` = `RK_AIQ_OP_MODE_AUTO`（`TimeValue=0.01` / `GainValue=1`，即原值）；**板上无残留**。

---

## 11. IQ 两套档位工具（`iq_sets.py`）+ 采集侧防呆的板上实测（**v3.78 追加**）

> **测量条件随行**（规则 `10-evidence.md` §1）：板型 **Embedfire LubanCat-5 V2**；内核 **5.10.160**；Python **3.9.2**；
> 节点 `/dev/video11`（NV12 1920×1080）；sensor subdev = `/dev/v4l-subdev2`（`m00_b_imx415 1-001a`）；
> IQ = `/etc/iqfiles/imx415_CMK-OT2022-PX1_IR0147-50IRC-8M-F20.json`（**原哈希 `4536f96a…`**）。
> **被验对象** = `tools/collect/iq_sets.py`（v3.78 新增）与 `tools/collect/capture_app.py` 的档位防呆。
> **权重 / 曲线档 / 精度档 / config 快照哈希：N/A**（不涉模型与 config）。**时钟口径**：不含时序比对，**不涉 `mono_ns`**。

### 11.1 目的

v3.78 把 O62（两套 IQ + 防呆）与 O65（演示侧载体）实现为工具；**开发机合成 IQ 自测已过**，
但**板上未验**（v3.78 ⑦）。本节补齐：在**真实 IQ** 上建基线、建两档、验防呆、**跑通 `switch` 的完整闭环**。

### 11.2 影响面与恢复（规则 `30-board-safety.md` §4）

| 步骤 | 副作用 | 恢复 |
|---|---|---|
| `import-stock` / `build` / `status` | **无**（只读 `/etc/iqfiles/`、只写新建 `/etc/iqfiles_sets/`） | 无需 |
| `switch {collect\|demo\|stock}` | **改 `/etc/iqfiles/` + 重启 `rkaiq_3A.service`** ⇒ 期间任何取流者的 ISP 行为改变 | `switch stock` 一键还原（实测 sha256 逐位一致） |

**执行前已确认**：`/dev/video11` 无取流者；3A active；`/etc/iqfiles_sets` 不存在（首次建）。

### 11.3 命令与结果

```bash
scp tools/collect/iq_sets.py board:~/starlight/tools/collect/
sudo python3 tools/collect/iq_sets.py import-stock
sudo python3 tools/collect/iq_sets.py build --exp-us 15000 --gain 4.0   # 测点值，非定稿值
python3 tools/collect/iq_sets.py status
sudo python3 tools/collect/iq_sets.py switch collect
sudo v4l2-ctl -d /dev/video11 --set-fmt-video=width=1920,height=1080,pixelformat=NV12 \
     --stream-mmap --stream-count=200 --stream-to=/dev/null
sudo v4l2-ctl -d /dev/v4l-subdev2 --get-ctrl exposure,analogue_gain
sudo python3 tools/collect/iq_sets.py switch stock                      # 还原
```

| # | 项 | 实测结果 |
|---|---|---|
| 1 | `import-stock` | 落 `/etc/iqfiles_sets/stock/…`，sha256 = **`4536f96a…`**（= 当前 `/etc/iqfiles/` 原值，**一致**） |
| 2 | `build --exp-us 15000 --gain 4.0` | **collect** sha256 = `d019158a…`，`nr3d=[0]`、`AecOpType=[RK_AIQ_OP_MODE_MANUAL]`、`TimeValue=[0.015]`、`GainValue=[4.0]`；**demo** sha256 = `fc1da72f…`，`nr3d=[1]`，其余同上 |
| 3 | 真实 IQ 内容核对 | stock = `AUTO` / 0.01 / 1 / **bayertnr=1**；collect = `MANUAL` / 0.015 / 4.0 / **bayertnr=0**；demo = `MANUAL` / 0.015 / 4.0 / **bayertnr=1**（`bayer2dnr_v2` 两档均保持 1，符合 O40 只关 3DNR） |
| 4 | `status`（未换档） | `set: stock`（rc=0）——**正确识别** |
| 5 | **防呆（对真实 manifest）** | 当前 = stock ⇒ `iq_guard` **False / blocked**；`do_capture` 返回 **拒采**文案；`bypass=True` ⇒ `bypassed`（**留痕**）；sidecar 落 `{iq_file, iq_hash, iq_set, iq_guard}` **正确** |
| 6 | `switch collect` | 换文件 + 重启 3A + **服务 active 校验通过** + 回读 sha256 = `d019158a…`（一致） |
| 7 | **取流 + subdev 回读** | **`exposure = 2025` / `analogue_gain = 40`**，取流 **60.00 fps** ⇒ 与 §10.7 的映射**完全吻合**（`TimeValue=0.015 s` → 2025 lines；`GainValue=4.0` → reg 40）**在真实 IQ 上复现** |
| 8 | `switch stock`（还原） | sha256 回到 **`4536f96a…`**、服务 active、回读一致 |
| 9 | `switch <同档>` 幂等 | `当前已是 demo 档（sha256 一致），无需换档` ✓ |
| 10 | **收尾** | 3A = **active**；`/etc/iqfiles/` = `4536f96a…`（**逐位还原**）；`status` = stock；`/etc/iqfiles/` 内 **`.prev` 数 = 0**；**无取流者** |

⇒ **O62 的实现（两套 IQ + 换档 + 防呆 + 指纹）在板上闭环通过**；`/etc/iqfiles/` 收尾**逐位还原**。

### 11.4 实测暴露的两个缺陷（**实现侧，已修复**）

| # | 缺陷 | 现象 | 修复 |
|---|---|---|---|
| 1 | **备份落在 IQ 加载目录** | 旧版把备份写成 `/etc/iqfiles/<name>.json.prev` ⇒ **污染 `/etc/iqfiles/`**（该目录由 `rkaiq_3A_server` 扫描） | 备份改到 `sets_dir/.prev/`；并**主动清理**历史误落的 `<name>.json.prev`（已实测：目录内 `.prev` 数 = 0） |
| 2 | **`switch` 误报"已重启 ✓"** | `systemctl restart` **返回 0 但 `rkaiq_3A_server` 随后自行退出** ⇒ 服务实际 inactive，而旧版只查返回码 | 新增 `_is_active()` 轮询（≤10 s）**校验服务真的起来**；未起来 ⇒ **大声报错 + 给恢复命令 + 返回非零**。另加**取流占用前置告警**（`fuser`） |

### 11.5 未定根因的现象（**登记 §12 R21**，不写成结论）

**现象**：一次 `switch`（由 stock 换 demo）中，`systemctl restart rkaiq_3A.service` 返回 0、`ExecStart` 亦返回 0，
但 `rkaiq_3A_server` **启动后随即退出**（`journalctl` 止于 `MessageParser process loop exit!`，`ExecStop` 报"未找到进程"）
⇒ 服务 inactive。**同期日志**只有 `ERR: Bad media topology for: /dev/media2…15`（**属常态噪声**，其它次启动同样出现）。

**已排除**：与 IQ 内容无关（同一份文件在另一次重启中正常生效——§11.3 第 6/7 项）。
**未验证（不得写成结论）**：与"**紧接上一次重启**"或"**刚结束取流**"是否相关（**本次 n=1，无对照**）。
⇒ **登记为 R21**；**缓解 = 工具已校验 `is-active` 并大声报错**（不静默）；根治待查。

### 11.6 附带发现：`~/starlight/bisect.py` 遮蔽 stdlib（**非本项目引入，只记现象**）

板上 `/home/cat/starlight/bisect.py` 是**项目自带的基准脚本**，其文件名与 stdlib `bisect` **同名**。

- **可复现事实**：`cd /home/cat/starlight && python3 -c "import http.server"` ⇒
  `File "/home/cat/starlight/bisect.py", line 5 … IndexError`（`import random` → `from bisect import …` 被遮蔽）。
- **边界（已核）**：**以脚本方式运行不受影响**（`python3 tools/collect/capture_app.py` 的 `sys.path[0]` 是**脚本所在目录** `tools/collect/`，该目录内无同名文件）；`tools/collect/` 下仅 `capture_app.py` / `iq_sets.py`，**无 stdlib 同名**。
- **交付链路**：`main.py` 及其全部本地模块（`config` / `log` / `capture` / `curve_estim` / `display_mipi` / `osd` / `realtime_apply` / `ui_buttons`）**均不 import** `bisect` / `random` / `http` / `statistics` / `secrets` / `uuid`（已 grep 核实）⇒ **未发现当前受影响路径**。
- **结论口径**：**只记现象**；**不修改该文件**（非本次范围），若后续出现 `IndexError: list index out of range` 的怪异报错，**优先怀疑此项**。

### 11.7 本节遗留（如实登记）

1. **`switch` 的重启偶发失败根因未定**（§11.5；已登记 R21）——**已用 `is-active` 校验兜住**，但未消除。
2. **两套 IQ 的取值仍是测点**（`15000 µs` / `4.0×`）——**最终取值 = M1 定稿项**（§15 O63：1/30 s 须待 1080p@30 锁定）⇒ 定稿后须**重跑 `build`** 并更新 manifest。
3. **演示链路（M2）未接线**——本次只验工具与采集侧防呆；**演示侧启动指纹打印**未做（O65 残留）。
4. **`capture_app.py` 的端到端采集未在本节复测**（防呆与 sidecar 为**单元级**验证；v3.71 已验过落盘链路）。

---

## 12. 1080p@30 锁定（**v3.79 板上实测**）

> **测量条件随行**（规则 `10-evidence.md` §1）：板型 **Embedfire LubanCat-5 V2**；内核 **5.10.160 #16**；
> 节点 `/dev/video11`（`rkisp_mainpath`，NV12 1920×1080 单平面）+ **sensor subdev `/dev/v4l-subdev2`（`m00_b_imx415 1-001a`）**；
> IQ 两档 sha256 = **collect `7e1ec9da70ee05e3e612d06cac08356d5c45ebbb2d84064524a883a4bbadae49`**、
> **demo `968a636db46d5d53799776bd9baf990c52e62f30b43020bff56d96c334cf6aa0`**（均 `TimeValue=0.033333` / `GainValue=4.0` /
> `(isFpsFix,FpsValue)=(1,30)`）；**权重 / 曲线档 / 精度档：N/A**（本实验不涉模型）；
> **config 快照哈希 = `a0887d2235f7730d` 前缀**（**完整哈希未记录**）。
> **时钟口径**：本档不含函数级时序比对，**不涉 `mono_ns`**；帧率计时口径 = **`v4l2-ctl --stream-mmap`**（见 §12.6 反例）。

### 12.1 目的

为 **§11 M1 出口标准 #1「暗光 1080p@30 稳定」** 与 **O63（§9.5 的「固定曝光 1/30 s」可达性）**
建立可复现依据：确认 IMX415 在 1080p 下**能锁到 30 fps**，且 30 fps 下 **1/30 s 曝光可达**。

### 12.2 前置事实（只读探测）

| 事实 | 实测 |
|---|---|
| 设备树 overlay | `/boot/dtb/overlay/` 下 imx415 **只有** `1920x1080-60fps` 与 `3840x2160-30fps/15fps`，**无 1080p@30** |
| 当前生效 cam overlay | `rk3588-lubancat-5-cam0-imx415-1920x1080-60fps-overlay.dtbo`（`uEnvLubanCat5-V2.txt` 第 63 行），其 `fragment@1` 置 `rockchip,imx415-capture-mode = 1`（**4K30 overlay 不置该属性**） |
| `/dev/video11` 帧率口 | **不枚举任何帧率**；`VIDIOC_G_PARM` 返回 **EINVAL**（**无 set-parm 口**） |
| 唯一可用口 | **sensor subdev 的 `V4L2_CID_VBLANK`（rw）**：min **1154** / max **31671** / default **1154** |
| subdev 判别 | 板上 5 个 v4l-subdev 中**只有它**暴露该控制（`v4l-subdev0` rockchip-mipi-csi2 / `1` rockchip-csi2-dphy0 / `3` rkisp-isp-subdev / `4` rkcif-mipi-lvds **均为 0 个**）⇒ 可作**语义判据**运行时解析，**不写死编号** |

### 12.3 方法与复现命令

```bash
# (0) 只读：运行时解析 sensor subdev + 回读当前 vblank
python3 sensor_lock.py --check

# (1) 施加 30 fps 锁定（须在 STREAMON 之前；写入需 root）
sudo v4l2-ctl -d /dev/v4l-subdev2 -c vertical_blanking=3405
#     等价：sudo python3 sensor_lock.py --apply 3405      # 写入 + 回读校验

# (2) 两点法测帧率（**计时口径以 v4l2-ctl 为准**，勿用 gst，见 §12.6）
for N in 600 1200; do
  t0=$(date +%s.%N)
  v4l2-ctl -d /dev/video11 --set-fmt-video=width=1920,height=1080,pixelformat=NV12 \
      --stream-mmap --stream-count=$N --stream-to=/dev/null
  t1=$(date +%s.%N)
  echo "N=$N  elapsed=$(echo "$t1 - $t0" | bc)"
done

# (3) 效度检查（只认 sensor subdev 回读）
sudo v4l2-ctl -d /dev/v4l-subdev2 --get-ctrl vertical_blanking,exposure

# (4) 两套 IQ 以 30 fps 档重建 + 换到采集档（`--fps` 为 v3.79 新增必填）
sudo python3 tools/collect/iq_sets.py build --exp-us 33333 --gain 4.0 --fps 30
sudo python3 tools/collect/iq_sets.py switch collect

# (5) TH1 自检（板上；`/dev/v4l-subdev2` 属 `root:video` 且用户 cat 在 video 组 ⇒ 写不需 root）
python3 capture.py

# (6) 采集工具端到端（启动时 lock_guard() 自动施加 + 回读校验；fail-closed）
python3 tools/collect/capture_app.py --outdir /tmp/cap30 --port 8093
```

### 12.4 结果（实测）

**(a) 帧周期锁定（测点 → VMAX → exposure max → fps）**

| 测点 | `vertical_blanking` | VMAX（= 1080 + vblank） | `exposure max` 回读 | 帧率 |
|---|---|---|---|---|
| 默认（1080p@60 overlay） | 1154 | 2234 | **2242** | 60 fps |
| 中间测点 | 3418 | 4498 | **4506** | — |
| **30 fps 锁定** | **3405** | **4485** | **4493** | **30.0 fps**（两点法 29.99） |

**(b) 帧率测量**

| 方法 | 结果 |
|---|---|
| 两点法（`v4l2-ctl --stream-mmap`） | 600 帧 20.234 s、1200 帧 40.242 s ⇒ Δ=20.007 s/600 帧 ⇒ **29.99 fps** |
| 单点（600 帧） | 20.045 s ⇒ **29.93 fps** |
| 理论 | VMAX 4485 行 × 7.433 µs = 33.33 ms = **30.0 fps** |

**(c) 曝光可达性（30 fps 锁定下，回读 `exposure`）**

| 条件 | IQ `(isFpsFix, FpsValue)` | 回读 `exposure` | 说明 |
|---|---|---|---|
| 30 fps 锁定 + 未指定帧率 | (1, **0**) | **2224 行**（≈16.53 ms ≈ 1/60 s − 余量） | 尽管 sensor `exposure max` 已是 **4493**，**rkaiq AE 按帧率算上界** ⇒ 被钳 |
| 30 fps 锁定 + 帧率档 30 | (1, **30**) | **4474 行**（≈33.26 ms ≈ 1/30 s），稳定（`exposure max` 4492） | **1/30 s 可达** |

**(d) 行时更正（相对 v3.77）**

| 量 | 真值 | 依据 |
|---|---|---|
| **行时** | **7.433 µs**（**134.5 k lines/s**） | 由帧周期反推，三个测点 **7.4317 / 7.4355 / 7.4326 µs** ⇒ **更正 v3.77 的 7.41 µs（偏低 0.3%）** |
| **30 fps 行数** | VMAX = **4485 行** | 1/30 s = 33333 µs ÷ 7.433 = 4484.4 ⇒ 4485；**更正 v3.77 的 ≈4468** |

### 12.5 四条明确结论

1. **复位语义 = 停流即复位**：取流**前**写入保持；**流中** t=4/8 s 回读仍为 **3418**；**停流后**回读回 **1154**
   ⇒ **每次启动都必须在 `STREAMON` 之前重施**。
2. **复位源 = stock 档（AE auto）**：stock 档下**流中 t=3 s** 就把 vblank 写回 **1154**；
   **手动档（采集 / 演示档）下 t=3…12 s 全程稳定**。
3. **`exposure max = VMAX + 8`**：三个测点吻合（2234→2242、4498→4506、4485→4493）⇒ 曝光上限随 VMAX **联动**。
4. **物理 VMAX 抬升不足以解锁 1/30 s**：rkaiq 的 AE（**即便手动档**）按**帧率**算曝光上界——
   IQ `AecFrameRateMode = {isFpsFix: 1, FpsValue: 0}`（`FpsValue=0` = 未指定 ⇒ 退到 sensor 默认 60）时，
   `TimeValue=0.033333` 回读被钳到 **2224 行**；把 **`FpsValue` 改 30**（`isFpsFix` 本就为 1）后回读 **4474 行**且稳定
   ⇒ **两个条件齐备（物理 30 fps 锁定 + IQ 帧率档 30）后 O63 收口、1/30 s 可达**。

### 12.6 板测记录（含反例）

| 项 | 结果 |
|---|---|
| **TH1 自检**（板上，用户 `cat` 身份；`/dev/v4l-subdev2` 属 `root:video` 且 cat 在 video 组 ⇒ **写不需 root**） | 锁定已施加、取帧 **30/30**、`stream_on_count` 恒 **1**、第二次 `stream_on_once` = **-5**（`E_STATE`）、软复位 `rc=0` 且计数仍 1 |
| **采集工具端到端** | IQ 防呆通过（档位 collect）、30 fps 锁定已施加、采集落盘成功（`260919_01_0001_dark.nv12` = **3,110,400 B** + sidecar）；流中回读 `vertical_blanking=3405` / `exposure=4474` **全程稳定** |
| **⚠️ 反例 / 假象（如实记）** | 用 **gst 计时**的两次两点法曾给出 **31.6 fps** 与 **44.9 fps** 的**假象**，原因是 gst **冷/热启动开销不等**（各次总时长与 30 fps 仍自洽）；改用 **`v4l2-ctl --stream-mmap`** 后两点法稳定给出 **29.99 fps** ⇒ **计时口径以 `v4l2-ctl` 为准** |
| **板端收尾** | 已**还原 stock 档**（sha256 **`4536f96a0bd594ed803c7a611aa928105c5be8903549b9b269a0e2b4764fa6e0`**，与出厂**逐位一致**；`rkaiq_3A.service` = **active**） |

### 12.7 未验项 / 遗留（如实登记）

1. **演示链路（`main.py`）整链未跑**——本轮只跑 TH1 自检与采集工具，**未做端到端演示验证**。
2. **长跑（>10 min）稳定性未测**。
3. **`capture_app.py` 的 sidecar 未记录 vblank / 曝光回读**（`sensor_ctrl` 仍为 `null`）——属**未实施项**
   （见 `tools/collect/README.md` §6 / §7）。

---

## 13. `main.py` 整链复验（**v3.80 板上实测，1080p@30 锁定下**）

> **性质（务必如实写明）**：本次为 **临时验证运行**，**不是 M2 出口证据**——**未产出 `bench.py` 报告**、
> **未按 §6.5 的完整报告口径落盘**。
>
> **测量条件随行**（规则 `10-evidence.md` §1）：日期 **2026-09-19**；板型 **Embedfire LubanCat-5 V2**；内核 **5.10.160 #16**；
> 节点 `/dev/video11`；sensor subdev `/dev/v4l-subdev2`（`m00_b_imx415 1-001a`）；面板 **1024×600 @ 60 Hz**（实测刷新周期 **16.67 ms**）；
> IQ = **演示档**（`/etc/iqfiles_sets/demo/…`，sha256 **`968a636db46d5d53799776bd9baf990c52e62f30b43020bff56d96c334cf6aa0`**，
> `TimeValue=0.033333 s` / `GainValue=4.0` / `(isFpsFix,FpsValue)=(1,30)` / `bayertnr_v2 enable=1`）；
> **config 快照哈希 = `a0887d2235f7730d` 前缀**（**完整哈希未记录**）；
> **权重 / 曲线档 / 精度档** = `pipeline.curve_level=480x272`、`pipeline.curve_field_precision=fp16`、
> 模型 = `models/zde/zde_curve_256x272_int8_tk232.rknn`（= 480×272 档的**条带**输入宽 256 × 高 272，与 `N_SLICES=2 + HALO=8` 自洽）。
> **时钟口径**：延迟只用 `mono_ns`（`CLOCK_MONOTONIC`）；帧率计时 = `main.py` 自身 wall 计时。

### 13.1 目的

§12 已把 **1080p@30 锁定**在**流层**验证（TH1 自检 + 采集工具端到端），但 **§12.7-1 明确遗留「演示链路（`main.py`）整链未跑」**。
本节补齐：在 **30 fps 锁定下**跑通 `main.py` **整链**，确认 **锁定在整链下不被复位**、并取得**整链吞吐与端到端延迟**的第一手观测。

### 13.2 前置与副作用操作（规则 `30-board-safety.md` §4；副作用已获用户确认）

| 项 | 内容 |
|---|---|
| **影响面 ①（DRM master）** | TH4 需 **DRM master**，而运行前桌面活跃（`gdm3` + `Xorg vt2` + `gnome-shell`，`systemctl get-default` = `graphical.target`） |
| **操作 ①** | `sudo systemctl isolate multi-user.target` 停桌面（Xorg/gnome-shell 退出；**默认 target 仍为 `graphical`**） |
| **影响面 ②（IQ 档）** | IQ 由 `stock` 切到 **demo 档** ⇒ 期间任何取流者的 ISP 行为改变 |
| **操作 ②** | `sudo python3 tools/collect/iq_sets.py switch demo`（3A 重启后 `is-active`，sha256 回读一致） |
| **切档后、未取流时回读** | `exposure=4474` / `analogue_gain=40` |

### 13.3 方法与复现命令

```bash
sudo systemctl isolate multi-user.target                          # 停桌面（TH4 需 DRM master）
sudo python3 tools/collect/iq_sets.py switch demo                 # 切演示档
cd /home/cat/starlight && python3 main.py --frames 900
```

**运行中锁定回读（关键证据）**：在 t=5/10/15/20/25/30 s 共 **6 次**采样

```bash
v4l2-ctl -d /dev/v4l-subdev2 --get-ctrl vertical_blanking,exposure
```

### 13.4 结果

**(a) 锁定施加（stdout 首行）**

```
[sensor_lock] /dev/v4l-subdev2 vertical_blanking = 3405（回读一致）⇒ VMAX = 4485 行
```

**(b) 整链 stdout（逐字）**

```
frames=900 estimates=150 flips=900 flip_skipped=0 dropped_stale=0 lease_timeouts=0 discarded_triggers=0
wall=34.17s  fps=26.3
配对率=1.0000（已配对 900 / flip 900；未回填丢弃 0；过期帧丢弃 0）
延迟·采集→提交（应用侧）    P50=65.6 ms  P95=150.7 ms
延迟·提交→vblank（显示排队）P50=6.9 ms  P95=16.3 ms
延迟·端到端（采集→vblank）  P50=74.6 ms  P95=162.6 ms
实测面板刷新周期=16.67 ms（60 Hz）
M1 空链路基线=缺失（M1 并行产出） ⇒ 超标归因尚不可闭合（§6.5）
f_est=4.46 Hz  alpha=0.4642
stream_on_count=1（契约：恒为 1）
```

**退出码 = 0**。

**(c) 运行中锁定回读（6 次采样，恒定）**

t=5/10/15/20/25/30 s 各一次，**6 次全部**为 `vertical_blanking=3405` / `exposure=4474`（**恒定，零漂移**）。

**(d) 恢复（全部成功，未需断电重启）**

| 步骤 | 结果 |
|---|---|
| `sudo systemctl isolate graphical.target` | **Xorg（新 pid 29849，vt2）与 gnome-shell（pid 30156）正常拉起、桌面恢复** |
| `iq_sets.py switch stock` | 还原 sha256 **`4536f96a0bd594ed803c7a611aa928105c5be8903549b9b269a0e2b4764fa6e0`**（**与出厂逐位一致**） |
| `rkaiq_3A.service` | **active** |
| 取流者 | **无** |

### 13.5 结论（四条）

1. **锁定在整链下不被复位**——TH1 自动施加（stdout 首行），运行中 **6 次采样恒定 3405/4474**。
   **这是 R22 要防的失效模式在本配置下未发生**（demo 档 = 手动曝光）。
2. **`fps=26.3` ≠ 30 属「整链消费吞吐」，不是锁定失效**：传感器确在 30 fps（VMAX 4485 行、`exposure=4474` 恒定），
   瓶颈是 `main.py` 的 run 循环**单线程串行**（TH2a 的 NPU 估计内联在显示循环里）。该值与 **v3.66 / O57 记录的 25.8~26.7 fps 同区间**
   ⇒ **无回归、亦无改善**；`f_est=4.46 Hz` 同样落在 O57 记录值（4.56 / 4.70 Hz）附近，**仍低于 §9.1 的 5~15 Hz ⇒ O57 依旧开放**。
3. **判据归属**：§11 M1 出口标准 #1「暗光 1080p@30 稳定」（**相机链路**）由"流层两点法 29.99 fps + 整链运行中 vblank 恒定 3405"证实；
   整链 **26.3 fps** 对应的是 **M2 的「MIPI 双窗 P50 ≥25 FPS」**（26.3 ≥ 25）。
   **延迟（端到端 P50 74.6 / P95 162.6 ms）在 `metrics.idle_link_baseline` 仍缺失的情况下不得归因**（§6.5、规则 `30-board-safety.md` §3）。
4. **R20 的正面补充（n=1）**：本轮 `isolate multi-user.target` → `isolate graphical.target` **往返可恢复桌面**，
   **未复现** v3.72 记录的"isolate 后拉不回 GNOME 会话、需现场断电重启"（注意：v3.72 当时的操作是
   `isolate graphical.target` + `gdm restart`，与本轮单纯 multi-user 往返**不是同一操作**，故**不推翻** v3.72，只作补充事实）。

### 13.6 未验项（如实登记）

1. **>900 帧长跑**——**未验**（本轮单次 900 帧）。
2. **HDMI 通路**——**未验**（当前未接显示器，走 `MIPI_PREVIEW_ONLY`）。
3. **软复位 / 退出键路径**——**未验**。
4. **`bench.py` 报告**——本轮**无**（故**非 M2 出口证据**）。

---

## 14. 试采（暗/亮对）+ 离线 ZDE++ 增强（**v3.81 追加**）

> **性质（务必如实写明）**：本次为**试采，供人工审查**；用户明确**「不按 U1 命名/场景纪律、不关桌面」**。
> **不是 U1 数据集、不是验收证据**（规则 `10-evidence.md` §2：不得以截图/口述/单次体验替代可复现命令的判据外推）。
>
> **测量条件随行**（规则 `10-evidence.md` §1）：日期 **2026-09-19**；板型 **Embedfire LubanCat-5 V2**；内核 **5.10.160 #16**；
> 节点 `/dev/video11`；sensor subdev `/dev/v4l-subdev2`；IQ = **采集档**（`/etc/iqfiles_sets/collect/…`，
> sha256 `7e1ec9da70ee05e3e612d06cac08356d5c45ebbb2d84064524a883a4bbadae49`，
> `bayertnr_v2 enable=0` / `TimeValue=0.033333 s` / `GainValue=4.0` / `(isFpsFix,FpsValue)=(1,30)`）；
> **config 快照哈希 = `a0887d2235f7730d` 前缀**（**完整哈希未记录**）；
> 曲线档 `pipeline.curve_level=480x272`、精度 `fp16`、模型 `models/zde/zde_curve_256x272_int8_tk232.rknn`。
> **时钟口径**：仅 `last_estimate_ns` 一项涉时序（RKNN `estimate` 内部功耗计时），**单一 `mono_ns`**，非比对口径。

### 14.1 目的

§12 / §13 已在**流层与整链**验证 1080p@30 锁定与采集工具端到端；**但从未把一段真实 NV12 喂进 ZDE++ 增强看输出**。
本次在**试采（关灯暗帧 + 开灯亮帧，同一帧槽成对）** 基础上做**离线增强**，确认：
① 离线增强路径（`rga` / `rknn` / `realtime_apply`）能否把文件里的 NV12 增强出可见结构；
② 满幅（`apply_full`）与显示窗（`apply_window`）两条路径当前各自的状态（阻塞与否）。

### 14.2 前置与口径

| 项 | 内容 |
|---|---|
| **采集工具前置** | `capture_app.py`（`--outdir /tmp/capture_review --port 8091`）启动即通过 **IQ 档位防呆**并**自动施加 30 fps 锁定**（`vertical_blanking = 3405`，回读一致 ⇒ **VMAX 4485 行**） |
| **两帧回读口径** | 两帧**流中**回读均 `exposure=4474`（≈1/30 s）/ `analogue_gain=40`（= 线性 4.0×）/ `vertical_blanking=3405`（VMAX 4485 ⇒ 30.0 fps）⇒ **只有光照变了**——符合 `docs/design/dataset.md` §4.5 的「固定 ISP 参数与曝光，只改光源亮度」范式 |
| **两帧同槽** | `260919_01_0001_dark.nv12` 与 `260919_01_0001_gt.nv12` **同一帧槽 0001**，ISP 参数逐位相同 ⇒ 构成一对 |

### 14.3 方法与复现命令

**采集**（板上，工具端到端）：
```bash
cd /home/cat/starlight && python3 tools/collect/capture_app.py --outdir /tmp/capture_review --port 8091
curl -s -X POST 'http://127.0.0.1:8091/capture?kind=dark'   # 关灯
curl -s -X POST 'http://127.0.0.1:8091/capture?kind=gt'     # 开灯（同帧槽成对）
```

**离线增强**（临时脚本 `/tmp/zde_offline.py`，**未入库**；板上以 `PYTHONPATH=/home/cat/starlight` 运行）：
```bash
cd /home/cat/starlight && PYTHONPATH=/home/cat/starlight python3 /tmp/zde_offline.py
```

**路径（与交付链路同源）**：
`rga.stage()` 收 NV12 → `rga.blit_nv12_va(stage, 0)`（EST 480×270 RGB888 供 NPU）→
`rknn.estimate(est, curve, prec=1, alpha=0.4642, use_ema=True, first=True)`（双条带 INT8，`first=True` ⇒ **不做 EMA 混合**，等价交付链路首帧）→
曲线场 FP16 → `rga.blit_nv12_va(stage, 1)`（WIN 512×288 RGB888）→
`realtime_apply.apply_window(win, curve, enh, 512, 288)`（**8-iter 单遍融合**）→ RGB888 → gst `rawvideoparse format=rgb ! pngenc`。

### 14.4 结果（实测）

**(a) 两帧（同一帧槽 0001，构成一对）**

| 文件 | 光照 | 字节 | Y mean / min / max | UV mean |
|---|---|---|---|---|
| `260919_01_0001_dark.nv12` | 关灯 | 3,110,400 | **7.50** / 0 / 66 | 128.40 |
| `260919_01_0001_gt.nv12` | 开灯 | 3,110,400 | **14.73** / **12** / **21** | 127.71 |

**(b) 离线 ZDE++ 增强（暗帧 → 显示窗 512×288）**

| 量 | 实测 |
|---|---|
| `last_estimate_ns` | **25,189,535 ns ≈ 25.2 ms** |
| `slice_mask_ret` | `[0, 0]`（两条带均落 core0） |
| 原图窗 mean RGB | **1.45 / 1.32 / 1.66**（min 0 / max 31） |
| 增强窗 mean RGB | **12.49 / 11.60 / 16.37**（min 0 / **max 155**） |
| 增幅 | **均值 ×8.6、峰值 31 → 155** |
| **目视（作者本人看图所得，可作记录）** | 原图窗**纯黑**；增强窗可见**中央一团偏暗的轮廓 + 明显噪声**——即暗帧中极微弱的结构被放大 |

### 14.5 关键发现（本轮实质产出）

1. **满幅增强路径被 O54 阻塞**：`realtime_apply._geometry_from_config()` 在 `isp.csc_matrix` / `isp.rgb_range`
   为 `null` 时传 `'unset'`（`native/apply_core.h` 注明「UNSET = 该键尚未定稿，此时 `apply_full` 返回 `E_STATE`」，源码亦言「**不得猜测取值**」），
   **`apply_full` 返回 `E_STATE`**。而 `apply_full` **同时是 §8.3 集合 B（触发存盘·满幅应用）的路径** ⇒ **O54 定稿前，集合 B 也产不出图**。
   `apply_window`（输入已是 RGB888）**不受影响** ⇒ 增强图当前只能出**显示窗分辨率（512×288）**。
2. **离线增强的可行路径已打通**（供以后复用）：`rga.blit_nv12_va()` 接受任意缓冲 ⇒ 可把文件里的 NV12 经 `rga.stage()` 喂进 RGA；
   `rga.stage()` 尺寸恰为 1920×1080×1.5 = 3,110,400 B；`rknn.estimate()` / `apply_window()` 均可离线调用。
3. **`pkill -9` 强杀采集工具后 sensor 的 `vertical_blanking` 未回默认 1154**（仍为 3404）⇒ **「停流即复位」只对干净 streamoff 成立**（n=1，无对照）。
4. **流中 rkaiq 会把 `vertical_blanking` 由 3405 微调为 3404**（VMAX 4484，fps 仍 30.0）——本轮采集工具侧观测到 3404；
   而 v3.80 的 `main.py` 整链 6 次采样为 3405。

### 14.6 现象待查（不得写成结论）

**开灯后的亮帧仍近乎均匀无纹理**——Y 仅 **12–21**、PNG 仅 **25,852 B**（近乎平坦）。对照 M0-1 时期证据帧
（`docs/evidence/reports/M0-1/cam0_8mm_focused.png`、`cam0_8mm_leveled_260919.png`）可见床沿/桌面/墙砖。
⇒ **怀疑镜头遮挡（镜头盖/挡板）或正对无纹理近距表面，但无对照实验 ⇒ 因果未定，只记「现象 + 待查」**。
建议的判定方法（供后续）：在已知有纹理的取景下重采一对，比较 Y 的 min/max 跨度与 PNG 体积。

### 14.7 交付物与收尾

**交付物（**已归档入库**：`docs/evidence/reports/M1-trial-capture/`，主基准 §10.3 已登记；同日由开发机 `/tmp` 移入）**：
`260919_01_0001_dark_full.png`（暗帧满幅）/ `260919_01_0001_gt_full.png`（亮帧满幅）/
`260919_01_0001_dark_enh_win.png`（增强窗）/ `260919_01_0001_dark_raw_win.png`（原图窗，同分辨率同 CSC 的对照）。
**性质 = 试采审查图**：非 U1 数据集、非验收依据（规则 `00-ssot.md` §3.5）。

> ⚠️ **（v3.82 更正：上述 4 个文件已删除）**——用户 2026-09-19 判定「上轮的没用」⇒ 已从
> `docs/evidence/reports/M1-trial-capture/` **删除**（因该批 gt 处于 ISP/3A 异常态、近乎均匀无纹理，
> 已由 **§16** 的第二次试采取代；主基准 §10.3 的目录注释已同步）。**本节的数值与结论不依赖图文件、继续有效**
> （两帧 Y 统计、离线增强参数与结果均已在本节以文字/数字登记）。**归档目录现存文件 = §16.5 的 `_b2141` 那批**。


**板端收尾**：IQ 还原 **stock**（sha256 `4536f96a0bd594ed803c7a611aa928105c5be8903549b9b269a0e2b4764fa6e0`，
与出厂逐位一致）；`rkaiq_3A.service` = **active**；工具已停；`/dev/video11` 空闲。

### 14.8 未验项

1. **U1 正式采集**（命名/场景纪律、关桌面 P7）**全未按**。
2. **离线增强脚本未入库**（用户选择「就到这里」，`/tmp/zde_offline.py` 未纳入版本管理）。
3. **亮帧无纹理的因果未定**（见 §14.6，无对照实验）。
4. **未测长跑**。

---

## 15. ISP/3A 异常态事件与恢复（**v3.82 追加**）

> **性质（务必如实写明）**：本节为**一次板上异常事件的现场登记**，**非受控实验、无对照**（记录类，非验收依据，规则 `00-ssot.md` §2/§3.5）。
> 事件的确切机制**无 console / panic 证据** ⇒ 凡涉因果**只写「现象 + 待查」**（规则 `10-evidence.md` §2）。
>
> **测量条件随行**（规则 `10-evidence.md` §1）：日期 **2026-09-19**；板型 **Embedfire LubanCat-5 V2**（`/proc/device-tree/model` 实测）；
> 内核 **5.10.160 #16**；节点 `/dev/video11`；sensor subdev `/dev/v4l-subdev2`（`m00_b_imx415 1-001a`）；
> IQ = **stock 档**（`/etc/iqfiles/` 原文件，sha256 `4536f96a…`）；`rkaiq_3A.service` active。
> **权重 / 曲线档 / 精度档 / config 快照哈希：N/A**（本事件不涉模型与 config）。
> **时钟口径**：本档不含时序比对，**不涉 `mono_ns`**（内核日志时刻为 `dmesg` 相对秒）。

### 15.1 事件经过

用户报告「**亮图不对，场景和对焦时一样，但很暗且无纹理**」，要求启动**采集-对焦应用**（`tools/collect/capture_app.py`）在 MIPI 屏查看。当时 **IQ = stock 档**。

排查过程中执行 `sudo systemctl restart rkaiq_3A.service`（**当时 ISP 取流是活跃的**）后板子**立刻失联**：`ping` 100% 丢包、SSH `No route to host`，网段扫描亦无该机 ⇒ 需**现场断电重启**。

### 15.2 失联前诊断（板上实测）

**(a) 画面不冻帧**：隔 6 s 取两帧 JPEG，md5 不同（`8fd90847…` vs `05eb54d7…`）、`seq=4552` / `raw_seq=4553` / `dup_run=0` ⇒ **取流在跑**。

**(b) sensor 回读（流活跃）**：`exposure=405` / `analogue_gain=0` / `vertical_blanking=3405`
⇒ 曝光 `405` 行（≈3 ms）+ 增益 **1.0×**，正是**主基准 §9.5 ①** 记录的 **IQ `stManual` 默认值** ⇒ **AE 未在驱动 sensor**（stock 档本应 AE 自动）。

**(c) 3A 日志**：`20:43:39 /dev/media1: wait stream start event success ...` → `wait stream stop event...`
⇒ **3A 自认已接上**，但**未在控 sensor**。

**(d) 内核**：**oops `#2`**，`Internal error: Oops: 96000005 [#2] SMP`，`Comm: Xorg`，`pc: samsung_mipi_dcphy_power_on+0x6a0/0x8ec`，时刻 `[22174.601309]`（约失联前 15 min，**与我采那两帧的时间窗重叠**）
⇒ 即**主基准 §12 R20** 记录的 **DPMS / DSI PHY oops**（内核编号 **`[#2]`** = **本 boot 第 2 次**；与 §5 / §8.3 的"每 boot 1 次"记录不同，**只记 n=2 这一事实、成因未查**）。

**(e)** 另有 `rkisp0-vir0: rkisp_stream_stop id:0 **timeout**`（清理孤儿取流时）⇒ **ISP 侧不健康**。

### 15.3 孤儿 gst 缺陷（**已确证**）

发现一个**孤儿 `gst-launch-1.0`** 进程仍在取流（`v4l2src device=/dev/video11 … tee … mppjpegenc … fdsink fd=1 … leaky=downstream … fdsink fd=4`），
其父 `capture_app.py` 早已被 `pkill -9 -f 'capture_ap[p]'` 杀掉
⇒ **强杀父进程会留下 gst 子进程存活并继续占 `/dev/video11`**（连续取流约 10 min）。

**后果**：① 后续应用无法打开 `/dev/video11`；② 相机长时间连续取流（**R20 的风险窗口**）。

**注**：工具的**正常退出路径**（SIGTERM / Ctrl-C，`finally: proc.kill()`）**会杀掉 gst** ✓ ⇒ **缺陷只在"强杀父进程"这条路径**。

### 15.4 重启后自愈实测（板上，用户现场断电重启后）

- `uptime = 68.95 s`（刚重启）；`hostname = lubancat`；`/proc/device-tree/model = Embedfire LubanCat-5 V2`；桌面 `graphical.target` active；`rkaiq_3A.service` active（1 进程）；`/dev/video11` 空闲；IQ = **stock**。
- **本次 boot 的 oops / panic 计数 = 0**。
- 启动 `capture_app.py`（stock 档 ⇒ **IQ 防呆拒绝采集**，预览/对焦可用；自动施加 30 fps 锁定 `vertical_blanking=3405`）后，**流活跃下**回读：
  `exposure = 1350`（= **主基准 §9.5 ④** 记的 **AE auto 曝光上限 ≈10.0 ms**）、`analogue_gain = 161`（≈48.3 dB ≈ **260×**）、
  `vertical_blanking = 1154`（**stock 档下 AE 把 30 fps 锁定复位为 60 fps，与 R22 预测一致**）。
- 预览 JPEG 字节数：**异常态 158,487 B / 161,536 B** ⇒ **恢复后 1,243,449 B / 1,238,954 B**（内容量差 ~8×）。
- 作者本人目视实时预览：画面**内容完整清晰**（可见桌面、塑料袋、瓶子、白色家电、蓝柜等）。

### 15.5 结论与待查

1. **相机与镜头正常**（重启后 AE 正常工作、画面内容完整）⇒ **v3.81 的「亮帧无纹理」待查项应改判**：原记"怀疑镜头遮挡（镜头盖/挡板）或正对无纹理近距表面"，现应改为"**最可能是 ISP/3A 处于异常态**（AE 未驱动 sensor、曝光停在 `stManual` 默认 405 / 1.0）"，**镜头遮挡的怀疑可降级**（**无对照实验，仍不作定论**）。
2. **现象 + 待查（不得写成结论）**：`systemctl restart rkaiq_3A.service`（**当时 ISP 取流是活跃的**）之后板子**立刻失联**（`ping` 100% 丢包、SSH `No route to host`，网段扫描亦无该机），需现场断电重启；**无 console / dmesg 证据**（重启后本次 boot 无 oops）⇒ **时序强相关但因果未定**。**操作纪律（硬约束，供回写 R21）**：**重启 3A 前必须先停流**。
3. 另如实登记：异常态下 3A 日志**自认已接上**却未控 sensor ⇒ **判定 3A 是否生效只认 sensor subdev 回读**（与**主基准 §9.5 ③** 既有口径一致），**不看 3A 日志**。

### 15.6 未验项

1. **失联的确切机制**——**未验**（无 console / panic 证据）。
2. **异常态是否由 oops #2 直接引起**——**未验**（无对照）。
3. **R20 的 oops 与"AE 未驱动"是否有因果关系**——**未验**（无对照）。

### 15.7 本 boot 内复现 + 「熄屏 → 点亮 → oops」完整时间链（**v3.82 追加实测**）

> **背景**：用户 2026-09-19 21:13 报告「**MIPI 屏是黑屏**」。以下为只读诊断所得（**未做任何副作用操作**）。
> **测量条件随行**：板型 **Embedfire LubanCat-5 V2**；内核 **5.10.160 #16**；`systemctl get-default` = `graphical.target`；
> 桌面进程在跑（`gdm3` / `Xorg` pid 1589 / `gnome-shell` pid 1827 / `gsd-power` pid 2017）。

**时间链（`dmesg` 相对秒，boot ≈ 20:56）**：

| 时刻 | 事件 | 判读 |
|---|---|---|
| `t = 337.6 s`（≈21:01:37） | `vop2_crtc_atomic_disable Crtc atomic disable vp2` | = **空闲 300 s 自动熄屏**（`idle-delay = 300` ⇒ 20:56 + 300 s ≈ 21:01 ✓） |
| `t = 813.4 s`（≈21:09:32） | `vop2_crtc_atomic_enable Update mode to 1024x600p60` | = **DPMS 点亮**（CRTC 重新 enable） |
| `t = 813.43 s`（**+23 ms**） | `Internal error: Oops: 96000005 [#1] SMP`、`CPU: 1 PID: 1589 Comm: Xorg`、`pc: samsung_mipi_dcphy_power_on+0x6a0/0x8ec`、`lr: …+0x5c` | **DSI PHY power-on 空指针 oops**（与 §8.1 的调用栈**同址**） |
| 21:09:55 | Chromium `gpu_process_host.cc(1002) GPU process exited unexpectedly: exit_code=512` | 显示侧失效的**下游后果** |

**当前 DRM 状态（`/sys/class/drm/`）**：`card0-DSI-1 status=connected / enabled=enabled / dpms=On`；
`card0-HDMI-A-1 status=disconnected`。⇒ **DRM 认为 MIPI 屏是开着的，而面板物理黑屏** ⇒
**PHY power-on 失败后 DRM 状态与硬件不一致**（面板不会自行恢复）。

**本 boot 的 oops 计数**：体检时刻（`uptime ≈ 69 s`）为 **0**；`t = 813.4 s` 后为 **1**（内核编号 `[#1]`）。

**结论（有保留）**：**R20 的机制由 v3.72 的「强支持、非定论」升级为「时间链完整」**——
**熄屏（CRTC disable）→ 点亮（CRTC enable）→ 23 ms 后同址 oops**，本次 boot 内独立复现一次
⇒ **旧 boot 与本 boot 各自复现，不再是单次孤例**。**仍未验**：oops 的确切成因（`power_on` 内哪一步、
为何空指针）——**无内核源码级证据**，**只记现象与时间链，不写成"已定因果"**。

**缓解措施（不变，且更紧迫）**：**采集 / 演示期间不得让 DPMS 熄屏**——
① 按 `docs/plans/M1.md` 的 **P7 在无桌面态进行**；或 ② 把 `org.gnome.desktop.session idle-delay` 置 **0**（并关锁屏）。
**注意**：本 boot 的 `idle-delay = 300` **未改动**（本次只做只读诊断）。

**恢复动作与结果（用户确认后执行，副作用操作）**：
1. 先把 `org.gnome.desktop.session idle-delay` 置 **0**、`screensaver lock-enabled` 与 `idle-activation-enabled` 置 **false**
   （**防复发**；经 `gdm-autologin` 会话总线 `/tmp/dbus-…` 以 `cat` 身份写入并回读确认，已落盘 `/home/cat/.config/dconf/user`）。
2. **正常停** `capture_app.py`（`pkill -f 'capture_ap[p]'`，**不带 `-9`**）⇒ gst 子进程随之退出、`/dev/video11` 空闲
   （⇒ 再次印证 §15.3：**正常退出不留孤儿**）。
3. `sudo reboot` ⇒ **约 2 min 后 SSH 恢复**（ping 先通、sshd 后起）。
4. **重启后复核**：`idle-delay = 0` / 锁屏 false / idle-activation false（**持久化生效**）；**本 boot oops 计数 = 0**；
   `rkaiq_3A.service` active；`/dev/video11` 空闲；IQ = **stock**；`card0-DSI-1` = connected / enabled / dpms=On。
5. 重起 `capture_app.py`（HTTP 200、30 fps 锁定已施加、IQ 防呆按预期拒采）+ Chromium kiosk 到 `:0`。
   **流活跃下**回读：`exposure = 1350` / `analogue_gain = 161` / `vertical_blanking = 1154`（**stock 档 ⇒ AE 再次把 30 fps 锁定复位为 60 fps**，与 §12 R22 一致）；
   预览 JPEG **1,000,126 B / 1,000,694 B**（两帧 md5 不同 ⇒ 不冻帧）。

> **附：三帧清晰度量化对照（`PIL` + Laplacian 方差，均 1920×1080；仅供参照，非验收判据）**
>
> | 帧 | 时刻 | 全幅 lapvar | 中心 480×270 lapvar | 灰度 mean | 灰度 std |
> |---|---|---|---|---|---|
> | `/tmp/live_now.jpg`（**异常态**） | 20:46 | **1.0** | 1.2 | 13.0 | **0.3**（近乎全平 ⇒ 即 v3.81 记的"无纹理"） |
> | `/tmp/live3.jpg`（重启前健康态） | 21:01 | **24.7** | 20.9 | 64.9 | 32.7 |
> | `/tmp/live_after_reboot.jpg`（本次重启后） | 21:28 | **15.1** | 10.2 | 93.9 | 42.4 |
>
> ⇒ 本次重启后**有真实纹理与对比度**（std 42.4，远非异常态的 0.3），但**锐度低于 21:01 那帧约 4 成**
> （lapvar 15.1 vs 24.7）。**成因未查**（候选：场景/取景变化、亮度提高、AE auto 的增益档不同）——
> **只记数字与现象，不写成"失焦"结论**；**对焦是否可接受由用户在现场目视判定**。

---

## 16. 第二次试采（暗/亮对）+ 暗帧离线 ZDE++ 增强（**v3.82 追加**）

> **性质**：与 §14 同为**试采**（供人工审查），**非 U1 数据集、非验收依据**（规则 `00-ssot.md` §2/§3.5）。
> **测量条件随行**：批次 `B20260919-2141`；节点 `/dev/video11`；**IQ = collect 档**（sha256 `7e1ec9da…`，3DNR 关）；
> **固定曝光/增益 = `exposure 4474`（≈1/30 s）/ `analogue_gain 40`（4.0×）**（= §12 的 v3.79 重建值）；
> `vertical_blanking = 3405`（30 fps 锁定，**流中未复位**）；**权重 / 曲线档 / 精度档 = 与交付链路同源**（`curve_estim` + `realtime_apply`），config 未改。

### 16.1 起因与前置（副作用操作，已获用户确认）

用户 2026-09-19 21:41 指示「继续采集亮图，供我人工审核」+「采完通知我关灯，然后采暗图」。
当时 IQ = **stock 档** ⇒ 工具 **fail-closed 拒采** ⇒ 必须先换档。**按 §12 R21 的新硬约束，先停流再动 3A**：

1. `pkill -f 'capture_ap[p]'`（**不带 `-9`**）⇒ gst 随之退出、`/dev/video11` 空闲；
   父进程残留于 `futex_wait`（**无子进程**）⇒ 确认后 `kill -9` 收掉（**未留孤儿**，与 §15.3 结论一致）。
2. `iq_sets.py switch collect` ⇒ 备份 → 换文件 → 重启 3A → **回读 sha256 = `7e1ec9da…` 一致 ✓**、服务 active。
   **板子未失联**（对照 §15.1：上次是**取流活跃时**重启 3A）。
3. 重起 `capture_app.py` ⇒ **IQ 防呆「通过」（档位 = collect）**、30 fps 锁定已施加（回读 3405）；
   **流活跃下**回读 `exposure 4474` / `analogue_gain 40` / `vertical_blanking 3405` ⇒ **手动档下 AE 未把 vblank 复位**（R22 的失效模式未发生）。

### 16.2 采集结果（同帧槽 `260919_01_0001`，仅光照变化）

| 帧 | 文件 | Y 灰度 mean | Y min / max | Y std | 落盘 |
|---|---|---|---|---|---|
| 亮帧 GT（开灯） | `260919_01_0001_gt.nv12` | **21.73** | **0 / 107** | **13.21** | 3,110,400 B + sidecar |
| 暗帧（关灯） | `260919_01_0001_dark.nv12` | **7.19** | 0 / 62 | 8.99 | 3,110,400 B + sidecar |

两帧 `exposure` / `analogue_gain` / `vertical_blanking` **逐位相同**，只有光照变了 ⇒ 符合 `docs/design/dataset.md` §4.5。

### 16.3 与 §14（v3.81 试采）的对照 —— **「亮帧无纹理」已不复现**

| 指标 | §14 的 gt（异常态） | **本次 gt** |
|---|---|---|
| Y mean | 14.73 | **21.73** |
| Y **min / max** | 12 / **21**（动态仅 9 级） | 0 / **107**（**107 级**） |
| Y std | ≈2（近乎全平） | **13.21** |
| PNG 体积 | 25,852 B | **2,489,186 B** |
| 目视 | 均匀无纹理 | **桌面 / 床铺格纹 / 塑料袋 / 白家电 / 蓝柜 均可辨** |

**同档位、同场景、同曝光/增益** ⇒ **本次 gt 有真实动态范围与纹理**。
⇒ 与 §15.5-1 的改判一致：**v3.81 的「亮帧无纹理」最可能是 ISP/3A 异常态，而非镜头遮挡**（**仍为"现象 + 对照"，不作机制定论**）。

### 16.4 暗帧的离线 ZDE++ 增强（与交付链路同源）

脚本 = `/tmp/zde_offline.py`（**临时件、未入库**；板上 `/tmp` 已被 §15.7 的重启清空 ⇒ 本次由开发机重新 `scp` 部署，
并以 `PYTHONPATH=<仓库根>` 运行——**直接 `python3 /tmp/zde_offline.py` 会 `ModuleNotFoundError: _starlight_native`**，
因 `sys.path[0]` 是脚本所在目录而非 cwd）。

- `geom: est=480x272(eff 480x270) win=512x288 precision=fp16 alpha=0.4642`
- `estimate ok: slice_mask_ret=[0, 0]  last_estimate_ns = 27,418,627`（**27.4 ms**，两条带均 core0）
- `apply_window` 8-iter 单遍融合 → 增强窗 512×288 RGB888

| 窗（512×288） | RGB mean | 灰度 mean | Laplacian 方差 |
|---|---|---|---|
| 原图窗（`…_raw_win`） | 1.18 / 0.93 / 1.02 | 1.0 | 34.8 |
| **增强窗（`…_enh_win`）** | **10.33 / 8.35 / 10.29** | 9.7 | **2711.2** |

**目视（γ=2.2 归一化副本）**：**以噪声为主**，中央可见**一团偏暗轮廓**——与 §14 的描述一致（v3.81：增强窗 mean 12.49/11.60/16.37、"中央一团偏暗轮廓 + 明显噪声"）。
**未验（不得写成结论）**：该窗内"本无结构"与"增强算法放大噪声"各自贡献多少——**无对照，只记现象**。

### 16.5 归档

`docs/evidence/reports/M1-trial-capture/`（**§10.3 已登记**）下，文件名带批次后缀 `_b2141` 以免与 §14 那批同名冲突：

| 文件 | 性质 |
|---|---|
| `260919_01_0001_gt_full_b2141.png` / `…_dark_full_b2141.png` | **链路原始输出**（满幅 1920×1080，NV12→PNG） |
| `…_dark_raw_win_b2141.png` / `…_dark_enh_win_b2141.png` | **链路原始输出**（窗 512×288 RGB888） |
| ~~`…_dark_raw_win_b2141_view.png` / `…_dark_enh_win_b2141_view.png`~~ | ⛔ **已删除（v3.83 更正）**——原为 **γ=2.2 目视归一化副本**，用户 2026-09-19 判定"**多此一举、均值超人眼舒适范围、放大噪声**"⇒ **该做法已废弃并立为禁止项**（`.trae/rules/10-evidence.md` §2）。**本节的量化与结论均取自链路原始输出，不受影响** |

**未入库**：两张 `.nv12` 原始帧仍在板端 `/tmp/focus_check/`（`/tmp` 会随重启清空）。
**注意**：满幅 PNG 偏暗源于 **O54**（`isp.csc_matrix` / `rgb_range` 为空 ⇒ CSC/range 未显式）——与 §14 同一口径，**非本次采集缺陷**。

---

## 17. 固定曝光/增益取值定稿：MIPI 屏目视亮度对齐（**v3.83 追加**）

> **性质**：**技术裁定的取证记录**（用户现场目视判定），**非受控实验、无对照**（记录类，规则 `00-ssot.md` §2/§3.5）。
> **测量条件随行**：板型 **Embedfire LubanCat-5 V2**；内核 **5.10.160 #16**；节点 `/dev/video11`；**照度 = 4 Lux（开灯）**；
> 环境光由用户现场控制（**未用照度计复核**，照度为用户口述）；权重 / 曲线档 / 精度档 = 与交付链路同源（本项不涉模型）。

### 17.1 目标与判据

用户 2026-09-19 指示：「**对齐 MIPI 屏和场景在演示时的人工目视亮度**」——即在 MIPI 屏上的**采集-对焦画面**应与**现场实际取景**的人眼观感亮度一致。
**判据 = 用户现场目视**（本项本就以"演示时人眼观感"为判据，§9.5 ①）；**量化仅作随行记录，不作判据**。

### 17.2 试值过程（4× → 16× → 8×）

| 步 | gain | IQ 档 sha256（collect） | 结果 |
|---|---|---|---|
| 起点 | **4×** | `7e1ec9da…`（v3.79 重建） | 用户判定**偏暗** |
| 试建 | **16×** | `cf930829…` | **试建后未用于任何采集即被取代**（用户改选 8×） |
| **定稿** | **8×** | **`5cc97e06…`** | 用户目视判定「**已对齐、可以定稿**」 |

**命令**（每次 `build` 会**同时重建两档**，由出厂基线派生）：

```bash
cd /home/cat/starlight
sudo python3 tools/collect/iq_sets.py build --exp-us 33333 --gain 8.0 --fps 30
sudo python3 tools/collect/iq_sets.py switch collect     # 换文件 + 重启 3A + 回读 sha256
```

**两档 sha256（定稿）**：collect `5cc97e06b76e89713ebdb26ac0492acd954af0e60e5c23feeeb9bb12faea9e83` /
demo `32f95bdfb19c6935224e9e5bcc9cf49c3ae6d63f17cae456d97d573c9ae69ebd`。

**sensor subdev 回读**（§9.5 ③ 口径，流活跃）：`exposure = 4474`（≈1/30 s）/ **`analogue_gain = 60`（= 8× = 18.06 dB）** /
`vertical_blanking = 3404`（流中 rkaiq 微调，fps 仍 30；对照 app 启动日志报 3405）。

### 17.3 量化随行（同 4 Lux、同取景）

| gain | 预览帧 Y mean | Y std | Laplacian 方差 | 备注 |
|---|---|---|---|---|
| 4× | 21.7 | 13.2 | 179.5 | 取自 §16.2 的 gt 帧 |
| **8×** | **30.5** | **16.5** | **308.9** | 本版实测（`/snap.jpg` 单帧） |

⇒ **亮度 ×1.41**（**不足理论 ×2**；候选解释 = ISP gamma / DRC 对高光的压缩，**未验**）；**std 与 lapvar 同时上升**
⇒ **gain 放大噪点在数字上可见**（与用户「gain 会放大噪点、越低越好」的判断一致）。

### 17.4 排除项：range 不是采集屏的亮度杠杆（**本轮实质产出之一**）

用户曾问「O54（CSC 矩阵 / range）能否改良 MIPI 屏亮度」。**查证结论 = 不能**，两条依据：

**(a) 采集-对焦路径不过 RGA**：`v4l2src → videoconvert`〔NV12→NV12，**空转**〕`→ mppjpegenc → 浏览器解码` ⇒
O54 的 `isp.csc_matrix` / `isp.rgb_range`（作用于 RGA 的 CSC）**改不到这块屏的亮度**。

**(b) ISP 侧三键一致为 full range**（只读查 IQ 得，`python3` 递归打印路径与取值）：

| IQ 键 | 取值 | 判读 |
|---|---|---|
| `YRangeMode` | `CAM_YRANGEV2_MODE_FULL` | full range |
| `full_range` | `1` | full range |
| `cgc_yuv_limit` | `0` | CGC 不做 limited 限制（= full） |

三键**互相印证**，且与实测暗帧 **`Y min = 0`** 相符（limited range 的黑点会钉在 16）
⇒ **range 这一维已处于"最亮"选项，无校正余量**（任何改动只会往 limited = 更暗）。

⇒ **亮度杠杆排序 = 曝光（已到 1/30 s 上限）> ISP gamma（`Gamma_en = 1`，**重量级**：会改数据集输入域）> gain（**取最小可用值**）**。
**O54 的作用面随之收敛**为**交付链路的 `apply_full`（§8.3 集合 B）+ 跨环节一致性**（已回写 §15 O54 行）。

### 17.5 事实更正：增益 ↔ 寄存器映射

**`analogue_gain 寄存器 = 20·log₁₀(gain) / 0.3`**（0.3 dB 步进）。

| gain | 20·log₁₀(gain) | /0.3 | 实测寄存器 | 吻合 |
|---|---|---|---|---|
| 1.0 | 0 dB | 0 | 0 | ✓ |
| 4.0 | 12.04 dB | 40.1 | 40 | ✓ |
| **8.0** | **18.06 dB** | **60.2** | **60** | ✓ |
| 16.0 | 24.08 dB | 80.3 | 80 | ✓ |

**v3.76 写的 `20·log₂(gain)/0.3` 错**——它**与其自列的三点例证自相矛盾**（三点**只满足 log₁₀**）；
本版以第四点 **8.0 → 60** 复核通过。**已回写 §10.4 `camera.*` 行**。

### 17.6 副作用与操作失误（如实登记）

1. **两次 `switch`（含 3A 重启）均先停流**（§12 R21 的新硬约束）⇒ **板未失联**。
   **恢复路径** = `iq_sets.py switch stock`（还原出厂 `4536f96a…`），或 `build --exp-us 33333 --gain 4.0 --fps 30` 重建旧档。
2. **操作失误（已恢复）**：`pkill -f "chromium"` 的**模式匹配到了我自己那条 ssh 命令行** ⇒ **自杀式断连（exit 255）**、
   Chromium 被误杀；**app / 取流 / 3A / DPMS 均未受影响、oops = 0**，Chromium 已重新拉起。
   **与 `tools/collect/README.md` §7.3 记的同类坑同源**（须用字符类如 `chromiu[m]` 规避自匹配）。

### 17.7 未验项（登记，不得写成结论）

1. **定稿判据只在 4 Lux（开灯）下取得**——**演示照度区间（0.5–2 Lux，O65 (iii)）内的目视对齐未验**。
2. **最亮端是否 clip 未验**（O65 (iii) 的原始顾虑）。
3. **"亮度 ×1.41 而非 ×2"的成因未验**（候选 = ISP gamma / DRC 压缩高光；**无对照实验**）。
4. **照度值 4 Lux 为用户口述**，**未用照度计复核**。

⇒ 上述 ①② 已回写为 **§15 O65 的残留 ③**（**O65 仍开放**，阻塞 = M2 开工前）。

### 17.8 8× 亮帧的 ZDE++ 增强推理（**效果核对**）

用户 2026-09-19「对亮图作一次增强推理，我看看效果」派生。**目的（本步实际核对到的）= 确认 gain 提到 8× 之后，ZDE++ 输出是否仍在量程内（不过曝）**。

- **输入**：`260919_01_0002_gt.nv12`（**批次 `B20260919-2141`、8× 档、4 Lux 开灯**；场景 01 / 帧槽 0002；3,110,400 B）
- **命令**：同 §16.4（`PYTHONPATH=<仓库根> python3 /tmp/zde_offline.py <输入> <前缀>`；**临时脚本、未入库**）
- `geom: est=480x272(eff 480x270) win=512x288 precision=fp16 alpha=0.4642`
- `estimate ok: slice_mask_ret=[0, 0]  last_estimate_ns = 28,615,523`（**28.6 ms**，双条带 core0）
- 输入 Y 平面（全幅）：`mean 29.75 / min 0 / max 128 / std 16.94`

| 窗（512×288） | RGB mean | 灰度 mean | Laplacian 方差 | **max** |
|---|---|---|---|---|
| 原图窗（`…_gt_raw_win_b2224`） | 19.34 / 15.67 / 19.20 | 18.1 | 170.7 | 100 |
| **增强窗（`…_gt_enh_win_b2224`）** | **80.58 / 66.80 / 91.92** | **79.8** | **3772.8** | **207** |

**两条实质读数**：
1. **未过曝**——**`max = 207 < 255`** ⇒ **8× 定稿下 ZDE++ 输出仍在量程内、无 clip**（本步核对目的达成）。
2. **放大倍数随输入变亮而收敛**——本帧 **×4.2**（19.34 → 80.58），而 §16.4 的暗帧是 **×8.8**（1.18 → 10.33）
   ⇒ 与 tone mapping 的预期行为一致（**同模型、同曲线档、仅输入不同**）。

**目视（γ=2.2 副本）**：桌面 / 床铺格纹 / 塑料袋 / 白家电 / 蓝柜 **结构清晰可辨**，但**噪声明显**（彩色斑点）。

**未验（登记，不得写成结论）**：
1. 增强窗 **R/B 略高于 G**（80.58 / 66.80 / 91.92）的现象**归入 O54 问题域**（离线 RGB 转换未指定 CSC / range），**因果未验**（与 §14.3 的"偏品红"观察同类）。
2. **噪声构成未做对照**（输入本身噪点 vs 增强放大各占多少）。
3. 本帧是**单帧推理**（`first=True`，不做 EMA 混合），与交付链路稳态行为**不等价**。

**性质**：**效果核对**（供人工审查），**非 U1 数据集、非验收依据**（规则 `00-ssot.md` §3.5）。
**归档**（`docs/evidence/reports/M1-trial-capture/`，§10.3 已登记，批次后缀 `_b2224`）：
`260919_01_0002_gt_raw_win_b2224.png` / `…_gt_enh_win_b2224.png`（**链路原始输出**，窗 512×288 RGB888）。
**未入库**：`.nv12` 原始帧仍在板端 `/tmp/focus_check/`。

> ⛔ **（v3.83 更正：`_view` 目视副本已删除，该做法已废弃）**——本节初版曾另出 `…_view.png`（γ=2.2 提亮副本）。
> **用户 2026-09-19 判定「多此一举、均值超出人眼舒适范围、噪声也多得离谱」** ⇒ **4 个 `_view` 文件已删除**
> （含 §16.5 的两个），并**立为禁止项**（`.trae/rules/10-evidence.md` §2：链路输出必须**原样归档、原样量化**，
> 不得生成非线性后处理副本）。**本节上述量化与结论全部取自「链路原始输出」，与 `_view` 无关、不受影响。**
> **教训**：γ 提亮会把均值推入不舒适区间、同时放大噪点，作为"审查图"反而**误导**；要看清暗画内容的**正确做法是修好 O54（显式 CSC/range）**，而非后处理。

---

## 18. HDMI 显示器接入与双 CRTC 异显核对（**v3.83 追加，只读**）

> **性质**：**硬件前置状态记录 + 只读核对**，**非验收证据**（M2-1 的验证本身未做，见 §11 M2-1 行）。
> **测量条件随行**：板型 **Embedfire LubanCat-5 V2**；内核 **5.10.160 #16**；本次为**冷启动后**（用户断电 → 接 HDMI 线 → 上电）。

### 18.1 上电后只读体检（全绿）

| 项 | 值 |
|---|---|
| `hostname` / model | `lubancat` / `Embedfire LubanCat-5 V2` |
| **本 boot oops 计数** | **0** |
| `org.gnome.desktop.session idle-delay` | **0**（**断电后仍保持** ⇒ §15.7 的防 DPMS oops 措施生效） |
| `screensaver lock-enabled` | `false` |
| `rkaiq_3A.service` | `active` |
| `/dev/video11` | **空闲**（关机前已正常停流、无孤儿 gst） |
| IQ | **collect 档**，sha256 `5cc97e06…`（= v3.83 定稿的 **8× / 1/30 s**；**IQ 文件在 `/etc/iqfiles/`，断电不丢**） |

### 18.2 HDMI 通路核对

| 项 | 实测值 |
|---|---|
| `card0-HDMI-A-1` | **`connected` / `enabled` / `dpms=On`**（接入前为 `disconnected`） |
| HDMI **首选(native) mode** | **1920×1080**（`/sys/class/drm/card0-HDMI-A-1/modes` 首位；后续 1280×1024 / 1440×900 / 1280×800 / 1152×864） |
| `card0-DSI-1` | `connected` / `enabled`（**1024×600**，仍正常） |
| **CRTC 映射**（`modetest -M rockchip`） | **HDMI-A-1 → CRTC `70` @ `1920×1080`**（fb 245）；**DSI-1 → CRTC `142` @ `1024×600`**（fb 241）⇒ **双 CRTC 同时扫描** |
| **Xorg 布局**（`xrandr`） | `Screen 0: minimum 320x200, current **2944 x 1080**`；**`HDMI-1 connected primary 1920x1080+0+0`**；**`DSI-1 connected 1024x600+1920+0`** ⇒ **扩展（异显非镜像）** |
| 用户目视确认 | 「**两块屏同为桌面，一切正常**」 |

**判读**：**HDMI 与 DSI 双路异显在冷启动下天然成立**（HDMI 1920×1080 primary 在 `+0+0`，DSI 1024×600 在 `+1920+0`，Xorg 画布宽 = 1920+1024 = 2944）
⇒ **满足 §11 M2-1 行的硬件前置「HDMI 显示器接入」**（已回写该行与 §11.1 顺延说明块）。
**注意（如实登记）**：**HDMI 现在是 Xorg 的 primary** ⇒ 若重起 Chromium kiosk（采集-对焦预览），**默认会开到 HDMI 而非 MIPI 屏**，需显式指定窗口位置（如 `--window-position=1920,0`）。

### 18.3 未做 / 未验（不得写成结论）

1. **M2-1 的验证**——`modetest` 双屏色条**已于 §18.4 完成**（用户目视确认）；**其余仍全部未做**（TH5 状态机、HDMI 热插拔往返、`display.hdmi_mode_selection` 定稿），详见 §18.4 末节。
2. **未验**：HDMI 热插拔（拔掉不崩主链路）；`RUN_DEMO ↔ MIPI_PREVIEW_ONLY` 迁移；双屏满载画布流量（§11.2 M2-1 ④）。
3. **未验**：DSI 屏在 HDMI 接入后是否仍**独立**正常（本次仅见 Xorg 扩展桌面，**未跑交付链路 TH4/TH5**）。
4. 本次**未起任何取流进程**（`/dev/video11` 空闲），故**未触及相机链路**。

### 18.4 `modetest` 双屏色条打通（**§11.1-9 收口判据，用户 2026-09-19 现场目视确认**）

> **性质**：**§11.1-9 的收口验证**（用户现场目视判定）。**副作用操作**——须停桌面取 **DRM master**（按规则 `30-board-safety.md` §4 事先说明影响面与恢复步骤并获用户确认）。

**执行序列（如实记录）**：

| 步 | 命令 / 动作 | 结果 |
|---|---|---|
| 0 | 前置只读 | `async.service` = **inactive**（历史卡死源不在）；`Xorg`=1 |
| 1 | `sudo systemctl isolate multi-user.target` | `Xorg`=0 / `gnome-shell`=0 / **无 D 态** |
| 2 | `dmesg -n 1`（压低控制台日志，免字符控制台顶掉色条） | — |
| 3 | **`sleep 240 \| modetest -M rockchip -s 207@70:1920x1080@XR24 -s 221@142:1024x600@XR24 -F smpte -v`** | **两路 `set mode` 均成功**；`-v` 持续翻页 **vsync ≈60 Hz**（日志逐行 `freq: 60.00Hz`） |
| 4 | **用户目视** | **两块屏均显示 SMPTE 彩色色条** ⇒ **双 CRTC 双屏色条打通** ✓ |
| 5 | `kill <modetest>` + `systemctl isolate graphical.target` | 桌面恢复：`Xorg`=1 / `gnome-shell`=2；`HDMI-1 primary 1920x1080+0+0` / `DSI-1 1024x600+1920+0` / `Screen 0 = 2944×1080` |

**关键方法要点（供复现）**：
1. **必须让 stdin 保持打开**——`sleep 240 | modetest …`（或 `nohup` 下不要 `< /dev/null`）。**实测**：`< /dev/null` 时 modetest 设完 mode 即因 stdin EOF 退出，**色条留不住**（用户只看到字符控制台）。
2. **`-s <conn>@<crtc>:<mode>@<format>` 两路并列**即可驱动两个 CRTC；`-F smpte` 指定色条图案。
3. **`-F` 只接受有效图案名**——`smpte2` 会报 `Error: unsupported test pattern`（实测）。
4. **未使用 `-aw` / `ASYNC_COMMIT`**（v3.66 记录的 DSI 原子提交卡死触发点，本次全程规避）。

**过程中如实登记的观察（不作结论）**：
- modetest `-v` 运行期间出现 **2 个 D 态 `kworker/u16:*+events_unbound`**，`wchan` = **`drm_atomic_helper_wait_for_vblanks.part.0`**（另一次为 `worker_thread`）；**modetest 退出后 D 态自行清除**（实测两次）。
  ⇒ 判读 = **原子提交过程中等 vblank 的瞬时等待**，**与 v3.66 记录的"卡死"不同**（那次是 D 态进程持锁不释放、`kill -9` 无效、只能断电重启）。**本现象无对照实验，只记现象 + 待查**（是否每次 `-v` 都出现、是否与双 CRTC 相关，**未验**）。
- dmesg 在桌面停/启与 modetest 运行期间**反复出现** `dwhdmi-rockchip fde80000.hdmi: use tmds mode`（HDMI 重配噪声），**无 oops**（全程 `Internal error: Oops` 计数 = **0**）。

**本次验证未覆盖（不得据此判 M2-1 出口）**：
1. **HDMI 热插拔**（拔掉不崩主链路）——**未验**。
2. **TH5 HDMI 状态机**（等待 / 处理中角标 / 淡入替换 / 保持）——**未做**。
3. **`RUN_DEMO ↔ MIPI_PREVIEW_ONLY` 合法迁移**——**未验**。
4. **`display.hdmi_mode_selection` 定稿**——**已于 §18.5 完成**（`edid_prefer_1080p`）；**其余见 §18.4 末节与 §18.5**。
5. **双屏满载画布流量**（§11.2 M2-1 ④）——**未做**。
6. **未跑交付链路 TH4/TH5**（本次是 `modetest` 独立测试，**不是**主链路双屏）。

### 18.5 `display.hdmi_mode_selection` 定稿（**§11.2 M2-1-③**）

**键语义（§10.4 已定，非本次新增）**：「**EDID mode list 优先 1080p，无则 preferred**」。

**定稿值 = `edid_prefer_1080p`**（字符串常量；与 `pipeline.curve_upsample` = `bilinear_fused`、`hq.hold_policy` = `replace_and_hold` 同风格）。**语义完全对应 §10.4 本行已定的策略**，未引入新取值。

**依据（实测，只读）**：

| 项 | 值 |
|---|---|
| `card0-HDMI-A-1/status` | `connected` |
| **EDID modes 列表（首位 = 定稿依据）** | **`1920x1080`**、1280x1024、1440x900、1280x800、1152x864、1024x768×2、800x600×2、640x480×2、720x400 |
| 运行时实际落点 | `xrandr` = `HDMI-1 connected primary 1920x1080+0+0`；modetest 双 CRTC 时 **CRTC 70 @ 1920×1080** |

⇒ **EDID 优先项确实存在且为 1080p、运行时也确实落在 1080p** ⇒ 定稿值与实测一致。

**config.yaml 变更**：`display.hdmi_mode_selection` 由 `null` → **`"edid_prefer_1080p"`**（含注释：定稿依据 + 证据指针）。**`config.py` 的键白名单已含该键**（第 42 行），**无需改代码**。

**未验**：「**无 1080p 时回退 preferred**」这一分支**未验**（当前显示器 EDID 含 1080p，构造不出该分支）——**属"无对照"，不写成结论**。

### 18.6 双屏 DDR 负载测量（**§11.2 M2-1-④ / §11.1-6 子项 ②**）

> **口径（用户 2026-09-19 选定）**：**双屏翻页实测 + 满载折算**。**副作用**：需停桌面取 DRM master（按 §4 说明影响面/恢复步骤并获确认）。
> **重要边界（先读）**：**"满载"的严格定义（两路画布逐帧写入）当前不可达**——它需要 **TH5（双屏出图）**，而 TH5 **正是 M2-1 的 ①、尚未实现**。本次实测的是**「双 CRTC 扫描 + 持续翻页」口径**（只有**扫描读 + flip**，**无画布写入**）。

**(a) 实测：DDR 控制器负载（`/sys/class/devfreq/dmc/load`，格式 `load@freq`）**

| 状态 | `dmc/load` 采样 | DDR 频率 |
|---|---|---|
| 桌面态（双屏桌面，静态） | **2 / 2 / 2**（3 次） | 1848 MHz（顶到峰值） |
| 无桌面态（双屏空闲 console） | **3 / 2 / 2**（3 次） | 1848 MHz |
| **双屏持续翻页**（modetest `-v`，两路 **vsync ≈60 Hz**） | **2 ×10 次（全为 2）** | 1848 MHz |

⇒ **双屏持续翻页相对静态**（2%）**无可观测增量**（仍 2%）。`governor = dmc_ondemand`，三态下 DDR 频率**均已顶到峰值 1848 MHz**（故 `load` 是有效指标）。

**(b) 折算：双屏扫描流量**

| 路 | 画布 | 每帧字节 | × 60 Hz |
|---|---|---|---|
| HDMI（1920×1080 XRGB8888） | 8,294,400 B | — | **497.7 MB/s** |
| DSI（1024×600 XRGB8888） | 2,457,600 B | — | **147.5 MB/s** |
| **合计（扫描读）** | | | **≈645.2 MB/s** |

**量级校验（只作校验，不作结论）**：645.2 MB/s ÷ 2% ≈ **32 GB/s**，与 **LPDDR4x 64-bit @1848 MHz ≈ 29.6 GB/s** 同量级（差 8%）；若为 32-bit（14.8 GB/s）则应显 **4.4%**，与实测 2% 不符。**DDR 位宽未由 dmesg / 文档确证**（`/proc/meminfo` 仅示 8 GB），故**该反推只作量级校验**。

**(c) 满载折算投影（**投影，非实测**；TH5 未实现）**

| 项 | 流量 |
|---|---|
| 扫描读（两路） | ≈645 MB/s |
| 画布逐帧写入（若两路画布均逐帧重写） | ≈645 MB/s |
| **合计（投影）** | **≈1.29 GB/s** ⇒ 按 29.6 GB/s 峰值 ≈ **4.4%** DDR 负载 |

⇒ **初步判读（投影，不得写成实测结论）**：**即使 TH5 满载，双屏 DDR 负载量级仅约 4~6%**（再叠加实时链路自身流量仍为个位数百分比）⇒ **DDR 不太可能是 MIPI 掉帧的瓶颈**（即 **R3 的 DDR 侧初步不成立**）。**但 TH5 未实现 ⇒ 满载实测待做。**

**(d) 未验 / 边界（不得写成结论）**

1. **满载（两路画布逐帧写入）未实测**——TH5 未实现（**M2-1 ④ 事实上依赖 M2-1 ①**）。
2. **本次实测口径 = 扫描 + flip**，**无画布写入**。
3. **R3 的「MIPI 掉帧 >20%」判据未验**——`pipeline_probe` **只驱动单 CRTC（DSI）**、**无 DDR 计数器**，**无法在双屏下对 MIPI 侧打点**；§3.3 的 946 MB/s 亦为**折算**、非实测。
4. **DDR 位宽未确证**（见 (b) 的量级校验）。
5. modetest `-v` 期间**再次出现 D 态 `kworker/u16:*`**（与 §18.4 同现象，**退出后自行清除**）。

---

## 19. 第三次试采（**换回原厂广角镜头后**）+ 色偏根因收敛：**镜头**（**v3.86 追加 36**）

> **性质**：**试采记录 + 换镜 A/B 取证**（供人工审查），**非 U1 数据集、非验收依据**（规则 `00-ssot.md` §2/§3.5）。
> **触发**：用户 2026-09-21 指示「现在是换成原装广角镜头，室内灯光+自然漫光，7 Lux，请采集一张并推理后，启动采集-对焦应用」→ 应用内采集多对 → 「保存其中一对图和环境设置，室内灯光+自然漫光，7.5 Lux，原厂镜头」。
> **测量条件随行**：板型 **Embedfire LubanCat-5 V2**；节点 `/dev/video11`；**镜头 = 原厂广角**（用户 2026-09-21 现场更换）；光源 = **室内灯光 + 自然漫光**；照度 = **7.5 Lux**（用户现场录入，§15 O45 ④ 口径）。

### 19.1 换镜 A/B（**同一 IQ、同一曝光/增益、同一板子**）

| 镜头 | 图（链路原样） | 照度 / 档位 | R / G / B（窗或满幅均值） | **G/R** |
|---|---|---|---|---|
| 3.6–10 mm（旧） | `storage/o87_restore/rt1_orig.png`（10:25） | 4 Lux / demo `2ed58401` | 111.2 / 62.7 / 136.5 | **0.564** |
| **原厂广角（新）** | `storage/7lux_wide/7lux_wide_orig.png`（12:29） | 7 Lux / demo `2ed58401` | 125.1 / 126.4 / 125.0 | **1.010** |
| **原厂广角（新）** | 本节 §19.3 归档对（12:37） | 7.5 Lux / collect `ff7fb3e9` | 87.5 / 87.3 / 86.7 | **0.998** |

**判读**：**只换镜头**（IQ sha、曝光/增益回读、板子、场景均不变）⇒ **色偏从 G/R 0.564 回到 0.998～1.010（中性）**。
⇒ **色偏根因 = 旧镜头（3.6–10 mm 那支，含其 IR-cut / 镀膜）**；**§12.16.2 表中「AWB 在低 SNR 下统计失效 = 主因（推论）」应撤回**——AWB 是**执行者**（在 IR 污染输入上做补偿），不是根因。
**未验**：旧镜头哪一项（IR-cut 缺失/谱透过率/镀膜）造成——**无对照，不写成结论**；派生的 IR 判据（遥控器红外照射）**未做**。

### 19.2 本次两处口径/前置事实（如实登记）

1. **7 Lux 单帧审查（12:29，demo 档）**：`exposure 8974 / analogue_gain 87（20×）/ vertical_blanking 7906`；`gr_balance.orig_win = {whole 1.0102, dark25 0.9537}`；R 通道峰值 255（**仍触饱和**）。
2. **采集工具（12:32 启动）暴露的两个板上坑**（已绕开，供复现）：
   - **root 无 `yaml`**（cat 的装在 `/home/cat/.local/lib/python3.9/site-packages`）⇒ `PYTHONPATH` 须含该目录，否则 `ModuleNotFoundError: No module named 'yaml'`；
   - 工程内 `bisect.py` **遮蔽标准库**，被 `http.server → email.utils → random` 拖入后**在 import 期 init DRM**，桌面（gdm3/gnome-shell/Xorg）占着 DRM master ⇒ `AssertionError: drm`；**把 stdlib 目录置于 `PYTHONPATH` 最前**（`/usr/lib/python3.9:…`）即绕开。
   - 启动结果：`IQ 防呆通过（collect, ff7fb3e9…）`、`30 fps 锁定已施加（vblank 3405 ⇒ VMAX 4485）`、监听 `0.0.0.0:8091`。
3. **IQ 档与工具锁定的冲突（须留意）**：本批 IQ（collect）的 `TimeValue = 66 ms`、`fps = 15`，而采集工具强制 **30 fps 锁定（vblank 3405）** ⇒ 流中曝光被**钳到 `exposure 4493`（= VMAX+8 ≈ 33.4 ms）**、增益 `87`（20×）。
   ⇒ **本批采到的曝光 ≠ IQ 定稿值**；采集期档位与工具锁定的这一组合**是否为目标态，待裁定**。

### 19.3 归档（`docs/evidence/reports/M1-trial-capture/`，§10.3 已登记目录）

| 文件 | 性质 |
|---|---|
| `260921_01_0001_dark.nv12` / `…_gt.nv12` | **链路原始输出**（满幅 NV12 1920×1080，stride 1920 / uv_offset 2073600 / 3,110,400 B） |
| `260921_01_0001_dark.json` / `…_gt.json` | **sidecar**（`schema=capture/v2`，批次 `B20260921-1232`，`isp_fingerprint.iq_set=collect`、`iq_hash=ff7fb3e9…`、`iq_guard=ok`） |
| `…_dark_full_b1232.png` / `…_gt_full_b1232.png` | **链路原始输出**（满幅 NV12→RGB，**BT.601 limited** 解码；**零色调后处理**） |

**两帧统计**（同帧槽、ISP 参数逐位相同）：

| 帧 | Y mean / min / max / std | 满幅 RGB mean | G/R | B/G |
|---|---|---|---|---|
| dark | 91.57 / 0 / 255 / 48.52 | 87.5 / 87.3 / 86.7 | **0.998** | 0.994 |
| gt | 91.62 / 0 / 255 / 48.55 | 87.5 / 87.3 / 86.8 | **0.998** | 0.994 |

⇒ **两帧亮度几乎相同**（Y 91.57 vs 91.62）⇒ **本对不承载"照明差"**（本轮用户未改灯），与 §14/§16「关灯/开灯对」不同。**未入库**：其余 4 对（scene 02/03 共 8 帧）仍只在板端 `/home/cat/capture_data/` 与开发机 `storage/trial_260921_b1232/`。

### 19.4 未决 / 未验（不得写成结论，亦不得据此出口）

1. ~~**色偏根因的正式裁定与回写未办**~~ ⇒ **（2026-09-21 已完成，用户「好的，执行吧」）**：**主基准 §14 追加 36 落行**、**§15 新增并就地裁定 O95**（色偏根因 = 旧镜头 / 当前实装 = 原厂广角 / 采集期 optics 一致性以换镜为界）+ 来源段、**§2 传感器行事实更正**、**§10.3 归档目录文件清单**、**头部计数 94 → 95 / 已裁定 79 → 80**、**`docs/evidence/O87-lowlight-limit.md` 新增 §12.32 更正块**（撤回 §12.12 / §12.14 / §12.15 / §12.16 的「AWB 主因」）+ 四处指向 + §12.16.2 表两行更正、**`docs/design/dataset.md` 光学行与同步注补 O95**。**残留不变**：旧镜头到底是哪一项（IR-cut / 谱透过率 / 镀膜）**未验**（IR 判据未做）。
2. **采集期桌面未停**：本次启动采集工具时 `gdm3 + gnome-shell + Xorg` 仍在线（与 R20 的缓解="采集期保持无桌面"不一致）；**未停桌面**（副作用操作，未取得确认）。工具本身**不 init DRM**，本轮采集未见异常。
3. **sidecar 的 `illuminance` / `optics` / `csc` 仍为 `null`**：本节的"7.5 Lux / 室内灯光+自然漫光 / 原厂广角"为**用户现场录入、只记在本节**，**未回填 sidecar**（§15 O45 ④ 的录入载体待定）。
4. **未做**：旧镜头的 IR 判据（遥控器 IR 照射对照）、`cpsl`（`force_gray=1` / `light_src=LED` / 阈值 3000、100）的关闭 A/B、`module_calib/sensor_module/IRCutT=90` 与实际模组的一致性核对。




