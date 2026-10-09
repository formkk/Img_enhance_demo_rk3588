#!/usr/bin/env python3
"""main.py —— 交付链路主程序：线程编排 + 启动序/退出序（§10.3、§5、§10.6-B）

线程（§5 线程模型）：
    TH1  采集/控制   capture.py      —— `QBUF` 唯一持有者、`seq` 分配者、lease 签发者
    TH2a 曲线估计    curve_estim.py  —— RGA + ZDE++ 双条带 + EMA，5~15 Hz
    TH2b 逐帧应用    realtime_apply  —— 8 iter 单遍融合（C/NEON）
    TH4  MIPI 显示   display_mipi.py —— DRM 单 CRTC 画布合成 + OSD + pageflip
    TH5  HDMI 输出   display_hdmi.py —— 热插拔监测 + RSM 接线（M2-1 第①批；无 HDMI 时进
                                       `MIPI_PREVIEW_ONLY`）。§5 写 TH5 为"线程"，实现为
                                       **主循环低频步进**（已登记偏离）；`publish()` 随 HQ 推迟

**启动序（§5）**：TH4（DSI/CRTC 就绪）→ TH2 → TH1（全生命周期只 stream on 一次）→ TH5
**退出序（§5）**：先停发新 lease → 等在途 lease/fence 有界清空 → TH1 `STREAMOFF` 并释放 DMABUF 池
                → TH4 释放 CRTC/plane
**软复位（O26）**：只做退出序前两段，**不 STREAMOFF、不释放池与 CRTC/plane**。
**退回桌面（O114）**：「复位·退出」键长按 ≥3 s ⇒ 退出序之后 `isolate graphical.target`（§10.6-C）；
该动作**只挂本键**，bench / `--frames` / 信号退出不回桌面（见 `_restore_desktop()`）。

运行模式（§10.6-B，**不设 `runtime.mode` 键**）：
    演示 = systemd 拉起本文件；bench = SSH 前台跑 `tools/bench.py`；调试 = 前台 + `logging.level=DEBUG`
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

import _starlight_native as _n

import capture as capture_mod
import config as starlight_config
import curve_estim as curve_mod
import display_hdmi as hdmi_mod
import display_mipi as display_mod
import keyframe_infer as kf_mod
import log as starlog
import pointer as pointer_mod
import realtime_apply as ra
import ui_buttons as ui_mod

# 曲线场精度档 → 扩展取值
WIN_W, WIN_H = curve_mod.WIN_W, curve_mod.WIN_H

# ── **（v3.86 裁定 O24-前 2 类）异常恢复的有界容忍与越阈停机**（§10.5 矩阵两行）──────
# 四问的裁定值：**不重试**（O26 禁止停流重开、`stream_on_once` 恒一次 ⇒ 进程内无自愈手段）；
# 阈值内容忍；越阈 ⇒ `RUN_DEMO → FAULT_EXIT`（不迁已作废的 `MIPI_PREVIEW_ONLY`）⇒ 停机。
# 判据只统计 `RUN_DEMO` 且**非冻结 / 非全屏期**（HQ 冻结期本就不取帧，不排除会误判）。
V4L2_ERR_STREAK_N = 15          # 自上次成功取帧起，连续硬错误（DQBUF 非 EAGAIN）次数 ≥ 此值
NO_FRAME_TIMEOUT_S = 1.0        # 连续无成功取帧时长 ≥ 此值（秒）
ACQUIRE_FAIL_BACKOFF_S = 0.005  # 取帧失败时的退避（防硬错误路径忙等；正常帧路径不受影响）
FAULT_EXIT_CODE = 1             # 停机退出码（≠0 ⇒ systemd `Restart=on-failure` 限次拉起）
DMESG_SCAN_BYTES = 64 * 1024    # 归因用：`dmesg` 尾部**只读**有界扫描（§10.5 第 2 行）
DMESG_KEYS = ("imx415", "i2c", "rkisp")   # 设备/链路标识
# **必须"同一行同时命中设备标识与错误关键词"才归因**——否则会误判：板上 `dmesg` 尾部 64 KiB 内
# `imx415` 有 350 行，其中绝大多数是 `sensor_lock` 的**例行**行（`set vblank` / `set analog gain`）
# ⇒ 只按设备标识匹配会**恒判** `sensor_i2c_dma`，使两类的区分失去意义。
DMESG_ERR_KEYS = ("error", "fail", "timeout", "timed out", "denied", "eio", "reset", "retry",
                  "no response", "not respond")

# **（v3.86 裁定 O108）** OSD 帧率的统计窗长（秒）：屏上帧率取**最近 1 s 滑窗**（见 `_fps()`）。
FPS_WINDOW_S = 1.0

# **（v3.86 裁定 O115）** 屏上「端延时」的可读性口径：旧实现**逐帧**取「最近一次配对的总计」
# （O108）⇒ 每帧一个新值，**人眼读不出**。现改为 **最近 `LAT_WINDOW_S` 内已配对帧的 P50**，
# 且**每 `LAT_REFRESH_S` 才刷新一次**（窗长与「帧率」同口径、刷新频率与统计降频 1 Hz 同频）。
# 该值**只作现场判读**，不构成验收口径（验收仍只看 `bench.py` 报告，§6.5）。
LAT_WINDOW_S = 1.0
LAT_REFRESH_S = 1.0

# **（v3.86 裁定 O114）** 退出键 = **退回桌面**：退出序（`d.shutdown()` 释放 DRM master）完成后
# 切回图形 target，桌面即回来（§10.6-C）。桌面是用 `isolate multi-user.target` 切走的（§2.3），
# 故恢复 = `isolate graphical.target`（**已实测往返可恢复**，见 `docs/evidence/M2-2-hq-align.md`）。
# **只由「复位·退出」键长按路径触发**（`Demo._restore_desktop` 置位）—— bench / `--frames` /
# SIGINT 等其它退出路径**不**回桌面，否则会污染计量窗口与取证（§6.5）。
# 目标名是**机制值、非 config 键**（同 §10.6-A 的 `ExecStartPre` 口径）。
DESKTOP_TARGET = "graphical.target"


def _restore_desktop() -> None:
    """退回桌面（§10.6-C / 裁定 O114）：`systemctl isolate graphical.target`。

    桌面缺席是**可降级项**（规则 `30-board-safety.md` §2）⇒ 失败**只提示、不阻塞退出、不改返回码**；
    提示走 stdout（**不新增日志事件**——§4 事件清单无对应项，不自行造事件）。
    """
    try:
        r = subprocess.run(["systemctl", "isolate", DESKTOP_TARGET],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"退回桌面失败（isolate {DESKTOP_TARGET}）：{e}；屏将停在最后一帧")
        return
    if r.returncode != 0:
        msg = r.stderr.decode(errors="replace").strip() if r.stderr else ""
        print(f"退回桌面失败（isolate {DESKTOP_TARGET} rc={r.returncode}）：{msg}；屏将停在最后一帧")


class Demo:
    def __init__(self, cfg: Any, logger: starlog.Logger, frames: int = 0, est_every: int = 2,
                 hdmi_every: int = 6, trigger_at: int = 0, trigger_times: int = 0,
                 trigger_interval_s: float = 15.0, touch_node: str = "", mouse_node: str = "",
                 dump_canvas_dir: str = "", dump_canvas_frames: int = 0,
                 dump_after: int = 0, dump_curve_once: str = "", snapshot_port: int = 0,
                 idle_link: bool = False,
                 inject_v4l2_error: int = 0, inject_v4l2_at: int = 30,
                 inject_v4l2_mode: str = "err",
                 no_est: bool = False, est_first: int = 0,
                 no_apply: bool = False, no_osd: bool = False,
                 apply_preserve_ratio: bool = True, hq_curve_in: bool = True,
                 hq_mfa_frames: int = 6, hq_chroma_gain: float = 1.5,
                 hq_tm_b: float = 0.8, hq_tm_target: float = 106.0, hq_tm_first: bool = False,
                 osd_status_only: bool = False, osd_no_stats: bool = False,
                 step_exp_times: int = 0, step_exp_interval_s: float = 3.0,
                 step_exp_low: int = 0, step_exp_high: int = 0, step_exp_subdev: str = ""):
        self.cfg = cfg
        self.log = logger
        self.max_frames = frames
        # **[计量钩子] 空链路基线（§6.1 的 v3.4「必读」段）**：*"关闭增强，只跑
        # 采集 → RGA 原图窗 → MIPI 显示"* ⇒ 本轮按**字面**口径实现：
        #   · 关 TH2a：不 `request()`（RGA 缩放 + NPU 双条带 + EMA 全不做）
        #   · 关 TH2b：不 `apply_window()`（8 iter 单遍融合不做）
        #   · 关 OSD：`DisplayMipi.init(with_osd=False)`（统计 / 曲线图 / 直方图 / 按钮区渲染都不做；
        #     O44 已实测其为实现成本 7.94 / 3.77 / 0.337 ms）
        #   · 保留：TH1 采集与 lease、RGA 原图窗、画布合成、DRM pageflip、`seq` 回填配对
        # `est.init()` **仍要跑**——它是 RGA 几何（`resolve_geometry`）与 RGA 目标的唯一配置点，
        # 且落在 `boot()` 内（§6.5：帧窗不含进程启动）。
        self.idle_link = bool(idle_link)
        # **[验证用钩子] ①-2 子项拆分（v3.86 追加 96）**：把「采集→提交」段的增量拆到
        # TH2a（曲线估计线程）/ TH2b（8-iter 应用）/ OSD 三个来源上。**非 config 键、不新增事件与返回码**；
        #   · `no_est` = 从不请求估计（TH2a worker 不参与帧路径）；
        #   · `est_first = N` = **只前 N 帧请求**，之后冻结「最近已发布场」（`latest()` 持久）⇒
        #     用于"只测 TH2b"（估计成本只在启动期、由 `--skip-first N` 在分析侧剔除）；
        #   · `no_apply` = 跳过 `ra.apply_window()`（窗内显示 RGA 原图，照常 pageflip）；
        #   · `no_osd` = OSD 不初始化（状态行 / 曲线图 / 直方图 / 按钮区全不做，同 `--idle-link` 的 OSD 口径）。
        self.no_est = bool(no_est)
        self.est_first = max(0, int(est_first))
        self.no_apply = bool(no_apply)
        self.no_osd = bool(no_osd)
        # **[A/B 开关] 保比语义**（交付实现选择，**非 config 键**，**默认开启 = 保比语义**，§14 追加 159）：
        # 在 `boot()` 里随 `ra.configure_from_config()` 一次性注入 C 层（`preserve_ratio`），
        # 使通道间比值在增强后保持不变（修去饱和）。**反向 opt-out = CLI `--no-preserve-ratio`**（仅供 A/B）。
        self.apply_preserve_ratio = bool(apply_preserve_ratio)
        # **[A/B 开关] HQ 输入侧接 ZDE++ 曲线**（**非 config 键**；**2026-10-08 目测通过后定为默认开**，
        # 反向 opt-out = `--no-hq-curve-in`，与 `--no-preserve-ratio` 同例）：开启后 TH3 在入模前
        # 把 TH2a 最近一次已发布曲线场施加到 HQ 内容区（应用语义随 `apply_preserve_ratio`，默认保比）。
        # 接线见 `keyframe_infer._apply_input_curve`。
        self.hq_curve_in = bool(hq_curve_in)
        # **[A/B 开关] HQ 输入侧「N 帧时域平均」**（= §15 O158 的「自制 3DNR」；**非 config 键**；
        # **2026-10-08 目测通过后定为默认 N=6**，反向 opt-out = `--hq-mfa-frames 0/1`）：
        # >1 时，TH3 触发受理后取 N 帧（含触发帧）、**逐帧 RGA 缩放到 HQ 目标几何后累加**
        # （取时拷走、**不持 lease** ⇒ 不动 V4L2 池），平均结果作 HQ 输入。
        # 实现位置 = O158 ④ 的「RGA 缩到 HQ 目标之后」；核 = native `mfa`。
        self.hq_mfa_frames = max(0, int(hq_mfa_frames))
        # **[A/B 开关] 链路末显式色度增强（§15 O161；非 config 键）**：在 HQ 模型输出之后、
        # `to_rgb888`/OSD 之前，做「保持亮度的线性色度增益 `Y+k·(C−Y)`」。
        # **2026-10-09 试验档 `1.5`**（用户要求试用；**GT 标定目标档为 `2.0`** —— 亮区 S 实测
        # 0.3508 vs GT 0.3467；`1.5` 对应亮区 S ≈ 0.2819，比 GT 目标低约 19% ⇒ 属保守档）。
        # **反向 opt-out = `--hq-chroma-gain 1.0`**（= 逐位恒等、回到未启用状态）。
        # 接线见 `keyframe_infer._apply_output_chroma`。
        self.hq_chroma_gain = float(hq_chroma_gain)
        # **[A/B 开关] 曲线后的黑位锚定色调映射（§15 O162；非 config 键）** —— 位置在
        # **曲线之后、进模型之前**（`keyframe_infer._tonemap_content`）。`tm_b` = 黑位锚定比例
        # （`b = tm_b × 曲线后 p1`）、`tm_target` = 目标 Y 均值。
        # **2026-10-09 定稿：输入侧 `b = 0.8×p1` / 目标 `106`**（用户目测裁定：输入侧明显优于
        # 输出侧——输出侧在链路末硬拉增益会把**色度噪声**一起放大且无人清理，白物被染暗红；
        # 输入侧经模型重渲染 ⇒ 伪影被洗掉。实测：归一化对比度 33% → **59%**、p1 98.4 → **60.8**）。
        # **opt-out = `--hq-input-tm ""`**（空 = 关闭 ⇒ `0/0` ⇒ 逐位恒等、回到改动前行为）。
        self.hq_tm_b = float(hq_tm_b)
        self.hq_tm_target = float(hq_tm_target)
        # **[A/B 实验]（非 config 键）** TM 顺序：`False`（默认）= 曲线 → TM（定稿）；`True` = TM → 曲线（情形 C）。
        self.hq_tm_first = bool(hq_tm_first)
        self._mfa_acc = None         # 16bit 累加器（**惰性分配**：只有开启该开关才占内存）
        # **[验证用钩子] OSD 瘦身两级（v3.86 追加 97）**：`osd_status_only` = 只画状态行（去两图）；
        # `osd_no_stats` = 连统计也不做。**非 config 键**，只用于 ①-2 的 OSD 增量归因实验。
        self.osd_status_only = bool(osd_status_only)
        self.osd_no_stats = bool(osd_no_stats)
        # **[验证用钩子] 「阶跃响应」场景（v3.86 裁定 O143）**：运行期按计划做 N 次**曝光阶跃**
        # （规则 20 §4 明许运行期曝光/增益微调），并开估计器**发布日志**；`bench.py --scenario
        # step-response` 据「阶跃时刻 → 场指纹收敛」算切换响应。**非 config 键 / 不新增事件与返回码**。
        self.step_exp_times = max(0, int(step_exp_times))
        self.step_exp_interval_s = max(0.5, float(step_exp_interval_s))
        self.step_exp_low = max(0, int(step_exp_low))
        self.step_exp_high = max(0, int(step_exp_high))
        self.step_exp_subdev = str(step_exp_subdev or "")
        self.step_log: list = []          # [(t_step_ns, frame, from, to)]
        self._step_series: list = []      # [(t_ns, frame, mean_orig, mean_enh)]（每 2 帧一次）
        self._step_next = 0
        # **[验证用钩子] §15 O24 的确定性注入**：在活动帧 `inject_v4l2_at` 起注入
        # `inject_v4l2_error` 次"acquire 返回硬错误"（**不是 config 键**、不新增返回码）。
        self.inject_v4l2_error = max(0, int(inject_v4l2_error))
        self.inject_v4l2_at = max(0, int(inject_v4l2_at))
        # 注入模式：`err` = 硬错误（计 `v4l2_errors`）/ `noframe` = 无帧超时（计 `lease_timeouts`）
        self.inject_v4l2_mode = 1 if str(inject_v4l2_mode).lower() == "noframe" else 0
        self._injected = False
        # **（v3.86 裁定 O24-前 2 类）** 越阈判据的运行态：`_no_frame_since` = 连续无成功取帧起点
        # （`None` = 有帧）；`_v4l2_err_base` = 上次成功取帧时的硬错误计数（差值即"连续"硬错误数）。
        self._no_frame_since: Optional[float] = None
        self._v4l2_err_base = 0
        self._v4l2_err_pending = False   # 上次成功取帧后是否出现过失败（省掉正常路径的 counters 读）
        self._last_frame_mono = 0.0          # 最后一次成功取帧的 `monotonic`（`dmesg` 归因的时间下界）
        self._first_frame_mono = 0.0         # 首个成功取帧的 `monotonic`（帧率旁证 `frame_span_ms`，O97）
        self._fault = False
        self._fault_reason = ""
        self._t_start = 0.0                  # 由 `main()` / bench 置位（用于 `service_exit` 的 uptime_s）
        self.est_every = max(1, est_every)
        # TH5 热插拔低频步进周期（帧）：与 `est_every` 同风格；6 帧 ≈ 200 ms @30 fps
        self.hdmi_every = max(1, hdmi_every)
        # **[验证钩子]** 触发注入（生产触发源 = 指针；O84 单源）：
        #   `trigger_at` = 第 N 个**活动帧**注入 1 次（兼容旧用法）；`trigger_times` = 时间驱动注入 N 次
        self.trigger_at = trigger_at
        self.trig_left = max(0, trigger_times) + (1 if trigger_at else 0)
        self.trig_interval_s = max(1.0, float(trigger_interval_s))
        self.trig_frame_at = trigger_at
        self._next_trig_t = 0.0
        self._triggered = False
        self.touch_node = touch_node
        self.mouse_node = mouse_node
        self.dump_canvas_dir = dump_canvas_dir
        # **[取证钩子]** 常规态**逐帧**导图的有界上限（0 = 关闭，既有行为不变）。
        # `_dump_n` = 已导出的**成功翻页序号**（0 起），与 `DisplayMipi.cur` 的奇偶一致
        # ⇒ `_dump_n % 2` 即该帧所用画布缓冲的索引（供两缓冲一致性分析）。
        self.dump_canvas_frames = max(0, int(dump_canvas_frames))
        # **[取证钩子] 预热帧数**（v3.86 追加 50）：跳过前 `dump_after` 个**成功翻页**后才开始逐帧转储
        # ⇒ 用于取**稳态**画面（否则转储本身的 PNG 压缩开销会把帧率/延迟拖垮，得到的是"负载态"）。
        self.dump_after = max(0, int(dump_after))
        self._dump_n = 0
        # **[取证钩子] 曲线场原样导出一次**（默认关闭）：首次拿到有效 `self.curve` 后把
        # `field` 原样写一份二进制 + `PATH.json` 随行信息（`field` 类型不定 ⇒ 见 `_dump_curve_once`）。
        self.dump_curve_once = dump_curve_once
        self._curve_dumped = False
        # **[调试钩子] 快照 HTTP 服务**（`--snapshot-port N`；**0 = 完全关闭**，既建线程不占端口）。
        # 起在**独立守护线程**里（**纯 `socket` + `threading` 手写**——**不用 `http.server`**，见
        # `_start_snapshot_server` 注释：板端 `cwd` 下有遮蔽标准库的同名文件），供**开发机浏览器**
        # 看板端 demo 进程产出的**链路原样** MIPI 画布（复用 `self.disp.dump_png`，零色调后处理）
        # ⇒ 对比"软件看到的像素"与"DMA 到屏的画面"。**帧路径零改动**（导出只在服务线程内做）。
        self.snapshot_port = max(0, int(snapshot_port))
        self._snapshot_srv = None
        self._snapshot_lock = threading.Lock()   # 临时文件名自增序号（**不用 tempfile/random**）
        self._snapshot_seq = 0
        # ── 指针 / 视图态 / HQ 结果与冻结（§3.5、O79、O81、O90）──────────
        self.ptr = None
        self.cursor = None
        self.view = "normal"                 # normal | fullscreen_hq（§3.5 两态）
        self.frozen = False
        self.fs_enter_ns = 0
        self.freeze_ns = 0                   # 累计冻结窗（fps 用活动窗口径，O90）
        self._frozen_since = 0
        self.hq_seq = 0
        self.hq_rgb = None                   # HQ 960×540 RGB888 常驻缓冲（O79 全屏视图内容）
        self._dumped_normal = False          # 常规态导图只做一次
        # §13「HQ 响应」行的 **seq 级明细**（`bench.py --scenario hq --seq-log` 的数据源）：
        # 每个触发周期一条记录（触发受理 → OSD 提示 → 全屏出图 → 退出），时刻一律 `mono_ns`。
        self.trig_log: list = []
        self._cur_trig: Optional[Dict[str, Any]] = None
        self._inject_fired = 0               # 已注入的触发次数（自动收尾判据，见 `run()`）
        self._reset_press_ns = 0             # 「复位·退出」键按下时刻（0 = 未按下；§10.6-C 短按/长按）
        # **（v3.86 裁定 O114）** 退出键 = 退回桌面：仅本路径置位（`main()` 退出序后 `isolate`）
        self._restore_desktop = False
        self.stop = threading.Event()
        self.cap: Optional[capture_mod.Capture] = None
        self.est: Optional[curve_mod.CurveEstimator] = None
        self.disp: Optional[display_mod.DisplayMipi] = None
        self.hdmi: Optional[hdmi_mod.DisplayHdmi] = None
        self.kf: Optional[kf_mod.KeyframeInfer] = None
        self.buttons: Optional[ui_mod.ButtonHit] = None
        self.curve = None
        self.counters: Dict[str, Any] = {"frames": 0, "estimates": 0, "flips": 0,
                                         "flip_skipped": 0, "dropped_stale": 0,
                                         "lease_timeouts": 0, "discarded_triggers": 0,
                                         # **（v3.86 裁定 O24-前 2 类）** 硬错误计数（掉流/接收错误）
                                         "v4l2_errors": 0,
                                         # **（v3.86 裁定 O122）** 翻页**负值**帧数（自愈后仍未成）
                                         # ——O122 前该分支**不计数**，负值被静默吞掉（49 min / 5 flip）
                                         "pageflip_fail": 0,
                                         "hq_started": 0, "hq_done": 0,
                                         "freeze_enter": 0, "freeze_exit": 0, "freeze_flips": 0,
                                         "fullscreen_enter": 0, "fullscreen_exit": 0}
        # **（v3.86 裁定 O122）** 显示失联 / 已恢复的**边沿**去重（与 TH4 的边沿计数比对，
        # 只在**翻转时**记 `degrade_action`，失联态内不刷屏）。
        self._pf_lost_seen = 0
        self._pf_rec_seen = 0
        # §6.5 延迟两段分解 + 按 seq 回填配对：`lat` = 端到端总计（采集→vblank），
        # `lat_app` = 采集→提交（应用侧），`lat_submit` = 提交→vblank（显示排队）。
        self.lat: list = []
        self.lat_app: list = []
        self.lat_submit: list = []
        self.pair_ok = 0
        # **（v3.86 裁定 O106）** OSD「延迟两段」的屏上取值：来自**最近一次成功配对**
        # （`display_mipi.lat_pair()`，按 `seq` 回填）⇒ 屏上显示**滞后一帧**（配对完成后才有值）。
        # 原实现的第一段是**写死的 0.0**（屏上该段从未真实测量），第二段是「本帧处理→提交」，
        # 与 §3.4/§6.5 定义的「采集→提交 / 提交→vblank」不是同一个量 ⇒ 一并改正。
        self._lat_cap_ms = 0.0
        self._lat_sub_ms = 0.0
        self._lat_total_ms = 0.0       # O108：屏上「端延时」= 单值显示；**O115：= 1 s 滑窗 P50、1 Hz 刷新**
        self._lat_win: list = []       # O115：窗内**已配对**样本 `(mono_s, total_ms)`
        self._lat_last_ms = 0.0        # O115：最近一次配对值（**窗内无样本时的回退值**）
        self._lat_refresh_s = 0.0      # O115：屏上值上次刷新时刻（`monotonic` 秒）
        self._fps_ts: list = []        # O108：屏上帧率的**最近 1 s 滑窗**（见 `_fps()`）

    # ── 启动序 ─────────────────────────────────────────────────────────
    def boot(self) -> int:
        cfg = self.cfg
        self.log.event("main", "version_triplet",
                       toolkit2=cfg.get("runtime.toolkit2_ver"),
                       librknnrt=cfg.get("runtime.librknnrt_ver"),
                       rknpu_driver=cfg.get("runtime.rknpu_driver_ver"))

        # ① TH4：DSI/CRTC 就绪
        self.disp = display_mod.DisplayMipi(cfg, self.log)
        rc = self.disp.init(with_osd=not (self.idle_link or self.no_osd),
                            osd_status_only=self.osd_status_only, osd_no_stats=self.osd_no_stats)
        if rc != 0:
            self.log.event("main", "boot_check_item", item="TH4", result="fail",
                           code=starlog.code_name(rc))
            return rc
        # **（v3.86 裁定 O121）** TH4 若**经有界重试才取到 DRM master** ⇒ 单独记一行
        # `result=degraded`（`code=` 仍是取 master 失败时的登记码 `E_DRM`）——
        # **不新增事件 / 不新增字段**；首拍即成功（`master_retries == 0`）时**不记**，保持日志干净。
        if self.disp.master_retries > 0:
            self.log.event("main", "boot_check_item", item="TH4", result="degraded",
                           code=starlog.code_name(display_mod.E_DRM))
        self.buttons = ui_mod.ButtonHit(cfg)

        # ② TH2：曲线估计（RGA + NPU）
        self.est = curve_mod.CurveEstimator(cfg, self.log)
        rc = self.est.init()
        if rc != 0:
            self.log.event("main", "boot_check_item", item="TH2a", result="fail",
                           code=starlog.code_name(rc))
            return rc
        self.est.alpha = float(cfg.require_final("pipeline.ema_alpha"))
        # **（v3.86 裁定 O143）** 阶跃响应场景：开估计器发布日志 + 解析 subdev / 高低曝光（启动期一次）
        if self.step_exp_times > 0:
            self.est.enable_publish_log()
            self._step_prepare()

        # 曲线应用内核（§5 GAP-1：启动时一次性注入几何）
        ra.shutdown()
        rc = ra.configure_from_config(cfg, preserve_ratio=self.apply_preserve_ratio)
        if rc != 0:
            self.log.event("main", "boot_check_item", item="realtime_apply", result="fail",
                           code=starlog.code_name(rc))
            return rc

        # ③ TH1：只 stream on 一次
        self.cap = capture_mod.Capture(cfg, self.log)
        rc = self.cap.init()
        if rc != 0:
            self.log.event("main", "boot_check_item", item="TH1", result="fail",
                           code=starlog.code_name(rc))
            return rc
        rc = self.cap.stream_on_once()
        if rc != 0:
            return rc

        # ④ TH5：HDMI 存在则启用，否则 `MIPI_PREVIEW_ONLY`（§5 启动序末位）
        # 热插拔探测走 sysfs 只读轮询（不碰 DRM 探测）。`init()` 内含首次探测 + RSM 接线，
        # 发 `hotplug` 与 `rsm_transition`（不自造事件）。HDMI 是可降级项（规则 30 §2）。
        self.hdmi = hdmi_mod.DisplayHdmi(cfg, self.log, self.cap)
        self.hdmi.init()
        # 第②批：HDMI 画布走 DRM 槽 1，**复用 TH4 已取得的 master fd**（master 是 per-fd）
        # ⇒ 必须在 `self.disp.init()` 之后调用。失败/未接均不阻塞主链路。
        self.hdmi.init_canvas()

        # ⑤ TH3：关键帧（HQ）推理。**独立 OS 线程**（§5 原文口径）——HQ 单次 ~8 s，
        # 若内联在主循环会让 MIPI 冻结 8 s，违反 §5「TH4 不得因结果槽空而锁帧」。
        # 前置：RGA 已 configure（TH2 的 `est.init()` 里唯一一次，含 hq_w/hq_h 目标缓冲）。
        # 失败（如模型缺失）**不阻塞主链路**——触发时 `request` 会返 E_STATE。
        self.kf = kf_mod.KeyframeInfer(cfg, self.log, self.est.geom,
                                       curve_src=(self.est.latest if self.hq_curve_in else None),
                                       chroma_gain=self.hq_chroma_gain,
                                       tm_b=self.hq_tm_b, tm_target=self.hq_tm_target,
                                       tm_first=self.hq_tm_first)
        rc = self.kf.init()
        if rc != 0:
            self.log.event("main", "boot_check_item", item="TH3", result="fail",
                           code=starlog.code_name(rc))

        # ⑥ 指针输入（O81）：触控 + 鼠标两源；无设备不阻塞（规则 30 §2 可降级）
        self.ptr = pointer_mod.Pointer(cfg, self.log,
                                       touch_node=self.touch_node or None,
                                       mouse_node=self.mouse_node or None)
        self.ptr.init()

        # ⑦ 运行态：自检过且至少一个显示口可用（TH4 已 init 成功）⇒ BOOT_CHECK → RUN_DEMO（§5）
        rc_rsm = self.cap.set_rsm(capture_mod.RSM_RUN_DEMO, reason="boot_ok")
        if rc_rsm != 0:
            self.log.event("main", "boot_check_item", item="RSM", result="fail",
                           code=starlog.code_name(rc_rsm))
            return rc_rsm

        # ⑧ HQ 结果上屏的**常驻缓冲**（全屏视图内容；一次性分配，O79/§3.5）
        g = self.est.geom
        self.hq_rgb = _n.alloc(int(g["hq_w"]) * int(g["hq_content_h"]) * 3)
        return 0

    # ── 主循环（TH2b + 编排；TH2a 与 TH4 在此串行推进，M2 单进程内可接受）──
    def run(self) -> int:
        assert self.cap and self.est and self.disp
        g = self.est.geom
        win = _n.rga.win(g)
        enh = _n.rga.enh(g)
        i = 0
        while not self.stop.is_set():
            # ── 指针（O81/O84 单源）+ 验证注入 + **HQ 冻结/全屏态**（O90）——每轮都跑 ──
            self._pump_pointer()
            self._pump_keys()             # 「复位·退出」键长按（§10.6-C；不依赖抬起边沿）
            self._pump_injection()
            if self._pump_hq_lifecycle():
                time.sleep(0.02)          # 冻结期/全屏期：**不取帧、不估计、不应用**（O90）
                continue
            t0 = time.monotonic_ns()
            # **（v3.86 裁定 O143）** 阶跃响应：低频推进曝光阶跃（稳态开销 = 两次比较）
            self._step_tick(i)
            # **[验证用钩子] §15 O24 的确定性注入**（只注入一次；`N=0` 时本段不生效）
            if (self.inject_v4l2_error and not self._injected
                    and i >= self.inject_v4l2_at and self.cap is not None):
                self._injected = True
                self.cap.inject_v4l2_error(self.inject_v4l2_error, self.inject_v4l2_mode)
                print(f"[inject] 已在活动帧 {i} 注入 {self.inject_v4l2_error} 次 acquire "
                      f"{'无帧超时' if self.inject_v4l2_mode else '硬错误'}（§15 O24 验证钩子）")
            f = self.cap.acquire(1, 500)
            if f is None:
                c = self.cap.counters()
                self.counters["lease_timeouts"] = c["lease_timeouts"]
                self.counters["v4l2_errors"] = c["v4l2_errors"]
                # **（v3.86 裁定 O24-前 2 类）** 越阈判据：连续无帧 ≥1 s **或** 连续硬错误 ≥15 次
                # ⇒ `RUN_DEMO → FAULT_EXIT` + 停机（见 `_fault_exit()`）。**阈值内容忍**。
                # 只统计 `RUN_DEMO` 且**非冻结 / 非全屏期**（冻结期本就不取帧 ⇒ 不参与判据）。
                if self.cap.rsm == capture_mod.RSM_RUN_DEMO and not self.frozen \
                        and self.view == "normal":
                    now = time.monotonic()
                    if self._no_frame_since is None:
                        self._no_frame_since = now
                    streak = int(c["v4l2_errors"]) - self._v4l2_err_base
                    if streak >= V4L2_ERR_STREAK_N or \
                            (now - self._no_frame_since) >= NO_FRAME_TIMEOUT_S:
                        self._fault_exit(streak)
                        break
                # 退避：硬错误路径 `acquire` 会**立即返回**，无退避即成忙等（规则 `30-board-safety.md` §3）
                self._v4l2_err_pending = True
                time.sleep(ACQUIRE_FAIL_BACKOFF_S)
                continue
            # 有帧 ⇒ 清零连续计数器（硬错误"连续"口径 = 自上次成功取帧起）。
            # **只在"上次成功取帧之后出现过失败"时才读一次 `counters()`** ⇒ 正常帧路径**零新增调用**
            # （空链路基线等既有报告因此仍可比）。
            self._no_frame_since = None
            self._last_frame_mono = time.monotonic()
            if not self._first_frame_mono:
                self._first_frame_mono = self._last_frame_mono
            if self._v4l2_err_pending:
                self._v4l2_err_pending = False
                self._v4l2_err_base = int(self.cap.counters()["v4l2_errors"])
            self.counters["frames"] += 1

            # 原图窗（RGA：NV12 → 512×288 RGB888）
            _n.rga.blit_nv12_fd(int(f["dmabuf_fd"]), 1)

            # TH2a：低频估计（每 est_every 帧一次）——**（v3.86 裁定 O123）改为「请求 + 独立线程」**：
            # `request()` 在本线程只同步做 RGA 缩放（持帧时长 = 一次 RGA），NPU 双条带 + EMA 在
            # TH2a worker 内跑 ⇒ **曲线估计不再落在帧路径上**（§6.1 第 413 / 415 行的契约位置）。
            # 曲线场按「最近一次已发布」非阻塞取用；worker 忙时本轮请求跳过（不含等待）。
            if not self.idle_link and not self.no_est:
                # **（v3.86 追加 96）** `--est-first N`：只前 N 帧请求估计，之后不再请求
                # ⇒ 场冻结在「最近已发布」，用于只测 TH2b（估计成本只在启动期）。
                if (self.est_first <= 0 or i < self.est_first) and i % self.est_every == 0:
                    if self.est.request(f) == 0:
                        self.counters["estimates"] += 1
                # 首拍（第 0 帧）等一次发布——与 O123 之前"首帧即持有有效曲线场"的行为一致；
                # 此后一律 `latest()` 非阻塞取值，**帧路径不再等 worker**。
                self.curve = self.est.wait_first() if i == 0 else self.est.latest()
            # **[取证钩子]** `--dump-curve-once PATH`（默认关闭）：**首次**拿到有效曲线场后
            # 把 `self.curve.field` **原样**写一次（二进制 + `PATH.json` 随行信息）。
            if self.dump_curve_once and not self._curve_dumped and self.curve is not None:
                self._dump_curve_once(i, int(f["seq"]))
            # **空链路（`--idle-link`）**：`self.curve` 恒为 `None` ⇒ 下面的 TH2b 与 OSD 统计
            # 两个 `if` 自然短路，链路只剩「采集 → RGA 原图窗 → 画布合成 → pageflip」。

            # TH5：低频热插拔步进（每 hdmi_every 帧一次；§5 写"线程"⇒ 已登记偏离）
            if self.hdmi is not None and i % self.hdmi_every == 0:
                self.hdmi.poll()

            # TH3 编排（§5）：① 消抖期满则受理触发 → 用**本帧**作触发暗帧投递 HQ（= 冻结起点，O90）；
            # ② HQ 完成与全屏视图的推进在 `_pump_hq_lifecycle()`（每轮开头）处理。
            # **`request` 只同步做 RGA 缩放**（复制源帧），推理在 TH3 worker 线程内跑（~8 s）。
            ev = self.cap.poll_trigger()
            if ev is not None and self.kf is not None:
                # 「触发」时点 = TH1 FSM 受理（§13「HQ 响应」行的计时起点，`mono_ns`）
                t_trig = time.monotonic_ns()
                self._cur_trig = {
                    "seq_src": int(ev["seq_src"]), "t_trigger_ns": t_trig,
                    "t_freeze_enter_ns": 0, "osd_flip_ns": 0, "osd_flip_seq": 0,
                    "t_fullscreen_ns": 0, "fs_mipi_flip_ns": 0, "fs_mipi_seq": 0,
                    "fs_hdmi_flip_ns": 0, "t_freeze_exit_ns": 0, "exit_reason": "",
                    "hq_model_id": "", "hq_active_shape": 0, "hq_done_ns": 0,
                }
                # **先把「冻结帧 + 处理中」上屏**（`mark_busy` 同步完成合成与翻页）⇒ §6.2 的
                # 「触发 → OSD ≤50 ms」不会被下面的 MFA 取帧（~0.35 s）推迟。
                self._freeze_enter(ev["seq_src"])
                # `request` 返回后本帧即可释放 lease（§5-C：先复制源帧再释放）。
                # **[A/B 开关]** `--hq-mfa-frames N>1` ⇒ 先做 N 帧时域平均（§15 O158），
                # 再以 `pre_scaled=True` 交 TH3（跳过其内部那次 RGA 缩放）。
                if self.hq_mfa_frames > 1:
                    rc_kf = self._mfa_submit(ev["seq_src"], f)
                else:
                    rc_kf = self.kf.request(ev["seq_src"], f)
                if rc_kf != 0:
                    self.log.event("main", "boot_check_item", item="TH3_request",
                                   result="fail", code=starlog.code_name(rc_kf))
                    self.cap.hq_done()          # 受理失败即回 IDLE，避免 FSM 卡在 HQ_BUSY
                    self._cur_trig = None       # 该周期未起 ⇒ 不落 seq 级明细
                    self._freeze_exit()         # 冻结已提前 ⇒ 必须解冻，否则显示停在"处理中"
                else:
                    self.counters["hq_started"] += 1

            # 释放 lease（TH1 引用计数归零后 QBUF 回池）
            self.cap.release(f["lease_id"])

            # TH2b：8 iter 单遍融合（交付内核）
            # **（v3.86 追加 96）** `--no-apply` 时跳过（窗内显示 RGA 原图；**不是** config 键）
            if self.curve is not None and not self.no_apply:
                ra.apply_window(win, self.curve.field, enh, WIN_W, WIN_H)

            # 统计（降采样 4× + 降频 1 Hz；`Stats` 内部自行节流，故可逐帧调用）
            # **（v3.86 裁定 O142-候选①，追加 99）** 统计**下沉 native**：直接传 `win` / `enh` 两个 native
            # 缓冲 ⇒ **零拷贝 + C 统计**（`_n.osd.stats`）；1 Hz 判据仍在 `Stats` 内唯一一处。
            # 原两条路径保留在 `osd.py`（回调式 / `bytes` 直传）作对照与自检，不在帧路径上。
            if self.disp.osd is not None and self.curve is not None:
                self.disp.osd.update_stats(win, enh, WIN_W, WIN_H)
            # **（v3.86 裁定 O143）** step-response 测点：每 2 帧取一次 (mean_orig, mean_enh)
            self._step_sample(i, win, enh)

            # TH4：合成 + OSD + pageflip
            st = {
                "fps": self._fps(),          # O108：**最近 1 s 滑窗**（O107 已删 `mode`；O108 已删 `curve_hz`）
                "lat_total_ms": self._lat_osd(),   # O115：端延时（**1 s 滑窗 P50、1 Hz 刷新**，单值定宽显示）
                "countdown_s": 0,
                "seq": int(f["seq"]),                 # §5-B ControlState（O56）：供 TH4 seq 回填配对
                "t_capture_ns": int(f["t_capture_ns"]),
                "mean_orig": self.disp.osd.stats.mean_orig if self.disp.osd else 0.0,
                "mean_enh": self.disp.osd.stats.mean_enh if self.disp.osd else 0.0,
                "curve_field": self.curve.field if self.curve is not None else None,
                "curve_w": g["eff_w"], "curve_h": g["eff_h"],
                "curve_precision": self.est.precision(),
                "view": self.view,                    # §3.5 视图态（O79）
                "cursor_pos": self.cursor,            # O81：光标每帧画进画布
            }
            r = self.disp.compose_flip(enh, win, st)
            if r == 0:
                self.counters["flips"] += 1
                # TH5 镜像（§3.5 常规态"两屏同画面"；§6.1 O85 行 ≤3 ms，**单列**不计入帧路径合计）
                if self.hdmi is not None:
                    self.hdmi.mirror(int(f["seq"]), self.disp.last_flip_idx())
                if self.dump_canvas_dir and not self._dumped_normal:
                    self._dumped_normal = True
                    self._dump_canvas("normal")           # 常规态**链路原样**导图（一次即可）
                # **[取证钩子]** 常规态逐帧导图（有界：仅前 `--dump-canvas-frames` 帧；默认 0 = 关）。
                # 位置在镜像之后 ⇒ 槽 1 已是**本帧**内容，可做槽 0/槽 1 同帧对拍。
                if self.dump_canvas_dir and self.dump_canvas_frames:
                    n = self._dump_n
                    if self.dump_after <= n < self.dump_after + self.dump_canvas_frames:
                        self._dump_frame(n)
                    self._dump_n += 1
                lp = self.disp.lat_pair()
                if lp is not None:
                    self.lat.append(lp["total_ms"])
                    self.lat_app.append(lp["seg_capture_submit_ms"])
                    self.lat_submit.append(lp["seg_submit_vblank_ms"])
                    # **（v3.86 裁定 O106）** 屏上「延迟两段」= 本次配对的真实两段
                    self._lat_cap_ms = lp["seg_capture_submit_ms"]
                    self._lat_sub_ms = lp["seg_submit_vblank_ms"]
                    # **（v3.86 裁定 O115）** 屏上「端延时」改取**滑窗 P50**：此处只**入窗**，
                    # 屏上取值与刷新节奏交给 `_lat_osd()`（窗内无样本时回退到本值）。
                    self._lat_win.append((time.monotonic(), lp["total_ms"]))
                    self._lat_last_ms = lp["total_ms"]
                    self.pair_ok += 1
            elif r == 1:
                self.counters["flip_skipped"] += 1
            else:
                # **（v3.86 裁定 O122）** 负值 = 显示层翻页失败（TH4 已先做有界自愈）⇒ **显式计数**，
                # 不再静默吞掉（旧实现该情形既不计数也不记日志，实测导致 49 min 只翻 5 帧）。
                self.counters["pageflip_fail"] += 1

            # **（v3.86 裁定 O122）** 显示**失联 / 已恢复**的**边沿**登记：用既有 `degrade_action`
            # 事件（`action=crtc_recover` 取自 §10.5 矩阵「降级动作」列），带 `rsm=` / `fsm=`（§3 要求）。
            # 只在边沿记一次（失联态内不刷屏）⇒ **不新增事件 / 字段 / 返回码**。
            if self.disp.lost_edges != self._pf_lost_seen:
                self._pf_lost_seen = self.disp.lost_edges
                self.log.event("main", "degrade_action", trigger="pageflip_fail",
                               action="crtc_recover",
                               rsm=self.cap.rsm, fsm=self.cap.fsm)
            if self.disp.recover_edges != self._pf_rec_seen:
                self._pf_rec_seen = self.disp.recover_edges
                self.log.event("main", "degrade_action", trigger="pageflip_recover",
                               action="crtc_recover",
                               rsm=self.cap.rsm, fsm=self.cap.fsm)

            i += 1
            if self.max_frames and i >= self.max_frames:
                break
            # **[验证钩子]** 注入的触发全部跑完（每个周期都回到常规态）⇒ 自动收尾，
            # 使 `bench.py --scenario hq` 可确定地终止。生产路径 `_inject_fired == 0` 不受影响。
            if (not self.max_frames and self._inject_fired > 0
                    and len(self.trig_log) >= self._inject_fired
                    and self.cap.fsm == capture_mod.FSM_IDLE
                    and self.view == "normal" and not self.frozen):
                break

        c = self.cap.counters()
        self.counters["dropped_stale"] = c["dropped_stale"]
        self.counters["lease_timeouts"] = c["lease_timeouts"]
        self.counters["v4l2_errors"] = c["v4l2_errors"]
        self.counters["discarded_triggers"] = c["discarded_triggers"]
        # **（v3.86 裁定 O24-前 2 类）** 越阈停机 ⇒ 非 0 退出码（`systemd` 的 `Restart=on-failure`
        # 据此限次拉起；正常退出（信号 / `--frames` / EXIT 键）仍返回 0）。
        return FAULT_EXIT_CODE if self._fault else 0

    # ── 异常恢复（**v3.86 裁定 O24-前 2 类**；§10.5 矩阵两行）────────────────
    def _dmesg_reason(self, since_mono: float) -> str:
        """停机前的**只读**归因：有界（≤64 KiB）扫描 `dmesg` 尾部，只在**同一行**同时命中
        「设备标识」与「错误关键词」且该行时刻 **≥ 最后一次成功取帧**时，归因 `sensor_i2c_dma`（第 2 类）；
        否则回落 `v4l2_stream_lost`（第 1 类）。

        **为什么要三重条件**：板上 `dmesg` 尾部 64 KiB 内 `imx415` 有 350 行（`sensor_lock` 的例行
        `set vblank` / `set analog gain`）⇒ 只按设备标识匹配会**恒判**第 2 类，使两类区分失去意义；
        `dmesg` 的行首 `[<uptime_s>]` 与 `CLOCK_MONOTONIC` 同域，故可直接与"最后一次成功取帧"比较。
        **只读、不改任何状态**；失败（无权限 / 无 `dmesg`）也回落第 1 类。
        """
        try:
            out = subprocess.run(["dmesg"], capture_output=True, timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            return "v4l2_stream_lost"
        blob = (out.stdout or b"")[-DMESG_SCAN_BYTES:].decode("utf-8", "replace")
        for line in blob.splitlines():
            low = line.lower()
            if not any(k in low for k in DMESG_KEYS):
                continue
            if not any(k in low for k in DMESG_ERR_KEYS):
                continue
            if line.startswith("["):
                try:
                    ts = float(line[1:line.index("]")].strip())
                except (ValueError, IndexError):
                    ts = None
                if ts is not None and ts < since_mono:
                    continue                      # 只认"最后一次成功取帧之后"的内核消息
            return "sensor_i2c_dma"
        return "v4l2_stream_lost"

    def _fault_exit(self, streak: int) -> None:
        """越阈 ⇒ `RUN_DEMO → FAULT_EXIT` + 停机（**事件与码零新增**，§10.5 矩阵）。

        日志沿用**既有**事件与字段：`boot_check_item`（承载码 `E_V4L2`，同 `TH3_request` 的既有点位写法）
        + `degrade_action`（边沿一次）+ `rsm_transition`（由 `set_rsm()` 落）+ `service_exit`（退出码）。
        """
        self._fault = True
        self.counters["v4l2_errors"] = int(self.cap.counters()["v4l2_errors"])
        # ① 先记"失败 + 码"（此刻即判据越阈点，不被下面的 `dmesg` 扫描（≤2 s）推迟）
        self.log.event("main", "boot_check_item", item="TH1", result="fail",
                       code=starlog.code_name(capture_mod.E_V4L2))
        # ② 归因（只读、有界）
        reason = self._dmesg_reason(self._last_frame_mono or time.monotonic())
        self._fault_reason = reason
        self.log.event("main", "degrade_action", trigger=reason, action="fault_exit",
                       rsm=self.cap.rsm, fsm=self.cap.fsm)
        # ③ 迁移 + 停机（退出码 ≠ 0 ⇒ systemd `Restart=on-failure` 限次拉起）
        self.cap.set_rsm(capture_mod.RSM_FAULT_EXIT, reason=reason)
        up = int(max(0.0, time.monotonic() - (self._t_start or time.monotonic())))
        self.log.event("main", "service_exit", exit_code=FAULT_EXIT_CODE, uptime_s=up)
        print(f"异常恢复越阈停机：reason={reason} 连续硬错误={streak} "
              f"（阈值 {V4L2_ERR_STREAK_N}）或无帧 ≥{NO_FRAME_TIMEOUT_S:g} s ⇒ "
              f"RUN_DEMO → FAULT_EXIT，退出码 {FAULT_EXIT_CODE}（systemd 限次拉起）")
        self.stop.set()

    # ── 指针 / 注入 / 冻结 / 全屏（**v3.86 落地 O79 / O81 / O84 / O90**）──────
    def _pump_pointer(self) -> None:
        """指针轮询（O81）：常规态判命中区（**增强窗 ≡ 底栏「触发HQ」**，§3.4）；全屏态任意点击退出；
        **「复位·退出」键只保留长按退出**（**v3.86 就地裁定 O98**：短按软复位自演示 UI 移除）。

        **全屏态退出时丢弃本批剩余点击**（v3.86 修正）：`_exit_fullscreen()` 会把 `view` 立即置回
        `normal`，若同批还有点击事件，它们会走"常规态"分支 ⇒ **可能立刻又触发一次 HQ**（多指 / 触控
        与鼠标同批时）。故退出后 `break`——本批剩余点击一律丢弃。

        长按**不依赖抬起事件**：主循环每轮由 `_pump_keys()` 检查按下是否已达阈值。
        """
        if self.ptr is None:
            return
        self.cursor = self.ptr.cursor_pos()
        for c in self.ptr.poll():
            edge = c.get("edge", "down")
            if edge == "up":
                # 抬起只结束按压窗（**短按无动作**，裁定 O98）；是否长按已由 `_pump_keys()` 判定
                self._reset_press_ns = 0
                continue
            if self.view == "fullscreen_hq":
                self._exit_fullscreen(reason="click")      # §3.5：内容区任意处点击即退出
                break                                      # 本批剩余点击丢弃（防立即再触发）
            key = self.buttons.hit(c["x"], c["y"]) if self.buttons is not None else None
            if key is None and self.disp is not None:
                x0, _y0, ww, wh = (int(v) for v in self.disp._geom()["win_enh"])
                if x0 <= c["x"] < x0 + ww and 0 <= c["y"] < wh:
                    key = ui_mod.KEY_TRIGGER_HQ
            if key == ui_mod.KEY_TRIGGER_HQ:
                self.cap.submit_trigger(capture_mod.SRC_TOUCH)   # **单源**（O84）
            elif key == ui_mod.KEY_RESET_EXIT:
                # 只记按下时刻；**长按 ≥ `ui.reset_hold_ms` 由 `_pump_keys()` 判**（裁定 O98）
                self._reset_press_ns = time.monotonic_ns()

    def _pump_keys(self) -> None:
        """长按检查（§10.6-C / 裁定 O98）：按下已超 `ui.reset_hold_ms` 即退出进程（不等抬起边沿）。"""
        if not self._reset_press_ns:
            return
        if (time.monotonic_ns() - self._reset_press_ns) / 1e6 >= self.buttons.hold_ms:
            self._reset_press_ns = 0
            self._exit_key()

    def _exit_key(self) -> None:
        """「复位·退出」键 = **长按退出进程并退回桌面**（§10.6-C / §13「部署与自启动」行；
        **v3.86 就地裁定 O98** 取消其上"短按软复位"语义，软复位机制保留于退出序 `shutdown()`；
        **v3.86 就地裁定 O114**：长按的落地行为由"退出进程、屏停在最后一帧"改为**退回桌面**）。

        发已登记事件 `display_mipi/reset_key`（`press=long`、`seq=`）+ `main/service_exit`；
        随后置 `stop` 由 `main()` 的 `finally` 走正常退出序，**退出序之后**再 `isolate graphical.target`
        （顺序有约束：须先由 `shutdown()` 释放 DRM master，桌面才拿得到 master）。
        """
        self.log.event("display_mipi", "reset_key", press=ui_mod.PRESS_LONG, seq=int(self.hq_seq))
        self.log.event("main", "service_exit", exit_code=0, uptime_s=0)
        self._restore_desktop = True
        self.stop.set()

    def _pump_injection(self) -> None:
        """**[验证钩子]** 触发注入（生产触发源 = 指针）。**时间驱动**——O90 冻结期帧号不推进。"""
        if self.trig_left <= 0 or self.view != "normal":
            return
        now = time.monotonic()
        if self.trig_frame_at and not self._triggered \
                and self.counters["frames"] >= self.trig_frame_at:
            self._triggered = True
            self.cap.submit_trigger(capture_mod.SRC_TOUCH)
            self.trig_left -= 1
            self._inject_fired += 1
            self._next_trig_t = now + self.trig_interval_s
            return
        if self._next_trig_t == 0.0:
            self._next_trig_t = now + self.trig_interval_s
        elif now >= self._next_trig_t:
            self.cap.submit_trigger(capture_mod.SRC_TOUCH)
            self.trig_left -= 1
            self._inject_fired += 1
            self._next_trig_t = now + self.trig_interval_s

    def _pump_hq_lifecycle(self) -> bool:
        """HQ 完成 → 消费结果 → 进全屏（§3.5）；返回 True = 冻结/全屏期（调用方 `continue`，O90）。"""
        if self.kf is None:
            return False
        if self.cap.fsm == capture_mod.FSM_HQ_BUSY and not self.kf.busy():
            res = self.kf.pop_result()
            self.cap.hq_done()                     # TH3 完成 → 触发 FSM 回 IDLE（§5）
            self.counters["hq_done"] += 1
            if res is not None:
                self._enter_fullscreen(res)
        if self.view == "fullscreen_hq":
            ms = int(self.cfg.require_final("ui.fullscreen_timeout_ms"))
            if time.monotonic_ns() - self.fs_enter_ns >= ms * 1_000_000:
                self._exit_fullscreen(reason="timeout")     # 30 s 兜底（§3.5）
            return True
        return self.frozen

    def _enter_fullscreen(self, res: Dict[str, Any]) -> None:
        """该次 HQ 完成即进全屏（§3.5：两屏同时；`reason=click` = 由那次点击触发的 HQ）。"""
        g = self.est.geom
        pad_top = int(g["hq_pad_top"])
        w, h = int(res["shape"][0]), int(res["shape"][1])
        rc = int(_n.rknn_hq.to_rgb888(res["buffer"], self.hq_rgb, w, h, pad_top, h - pad_top))
        if rc != 0:
            self.log.event("main", "boot_check_item", item="HQ_to_rgb888", result="fail", code=rc)
            self._freeze_exit()                    # 进不了全屏即解除冻结（不卡死链）
            return
        self.hq_seq = int(res.get("seq_src", 0))
        rc = self.disp.compose_fullscreen(self.hq_rgb, self.hq_seq, self.hq_seq)
        if rc != 0:
            self._freeze_exit()
            return
        self.view = "fullscreen_hq"
        self.fs_enter_ns = time.monotonic_ns()
        self.counters["fullscreen_enter"] += 1
        self.log.event("display_mipi", "fullscreen_enter", seq=self.hq_seq, reason="click",
                       rsm=self.cap.rsm, fsm=self.cap.fsm)
        if self._cur_trig is not None:
            self._cur_trig.update({
                "t_fullscreen_ns": self.fs_enter_ns,
                "fs_mipi_flip_ns": self.disp.last_flip_ns(),      # MIPI 全屏出图的 vblank（§6.5）
                "fs_mipi_seq": self.disp.last_flip_seq(),
                "hq_model_id": str(res.get("model_id", "")),
                "hq_active_shape": int(res.get("active_shape", 0)),
                "hq_done_ns": int(res.get("done_ns", 0)),
            })
        if self.hdmi is not None:
            self.hdmi.show_hq(self.hq_rgb, self.hq_seq)     # 2.0× 整幅（§3.5 集合 D）
            if self._cur_trig is not None:
                self._cur_trig["fs_hdmi_flip_ns"] = self.hdmi.last_flip_ns()
        self._dump_canvas("fullscreen")

    def _exit_fullscreen(self, reason: str) -> None:
        """退出全屏（`click` / `timeout`）→ 回常规视图并**解除冻结**（§3.5、O90）。"""
        if self.view != "fullscreen_hq":
            return
        self.view = "normal"
        self.counters["fullscreen_exit"] += 1
        if self._cur_trig is not None:
            self._cur_trig["exit_reason"] = reason
        self.log.event("display_mipi", "fullscreen_exit", seq=self.hq_seq, reason=reason,
                       rsm=self.cap.rsm, fsm=self.cap.fsm)
        self._freeze_exit()

    def _freeze_enter(self, seq: int) -> None:
        """冻结起点 = 触发受理（O90）：实时链静止 + "处理中"提示（§6.2：触发→OSD ≤50 ms）。"""
        if self.frozen:
            return
        self.frozen = True
        self._frozen_since = time.monotonic_ns()
        self.counters["freeze_enter"] += 1
        self.log.event("display_mipi", "freeze_enter", seq=int(seq))
        if self._cur_trig is not None:
            self._cur_trig["t_freeze_enter_ns"] = self._frozen_since
        if self.disp is not None and self.disp.mark_busy(int(seq)) == 0:
            self.counters["freeze_flips"] += 1      # 冻结窗内的翻页数（替代作废的 hq_flips_delta）
            if self._cur_trig is not None:
                # 触发 → OSD 提示上屏（§13：≤50 ms）——取该次 flip 的 **vblank** 时刻与回调 seq
                self._cur_trig["osd_flip_ns"] = self.disp.last_flip_ns()
                self._cur_trig["osd_flip_seq"] = self.disp.last_flip_seq()

    def _mfa_submit(self, seq_src: int, f: Dict[str, Any]) -> int:
        """**[A/B 开关]（非契约入口）** §15 O158 的「N 帧时域平均」上板实现。

        口径（O158 ④⑤）：触发受理后取 **N 帧（含触发帧）**，**逐帧** RGA 缩放到 HQ 目标几何
        （`hq_w×hq_h` RGB888，`native/rga_core.c` 的 `RGA_DST_HQ`）后累加进 16bit 累加器
        （核 = native `_starlight_native.mfa`），**每帧 RGA 完立即释放 lease**（= 「取时拷走」，
        **不持 lease ⇒ 不动 V4L2 池的 4 帧容量**）；取均后**写回 HQ 目标缓冲**，再以
        `pre_scaled=True` 交 TH3（跳过其内部那次 RGA）。

        调用时点 = **`_freeze_enter()` 之后**（冻结帧 + 「处理中」已上屏 ⇒ 本段取帧耗时
        不影响 §6.2 的「触发 → OSD ≤50 ms」）。

        返回码与 `KeyframeInfer.request` 同义；取帧不足 N 时**按实得帧数取均**并如实打印。
        """
        g = self.est.geom
        n_px = int(g["hq_w"]) * int(g["hq_h"]) * 3
        if self._mfa_acc is None:
            self._mfa_acc = _n.alloc(n_px * 2)   # 16bit 累加器（960×544 时 3.1 MB）
        hq = _n.rga.hq(g)                        # HQ 目标（**可写** ⇒ 取均结果写回这里）
        want = self.hq_mfa_frames
        _n.mfa.zero(self._mfa_acc)
        got = 0
        # 第 1 帧 = **已持有的触发帧 `f`**（本函数**不释放**它，由主循环统一 `release`）。
        if _n.rga.blit_nv12_fd(int(f["dmabuf_fd"]), kf_mod.RGA_DST_HQ) == 0:
            _n.mfa.accum(hq, self._mfa_acc)
            got += 1
        # 其余 N−1 帧：**逐帧「取 → RGA 复制 → 立即释放」**（= O158 ⑤「取时拷走」）。
        # **关键**：不可先攒齐 lease 再处理 —— V4L2 池只有 **4** 帧，多持一个 lease 就会把池耗尽、
        # 使下一次 `acquire` 超时（实测表现为「取帧 4/6」且 `lease_timeouts` 每次触发 +1）。
        while got < want:
            nf = self.cap.acquire(1, 500)
            if nf is None:
                break
            try:
                if _n.rga.blit_nv12_fd(int(nf["dmabuf_fd"]), kf_mod.RGA_DST_HQ) == 0:
                    _n.mfa.accum(hq, self._mfa_acc)
                    got += 1
            finally:
                self.cap.release(nf["lease_id"])
        if got == 0:
            return kf_mod.E_NPU
        _n.mfa.mean(self._mfa_acc, hq, got)
        print(f"[hq-mfa] seq_src={seq_src} 取帧 {got}/{want} → 取均写回 HQ 目标缓冲（§15 O158）")
        return self.kf.request(seq_src, None, pre_scaled=True)

    def _freeze_exit(self) -> None:
        """冻结终点（HQ 完成并退出全屏后）：累计冻结窗，供 **fps 活动窗口径** 排除（O90）。"""
        if not self.frozen:
            return
        self.freeze_ns += time.monotonic_ns() - self._frozen_since
        self.frozen = False
        self.counters["freeze_exit"] += 1
        self.log.event("display_mipi", "freeze_exit", seq=self.hq_seq)
        if self._cur_trig is not None:              # 一个触发周期结束 ⇒ 落一条 seq 级明细
            self._cur_trig["t_freeze_exit_ns"] = time.monotonic_ns()
            self.trig_log.append(self._cur_trig)
            self._cur_trig = None

    def _dump_canvas(self, tag: str) -> None:
        """链路原样导图（**零色调后处理**）：MIPI 画布 + HDMI 画布各一张，供人工目视与归档。"""
        if not self.dump_canvas_dir or self.disp is None:
            return
        import os as _os
        _os.makedirs(self.dump_canvas_dir, exist_ok=True)
        self.disp.dump_png(_os.path.join(self.dump_canvas_dir, f"canvas_{tag}_mipi.png"), 0)
        if self.hdmi is not None and self.hdmi._drm_ok:
            self.disp.dump_png(_os.path.join(self.dump_canvas_dir, f"canvas_{tag}_hdmi.png"),
                               1, idx=self.hdmi.last_flip_idx())

    def _dump_frame(self, k: int) -> None:
        """**[取证钩子]** 常规态**逐帧**导图：槽 0（MIPI）+ 槽 1（HDMI）各一张。

        文件名 `f{k:02d}_slot{0,1}.png`；`k` = **成功翻页序号**（0 起），与 `DisplayMipi.cur`
        的奇偶一致 ⇒ `k % 2` 即该帧所用画布缓冲索引。与 `_dump_canvas()` 同口径：链路原样、
        **零色调后处理**（规则 `10-evidence.md` §2）。HDMI 未配置时槽 1 跳过（可降级项）。
        """
        if not self.dump_canvas_dir or self.disp is None:
            return
        import os as _os
        _os.makedirs(self.dump_canvas_dir, exist_ok=True)
        self.disp.dump_png(_os.path.join(self.dump_canvas_dir, f"f{k:02d}_slot0.png"), 0)
        if self.hdmi is not None and self.hdmi._drm_ok:
            self.disp.dump_png(_os.path.join(self.dump_canvas_dir, f"f{k:02d}_slot1.png"),
                               1, idx=self.hdmi.last_flip_idx())

    def _dump_curve_once(self, frame_index: int, seq: int) -> None:
        """**[取证钩子]** 首次拿到有效曲线场后**原样**导出一次（默认关闭）。

        `--dump-curve-once PATH`：把 `self.curve.field` 按**其原始字节**写入 `PATH`
        （`field` 可能是 bytes / bytearray / memoryview / pybind buffer ⇒ 统一走
        `bytes(memoryview(...))`，**不假设类型**），随行信息写入 `PATH + ".json"`。
        只导一次（`self._curve_dumped` 守卫）；`PATH` 为空则整段不执行。
        """
        if not self.dump_curve_once or self.curve is None:
            return
        import os as _os
        import json as _json
        self._curve_dumped = True
        raw = bytes(memoryview(self.curve.field))
        _dir = _os.path.dirname(self.dump_curve_once)
        if _dir:
            _os.makedirs(_dir, exist_ok=True)
        with open(self.dump_curve_once, "wb") as fh:
            fh.write(raw)
        g = self.est.geom
        info = {
            "eff_w": int(g["eff_w"]), "eff_h": int(g["eff_h"]),
            "precision": self.est.precision(),
            "bytes": len(raw),
            "frame_index": int(frame_index),
            "seq": int(seq),
        }
        with open(self.dump_curve_once + ".json", "w") as fh:
            _json.dump(info, fh, ensure_ascii=False)

    # ── **[调试钩子] 快照 HTTP 服务**（`--snapshot-port N`；默认 0 = 关闭）────────────
    # **纯 `socket` + `threading` 手写的最小 HTTP/1.1 服务器**（**不用 `http.server`**）：
    # 板端 `cwd` 下存在遮蔽标准库的同名文件（`bisect.py` 等）⇒ `import http.server` 会经
    # `email.utils → random → bisect` 命中仓库内 `bisect.py` 而崩。本实现**只用** `socket` /
    # `os` / `json`（`threading` 已在模块顶部导入）——这几个模块**不传递性拉入** `random` /
    # `bisect` / `statistics`（并**特意避开 `tempfile`**：它 `from random import Random`）。
    # 独立守护线程；导出复用既有 `dump_png(path, 0)`（链路原样、零色调后处理）。
    # **帧路径零改动**：快照只在服务线程内做；每连接异常就地吞掉（返 500），绝不冒泡。
    # 端口建议 8092（与采集工具 8091 错开）。
    _SNAPSHOT_HTML = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>starlight canvas snapshot</title></head>"
        "<body style=\"background:#111;color:#ddd;font-family:monospace\">"
        "<h3>板端 demo 产出的 MIPI 画布（链路原样 / 零色调后处理）</h3>"
        "<p>每 1 s 自动刷新。&nbsp; <code>/canvas.png</code> 当前画布；"
        "<code>/status.json</code> 运行态。</p>"
        "<img id=\"c\" src=\"/canvas.png\" style=\"max-width:100%;border:1px solid #444\">"
        "<script>setInterval(function(){"
        "document.getElementById('c').src='/canvas.png?t='+Date.now();},1000);</script>"
        "</body></html>"
    )
    _SNAPSHOT_REASON = {200: "OK", 404: "Not Found", 405: "Method Not Allowed",
                        500: "Internal Server Error"}

    def _snapshot_png_bytes(self) -> bytes:
        """导**当前 MIPI 画布**为 PNG 字节（**复用既有 `dump_png(path, 0)`**，链路原样）。

        临时文件在 `TMPDIR`（缺省 `/tmp`）下自建（**不用 `tempfile`**——它会 `import random`），
        文件名 = `pid` + 自增序号（加锁，无随机）；读完即删。`self.disp` 未就绪或导出失败则抛异常。
        """
        import os as _os
        if self.disp is None:
            raise RuntimeError("display not ready")
        with self._snapshot_lock:
            n = self._snapshot_seq
            self._snapshot_seq += 1
        tmpdir = _os.environ.get("TMPDIR") or "/tmp"
        path = _os.path.join(tmpdir, f"snapshot_{_os.getpid()}_{n}.png")
        try:
            self.disp.dump_png(path, 0)
            with open(path, "rb") as fh:
                return fh.read()
        finally:
            try:
                _os.unlink(path)
            except OSError:
                pass

    def _snapshot_status(self) -> Dict[str, Any]:
        """`/status.json` 载荷：`seq`（最近翻页）、`frames`、`flip_fail`、`curve_ready`、`snapshot_port`。"""
        c = self.counters
        return {
            "seq": self.disp.last_flip_seq() if self.disp is not None else -1,
            "frames": int(c.get("frames", 0)),
            "flip_fail": int(c.get("pageflip_fail", 0)),
            "curve_ready": self.curve is not None,
            "snapshot_port": int(self.snapshot_port),
        }

    def _snapshot_respond(self, conn, code: int, ctype: str, body: bytes) -> None:
        """写一个带 `Content-Length` + `Connection: close` 的 HTTP/1.1 响应（异常就地吞掉）。"""
        reason = self._SNAPSHOT_REASON.get(code, "OK")
        head = (f"HTTP/1.1 {code} {reason}\r\n"
                f"Content-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n\r\n").encode("latin-1")
        try:
            conn.sendall(head + body)
        except Exception:
            pass

    def _snapshot_handle(self, conn) -> None:
        """处理一条连接：手动解析请求行（只支持 `GET`，忽略请求头）→ 分发 → 关闭。

        **本方法绝不抛异常冒泡**：任何异常就地转 500；连接必然关闭。
        """
        try:
            import json as _json
            conn.settimeout(5.0)
            data = b""
            while b"\r\n" not in data and len(data) < 8192:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            line = data.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
            parts = line.split()
            method = parts[0] if parts else ""
            target = parts[1] if len(parts) > 1 else ""
            path = target.split("?", 1)[0]
            if method != "GET":
                self._snapshot_respond(conn, 405, "text/plain; charset=utf-8",
                                       b"method not allowed")
            elif path == "/canvas.png":
                self._snapshot_respond(conn, 200, "image/png", self._snapshot_png_bytes())
            elif path == "/status.json":
                body = _json.dumps(self._snapshot_status(), ensure_ascii=False).encode()
                self._snapshot_respond(conn, 200, "application/json", body)
            elif path in ("/", "/index.html"):
                self._snapshot_respond(conn, 200, "text/html; charset=utf-8",
                                       self._SNAPSHOT_HTML.encode())
            else:
                self._snapshot_respond(conn, 404, "text/plain; charset=utf-8", b"not found")
        except Exception:
            self._snapshot_respond(conn, 500, "text/plain; charset=utf-8", b"snapshot error")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _snapshot_serve_forever(self, srv) -> None:
        """接受循环（守护线程体）：每连接一个守护子线程；**永不抛异常**。"""
        while True:
            try:
                conn, _addr = srv.accept()
            except Exception:
                return                     # 监听套接字被关/出错 ⇒ 静默退出线程
            threading.Thread(target=self._snapshot_handle, args=(conn,),
                             daemon=True).start()

    def _start_snapshot_server(self) -> None:
        """`--snapshot-port N > 0` 时起后台快照服务；**N = 0 时整段不执行**（不建线程、不占端口）。

        **整体尽力而为**：端口占用 / import 失败 / 任何异常 ⇒ 打印一行、返回、**绝不抛出**。
        """
        if self.snapshot_port <= 0:
            return
        try:
            import socket
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", self.snapshot_port))
            srv.listen(16)
        except Exception as e:
            print(f"[快照服务] 启动失败（端口 {self.snapshot_port}）：{e}")
            return
        try:
            self._snapshot_srv = srv
            threading.Thread(target=self._snapshot_serve_forever, args=(srv,),
                             name="TH-snapshot", daemon=True).start()
            print(f"[快照服务] 已启动：http://0.0.0.0:{self.snapshot_port}/  "
                  f"（/canvas.png 当前 MIPI 画布；/status.json 运行态；建议端口 8092）")
        except Exception as e:
            print(f"[快照服务] 线程启动失败：{e}")

    def _fps(self) -> float:
        """**最近 1 s 滑窗**的帧率（**v3.86 裁定 O108**）。

        口径：取**最近 1 秒内**的成功帧时刻，用**相邻间隔**求速率 `(n-1)/(t_last − t_first)`
        ⇒ 与 §13 O93 的「两点法」同族，**是实测值、不含进程启动、不被历史拖累**（O97 的
        「分母含进程启动」问题在**屏上**由此规避）。
        **与 `bench.py` 报告的 `fps_active` / `fps_steady` 口径不同**（那两值仍是累计口径；
        §15 O97 未裁定）⇒ **屏上值只作现场判读，不构成验收口径**。
        """
        ts = self._fps_ts
        now = time.monotonic()
        ts.append(now)
        while ts and now - ts[0] > FPS_WINDOW_S:
            ts.pop(0)
        if len(ts) < 2:
            return 0.0
        return (len(ts) - 1) / max(1e-6, ts[-1] - ts[0])

    def _lat_osd(self) -> float:
        """屏上「端延时」（**v3.86 裁定 O115**）= **最近 `LAT_WINDOW_S` 内已配对帧的 P50**，
        **每 `LAT_REFRESH_S` 才刷新一次**。

        旧口径（O108）= 逐帧取「最近一次配对的总计」⇒ 数字每帧都变、**人眼读不出**（用户
        2026-09-22 反馈）。改后：值本身抗抖（中位数）、刷新节奏与统计降频同频（1 Hz）；
        窗内**无样本时回退**为最近一次配对值（启动后第一拍即有值，不出现 `---`）。

        该值**只作现场判读**，不构成验收口径——验收仍只认 `bench.py` 报告（§6.5）。
        """
        win = self._lat_win
        now = time.monotonic()
        while win and now - win[0][0] > LAT_WINDOW_S:
            win.pop(0)
        if now - self._lat_refresh_s >= LAT_REFRESH_S:
            self._lat_refresh_s = now
            if win:
                self._lat_total_ms = self._lat_pct(0.50, [v for _t, v in win])
            else:
                self._lat_total_ms = self._lat_last_ms
        return self._lat_total_ms

    def _lat_pct(self, p: float, data: Optional[list] = None) -> float:
        s = sorted(self.lat if data is None else data)
        if not s:
            return 0.0
        return s[min(len(s) - 1, int(len(s) * p))]

    def trig_summary(self) -> Dict[str, Any]:
        """§13「HQ 响应」行的 seq 级汇总（**唯一数据源 = `trig_log`**，供 `main.py` 打印与
        `tools/bench.py --scenario hq` 报告共用）。全部差值取自 `mono_ns`（§6.5 时钟纪律）。
        """
        osd, fs_mipi, fs_hdmi, freeze, reasons = [], [], [], [], {}
        for r in self.trig_log:
            if r["osd_flip_ns"]:
                osd.append((r["osd_flip_ns"] - r["t_trigger_ns"]) / 1e6)
            if r["fs_mipi_flip_ns"]:
                fs_mipi.append((r["fs_mipi_flip_ns"] - r["t_trigger_ns"]) / 1e6)
            if r["fs_hdmi_flip_ns"]:
                fs_hdmi.append((r["fs_hdmi_flip_ns"] - r["t_trigger_ns"]) / 1e6)
            if r["t_freeze_exit_ns"]:
                freeze.append((r["t_freeze_exit_ns"] - r["t_freeze_enter_ns"]) / 1e6)
            k = r["exit_reason"] or "none"
            reasons[k] = reasons.get(k, 0) + 1
        return {"cycles": len(self.trig_log), "osd_ms": osd,
                "fullscreen_mipi_ms": fs_mipi, "fullscreen_hdmi_ms": fs_hdmi,
                "freeze_ms": freeze, "exit_reasons": reasons}

    # ── **（v3.86 裁定 O143）「阶跃响应」验证钩子**（非 config 键 / 不新增事件与返回码）──
    def _step_prepare(self) -> None:
        """解析阶跃用的 subdev 与高低曝光值（**启动期一次**，落在帧窗之外）。"""
        dev = self.step_exp_subdev
        if not dev:
            for cand in ("/dev/v4l-subdev2", "/dev/v4l-subdev3", "/dev/v4l-subdev0",
                         "/dev/v4l-subdev1", "/dev/v4l-subdev4"):
                try:
                    r = subprocess.run(["v4l2-ctl", "-d", cand, "--list-ctrls"],
                                       capture_output=True, text=True, timeout=2)
                except Exception:
                    continue
                if "exposure" in (r.stdout or ""):
                    dev = cand
                    break
        self._step_dev = dev
        base = int(self.cfg.get("camera.exposure_us") or 8974)
        # **曝光方向（重要，v3.86 裁定 O143 复验修正）**：`camera.exposure_us`（实测 66666 µs）
        # 在 15 fps 下**已接近满帧上限**（帧周期 66.7 ms）⇒ **不可再往上加**：4× 会被驱动
        # clamp 成同上限 ⇒ **阶跃成为空操作**（首轮实测场指纹仅变 0.13% 即此因）。
        # 故默认改为**向下阶跃**：高档 = 现值，低档 = 现值 / 4（亮度比同为 4×，方向相反）。
        self._step_high = self.step_exp_high or base
        self._step_low = self.step_exp_low or max(1, base // 4)
        self._step_cur = base
        print(f"[step-response] subdev={dev} low={self._step_low} high={self._step_high} "
              f"times={self.step_exp_times} interval={self.step_exp_interval_s}s")

    def _step_apply(self, value: int) -> bool:
        """运行期曝光微调（规则 20 §4 明许）；**只在阶跃时刻发生**，非稳态开销。"""
        if not getattr(self, "_step_dev", None):
            return False
        try:
            subprocess.run(["v4l2-ctl", "-d", self._step_dev,
                            f"--set-ctrl=exposure={int(value)}"],
                           capture_output=True, text=True, timeout=2, check=True)
            return True
        except Exception as exc:
            print(f"[step-response] 设曝光失败：{exc}")
            return False

    def _step_sample(self, i: int, win, enh) -> None:
        """**（v3.86 裁定 O143 复验修正）** 阶跃响应的**测点** = **增强窗均值**（逐次取样）。

        首轮曾用「曲线场前 256 个 fp16 的均值」作状态量 ⇒ 对亮度**不敏感**（4× 曝光阶跃仅变
        0.1%~0.4%，被判据噪声淹没）⇒ 改为直测语义量：**`mean_orig` / `mean_enh` 的时间序列**
        （走 native 统计，每 2 帧一次 ⇒ 7.5 Hz，成本亚毫秒；**只在阶跃场景开启**）。
        """
        if self.step_exp_times <= 0 or (i % 2):
            return
        try:
            mo, me, _n_s, _ho, _he = _n.osd.stats(win, enh, WIN_W, WIN_H, 4)
        except Exception:                              # 钩子失败不得影响链路
            return
        self._step_series.append((time.monotonic_ns(), int(i), float(mo), float(me)))

    def _step_tick(self, i: int) -> None:
        """帧循环内的**低频**阶跃推进（稳态开销 = 一次整数比较 + 一次时间比较）。"""
        if self.step_exp_times <= 0 or self._step_next >= self.step_exp_times:
            return
        t0 = getattr(self, "_step_t0", None)
        if t0 is None:
            self._step_t0 = time.monotonic()          # 首个活动帧 = 计时起点
            return
        if time.monotonic() < t0 + (self._step_next + 1) * self.step_exp_interval_s:
            return
        value = self._step_low if (self._step_next % 2 == 0) else self._step_high   # 首跳 = 高→低
        prev = self._step_cur
        if self._step_apply(value):
            self.step_log.append({"t_step_ns": time.monotonic_ns(), "frame": int(i),
                                  "from": int(prev), "to": int(value)})
            self._step_cur = value
        self._step_next += 1

    # ── 退出序 ─────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        self.stop.set()
        # TH5 无 DRM 资源（第①批不碰 DRM），先停步进
        if self.hdmi:
            self.hdmi.shutdown()
        # TH3：停 worker 线程并释放 HQ 模型（**在途推理会被等完**，最长 ~8 s、有界）。
        # 必须在 TH1 停流之前——`request` 只做 RGA 缩放（已复制源帧），停流后不再有新请求。
        if self.kf:
            self.kf.shutdown()
        # ① 停发新 lease ② 等在途 lease/fence 有界清空
        if self.cap:
            self.cap.soft_reset()
        # ③ TH1 STREAMOFF + 释放 DMABUF 池
        if self.cap:
            self.cap.shutdown()
        # ④ TH4 释放 CRTC/plane
        if self.est:
            self.est.shutdown()
        if self.disp:
            self.disp.shutdown()
        ra.shutdown()


def metrics_policy(cfg: Any) -> dict:
    """§6.5 计量四键（**v3.86 追加 95 定稿**）：语义 = **期望值 / 断言值**。

    取值唯一来源 = `config.yaml`（§10.4 键表）；**键空即报错**（"空值 = 未定稿"）。
    本函数只读不写，**不新增 config 键 / 日志事件 / 返回码**。

    断言分工（两种不一致的性质不同，处置也不同）：
      · `timestamp_domain` / `pair_by` —— 与**实现**矛盾（实现只有 `mono_ns` / `seq`）
        ⇒ 属配置写错，**硬报错**（`ConfigError`），不得出报告；
      · `timestamp_source` —— 是**运行期实测**（驱动给的 SOE / EOF），偏离期望不属配置错误
        ⇒ **不硬报错**，在报告中标记 `metrics_policy_ok=False` 并随行注明（§6.5：来源须随报告注明；
        窗口作废由既有 `capture/timestamp_source_changed` 事件承担）。
    """
    pol = {"window_frames": int(cfg.require_final("metrics.window_frames")),
           "timestamp_domain": str(cfg.require_final("metrics.timestamp_domain")),
           "timestamp_source": str(cfg.require_final("metrics.timestamp_source")),
           "pair_by": str(cfg.require_final("metrics.pair_by"))}
    if pol["timestamp_domain"] != "MONOTONIC":
        raise starlight_config.ConfigError(
            f"metrics.timestamp_domain={pol['timestamp_domain']!r} ≠ MONOTONIC（§6.5 唯一合法时钟域）")
    if pol["pair_by"] != "seq":
        raise starlight_config.ConfigError(
            f"metrics.pair_by={pol['pair_by']!r} ≠ seq（§6.5 禁用时间就近配对）")
    return pol


def main() -> int:
    p = argparse.ArgumentParser(description="RK3588 极暗光增强实时演示 —— 交付链路主程序")
    p.add_argument("--frames", type=int, default=0,
                   help="跑 N 帧后退出（0 = 直到信号；**配合 `--report-json` 时缺省取 "
                        "`metrics.window_frames`**——§6.5 判定窗下界，键值唯一来源）")
    p.add_argument("--est-every", type=int, default=2, help="每 N 帧估计一次")
    p.add_argument("--hdmi-every", type=int, default=6, help="每 N 帧做一次 HDMI 热插拔步进")
    # **验证钩子**（非生产路径）：在第 N 帧注入一次 `TOUCH` 触发，用于验证 TH3 通路。
    # 生产触发源 = 触控 evdev（§5 TH1「四源共用一套触发状态机」），其接线尚未落地。
    p.add_argument("--trigger-at", type=int, default=0,
                   help="[验证钩子] 第 N 个**活动帧**注入一次 TOUCH 触发")
    p.add_argument("--trigger-times", type=int, default=0,
                   help="[验证钩子] **时间驱动**注入 N 次触发（间隔见 --trigger-interval-s）")
    p.add_argument("--trigger-interval-s", type=float, default=15.0,
                   help="[验证钩子] 注入间隔（秒；默认 15 s ≥ HQ 单次耗时 ~10 s）")
    p.add_argument("--pointer-touch-node", default="", help="[调试] 覆盖触控 evdev 节点")
    p.add_argument("--pointer-mouse-node", default="", help="[调试] 覆盖 ui.pointer_mouse_node")
    p.add_argument("--dump-canvas-dir", default="",
                   help="链路原样导出 MIPI/HDMI 画布 PNG 的目录（**零色调后处理**；常规态一次 + 全屏态各一次）")
    # **[取证钩子]** 常规态**逐帧**导图（有界）：前 N 帧每帧导出槽 0 + 槽 1 各一张，命名
    # `f{帧序:02d}_slot{0,1}.png`。默认 0 = 关闭（既有"常规态只导一次"行为不变）。
    p.add_argument("--dump-canvas-frames", type=int, default=0,
                   help="[取证钩子] 常规态逐帧导出画布 PNG 的帧数（槽 0/槽 1 各一张；0 = 关闭）")
    p.add_argument("--dump-after", type=int, default=0,
                   help="[取证钩子] 跳过前 N 个成功翻页后才开始逐帧转储（用于取**稳态**画面；默认 0）")
    # **[取证钩子]** 首次拿到有效曲线场后**原样**导出一次：`PATH` = 二进制场；`PATH.json` = 随行信息。
    # 默认空 = 关闭（既有行为不变）；**不新增日志事件与 config 键**。
    p.add_argument("--dump-curve-once", default="",
                   help="[取证钩子] 首次有效曲线场原样导出一次（写 PATH 与 PATH.json；默认空 = 关闭）")
    # **[调试钩子] 快照 HTTP 服务**（默认 0 = 关闭）：**N > 0** 时在独立守护线程起**纯 `socket`
    # 手写**的最小 HTTP/1.1 服务（**不用 `http.server`**——板端 `cwd` 下有遮蔽标准库的同名文件）
    # 监听 `0.0.0.0:N`，供**开发机浏览器**看板端 demo 产出的**链路原样** MIPI 画布
    # （`/canvas.png` 复用 `dump_png`、`/status.json` 运行态、`/` 1 s 自刷页）。
    # **非 config 键、不新增日志事件与返回码、帧路径零改动**；端口建议 8092（采集工具用 8091）。
    p.add_argument("--snapshot-port", type=int, default=0,
                   help="[调试钩子] 快照 HTTP 端口（0 = 关闭；建议 8092，采集工具用 8091）")
    # **[计量钩子] 空链路基线**（§6.1 v3.4「必读」段：*"关闭增强，只跑采集 → RGA 原图窗 →
    # MIPI 显示"*；采集方式见 `docs/measurements.md` §1 与 §13「端到端延迟」行的
    # `bench.py --scenario idle-link --breakdown`）。**不是 config 键**、不改任何取值。
    p.add_argument("--idle-link", action="store_true",
                   help="[计量钩子] 空链路基线：关 TH2a / TH2b / OSD，只跑采集 → 原图窗 → MIPI")
    # **[验证用钩子] §15 O24 的确定性注入**（前 2 类异常恢复的判据与处置分支验证；
    # **不是 config 键**、不新增返回码）。
    p.add_argument("--inject-v4l2-error", type=int, default=0,
                   help="[验证钩子] 在活动帧 `--inject-v4l2-at` 起注入 N 次 acquire 硬错误（0 = 关）")
    p.add_argument("--inject-v4l2-at", type=int, default=30,
                   help="[验证钩子] 注入起点（活动帧序号；默认 30）")
    p.add_argument("--inject-v4l2-mode", default="err", choices=["err", "noframe"],
                   help="[验证钩子] err = 硬错误（计 v4l2_errors）/ noframe = 无帧超时（计 lease_timeouts）")
    # **[验证用钩子] §8.1 ①-2 的子项拆分（v3.86 追加 96）**——把「采集→提交」段的增量拆到
    # TH2a / TH2b / OSD 三个来源；**非 config 键、不新增日志事件与返回码**，只在计量路径使用。
    p.add_argument("--no-est", action="store_true",
                   help="[验证钩子] 不请求曲线估计（TH2a worker 不参与帧路径）")
    p.add_argument("--est-first", type=int, default=0,
                   help="[验证钩子] 只前 N 帧请求估计，之后冻结最近发布场（0 = 一直估计）")
    p.add_argument("--no-apply", action="store_true",
                   help="[验证钩子] 跳过 TH2b 增强应用（窗内显示 RGA 原图）")
    # **[A/B 开关] 保比语义**（交付实现选择，**非 config 键、不新增日志事件与返回码**）：
    # **默认走保比语义**：v 取三通道均值、对亮度 L 施加曲线得 L2、按标量 k=L2/L 回乘三通道
    # ⇒ 通道间比值不变（修去饱和）。本**反向开关切回旧逐通道语义，仅供 A/B**。实现见 native/apply_core.c。
    p.add_argument("--no-preserve-ratio", action="store_false", dest="apply_preserve_ratio",
                   default=True,
                   help="[A/B 开关] 切回旧逐通道语义（默认走保比语义；本开关仅供 A/B）")
    p.add_argument("--no-hq-curve-in", action="store_false", dest="hq_curve_in",
                   default=True,
                   help="[A/B 开关] 关闭 HQ 输入侧 ZDE++ 曲线提亮（**2026-10-08 定稿后默认开**；"
                        "本开关仅供 A/B 与回退；非 config 键）")
    p.add_argument("--hq-mfa-frames", type=int, default=6,
                   help="[A/B 开关] HQ 输入侧 N 帧时域平均（§15 O158；**2026-10-08 定稿后默认 6**；"
                        "0/1 = 关闭；>1 每触发取 N 帧取时拷走、逐帧 RGA 后 16bit 累加取均；非 config 键）")
    p.add_argument("--hq-chroma-gain", type=float, default=1.5,
                   help="[A/B 开关] 链路末色度增强增益 k（§15 O161；**2026-10-09 试验档 1.5**"
                        "（GT 标定目标档 = 2.0）；口径 = 保持亮度的线性色度增益 Y+k*(C-Y)，"
                        "作用于 HQ 模型输出之后、OSD 之前；`1.0` = 逐位恒等（回退）；非 config 键）")
    p.add_argument("--hq-input-tm", default="0.8,106",
                   help="[A/B 开关] 曲线后（进模型前）黑位锚定色调映射，格式 \"B,TARGET\""
                        "（§15 O162；**2026-10-09 定稿默认 0.8,106**；B = 黑位锚定比例×曲线后 p1、"
                        "TARGET = 目标 Y 均值；**空串 = 关闭 ⇒ 逐位恒等**；非 config 键）")
    p.add_argument("--hq-tm-first", action="store_true",
                   help="[A/B 实验] 把黑位锚定色调映射置于曲线**之前**（情形 C；默认 = 曲线之后；非 config 键）")
    p.add_argument("--no-osd", action="store_true",
                   help="[验证钩子] OSD 不初始化（状态行 / 曲线图 / 直方图 / 按钮区全不做）")
    # **[验证用钩子] OSD 瘦身两级（v3.86 追加 97）**：把 OSD 的 ①-2 增量（P95 ≈ +30 ms）
    # 再拆成「图示绘制」与「统计」两块。**非 config 键、不新增日志事件与返回码**。
    p.add_argument("--osd-status-only", action="store_true",
                   help="[验证钩子] OSD 只画状态行（跳过曲线图 / 直方图 / 图名位图）")
    p.add_argument("--osd-no-stats", action="store_true",
                   help="[验证钩子] OSD 连统计也不做（`update_stats()` 直接返回；均值字段保持初值）")
    # **[验证用钩子] 「阶跃响应」场景（v3.86 裁定 O143）**：运行期做 N 次**曝光阶跃**
    # （规则 20 §4 明许运行期曝光微调），估计器按每次发布记 (t, seq_est, 场指纹)。
    # **非 config 键、不新增日志事件与返回码**；`bench.py --scenario step-response` 依赖本组开关。
    p.add_argument("--step-exp-times", type=int, default=0,
                   help="[验证钩子] 在本轮做 N 次曝光阶跃（0 = 关；§13 要求 ≥10）")
    p.add_argument("--step-exp-interval-s", type=float, default=3.0,
                   help="[验证钩子] 阶跃间隔（秒；须 ≥ 收敛时间 + 余量）")
    p.add_argument("--step-exp-low", type=int, default=0, help="[验证钩子] 低档曝光（0 = 取 camera.exposure_us）")
    p.add_argument("--step-exp-high", type=int, default=0, help="[验证钩子] 高档曝光（0 = 4× 低档）")
    p.add_argument("--step-exp-subdev", default="", help="[验证钩子] 曝光控件所在 subdev（缺省自动探测）")
    # **[计量出口]** 机器可读报告（供 `tools/bench.py` 编排多轮；**不新增日志事件 / config 键**）。
    # 每轮一个**独立进程**跑本程序 → 满足「全生命周期只 stream on 一次」（规则 30-board-safety §1）。
    p.add_argument("--report-json", default="",
                   help="[计量出口] 退出时把本轮计量写为 JSON（供 bench.py 汇总）")
    p.add_argument("--config", default="", help="config.yaml 路径（默认仓库根）")
    args = p.parse_args()

    cfg = starlight_config.load(args.config or None)
    logger = starlog.from_config(cfg)
    # **（v3.86 追加 95）** 计量四键（§6.5）= 期望值 / 断言值；**键空即报错**（§10.4「空值 = 未定稿」）。
    # **只在计量出口路径接线** ⇒ systemd 演示服务（不传 `--report-json`）不受影响：
    # `--frames` 缺省仍为 0（直到信号），仅 `--report-json` 路径取 `metrics.window_frames` 作窗长。
    pol = metrics_policy(cfg)
    frames = args.frames or (pol["window_frames"] if args.report_json else 0)
    # **[A/B 开关] `--hq-input-tm "B,TARGET"` → 两个浮点**（空串 = 关闭 ⇒ 0/0 ⇒ 逐位恒等）。
    _tm_b, _tm_tgt = 0.0, 0.0
    if args.hq_input_tm:
        _tm_p = str(args.hq_input_tm).split(",")
        if len(_tm_p) == 2:
            _tm_b, _tm_tgt = float(_tm_p[0]), float(_tm_p[1])
    d = Demo(cfg, logger, frames=frames, est_every=args.est_every,
             hdmi_every=args.hdmi_every, trigger_at=args.trigger_at,
             trigger_times=args.trigger_times, trigger_interval_s=args.trigger_interval_s,
             touch_node=args.pointer_touch_node, mouse_node=args.pointer_mouse_node,
             dump_canvas_dir=args.dump_canvas_dir, dump_canvas_frames=args.dump_canvas_frames,
             dump_after=args.dump_after, dump_curve_once=args.dump_curve_once,
             snapshot_port=args.snapshot_port,
             idle_link=args.idle_link,
             inject_v4l2_error=args.inject_v4l2_error, inject_v4l2_at=args.inject_v4l2_at,
             inject_v4l2_mode=args.inject_v4l2_mode,
             no_est=args.no_est, est_first=args.est_first,
             no_apply=args.no_apply, no_osd=args.no_osd,
             apply_preserve_ratio=args.apply_preserve_ratio,
             hq_curve_in=args.hq_curve_in,
             hq_mfa_frames=args.hq_mfa_frames,
             hq_chroma_gain=args.hq_chroma_gain,
             hq_tm_b=_tm_b, hq_tm_target=_tm_tgt, hq_tm_first=args.hq_tm_first,
             osd_status_only=args.osd_status_only, osd_no_stats=args.osd_no_stats,
             step_exp_times=args.step_exp_times, step_exp_interval_s=args.step_exp_interval_s,
             step_exp_low=args.step_exp_low, step_exp_high=args.step_exp_high,
             step_exp_subdev=args.step_exp_subdev)

    def on_sig(signum, _frame):
        logger.event("main", "service_exit", exit_code=0, uptime_s=0)
        d.stop.set()

    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    logger.event("main", "service_start", exit_code=0, uptime_s=0)
    t0 = time.monotonic()
    d._t_start = t0
    rc = d.boot()
    if rc != 0:
        print(f"启动失败 rc={rc}（TH4 需 DRM master：先 `systemctl isolate multi-user.target`）")
        d.shutdown()
        return 1
    # **[口径记录]** 启动耗时（RGA 配置 + 模型加载 + `stream on` + 预热）——**不计入帧窗口**
    t_boot_done = time.monotonic()
    boot_s = t_boot_done - t0
    # **[调试钩子]** `--snapshot-port N > 0` 时起后台快照 HTTP（**默认 0 ⇒ 整段不执行**）。
    # 放在 `t_boot_done` 之后 ⇒ 不计入启动耗时；帧路径零改动（导出只在服务线程内做）。
    # **只挂"成功路径"**：上面 `rc != 0` 时已 `return 1` ⇒ 启动自检失败（含 TH1 的
    # `v4l2_init_failed` ⇒ `BOOT_CHECK → FAULT_EXIT`，其 `cap.init()` 返非 0）**根本走不到这里**。
    # 外面**再包一层 try/except** 作双保险：任何异常只打一行提示、**绝不影响主流程与退出码**。
    try:
        d._start_snapshot_server()
    except Exception as e:
        print(f"[快照服务] 启动异常（已忽略，不影响主流程）：{e}")
    # **（v3.86 裁定 O97）** `t_run_done` = **帧循环退出**时刻（`shutdown()` 之前）
    # ⇒ 帧率分母 = `t_run_done − t_boot_done − 冻结窗`（**扣进程启动与退出序**）。
    t_run_done = 0.0
    try:
        rc = d.run()
        t_run_done = time.monotonic()
    finally:
        # TH3 / TH5 的 `info()` 须在 `shutdown()` **之前**取（shutdown 会 deinit 模型、复位 RGA，
        # 后取则 `last_scale_ns` 等打点已被清零 —— v3.86 修正）
        th3_info = d.kf.info() if d.kf is not None else None
        th5_info = d.hdmi.info() if d.hdmi is not None else None
        d.shutdown()
        # **（v3.86 裁定 O114）** EXIT 键 = 退回桌面：**退出序之后**才切 target（master 须先释放）。
        # 仅 EXIT 路径置位 ⇒ bench / `--frames` / 信号退出不回桌面（§6.5 计量窗口不受扰）。
        if d._restore_desktop:
            _restore_desktop()

    c = d.counters
    wall = max(1e-6, time.monotonic() - t0)
    # **（v3.86 裁定 O97）** 唯一判定口径 = **帧窗**：扣进程启动（`t_boot_done`）与退出序（`t_run_done`）。
    frame_win_s = max(1e-6, (t_run_done or time.monotonic()) - t_boot_done - d.freeze_ns / 1e9)
    fps_window = c['frames'] / frame_win_s
    frame_span_ms = ((d._last_frame_mono - d._first_frame_mono) * 1e3
                     if d._first_frame_mono else None)
    print(f"frames={c['frames']} estimates={c['estimates']} flips={c['flips']} "
          f"flip_skipped={c['flip_skipped']} dropped_stale={c['dropped_stale']} "
          f"lease_timeouts={c['lease_timeouts']} discarded_triggers={c['discarded_triggers']} "
          # **（v3.86 裁定 O122）** 翻页**失败帧数**必须随停机计数给出（不再静默吞掉）
          f"pageflip_fail={c['pageflip_fail']} v4l2_errors={c['v4l2_errors']}")
    if d._fault:
        print(f"[异常恢复] 越阈停机：reason={d._fault_reason} 退出码={FAULT_EXIT_CODE}"
              f"（§10.5 矩阵：RUN_DEMO → FAULT_EXIT ⇒ systemd Restart=on-failure 限次拉起）")
    print(f"wall={wall:.2f}s  冻结窗合计={d.freeze_ns / 1e9:.2f}s  "
          f"fps（**活动窗口径**，O90）={c['frames'] / max(1e-6, wall - d.freeze_ns / 1e9):.1f}")
    # **（v3.86 裁定 O97）判定口径 = 帧窗**（扣进程启动与退出序）；下面两个旧值降为**诊断旁证**。
    print(f"fps（**帧窗口径，O97 判定口径**）={fps_window:.3f}"
          f"（帧窗 {frame_win_s * 1e3:.1f} ms / {c['frames']} 帧；启动 {boot_s:.2f} s 与退出序已在分母外；"
          f"首帧→末帧 {frame_span_ms if frame_span_ms is None else round(frame_span_ms, 1)} ms）")
    # §6.5 延迟口径：按 `seq` 回填配对（禁止按时间就近配对）；配对率与丢弃帧数必须随报告给出
    paired = d.pair_ok
    print(f"配对率={paired / max(1, c['flips']):.4f}"
          f"（已配对 {paired} / flip {c['flips']}；未回填丢弃 {c['flips'] - paired}；"
          f"过期帧丢弃 {c['flip_skipped']}）")
    print(f"延迟·采集→提交（应用侧）    P50={d._lat_pct(0.50, d.lat_app):.1f} ms  "
          f"P95={d._lat_pct(0.95, d.lat_app):.1f} ms")
    print(f"延迟·提交→vblank（显示排队）P50={d._lat_pct(0.50, d.lat_submit):.1f} ms  "
          f"P95={d._lat_pct(0.95, d.lat_submit):.1f} ms")
    print(f"延迟·端到端（采集→vblank）  P50={d._lat_pct(0.50):.1f} ms  "
          f"P95={d._lat_pct(0.95):.1f} ms")
    hz = float(d.cfg.require_final("display.panel_refresh_hz"))
    print(f"实测面板刷新周期={1e3 / hz:.2f} ms（{hz:g} Hz）")
    base = d.cfg.get("metrics.idle_link_baseline")
    if d.idle_link:
        print("链路口径=空链路（`--idle-link`）：关 TH2a / TH2b / OSD"
              "（只有 采集 → RGA 原图窗 → 画布合成 → pageflip）")
    print(f"M1 空链路基线={base if base else '缺失（M1 并行产出）'} ⇒ 超标归因尚不可闭合（§6.5）")
    print(f"f_est={d.est.f_est:.2f} Hz  alpha={d.est.alpha:.4f}")
    print(f"stream_on_count={_n.capture.stream_on_count()}（契约：恒为 1）")
    # TH3：HQ 触发通路（§5）。`hq_flips_delta` = HQ 在途期间 MIPI 的翻页数——
    # **非 0 即证明 TH4 未被 HQ 冻结**（§5「TH4 不得因结果槽空而锁帧」）。
    if d.kf is not None:
        print(f"TH3: hq_started={c['hq_started']} hq_done={c['hq_done']} "
              f"freeze_enter={c['freeze_enter']} freeze_exit={c['freeze_exit']} "
              f"freeze_flips={c['freeze_flips']} "
              f"fullscreen_enter={c['fullscreen_enter']} fullscreen_exit={c['fullscreen_exit']}")
        print(f"TH3 info（shutdown 前）: {th3_info}")
    if d.trig_log:
        ts = d.trig_summary()
        print(f"§13 HQ 响应：周期数={ts['cycles']}  "
              f"触发→OSD P50={d._lat_pct(0.50, ts['osd_ms']):.1f} ms "
              f"P95={d._lat_pct(0.95, ts['osd_ms']):.1f} ms（判据 ≤50）")
        print(f"  触发→全屏 MIPI P50={d._lat_pct(0.50, ts['fullscreen_mipi_ms']):.1f} ms "
              f"P95={d._lat_pct(0.95, ts['fullscreen_mipi_ms']):.1f} ms；"
              f"HDMI P50={d._lat_pct(0.50, ts['fullscreen_hdmi_ms']):.1f} ms（判据 ≤12 s）")
        print(f"  退出 reason={ts['exit_reasons']}  "
              f"冻结窗合计={sum(ts['freeze_ms']) / 1e3:.2f} s（已从 fps 分母排除）")
    if d.hdmi is not None:
        hi = th5_info or {}
        sc = hi.get("scale") or {}
        print(f"TH5 HDMI: drm_ok={hi.get('drm_ok')} flips={hi.get('flips')} "
              f"flip_skipped_slot={hi.get('flip_skipped_slot')} "
              f"末次放大={hi.get('last_scale_ns')} ns")
        print(f"镜像/HQ 放大（§6.1 O85 行 ≤3 ms）n={sc.get('n')} "
              f"P50={sc.get('p50_ms')} ms  P90={sc.get('p90_ms')} ms  max={sc.get('max_ms')} ms")
    if args.report_json:
        # **[计量出口]** 本轮原始计量（供 `tools/bench.py --scenario realtime` 汇总）。
        # 字段取值与本函数上面的打印**同源**；**不新增日志事件 / config 键 / 返回码**。
        import json as _json
        th5 = th5_info or {}
        # **（v3.86 追加 95）** 四键断言结果随报告给出：`timestamp_source` 是运行期实测
        # （驱动给 SOE / EOF），偏离期望**不硬报错**，只标记并注明（§6.5 来源须随报告给出）。
        _ts_src = c.get("ts_source")
        _pol_mismatch = {}
        if _ts_src is not None and str(_ts_src) != pol["timestamp_source"]:
            _pol_mismatch["timestamp_source"] = {"expected": pol["timestamp_source"],
                                                 "measured": str(_ts_src)}
        rec = {
            "wall_s": wall, "boot_s": boot_s, "frames": c["frames"], "flips": c["flips"],
            "freeze_ns": d.freeze_ns,
            # **（v3.86 裁定 O97）** `fps_window` = **唯一判定口径**（帧窗）；`fps_active`（含启动）与
            # `fps_steady`（扣启动、仍含退出序）**降为诊断旁证**（保留是为旧报告可比）。
            "fps_window": fps_window, "window_ms": frame_win_s * 1e3,
            "frame_span_ms": frame_span_ms,
            "fps_active": c["frames"] / max(1e-6, wall - d.freeze_ns / 1e9),
            # **同源第二口径**：分母扣掉进程启动（**§6.5 的"帧滑窗"不含 boot**）；两值同报告给出，
            # 口径以何为准待裁定（§15 已登记），**当前 §11.2 回归基线按 `fps_active` 口径取得**。
            "fps_steady": c["frames"] / max(1e-6, wall - boot_s - d.freeze_ns / 1e9),
            "pairing_rate": paired / max(1, c["flips"]), "paired": paired,
            "counters": dict(c),
            "lat_p50_ms": d._lat_pct(0.50), "lat_p95_ms": d._lat_pct(0.95),
            "lat_app_p50_ms": d._lat_pct(0.50, d.lat_app),
            "lat_app_p95_ms": d._lat_pct(0.95, d.lat_app),
            "lat_submit_p50_ms": d._lat_pct(0.50, d.lat_submit),
            "lat_submit_p95_ms": d._lat_pct(0.95, d.lat_submit),
            "lat_all_ms": list(d.lat), "lat_app_all_ms": list(d.lat_app),
            "lat_submit_all_ms": list(d.lat_submit),
            "f_est": d.est.f_est if d.est else None,
            "alpha": d.est.alpha if d.est else None,
            "panel_refresh_ms": 1e3 / hz, "idle_link_baseline": base,
            # **[计量钩子]** 本轮的链路口径（`--idle-link` = 空链路：关 TH2a / TH2b / OSD）。
            # 报告据此标注口径，**不新增 config 键 / 日志事件 / 返回码**（§6.1 / O19）。
            "idle_link": bool(d.idle_link),
            # **（v3.86 追加 96）** ①-2 子项拆分的路径自述（**非 config 键**）：报告据此可判读
            # 「本轮到底开了哪几项」——`no_est` / `est_first` / `no_apply` / `no_osd`。
            "path_variant": {"idle_link": bool(d.idle_link), "no_est": bool(d.no_est),
                             "est_first": int(d.est_first), "no_apply": bool(d.no_apply),
                             "no_osd": bool(d.no_osd),
                             # [A/B 开关] 曲线应用语义（false = 旧逐通道；true = 保比）。**非 config 键**。
                             "apply_preserve_ratio": bool(d.apply_preserve_ratio),
                             "osd_status_only": bool(d.osd_status_only),
                             "osd_no_stats": bool(d.osd_no_stats)},
            "mirror_scale": th5.get("scale"), "th5": th5, "th3": th3_info,
            "stream_on_count": int(_n.capture.stream_on_count()),
            # **（v3.86 裁定 O24-前 2 类）** 异常恢复：越阈停机的归因与退出码（未触发时 = None）。
            "v4l2_errors": int(c.get("v4l2_errors", 0)),
            "fault": ({"reason": d._fault_reason, "exit_code": FAULT_EXIT_CODE} if d._fault else None),
            "trigger_cycles": d.trig_summary() if d.trig_log else None,
            # **（v3.86 追加 95）** 计量四键（§6.5）= 期望值 / 断言值：报告须与键值一致；
            # `metrics_policy_ok=False` 表示运行期实测偏离期望（如 `timestamp_source`）。
            "metrics_policy": pol,
            "metrics_policy_ok": not _pol_mismatch,
            "metrics_policy_mismatch": _pol_mismatch,
            # **（v3.86 裁定 O143）** 「阶跃响应」证据：阶跃时刻表 + 估计器发布日志
            # （`[(publish_ns, seq_est, 场指纹)]`，**仅在 `--step-exp-times > 0` 时有值**）。
            "step_log": (d.step_log or None),
            "est_publish_log": (d.est.publish_log() if d.est else None),
            "step_series": (d._step_series or None),
        }
        try:
            with open(args.report_json, "w", encoding="utf-8") as fp:
                _json.dump(rec, fp, ensure_ascii=False)
        except OSError as exc:
            print(f"[report-json] 写入失败：{exc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
