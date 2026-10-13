#!/usr/bin/env python
# export_rpn_head.py
# 导出 detectron2 X-101-FPN 的 RPN 头卷积子图（StandardRPNHead），用于 RKNN 可行性判别（方案 B 第一步）。
# 输入: FPN p2~p6 五级特征（静态 shape）；输出: 每级 objectness logits (1,3,h,w) + anchor_deltas (1,12,h,w)。
# 不含 anchor 生成 / decode / NMS（那些放板端 CPU）。
# 模型构建复用 dcm_c/b3c_eval_official.py；RPN 权重取 dcm_c/ckpt/ep24.pth 的 proposal_generator（微调后）。

import os
import sys

sys.path.insert(0, "/home/jiangfeng/jf_app/DTM-V-1.5/scripts/DCM_Datasets_Feature_Inference_Scripts")

import torch
import torch.nn as nn

# 与 b3c_eval_official.py 相同: detectron2 内部有 @torch.jit.script_method 装饰器, 屏蔽之
def script_method(fn, _rcb=None):
    return fn


def script(obj, optimize=True, _frames_up=0, _rcb=None):
    return obj


import torch.jit

torch.jit.script_method = script_method
torch.jit.script = script

from detectron2.config import get_cfg
from detectron2.modeling import build_model

CONFIG_FILE = "/home/jiangfeng/jf_app/dcm_step1/libs/detectron2/model_zoo/configs/COCO-Detection/faster_rcnn_X_101_32x8d_FPN_3x.yaml"
RPN_CKPT = "/home/jiangfeng/jf_app/dcm_c/ckpt/ep24.pth"
OUT_DIR = "/home/jiangfeng/jf_app/dcm_d/rpn"
OUT_ONNX = os.path.join(OUT_DIR, "rpn_head.onnx")

# 静态 shape（512x288 帧经 RM 几何 + pad64 -> 768x1344 网络输入 -> FPN 五级）
SHAPES = {
    "p2": (1, 256, 192, 336),
    "p3": (1, 256, 96, 168),
    "p4": (1, 256, 48, 84),
    "p5": (1, 256, 24, 42),
    "p6": (1, 256, 12, 21),
}
IN_NAMES = ["p2", "p3", "p4", "p5", "p6"]
OUT_NAMES = ["obj2", "obj3", "obj4", "obj5", "obj6",
             "dlt2", "dlt3", "dlt4", "dlt5", "dlt6"]


class RPNHeadSubgraph(nn.Module):
    """StandardRPNHead: forward([p2..p6]) -> ([obj2..obj6], [dlt2..dlt6])，此处展平为 10 个输出张量。"""

    def __init__(self, rpn_head):
        super().__init__()
        self.rpn_head = rpn_head

    def forward(self, p2, p3, p4, p5, p6):
        obj, dlt = self.rpn_head([p2, p3, p4, p5, p6])
        return obj[0], obj[1], obj[2], obj[3], obj[4], dlt[0], dlt[1], dlt[2], dlt[3], dlt[4]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    device = torch.device("cpu")

    cfg = get_cfg()
    cfg.merge_from_file(CONFIG_FILE)
    cfg.freeze()

    task_model = build_model(cfg)
    task_model.eval().to(device)

    pg = task_model.proposal_generator
    print("proposal_generator type:", type(pg).__module__ + "." + type(pg).__name__)
    print("proposal_generator.in_features:", pg.in_features)
    rpn_head = pg.rpn_head
    print("rpn_head type:", type(rpn_head).__module__ + "." + type(rpn_head).__name__)
    print("rpn_head structure:")
    print(rpn_head)

    # RPN 权重: 取 ep24.pth 的 proposal_generator（微调 D+RPN+ROI 中的 RPN 部分）
    ckpt = torch.load(RPN_CKPT, map_location=device)
    pg_keys = list(ckpt["proposal_generator"].keys())
    print("ep24.pth proposal_generator keys:", pg_keys)
    w_before = float(rpn_head.conv.weight.abs().sum())
    incompatible = pg.load_state_dict(ckpt["proposal_generator"], strict=True)
    w_after = float(rpn_head.conv.weight.abs().sum())
    print("load_state_dict strict=True OK; missing=%s unexpected=%s" % (incompatible.missing_keys, incompatible.unexpected_keys))
    print("rpn_head.conv.weight abs-sum before=%.4f after=%.4f (before 为随机初始化)" % (w_before, w_after))

    subgraph = RPNHeadSubgraph(rpn_head)
    subgraph.eval()

    torch.manual_seed(0)
    inputs = tuple(torch.randn(SHAPES[n], device=device) for n in IN_NAMES)

    with torch.no_grad():
        pt_out = subgraph(*inputs)
    for n, t in zip(OUT_NAMES, pt_out):
        print("  pytorch %s shape=%s" % (n, tuple(t.shape)))

    torch.onnx.export(
        subgraph,
        inputs,
        OUT_ONNX,
        input_names=IN_NAMES,
        output_names=OUT_NAMES,
        opset_version=13,
        do_constant_folding=True,
        dynamic_axes=None,
    )
    print("exported: %s (%d bytes)" % (OUT_ONNX, os.path.getsize(OUT_ONNX)))

    # ---------------- 自检 ----------------
    import onnx
    from collections import Counter

    model = onnx.load(OUT_ONNX)
    onnx.checker.check_model(model)
    print("onnx.checker: OK")
    print("opset_import:", [(op.domain, op.version) for op in model.opset_import])
    g = model.graph

    def dims(v):
        return [d.dim_value if d.HasField("dim_value") else (d.dim_param or "?") for d in v.type.tensor_type.shape.dim]

    for inp in g.input:
        print("  onnx input : %s %s" % (inp.name, dims(inp)))
    for out in g.output:
        print("  onnx output: %s %s" % (out.name, dims(out)))

    ops = Counter(n.op_type for n in g.node)
    print("op census (%d nodes): %s" % (len(g.node), dict(sorted(ops.items()))))

    try:
        import numpy as np
        import onnxruntime as ort

        sess = ort.InferenceSession(OUT_ONNX, providers=["CPUExecutionProvider"])
        feed = {n: t.numpy() for n, t in zip(IN_NAMES, inputs)}
        onnx_out = sess.run(OUT_NAMES, feed)
        for n, a, b in zip(OUT_NAMES, pt_out, onnx_out):
            diff = float(np.max(np.abs(a.numpy() - b)))
            print("  max|delta| %s: %.3e   (pytorch %s vs onnx %s)" % (n, diff, tuple(a.shape), b.shape))
    except Exception as e:
        print("onnxruntime numerical check FAILED: %r" % (e,))


if __name__ == "__main__":
    main()
