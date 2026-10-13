#!/usr/bin/env python3
"""V7：判定 DCM 的**输入通道约定**（BGR 还是 RGB）—— 用"哪个更贴训练域"来判，不猜配置。

理由：RM 里 `read_image(path, format="BGR")` 与 `Image.fromarray()` 混用，静态读代码会有歧义。
判据（两条一起看）：
  ① 码率：训练域内的（正确）约定应压得更省；
  ② 重建保真：正确约定下 REC 应更贴输入（尤其**不该把输入的色偏反向**）。

产出 DTM/out_v1/conv/：rec_bgr.png / rec_rgb.png
"""

import os
import sys
import types

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.modules.setdefault("torchac", types.ModuleType("torchac"))
import s0_export_onnx as s0                                     # noqa: E402
import v1_make_visuals as v1                                    # noqa: E402
import v6_rm_rec_probe as v6                                    # noqa: E402
from models.rec.rec import build_rec_model                      # noqa: E402

PAD_H, PAD_W, CONTENT_H = 576, 960, 540
OUT = os.path.join(PROJ, "DTM", "out_v1", "conv")


def stats(tag, u8, ref=None):
    m = u8.reshape(-1, 3).mean(0)
    sat = float(np.abs(u8.astype(np.float32) - u8.mean(2, keepdims=True)).max(2).mean())
    s = f"{tag:10s} 均值(ch0,ch1,ch2)={m.round(1)}  饱和={sat:.1f}"
    if ref is not None:
        s += f"  |Δ|均值/通道={np.abs(u8.astype(np.float32) - ref).mean((0, 1)).round(1)}"
    return s


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    fe, fc = v1.load_chain()
    rec = build_rec_model()
    rec.load_state_dict(torch.load(os.path.join(s0.ROOT, "packages/Model/REC_model/model_rec.pt"),
                                   map_location=s0.DEV))
    rec.eval()
    rec.requires_grad_(False)
    fc.context.quantize_func = __import__("functools").partial(fc.quantize)

    x_bgr = v1.load_frame()                                  # v1 现行：ch0 = 文件里的 B
    x_rgb = x_bgr[:, [2, 1, 0], :, :].contiguous()           # 翻成 ch0 = R
    u8_in = np.rint(np.clip(x_bgr[0, :, :CONTENT_H, :].permute(1, 2, 0).numpy(), 0, 1) * 255).astype(np.uint8)
    print("输入帧（按 ch0/ch1/ch2 直接当 R/G/B 看）：", stats("input", u8_in))
    print("（参考：文件用 cv2 读的 BGR 均值 = [204.5 142.4 147.5] ⇒ 存储序 = B 最大）")

    qm = torch.full((1, 1, PAD_H // 16, PAD_W // 16), 0.265, dtype=torch.float32)   # 第 3 档
    for tag, x in (("BGR", x_bgr), ("RGB", x_rgb)):
        with torch.no_grad():
            f0 = fe(x)
            out = fc.compress(f0, qm, rate_control_flag=False,
                              after_padding_H=PAD_H, after_padding_W=PAD_W)
            feat = fc.decompress(out["strings"], out["shape"], qm)["x_hat"]
            pix = rec(feat)
        n = len(out["strings"][0][0]) + len(out["strings"][1][0])
        p = np.clip(pix[0, :, :CONTENT_H, :].permute(1, 2, 0).numpy(), 0, 255).astype(np.uint8)
        v6.write_png(os.path.join(OUT, f"rec_{tag.lower()}.png"), p[:, :, ::-1].tobytes(), PAD_W, CONTENT_H)
        print(f"x={tag}  码流={n:5d} B  {n * 8 / (960 * 544):.5f} bpp   " + stats("REC", p, u8_in))
    print("V7_DONE", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
