#!/usr/bin/env python3
"""V3：用**板端**单帧跑出来的数据渲染可视化（渲染在开发机做，只是为了能画中文标注）。

输入（先由 `v2_board_oneframe.py` 在板上产出并 scp 回来）：
    DTM/out_v1/board_out/board_y.npy    板端 y（128×36×60）
    DTM/out_v1/board_out/board_yb.bin   板端真码流 y 流
    DTM/out_v1/board_out/board_zb.bin   板端真码流 z 流

**数据来源 = 板端（FP16 移植版）**；本脚本只做画面排版，不改任何数值
（特征图仍是"每通道 p1–p99 拉伸"的纯显示变换，图上已标注）。

用法：~/anaconda3/envs/dtm/bin/python DTM/code/v3_render_from_board.py
"""

import os
import sys
import types
from functools import partial

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.modules.setdefault("torchac", types.ModuleType("torchac"))

import v1_make_visuals as v1                                     # noqa: E402

BO = os.path.join(PROJ, "DTM", "out_v1", "board_out")
OUT = os.path.join(PROJ, "DTM", "out_v1")


def main() -> int:
    y = np.load(os.path.join(BO, "board_y.npy"))
    yb = open(os.path.join(BO, "board_yb.bin"), "rb").read()
    zb = open(os.path.join(BO, "board_zb.bin"), "rb").read()
    n = len(yb) + len(zb)
    print(f"板端: y={len(yb)} B  z={len(zb)} B  合计={n} B  bpp@960x544={n * 8 / (960 * 544):.5f}")
    print(f"板端 y: {y.shape}  值域 [{float(y.min()):.2f}, {float(y.max()):.2f}]"
          f"  均值 {float(y.mean()):.3f}  标准差 {float(y.std()):.3f}")

    p1 = os.path.join(OUT, "board_stream_ribbon.png")
    v1.draw_stream_figure(yb, zb, 960 * 544, 960 * 540 * 3,
                          title="真实码流（板端 FP16）：一个像素 = 1 bit").save(p1)

    p2 = os.path.join(OUT, "board_feature_atlas_y.png")
    v1.atlas(torch.from_numpy(y), 54, 90,
             "y = 真正进熵编码的特征（板端 128×36×60）",
             "板上 NPU 算出来的潜在；码流里那 128 张「小图」就是它们").save(p2)

    print(f"产物 -> {p1}\n        {p2}")
    print("V3_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
