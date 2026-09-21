#!/usr/bin/env python3
"""realtime_frame_review.py —— M1#5 审查工具（**实时线**）：采一段帧 → 走交付链路推理 → 出两图 + 量化随行

地位：交付工具（§10.3 登记）。效力：**审查素材与量化随行，非验收依据**（规则 `00-ssot.md` §3.5）。
口径出处：主基准 §9.5 的 **O94 块** (a) 实时线审查手段 / (c) 量化随行；SNR 口径 = §13
「采集信噪比（推理域）」行；**不得做非线性后处理** = 规则 `.trae/rules/10-evidence.md` §2。
本文件由 `docs/evidence/reports/O87/zde_single_frame_review.py` **转正**而来（O94 (a) 待办），
原有参数（`--arms` / `--warmup` / `--vblank` / `--exposure` / `--late-exp` / `--late-gain`）**一律保留**。

用途：在指定照度下采一段帧，经**与交付链路完全相同的模块**（TH1 `Capture` + TH2a `CurveEstimator`
（RGA + ZDE++ 2 条带 + EMA）+ TH2b `realtime_apply.apply_window` 8-iter 单遍融合）产出两张图：
  · `<prefix>_orig.png` = **原图窗**（RGA NV12 → 窗尺寸 RGB888，链路原样）
  · `<prefix>_enh.png`  = **增强窗**（8-iter 应用结果，链路原样）
PNG 为**直出编码**（RGB888 → PNG，零滤镜、零后处理）。

**为什么不停桌面**：本工具**不初始化显示模块**（不做 DRM/pageflip），只取流 + 推理 + 落盘 ⇒ 无 master 需求。

用法（板上）：
  cd /home/cat/starlight && PYTHONPATH=/home/cat/starlight \
    python3 tools/realtime_frame_review.py --frames 300 --snr-frames 100 --every 6 \
            --prefix /home/cat/m1_review
  快速目视（不满足 §13 的 ≥100 帧样本）可 `--frames 30`，此时 `inference_snr.insufficient_sample = true`。

报告：`<prefix>_report.json`（**随行落盘**，与 PNG 同主干名）+ stdout 同内容 JSON。

量化随行（O94 (c)）：
  · **推理域 SNR** = `inference_snr`（§13 口径：推理域块均值最小 25% 的中位数 ÷ 相邻帧 |Δ| 的 RMS/√2）
  · **暗部块 σ** = `dark_block_sigma_median`（空间口径；§12.23 方法学结论）
  · **通道平衡 G/R** = `gr_balance`（全窗 + 最暗 25% 块）
  · **原图窗 / 增强窗 mean·max** = `mean_*_rgb` / `max_*_rgb`
  · **测量条件随行**（§6.5）= `conditions`：IQ 档位 + sha256 / 曝光·增益·vblank 回读 / config 快照哈希

链路调用序照 `docs/evidence/M1-collect-tool-board.md` §14.3 与 `bisect.py`（板上实测可跑）。
**⚠️ 板上坑**：`/home/cat/starlight/bisect.py` 遮蔽标准库 `bisect` ⇒ 本文件**禁止** `import statistics`
/ `random`（会 `ImportError`），一切统计**自算**（见 `_median`）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 交付链路模块（**板上**才有 aarch64 扩展）。开发机上无扩展 ⇒ 记为导入失败，
# 仅供 `import` 本模块做**纯函数口径自检**（`main()` 会因此拒绝运行）。
try:
    import _starlight_native as _n
    import capture as capm
    import config as C
    import curve_estim as ce
    import log as L
    import realtime_apply as ra
    _IMPORT_ERR = ""
except ImportError as _e:                                   # 开发机（x86，无交付扩展）
    _n = capm = C = ce = L = ra = None
    _IMPORT_ERR = str(_e)

# IQ 档位（部署约定；与 `tools/collect/iq_sets.py` 一致）
IQ_LOAD_DIR = "/etc/iqfiles"
IQ_MANIFEST = "/etc/iqfiles_sets/manifest.json"
IQ_MANIFEST_SCHEMA = "iqsets/v1"

# ── 纯函数：落盘 / 统计（`hq_frame_review.py` 亦 import 本模块取用）───────────
# SNR 口径常量（§13「采集信噪比（推理域）」行；与 `reports/O87/inference_domain_snr.py` 一致）：
# 推理域 480×270 上 **16×16 块**，**只用前 256 行**（与离线脚本的 `ds[:256, :480]` 同口径）。
SNR_BS = 16
SNR_ROWS = 256


def write_png(path, rgb, w, h):
    """RGB888 → PNG，**无任何后处理**（filter=None，直出编码）。"""
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


def _median(xs):
    """中位数（**自算**，不用 `statistics`——板上被 `bisect.py` 遮蔽链带崩）。"""
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _percentile(xs, q):
    """线性插值分位数（与 `np.percentile` 同法）。"""
    s = sorted(xs)
    if not s:
        return None
    k = q * (len(s) - 1)
    i0 = int(k)
    i1 = min(i0 + 1, len(s) - 1)
    return s[i0] + (s[i1] - s[i0]) * (k - i0)


def mean_rgb(b):
    n = len(b)
    return [round(sum(b[i::3]) / (n / 3), 2) for i in range(3)]


def max_rgb(b):
    return [max(b[i::3]) for i in range(3)]


def dark_block_stats(buf, w, h, bs=16, frac=0.25):
    """单帧「**块均值最小的 `frac` 比例**」的 bs×bs 块统计（块均值 = **R 通道**均值）。

    返回 `sigma_median` = R 通道块 σ 的中位数（**空间**口径，与 O86/O87 的「暗部块 σ」同族；
    §12.23 方法学结论：判数字增益就该用空间口径）、`r_mean` / `g_mean` = 暗块 R/G 均值。
    """
    ch = buf[0::3]
    cg = buf[1::3]
    bw, bh = w // bs, h // bs
    means, stds, gmeans = [], [], []
    for by in range(bh):
        for bx in range(bw):
            rv, gv = [], []
            for y in range(by * bs, (by + 1) * bs):
                row = y * w + bx * bs
                rv.extend(ch[row:row + bs])
                gv.extend(cg[row:row + bs])
            n = len(rv)
            m = sum(rv) / n
            means.append(m)
            gmeans.append(sum(gv) / n)
            stds.append((sum((v - m) ** 2 for v in rv) / n) ** 0.5)
    order = sorted(range(len(means)), key=lambda i: means[i])
    k = max(1, int(len(means) * frac))
    sel = order[:k]
    return {
        "sigma_median": round(_median([stds[i] for i in sel]), 3),
        "r_mean": round(sum(means[i] for i in sel) / k, 2),
        "g_mean": round(sum(gmeans[i] for i in sel) / k, 2),
        "blocks": len(means), "blocks_used": k, "block_px": bs,
    }


def gr_balance(buf, w, h, dk=None):
    """**通道平衡 G/R**（O94 (c)）：全窗 + 最暗 25% 块（选择规则见 `dark_block_stats`）。"""
    n = w * h
    r = sum(buf[0::3]) / n
    g = sum(buf[1::3]) / n
    dk = dk or dark_block_stats(buf, w, h)
    return {
        "whole_g_over_r": round(g / r, 4) if r else None,
        "dark_g_over_r": round(dk["g_mean"] / dk["r_mean"], 4) if dk["r_mean"] else None,
    }


def luma_from_rgb888(buf, w, h):
    """RGB888 → 亮度（**BT.601 整数近似** `(77R+150G+29B)>>8`，与 §13 登记的离线脚本
    只在"域"上不同：离线脚本在 NV12 的 **Y 平面**上算，本工具手上只有 RGA 输出的 RGB888）。"""
    r = buf[0::3]
    g = buf[1::3]
    b = buf[2::3]
    return bytes((77 * rr + 150 * gg + 29 * bb) >> 8 for rr, gg, bb in zip(r, g, b))


def inference_domain_snr(frames_rgb, w, h, ts_ns=None):
    """**推理域 SNR**（§13「采集信噪比（推理域）」行的**代理口径**，非 ISO/CPI）。

      · 信号 = 推理域上 16×16 块中「块均值最小 25%」的**块均值中位数**
      · 噪声 = **相邻帧**同像素 |Δ| 的 **RMS/√2**（时域口径；3DNR 开启时中位数为 0 故不用中位数）
      · SNR = 信号 / 噪声           （各量均先在帧维取中位数，再相除——与离线脚本一致）

    **与 §13 登记脚本的两处口径差异（如实随行，不得掩盖）**：① 本工具只有 RGA 输出的 RGB888
    ⇒ 亮度用 **BT.601 整数近似**（离线脚本用真 Y 平面）；② 下采样由 **RGA 双线性**完成
    （离线脚本用 **4×4 box**；离线脚本自带"双线性 ⇒ 略偏乐观"同款声明）。
    """
    frames_rgb = list(frames_rgb)
    if len(frames_rgb) < 2:
        return {"error": "帧数不足（需 ≥2 帧才有时域项）", "frames_used": len(frames_rgb)}
    bs = SNR_BS
    bw, bh = w // bs, min(h, SNR_ROWS) // bs
    sig, sqs, p90s, prev = [], [], [], None
    for buf in frames_rgb:
        luma = luma_from_rgb888(buf, w, h)
        rows = [luma[y * w:(y + 1) * w] for y in range(bh * bs)]
        means = []
        for by in range(bh):
            for bx in range(bw):
                tot = 0
                for y in range(by * bs, (by + 1) * bs):
                    row = rows[y]
                    tot += sum(row[bx * bs:bx * bs + bs])
                means.append(tot / float(bs * bs))
        order = sorted(range(len(means)), key=lambda i: means[i])
        k = max(1, int(len(means) * 0.25))
        sig.append(_median([means[i] for i in order[:k]]))
        if prev is not None:
            d = [abs(x - y) for x, y in zip(luma, prev)]
            sqs.append(sum(v * v for v in d) / len(d))
            p90s.append(_percentile(d, 0.9))
        prev = luma
    s = _median(sig)
    noise = (float(_median(sqs)) / 2.0) ** 0.5
    out = {
        "signal_dark_block_mean_median": round(s, 3),
        "noise_temporal_sigma_rms": round(noise, 4),
        "absdiff_p90_median": round(_median(p90s), 2),
        "snr": round(s / noise, 3) if noise else None,
        "frames_used": len(frames_rgb),
        "insufficient_sample": len(frames_rgb) < 100,      # §13：统计用 ≥100 帧
        "domain": "%dx%d（推理域）" % (w, h),
        "blocks": "%dx%d×%d" % (bw, bh, bs),
        "metric": "代理口径（非 ISO/CPI）；亮度 = BT.601 整数近似 + RGA 双线性下采样"
                  "（离线登记脚本用 Y 平面 + 4×4 box ⇒ 本口径略偏乐观）",
        "threshold": "§13 判据：1 Lux 下 SNR ≥ 4",
    }
    if ts_ns and len(ts_ns) > 1:
        gaps = [(ts_ns[i + 1] - ts_ns[i]) / 1e6 for i in range(len(ts_ns) - 1)]
        out["frame_gap_ms_p50"] = round(_median(gaps), 2)
        out["frame_gap_ms_max"] = round(max(gaps), 2)
        out["adjacency_note"] = "噪声项按**采集到的相邻帧**计算；gap ≈ 帧周期 ⇒ 即真「相邻帧」"
    return out


# ── 纯函数：测量条件随行（§6.5）────────────────────────────────────────────
def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for blk in iter(lambda: fp.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def iq_conditions(iq_dir=IQ_LOAD_DIR, manifest_path=IQ_MANIFEST):
    """IQ **档位 + sha256**（比对 `tools/collect/iq_sets.py` 的 manifest；§9.5 指纹）。

    **只读、不切档、不做防呆**——本工具是审查工具，允许在任意档下出图，但**档位必须随行**。
    档位不可判定时如实记 `unknown`（带 `why`）。
    """
    d = {"iq_dir": iq_dir, "manifest": manifest_path, "set": "unknown",
         "sha256": None, "file": None}
    try:
        with open(manifest_path) as fh:
            m = json.load(fh)
    except Exception as exc:                                  # 只读探测失败 ⇒ 如实记录
        d["why"] = "无法读取 manifest：%s" % exc
        return d
    if m.get("schema") != IQ_MANIFEST_SCHEMA:
        d["why"] = "manifest schema 不符（期望 %r，实得 %r）" % (IQ_MANIFEST_SCHEMA, m.get("schema"))
        return d
    name = m.get("sensor_file")
    cur = os.path.join(iq_dir, name or "")
    if not name or not os.path.exists(cur):
        d["file"] = name
        d["why"] = "当前 IQ 文件不存在：%s" % cur
        return d
    h = _sha256_file(cur)
    d.update(file=name, sha256=h)
    if h == (m.get("stock") or {}).get("sha256"):
        d["set"] = "stock"
    for s, e in (m.get("sets") or {}).items():
        if h == (e or {}).get("sha256"):
            d["set"] = s
            d["nr3d_enable"] = (e or {}).get("nr3d_enable")
            d["exp_us_iq"] = (e or {}).get("exp_us")
            d["gain_iq"] = (e or {}).get("gain")
            d["fps_iq"] = (e or {}).get("fps")
    return d


def config_snapshot():
    """config 快照哈希（§6.5 条件随行；路径取交付 `config.py` 的 `DEFAULT_PATH`）。"""
    path = getattr(C, "DEFAULT_PATH", None) or os.path.join(ROOT, "config.yaml")
    try:
        return {"config_yaml": path, "sha256": _sha256_file(path)}
    except OSError as exc:
        return {"config_yaml": path, "sha256": None, "why": str(exc)}


def find_sensor_subdev():
    """解析 sensor subdev：判据 = **唯一暴露 `vertical_blanking` 的 v4l-subdev**（§9.5 ③ 同族）。"""
    for d in sorted("/dev/" + n for n in os.listdir("/dev") if n.startswith("v4l-subdev")):
        r = subprocess.run(["v4l2-ctl", "-d", d, "--list-ctrls"], capture_output=True, text=True)
        if "vertical_blanking" in r.stdout:
            return d
    return None


def read_sensor_ctrls(subdev):
    """曝光 / 增益 / vblank 的 **subdev 回读**（§9.5 ⑤：`getExp*` 非设定值，须用 subdev 回读）。"""
    if not subdev:
        return {}
    out = subprocess.run(
        ["v4l2-ctl", "-d", subdev, "--get-ctrl=exposure,analogue_gain,vertical_blanking"],
        capture_output=True, text=True).stdout.strip()
    d = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            try:
                d[k.strip()] = int(v.strip())
            except ValueError:
                d[k.strip()] = v.strip()
    return d


def conditions_snapshot(subdev):
    """一条完整的测量条件随行（IQ 档位 + sha256 + subdev 回读 + config 快照哈希）。"""
    return {
        "iq": iq_conditions(),
        "sensor_subdev": subdev,
        "sensor_readback": read_sensor_ctrls(subdev),
        "config": config_snapshot(),
    }


def labels_block(lux=0.0, scene=""):
    """**人工标签随行**（§13「采集信噪比（推理域）」行的记录项 = **照度标签**）。

    照度由**人工测得**（照度计 / 手机 app，**每批次一次**，见 §15 O45 ④）⇒ 本工具只**如实记录**，
    **不测量、不推断、不据它自动判定达标**；未给则记 `null`（**不得以默认值顶替**）。
    """
    return {
        "lux": (round(float(lux), 3) if lux else None),
        "scene": (scene or None),
        "source": "人工录入（照度计 / 手机 app，每批次一次；§15 O45 ④）",
    }


def apply_pre_overrides(subdev, vblank=0, exposure=0):
    """**起流前**覆盖 vblank / 曝光（先 vblank 抬高 VMAX，才允许更长曝光）+ 回读校验。"""
    if vblank:
        subprocess.run(["v4l2-ctl", "-d", subdev, "--set-ctrl=vertical_blanking=%d" % vblank],
                       check=False)
    if exposure:
        subprocess.run(["v4l2-ctl", "-d", subdev, "--set-ctrl=exposure=%d" % exposure],
                       check=False)
    rb = subprocess.run(["v4l2-ctl", "-d", subdev, "--get-ctrl=vertical_blanking,exposure"],
                        capture_output=True, text=True).stdout.strip().replace("\n", " ")
    print("[override] subdev=%s  回读=%s" % (subdev, rb))
    sys.stdout.flush()


def apply_late_overrides(subdev, late_exp=0, late_gain=0):
    """**起流后**（AE 已收敛）覆盖曝光 / 增益——起流前写会被 AE 按 IQ 的 `FpsValue` 钳回。"""
    time.sleep(2.0)
    if late_exp:
        subprocess.run(["v4l2-ctl", "-d", subdev, "--set-ctrl=exposure=%d" % late_exp], check=False)
    if late_gain:
        subprocess.run(["v4l2-ctl", "-d", subdev, "--set-ctrl=analogue_gain=%d" % late_gain],
                       check=False)
    time.sleep(1.5)
    rb = subprocess.run(["v4l2-ctl", "-d", subdev, "--get-ctrl=exposure,analogue_gain,vertical_blanking"],
                        capture_output=True, text=True).stdout.strip().replace("\n", " ")
    print("[late-override] %s" % rb)
    sys.stdout.flush()


def set_analogue_gain(subdev, gain):
    """单流多臂用：流内设 `analogue_gain` + 回读。"""
    subprocess.run(["v4l2-ctl", "-d", subdev, "--set-ctrl=analogue_gain=%d" % gain], check=False)
    time.sleep(1.0)
    rb = subprocess.run(["v4l2-ctl", "-d", subdev, "--get-ctrl=exposure,analogue_gain,vertical_blanking"],
                        capture_output=True, text=True).stdout.strip().replace("\n", " ")
    print("[arm gain=%d] %s" % (gain, rb))
    sys.stdout.flush()


# ── 主流程 ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", type=int, default=300,
                    help="每臂采帧数（§13 的复现口径 = 300 帧连续取流；快速目视可给 30）")
    ap.add_argument("--snr-frames", type=int, default=100,
                    help="推理域 SNR 统计用帧数（§13：统计用 ≥100 帧）")
    ap.add_argument("--every", type=int, default=6, help="每 N 帧做一次估计（与 main.py est_every 同义）")
    ap.add_argument("--prefix", default="/home/cat/m1_review")
    ap.add_argument("--window", default="", help="仅供记录（留空则记 geom 的实际窗尺寸）")
    # **（v3.86 追加 9）降帧率换曝光**：在 `cap.init()`（含 sensor_lock 先施 vblank）**之后**、
    # `stream_on` **之前**覆盖 vblank / 曝光并**回读校验**。不改 IQ、不改 DT（规则 §4 明许运行期微调）。
    ap.add_argument("--vblank", type=int, default=0)
    ap.add_argument("--exposure", type=int, default=0)
    # **（v3.86 追加 17）组合档通道**：起流前写曝光会被 AE 按 IQ 的 `FpsValue` **钳回**；
    # **起流后**（AE 已收敛）再写则**站住**（§12.18 后补充实测）⇒ 组合档走 `--late-exp` / `--late-gain`。
    ap.add_argument("--late-exp", type=int, default=0)
    ap.add_argument("--late-gain", type=int, default=0)
    # **（v3.86 追加 18）单流多臂**：`--arms 90,120` = 同一条流内依次把 `analogue_gain` 设为 90 / 120
    # 各采一组；`--warmup` = 每臂先丢弃的帧数（避开起流瞬态）。**跨 run 比较不可靠，以本模式为准**。
    ap.add_argument("--arms", default="")
    ap.add_argument("--warmup", type=int, default=20)
    # **人工标签随行**（§13「采集信噪比（推理域）」行要求记录**照度标签**；O94 (c) 的条件随行）
    ap.add_argument("--lux", type=float, default=0.0, help="照度标签（Lux，人工测得；0 = 未记录 ⇒ 报告记 null）")
    ap.add_argument("--scene", default="", help="场景 / 预设标签（如 darkroom / night / face；留空 = 未记录）")
    # **O54 对照（不走 RGA）**：`--dump-nv12` 存**该帧原始 NV12**（离线复算用）；
    # `--in-est/--in-win` 把**离线准备**的 RGB888（显式 CSC，不经 RGA）**注入**既有缓冲 ⇒
    # 同一 native 内核、只换输入路径（唯一变量 = NV12→RGB 这一步）。
    ap.add_argument("--dump-nv12", default="", help="另存同一帧的原始 NV12（留空 = 不存）")
    ap.add_argument("--in-est", default="", help="注入的估计输入（pad_w×pad_h×3 RGB888；须与 --in-win 同给）")
    ap.add_argument("--in-win", default="", help="注入的窗输入（win_w×win_h×3 RGB888；须与 --in-est 同给）")
    a = ap.parse_args()
    if bool(a.in_est) != bool(a.in_win):
        print("ERR --in-est 与 --in-win 必须同时给")
        return 1

    if _IMPORT_ERR:
        print("ERR 交付链路模块导入失败（本工具须在板上运行）：%s" % _IMPORT_ERR)
        return 1

    cfg = C.load()
    logger = L.from_config(cfg)
    cap = capm.Capture(cfg, logger)
    est = ce.CurveEstimator(cfg, logger)

    if cap.init() != 0:
        print("ERR cap.init")
        return 1

    # 测量条件随行（§6.5）：**无条件**解析 sensor subdev（只读探测）⇒ 收尾时才能给出
    # 曝光/增益/vblank 回读（§9.5 ⑤：subdev 回读是唯一效度检查，`getExp*` 不回读设定值）。
    subdev = find_sensor_subdev()
    if (a.vblank or a.exposure or a.late_exp or a.late_gain) and subdev is None:
        print("ERR sensor subdev 未找到")
        return 1
    if subdev is not None and (a.vblank or a.exposure):
        apply_pre_overrides(subdev, a.vblank, a.exposure)

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

    if a.late_exp or a.late_gain:
        if subdev is None:
            subdev = find_sensor_subdev()
        apply_late_overrides(subdev, a.late_exp, a.late_gain)

    g = est.geom
    win_w, win_h = int(g["win_w"]), int(g["win_h"])
    est_w, est_h = int(g["eff_w"]), int(g["eff_h"])
    win = _n.rga.win(g)
    enh = _n.rga.enh(g)
    last_seq = -1

    # **O54 对照（不走 RGA）**：把离线准备的 RGB888 **一次性写入既有缓冲** ⇒
    # 交付模块（curve_estim / realtime_apply）与其内核**一字未改**，唯一变量 = NV12→RGB 这一步。
    inj = bool(a.in_est)
    inj_meta = None
    if inj:
        est_pad_w, est_pad_h = int(g["pad_w"]), int(g["pad_h"])
        ebytes = open(a.in_est, "rb").read()
        wbytes = open(a.in_win, "rb").read()
        if len(ebytes) != est_pad_w * est_pad_h * 3:
            print("ERR --in-est 尺寸 %d ≠ %d" % (len(ebytes), est_pad_w * est_pad_h * 3))
            return 1
        if len(wbytes) != win_w * win_h * 3:
            print("ERR --in-win 尺寸 %d ≠ %d" % (len(wbytes), win_w * win_h * 3))
            return 1
        _n.rga.est(g).write(ebytes)
        win.write(wbytes)
        inj_meta = {"mode": "no-rga",
                    "in_est": {"path": a.in_est, "sha256": hashlib.sha256(ebytes).hexdigest()},
                    "in_win": {"path": a.in_win, "sha256": hashlib.sha256(wbytes).hexdigest()},
                    "note": "离线显式 CSC（BT.601 limited，O54 实测值）准备，**不经 RGA**"}
    dump_meta = None

    arms = [int(x) for x in a.arms.split(",")] if a.arms else [0]
    for gain in arms:
        prefix = a.prefix if gain == 0 else "%s_g%d" % (a.prefix, gain)
        if gain:
            set_analogue_gain(subdev, gain)
        for _ in range(a.warmup):                      # 丢弃起流瞬态帧
            _f = cap.acquire(1, 500)
            if _f is not None:
                cap.release(_f["lease_id"])
        curve = None
        n_est = 0
        ys = []
        prev = None
        diffs = []
        snr_frames = []                                # 推理域（480×270）原样副本，供 SNR 随行
        snr_ts = []
        for i in range(a.frames):
            f = cap.acquire(1, 500)
            if f is None:
                continue
            seq = f.get("seq")
            if inj:
                # 不走 RGA：不 blit，直接读注入缓冲（每轮估计即用注入内容）
                if i % a.every == 0 and est.estimate(None, alpha=est.alpha) == 0:
                    curve = est.publish()
                    n_est += 1
            else:
                _n.rga.blit_nv12_fd(int(f["dmabuf_fd"]), 1)          # 1 = WIN = 原图窗
                if i % a.every == 0:
                    if est.estimate(f, alpha=est.alpha) == 0:        # estimate 内部会 blit EST
                        curve = est.publish()
                        n_est += 1
                else:
                    _n.rga.blit_nv12_fd(int(f["dmabuf_fd"]), 0)      # 0 = EST = 推理域（供 SNR 随行）
            # **量化随行**：先复制推理域（RGA 自有缓冲，非 V4L2 lease）再放 lease；
            # 复制是 C 级 memcpy（<1 ms）⇒ **不拖慢取流**，帧间隔保持 ≈ 帧周期（真"相邻帧"）。
            if (not inj) and len(snr_frames) < a.snr_frames:
                snr_frames.append(_n.rga.est_eff(g).to_bytes())
                snr_ts.append(int(f["t_capture_ns"]))
            # 末帧另存**原始 NV12**（离线复算/O54 对照用）
            if a.dump_nv12 and i == a.frames - 1:
                nb = _n.capture.buffer(int(f["buffer_index"])).to_bytes()
                with open(a.dump_nv12, "wb") as fh:
                    fh.write(nb)
                dump_meta = {"path": a.dump_nv12, "bytes": len(nb),
                             "sha256": hashlib.sha256(nb).hexdigest(), "seq": seq,
                             "w": int(_n.capture.width()), "h": int(_n.capture.height()),
                             "stride": int(_n.capture.stride()),
                             "sizeimage": int(_n.capture.sizeimage())}
            cap.release(f["lease_id"])
            if curve is not None:
                ra.apply_window(win, curve.field, enh, win_w, win_h)
            orig = win.to_bytes()
            ys.append(round(sum(orig[0::3]) / (len(orig) / 3), 2))
            # **时域噪声代理**：同窗口相邻帧的 |Δ| 均值（R 通道按 stride 48 = 每 16 像素抽样）
            if prev is not None:
                _a = orig[0::48]
                _b = prev[0::48]
                diffs.append(sum(abs(_x - _y) for _x, _y in zip(_a, _b)) / max(1, len(_a)))
            prev = orig
            last_seq = seq

        orig = win.to_bytes()
        e = enh.to_bytes()
        dk = dark_block_stats(orig, win_w, win_h)
        rep = {
            "tool": "tools/realtime_frame_review.py",
            "scope": "M1#5 审查（实时线；口径 = §9.5 的 O94 块 (a)(c)）",
            "arm_gain": gain, "frames": a.frames, "warmup": a.warmup, "estimates": n_est,
            "win": "%dx%d" % (win_w, win_h), "est": "%dx%d" % (est_w, est_h),
            "win_arg": a.window, "last_seq": last_seq,
            "mean_orig_rgb": mean_rgb(orig), "mean_enh_rgb": mean_rgb(e),
            "max_orig_rgb": max_rgb(orig), "max_enh_rgb": max_rgb(e),
            "orig_window_mean_red_channel_per_frame": ys[-5:],
            "win_temporal_absdiff_mean": (round(sum(diffs) / len(diffs), 3) if diffs else None),
            "win_temporal_absdiff_p90": (round(sorted(diffs)[int(0.9 * (len(diffs) - 1))], 3)
                                         if diffs else None),
            "dark_block_sigma_median": dk["sigma_median"],
            "dark_block_mean": dk["r_mean"],
            "gr_balance": {
                "orig_win": gr_balance(orig, win_w, win_h, dk),
                "enh_win": gr_balance(e, win_w, win_h),
            },
            "inference_snr": (inference_domain_snr(snr_frames, est_w, est_h, snr_ts) if not inj else
                             {"skipped": "no-RGA 注入模式：输入为单帧离线准备，无相邻帧对 ⇒ 时域项不可算"}),
            "input_path": ("no-rga（离线显式 CSC 注入）" if inj else "rga"),
            "injected": inj_meta,
            "nv12_dump": dump_meta,
            "curve_precision": est.precision(), "f_est_hz": round(est.f_est, 3), "alpha": est.alpha,
            "last_estimate_ns": _n.rknn.last_estimate_ns(),
            "slice_mask_ret": [_n.rknn.slice_mask_ret(k) for k in range(_n.rknn.slice_count())],
            "conditions": conditions_snapshot(subdev),
            "labels": labels_block(a.lux, a.scene),
            "png_bytes": {"orig": write_png("%s_orig.png" % prefix, orig, win_w, win_h),
                          "enh": write_png("%s_enh.png" % prefix, e, win_w, win_h)},
            "files": ["%s_orig.png" % prefix, "%s_enh.png" % prefix,
                      "%s_report.json" % prefix],
        }
        with open("%s_report.json" % prefix, "w") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)
        print(json.dumps(rep, ensure_ascii=False))
        sys.stdout.flush()

    est.shutdown()
    cap.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())