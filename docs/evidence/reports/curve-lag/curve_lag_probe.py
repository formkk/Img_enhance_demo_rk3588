#!/usr/bin/env python3
"""curve_lag_probe.py - 增强窗拖影的定量取证：曲线场滞后（est_every + EMA）对输出的影响

地位：证据脚本（docs/evidence/，非 10.3 交付模块）。效力：审查素材与量化随行，非验收依据。
口径出处：主基准 9.1（EMA 公式 + N/f_est <= 400 ms）、10.4（pipeline.est_freq_hz /
pipeline.ema_alpha）、13「增强响应」行（<=500 ms）、13「实时画质」行（移动无明显拖影）、15 的 O57。
纪律：不动交付链路（只调用既有模块与既有 native 符号）；链路输出原样（PNG 直出、零色调后处理）。

假说：交付链在帧 k 用的曲线场 = 上一次估计（<= est_every 帧前）经 EMA 平滑后的结果 =>
曲线场对场景存在滞后；场景运动时增强窗的色调映射滞后于画面内容 => 目视「拖影」。
滞后量 = 离散滞后（0..est_every-1 帧）+ EMA 收敛时间 ~ N/f_est（N = ln0.1/ln alpha）。
若 config 的 alpha 仍是按 30 fps 的 f_est~9.47 Hz 推得（alpha=0.4642 => 317 ms），而 O91 已把
实时链改为 15 fps（f_est~2.5 Hz）=> 实际收敛 ~1.2 s 远大于 400 ms（违反 9.1 自身规则）。

方法（同帧换曲线，唯一变量 = 曲线场）：一次取流，逐帧落盘交付链路自身的缓冲 W_k（窗输入）
与 EST_k（估计输入）；随后在板端离线复算 参照（零滞后，用第 k 帧自身曲线）与 链路实际
（按 (est_every, alpha) 递推 EMA），多组候选在同一批帧上对照。

用法（板上，须停桌面 + 停交付链路，二者独占 /dev/video11）：
  cd /home/cat/starlight && python3 tools/curve_lag_probe.py --frames 60 --scene night --lux 1.0

板上坑：/home/cat/starlight/bisect.py 遮蔽标准库 bisect => 禁止 import statistics / random。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
for _cand in (ROOT, os.path.dirname(ROOT), "/home/cat/starlight"):
    if os.path.exists(os.path.join(_cand, "config.py")):
        ROOT = _cand
        break
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import _starlight_native as _n                    # noqa: E402

import capture as capm                            # noqa: E402
import config as C                                # noqa: E402
import curve_estim as ce                          # noqa: E402
import log as L                                   # noqa: E402
import realtime_apply as ra                       # noqa: E402

import realtime_frame_review as rfr               # noqa: E402

CANDIDATES = [
    (6, 0.4642, "交付现值（alpha 按 30 fps 的 f_est=9.47 Hz 推得）"),
    (6, 0.1, "仅压 alpha：f_est=2.5 Hz 下 N=1 => 400 ms"),
    (3, 0.3162, "折中：f_est=5.0 Hz 下 N=2 => 400 ms"),
    (2, 0.4642, "只提频率：f_est=7.5 Hz 下 N=3 => 400 ms"),
]
DELIVERED = (6, 0.4642)


def _mean_abs(a: bytes, b: bytes) -> float:
    s = 0
    n = 0
    for x, y in zip(a, b):
        s += x - y if x >= y else y - x
        n += 1
    return s / n if n else 0.0


def _curve_rel_err(cur_a: bytes, cur_b: bytes, elems: int, sample: int = 4096) -> dict:
    step = max(1, elems // sample)
    sa = sb = 0.0
    n = 0
    for i in range(0, elems, step):
        va = struct.unpack_from("<e", cur_a, i * 2)[0]
        vb = struct.unpack_from("<e", cur_b, i * 2)[0]
        d = va - vb
        sa += d if d >= 0 else -d
        sb += vb if vb >= 0 else -vb
        n += 1
    return {"mean_abs_diff": round(sa / n, 6), "mean_abs_ref": round(sb / n, 6),
            "rel": round((sa / n) / (sb / n), 5) if sb else None, "samples": n}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", type=int, default=60, help="逐帧录取帧数（15 fps 下 60 帧 ~ 4.0 s）")
    ap.add_argument("--prefix", default="/home/cat/curve_lag")
    ap.add_argument("--lux", type=float, default=0.0, help="照度标签（人工测得；0 = 未记录）")
    ap.add_argument("--scene", default="", help="场景标签（如 darkroom / night / face）")
    ap.add_argument("--png-frames", type=int, default=3,
                    help="导出 PNG 的运动最强帧数（0 = 不导图；每帧 4 张）")
    ap.add_argument("--synth-frames", type=int, default=0,
                    help=">0 = 改用**合成运动**：只实拍 1 帧，随后对其做受控水平循环平移，"
                         "生成该帧数条序列（确定性、可复现，无需人工制造运动）")
    ap.add_argument("--synth-shift-src", type=int, default=60,
                    help="合成运动的**源图像素**位移/帧（窗位移 = d/3.75、估计位移 = d/4，取整）")
    a = ap.parse_args()

    cfg = C.load()
    logger = L.from_config(cfg)
    cap = capm.Capture(cfg, logger)
    est = ce.CurveEstimator(cfg, logger)

    if cap.init() != 0:
        print("ERR cap.init")
        return 1
    subdev = rfr.find_sensor_subdev()
    t_stream = time.monotonic()
    if cap.stream_on_once() != 0:
        print("ERR stream_on_once")
        return 1
    if est.init() != 0:
        print("ERR est.init")
        return 1
    ra.shutdown()
    if ra.configure_from_config(cfg) != 0:
        print("ERR ra.configure")
        return 1

    g = est.geom
    win_w, win_h = int(g["win_w"]), int(g["win_h"])
    eff_w, eff_h = int(g["eff_w"]), int(g["eff_h"])
    est_buf = _n.rga.est(g)
    win = _n.rga.win(g)
    enh = _n.rga.enh(g)
    plane = 3 * eff_w * eff_h
    cur_buf = _n.alloc(2 * plane)

    def _rotate_rgb(buf: bytes, w: int, h: int, dx: int) -> bytes:
        """按行对 RGB888 做**水平循环平移**（受控合成运动；无边界引入）。"""
        dx %= w
        if dx == 0:
            return buf
        rb = w * 3
        sh = dx * 3
        out = bytearray(len(buf))
        for y in range(h):
            b = y * rb
            out[b:b + rb] = buf[b + sh:b + rb] + buf[b:b + sh]
        return bytes(out)

    W, EST, SEQ, TS = [], [], [], []
    synth = a.synth_frames > 0
    if not synth:
        for _k in range(a.frames):
            f = cap.acquire(1, 500)
            if f is None:
                continue
            _n.rga.blit_nv12_fd(int(f["dmabuf_fd"]), 1)
            W.append(win.to_bytes())
            _n.rga.blit_nv12_fd(int(f["dmabuf_fd"]), 0)
            EST.append(est_buf.to_bytes())
            SEQ.append(f.get("seq"))
            TS.append(int(f["t_capture_ns"]))
            cap.release(f["lease_id"])
        n = len(W)
    else:
        # 实拍 5 帧：量帧周期 + 取第 0 帧为合成基帧（其后全部由平移派生）
        base = None
        for _k in range(5):
            f = cap.acquire(1, 500)
            if f is None:
                continue
            _n.rga.blit_nv12_fd(int(f["dmabuf_fd"]), 1)
            _w0 = win.to_bytes()
            _n.rga.blit_nv12_fd(int(f["dmabuf_fd"]), 0)
            _e0 = est_buf.to_bytes()
            if base is None:
                base = (_w0, _e0)
            TS.append(int(f["t_capture_ns"]))
            cap.release(f["lease_id"])
        n = a.synth_frames
        d_src = max(0, a.synth_shift_src)
        dxw = int(round(d_src / 3.75))
        dxe = int(round(d_src / 4.0))
        period = ((TS[-1] - TS[0]) / 1e6 / (len(TS) - 1)) if len(TS) > 1 else 66.67
        TS = []                       # 合成时间轴重建（清空实拍时间戳，避免 span 失真）
        for k in range(n):
            W.append(_rotate_rgb(base[0], win_w, win_h, dxw * k))
            EST.append(_rotate_rgb(base[1], int(g["pad_w"]), int(g["pad_h"]), dxe * k))
            SEQ.append(k + 1)
            TS.append(int(k * period * 1e6))
        a.frames = n
    if n < 4:
        print("ERR 取帧不足（%d）" % n)
        est.shutdown()
        cap.shutdown()
        return 1
    span = (TS[-1] - TS[0]) / 1e9
    frame_period_ms = 1000.0 * span / (n - 1) if n > 1 else 66.67

    def _apply_shot(k: int) -> bytes:
        win.write(W[k])
        if ra.apply_window(win, cur_buf, enh, win_w, win_h) != 0:
            raise RuntimeError("apply_window 失败 @k=%d" % k)
        return enh.to_bytes()

    fresh_png, fresh_curve = [], []
    for k in range(n):
        est_buf.write(EST[k])
        if _n.rknn.estimate(est_buf, cur_buf, 1, 0.0, 0, 1) != 0:
            print("ERR estimate(fresh)")
            est.shutdown()
            cap.shutdown()
            return 1
        fresh_curve.append(cur_buf.to_bytes())
        fresh_png.append(_apply_shot(k))

    results = []
    cand_png = {}
    for every, alpha, why in CANDIDATES:
        first = True
        snap = [None] * n
        out = []
        for k in range(n):
            if k % every == 0:
                est_buf.write(EST[k])
                if _n.rknn.estimate(est_buf, cur_buf, 1, alpha, 1, 1 if first else 0) != 0:
                    print("ERR estimate(chain)")
                    est.shutdown()
                    cap.shutdown()
                    return 1
                first = False
                snap[k] = cur_buf.to_bytes()
            out.append(_apply_shot(k))
        cand_png[(every, alpha)] = out

        d_chain = [_mean_abs(out[k], fresh_png[k]) for k in range(n)]
        c_rel = [_curve_rel_err(snap[k], fresh_curve[k], plane)["rel"]
                 for k in range(n) if snap[k] is not None]
        nconv = math.log(0.1) / math.log(alpha)
        f_est = 1000.0 / (every * frame_period_ms)
        results.append({
            "est_every": every, "alpha": alpha, "why": why,
            "N_ema": round(nconv, 3), "f_est_hz": round(f_est, 3),
            "conv_ms": round(nconv / f_est * 1000.0, 1),
            "conv_ok_400ms": bool(nconv / f_est <= 0.4),
            "discrete_lag_ms_p50": round((every - 1) / 2.0 * frame_period_ms, 1),
            "mean_abs_diff_p50": round(rfr._median(d_chain), 3),
            "mean_abs_diff_p95": round(rfr._percentile(d_chain, 0.95), 3),
            "mean_abs_diff_max": round(max(d_chain), 3),
            "curve_rel_err_p50": round(rfr._median(c_rel), 5) if c_rel else None,
            "per_frame": [round(v, 3) for v in d_chain],
        })

    motion = [_mean_abs(fresh_png[k], fresh_png[k - 1]) for k in range(1, n)]
    motion_p50 = rfr._median(motion)
    for r in results:
        r["motion_ref_p50"] = round(motion_p50, 3)
        r["ghost_ratio"] = (round(r["mean_abs_diff_p50"] / motion_p50, 3) if motion_p50 else None)

    # ── x–t 诊断图：时域滞后在「宽=窗宽 / 高=帧数」的图上就是竖向错位 ──────────────
    def _xt(frames_png, tail):
        y0, y1 = 100, 188
        nb = y1 - y0
        rows = []
        for f in frames_png:
            acc = [0] * win_w
            for y in range(y0, y1):
                base = y * win_w * 3
                for x in range(win_w):
                    acc[x] += f[base + x * 3]
            rows.append(bytes((v // nb) for v in acc))
        rgb = bytearray()
        for r in rows:
            for v in r:
                rgb += bytes((v, v, v))
        path = "%s_xt_%s.png" % (a.prefix, tail)
        rfr.write_png(path, bytes(rgb), win_w, len(rows))
        return path

    xt_best = min((r for r in results if (r["est_every"], r["alpha"]) != DELIVERED),
                  key=lambda r: r["ghost_ratio"] or 0)
    xt = {
        "fresh__zero_lag": _xt(fresh_png, "fresh"),
        "chain__delivered_e06_a04642": _xt(cand_png[DELIVERED], "delivered"),
        "chain__best_e%02d_a%s" % (xt_best["est_every"],
                                   str(xt_best["alpha"]).replace(".", "")):
            _xt(cand_png[(xt_best["est_every"], xt_best["alpha"])], "best"),
    }
    print("x-t diagnostics: %s" % xt)
    sys.stdout.flush()

    png_meta = []
    if a.png_frames > 0:
        # 「最优候选」= **除交付现值外**滞后误差最小的那组（即候选修复，而非最差的那组）
        best = min((r for r in results
                    if (r["est_every"], r["alpha"]) != DELIVERED),
                   key=lambda r: r["ghost_ratio"] or 0)
        best_key = (best["est_every"], best["alpha"])
        best_tag = "e%02d_a%s" % (best["est_every"], str(best["alpha"]).replace(".", ""))
        deliv_tag = "e%02d_a%s" % (DELIVERED[0], str(DELIVERED[1]).replace(".", ""))
        order = sorted(range(1, n), key=lambda k: motion[k - 1], reverse=True)[:a.png_frames]
        for k in sorted(order):
            tag = "%s_f%02d" % (a.prefix, k)
            files = {"orig": "%s_orig.png" % tag, "fresh": "%s_fresh.png" % tag,
                     "chain_delivered": "%s_chain_%s.png" % (tag, deliv_tag),
                     "chain_best": "%s_chain_%s.png" % (tag, best_tag)}
            rfr.write_png(files["orig"], W[k], win_w, win_h)
            rfr.write_png(files["fresh"], fresh_png[k], win_w, win_h)
            rfr.write_png(files["chain_delivered"], cand_png[DELIVERED][k], win_w, win_h)
            rfr.write_png(files["chain_best"], cand_png[best_key][k], win_w, win_h)
            png_meta.append({"frame": k, "seq": SEQ[k],
                             "motion_absdiff": round(motion[k - 1], 3), "files": files})

    rep = {
        "tool": "docs/evidence/reports/curve-lag/curve_lag_probe.py",
        "scope": "增强窗拖影的定量取证：曲线场滞后（est_every + EMA）对输出的影响",
        "frames": n, "frame_period_ms_p50": round(frame_period_ms, 3),
        "capture_span_s": round(span, 3),
        "win": "%dx%d" % (win_w, win_h), "est": "%dx%d" % (eff_w, eff_h),
        "config_est_freq_hz": cfg.get("pipeline.est_freq_hz"),
        "config_ema_alpha": cfg.get("pipeline.ema_alpha"),
        "est_cost_ns_last": _n.rknn.last_estimate_ns(),
        "stimulus": ("合成运动：单帧受控水平循环平移（源 %d px/帧；窗 %d px、估计 %d px；"
                     "无边界引入）" % (a.synth_shift_src,
                                       int(round(a.synth_shift_src / 3.75)),
                                       int(round(a.synth_shift_src / 4.0)))
                     if a.synth_frames > 0 else "实拍序列"),
        "reference": "零滞后 = 用第 k 帧自身估计的曲线（use_ema=0）作用于第 k 帧的窗输入",
        "metric": "mean|E_chain-E_fresh|（RGB888 平面逐字节 |d| 均值）；ghost_ratio = 该值 / 帧间变化",
        "candidates": results,
        "pngs": png_meta,
        "xt_bg888_note": "x-t 图在此直出为 RGB888（每像素 R=G=B=行带均值）；读图时只看灰度通道即可",
        "first_seq": SEQ[0], "last_seq": SEQ[-1],
        "conditions": rfr.conditions_snapshot(subdev),
        "labels": rfr.labels_block(a.lux, a.scene),
        "stream_on_count": _n.capture.stream_on_count(),
        "uptime_s": round(time.monotonic() - t_stream, 2),
    }
    with open("%s_report.json" % a.prefix, "w") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=2)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    sys.stdout.flush()

    est.shutdown()
    cap.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
