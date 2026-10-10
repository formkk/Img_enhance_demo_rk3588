#!/usr/bin/env python3
"""S1: 用 rknn-toolkit2 把 S0 导出的 ONNX 逐个转 RKNN.

第一轮只测"算子是否被 RKNN 支持": do_quantization=False (FP16), 不涉及量化配置.
  理由: 若 build() 就过不去, 说明算子面不支持, 与量化无关;
        量化(INT8)是第二步, 需另配 mean/std 与校准集.

产物: DTM/out_s1/*.rknn + s1_report.json
"""
from __future__ import annotations

import json
import os
import sys

from rknn.api import RKNN

HERE = os.path.dirname(os.path.abspath(__file__))
S0 = os.path.abspath(os.path.join(HERE, "..", "out_s0"))
OUT = os.path.abspath(os.path.join(HERE, "..", "out_s1"))

CASES = [
    "01_feature_extractor",
    "02_g_a",
    "03_g_a_sft",
    "04_hyper_encoder",
    "05_hyper_decoder",
    "06b_hyper_scale_decoder_float",
    "07b_context_mcm_static",
    "08_g_s_sft",
    "09_g_s",
    "10b_rec_clamp",
]
TARGET = "rk3588"


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
        rknn = RKNN(verbose=False)
        try:
            rknn.config(target_platform=TARGET)
            r = rknn.load_onnx(model=src)
            if r != 0:
                raise RuntimeError(f"load_onnx ret={r}")
            r = rknn.build(do_quantization=False)          # FP16: 只看算子支持
            if r != 0:
                raise RuntimeError(f"build ret={r}")
            r = rknn.export_rknn(dst)
            if r != 0:
                raise RuntimeError(f"export ret={r}")
            rep[name] = {"status": "OK", "rknn": os.path.basename(dst),
                         "size": os.path.getsize(dst)}
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

    with open(os.path.join(OUT, "s1_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    ok = sum(1 for v in rep.values() if v["status"] == "OK")
    print(f"\nOK {ok}/{len(CASES)}  ->  {OUT}")


if __name__ == "__main__":
    main()
