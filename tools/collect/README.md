# tools/collect —— IMX415 成对数据集采集

> **本目录为隔离工作区**（2026-09-19 建立）。采集功能任务的全部代码与文档暂存于此，
> 以免与并行推进的主流程开发产生文件冲突。**待并行任务收敛后再按规则 `00-ssot.md` §3.4
> 迁出**：设计内容应并入 `docs/design/dataset.md`，本 README 降为纯用法说明。

## 1. 这是什么

板上运行的采集工具：**实时预览 + 三个按键 + 落盘无损裸 NV12 + 逐帧 sidecar 元数据**。
供 M1 阶段实拍数据，服务于 `docs/design/dataset.md` 的 **U1（INT8 校准）**、**U2（ZDE++ 零参考训练）**
与 **U4（评测）**；成对采集（暗帧 + 亮帧 GT）亦服务 U3（HQ 配对训练）。
**（v3.70）落盘 = 无损裸 NV12（`.nv12`），JPEG 仅供浏览器预览、不进数据集。**

**不是交付链路**。交付显示路径见主基准 §3.3/§3.4（DRM 双 CRTC + DMABUF lease），与本工具无关。

## 2. 与其它取流者的关系（**摄像头节点互斥**）

> **（v3.68）本目录已吸收原对焦应用**：`tools/cam_preview.py` 的**对焦核对能力（清晰度指标 + 中心放大）已并入 `capture_app.py`**，故**日常只需本工具**（一个进程同时满足"对焦核对 + 成对采集"）。原文件仍留在库中、可独立运行，但**不再是必需**。

| 项 | 说明 |
|---|---|
| **对焦能力（已并入）** | `capture_app.py` 页面含**清晰度指标**（Laplacian 类，实时显示"清晰度 / 本次最大"）与**中心 4× 放大窗**（`canvas#c2`，1920×1080）⇒ 对焦核对与采集**同一进程内完成**，无需另开一个取流进程 |
| **与 `tools/cam_preview.py`（原对焦应用）** | 除仓库根的 `sensor_lock.py`（30 fps 锁定共享实现，见 **§16**）外**不 import 其它文件**；且**不 import、不修改**该文件（用户 2026-09-19 决定）；取流（GStreamer MJPEG）、multipart 解析、浏览器轮询、`dup_run` 冻帧监控、对焦核对等做法**继承自它**。**功能已并入本工具 ⇒ 二者不必并存**；若仍同时启动，**因默认同一 `/dev/video11` 而互斥**（见下） |
| **与主链路 TH1（交付链路）** | **互斥，不得同时运行**——`tools/collect/capture_app.py` 与主链路 `capture.py`（TH1）**默认同一个 V4L2 节点 `/dev/video11`**（= `config.yaml` 的 `camera.video_node`），而**同一节点不允许同时打开**（见 §9.3）。⇒ **M1 采集期间不得跑演示链路（`main.py`）**，反之亦然 |
| **端口与"可同时跑"（v3.68 更正）** | ⚠️ 本表原写"与对焦应用端口不同（8090 / 8091）**可同时跑**"——**该表述错误**：端口确实不冲突，但**两者默认同一个 `/dev/video11`**（`cam_preview.py` 第 164 行 / 本工具第 384 行），**同一节点不可双开** ⇒ **实际互斥**。与 §9.3 的"`v4l2src` 同一节点不允许同时打开"一致。**端口错开只解决 HTTP 端口冲突，不解决摄像头争用** |

> **（v3.68）纪律**：本工具的**运行前置** = 板上**无其它取流者**（主链路 TH1 / 原对焦应用 / 任何 `v4l2src`）；**端口不同不代表可同时跑**。
>
> **（v3.78）第二条运行前置**：**当前 IQ 必须是"采集档"**（3DNR 关；O40 / O62 ④(ii)）。本工具**启动时校验**，
> 不通过则**拒绝采集**（预览/对焦仍可用）。档位管理见 **§15**；不通过时页面顶部会显示原因与换档命令。
>
> **（v3.79）第三条运行前置**：**1080p@30 锁定必须已施加**（`camera.vblank_lines` = **3405** ⇒ VMAX **4485 行** ⇒ 30 fps）。
> 本工具**启动时自动施加并回读校验**（调仓库根 `sensor_lock.apply`，见 **§16**），**未生效即拒绝采集**
> （**fail-closed**；预览/对焦仍可用）。⚠️ **停流即复位 ⇒ 每次启动都要重施**；且**必须在手动档（采集档 / 演示档）下**——
> stock 档（AE auto）会在流中把 vblank 写回 1154。依据 = §16；证据 = `docs/evidence/M1-collect-tool-board.md` **§12**。

## 3. 用法

```bash
# 0) 首次部署（v3.71 实测发现：板上 /home/cat/starlight/ 只有交付链路，本工具须单独部署）
ssh board 'mkdir -p ~/starlight/tools/collect'
scp tools/collect/capture_app.py board:~/starlight/tools/collect/capture_app.py
#    （v3.79）**必须同时部署仓库根的 sensor_lock.py**（本工具 import 它；按 __file__ 反推仓库根 ⇒ 须落在上一层）
#    否则本工具**启动即拒采**（第三条运行前置，见 §2）
scp sensor_lock.py board:~/starlight/sensor_lock.py

# 板上运行
python3 tools/collect/capture_app.py --outdir ~/capture_data

# 手机/PC 浏览器打开
http://<板地址>:8091/
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--outdir` | `~/capture_data` | 落盘目录。**不得指向 `storage.dir`**（受 §8.3 环形淘汰），也不入 git |
| `--port` | `8091` | 与原对焦应用 8090 错开 |
| `--device` | `/dev/video11` | rkisp mainpath |
| `--w` / `--h` | `1920` / `1080` | 依用户决定**按 1080p 采集**（见 §5 的 crop 约束） |
| `--batch` | 自动 `B<日期>-<时分>` | 采集批次号，写入 sidecar |
| `--raw-frame-size` | `0`（= `w*h*3/2`） | **（v3.70）** 裸 NV12 单帧字节数；1920×1080 默认 **3110400**（板端实测 `sizeimage`）。仅在驱动改变 `bytesperline`/`sizeimage` 时才需覆盖 |
| `--vblank-lines` | `7906`（= config `camera.vblank_lines`） | **（2026-09-29 对齐 O91）** 帧率锁定的 `vertical_blanking` 目标值（VMAX = 1080 + 7906 = 8986 行 ≈ 66.8 ms ≈ **15 fps**）。启动时经 `sensor_lock.apply` 写入并**回读校验**；**停流即复位 ⇒ 每次启动重施**（§2 / §16）。⚠️ 历史默认 **3405**（= v3.79 的 30 fps 值）**已作废**——不改则**静默**锁到 30 fps 且 66.6 ms 曝光被钳 |
| `--sensor-subdev` | 自动（**运行时解析**） | **（v3.79）调试用**：强制指定 sensor subdev 节点。**默认不指定**——按「唯一暴露 `V4L2_CID_VBLANK` 的 v4l-subdev」**语义判据**解析，**禁止写死编号**（§16） |
| `--lux-anchor` | `None`（= 未录，记 `null`） | **（2026-09-29，G3）** 批次级**照度计实测**（Lux，**每批一次**，见 §12.4）。写入每帧 sidecar 的 `illuminance.lux_anchor`；不填不臆造 |
| `--lux-anchor-note` | 空 | 照度锚点的测量方式 / 照度计型号 / 测点位置（人读）。**各档照度不同时写在此**（如"暗档 0.75 / 亮档 4.0"） |
| `--light-type` / `--light-cct-k` / `--light-dimming` / `--light-distance-m` / `--light-note` | 空 / `None` | **（2026-09-29，G3）** **批次级**光源信息 → `light_source.*`。⚠️ `--light-dimming`（调光方式）**与"调光致色温漂移"的判别直接相关**，建议必填。**光源条件中途变化 ⇒ 换 `--batch` 重开一批** |

停止（**（v3.82）两步**）：

```bash
# ① 优先**正常终止**（**不带 `-9`**）——工具的 SIGTERM 处理会自行杀掉 gst 子进程
ssh board "sudo pkill -f 'capture_ap[p]'"
# ② 若确认无残留再补杀 gst
ssh board "pgrep -af 'v4l2src device=/dev/video11'"        # 检查
ssh board "sudo pkill -f 'v4l2src device=/dev/video11'"    # 必要时
```

> ⚠️ **（v3.82）实测说明**：**`pkill -9` 只杀父进程会留下孤儿 `gst-launch-1.0` 继续占用 `/dev/video11`**
> （v3.82 实测，证据 = `docs/evidence/M1-collect-tool-board.md` **§15.3**）。
>
> **（2026-09-29 修复）** 先前 `SIGTERM` 处理里 `srv.shutdown()` 与 `serve_forever()` **同线程**调用会**自锁**
> ⇒ 进程**退不掉**且**仍占 8091**（实测：gst 已退、`/dev/video11` 已释放，但 python 仍 `LISTEN`，
> 下次启动报 `Address already in use`）。现改为**另一线程**调用 `shutdown()`；复测 = **进程退出 ✓ / 8091 释放 ✓ / gst 无残留 ✓**。
> ⚠️ 注意 `pkill -f 'capture_ap[p]'` **不要**与含 `capture_app.py` 字样的命令行写在同一串（会连自己的 shell 一起匹配杀掉）。

## 4. 操作流程（按键语义）

采集范式依 `docs/design/dataset.md` §4.5 的 **(a) 改良版**：**固定 ISP 参数与曝光，
只改光源亮度**。这样同一场景的暗帧与亮帧 ISP 行为完全一致、几何天然对齐、3DNR 不错配。

```
【成对采集】摆好取景
  → 把光源调亮  → 按【采集亮帧 GT】   ⇒ 260919_01_0001_gt.nv12
  → 把光源调暗  → 按【采集暗帧】      ⇒ 260919_01_0001_dark.nv12（同帧序号 ⇒ 一对）
  → 按【更换场景】                     ⇒ 场景号 +1，进入 02

【U1 单边图】摆好取景 → 按【采集暗帧】 ⇒ …_01_0001_dark.nv12
  → 改光照 → 再按【采集暗帧】          ⇒ …_01_0002_dark.nv12（帧序号递增）
  → 换取景 → 按【更换场景】再按【采集暗帧】
```

- 顺序**不限**（先采暗后采亮亦可），两个键各自独立落盘。
- 状态栏实时显示当前**帧槽**号与该槽的 `暗帧 已采/未采`、`亮帧GT 已采/未采`。
- 【更换场景】时若该槽有缺失，提示中会列出**缺哪个**（不阻断，但会留痕），并给出本场景累计张数。
- **同一帧槽内重复按同一档位 ⇒ 开新的一帧槽**（帧序号 +1），**不再覆盖**；确需重采时删掉该帧文件即可。

## 5. 落盘约定

> ✅ **本节为已实施口径（v3.70 落地）**：命名 = `YYMMDD_<场景2位>_<帧序号>_dark|gt.nv12`，
> 落盘 = **无损裸 NV12**，sidecar = **逐帧一份**。旧口径（`S001_dark.jpg`、有损 JPEG、每场景一份
> sidecar）**已作废**。

```
<capture_data>/
├── 260919_01_0001_dark.nv12   暗帧（增强链路输入域；**无损裸 NV12**）
├── 260919_01_0001_dark.json   该帧 sidecar（**逐帧一份**）
├── 260919_01_0001_gt.nv12     亮帧（配对 GT；与 dark 同帧序号 ⇒ 构成一对）
├── 260919_01_0001_gt.json
├── 260919_01_0002_dark.nv12   U1：同一取景内改光照后再按一次 ⇒ 帧序号 +1
└── .capture_state.json        采集进度（下一场景号 / 各场景帧槽），**重启不丢**
```

| 段 | 规则 |
|---|---|
| `YYMMDD` | 采集日期戳（如 `260919`），**逐帧取本地日期** |
| `<场景2位>` | **00–99**，由「更换场景」按钮 **+1**；**仅作编号与 dark-gt 配对键**，不承担表达场景内容 |
| `<帧序号>` | 同场景内的**帧槽号**（4 位，从 `0001` 起），**区分同场景多帧**；**成对采集时 dark / gt 同号**（构成一对） |
| `dark` / `gt` | 档位：暗帧（增强链路输入域）/ 亮帧（配对 GT） |
| 后缀 | **`.nv12`**（无损裸 NV12，见 §9） |

**帧槽推进规则（实现口径）**：**同一帧槽内重复按同一档位 ⇒ 开新的一帧槽**（帧序号 +1）。⇒
成对采集 `GT → dark` 得同号一对；U1 全按 `dark` 则 `0001/0002/0003…` 递增。

- **JPEG 降级为仅供预览，不进数据集**（预览走 gst 路①，落盘走路② 裸 NV12）。
- **进度持久化**：重启应用**不会**重置场景号与帧槽（否则会覆盖已采数据）。
- 写 sidecar 用**临时文件 + `os.replace` 原子替换**，避免断电写坏。

> **术语（用户 2026-09-19 澄清）**：本工具的"**场景**"= **取景**（狭义）。成对采集时 = 同一取景的"暗光 vs 加光源"；**U1 采集时全程只按 `dark` 键、无 `gt`**——同一取景内改光照后再按 `dark`（帧序号递增），**换取景才按一次「更换场景」**。U1 的组成结构与落盘约定见 `docs/design/dataset.md` §5.2.8。

### crop 约束（重要）

用户 2026-09-19 决定**按 1080p 采集**。据 `docs/design/dataset.md` §5.2.1：
**1080p 采集下 IMX415 不可做保尺度 crop**——1080p 裁 960×540 再降到 480×272 是 2× 倍率，
而演示条件是 4×（1080p 整幅 → 480×272），倍率改变即噪声统计改变。
⇒ **IMX415 样本数 = 场景数**，本工具不提供 crop 功能，这是刻意的。

## 6. sidecar 元数据

字段分组依 `docs/design/dataset.md` §6.1。**只写本应用能确知的字段**；
需要另行取数的字段一律置 `null` 并列入 `_pending`，**不填占位假值**（规则 `10-evidence.md` §2）。

| 字段 | 状态 |
|---|---|
| `scene_id` / `frame_id` / `pair_id` / `capture_batch` / `kind` | ✅ 本应用写入 |
| `time.mono_ns`（计量域）+ `time.utc_iso8601`（可读） | ✅ 双时钟 |
| `capture_condition`（设备 / 分辨率 / 像素格式 / gst 管线） | ✅ 本应用写入。**（v3.69）无损落盘落地后须增 `stride` / `uv_offset` / `size_bytes`**（裸 NV12 无自描述，**缺此三项开发机侧会按 `W×H×1.5` 错位解析**） |
| `pair`（配对帧 ID / GT 来源 / 几何对齐方式） | ✅ 本应用写入 |
| `file`（文件名 / 字节数） | ✅ 本应用写入 |
| `isp_fingerprint` | ✅ **全项落地（2026-09-29，G5 补齐）**：`iq_file` / `iq_hash` / `iq_set` / `iq_guard` + **7 子项 × 每 `scene_isp30`**（`gamma_table` / `ccm` / `black_level` / `awb_lock` / `nr_2d` / `nr_3d` / `dpc`；**口径 = IQ 内路径 + 可观测状态 + 规范化子树哈希前 16 位**，**不复制取值**）。**（v3.86 追加 161）子项数更正：实测 = 实现 `_iq_fp_scene()` 与 `config.yaml` 均为 7 子项，原写"8"为计数错误**。子树边界**以板端实测确定**（与 `config.yaml` 已归档 16 位逐位一致）；解析失败**不静默**（页面横幅 + `_pending`） |
| `sensor_ctrl`（曝光 / 模拟增益 / vblank 回读） | ✅ **落地（2026-09-29，G5）**：采集时刻 `v4l2-ctl -d <subdev> --get-ctrl exposure,analogue_gain,vertical_blanking` 实测值 + IQ 手动档期望值（`time_value_s` / `gain_value` / `is_fps_fix` / `fps_value`）；失败则**保留 `null` 并把原因记入 `sensor_ctrl_error`** |
| `csc`（RGA CSC 矩阵 + range） | ⛔ `null` + **`_not_applicable.csc` 说明**（本链路为 v4l2 直出、未过 RGA ⇒ **无 CSC 可记**；O54 的 CSC 显式化只影响交付链路与离线转换工具） |
| `illuminance`（光照） | ✅ **落地（2026-09-29，G3）**：`frame_y_mean`（**逐帧** Y 平面 1/16 隔行隔列降采样，零人工）+ `lux_anchor` / `lux_anchor_note`（**批次级**，`--lux-anchor`）+ `method` 口径声明。缺锚点 ⇒ `lux_anchor=null` + `_pending:['lux_anchor']` |
| `light_source`（光源） | ✅ **新增（2026-09-29，G3）**：`type` / `cct_k` / `dimming` / `distance_m` / `note`（**批次级**，`--light-*`）；缺项记 `null` + `_pending` |
| `optics`（镜头型号 + 对焦判据） | ⛔ `null`，O31 关闭后才有 |
| `split`（train/val/test） | ⛔ `null`，划分时回填 |
| `dataset_version`（数据集版本哈希） | ⛔ `null`，打包时回填 |

> ⚠️ **`_pending` 里的字段未补齐前，本工具产出的数据集不满足 §6.1 的"必须包含 §9.5 全部
> ISP 指纹字段"要求**。补齐是 M1 采集开工前的必办项。

## 7. 已知限制

1. ~~图像为 JPEG 有损~~ → **已解决（v3.70）**：落盘改走**裸 NV12**（§9），**JPEG 仅供预览、不进数据集**。
2. ~~sidecar 每场景一份~~ → **已解决（v3.70）**：改为**逐帧一份**（§11），同场景多帧不再互相覆盖。
3. ~~**无照度录入 UI**~~ → **✅ 已解决（2026-09-29，G3）**：改为**零人工 + 每批一次**——逐帧 Y 均值自动算，照度/光源走 CLI（`--lux-anchor` / `--light-*`）。**不提供页面输入框**（避免运行期改值与批次语义冲突）。
4. ~~**ISP 指纹仅部分落地**~~ → **✅ 已解决（2026-09-29，G5 补齐）**：`isp_fingerprint` 扩为 **7 子项 × 每 `scene_isp30`**（**v3.86 追加 161 更正 8 → 7**）、`sensor_ctrl` 改为采集时刻回读、`csc` 判 N/A（理由入 `_not_applicable`）。**⚠️ 仍受 O54 影响的是「离线 RGB 转换工具的 CSC/range」**（属交付链路，不属本工具）。
5. **冻帧拦截（v3.70 改口径）**：陈旧判定**跟数据源走**——数据取自路② 裸帧，故以
   **裸帧是否仍在更新**为准（>1 s 未更新即拒绝采集）；`dup_run`（路① JPEG 的重复帧计数）
   **降为页面诊断指标**。理由：`dup_run` 是 `mppjpegenc` 陈旧缓冲的征候、**与路② 数据无关**，
   且**静止画面下会误拦**（原"待确认是否放宽"的顾虑随之消除）。
6. ~~**（v3.79）sidecar 未记录 30 fps 锁定回读**~~ → **✅ 已解决（2026-09-29，G5）**：`sensor_ctrl.readback` 现记 `vertical_blanking` / `exposure` / `analogue_gain` 的**采集时刻实测值**（+ `expected` 期望值）。**警**：`sensor_ctrl` 为**逐帧**项，仅在该帧采集成功时计入；失败则 `null` + `sensor_ctrl_error`。

## 8. 登记状态

- 主基准 §10.3 代码结构树：**已登记**（v3.61 落位；见 §14 对应变更行）；**（v3.76）本目录新增 `isp_owner.c` 并已登记**（§10.3 + 主基准 §14 v3.76）。**（v3.77）`isp_owner.c` 的登记注释已改判为"调试工具"**（§10.3 + 主基准 §14 v3.77），承载方式改走 **§14.6 纯 IQ 路径**。**（v3.79）新增仓库根共享模块 `sensor_lock.py`（1080p@30 锁定）并已登记**（主基准 §10.3 + §14 v3.79）；**由 TH1（`capture.py`）与采集工具（`capture_app.py`）共同调用**，说明见 **§16**
- **（v3.70）命名 + 无损落盘 + 逐帧 sidecar 已实施**（`tools/collect/capture_app.py`）；**（v3.71）板上实测通过**——真机双 `fdsink` 共存、路② 帧长 3110400、命名/帧槽/逐帧 sidecar 落盘、裸帧解码目视均符合；**证据 `docs/evidence/M1-collect-tool-board.md`**。主基准已升至 **v3.71**，§10.3 与 §14 已同步
- `docs/design/dataset.md` §7「工具与实现落点」：**已于 v3.68 补登本工具**（`tools/collect/capture_app.py`），并核对了与三张契约白名单的一致性
- 本目录文档的**归属仍未落定**：规则 `00-ssot.md` §2 规定设计类文档应置于 `docs/design/<模块>.md`。本 README 的 **§1~§8 属工具用法说明**（可留在原地），但 **§9~§12 是"补法方案"= 设计类内容**（决定"怎么做"），按 §2 应并入 `docs/design/dataset.md`。
  **（v3.68）本目录建立时的"待并行任务收敛后再迁出"前提已满足**（并行任务已收敛）⇒ **迁出动作本身待裁定**：设计内容并入 `docs/design/dataset.md` 后本 README 降为纯用法说明。**已登记为主基准 §15 的 O60**（与 `tools/collect_calib.py` 的命名归属同属"登记/架构"类）。

## 9. 无损 NV12 落盘（**已实施，v3.70**）

> **状态：v3.69 定方案 → v3.70 实施**（本目录 §5 的命名、§11 的逐帧 sidecar 与本节同批落地）。
> **实施记录**：gst 管线 = `v4l2src ! … ! videoconvert ! video/x-raw,format=NV12 ! tee name=t`
> → 路① `t. ! queue ! mppjpegenc ! multipartmux ! fdsink fd=1`（预览）
> → 路② `t. ! queue leaky=downstream max-size-buffers=2 silent=true ! fdsink fd=<pipe>`
> （落盘，**按定长读、无 multipart 分帧**）。路② 的写端经 `Popen(pass_fds=…)` 交给 gst，
> 读端由本进程的 `raw_reader` 线程抽干、**只保留最新一帧**。

### 9.1 背景（b 问题的本质）

当前采集源是 `STATE['jpg']`——**内存里只有 `mppjpegenc` 编出的有损 JPEG**，故无论暗/亮帧落的都是
有损图。两层伤害：**GT（亮帧）有损 → 抬 PSNR 基线**；**输入（暗帧）有损 → 训练/校准统计偏移**。
要补的是**让采集能拿到无损帧**；预览可继续用 JPEG（手机浏览器需要可解码格式），但**落盘换无损源**。

### 9.2 选定方案：tee 分叉 + 双 fdsink

```
v4l2src → videoconvert(videoconvert 前置拷贝) → tee
   ├→ queue → mppjpegenc → multipartmux → fdsink  —— 路① JPEG（预览，沿用现状）
   └→ queue → fdsink                                  —— 路② 原始 NV12（采集，新加）
```

- 采集时写**路②的最新 NV12 缓冲**，而非路①的 JPEG。
- NV12 每帧 = 1920×1080×1.5 字节 = **固定长**，路② reader **按定长读、无 multipart 分帧**。
- 路②**独立 reader 线程**维护 raw 缓冲（结构同现有 JPEG reader，去掉 multipart 解析）。

### 9.3 否决的两路（留档反模式）

| 方案 | 否决理由 |
|---|---|
| **3 整体改 NV12、砍 JPEG** | 手机浏览器**无法显示 NV12**，预览报废 ⇒ 不满足方案 A 前提 |
| **2 采集时另起子进程抓单帧** | `v4l2src` 同一节点**不允许同时打开**（rkisp mainpath 通常单打开者）；主预览占着就开不了 |

### 9.4 必须处理的坑

1. **tee 拖影**：v3.45 曾归因 `v4l2src ! tee`，**v3.46 已撤回该因果**（唯一核实事实 =「
   去掉 tee 改单路后拖影依旧」，tee 非唯一原因，原因未定、嫌疑在渲染/sink 侧）。⇒ **tee 可用**，
   但两分支**各加 `! queue`**，避免慢支路（JPEG 编码）堵住快支路（raw）。
2. **多 fd 取流**：路①走 fd 1（现有 `subprocess.stdout`），路②需另开管道。
   **（v3.70 实施定案）** `os.pipe()` + `Popen(..., pass_fds=(w,))`：`pass_fds` **保留 fd 编号**，
   故子进程内 `fdsink fd=<同一个编号>` 即可，**不需要 C 辅助、也不需要临时文件**。
   父进程 `Popen` 后立即 `os.close(w)`，读端才能在 gst 退出时收到 EOF。
   **另**：路② 的 `queue` 用 `leaky=downstream`（满时丢**旧**缓冲，`GstQueueLeaky` 语义：
   `upstream` 丢新 / `downstream` 丢旧）⇒ 只保留最新帧，且慢读者不会把主链路堵住；
   `silent=true` 抑制丢帧告警刷屏。
   **验证（开发机，`videotestsrc` 代源）**：`rc=0`、路① JPEG 正常、路② 字节数**恰为帧长整数倍**
   （零头 0 B）⇒ **定长假设成立**。
3. **帧同步**：数据集**只需要 NV12（raw）落盘**，JPEG 仅作预览、**不存** ⇒ 两路"最新缓冲"
   不必严格对齐到同一帧，抖动在编辑上无影响。若未来要 JPEG 与 raw 严格成对，需按 `seq` 配对。

### 9.5 落盘命名（用户 2026-09-19 决定）

- **后缀 = `.nv12`（v3.69 定稿）**——扩展名如实反映无损裸 NV12，避免 `.jpg` 名不副实。
  **命名前缀已由 v3.69 改为 `YYMMDD_<场景2位>_<帧序号>_dark|gt`**（原 `S001_dark` 形式**作废**，见 §5 与 `docs/design/dataset.md` §5.2.8）。
- **必须同时记录 `stride` / `uv_offset` / `size_bytes`**（写入 sidecar 的 `capture_condition`）：裸 NV12 无自描述，缺此三项开发机侧按 `W×H×1.5` 解析会**错位**。**（v3.70 已实施）**——板端实测该节点为 **NV12 1920×1080 单平面 continuous、`bytesperline`=1920、`sizeimage`=3110400**（`environment/board-lubancat5.md` §7）⇒ 三项分别落 1920 / 2073600 / 3110400；`--raw-frame-size` 可覆盖以防驱动改值。
- **JPEG 降级为仅供预览，不进数据集。**
- 色彩空间：NV12 是 ISP 直出（RGA CSC **之前**），而训练/评测走 RGB。**建议采集只管无损 NV12，
  RGB 转换留作离线工具**（采集快、无损；色彩/range 决策推迟到训练工具）。——**待实施时确认**。
- **存储代价**：NV12 ≈ **2.97 MB/帧**，是 JPEG 的 **~30 倍**。板 58 G 根分区暂够；数据以开发机
  侧为主、板上只做采集并及时 rsync 走（符合 §6.2 归档纪律）。

### 9.6 未随本方案改的（另列）

本方案只解决 b（防 PSNR 抬升）。**（v3.70）c（sidecar 每场景一份）已随命名改造一并落地**
（改为逐帧一份，见 §11）；**a（ISP 指纹）仍开放**（受 **O54** 阻塞），见 §7/§6/§10。

## 10. 补法方案：ISP 指纹采集（已定方案，未实施）

> **状态：2026-09-19 定方案、暂缓实施**。与 §9 同因（并行会话占用板资源）。
> **本方案不碰板、纯设计**；其中三项需板上核实（见 §10.4）。

### 10.1 三条设计洞察

**洞察 1：静态指纹与运行期指纹必须分开记。**

| 类 | 内容 | 来源 | 回答的问题 |
|---|---|---|---|
| **静态（配置态）** | IQ 文件 + 哈希、gamma 表、CCM、black level、NR 档、DPC 开关 | **IQ JSON 文件** | "**打算**用什么 ISP 行为" |
| **运行期（实测态）** | 曝光/增益**回读值**、AWB 锁定态、3A 当前实际应用的参数 | `v4l2-ctl` / debugfs / 3A 服务 | "**实际**生效的是什么" |

**理由**：`docs/design/dataset.md` §6.1 与主基准 §9.5 的目标是"杜绝**同名预设、实际 ISP 行为漂移**"——
而漂移恰恰发生在**两者不一致**时（典型：改了 IQ JSON 未重启 `rkaiq_3A_server`；AE/AWB 自动改了参数）。
**只记 IQ 文件不足以发现漂移** ⇒ 两类都记，且**不一致本身就是可检出的信号**。

**洞察 2：CSC 目前是「不可知」的——这是必须修的前置。**

核查 `native/rga_core.c`：CSC 走 `RK_FORMAT_YCbCr_420_SP`（NV12）→ `RK_FORMAT_RGB_888`，
**未显式设置色彩空间** ⇒ 用的是 **librga 的隐式默认**。对应地，`config.yaml` 的
`isp.csc_matrix` / `isp.rgb_range` **均为 `null`**（定稿时点 M1）。

⇒ **后果有两层**：
1. **指纹记不下来**——隐式默认值不查 librga 源码/实测就不知道；
2. **离线 RGB 转换无法与部署一致**——§9.5 v3.4 ① 早已警示"CSC 同样移动模型输入分布，却不属 ISP 参数，**最易被漏掉**"。

**修法**：在 RGA 初始化处**显式设置**色彩空间（`imconfig(IM_CONFIG_COLOR_SPACE_MODE, …)`），
使 CSC 成为**已知且可回放**的值 ⇒ 既可知、又可记、又能让离线工具精确复现。
**这条属于交付链路（`native/rga_core.c`），不在本采集目录内**——本方案只登记该依赖，**不代改**。

**洞察 3：CSC 不只是元数据，是离线转换的正确性前提。**

§9 定的路线是"采集只管无损 NV12，**RGB 转换留作离线工具**"。⇒ **该离线工具必须用与 RGA
完全相同的 CSC 矩阵与 range**，否则**训练域与部署域的色彩不一致**——这是我们自己造出来的域差，
且会直接污染 PSNR/SSIM 结论。故 CSC 必须进 sidecar，**离线工具须读它、而非用 OpenCV 默认**。

### 10.2 字段 × 取法映射

| 字段（§10.4 `isp.*` / §9.5） | 取法 | 板上已验证？ |
|---|---|---|
| `iq_file` | 已知路径 `/etc/iqfiles/imx415_CMK-OT2022-PX1_IR0147-50IRC-8M-F20.json`；**更稳妥**取 `rkaiq_3A_server` 的实际加载路径（`/proc/<pid>/cmdline`） | ✅ 路径已知；**pid 实际加载路径待核** |
| `iq_hash` | `sha256sum <iq_file>` | 待核 |
| `gamma_table` | 解析 IQ JSON 的 gamma 段 | 待核（**段名待定**） |
| `ccm` | 解析 IQ JSON 的 CCM 段 | 待核 |
| `black_level` | 解析 IQ JSON 的 BLC 段 | 待核 |
| `nr_2d` / `nr_3d` / `dpc` | 解析 IQ JSON 对应段 | 待核（**注意**：`/dev/video11` 无 NR 控制项，只能从 IQ 取——v3.46 已核） |
| **曝光 / 模拟增益 / 数字增益（回读）** | `v4l2-ctl -d /dev/v4l-subdev<N> --get-ctrl` | ✅ **已实测通过**（档案 §4：`exposure = 1350` / `analogue_gain = 161`） |
| `awb_lock` | 3A 运行态 | 待核（debugfs 或 3A 接口） |
| `isp.csc_matrix` / `isp.rgb_range` | **见洞察 2**——须先显式化 | ⛔ 当前 `null` |
| `isp.nr_3d_ref_frames` + 帧级延迟 | **属 O40**，未裁定 | ⛔ 见 §10.5 |

### 10.3 实现落点（未实施）

- 新增 **`tools/collect/isp_fingerprint.py`**：单一函数 `collect(device) -> dict`，采集时调用，
  产出填入 sidecar 的 `isp_fingerprint` / `sensor_ctrl` / `csc` 三组（替换当前的 `null` + `_pending`）。
- **失败不得静默**：任一项取不到，**保留 `null` 并在 `_pending` 留该项**（规则 `10-evidence.md` §2
  ——**禁止填占位假值**）；同时在页面回显一条警告，避免"以为记上了、其实没记"。
- **不新增 config 键**：本工具只**读** config 与板上状态，**不写**（规则 `20-contracts.md` §1）。

### 10.4 需板上核实的三项（暂缓，待并行会话退出）

| # | 待核 | 为什么必须先核 |
|---|---|---|
| 1 | **IQ JSON 的实际段名与结构**（gamma / CCM / BLC / NR / DPC） | 段名猜错 ⇒ 解析静默失败。**须以实际文件为准**，不得照文档推测 |
| 2 | **传感器 subdev 编号**（`/dev/v4l-subdev0…2` 中哪个是 `m00_b_imx415`） | 取错设备 ⇒ 读到无关 ctrl。档案 §4 已确认 ctrl **可读**，但**未记哪个 subdev** |
| 3 | **3A 运行态的暴露方式**（debugfs 路径 / 3A 服务接口 / 是否可得） | 决定 `awb_lock` 能否自动化。**若不可得，则该项须人工录入**（与 d 合并） |

### 10.5 与 O40 的交叉（不属本方案）

`isp.nr_3d_ref_frames` 与 3DNR 帧级延迟归 **O40**（3DNR 采集期与运行期一致性）。**（v3.73 已裁定 = 采集期关闭 3DNR**——实测 IQ 的 `bayertnr_v2/TuningPara/enable = 1` 即现采集期是开着的；**（v3.75 裁定 O62 = 选项 (b) 两套 IQ 文件**：**采集档关 NR / 演示档开 NR，各自记文件名 + sha256**；实现 = 建两套 IQ + 采集档 `enable=0` + `systemctl restart rkaiq_3A.service`，**未执行**）。
**本工具侧的硬约束（v3.75 新增，实现时必做）**：**采集工具启动时须校验"当前 IQ = 采集档"，否则拒绝采集**（防"忘切"导致**静默污染数据集**）；sidecar 须记 **IQ 文件名 + 哈希**。
**实现前置阻塞**：§9.5 的"关 AE + 固定曝光/增益"**（v3.76 已落实，见 §14）**——v3.75 实测发现 AE 仍自动跑 ⇒ ISP 参数 A/B 被混淆（证据 = `docs/evidence/M1-collect-tool-board.md` §9）；**v3.76 新增 `tools/collect/isp_owner.c` 自持 ISP 上下文，把 auto 曝光/增益范围钉成一点** ⇒ **该阻塞已解除**（板上实测见该档 §10）。
本方案**只留接口位**（sidecar 字段已在）、**不代裁定、不填值**；**实现随该前置落实后办**。
**（v3.76 承载方式修订）** 因 3A 改由**自持进程**持有、**IQ 目录可在 `rkaiq_uapi2_sysctl_init` 参数指定** ⇒ 上文"采集工具启动时须校验'当前 IQ = 采集档'，否则拒绝采集"的**防呆不再需要**（"忘切"通道被结构性消除）；**sidecar 记 IQ 文件名 + 哈希的护栏保留**；**两套 IQ 的实现仍待办**（`isp_owner.c` 目前硬编码 `/etc/iqfiles/`）。

### 10.6 本方案不解决的

**（v3.70）c（sidecar 每场景一份）已随 §11 落地**；**a（ISP 指纹）仍开放**（受 **O54** 阻塞）；
**洞察 2 的 CSC 显式化属交付链路**，须在 `native/` 侧另行走 §15 裁定后实施（**已登记为 O54**）。

## 11. sidecar 逐帧一份（**已实施，v3.70**）

> **状态：v3.64 定方案 → v3.70 实施**（与 §9 的无损落盘、§5 的命名同批落地）。
> **实施口径**：sidecar 与图像**同主干名** —— `YYMMDD_<场景2位>_<帧序号>_<档位>.json`。

### 11.1 c 问题的本质（旧实现）

旧实现**每场景只写一份 `S001.json`**，且 `do_capture()` **每次采集都整份重写**它。⇒
同场景先采 `gt` 后采 `dark` 时，**后写的那份覆盖前一份**：

| 现象 | 是否丢失 |
|---|---|
| 两张图（旧 `S001_dark.jpg` / `S001_gt.jpg`） | ✅ **不丢**——各写各的文件 |
| sidecar 里的 `kind` / `pair` / `sensor_ctrl` / `time` / `file` | ❌ **只反映最后一次采集** |

**为什么这是真问题（不只是"不整齐"）**：sidecar 里**逐帧不同**的字段恰恰是关键的几个——
`time.mono_ns`、`sensor_ctrl`（曝光/增益回读，**暗帧与亮帧可能不同**）、`file`（文件名/字节数）、
`kind`。**覆盖后，先采那一帧的这四项信息永久丢失**，且**无法从图像反推**。

**放大风险**：§10 的 a 方案一旦落地，`isp_fingerprint` 也会逐帧写入。若指纹逐帧相同（同场景同 ISP 设置）尚可接受；
但若采集中途有人改了 ISP/光照，**覆盖会掩盖这次漂移**——而这正是 §9.5 要"杜绝"的东西。

### 11.2 选定方案：逐帧一份 sidecar（**已实施**）

```
260919_01_0001_dark.nv12  +  260919_01_0001_dark.json   ← 暗帧及其 sidecar
260919_01_0001_gt.nv12    +  260919_01_0001_gt.json     ← 亮帧及其 sidecar（同帧序号 ⇒ 一对）
```

- **命名规则**：与图像**同主干名、仅换后缀**（`.nv12` → `.json`），一一对应。
- **写入时机**：`do_capture(kind)` 只写本帧那份 json，**不再触碰其它帧的文件**
  ⇒ **天然消除覆盖**，且**天然支持单帧重采**（重采某一帧不影响别帧的 sidecar）。
- **场景级公共字段的冗余**：`scene_id` / `pair_id` / `capture_batch` / `capture_condition`（设备/分辨率/管线）
  在两份里**重复出现**——这是**刻意的冗余**：换取"单文件自洽"（任取一份即可完整解读该帧），
  避免解析时还要去猜"同场景的另一份在哪"。

### 11.3 兼容性处置（关键决策点）

**现状**：尚无实拍数据（M1 采集未开工，O31 残留「摆正」未关）⇒ **不存在需要迁移的历史数据**。

⇒ **选定：直接切换，不做兼容**。
- **不保留** `S001.json` 单份形式，**不写兼容读取分支**（规则 `00-ssot.md` §3.1 精神：不留双轨）。
- **前提复核**：若切换时已有数据产出，则须先迁移再切换。**当前前提成立**（零数据）。
- **`.capture_state.json` 不受影响**——它是**采集进度**（下一场景号/批次/计数），
  与逐帧元数据是两回事，**继续每场景一份**。

### 11.4 与 a / b 的实施顺序

三项（**a** ISP 指纹 / **b** 无损 NV12 / **c** 逐帧 sidecar）**互相独立**，但**建议一次实施**：
- 三者都改 `capture_app.py` 的**同一处采集路径**（`do_capture` / `_sidecar` / gst 管线），
  分批实施 = **三次改动同一函数 + 三次上板验证**。
- **c 是 a 的前置**——a 落地的 `isp_fingerprint` 是**逐帧字段**，
  若仍在单份 sidecar 里，会**加剧覆盖问题**（见 §11.1 末段）。
⇒ **实施顺序：c → a → b**（c 最简、且为 a 铺路；b 改动 gst 管线最重、放最后）。

### 11.5 本方案不解决的

**d（无照度录入 UI）** 仍开放。另：**照度是逐帧字段**（同场景暗/亮帧的照度不同），
故 d 落地后**同样受益于本方案的逐帧结构**——**本方案是 d 的前置**。

## 12. 光照指标的替代方案（**原 d 项已放弃，改为此方案**）

> **状态：2026-09-19 用户决定「放弃照度的人工录入」，改为「传感器侧相对指标 + 一次批次级标定锚点」**。
> **本方案纯采集侧、不碰板**；**✅ 已实施（2026-09-29，G3 补齐）** —— 逐帧 `frame_y_mean` + 批次
> `lux_anchor` + `light_source.*`（见 §6 / §12.5）。**原"d = 无照度录入 UI"作为一项遗留就此取消**——
> **不是"未做"，而是不做**（用户明确放弃人工逐张录入）。

### 12.1 为什么放弃人工录入（而不是"以后再说"）

照度人工逐张录入的成本是**每张一次手工输入**（本工具一场景两帧 = 每场景两次），
在几十~上百场景的采集里是**持续的操作负担与出错来源**（漏录、录错、单位混淆）。
用户选择放弃，改以下述**零人工**方案达成**同一目的**（光照水平可复现、可比较）。

### 12.2 一个必须先澄清的技术矛盾（**重要**）

**直觉方案是错的**：`exposure × analogue_gain` 看似可当光照度量（§10 的 a 方案已确认能读回，实测 1350 / 161）。
**但 §9.5 要求「固定曝光 1/30 s + 手动增益 + 关 AE + AWB 锁定」**——**曝光与增益在采集期恒定**
⇒ **该乘积不随光照变化，不能作光照指标**。

⇒ 修正后的分工（**两者都记，但作用完全不同**）：

| 字段 | 作用 | 判据 |
|---|---|---|
| **曝光 / 增益回读** | **效度检查**（**不是**光照度量） | 证明 §9.5 的"固定曝光 + 手动增益"契约**确实被遵守**；**若批次内漂移 ⇒ 契约被破坏 ⇒ 该批数据作废** |
| **帧亮度均值（luma mean）** | **相对光照指标** | 固定曝光增益下，**光照水平直接体现在画面亮度上** |

### 12.3 相对光照指标 = 帧亮度均值

- **来源**：采集帧 NV12 的 **Y 通道均值**。**零额外成本**——数据本就在手（§9 的无损 NV12 路线下更直接）；
  且主基准 §3.4 的 OSD 已算"原图 mean"，**口径一致**。
- **记法**：**逐帧**（故**依赖 §11 的逐帧 sidecar 结构**——§11 是本方案的前置）。
- **同场景内有效**：同场景暗帧/亮帧的**均值比** = **光照比**（场景内容相同 ⇒ 反射率因素抵消）。
- **⚠️ 诚实的局限**：帧均值与**场景反射率混淆**（同一光照下黑墙 vs 白墙均值不同）
  ⇒ **跨场景的均值不可直接比较**；跨场景光照比较**只能靠 §12.4 的锚点**。**该局限必须随结论声明。**

### 12.4 批次级标定锚点（保留绝对值，支撑"~1 Lux"主张）

**做法**：每个 `capture_batch` 开始前，在**参考条件**下**同时**记录：
1. **实测照度**（照度计，或**手机 lux app**——**只需一次**，非逐张）
2. **同条件下的帧亮度均值**（顺带记录）

⇒ 二者构成该批次的**换算锚点**，写入**批次级**字段（非逐帧），供后续把相对均值折算到 Lux 量级。
⇒ **既免去逐张录入，又保住"~1 Lux"主张的可自证性**。

### 12.5 落地要点（**已实施，2026-09-29**）

- **帧均值计算**：在 `reader()` 里对 NV12 的 Y 平面求均值。**注意性能**——1920×1080 的 Y 平面是 2,073,600 字节，
  逐字节求和是**每帧 O(2M)**；**建议降采样**（如隔行隔列取 1/16，约 13 万样本）——均值精度足够，
  且与 §3.4 的 OSD 降采样口径一致（**O44** 的 4× 降采样可参照）。
- **批次锚点录入**：**仍需一次人工输入**（一个数字 + 单位），但**每批次一次**而非每张一次。
  可作为启动参数（`--lux-anchor 1.2`）或页面上的一个输入框——**实施时定**。
- **不新增 config 键**：本工具只读、不写 config（规则 `20-contracts.md` §1）。
- **与 a 的交叉**：`sensor_ctrl` 回读在 a 方案里已列为必取项（`v4l2-ctl --get-ctrl`，**✅ 已实测通过**）
  ⇒ **本方案不新增取数负担**，只是**重新定义其用途**（从"光照度量"改为"效度检查"）。

### 12.6 连带影响（已在文档侧同步）

- `docs/design/dataset.md` **§4.4** 已改写（新增 §4.4.1~§4.4.4，含本技术矛盾的澄清与局限声明）；
  **§6.1** 的「照度」行已改为「光照」行（逐帧均值 + 批次锚点）。
- 主基准 **O45** 的第一子项由"照度级清单"改为"**光照档分级 + 标定方式**"。
- **原 d 项取消** ⇒ 遗留清单由 **a/b/c/d 四项**变为 **a/b/c 三项**（详见 §13）。

## 13. 遗留清单（当前口径）

> **2026-09-19 更新：原 4 项遗留 a/b/c/d → 现 3 项 a/b/c**（d 已由 §12 方案取代并取消）。
> **（v3.68）新增遗留项 e**——由 v3.67 裁定 O45（"3 个光照档每档均有覆盖"）派生。
> **（v3.69）e 的适用范围收窄为 U4 评测集**（**U1 校准集豁免三档**，裁定 O59）；**新增 §13.1 能力缺口清单（G1~G5）**。
> **（v3.70）b / c 已实施**（无损落盘 + 逐帧 sidecar + 新命名，同批落地）⇒ **遗留项只剩 a 与 e**。

| 项 | 内容 | 方案章节 | 状态 |
|---|---|---|---|
| ~~**a**~~ | ~~sidecar 的 ISP 指纹 / CSC / 传感器 ctrl 为 `null`+`_pending`~~ | §10（ISP 指纹采集） | **✅ 已实施（2026-09-29，G5）**——改为**内联**实现（非 §10.3 拟议的独立模块） |
| ~~b~~ | ~~采集为有损 JPEG、GT 抬 PSNR 基线~~ | §9（无损 NV12） | **✅ 已实施（v3.70）**——落盘改裸 NV12，JPEG 仅供预览 |
| ~~c~~ | ~~sidecar 每场景一份、覆盖导致逐帧信息丢失~~ | §11（逐帧一份） | **✅ 已实施（v3.70）**——与图像同主干名 |
| ~~d~~ | ~~无照度录入 UI~~ | §12（光照替代方案） | **已取消**——用户放弃人工录入 |
| **e** | **光照档标注与"每档覆盖"的机械核验缺失**（**v3.68 新增**；**= 能力缺口 G3**）：v3.67 裁定 O45 要求实拍集的 **「3 个光照档（0.5 / 1 / 2 Lux）每档均有覆盖」**——**（v3.69 裁定 O59）该要求只适用于 U4 评测集，U1 校准集豁免**。但**本工具当前无任何光照档字段**——命名只有场景号/帧序号 + 档位，`.capture_state.json` 只记场景号/帧槽/计数。⇒ **"每档覆盖"无法被机械核验**；档位判定须依赖 §12 的**帧均值 + 批次锚点**（**该方案亦未实施**）。**依赖链**：e 的落地 = §12.3（帧均值） + §12.4（批次锚点）实施 + 一个"按锚点折算档位并落 sidecar"的步骤 | §12（光照替代方案） | **✅ 已实施（2026-09-29，G3）**：逐帧 Y 均值 + 批次锚点 + 光源字段均落 sidecar；**U1 不需要**（v3.69） |

**实施顺序（v3.70 更新）**：**~~c → a → b~~ 中的 b 与 c 已随命名改造同批落地**（§5 / §9 / §11）；
**剩余 = a（ISP 指纹，受 O54 阻塞）**；**e 建议紧随 §12 一并做**（同属"光照"一族，且 U4 的出口要求依赖它）。

> **（v3.76）新增前置工具**：**§14 的 `isp_owner.c`**（采集期自持 ISP、固定曝光/增益）**已落地并板上实测** ⇒
> **§9.5 的"关 AE"阻塞解除**；**它与遗留项 a 的关系**——`isp_owner` 只**固定曝光/增益**，
> **不产出 sidecar 的 `isp_fingerprint` / `sensor_ctrl`**（该两组仍为 `null` + `_pending`，受 O54 阻塞）。

> **（v3.70）** 本目录 §9~§12 的暂缓理由（并行会话占用板资源）**早已消失**，命名 + 无损落盘 + 逐帧 sidecar
> **已于 v3.70 实施并离线自测通过**；**（v3.71）板上实测已完成并通过**（证据 `docs/evidence/M1-collect-tool-board.md`；
> 遗留未验项见该档 §6：路② >3 min 长跑、U1 完整动作序列、真实采集目录落盘）。

**残留的外部依赖（未在本目录内解决）**：
- **O54**（CSC 显式化与取值）——位于 `native/rga_core.c`，是**交付链路**，须另走 §15 裁定后实施；**阻塞 a**（CSC 指纹）**与"离线 RGB 转换工具"的正确性**（**b 的板上部分已于 v3.70 实施**，但**离线转换仍须 O54 定 CSC / range**，否则训练域 ≠ 部署域）。
- **需板上核实的项**（§10.4 三项 + §12 的批次锚点取数）——**（v3.68）板资源已释放**，可开工核实。
- ~~**O59**（U1 的 50 张来源未定）~~ ——**已于 v3.69 裁定**（**来源 = IMX415 另采、≥46/计划 50、U1 豁免三档**）；**不属本目录**（是采集计划的分配问题，落点 = `docs/design/dataset.md` §5.2.2 / §5.2.8）。
- **O60**（本目录文档归属 + `tools/collect_calib.py` 命名）——**登记/架构类**，见 §8。

### 13.1 能力缺口清单（**采集子系统能否满足采集计划**；2026-09-19 核查）

> **结论（2026-09-29 更新）**：**G1 / G2 已于 v3.70 实施、v3.71 板上实测通过**（真机双 `fdsink` 共存、路② 帧长 3110400、命名/帧槽/逐帧 sidecar 落盘、裸帧解码目视；证据 `docs/evidence/M1-collect-tool-board.md`）；
> **G5 已于 2026-09-29 实施**（ISP 指纹 **7 子项**（**v3.86 追加 161 更正 8 → 7**）+ `sensor_ctrl` 回读入 sidecar；见主基准 §14 **追加 152**）；**G3 已于 2026-09-29 实施**
> （逐帧 Y 均值 + 批次照度锚点 + 光源字段）；**U1 不需要 G3**（v3.69 裁定 O59）；**G4 沿用现状**即可。
> ⇒ **M1 采集开工前的必办缺口已清零（G1/G2/G3/G5 全落地）**。

| # | 缺口 | 现状（代码 / 文档依据） | 阻塞 M1 采集？ | 处置 |
|---|---|---|---|---|
| ~~G1~~ | ~~文件名无帧序号 ⇒ 同场景同档位重复采集即覆盖~~ | 旧实现落盘名无帧序号、同名即覆盖 | ~~是~~ | **✅ 已实施（v3.70）**：命名改 `YYMMDD_<场景2位>_<帧序号>_<档位>.nv12`（见 §5） |
| ~~G2~~ | ~~无无损落盘（落的是有损 JPEG）~~ | 旧采集源 = `STATE['jpg']`（有损） | ~~是~~ | **✅ 已实施（v3.70）**：gst `tee` + 双 `fdsink`，路② 写裸 NV12（§9） |
| ~~**G3**~~ | ~~**无光照档标注与批次锚点** ⇒ "**每档覆盖**"无法机械核验~~ | 命名只有场景号 + 档位；`.capture_state.json` 只记场景号 / 批次 / 计数 | ~~U1 否 / U4 是~~ | **✅ 已实施（2026-09-29）**：逐帧 `illuminance.frame_y_mean`（**相对档位**由此可机械核验）+ 批次 `lux_anchor`（绝对刻度）+ `light_source.*`（含**调光方式**，与色温漂移判别相关）。方案见 §12.3/§12.4；实现 = `capture_app.py` 内联（**未新增文件**） |
| **G4** | **`split` 字段留空** ⇒ U1/U2/U3/U4 用途分离靠场景号人工记账 | sidecar 有 `split` 字段但标"采集期不定、划分时回填" | 否 | **可接受，沿用现状**（须登记） |
| ~~**G5**~~ | ~~`isp_fingerprint` / `sensor_ctrl` 为 `null` ⇒ 校准集溯源不完整~~ | sidecar 的 `_pending` 清单 | ~~是~~ | **✅ 已实施（2026-09-29）**：`isp_fingerprint` 扩为 **7 子项 × 每 `scene_isp30`**（**v3.86 追加 161 更正 8 → 7**）、`sensor_ctrl` 改为采集时刻 `v4l2-ctl` 回读、`csc` 判 **N/A**（理由入 `_not_applicable`）。实现**内联于 `capture_app.py`**（未新增 `isp_fingerprint.py` 模块，保持自包含单文件部署）；`config.yaml` 的 `isp.fingerprint` 7 子项作**自检对照**（子树哈希逐位一致；**其注释仍写"8 子项"，因本轮硬约束不改 `config.yaml`，已登记于 §14 追加 161**）。**与 O54 的关系已澄清**：O54 只阻塞「离线 RGB 转换工具的 CSC/range」，**不阻塞本工具** |

**与"无损 → 开发机 → 转格式"路线的衔接**：G2 落地后**板上只写裸 NV12**，**RGB 转换留作开发机侧离线工具**——该工具**必须用与 RGA 完全相同的 CSC / range**（否则**训练域 ≠ 部署域**，直接污染 PSNR/SSIM 结论），故 **G5（CSC 显式化，O54）是该离线工具的正确性前提**。

**已核对、无冲突**：本工具**不读写 `config.yaml`、不发出任何日志 `event`（0 处）、不涉及 §5 返回码** ⇒ 与规则 `20-contracts.md` 三张白名单无冲突；**与主链路 TH1 默认同一 `/dev/video11` ⇒ 互斥**（§2）。

## 14. `isp_owner.c` —— ISP 3A 自持程序（**v3.76 新增、已板上实测；v3.77 改判为调试工具**）

> **地位**：板上辅助程序（**非交付链路**）。**依据** = 主基准 §9.5「固定 ISP 参数与曝光」。
> **板上实测证据** = `docs/evidence/M1-collect-tool-board.md` **§10**；**主基准 §14 v3.76 / §15 O62 补记 / O63**。
>
> ⚠️ **（v3.77 改判）承载方式已改为「纯 IQ 路径」（见 §14.6），本程序自此降为调试工具**——**不再作为采集/演示的 3A 承载**（**不须再停 `rkaiq_3A.service`**）。其**已实测结论**（钉范围口径 / `setExpMode` 失效原因 / 单位 / 效度检查须用 subdev 回读）**仍是 §9.5 的有效依据**。

### 14.1 为什么需要它

曝光/增益**不在 V4L2 节点上**：`/dev/video11` 只有 `pixel_rate`；sensor subdev（`/dev/v4l-subdev2`）
虽有 `exposure` / `analogue_gain`，但**没有 `auto_exposure` 控制项** ⇒ **AE 完全由 rkaiq 驱动**，
只能经 rkaiq 的 API 关闭。而 rkaiq 的 uAPI2 是**进程内** API（需 `rk_aiq_sys_ctx_t*`），
跨进程只有**未公开的 socket 报文通道**（`MessageParser.hpp` 板上缺失、`__RkAiqSocketPacket_s`
不在 DWARF）⇒ 本程序**自行持有 ISP 上下文**。

**代价（硬约束）**：采集期间 **`rkaiq_3A.service` 必须停止**（同一 ISP 只能有一个持有者）。
**恢复** = `sudo systemctl start rkaiq_3A.service`，或重启板子。
**（v3.77）本代价仅在使用本程序时成立**——改走 §14.6 的纯 IQ 路径后**不须停服务**（这正是改判的理由）。

### 14.2 构建与用法

```bash
# 板上构建（须以系统工具链；-Wall -Wextra 零告警）
cd ~/starlight/tools/collect
gcc -O2 -Wall -Wextra -I/usr/include/rkaiq \
    $(for d in /usr/include/rkaiq/*/; do printf -- "-I%s " "$d"; done) \
    -o isp_owner isp_owner.c -lrkaiq

# ① 只读探测（不改任何状态；打印当前 AE 模式与曝光/增益范围后即退出）
sudo ./isp_owner /dev/video11 --probe

# ② 固定曝光/增益（停 3A 服务后；--gain 必填，单位 = 线性倍数）
sudo systemctl stop rkaiq_3A.service
sudo ./isp_owner /dev/video11 --exp-us 15000 --gain 4.0     # 前台；Ctrl-C / SIGTERM 退出

# ③ 取流中回读（**效度检查只能走 sensor subdev**）
v4l2-ctl -d /dev/v4l-subdev2 --get-ctrl exposure,analogue_gain
```

| 参数 | 说明 |
|---|---|
| `<video-node>` | ISP 输出节点，如 `/dev/video11` |
| `--probe` | 只读：打印 `getExpMode` / `getExpTimeRange` / `getExpGainRange`，**不改状态**，随后退出 |
| `--exp-us N` | 固定曝光时间，单位**微秒**（默认 33333 = 1/30 s，见 §9.5） |
| `--gain G` | 固定总增益，单位**线性倍数**；**apply 模式必填**（§10.4 `camera.gain_*` 未定稿，**不由程序臆造取值**） |
| `--manual` | **调试口**：额外 `setExpMode(OP_MANUAL)` + AE lock——**实测会失控，勿用于采集**（见 14.3） |
| `--wb-watch` | 每 250 ms 打印 AWB 运行态（生效增益 / CCT / `awbConverged` / 统计增益 / IQ manual 预设）——用于观测 AWB |
| `--awb-lock` / `--awb-lock-delay S` | 从 **READY 起算**延时 `S` 秒后 `rk_aiq_user_api2_awb_Lock`（默认 5）。**⚠️ 必须在此期间取流**——AWB 只在处理帧时更新，不取流则锁住的是 IQ 初值而非收敛值。**效果未验证**（见 14.5） |

**必须 root 运行**：`librkaiq` 的 `init` 会 `fopen("/tmp/aiq%d.lock","w+")` 后**不检查 NULL** 就调 `fileno()`；
该文件属 `root:644` ⇒ 以普通用户跑会 **`fileno(NULL)` 段错误**（librkaiq 缺陷）。

### 14.3 三条关键实测口径（**踩坑留档**）

1. **「关 AE」的正解 = 把 auto 范围钉成一点**（`setExpTimeRange` / `setExpGainRange` 传 `min == max`，
   **不切模式**）。**`setExpMode(OP_MANUAL)` 是反向的**——切 manual 后 AEC 改用 **IQ 的 `stManual` 默认值**
   （5 点扫描实测恒为 `exposure=405 / analogue_gain=0`，**与请求值无关**）。
2. **单位**：曝光 = **秒**、增益 = **线性倍数**（`paRange_t` 字段序为 `{max, min}`）；
   sensor 模拟增益寄存器 = `20·log2(gain)/0.3`（0.3 dB 步进）；**曝光行数 = `exp_us / 7.433`**（行时 **7.433 µs**、行率 **134.5 k lines/s**；**v3.77 更正**，原写 `exp_us × 0.09` **错**——两组独立测点吻合：`5000 µs → 675 lines`、`15000 µs → 2025 lines`。**（v3.79 再更正行时）**：由帧周期反推三个测点 **7.4317 / 7.4355 / 7.4326 µs** ⇒ **7.433 µs**，**v3.77 的 7.41 µs 偏低 0.3%**；证据 = 证据档 **§12**）。
3. **效度检查只能用 sensor subdev 回读**——`getExpTimeRange` / `getExpGainRange` **不反映设定值**
   （设定 `0.015 s / 4.0×` 后仍返回 `0.010 / 256..1.0`）。

**已知限制（登记 O63；数值与成因已于 v3.77 更正）**：**曝光上限分两档**——**AE auto 档 = 1350 lines = 10.0 ms**
（= `getExpTimeRange.max`，是 **AE 策略自身**的上限，**不是** 60 fps 钳制）、**manual 档 = 2242 lines ≈ 16.6 ms**
（= sensor `exposure` 的 max，受 **sensor VMAX** 限制）。⇒ **§9.5 的「固定曝光 1/30 s」须待主基准 §11 M1 出口标准 #1「暗光 1080p@30 稳定」落实后方可达**；在此之前采集的曝光值定在**可达上限**
（**manual 档 ≈16.6 ms，比 AE auto 档多 66% 进光量**）并随 sidecar 记实回读。
**（v3.79 收口）**：**1080p@30 锁定已落地并板上验证**（见 **§16**）⇒ **1/30 s 可达**（30 fps 下 sensor `exposure max` = **4493 行**）；
但**除物理 30 fps 外还须 IQ 帧率档 `FpsValue=30`**（否则 AE 按帧率把曝光钳到 **2224 行**，见 §15.2）。证据 = `docs/evidence/M1-collect-tool-board.md` **§12**。

### 14.4 与 `capture_app.py` 的关系（**未集成**）

本次为**手工分步验证**（停服务 → 起 `isp_owner` → 取流 → 回读）。**与 `capture_app.py` 的自动集成未做**
——采集流程中"自动停/起 3A 服务 + 传曝光/增益"仍待实现（M1 采集开工前办）。

### 14.5 AWB 锁定（`--awb-lock`）的验证状态（**v3.76：未验证，登记 O64**）

**结论：`--awb-lock` 的效果既未被证实、也未被否证。** 依据 = `docs/evidence/M1-collect-tool-board.md` §10.6。

- **结构上它是真锁**：`rk_aiq_user_api2_awb_Lock` → `RkAiqAwbHandleInt::lock()` → `rk_aiq_uapiV2_awb_Lock(algoCtx)`；
  调用 `rc=0`，且**不改变** `wbMode`（恒为 `OP_AUTO`）。
- **可观测量有效**（已交叉验证）：`QueryWBInfo.gain` 是**实时生效值**——取流前 `stat_gain_glb=0`（LP 滤波未起）、
  取流后收敛到同值；且它与 IQ 的 manual 预设（`r=1.3270 b=3.3143`）明确不同。
- **但当前场景无判别力**：AWB 的生效增益从 `LV=0` 到收敛后（`LV=54/57`）**逐位不变**，
  改总增益档（10× vs 16×）后**仍逐位相同**，图像域 UV 通道 spread 仅 ~6 ⇒ **AWB 本就无需修正**
  ⇒ 锁与不锁**读数一致**。
- **纯软件无法构造扰动**：`setMWBGain` 内部会 `SetWpModeAttrib` **切到 manual**，而 manual 下 AWB 不运行
  ⇒ 构造不出"让 AWB 持续追变"的输入。
- **要判定需**：**(i)** 板前改变光源色温（推荐；**这正是 M1 采集范式会遇到的真实情形**——调亮/调暗光源同时改变色温），
  或 **(ii)** 软件注入错值后回 AUTO 看是否拉回（需先验 `setWBMode` 是否清除 lock）。
  **复现命令见证据档 §10.6(g)**；**判据 = 扰动后生效增益随之改变 ⇒ 未锁住；保持不动 ⇒ 锁住**。

---

## 14.6 纯 IQ 路径：推荐的「关 AE + 固定曝光/增益」承载方式（**v3.77 新增、已板上实测；v3.78 已工具化 ⇒ 正式流程见 §15**）

> **地位**：**替代 §14 自持进程的承载方式**，主基准 §9.5 ⑥ 已裁定为**演示链路（M2）与采集链路同构采用**；
> `isp_owner.c` **降为调试工具**。**证据** = `docs/evidence/M1-collect-tool-board.md` **§10.7**。

**原理**：IQ 的 `main_scene[0]/sub_scene[0]/scene_isp30/ae_calib/CommCtrl/` **本就有** AEC 工作模式开关与手动档——
只需把 `AecOpType` 由 `RK_AIQ_OP_MODE_AUTO` 改为 **`RK_AIQ_OP_MODE_MANUAL`**，并把
`AecManualCtrl/LinearAE/{TimeValue, GainValue, IspDGainValue}` 设为目标值
（三个 `Manual*En` **本来就是 1**）⇒ **`rkaiq_3A_server` 启动即固定曝光/增益**。

**板上实测**（含备份 / 还原，IQ 原哈希 `4536f96a…` 逐位还原）：`TimeValue=0.015` + `GainValue=4.0`
⇒ 回读 **`exposure=2025` / `analogue_gain=40`**，**重启服务后两轮逐位一致**；`TimeValue=0.033333`（1/30 s）
⇒ **2224**（被 manual 档上限 2242 钳制）。对照 = AE auto 同场景 **5 次读数 gain = 67/93/126/132/139，每次不同**。
**优点 = 不动 `rkaiq_3A.service`、零新增进程**，与 O62 的两套 IQ **天然同构**。

**厂家 schema（一手资料 = 板上头文件 `aec_head.h` / `aec_uapi_head.h`）**：`TimeValue` **单位 = 秒**（range [0,1]，
默认 0.003）、`GainValue` / `IspDGainValue` = **线性倍数**（range [1,4096]，默认 1）；`RK_AIQ_OP_MODE_MANUAL = 2`；
**三个增益级要完全钉死须三个都设**（sensor 模拟 `GainValue` + ISP 数字 `IspDGainValue`）。

**实现后果（硬约束，登记 O62 ④(ii)/⑤ 与 O65）**：`rkaiq_3A_server` **硬编码 `/etc/iqfiles/` 且仅支持 `--silent`**
（板上实测）⇒ **两套 IQ 无法共存于同一 sensor 名** ⇒ 必须**换文件 + `systemctl restart rkaiq_3A.service`**，
且**采集工具启动时须校验"当前 IQ = 采集档"**（防"忘切"；v3.76 因自持而免除的防呆**随之恢复**）；
**换档重启对演示的扰动未验**。

**取值 SSOT（规则 `00-ssot.md` §3.1）**：固定曝光/增益的**值只写在 IQ 文件里**；§10.4 的 `camera.exposure_us` /
`camera.gain_*` **不再单独存值**，改记「IQ 文件名 + sha256 + sensor subdev 实测回读」。

**命令速查**（改副本 → 换文件 → 重启 → 回读 → 还原）：

```bash
F=/etc/iqfiles/imx415_CMK-OT2022-PX1_IR0147-50IRC-8M-F20.json
sudo cp $F /tmp/iq_backup.json
python3 - <<'PY'
import json
d=json.load(open("/tmp/iq_backup.json"))
c=d["main_scene"][0]["sub_scene"][0]["scene_isp30"]["ae_calib"]["CommCtrl"]
c["AecOpType"]="RK_AIQ_OP_MODE_MANUAL"
l=c["AecManualCtrl"]["LinearAE"]; l["TimeValue"]=0.015; l["GainValue"]=4.0; l["IspDGainValue"]=1.0
json.dump(d,open("/tmp/iq_manual.json","w"),indent=2)
PY
sudo cp /tmp/iq_manual.json $F && sudo systemctl restart rkaiq_3A.service && sleep 6
sudo v4l2-ctl -d /dev/v4l-subdev2 --get-ctrl exposure,analogue_gain   # 效度检查（只认 subdev 回读）
# 还原：sudo cp /tmp/iq_backup.json $F && sudo systemctl restart rkaiq_3A.service
```

> **（v3.78）上例是"手工一次性"的做法，仅用于机制验证。正式流程请用 §15 的 `iq_sets.py`**
> ——它把上述步骤固化为可复现的子命令，并产出 manifest 供防呆与指纹使用。

---

## 15. `iq_sets.py` —— IQ 两套档位（采集档 / 演示档）的生成、校验与切换（**v3.78 新增，= O62 的实现**）

> **地位**：本目录的板上辅助工具（**非交付链路**）。**依据** = 主基准 §15 **O62**（v3.75 裁定两套 IQ、v3.77 定承载方式为纯 IQ 路径）与 **O40**（采集期关 3DNR）。
> **证据**：机制验证见 `docs/evidence/M1-collect-tool-board.md` **§10.7**；**本工具的板上实测未做**（仅开发机合成 IQ 自测）。

### 15.1 为什么必须换文件（而不是并存）

`rkaiq_3A_server` 按 **sensor 名**匹配 IQ，且**硬编码 `/etc/iqfiles/`**、**仅支持 `--silent`**
⇒ **同一 sensor 名下只能存在一份 IQ** ⇒ 采集档与演示档**必须换文件 + 重启 3A**。

### 15.2 两套档位的差异

| 档位 | 3DNR（`bayertnr_v2/TuningPara/enable`） | 用途 |
|---|---|---|
| **collect** | **0（关）** | U1/U2/U3/U4 取"纯净底"（O40） |
| **demo** | **保持出厂值（1，开）** | 运行期观感 / 降噪（§9.2、§3.1 v3.1 ⑧） |

**两档共同点**：`ae_calib/CommCtrl/AecOpType = RK_AIQ_OP_MODE_MANUAL` +
`AecManualCtrl/LinearAE/{TimeValue, GainValue, IspDGainValue}` 设为目标值
⇒ **`rkaiq_3A_server` 启动即固定曝光/增益**（**不动服务、零新增进程**）。
**（v3.79 追加）** 还须写 **`AecFrameRateMode = {isFpsFix: 1, FpsValue: fps}`**（由 `build --fps` 写入）——
否则即便物理已锁 30 fps，AE 仍按 sensor 默认 **60** 把曝光钳到 **2224 行**（**1/30 s 不可达**；证据 = 证据档 **§12**）。

> **（2026-09-29 / O64 收口）两档**还共同写 **WB 手动档**：`wb_v21.control.mode = CALIB_WB_MODE_MANUAL`
> + `manualPara.cfg.mwbGain = [1.32703, 1, 1, 3.31432]`（由 `build --wb-lock manual --wb-gain …` 写入）
> ⇒ **采集期 WB 确定性**（结构上 AWB 算法不运行）。**两档 sha256 已随之变更**：
> collect `81e61da4…` / demo `3a047b06…`（依据 = 主基准 §14 追加 152）。**除 `wb_v21`**
> 外其余子树逐位未变。**⚠️ 效力边界**：取值来源 = IQ **既有手动预设**（非本地场景标定）；
> 「改光源色温后 WB 增益不动」的板端观测**未做**。

### 15.3 用法（板上）

```bash
# 0) 部署（与 capture_app.py 同批）
ssh board 'mkdir -p ~/starlight/tools/collect'
scp tools/collect/iq_sets.py board:~/starlight/tools/collect/

# 1) 首次：把当前 /etc/iqfiles/ 的 IQ 固化为**出厂基线**（build 的输入、switch stock 的还原源）
sudo python3 tools/collect/iq_sets.py import-stock

# 2) 生成两套档位。**取值必须显式给出**（本工具不臆造取值）
sudo python3 tools/collect/iq_sets.py build --exp-us 33333 --gain 4.0 --fps 30
#    ⚠️ **（v3.79）`--fps` 为必填**——写 `AecFrameRateMode.{isFpsFix=1, FpsValue=fps}`；
#       缺它则即便物理锁 30 fps，AE 仍按帧率把 1/30 s 钳到 2224 行（证据 = 证据档 §12）
#    ⚠️ 固定曝光/增益的**最终取值 = M1 定稿项**（主基准 §15 O63）；**v3.79 起 O63 已收口**，
#       30 fps 锁定下 1/30 s 可达 ⇒ 两套 IQ 已按 33333/4.0 重建（旧测点 15000/4.0 见 §11 证据）

# 3) 只读：当前 IQ 是哪一档（= 防呆依据）
python3 tools/collect/iq_sets.py status

# 4) 换档（备份当前 → 复制 → 重启 3A → 回读校验 sha256）
sudo python3 tools/collect/iq_sets.py switch collect     # 采集前
sudo python3 tools/collect/iq_sets.py switch demo        # 演示前
sudo python3 tools/collect/iq_sets.py switch stock       # 还原出厂

# 5) 效度检查（**只认 sensor subdev 回读**；`getExp*` 不回读设定值）
v4l2-ctl -d /dev/v4l-subdev2 --get-ctrl exposure,analogue_gain
```

### 15.4 产物布局

```
/etc/iqfiles_sets/
├── stock/<sensor>.json     出厂基线（import-stock 落盘）
├── collect/<sensor>.json   采集档（3DNR 关 + AEC 手动档）
├── demo/<sensor>.json      演示档（3DNR 开 + AEC 手动档）
└── manifest.json           文件名 + sha256 + 生成参数（**v3.79 增 `fps` 与 `aec_frame_rate_mode`**）+ 回读值（schema `iqsets/v1`）
```

**`manifest.json` 是 `iq_sets.py` 与 `capture_app.py` 之间的唯一接口**（后者只读它，不 import 本工具）。

### 15.5 与 `capture_app.py` 的防呆关系（O62 ④(ii)）

`capture_app.py` 启动时比对 manifest 与 `/etc/iqfiles/` 当前文件的 sha256：
- **一致且 = collect** ⇒ 放行；
- **否则一律拒采**（**fail-closed**：manifest 缺失 / schema 不符 / 档位不可判定**也拒**）；
- `--no-iq-guard` 可绕过，但**会记入 sidecar**（`isp_fingerprint.iq_guard = 'bypassed'`）以便审计。

⇒ **采集会话的固定动作序**：`switch collect` → 起 `capture_app.py`（确认页面横幅为绿）→ 采集 → `switch demo`（或 `stock`）。

### 15.6 未验项与边界（如实登记）

1. ~~板上实测未做~~ → **已补齐（v3.78 追加）**：真实 IQ 上 `import-stock` / `build` / `status` / 防呆 / `switch` **完整闭环通过**（`collect` 回读 **`exposure=2025` / `analogue_gain=40`**、60 fps；`switch stock` 还原 `4536f96a…` 逐位一致）。**残留 = 两套 IQ 的取值仍是测点**（`15000 µs` / `4.0×`）——最终取值 = M1 定稿项（O63），定稿后须**重跑 `build`**。证据 = `docs/evidence/M1-collect-tool-board.md` **§11**。**（v3.79 收口）**：O63 收口 ⇒ 两套 IQ 已按 **`--exp-us 33333 --gain 4.0 --fps 30`** 重建（collect sha256 **`7e1ec9da…`**、demo **`968a636d…`**，均 `TimeValue=0.033333` / `GainValue=4.0` / `(isFpsFix,FpsValue)=(1,30)`）；`--fps` 自 v3.79 起**必填**。证据 = 证据档 **§12**。
2. **`switch` 的重启偶发失败（根因未定，登记 §12 R21）**——一次 `systemctl restart` 返回 0 但 `rkaiq_3A_server` 随退 ⇒ 服务 inactive。**已用 `is-active` 回读校验兜住**（未 active 即报错 + 给恢复命令 + 返回非零），**但不等于已消除**；换档后请确认输出含「服务 active」。
3. **`switch` 会重启 `rkaiq_3A.service`**（有副作用）——采集与演示本就互斥（§2），故**只在会话前后各一次**，不做运行期热切换（O62 ④(iii)）。
4. **换档重启对正在运行的取流者的影响未验**（须先停取流再换档；工具已加 `fuser` 前置告警）。
5. **`manifest.json` 的路径是部署约定**（默认 `/etc/iqfiles_sets/`）——`iq_sets.py` 与 `capture_app.py` 必须一致；改路径须同时改两处的参数/默认值。
6. **备份保留在 `sets_dir/.prev/`**（每次换档覆盖一份）——**不落在 `/etc/iqfiles/` 内**（旧版缺陷已修）；该目录可随时删，不影响 manifest。

---

## 16. `sensor_lock.py` —— 1080p@30 锁定（**v3.79 新增；仓库根共享模块**）

> **地位**：**仓库根共享模块**（**非交付链路**，但**由交付链路的 TH1 调用**）。**依据** = 主基准 §9.5 ④（1080p@30 锁定）+ §10.4 键表 `camera.vblank_lines`。
> **证据** = `docs/evidence/M1-collect-tool-board.md` **§12**；**登记** = 主基准 §10.3 + §14 v3.79（本文件 §8）。
> **本模块不 import 本目录任何文件**；采集工具**只调用它**（§2 的"除 `sensor_lock.py` 外不 import 其它文件"即指此）。

### 16.1 为什么需要它

IMX415 的帧周期 = `VMAX 行数 × 行时`，而 `VMAX = height + vertical_blanking`。设备树**只提供 1080p@60** 的
overlay（VMAX = 1080 + 1154 = 2234），且 `/dev/video11` **既不枚举帧率也不支持 `set-parm`**（`VIDIOC_G_PARM` = EINVAL，板上实测）
⇒ **锁定只能走 sensor subdev 的 `V4L2_CID_VBLANK`**。本模块把「解析 subdev → 写 VBLANK → **回读校验**」固化为可复现调用。

### 16.2 实测语义（**硬约束**，证据 = 证据档 §12）

| 语义 | 实测 |
|---|---|
| 锁定值 | `vertical_blanking = 3405` ⇒ VMAX = **4485 行** ⇒ **30.0 fps**（行时 **7.433 µs**） |
| 曝光上限联动 | **`exposure max = VMAX + 8`**（60 fps 时 2242、30 fps 时 4493）⇒ 1/30 s（4485 行）在 30 fps 下**可设** |
| **停流即复位** | 取流前写入保持；**停流后回读回 1154（60 fps）** ⇒ **每次启动都必须在 `STREAMON` 之前重施** |
| **必须在手动档下** | **stock 档（AE auto）** 流中 t=3 s 即把 vblank 写回 1154；**手动档（采集 / 演示档）** t=3…12 s 全程稳定 |
| 第二机制（**IQ 帧率档**） | 物理 VMAX 抬升**不足以**解锁 1/30 s——rkaiq AE（即便手动档）按**帧率**算上界：IQ `AecFrameRateMode = {isFpsFix: 1, FpsValue: 0}` 时 `TimeValue=0.033333` 被钳到 **2224 行**；**`FpsValue=30`** 后回读 **4474 行**（⇒ 见 §15.2 的 `build --fps`） |

### 16.3 用法

```bash
python3 sensor_lock.py --check                # 只读：运行时解析 sensor subdev + 回读当前 vblank
sudo python3 sensor_lock.py --apply 3405      # 写入 + 回读校验（须在取流前执行）
```
库用法：`import sensor_lock; rc = sensor_lock.apply(3405, logger=log)`（`0` = OK；`-1` = **`E_V4L2`**，**不新增返回码**）。
写入权限：板上 `/dev/v4l-subdev2` 属 **`root:video`** ⇒ **video 组成员可直接写**（TH1 自检即以用户 `cat` 身份通过）；否则需 root。

### 16.4 调用方（两处）

| 调用方 | 时机 | 失败处置 |
|---|---|---|
| **TH1 `capture.py`** | `Capture.init()` 在 `_n.capture.init()` 之后、`stream_on_once()` **之前** | `shutdown()` + RSM **`FAULT_EXIT`**（reason `sensor_lock_failed`） |
| **采集工具 `capture_app.py`** | 启动 gst **之前** `lock_guard()`（参数 `--vblank-lines` / `--sensor-subdev`，见 §3） | **fail-closed**：**拒绝采集**（预览 / 对焦仍可用）；页面顶部显示锁定横幅；`/stat` 新增 `lock` 字段 |

### 16.5 边界与未验项（如实登记）

1. **依赖手动档 + IQ `FpsValue=30`**（登记主基准 §12 **R22**）——**stock 档下两者同时失效**（vblank 被写回、曝光被钳到 ≈1/60 s）；
   缓解 = 采集工具 **fail-closed 防呆** + TH1 **强制项**。
2. **部署硬约束**：采集工具**必须同时部署仓库根的 `sensor_lock.py`**（按 `__file__` 反推仓库根，见 §3），否则**启动即拒采**。
3. **subdev 编号不写死**——判据 = 「**唯一暴露 `V4L2_CID_VBLANK` 的 v4l-subdev**」（板上 = `/dev/v4l-subdev2`，`m00_b_imx415 1-001a`；其余 4 个均无此控制）。
4. **长跑（>10 min）稳定性未测**；**演示链路（`main.py`）整链未跑**（证据档 §12.7）。
