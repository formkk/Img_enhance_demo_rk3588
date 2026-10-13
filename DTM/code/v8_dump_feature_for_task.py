#!/usr/bin/env python3
"""V8：导出 RM 的**解码特征**（`decode_model` 输出 = REC 的输入），供 147 上的特征域任务推理使用。

口径依据 `tools/main.py` L1510-1522：
    feature_out = decode_model(strings, shape, q_map)      # 解码特征（128ch, res2 级）
    torch.save(feature_out, f"res2_{image_name}.pt")
    rec_img_model(feature_out)                            # 同一张量喂 REC
⇒ 对应我们链路的 `09_g_s` 输出（= x_hat），**不是像素、不是 y**。

产出（`DTM/out_v1/feat/`）：
    q03/res2_hq_final_1.pt / q31/res2_hq_final_1.pt       （分别对应第 3 / 第 31 档）
    帧图副本 hq_final_1.png（供 val_dir 取 image_sizes）

用法：~/anaconda3/envs/dtm/bin/python DTM/code/v8_dump_feature_for_task.py
"""

import os
import shutil
import sys
import types

import torch

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.modules.setdefault("torchac", types.ModuleType("torchac"))
import v1_make_visuals as v1                                    # noqa: E402

PAD_H, PAD_W = 576, 960
Q = {3: 0.265, 31: 0.870}
IMG_ID = "hq_final_1"
FRAME = os.path.join(PROJ, "hq_optimize/captures/noise_diag/hq_final_1.png")
OUT = os.path.join(PROJ, "DTM", "out_v1", "feat")


def main() -> int:
    fe, fc = v1.load_chain()
    x = v1.load_frame()
    for q, f in Q.items():
        d = os.path.join(OUT, f"q{q:02d}")
        os.makedirs(d, exist_ok=True)
        qm = torch.full((1, 1, PAD_H // 16, PAD_W // 16), f, dtype=torch.float32)
        with torch.no_grad():
            f0 = fe(x)
            out = fc.compress(f0, qm, rate_control_flag=False,
                              after_padding_H=PAD_H, after_padding_W=PAD_W)
            feat = fc.decompress(out["strings"], out["shape"], qm)["x_hat"]
        n = len(out["strings"][0][0]) + len(out["strings"][1][0])
        torch.save(feat.cpu(), os.path.join(d, f"res2_{IMG_ID}.pt"))
        print(f"q={q:2d}  码流={n} B  {n * 8 / (960 * 544):.5f} bpp  "
              f"特征 shape={tuple(feat.shape)}  值域=[{float(feat.min()):.3f}, {float(feat.max()):.3f}]"
              f"  均值={float(feat.mean()):.4f}  → {d}/res2_{IMG_ID}.pt")
    shutil.copy(FRAME, os.path.join(OUT, "hq_final_1.png"))
    print("V8_DONE", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
