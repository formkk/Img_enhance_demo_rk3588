#!/usr/bin/env python3
"""B5：把演示域素材汇总成一份可送 DCM 链的图集（演示帧自蒸馏的输入）。

两类混用（用户 2026-10-11 裁定「两者混用」）：
  · RAW：`hq_optimize/captures/seq*/*.nv12`（增强前，板端采集）→ 按项目权威口径转 JPEG
  · ENH：`*_E_/*_S2_/*_F1080_/orig_/*_A60_*.png` + `noise_diag/hq_final_1.png`（增强输出侧）

NV12→RGB 复用 `tools/healthcheck_capture.py` 的函数（BT.601 / full range，**不做任何非线性后处理**）。
排除 `cmp_*/montage_*/canvas_*/c_*`（对比图、拼图、截图、裁剪）。

产物：DTM/assets/indomain/*.jpg（本地暂存，随后 tar 送 147）
用法：~/anaconda3/envs/dtm/bin/python DTM/code/b5_make_indomain.py
"""

import glob
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, PROJ)
from tools.healthcheck_capture import load_nv12, nv12_to_rgb      # noqa: E402

CAP = os.path.join(PROJ, "hq_optimize", "captures")
OUT = os.path.join(PROJ, "DTM", "assets", "indomain")

W, H, STRIDE, UV_OFF, FB = 1920, 1080, 1920, 2073600, 3110400
# 同一批 seq* 是 1920x1088（高度按 16 对齐）⇒ 按字节数自适应布局，再裁到 1080 行
LAYOUTS = [(1088, 1920, 1920 * 1088, 3133440, 1080),
           (1080, 1920, 1920 * 1080, 3110400, 1080)]
EXCL = ("cmp_", "montage", "canvas_", "c_")
ENH_PAT = ("*_E_*.png", "*_S2_*.png", "*_F1080_*.png", "orig_*.png", "*_A60_*.png")


def load_any_nv12(path):
    n = os.path.getsize(path)
    for h, stride, uv_off, fb, crop_h in LAYOUTS:
        if n == fb:
            y, cb, cr, err = load_nv12(path, W, h, stride, uv_off, fb)
            if err:
                return None, err
            return nv12_to_rgb(y, cb, cr)[:crop_h], None
    return None, f"未知帧字节数 {n}"


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    n_raw = n_enh = n_skip = 0

    for p in sorted(glob.glob(os.path.join(CAP, "**", "*.nv12"), recursive=True)):
        rgb, err = load_any_nv12(p)
        if err:
            print("[skip nv12]", os.path.basename(p), err, flush=True)
            n_skip += 1
            continue
        rel = os.path.relpath(p, CAP).replace(os.sep, "_")[:-len(".nv12")]
        Image.fromarray(rgb).save(os.path.join(OUT, f"raw_{rel}.jpg"), quality=95)
        n_raw += 1

    enh = []
    for pat in ENH_PAT:
        enh += glob.glob(os.path.join(CAP, "**", pat), recursive=True)
    enh.append(os.path.join(CAP, "noise_diag", "hq_final_1.png"))
    for p in sorted(set(enh)):
        b = os.path.basename(p)
        if any(b.startswith(x) for x in EXCL):
            n_skip += 1
            continue
        rel = os.path.relpath(p, CAP).replace(os.sep, "_")
        Image.open(p).convert("RGB").save(os.path.join(OUT, f"enh_{rel}"), quality=95)
        n_enh += 1

    print(f"RAW→jpg = {n_raw} | ENH = {n_enh} | 跳过 = {n_skip} | 合计 = {n_raw + n_enh}",
          flush=True)
    print("B5_DONE", OUT, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
