#!/usr/bin/env python3
"""zde_single_frame_review.py —— O87 人工审查用：**采一帧 → 走交付链路推理 → 出两图**

**（v3.86 追加 35）已转正为交付工具 `tools/realtime_frame_review.py`**（O94 (a) 待办；登记 §10.3）——
**本文件自此为 O87/O88/O91 判定当时的留档件，请勿再用于新审查**（新审查用工具，它多了量化随行：
推理域 SNR / 通道平衡 G/R / mean·max / IQ 档位 + sha256 / 曝光·增益·vblank 回读 / config 快照哈希）。

用途：在指定照度下（本轮 = 1 Lux）采一帧，经**与交付链路完全相同的模块**（TH1 `Capture` +
TH2a `CurveEstimator`（RGA + ZDE++ 2 条带 + EMA）+ TH2b `realtime_apply.apply_window` 8-iter 单遍融合）
产出两张图供人工审核：
  · `<prefix>_orig.png` = **原图窗**（RGA NV12 → 512×288 RGB888，链路原样）
  · `<prefix>_enh.png`  = **增强窗**（8-iter 应用结果，链路原样）
**不做任何非线性后处理**（规则 `.trae/rules/10-evidence.md` §2：不得生成 γ 副本）；PNG 为
**直出编码**（RGB888 → PNG，零滤镜）。

**为什么不停桌面**：本脚本**不初始化显示模块**（不做 DRM/pageflip），只取流 + 推理 + 落盘 ⇒ 无 master 需求。

用法（板上）：
  cd /home/cat/starlight && PYTHONPATH=/home/cat/starlight \
    python3 <本脚本> --frames 30 --every 6 --prefix /home/cat/o87_review_1lux

口径与出处：链路调用序照 `docs/evidence/M1-collect-tool-board.md` §14.3 与 `bisect.py`（板上实测可跑）。
"""

import argparse
import json
import struct
import sys
import time
import zlib

import _starlight_native as _n
import capture as capm
import config as C
import curve_estim as ce
import log as L
import realtime_apply as ra


def write_png(path, rgb, w, h):
    """RGB888 → PNG，**无任何后处理**（filter=None）。"""
    stride = w * 3
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))   # 8bit truecolor
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)
    return len(png)


def mean_rgb(b):
    n = len(b)
    return [round(sum(b[i::3]) / (n / 3), 2) for i in range(3)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("--every", type=int, default=6, help="每 N 帧做一次估计（与 main.py est_every 同义）")
    ap.add_argument("--prefix", default="/home/cat/o87_review")
    ap.add_argument("--window", default="512x288", help="仅供记录（契约常量 512×288）")
    # **（v3.86 追加 9）降帧率换曝光**：在 cap.init()（含 sensor_lock 的 30 fps 锁定）**之后**、
    # stream_on **之前**覆盖 vblank / exposure，并**回读校验**。不改 IQ、不改 DT（规则 §4 明许运行期微调）。
    # 例（+1 stop）：--vblank 7906 --exposure 8910  ⇒ VMAX 9002 ⇒ 15 fps、曝光 ≈66 ms
    ap.add_argument("--vblank", type=int, default=0)
    ap.add_argument("--exposure", type=int, default=0)
    # **（v3.86 追加 17）组合档通道**：**起流前**写曝光会被 AE 按 IQ 的 `FpsValue` **钳回**（§12.13 实测：
    # 起流前写 8910 → 流中被压回 4492）；**起流后**（AE 已收敛）再写则**站住**（§12.18 后补充实测：
    # 1/4/7 s 回读均 8910）。故组合档（66 ms + 高增益）走 `--late-exp` / `--late-gain`。
    ap.add_argument("--late-exp", type=int, default=0)
    ap.add_argument("--late-gain", type=int, default=0)
    # **（v3.86 追加 18）单流多臂**：`--arms 90,120` = 同一条流内依次把 `analogue_gain` 设为 90 / 120
    #  各采一组；`--warmup` = 每臂先丢弃的帧数（避开起流瞬态）。**跨 run 比较不可靠，以本模式为准**。
    ap.add_argument("--arms", default="")
    ap.add_argument("--warmup", type=int, default=20)
    a = ap.parse_args()

    cfg = C.load()
    logger = L.from_config(cfg)
    cap = capm.Capture(cfg, logger)
    est = ce.CurveEstimator(cfg, logger)

    if cap.init() != 0:
        print("ERR cap.init"); return 1

    # **（v3.86 追加 9）** vblank 先设（抬高 VMAX，才允许更长曝光），再设 exposure；随后回读校验。
    subdev = None
    if a.vblank or a.exposure:
        import os
        import subprocess
        # 解析 sensor subdev：判据 = 「唯一暴露 vertical_blanking 的 v4l-subdev」（§9.5 ③ 同族）
        devs = sorted("/dev/" + n for n in os.listdir("/dev") if n.startswith("v4l-subdev"))
        for d in devs:
            r = subprocess.run(["v4l2-ctl", "-d", d, "--list-ctrls"],
                               capture_output=True, text=True)
            if "vertical_blanking" in r.stdout:
                subdev = d
                break
        if subdev is None:
            print("ERR sensor subdev 未找到"); return 1
        if a.vblank:
            subprocess.run(["v4l2-ctl", "-d", subdev, "--set-ctrl=vertical_blanking=%d" % a.vblank], check=False)
        if a.exposure:
            subprocess.run(["v4l2-ctl", "-d", subdev, "--set-ctrl=exposure=%d" % a.exposure], check=False)
        rb = subprocess.run(["v4l2-ctl", "-d", subdev, "--get-ctrl=vertical_blanking,exposure"],
                            capture_output=True, text=True).stdout.strip().replace("\n", " ")
        print("[override] subdev=%s  回读=%s" % (subdev, rb))
        sys.stdout.flush()

    if cap.stream_on_once() != 0:
        print("ERR stream_on_once"); return 1
    if est.init() != 0:
        print("ERR est.init"); return 1
    ra.shutdown()
    if ra.configure_from_config(cfg) != 0:
        print("ERR ra.configure"); return 1

    # **（v3.86 追加 17）** 组合档：等 3A 收敛后再写曝光/增益（起流后写才站得住），随后回读校验。
    if a.late_exp or a.late_gain:
        import subprocess
        import time
        if subdev is None:
            import os
            for _d in sorted("/dev/" + _n for _n in os.listdir("/dev") if _n.startswith("v4l-subdev")):
                _r = subprocess.run(["v4l2-ctl", "-d", _d, "--list-ctrls"], capture_output=True, text=True)
                if "vertical_blanking" in _r.stdout:
                    subdev = _d
                    break
        time.sleep(2.0)
        if a.late_exp:
            subprocess.run(["v4l2-ctl", "-d", subdev, "--set-ctrl=exposure=%d" % a.late_exp], check=False)
        if a.late_gain:
            subprocess.run(["v4l2-ctl", "-d", subdev, "--set-ctrl=analogue_gain=%d" % a.late_gain], check=False)
        time.sleep(1.5)
        _rb = subprocess.run(["v4l2-ctl", "-d", subdev, "--get-ctrl=exposure,analogue_gain,vertical_blanking"],
                             capture_output=True, text=True).stdout.strip().replace("\n", " ")
        print("[late-override] %s" % _rb)
        sys.stdout.flush()

    g = est.geom
    win = _n.rga.win(g)
    enh = _n.rga.enh(g)
    last_seq = -1

    # **（v3.86 追加 18）单流多臂**：跨 run 比较已被证明不可靠（上轮 A/B/C 的比值与设置不自洽，
    #  成因未定）⇒ 改为**同一条流内**依次设增益采样，照度 / 取景 / 3A 收敛态全程相同；
    #  每臂先丢弃 `--warmup` 帧（避开起流瞬态）再采集。
    arms = [int(x) for x in a.arms.split(",")] if a.arms else [0]
    for gain in arms:
        prefix = a.prefix if gain == 0 else "%s_g%d" % (a.prefix, gain)
        if gain:
            import subprocess
            import time
            subprocess.run(["v4l2-ctl", "-d", subdev, "--set-ctrl=analogue_gain=%d" % gain], check=False)
            time.sleep(1.0)
            _r = subprocess.run(["v4l2-ctl", "-d", subdev, "--get-ctrl=exposure,analogue_gain,vertical_blanking"],
                                capture_output=True, text=True).stdout.strip().replace("\n", " ")
            print("[arm gain=%d] %s" % (gain, _r))
            sys.stdout.flush()
        for _ in range(a.warmup):                      # 丢弃起流瞬态帧
            _f = cap.acquire(1, 500)
            if _f is not None:
                cap.release(_f["lease_id"])
        curve = None
        n_est = 0
        ys = []
        prev = None
        diffs = []
        for i in range(a.frames):
            f = cap.acquire(1, 500)
            if f is None:
                continue
            seq = f.get("seq")
            _n.rga.blit_nv12_fd(int(f["dmabuf_fd"]), 1)          # WIN = 原图窗
            if i % a.every == 0:
                rc = est.estimate(f, alpha=est.alpha)
                if rc == 0:
                    curve = est.publish()
                    n_est += 1
            cap.release(f["lease_id"])
            if curve is not None:
                ra.apply_window(win, curve.field, enh, 512, 288)
            orig = win.to_bytes()
            ys.append(round(sum(orig[0::3]) / (len(orig) / 3), 2))
            # **（v3.86 追加 23）时域噪声代理**：同窗口相邻帧的 |Δ| 均值（R 通道按 stride 48 = 每 16 像素抽样）
            if prev is not None:
                _a = orig[0::48]
                _b = prev[0::48]
                diffs.append(sum(abs(_x - _y) for _x, _y in zip(_a, _b)) / max(1, len(_a)))
            prev = orig
            last_seq = seq

        orig = win.to_bytes()
        e = enh.to_bytes()

        # **（v3.86 追加 26）空间噪声口径**：单帧内「块均值最小 25%」的 16×16 块的 σ 中位数（R 通道）
        # —— 与 O86/O87 既有"暗部块 σ"口径同族；**判数字增益就该用空间口径**（§12.23 的方法学结论）。
        def _dark_block_sigma(buf, w, h, bs=16, frac=0.25):
            ch = buf[0::3]
            bw, bh = w // bs, h // bs
            means, stds = [], []
            for by in range(bh):
                for bx in range(bw):
                    vals = []
                    for y in range(by * bs, (by + 1) * bs):
                        row = y * w + bx * bs
                        vals.extend(ch[row:row + bs])
                    n = len(vals)
                    m = sum(vals) / n
                    means.append(m)
                    stds.append((sum((v - m) ** 2 for v in vals) / n) ** 0.5)
            order = sorted(range(len(means)), key=lambda i: means[i])
            k = max(1, int(len(means) * frac))
            sel = sorted(stds[i] for i in order[:k])
            return round(sel[len(sel) // 2], 3), round(sum(means[i] for i in order[:k]) / k, 2)

        _sigma, _dkmean = _dark_block_sigma(orig, 512, 288)
        n1 = write_png("%s_orig.png" % prefix, orig, 512, 288)
        n2 = write_png("%s_enh.png" % prefix, e, 512, 288)
        rep = {
            "arm_gain": gain, "frames": a.frames, "warmup": a.warmup, "estimates": n_est, "win": a.window,
            "last_seq": last_seq,
            "mean_orig_rgb": mean_rgb(orig), "mean_enh_rgb": mean_rgb(e),
            "orig_window_mean_red_channel_per_frame": ys[-5:],
            "win_temporal_absdiff_mean": (round(sum(diffs) / len(diffs), 3) if diffs else None),
            "win_temporal_absdiff_p90": (round(sorted(diffs)[int(0.9 * (len(diffs) - 1))], 3) if diffs else None),
            "dark_block_sigma_median": _sigma,
            "dark_block_mean": _dkmean,
            "curve_precision": est.precision(), "f_est_hz": round(est.f_est, 3), "alpha": est.alpha,
            "last_estimate_ns": _n.rknn.last_estimate_ns(), "slice_mask_ret": [_n.rknn.slice_mask_ret(k) for k in range(_n.rknn.slice_count())],
            "png_bytes": {"orig": n1, "enh": n2},
            "files": ["%s_orig.png" % prefix, "%s_enh.png" % prefix],
        }
        print(json.dumps(rep, ensure_ascii=False))
        sys.stdout.flush()

    est.shutdown()
    cap.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())