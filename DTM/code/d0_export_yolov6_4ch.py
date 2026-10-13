"""d0：在 147 数据机上把 VOC-20 检测器（best_ckpt_1.pt）导出为 4 通道 ONNX。

背景（2026-10-10/11）：
  - 权重来源 `/home/jiangfeng/jf_app/fpga_u280_quant/weights/best_ckpt_1.pt`
    （YOLOv6，nc=20 = PASCAL VOC 类的 399-epoch 权重）
  - 该模型第一层卷积为 [16,4,3,3] ⇒ **4 通道输入**；第 4 通道由 *RGB 子采样* 构造，
    见 `mq_td_demo/yolov6/core/inferer.py::png2bayer_patten()`：
        R0=R[...,0::2,0::2], G0=G[...,0::2,0::2], B0=B[...,0::2,0::2], G1=G[...,1::2,1::2]
        new_im = concat([R0,G0,B0,G1], axis=0)
    即 4ch 仍是 RGB 域表示（非传感器 RAW）⇒ 可用于我们的 RGB 增强帧。
  - 空间减半 ⇒ 他们 `--img-size 640`（letterbox）对应网络真实输入 **4x320x320**。
  - 模型内部含 2x 上采样 ⇒ 320 输入的检测网格等价 640（输出 (1,8400,25)）。

运行环境（147 上，需 torch + onnx）：`~/anaconda3/envs/mq_det/bin/python`

用法：`ssh 147_data '~/anaconda3/envs/mq_det/bin/python -' < d0_export_yolov6_4ch.py`
"""

import os
import sys
import time

import torch
import torch.nn as nn

REPO = "/home/jiangfeng/mq_demo_code/mq_td_demo"
W = "/home/jiangfeng/jf_app/fpga_u280_quant/weights/best_ckpt_1.pt"
OUTDIR = "/tmp/yolov6_export"
OUT = os.path.join(OUTDIR, "best_ckpt_1_voc20_bayer4ch_320.onnx")

LETTERBOX = 640          # 他们的 --img-size 默认值（letterbox 尺寸）
NET = LETTERBOX // 2     # png2bayer_patten 空间减半 ⇒ 网络真实输入
NCH = 4

sys.path.insert(0, REPO)
os.makedirs(OUTDIR, exist_ok=True)

from yolov6.layers import common as C

try:
    SiLU = C.SiLU
except AttributeError:
    SiLU = nn.SiLU
try:
    from yolov6.models.effidehead import Detect
except Exception:
    from yolov6.models.heads import Detect

print("torch", torch.__version__, "| letterbox", LETTERBOX, "-> net", NET, "| nch", NCH)
t0 = time.time()

# 与官方 deploy/ONNX/export_onnx.py 一致：EMA 权重优先（同 load_checkpoint 口径）
ck = torch.load(W, map_location="cpu", weights_only=False)
model = ck["ema" if ck.get("ema") else "model"].float()
print("weights:", "ema" if ck.get("ema") else "model", "| epoch", ck.get("epoch"),
      "| nc", getattr(model, "nc", None))
model.eval()

n_repvgg = 0
for m in model.modules():
    if isinstance(m, C.RepVGGBlock):
        m.switch_to_deploy()
        n_repvgg += 1
    elif isinstance(m, nn.Upsample) and not hasattr(m, "recompute_scale_factor"):
        m.recompute_scale_factor = None
print("RepVGG switched:", n_repvgg)

n_silu = n_det = 0
for _, m in model.named_modules():
    if isinstance(m, C.ConvModule):
        if hasattr(m, "act") and isinstance(m.act, nn.SiLU):
            m.act = SiLU()
            n_silu += 1
    elif isinstance(m, Detect):
        m.inplace = False
        if hasattr(m, "export"):
            m.export = True
        n_det += 1
print("SiLU swapped:", n_silu, "| Detect:", n_det)


def shape_of(v):
    if isinstance(v, torch.Tensor):
        return tuple(v.shape)
    return [shape_of(x) for x in v]


for sz in (NET, LETTERBOX):
    with torch.no_grad():
        y = model(torch.zeros(1, NCH, sz, sz))
    print(f"  probe {NCH}x{sz}x{sz} -> {shape_of(y)}")

img = torch.zeros(1, NCH, NET, NET)
torch.onnx.export(model, img, OUT, verbose=False, opset_version=13,
                  training=torch.onnx.TrainingMode.EVAL, do_constant_folding=True,
                  input_names=["images"], output_names=["outputs"])

import onnx  # noqa: E402

mo = onnx.load(OUT)
onnx.checker.check_model(mo)
print("graph inputs :", [(i.name, [d.dim_value for d in i.type.tensor_type.shape.dim]) for i in mo.graph.input])
print("graph outputs:", [(o.name, [d.dim_value for d in o.type.tensor_type.shape.dim]) for o in mo.graph.output])
print("producer:", mo.producer_name, mo.producer_version, "| opset:", mo.opset_import[0].version)
print("size:", os.path.getsize(OUT), "| elapsed %.1fs" % (time.time() - t0))
print("EXPORT_OK", OUT)
