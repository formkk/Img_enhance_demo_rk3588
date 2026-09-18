# 证据：MIPI 屏 ZDE++ 全流程打通（pipeline_probe 首版）

| 项 | 内容 |
|---|---|
| 地位 | **C 层记录类**：一次实时链路打通实验的实测结果与判定；记录 `tools/pipeline_probe.c`（首版单体 C 探针）与屏上出图结果 |
| 效力 | **非验收依据**（规则 `00-ssot.md` §3.5）；验收只认主基准 §13 复现方式 + §11.2 回归项 + `tools/bench.py` 报告。本实验的耗时/帧率数据**不构成 §6.1 / §13 达标依据**（M0 未出口 + 用户豁免记账见主基准 §15） |
| 对应主基准节 | §3.1、§3.2、§3.3、§3.4、§5（启动序 / 时间戳）、§6.1、§9.1（EMA）、§15（**新登记用户豁免** + O42 / O43 实测回填） |
| 工具 | `tools/pipeline_probe.c`（新增，板端原生编译） |
| 实验时点 | **2026-09-18 22:4x–23:1x CST**（板 192.168.0.104，`cat@`，SSH 别名 `board`） |
| 实验方式 | 板上原生编译 + 采集 / RGA / NPU / 曲线应用 / DRM 全链路实时出屏；期间 `mult-user.target` 无桌面；结束恢复 `graphical.target` |
| 状态 | `draft` |

> 本档只记录实测事实与判定。**本轮不改 config、不改递送链路（capture.py 等未建）、不动 ISP/IQ**——`pipeline_probe.c` 是探针，按工具惯例登记 §10.3，M2 再按 §10.3 拆分。

## 1. 目的

用户指令（2026-09-18）：**「先实现 MIPI 屏上 ZDE++ 网络的全流程实现，画质相关的在打通全流程后再进行优化。」**

此前 ZDE++ 链路的四段（RGA 几何 / NPU 曲线估计 / 8 iter 曲线应用 / DRM 显示）各自在板上单独实测过，但**彼此独立、都吃文件、未串成实时链路**。全仓库经排查无任何裸 V4L2 采集代码、无 DRM/KMS 代码、无主链路骨架。本实验写一个**单体 C 探针**把四段串成一条实时链路并在 7" MIPI 屏出图，为 M2 按 §10.3 拆分 `capture.py`/`curve_estim.py`/`realtime_apply.py`/`display_mipi.py` 提供可复用、已验证的代码基线与函数级打点。

## 2. 设计与实现要点

- 链路 = 采集 NV12 1920×1080 → RGA 缩放到有效区 480×270（落 480×272 缓冲第 1..270 行，pad 零填）→ ZDE++ **INT8 双条带 core0+core1**（切片模型 `zde_curve_256x272_int8_tk232.rknn`，HALO=7）→ 拼接成 3ch 场（取有效区 480×270）→ **EMA（α=0.63，每 16 帧估计一次）** → RGA NV12→512×288（原图窗）→ **8 iter 单遍融合 NEON 曲线应用** → 512×288 增强窗 → 画布 1024×600（左原图 / 右增强）→ **DRM DSI pageflip**。
- **缓冲与格式**：画布用 **RGB888（DRM_FORMAT_RGB888）** 而非 XRGB8888——理由见 §5.2：RGA 的 `RK_FORMAT_RGB_888 → RK_FORMAT_RGB_888` 是同格式 blit（M0-13 已验证），完全规避"RGA 能否一次完成 RGB888→XRGB8888"这一未验证项；`drmModeAddFB2` stride=3072（1024×3）。
- **画布由 E 层走 cma，中间缓冲走 system-dma32**；V4L2 用 `VIDIOC_EXPBUF` 导出 dmabuf 直喂 RGA（**零拷贝**），`rga_blit_nv12` 失败才退 staging 拷贝。
- **V4L2 MPLANE 坑**：`v4l2_buffer.length` 在 MPLANE 下 = plane 个数（=1），不是字节数；`VIDIOC_EXPBUF.plane` 必须显式写 0；非阻塞 + `poll()`。全部实测通过（见 §3.1）。
- **流程纪律**：探针**不吃 config.yaml、不新增 config 键/日志 event/返回码**；CLI 参数驱动（`--offline` / `--display` / `--smoke`）。

## 3. 实测

### 3.1 V4L2 采集（`/dev/video11`，MPLANE）

```
[cap] driver=rkisp_v6 card=rkisp_mainpath caps=0x84201000
[cap] fmt=NV12 1920x1080 num_planes=1 bytesperline=1920 sizeimage=3110400
[cap] REQBUFS count=4
[cap] 4 缓冲就绪（length=3133440，dmabuf_fd[0]=4）
```

- `VIDIOC_S_FMT` → `G_FMT` 回读：NV12 1920×1080、`num_planes=1`、`sizeimage=3110400`。
- `REQBUFS(MMAP, 4)`、`QUERYBUF`（`length=1`）、`EXPBUF`（`plane=0`）**成功拿到 dmabuf fd**。
- `rga_blit_nv12` **直接喂 V4L2 导出的 dmabuf 成功**——本档全部三轮运行的报告里 `params.copy_cap` 均为 **0**（`offline.json` / `display.json` / `display2.json`），且**从未打印** `[rga] 直接喂 V4L2 dmabuf 失败` ⇒ **零拷贝通路成立**；`staging` 拷贝兜底路径**代码在位但本轮从未被触发**（无实测数据）。

### 3.2 离线单帧对账（`--offline`，不出屏）

- NEON 与标量参考（`apply_fused_scalar_to`，与 M0-13 的 `tools/apply_probe.c` 标量同源）**逐位一致**：`mismatch_bytes=0, max_abs=0 / 442368`。
- 画布左窗 vs `raw_win`、右窗 vs `enh_win` **均逐字节一致**（max_abs=0）——画布合成无错行、无通道错序。
- 原图窗 mean ≈92–99（场景而异）→ 增强窗 mean ≈164 → **提亮明显**。

### 3.3 实时出屏（`--display --frames 2400`，约 46 s）

reports：`docs/evidence/reports/M2-pipeline/display2.json`（2400 帧长跑）、`display.json`（300 帧）。

| 项 | 值 |
|---|---|
| 帧数 / 估计次数 / 丢 flip | **2400 / 150 / 0** |
| wall / 平均 FPS | 45.66 s / **52.6 fps** |
| NEON vs 标量自检 | **mismatch=0, max_abs=0** |
| 各段函数级耗时（中位 ms） | cap_dqbuf 0.006 · rga_est 1.81 · rga_win 1.66 · **apply 6.76** · npu_est 21.95 · canvas 1.47 · pageflip 0.26 |
| 帧路径 frame_ms | min 8.45 / **中位 16.80** / p90 30.10 / max 83.90 |

- **用户目视确认（运行期间）**：屏上出现**左右并排双窗**，左暗右亮。程序退出后屏回终端（fbcon 接管显示），属预期。

### 3.4 S0 预测量（决定单线程是否够，`tools/apply_probe` / `tools/rga_probe`）

| 项 | 实测 | 说明 |
|---|---|---|
| O42 曲线应用 @512×288 ×8 iter 单遍融合（NEON） | **3.985 ms 中位**（p90 3.992；NEON vs 标量逐位一致；DDR 26.5 vs 212.3 MB/s@30） | 由 1080×608 的 17.81 ms 改为新窗尺寸后**实测封闭**，未按面积比外推 |
| O43 RGA NV12 1920×1080 → 512×288 RGB888 | **2.079 ms 中位**（blit；`core_mask=3`） | 与 480×270 的 2.10 ms 同量级 |

### 3.5 实现缺陷与修复（记录）

**缺陷**：`pipeline_probe.c` 首版用 `pthread_barrier` 做「主线程 ↔ 两条带 worker」的会合，**退出时死锁**——`g_sl_stop` 的置位发生在 worker 越过 stop 检查**之后**时，worker 会回到下一次 `barrier_wait`，而主线程已不再参与 ⇒ `pthread_join` 永久阻塞。**现象**：报告已完整写出、进程却卡住不退（首轮三次运行均由外层 `timeout` 兜底终止；其中一次未加 `timeout` 的后台运行**残留进程并持续持有 `/dev/dri/card0`**，已 `kill -9` 清理）。

**修复**：改用 **mutex + cond + 代次**（`g_sl_gen` / `g_sl_done` / `g_sl_stop` 同锁发布），消除竞态窗口。**修复后实测干净退出**（不加 `timeout`）：

| 模式 | 结果 |
|---|---|
| `--offline` | 4 帧 / 1 次估计，`real 0.475 s`，退出码 0，无残留进程 |
| `--display`（600 帧） | **600 帧 / 10.99 s / 54.57 fps**，`real 11.408 s`（收尾 0.4 s），退出码 0，无残留进程 |

> **口径说明**：§3.3 的 2400 帧长跑报告由外层 `timeout 60` 兜底终止（报告在收尾前已写出，故**帧率/耗时统计有效**），但**当时未验干净退出**；干净退出由本节 600 帧运行验证。二者是同一份链路代码，仅退出路径不同。

> **apply 耗时的口径差异（观察，非结论）**：实机 `apply_ms` 中位 **6.76 ms**，高于 microbenchmark 的 3.985 ms。可核实的差异来源有二——① microbenchmark 是**纯 apply 内核**（输入预热、无并发），实机是**每帧与其它 RGA/NPU 工作共享缓存与内存带宽**；② 估计帧内 apply 紧跟 NPU 两条带之后，曲线场（1.55 MB float32）刚被 EMA 重写，cache 冷。**未做对照实验，故不下因果结论**（规则 `10-evidence.md` §1）；该差异**不预示 §6.1 达标**，仅为链路现状记录。

## 4. 判定

1. **ZDE++ 全流程已在 MIPI 屏上打通**（采集→RGA→NPU→8 iter 应用→两窗→DRM pageflip），用户目视确认双窗画面。
2. **零拷贝通路成立**：V4L2 EXPBUF → RGA 直喂 V4L2 dmabuf，无 per-frame 拷贝。
3. **NEON 与标量逐位一致、画布合成逐字节正确**：数值链路与 M0-13 基线一致，无通道错序/错行。
4. **实时链路的帧路径中位 16.80 ms，落在 60 Hz 面板周期 16.67 ms 附近**；45 s 长跑 0 丢 flip。**该数字仅供链路现状记录，不构成 §6.1/§13 达标结论**（M0 未出口，见 §5）。

## 5. 未做（按用户"打通后再优化"指示，本轮明确不做）

- 不调画质：不动 ISP / IQ JSON / 不做 fine-tune / 不切精度档 / 不调曝光增益。
- 不做 OSD 文本与曲线图/直方图（画布下方 312 px 留黑）、不做触控按钮与触发 FSM、不做 HQ(Retinexformer) 链路、不做 HDMI。
- 不改 config.yaml、不新增 config 键 / 日志事件 / 返回码。
- 不做软复位 / 部署 / systemd 单元。
- **交付链路未建**：`capture.py`/`curve_estim.py`/`realtime_apply.py`/`display_mipi.py` 仍按 §10.3 待 M2 拆分；`pipeline_probe.c` 的 `realtime_apply` 内核与画布合成可直接复用。

## 6. 复现

```
# 板上编译（librknnrt 通过 -lrknnrt 链接，或运行时加载 /usr/lib/librknnrt.so）
cd /home/cat/rknn_ab/probe
gcc -O3 -mcpu=cortex-a76 -Wall -Wextra -I. -I/usr/include/rga -I/usr/include/libdrm \
    -o pipeline_probe pipeline_probe.c -lrga -ldrm -lm -lpthread -lrknnrt

# 关桌面取得 DRM master（有副作用，运行时需授权；结束 restore graphical.target）
sudo systemctl isolate multi-user.target

# 离线单帧（不出屏，落图对账）
./pipeline_probe --offline --dump-dir /home/cat/rknn_ab/data/pipe \
    --report /home/cat/rknn_ab/data/pipe/offline.json

# 实时出屏（左右并排双窗）
./pipeline_probe --display --frames 2400 \
    --report /home/cat/rknn_ab/data/pipe/display2.json

# DRM 冒烟（纯色条，只验 master/pageflip）
./pipeline_probe --smoke

# 恢复桌面
sudo systemctl isolate graphical.target
```

S0 预测量复现（同为板上探针）：
```
./probe/apply_probe data/apply_in_512x288.rgb 512 288 data/curve_eff_480x270.f32 480 270 30 fused_neon
./probe/rga_probe data/frame_1080p_nv12.raw 1920 1080 512 290 30 3 fdva system-dma32 sync \
    data/rga_512x288.rgb data/rga_512x288.json
```

## 7. 报告原档与预览

报告：`docs/evidence/reports/M2-pipeline/{offline,display,display2}.json`、`display2.log`。
预览 PNG：`canvas.png`（1024×600 全画布，左原图 / 右增强）、`raw_win.png`、`enh_win.png`。三者由板上落盘的 `.rgb` 转存而来；`.rgb` / `.f32` 中间产物按 `.gitignore` 不入库，可按 §6 的命令重新生成。