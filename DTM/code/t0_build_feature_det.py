#!/usr/bin/env python3
"""T0（Step 2-A）：定义吃 128ch 解码特征的**单阶段 anchor-free 头**，warm start，导出 ONNX。

设计（结构定型；只验通路，不追精度）：
    输入 res2 (1,128,192,336)  ← DCM 解码特征（stride 4）
      → adapter  Conv2d(128→256,k3,p1)+ReLU     【warm start：载入 RM 预训练的 feature_adapter】
      → l3/l4/l5/l6  逐级 Conv2d(256→256,k3,s2,p1)+ReLU  ⇒ stride 8/16/32/64
      → tower  Conv2d(256→256,k3,p1)+ReLU（各级共享）
      → cls Conv2d(256→80,k3,p1) / reg Conv2d(256→4,k3,p1)   【随机初始化】
      → 输出 RAW 张量 (1, N, 84)：N = 各级 h*w 之和；前 80 为 cls logits，后 4 为 ltrb
    刻意**不带 sigmoid/GN**（sigmoid 与解码、NMS 都留给板端 numpy），保证 ONNX→RKNN 干净。

产物（DTM/assets/fdet/）：
    fdet_feature.onnx        静态输入 (1,128,192,336) 的单输出模型
    feat_q19.npy             板端输入特征（来自 sweep/q19 的解码特征）
    onnx_out_q19.npy         onnxruntime 参考输出 (1,N,84) —— 板端交叉校验用
    t0_report.json           结构/形状/对拍结论

用法：~/anaconda3/envs/dtm/bin/python DTM/code/t0_build_feature_det.py
"""

import json
import os
import sys
import types

import numpy as np
import torch
import torch.nn as nn

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.modules.setdefault("torchac", types.ModuleType("torchac"))
import s0_export_onnx as s0                                     # noqa: E402

OUT = os.path.join(PROJ, "DTM", "assets", "fdet")
FEAT_PT = os.path.join(PROJ, "DTM", "out_v1", "sweep", "q19", "res2_hq_final_1.pt")
NUM_CLS = 80


class FeatureDet(nn.Module):
    """128ch 解码特征 → 单阶段 anchor-free 原始预测 (1,N,84)。"""

    def __init__(self, num_cls=NUM_CLS, ch=256):
        super().__init__()
        self.adapter = nn.Sequential(nn.Conv2d(128, ch, 3, padding=1), nn.ReLU(inplace=True))
        self.l3 = nn.Sequential(nn.Conv2d(ch, ch, 3, stride=2, padding=1), nn.ReLU(inplace=True))
        self.l4 = nn.Sequential(nn.Conv2d(ch, ch, 3, stride=2, padding=1), nn.ReLU(inplace=True))
        self.l5 = nn.Sequential(nn.Conv2d(ch, ch, 3, stride=2, padding=1), nn.ReLU(inplace=True))
        self.l6 = nn.Sequential(nn.Conv2d(ch, ch, 3, stride=2, padding=1), nn.ReLU(inplace=True))
        self.tower = nn.Sequential(nn.Conv2d(ch, ch, 3, padding=1), nn.ReLU(inplace=True),
                                   nn.Conv2d(ch, ch, 3, padding=1), nn.ReLU(inplace=True))
        self.cls = nn.Conv2d(ch, num_cls, 3, padding=1)
        self.reg = nn.Conv2d(ch, 4, 3, padding=1)

    def forward(self, x):
        f = self.adapter(x)
        l3 = self.l3(f)
        l4 = self.l4(l3)
        l5 = self.l5(l4)
        l6 = self.l6(l5)
        outs = []
        for lv in (l3, l4, l5, l6):
            t = self.tower(lv)
            c = self.cls(t).flatten(2)          # (1,80,h*w)
            r = self.reg(t).flatten(2)          # (1,4,h*w)
            outs.append(torch.cat([c, r], dim=1).transpose(1, 2))   # (1,h*w,84)
        return torch.cat(outs, dim=1)           # (1,N,84)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    torch.manual_seed(0)
    model = FeatureDet().eval()
    for p in model.parameters():
        p.requires_grad_(False)

    # ---- warm start：载入 RM 预训练的 feature_adapter ----
    ck = torch.load(os.path.join(s0.ROOT, "packages/Model/MIKcodec_MCM_model/"
                                           "dtm-V_entropy_vm_1_270000.pth"), map_location=s0.DEV)
    fa = ck["feature_adapter"]
    with torch.no_grad():
        model.adapter[0].weight.copy_(fa["adapter.weight"])
        model.adapter[0].bias.copy_(fa["adapter.bias"])
    print("warm start: adapter <- RM feature_adapter", tuple(fa["adapter.weight"].shape))

    x = torch.load(FEAT_PT, map_location=s0.DEV).float()
    assert tuple(x.shape) == (1, 128, 192, 336), tuple(x.shape)
    np.save(os.path.join(OUT, "feat_q19.npy"), x.numpy().astype(np.float32))

    with torch.no_grad():
        ref = model(x).numpy()
    level_hw = [[96, 168], [48, 84], [24, 42], [12, 21]]
    print("torch out", ref.shape, "N expected", sum(h * w for h, w in level_hw))

    onnx_path = os.path.join(OUT, "fdet_feature.onnx")
    torch.onnx.export(model, (x,), onnx_path, opset_version=s0.OPSET,
                      input_names=["feat"], output_names=["pred"],
                      do_constant_folding=True)
    print("onnx ->", onnx_path, os.path.getsize(onnx_path), "B")

    import onnx
    op_types = sorted({n.op_type for n in onnx.load(onnx_path).graph.node})
    print("onnx op_types:", op_types)

    import onnxruntime as ort
    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    o = sess.run(None, {"feat": x.numpy().astype(np.float32)})[0]
    np.save(os.path.join(OUT, "onnx_out_q19.npy"), o)
    dmax = float(np.abs(o - ref).max())
    print("ONNX(ORT) vs torch: max|Δ| =", dmax, " mean|Δ| =", float(np.abs(o - ref).mean()))

    rep = {"input_shape": list(x.shape), "output_shape": list(o.shape),
           "levels_hw": level_hw, "num_points": int(o.shape[1]), "num_cls": NUM_CLS,
           "warm_start": "RM feature_adapter (256,128,3,3)",
           "onnx_op_types": op_types,
           "onnx_vs_torch_max_abs": dmax,
           "onnx_vs_torch_mean_abs": float(np.abs(o - ref).mean())}
    with open(os.path.join(OUT, "t0_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print("T0_DONE", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
