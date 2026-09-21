#!/usr/bin/env python3
"""hq_frame_review.py —— M1#5 审查工具（**HQ 线**）：实拍帧 → **§3.1 口径**预处理 → `hq_core` 推理 → PNG

地位：交付工具（§10.3 登记）。效力：**审查素材与量化随行，非验收依据**（规则 `00-ssot.md` §3.5）。
口径出处：主基准 §9.5 的 **O94 块** (b) HQ 线审查手段 / (c) 量化随行；输入口径 = **§3.1
`hq_shape_ladder` 行**（用户 2026-09-21 裁定：**按 §3.1 契约**取口径，而非交付链路现状）；
**不得做非线性后处理** = 规则 `.trae/rules/10-evidence.md` §2。

**输入口径（§3.1，本工具的核心）**：
  `1920×1080` **等比**缩至内容高 **`960×540`**（= 0.5×0.5，RGA 一次 `improcess`）
  → 落在 **`960×544` 缓冲**的第 2..541 行，**上下各 2 行为零 pad**（缓冲池初始化时一次性 memset）
  → 整幅 `960×544` 入模 → 推理 → 输出 **crop 回 `960×540`**。
  pad 行数与内容高由 `curve_estim.resolve_geometry()` 的 `hq_pad_top` / `hq_content_h` 给出
  （**单一来源**；`hq.active_shape` 主档 960×544 ⇒ 540 + 2/2；降级档 768×432 / 512×288 ⇒ pad = 0）。

**与交付链路一致（v3.86 追加 35 后）**：`native/rga_core.c` 的 `RGA_DST_HQ` 目标已按 §3.1 改为
**内容子视图 + 零 pad**，`keyframe_infer.py` 也已把输出裁回内容高 ⇒ **本工具的 HQ 线与交付链路
逐位同源**（同一 `rga.configure` 几何、同一 `hq_core` 调用序）。此前交付链路的"全幅直拉到
960×544（无 pad / 无 crop）"三点偏离**已由用户 2026-09-21 裁定修正**。

用途（板上）：
  cd /home/cat/starlight && PYTHONPATH=/home/cat/starlight \
    python3 tools/hq_frame_review.py --frames 300 --snr-frames 100 --prefix /home/cat/m1_review_hq
产出（同一采帧）：
  · `<prefix>_orig.png` = **原图窗**（RGA NV12 → 512×288 RGB888，链路原样）
  · `<prefix>_hq.png`   = **HQ 增强图**（`960×540`，模型输出裁掉 pad 行后原样映射）
  · `<prefix>_report.json` = 量化随行 + 测量条件（与 `tools/realtime_frame_review.py` 同族口径）

单帧 HQ 推理**约 8~10 s**（`hq.active_shape = 960x544`，core2，O36）⇒ 本工具只对**最后一帧**推理。
量化随行 / 条件随行的实现与**实时线共用**：`import realtime_frame_review`（同目录）取用。

**⚠️ 板上坑**：`/home/cat/starlight/bisect.py` 遮蔽标准库 `bisect` ⇒ 本文件**禁止** `import statistics`
/ `random`。
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # 同级取 realtime_frame_review

from realtime_frame_review import (                              # noqa: E402
    apply_late_overrides, apply_pre_overrides, conditions_snapshot, dark_block_stats,
    find_sensor_subdev, gr_balance, inference_domain_snr, labels_block, max_rgb, mean_rgb,
    write_png,
)

try:
    import _starlight_native as _n
    import capture as capm
    import config as C
    import curve_estim as ce
    import log as L
    _IMPORT_ERR = ""
except ImportError as _e:                                        # 开发机（x86，无交付扩展）
    _n = capm = C = ce = L = None
    _IMPORT_ERR = str(_e)

# O36 裁定：HQ 分核 = `RKNN_NPU_CORE_2`（枚举值 4）
HQ_CORE_MASK = 4
# RGA 目标枚举（与 native/rga_core.h 的 rga_dst_t 一致）
RGA_DST_EST = 0
RGA_DST_WIN = 1
RGA_DST_HQ = 2


def ladder_of(shape):
    """`hq.active_shape` → (W, H, content_h, pad)：**仅用于报告/自洽核对**。

    **口径的唯一来源是 `curve_estim.resolve_geometry()`**（`hq_content_h` / `hq_pad_top`）；
    本函数只把该规则复述一遍用于校验几何是否自洽——**不参与任何缓冲/推理决策**。
    """
    w, h = (int(v) for v in str(shape).lower().split("x"))
    content_h = int(round(w * 9.0 / 16.0))
    pad = (h - content_h) // 2
    return w, h, content_h, pad


def f32_nchw_to_rgb888(buf, w, h, y0, y1):
    """NCHW float32 [0,1] → RGB888（取行区间 `[y0, y1)` = **裁掉 pad 行**）。

    值域映射与 `tools/hq_eval.py` 的 `save_rgb_png` **同口径**：`clip(0,1)*255`（截断取整）。
    **无任何非线性后处理**（规则 `10-evidence.md` §2）。
    """
    a = array.array("f")
    a.frombytes(bytes(buf))
    hw = w * h

    def row_u8(vals):
        return bytes(min(255, int(v * 255.0)) if v > 0.0 else 0 for v in vals)

    out = bytearray()
    for y in range(y0, y1):
        off = y * w
        r = row_u8(a[off:off + w])
        g = row_u8(a[hw + off:hw + off + w])
        b = row_u8(a[2 * hw + off:2 * hw + off + w])
        row = bytearray(w * 3)
        row[0::3] = r
        row[1::3] = g
        row[2::3] = b
        out += row
    return bytes(out)


def pad_rows_zero(buf, w, h, pad_top, content_h):
    """哨兵校验：**上下 pad 行是否仍全零**（§3.1 pad 机制；等价 O32 的哨兵验证）。

    RGA 的 blit 只写内容子视图 ⇒ pad 行应保持缓冲池初始化时的零值。返回 (bool, 非零字节数)。
    """
    if pad_top <= 0:
        return True, 0
    row = w * 3
    top = buf[:pad_top * row]
    bot = buf[(pad_top + content_h) * row:h * row]
    bad = sum(1 for b in top if b) + sum(1 for b in bot if b)
    return bad == 0, bad


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", type=int, default=300,
                    help="采帧数（§13 的复现口径 = 300 帧连续取流；快速目视可给 30）")
    ap.add_argument("--snr-frames", type=int, default=100,
                    help="推理域 SNR 统计用帧数（§13：统计用 ≥100 帧）")
    ap.add_argument("--prefix", default="/home/cat/m1_review_hq")
    ap.add_argument("--warmup", type=int, default=20, help="起流瞬态丢弃帧数")
    # 运行期微调通道（同 `tools/realtime_frame_review.py`）：`--vblank/--exposure` 起流前，
    # `--late-exp/--late-gain` 起流后（起流前写会被 AE 按 IQ 的 `FpsValue` 钳回）。
    ap.add_argument("--vblank", type=int, default=0)
    ap.add_argument("--exposure", type=int, default=0)
    ap.add_argument("--late-exp", type=int, default=0)
    ap.add_argument("--late-gain", type=int, default=0)
    # **人工标签随行**（§13「采集信噪比（推理域）」行要求记录**照度标签**；O94 (c) 的条件随行）
    ap.add_argument("--lux", type=float, default=0.0, help="照度标签（Lux，人工测得；0 = 未记录 ⇒ 报告记 null）")
    ap.add_argument("--scene", default="", help="场景 / 预设标签（如 darkroom / night / face；留空 = 未记录）")
    # **O54 对照（不走 RGA）**：`--dump-nv12` 存该帧原始 NV12；`--in-hq` / `--in-win` 注入
    # 离线准备的 RGB888（显式 CSC）⇒ 同一 `hq_core` 内核、只换 NV12→RGB 这一步。
    ap.add_argument("--dump-nv12", default="", help="另存同一帧的原始 NV12（留空 = 不存）")
    ap.add_argument("--in-hq", default="", help="注入的 HQ 模型输入（hq_w×hq_h×3 RGB888，含零 pad；须与 --in-win 同给）")
    ap.add_argument("--in-win", default="", help="注入的窗输入（win_w×win_h×3 RGB888；须与 --in-hq 同给）")
    a = ap.parse_args()
    if bool(a.in_hq) != bool(a.in_win):
        print("ERR --in-hq 与 --in-win 必须同时给")
        return 1

    if _IMPORT_ERR:
        print("ERR 交付链路模块导入失败（本工具须在板上运行）：%s" % _IMPORT_ERR)
        return 1

    cfg = C.load()
    logger = L.from_config(cfg)
    cap = capm.Capture(cfg, logger)
    est = ce.CurveEstimator(cfg, logger)          # **只用它的几何解析**（不 init，不载 ZDE++）

    # ── 几何：**交付链路同源**（`resolve_geometry` 是 pad/crop 口径的唯一来源；
    # RGA 的 HQ 目标已按 §3.1 落在内容子视图上，本工具不再自行 pad/crop 几何）
    geom = dict(est.resolve_geometry())
    shape = str(cfg.require_final("hq.active_shape"))
    hq_w, hq_h = int(geom["hq_w"]), int(geom["hq_h"])
    pad, content_h = int(geom["hq_pad_top"]), int(geom["hq_content_h"])
    _w, _h, _ch, _pt = ladder_of(shape)          # 自洽核对（防几何被改坏，见 ladder_of 注释）
    if (hq_w, hq_h, content_h, pad) != (_w, _h, _ch, _pt):
        print("ERR 几何不自洽：geom=%s vs §3.1=%s" % ((hq_w, hq_h, content_h, pad), (_w, _h, _ch, _pt)))
        return 1
    win_w, win_h = int(geom["win_w"]), int(geom["win_h"])
    est_w, est_h = int(geom["eff_w"]), int(geom["eff_h"])

    # 模型路径按 `hq.active_shape` 推导（**临时口径**，同 `keyframe_infer.py`；`hq.model_id` 空缺，O30/O72）
    model = os.path.join(ROOT, "models", "hq", "hq_%s_fp16_tk232.rknn" % shape)

    if cap.init() != 0:
        print("ERR cap.init")
        return 1
    # 测量条件随行（§6.5）：**无条件**解析 sensor subdev（只读探测）⇒ 收尾时才能给出
    # 曝光/增益/vblank 回读（同 `tools/realtime_frame_review.py`）。
    subdev = find_sensor_subdev()
    if (a.vblank or a.exposure or a.late_exp or a.late_gain) and subdev is None:
        print("ERR sensor subdev 未找到")
        return 1
    if subdev is not None and (a.vblank or a.exposure):
        apply_pre_overrides(subdev, a.vblank, a.exposure)

    if cap.stream_on_once() != 0:
        print("ERR stream_on_once")
        return 1
    rc = _n.rga.configure(geom)
    if rc != 0:
        print("ERR rga.configure rc=%d" % rc)
        cap.shutdown()
        return 1
    rc = _n.rknn_hq.init(model, {"w": hq_w, "h": hq_h, "core_mask": HQ_CORE_MASK})
    if rc != 0:
        print("ERR rknn_hq.init rc=%d（模型 %s）" % (rc, model))
        _n.rga.shutdown()
        cap.shutdown()
        return 1

    if a.late_exp or a.late_gain:
        if subdev is None:
            subdev = find_sensor_subdev()
        apply_late_overrides(subdev, a.late_exp, a.late_gain)

    win = _n.rga.win(geom)
    snr_frames, snr_ts = [], []
    last_seq, got = -1, 0
    rc_hq_blit = None
    # **O54 对照（不走 RGA）**：注入离线准备的 RGB888（只写一次进既有缓冲；交付模块零改动）
    inj = bool(a.in_hq)
    hq_in_rgb, inj_meta, dump_meta = None, None, None
    if inj:
        hb = open(a.in_hq, "rb").read()
        wb = open(a.in_win, "rb").read()
        if len(hb) != hq_w * hq_h * 3:
            print("ERR --in-hq 尺寸 %d ≠ %d" % (len(hb), hq_w * hq_h * 3))
            return 1
        if len(wb) != win_w * win_h * 3:
            print("ERR --in-win 尺寸 %d ≠ %d" % (len(wb), win_w * win_h * 3))
            return 1
        hq_in_rgb = hb
        win.write(wb)
        inj_meta = {"mode": "no-rga",
                    "in_hq": {"path": a.in_hq, "sha256": hashlib.sha256(hb).hexdigest()},
                    "in_win": {"path": a.in_win, "sha256": hashlib.sha256(wb).hexdigest()},
                    "note": "离线显式 CSC（BT.601 limited，O54 实测值）准备，**不经 RGA**"}
    for _ in range(a.warmup):
        _f = cap.acquire(1, 500)
        if _f is not None:
            cap.release(_f["lease_id"])

    for i in range(a.frames):
        f = cap.acquire(1, 500)
        if f is None:
            continue
        got += 1
        last_seq = f.get("seq")
        fd = int(f["dmabuf_fd"])
        if not inj:
            # 推理域（480×270）：供 SNR 随行；**复制是 C 级 memcpy** ⇒ 不拖慢取流
            _n.rga.blit_nv12_fd(fd, RGA_DST_EST)
            if len(snr_frames) < a.snr_frames:
                snr_frames.append(_n.rga.est_eff(geom).to_bytes())
                snr_ts.append(int(f["t_capture_ns"]))
            # 最后一帧：另取原图窗，并把该帧缩放进 HQ 目标（= §5-C 的「先复制源帧再释放 lease」）
            if i == a.frames - 1:
                _n.rga.blit_nv12_fd(fd, RGA_DST_WIN)
                rc_hq_blit = _n.rga.blit_nv12_fd(fd, RGA_DST_HQ)
        if a.dump_nv12 and i == a.frames - 1:
            nb = _n.capture.buffer(int(f["buffer_index"])).to_bytes()
            with open(a.dump_nv12, "wb") as fh:
                fh.write(nb)
            dump_meta = {"path": a.dump_nv12, "bytes": len(nb),
                         "sha256": hashlib.sha256(nb).hexdigest(), "seq": last_seq,
                         "w": int(_n.capture.width()), "h": int(_n.capture.height()),
                         "stride": int(_n.capture.stride()),
                         "sizeimage": int(_n.capture.sizeimage())}
        cap.release(f["lease_id"])

    if not inj:
        # 末帧若因取帧超时被跳过 ⇒ 补取一帧（有界重试）以便 HQ/WIN 有源
        for _ in range(5):
            if rc_hq_blit is not None:
                break
            f = cap.acquire(1, 500)
            if f is None:
                continue
            last_seq = f.get("seq")
            _n.rga.blit_nv12_fd(int(f["dmabuf_fd"]), RGA_DST_WIN)
            rc_hq_blit = _n.rga.blit_nv12_fd(int(f["dmabuf_fd"]), RGA_DST_HQ)
            cap.release(f["lease_id"])
        if rc_hq_blit is None:
            print("ERR 未取到 HQ 源帧（acquisitions=%d）" % got)
            _n.rknn_hq.deinit()
            _n.rga.shutdown()
            cap.shutdown()
            return 1
        hq_in_rgb = _n.rga.hq(geom).to_bytes()   # **整幅 hq_w×hq_h**（内容 + 零 pad，内核按 §3.1 填）

    # ── HQ：pack（含 §3.1 的上下零 pad）→ 推理 → 裁 pad 回内容高
    pad_ok, pad_bad = pad_rows_zero(hq_in_rgb, hq_w, hq_h, pad, content_h)   # 哨兵校验
    in_f32 = bytearray(hq_w * hq_h * 3 * 4)
    _n.rknn_hq.pack_rgb888_nhwc_f32(hq_in_rgb, in_f32, hq_w, hq_h)
    out = bytearray(hq_w * hq_h * 3 * 4)
    rc_run = _n.rknn_hq.run(in_f32, out)
    run_ns = int(_n.rknn_hq.last_run_ns())
    hq_rgb = (f32_nchw_to_rgb888(out, hq_w, hq_h, pad, pad + content_h)   # 输出 crop 回内容高
              if rc_run == 0 else b"")

    orig = win.to_bytes()
    dk_orig = dark_block_stats(orig, win_w, win_h)
    rep = {
        "tool": "tools/hq_frame_review.py",
        "scope": "M1#5 审查（HQ 线；口径 = §9.5 的 O94 块 (b)(c)，输入口径 = §3.1 hq_shape_ladder）",
        "frames": a.frames, "frames_acquired": got, "warmup": a.warmup, "last_seq": last_seq,
        "hq_input": {
            "active_shape": shape, "content": "%dx%d" % (hq_w, content_h),
            "pad_rows_each_side": pad, "model_input": "%dx%d" % (hq_w, hq_h),
            "policy": "§3.1 主档：1920×1080 等比缩至 %dx%d → 落在 %dx%d 缓冲第 %d..%d 行、"
                      "上下各 %d 行零 pad → 输入 %dx%d；输出 crop 回 %dx%d"
                      % (hq_w, content_h, hq_w, hq_h, pad, pad + content_h - 1, pad,
                         hq_w, hq_h, hq_w, content_h),
            "implemented_by": "**交付链路内核** `native/rga_core.c`（RGA_DST_HQ = 内容子视图 + "
                              "缓冲池初始化一次性 memset 零）+ `keyframe_infer.crop_nchw_f32_pad`"
                              "（输出裁行）⇒ 本工具与交付链路逐位同源",
            "pad_rows_zero": bool(pad_ok), "pad_rows_nonzero_bytes": int(pad_bad),
        },
        "hq_model": {
            "path": model, "core_mask": HQ_CORE_MASK,
            "core_mask_ret": int(_n.rknn_hq.core_mask_ret()),
            "rc": rc_run, "run_ns": run_ns, "rc_hq_blit": rc_hq_blit,
        },
        "orig_win": {
            "size": "%dx%d" % (win_w, win_h),
            "mean_rgb": mean_rgb(orig), "max_rgb": max_rgb(orig),
            "dark_block_sigma_median": dk_orig["sigma_median"],
            "dark_block_mean": dk_orig["r_mean"],
            "gr_balance": gr_balance(orig, win_w, win_h, dk_orig),
        },
        "hq_out": ({
            "size": "%dx%d" % (hq_w, content_h),
            "mean_rgb": mean_rgb(hq_rgb), "max_rgb": max_rgb(hq_rgb),
            "dark_block_sigma_median": dark_block_stats(hq_rgb, hq_w, content_h)["sigma_median"],
            "gr_balance": gr_balance(hq_rgb, hq_w, content_h),
        } if hq_rgb else {"error": "HQ 推理未产出（rc=%d）" % rc_run}),
        "inference_snr": (inference_domain_snr(snr_frames, est_w, est_h, snr_ts) if not inj else
                          {"skipped": "no-RGA 注入模式：输入为离线准备单帧，无相邻帧对 ⇒ 时域项不可算"}),
        "input_path": ("no-rga（离线显式 CSC 注入）" if inj else "rga"),
        "injected": inj_meta,
        "nv12_dump": dump_meta,
        "conditions": conditions_snapshot(subdev),
        "labels": labels_block(a.lux, a.scene),
        "files": [], "png_bytes": {},
    }
    rep["png_bytes"]["orig"] = write_png("%s_orig.png" % a.prefix, orig, win_w, win_h)
    rep["files"].append("%s_orig.png" % a.prefix)
    if hq_rgb:
        rep["png_bytes"]["hq"] = write_png("%s_hq.png" % a.prefix, hq_rgb, hq_w, content_h)
        rep["files"].append("%s_hq.png" % a.prefix)
    rep["files"].append("%s_report.json" % a.prefix)
    with open("%s_report.json" % a.prefix, "w") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=2)
    print(json.dumps(rep, ensure_ascii=False))
    sys.stdout.flush()

    _n.rknn_hq.deinit()
    _n.rga.shutdown()
    cap.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())