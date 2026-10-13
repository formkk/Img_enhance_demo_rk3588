#!/usr/bin/env python3
"""T1（Step 2-A）：把 T0 导出的特征域单阶段头 ONNX 转 RKNN（FP16，rk3588，同既有 10 个 DCM 网口径）。

输入已是 128ch 特征（非图像）⇒ mean=0 / std=1（不做任何归一化）。
环境：~/anaconda3/envs/dtm/bin/python（rknn-toolkit2）
用法：~/anaconda3/envs/dtm/bin/python DTM/code/t1_convert_rknn.py
"""

import os
import sys

from rknn.api import RKNN

PROJ = "/home/jenkolux/PKU_WH/Img_enhance_demo"
ONNX = os.path.join(PROJ, "DTM/assets/fdet/fdet_feature.onnx")
OUT = os.path.join(PROJ, "DTM/assets/fdet/fdet_feature.rknn")


def main() -> int:
    onnx = sys.argv[1] if len(sys.argv) > 1 else ONNX
    out = sys.argv[2] if len(sys.argv) > 2 else OUT
    if not os.path.exists(onnx):
        print("MISSING:", onnx)
        return 1
    rknn = RKNN(verbose=True)
    print("--> config")
    rknn.config(mean_values=[[0.0] * 128], std_values=[[1.0] * 128], target_platform="rk3588")
    print("--> load_onnx", onnx)
    if rknn.load_onnx(model=onnx) != 0:
        print("load_onnx FAILED")
        return 1
    print("--> build (do_quantization=False ⇒ FP16)")
    if rknn.build(do_quantization=False) != 0:
        print("build FAILED")
        return 1
    print("--> export_rknn")
    if rknn.export_rknn(out) != 0:
        print("export_rknn FAILED")
        return 1
    rknn.release()
    print("CONVERT_OK", out, os.path.getsize(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
