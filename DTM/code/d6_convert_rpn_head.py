"""d6：把 RPN 头 ONNX 转成 RKNN（FP16），判别方案 B 混合架构的可行性。

环境：`~/anaconda3/envs/dtm/bin/python`（rknn-toolkit2 2.3.2）

链路（方案 B 混合架构）：
  NPU: res2 -> adapter -> D -> X-101 backbone+FPN -> p2~p6   （已验证 1.83 s）
  NPU: RPN 头（本文件）-> objectness + anchor_deltas
  CPU: anchor 生成 + decode + NMS -> proposals
  CPU: RoIAlign + RoIHeads -> 类别 + 框精修

输入是 FPN 特征（256ch），非图像 => 不做归一化。

用法：
  ~/anaconda3/envs/dtm/bin/python DTM/code/d6_convert_rpn_head.py
"""

import os
import sys

from rknn.api import RKNN

ONNX = "DTM/assets/x101/rpn_head.onnx"
OUT = "DTM/assets/x101/rpn_head.rknn"

# 5 级输入，每级 256 通道
MEAN = [[0.0] * 256] * 5
STD = [[1.0] * 256] * 5


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
