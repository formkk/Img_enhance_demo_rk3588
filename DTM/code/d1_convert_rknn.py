"""d1：把 d0 导出的 4 通道 YOLOv6 ONNX 转成 RKNN（FP16，与既有 10 个 DCM 网同口径）。

环境：`~/anaconda3/envs/dtm/bin/python`（rknn-toolkit2 2.3.2）

用法：
  ~/anaconda3/envs/dtm/bin/python DTM/code/d1_convert_rknn.py
"""

import os
import sys

from rknn.api import RKNN

ONNX = "DTM/assets/best_ckpt_1_voc20_bayer4ch_320.onnx"
OUT = "DTM/assets/best_ckpt_1_voc20_bayer4ch_320.rknn"

# 4 通道输入（R0,G0,B0,G1）；他们 `process_image` 后**不做 /255**（源码里被注释掉），
# 故喂进网络的数值就是 0..255 ⇒ mean=0 / std=1，保持原值。
MEAN = [[0.0, 0.0, 0.0, 0.0]]
STD = [[1.0, 1.0, 1.0, 1.0]]


def main() -> int:
    for p in (ONNX,):
        if not os.path.exists(p):
            print("MISSING:", p)
            return 1
    rknn = RKNN(verbose=True)
    print("--> config")
    rknn.config(
        mean_values=MEAN,
        std_values=STD,
        target_platform="rk3588",
    )
    print("--> load_onnx")
    if rknn.load_onnx(model=ONNX) != 0:
        print("load_onnx FAILED")
        return 1
    print("--> build (do_quantization=False ⇒ FP16)")
    if rknn.build(do_quantization=False) != 0:
        print("build FAILED")
        return 1
    print("--> export_rknn")
    if rknn.export_rknn(OUT) != 0:
        print("export_rknn FAILED")
        return 1
    rknn.release()
    print("CONVERT_OK", OUT, os.path.getsize(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
