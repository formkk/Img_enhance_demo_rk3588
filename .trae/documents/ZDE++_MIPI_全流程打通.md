# 计划：MIPI 屏上 ZDE++ 全流程打通（首版单体 C 程序）

## Context（为什么做这件事）

用户指令：**「先实现 MIPI 屏上 ZDE++ 网络的全流程实现，画质相关的在打通全流程后再进行优化。」**

现状：项目已把 ZDE++ 链路的**每一段单独**在板上实测通了（RGA 几何 2.10 ms、INT8 双条带 NPU 17.96 ms、8 iter 单遍融合 apply），但这些探针彼此独立、都吃文件不吃实时帧，**从未串成一条能在屏上出图的实时链路**。全仓库经穷尽核查：**没有任何 V4L2 采集 C 代码、没有任何 DRM/KMS C 代码、没有主链路骨架**——这两块必须从零写。

本计划的目标是**在 7" MIPI 屏上看到实时增强画面**（左右并排：原图 | 增强），画质与性能优化一律留到打通之后。

### 已确认的决策（用户 2026-09-18 选择）

| 决策 | 取值 |
|---|---|
| 实现形态 | **单个板端 C 程序**（`tools/pipeline_probe.c`），按现有探针惯例登记；M2 再按 §10.3 拆模块 |
| 曲线估计档位 | **INT8 双条带 core0+core1**（17.96 ms 中位，已过 O27-①b 闸门） |
| MIPI 显示内容 | **左右并排双窗**（原图 512×288 左 / 增强 512×288 右），OSD 与按钮区先留黑 |
| 桌面处置 | **授权 `systemctl isolate multi-user.target`**（M0-10 已验证可回退） |

### 流程纪律（必须先记账）

`40-gate.md` §5 规定「闸门未过时不得开始下一里程碑的实现」。当前 **M0 未出口**（根因 O30 暂挂 + §11.2-② 4 项空缺，为用户显式接受的现状）。本次是**用户显式指示**在 M0 未出口状态下先行打通 M2 的实时链路 ⇒ 须按 §15 登记一条**用户豁免**，不得当作默认放行。画质、性能指标一律**不**据此判达标。

---

## 关键事实（本次探测已核实，作为实现依据）

| 项 | 事实 | 来源 |
|---|---|---|
| 采集节点 | `/dev/video11` = `rkisp_mainpath`，**仅支持 MPLANE**（caps 无 `V4L2_CAP_VIDEO_CAPTURE`），NV12 1920×1080，`sizeimage` 3110400，`bytesperline` 1920，`num_planes`=1 | 板上 `v4l2-ctl --all` / `videodev2.h` |
| 显示节点 | DSI-1 = connector **221** / CRTC **142** / encoder **220** / primary plane **114**，唯一 mode `1024x600@60`（pclk 51668），`status=connected`、`enabled`、`dpms=On`；HDMI-A-1(207) disconnected | `/sys/class/drm/*`、M0-10 |
| plane 格式 | plane 114 支持 `XR24`，`IN_FORMATS` **只有 LINEAR** ⇒ `DRM_FORMAT_XRGB8888` 线性可用 | 板上只读解码 |
| libdrm 能力 | `drmModeAddFB2` / `drmModeAddFB2WithModifiers` / `drmModePageFlip` / `drmSetMaster` / `drmPrimeFDToHandle` / `drmHandleEvent` 均可用；**无 `drmModeCreateDumbBuffer`**（dumb 须走原始 ioctl） | `/usr/include/xf86drm*.h` |
| RGA 头 | `/usr/include/rga/im2d.h`；格式枚举在 `rga.h`（`RK_FORMAT_XRGB_8888 = 0x29<<8`、`RK_FORMAT_RGB_888 = 0x2<<8`） | 板上 |
| RGA 硬约束 | `imfill`/colorfill 对 RGB888 **直接失败**；`drect` 的 y 偏移**必须偶对齐** ⇒ 有效区落第 1..270 行只能靠**偏移基址子视图** | M0-13 证据 |
| dma_heap | 可用堆：`cma / cma-uncached / system / system-dma32 / system-uncached / system-uncached-dma32` | 板上 |
| RKNN | `rknn_api.h` 在 `/home/cat/rknn_ab/probe/`；切片模型 `zde_curve_256x272_int8_tk232.rknn`（输入 `[1,272,256,3]` uint8 NHWC，HALO=7，输出同 shape）在 `/home/cat/rknn_ab/models/` | 板上 |
| DRM master | 当前 `gnome-shell` 持 `card0`；`drmSetMaster` 在已有 master 时需 **root** ⇒ 必须先 `isolate multi-user.target`（用户已授权） | 板上 + M0-10 |
| 可复用代码 | `tools/rga_probe.c`（dma_heap + 偏移子视图 + `improcess`）、`tools/apply_probe.c`（`build_taps` + 融合 NEON 8 iter）、`tools/rknn_split_probe.c`（2 条带 + 拼接） | 本仓库 |

### 三项**尚不能静态确认**、必须实跑验证的（不得当结论）
1. RGA 能否一次 `improcess` 完成 `RGB888 → XRGB8888`（无 CSC，属基础能力，但本平台未验）；
2. 我们的进程能否**独立拉起** rkisp mainpath（`rkaiq_3A_server` 常驻，ISP params 依赖关系未定）；
3. apply 在 **512×288** 的真实耗时（现有 17.81 ms 是 @1080×608，**禁止按面积折算**，= O42）。

---

## 实施方案

### 交付物
- **`tools/pipeline_probe.c`**（新增，板端原生编译）：单进程，串起 采集→RGA→ZDE++→曲线应用→画布→DRM pageflip。
- **`docs/evidence/M2-pipeline.md`**（新增证据档）+ **`docs/evidence/reports/M2-pipeline/`**（报告原档）。
- 主基准回写：§10.3（新工具 + 新证据档 + 新报告目录）、§14（新版本行）、§15（用户豁免 + O42/O43 实测值）。
- **不改 `config.yaml`**（探针吃 CLI 参数，键表定稿须走 §15 裁定，不在本轮）。

### 程序结构（单文件，约 700~900 行）

```
dma_heap_alloc()                       ← 直接搬 tools/rga_probe.c
v4l2_*()   open/S_FMT/G_FMT/REQBUFS/QUERYBUF/mmap/EXPBUF/QBUF/STREAMON/DQBUF + poll
rga_*()    est_pad_fill_once()  blit_est()  blit_win()
rknn_*()   init 2 ctx + set_core_mask(1/2)  build_slice_input()  infer()  splice()
apply_*()  build_taps()  apply_fused_neon()   ← 搬 tools/apply_probe.c
drm_*()    init/get_res/get_conn/get_crtc  create_fb(drmPrimeFDToHandle+AddFB2)  set_crtc  pageflip  wait_event
main       参数解析 + 模式分派 + 实时循环
```

**缓冲与格式**

| 缓冲 | 尺寸/格式 | 分配 | 说明 |
|---|---|---|---|
| 采集 | 1920×1080 NV12 ×4 | V4L2 MMAP + `VIDIOC_EXPBUF` | fd 全程不 close |
| est_in | 480×272 RGB888 | dma_heap `system-dma32` | 有效区 480×270 落第 1..270 行；pad 行 init 时零填一次 |
| raw_win | 512×288 RGB888 | dma_heap `system-dma32` | 原图窗（同时作 apply 的输入） |
| enh_win | 512×288 RGB888 | dma_heap `system-dma32` | apply 输出 |
| canvas ×2 | 1024×600 XRGB8888 | dma_heap **`cma`** | 物理连续，VOP2 扫描最稳；双缓冲供 pageflip |

**关键 API 形状（易错处，逐条落实）**
- MPLANE：`v4l2_buffer.length` = **plane 个数（=1）**，非字节数；`m.planes` 指向 `v4l2_plane[]`；`VIDIOC_EXPBUF` 的 `plane` 字段必须显式写 0；非阻塞 + `poll()` 带超时。
- RGA：est_in 的 blit 目标 = `wrapbuffer_virtualaddr_t(base + stride*1, 480, 270, 480, 272, RK_FORMAT_RGB_888)`（偏移基址子视图，**不用 drect dy**）；pad 用 `improcess` 从零源块填（`imfill` 不可用）。
- 画布合成：`drect = {0,0,512,288}` 与 `{512,0,512,288}`（x=512 是 4 对齐、y=0 偶对齐）；`RK_FORMAT_XRGB_8888`。
- DRM：`open("/dev/dri/card0")` → `drmSetMaster` → `drmPrimeFDToHandle` → `drmModeAddFB2(...,DRM_FORMAT_XRGB8888, pitch=4096)` → `drmModeSetCrtc(fd,142,fb0,0,0,&conn221,1,&mode)` → `drmModePageFlip(...,DRM_MODE_PAGE_FLIP_EVENT)` → `drmHandleEvent`。**非阻塞**：只在上一次 flip 回调完成后才发下一次。
- 若 CPU 写过 cached 缓冲再交 DRM 扫描，须 `DMA_BUF_IOCTL_SYNC` 包住（本设计画布由 RGA 写，优先 `cma` 规避）。

**实时循环（首版单线程 + 抽帧估计）**
```
DQBUF(frame)
  → blit_est(frame)                    ~2.1 ms
  → if (++n % est_every == 0) {        est_every 默认 16 → 摊薄 ~1.1 ms
        2× build_slice_input + rknn_run(2 ctx 并行) + splice   ~18 ms（仅 1/16 帧）
        EMA: c = α·c_prev + (1-α)·c_new    α 默认 0.63（N≈5 @10Hz，§9.1）
    }
  → blit_win(frame) → raw_win           ~2.2 ms（= O43，本轮实测）
  → apply_fused_neon(raw_win, c, enh_win, 512, 288)   （= O42，本轮实测）
  → RGA blit raw_win → canvas[0..511],  enh_win → canvas[512..1023]
  → pageflip(canvas) + 等 vblank 事件
  → QBUF(frame)
```
15/16 帧 ≈ 11~13 ms（稳 60 Hz），1/16 帧 ≈ 29~31 ms（丢一次 flip，肉眼不可见）。若实测 apply 偏大导致超 16.67 ms，退路是**把估计挪到独立线程**（TH2a）+ 曲线场双缓冲——但首版不做。

**CLI**
```
pipeline_probe --offline [--dump-dir DIR] [--device /dev/video11]
pipeline_probe --display [--est-every 16] [--alpha 0.63] [--frames N] [--report J.json]
               [--model PATH] [--no-ema] [--smoke]     # --smoke = dumb buffer 纯色 pageflip
```

---

## 分阶段步骤（每步可独立验证，失败即停）

| # | 步骤 | 产出 / 通过判据 | 副作用 |
|---|---|---|---|
| **S0** | **补两项预测量**：跑现成 `apply_probe` @512×288×8iter；跑现成 `rga_probe` 1920×1080 NV12→512×288 RGB888 | O42 / O43 实测值（决定单线程够不够） | 无（纯文件 I/O） |
| **S1** | **V4L2 采集段**：实现 `v4l2_*`，抓 1 帧落 raw，与 `v4l2-ctl --stream-to` 结果对账 | 帧长 3110400 B、逐帧对齐；EXPBUF 拿到 fd | 短暂开流（M0/M1 板级诊断口径） |
| **S2** | **离线单帧全链路**：`--offline`，抓帧→RGA→NPU→apply→存图，**不出屏** | 增强窗与 Python 参考（`tools/enhance_ref.py` 的 `enhance()`，同帧同曲线场）**逐元素一致或 ≤1 LSB** | 无显示副作用 |
| **S3** | **DRM 亮屏冒烟**：`--smoke`（dumb buffer 色条 + pageflip） | 屏上出现色条 ⇒ master/DSI/pageflip 通路通 | **需先 `sudo systemctl isolate multi-user.target`**（已授权） |
| **S4** | **切 dma_heap+cma + AddFB2，接全链路**：`--display`，左右并排双窗 | 屏上出现**实时增强画面**；记录各段耗时 | 同上，开流 + 占屏 |
| **S5** | **收口**：报告 JSON 归档；回写 §10.3 / §14 / §15；退出后 `isolate graphical.target` 恢复桌面 | 闸门清单：逐条 状态 + 证据路径 | 恢复桌面 |

**S0 必须先做**：apply @512×288 若显著超预期，S4 的循环结构（单线程 vs 双线程）要改，先量再写代码可省一轮返工。

---

## 风险与兜底

| 风险 | 兜底 |
|---|---|
| RGA `RGB888→XRGB8888` 一次转换失败 | CPU/NEON 转换两窗（294912 px ≈ 0.5~0.8 ms，可接受）；或让 apply 内核直接以 stride=4096 写进画布右半 |
| `VIDIOC_EXPBUF` 在本 rkisp 节点不可用 | 退化为 mmap VA → 拷进 dma_heap 缓冲（1080p NV12 3.0 MB ≈ 1 ms/帧，打通够用） |
| 我们的进程拉不起 mainpath（ISP 依赖） | 先用 `v4l2-ctl` 常驻占流做对照实验定位；必要时改由 gst 供帧（仅诊断，不进演示路径） |
| dma_heap→DRM 扫描出现陈旧/半帧 | 画布改用 `cma` / `cma-uncached`；或对 CPU 写加 `DMA_BUF_IOCTL_SYNC` |
| 单线程超 16.67 ms | 先加大 `est_every`；仍超则上 TH2a 独立线程 + 曲线场双缓冲 |
| 桌面/master 冲突 | 已授权 `isolate multi-user.target`；退出前务必 `isolate graphical.target` 恢复 |

---

## 验证方法

1. **数值正确性（S2）**：`--offline --dump-dir` 落 `raw_win.rgb` / `enh_win.rgb` / `curve.f32`，用 `tools/enhance_ref.py` 的 `enhance()` 在同一份输入上重算，要求逐元素 max_abs ≤ 1 LSB（NEON 与标量在 M0-13 已证逐位一致，故差异只应来自取整）。
2. **屏上目视（S4）**：左右并排，右侧明显提亮且无错行/无错色（B/G/R 通道不得互换——这是 RGB888→XRGB8888 写错的最典型症状）。
3. **性能**：各段耗时（采集/RGA est/RGA win/apply/NPU/合成/pageflip）分项打点，写 JSON 到 `docs/evidence/reports/M2-pipeline/`；**FPS 与函数级耗时分开上报**（§6.4）。
4. **可复现命令**：编译与运行命令逐字写入证据档（`gcc -O3 -mcpu=cortex-a76 -I/usr/include/rga -I/home/cat/rknn_ab/probe -o pipeline_probe pipeline_probe.c -lrga -ldrm -lm -lpthread`，链接 librknnrt 的写法在 S1 落地时确认）。

---

## 明确不做（本轮）

- 不调画质：不动 ISP / IQ JSON / 不做 fine-tune / 不切精度档（用户明确「打通后再优化」）。
- 不做 OSD 文本与曲线图/直方图（留黑），不做触控按钮与触发 FSM，不做 HQ(Retinexformer) 链路，不做 HDMI。
- 不改 `config.yaml`、不新增 config 键 / 日志事件 / 返回码（探针口径）。
- 不做软复位 / 部署 / systemd 单元。
- 不把本轮任何耗时/画质数字当作 §6.1 / §13 的达标依据（M0 未出口 + 用户豁免的记账见 §15）。
