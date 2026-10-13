#!/usr/bin/env python3
"""S10: 把 S0 重导到 768x1344 的 DCM 子网 ONNX 逐个转 RKNN.

口径与 s1_convert_rknn.py 完全一致:
  rknn.config(target_platform="rk3588") + rknn.build(do_quantization=False)  # FP16
  (FP16 不涉及 mean/std, 故图像域(01/10b)与特征域(其余)无差别处理;
   d6_convert_rpn_head.py 的 mean/std 是检测头专用, DCM 不采用)

07d/07e 已由 s9b/s9c 在 out_s0_1344/ 内置主机侧转出 rknn, 本脚本不重复转换.

产物: DTM/assets/dcm1344/*.rknn + s10_report.json
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

from rknn.api import RKNN

HERE = os.path.dirname(os.path.abspath(__file__))
S0 = os.path.abspath(os.path.join(HERE, "..", "out_s0_1344"))
OUT = os.path.abspath(os.path.join(HERE, "..", "assets", "dcm1344"))

CASES = [
    "01_feature_extractor",
    "02_g_a",
    "03_g_a_sft",
    "04_hyper_encoder",
    "05_hyper_decoder",
    "06b_hyper_scale_decoder_float",
    "08_g_s_sft",
    "09_g_s",
    "10b_rec_clamp",
]
TARGET = "rk3588"


def _capture(fn):
    """fd 级捕获 stdout: rknn C 层日志走 printf, python 层 redirect 抓不到."""
    sys.stdout.flush()
    saved = os.dup(1)
    tmp = tempfile.TemporaryFile(mode="w+")
    os.dup2(tmp.fileno(), 1)
    try:
        ret = fn()
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
        tmp.seek(0)
        log = tmp.read()
        tmp.close()
    return ret, log


def main():
    os.makedirs(OUT, exist_ok=True)
    rep = {}
    for name in CASES:
        src = os.path.join(S0, name + ".onnx")
        dst = os.path.join(OUT, name + ".rknn")
        if not os.path.exists(src):
            rep[name] = {"status": "NO_ONNX"}
            print(f"{name:38s} NO_ONNX")
            continue
        rknn = RKNN(verbose=True)
        try:
            r, _ = _capture(lambda: rknn.config(target_platform=TARGET))
            if r != 0:
                raise RuntimeError(f"config ret={r}")
            r, log_load = _capture(lambda: rknn.load_onnx(model=src))
            if r != 0:
                raise RuntimeError(f"load_onnx ret={r}")
            r, log_build = _capture(lambda: rknn.build(do_quantization=False))
            if r != 0:
                raise RuntimeError(f"build ret={r}")
            r, _ = _capture(lambda: rknn.export_rknn(dst))
            if r != 0:
                raise RuntimeError(f"export ret={r}")
            low = (log_load + "\n" + log_build).lower()
            rep[name] = {"status": "OK", "rknn": os.path.basename(dst),
                         "size": os.path.getsize(dst),
                         "unsupported": "unsupported" in low,
                         "warn": "warn" in low}
        except Exception as e:                              # noqa: BLE001
            rep[name] = {"status": "FAIL", "error": f"{type(e).__name__}: {e}"[:600]}
        finally:
            try:
                rknn.release()
            except Exception:                               # noqa: BLE001
                pass
        st = rep[name]
        print(f"{name:38s} {st['status']}  {st.get('error','')}")
        sys.stdout.flush()

    with open(os.path.join(OUT, "s10_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    ok = sum(1 for v in rep.values() if v["status"] == "OK")
    print(f"\nOK {ok}/{len(CASES)}  ->  {OUT}")


if __name__ == "__main__":
    main()
