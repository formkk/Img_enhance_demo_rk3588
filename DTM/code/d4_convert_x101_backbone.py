"""d4：把 X-101 backbone+FPN 的 ONNX 转成 RKNN（FP16），判别"能否上板"。

环境：`~/anaconda3/envs/dtm/bin/python`（rknn-toolkit2 2.3.2）

链路：res2(1,128,192,336) -> feature_adapter -> D -> bottom_up(res3/4/5) -> my_fpn_forward -> p2..p6
目的：**只判别可转换性与体积/速度，不做精度结论**（RoIHeads 不在内 => 无检测结果）。

用法：
  ~/anaconda3/envs/dtm/bin/python DTM/code/d4_convert_x101_backbone.py
"""

import os
import sys

from rknn.api import RKNN

ONNX = "DTM/assets/x101/x101_backbone_fpn.onnx"
OUT = "DTM/assets/x101/x101_backbone_fpn.rknn"

# 输入是 DCM 解码特征（128ch），非图像 => 不做任何归一化
MEAN = [[0.0] * 128]
STD = [[1.0] * 128]


def main() -> int:
    if not os.path.exists(ONNX):
        print("MISSING:", ONNX)
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
    print("--> build (do_quantization=False => FP16)")
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
