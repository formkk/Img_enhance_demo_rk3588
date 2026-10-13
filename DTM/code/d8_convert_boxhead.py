"""d8：把 box_head + box_predictor ONNX 转成 RKNN（FP16），方案 B 混合架构第三步。

环境：`~/anaconda3/envs/dtm/bin/python`（rknn-toolkit2 2.3.2）

链路（方案 B 混合架构）：
  NPU: res2 -> adapter -> D -> X-101 backbone+FPN -> p2~p6   （已验证 1.83 s）
  NPU: RPN 头 -> objectness + anchor_deltas                  （已验证 0.64 s）
  CPU: anchor 生成 + decode + NMS -> proposals               （29.47 ms）
  CPU: RoIAlign -> pooled (N,256,7,7)                        （615 ms/1000）
  NPU: box_head + box_predictor（本文件）-> cls_logits + box_deltas

输入是 pooled 特征（256ch），非图像 => 不做归一化。

用法：
  ~/anaconda3/envs/dtm/bin/python DTM/code/d8_convert_boxhead.py
"""

import os
import sys

from rknn.api import RKNN

ONNX = "DTM/assets/x101/boxhead.onnx"
OUT = "DTM/assets/x101/boxhead.rknn"

# 单输入 pooled 特征，256 通道
MEAN = [[0.0] * 256]
STD = [[1.0] * 256]


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
