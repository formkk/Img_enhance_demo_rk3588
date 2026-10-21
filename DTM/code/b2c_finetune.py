#!/usr/bin/env python3
"""b2c_finetune.py —— DCM 演示路「检测器」在**特征域**上的适配微调（D 模块 + RPN + ROI）。

口径来源（本文件为**重写版**，2026-10-15）
----------------------------------------
原脚本只存在于 147（`jf_app/dcm_c/b2c_finetune.py`），2026-10-15 随 `jf_app/*` 一起被清理
⇒ **本文件按记录重写**，设计取自 `DTM/立项草案-DCM演示路.md` §8.21(四)：
  · D = ResNet **BasicBlock(256→256, GN)**，**末层 Conv 零初始化**（⇒ 初始恒等；
    adapter 末尾是 ReLU ⇒ 激活恒 ≥0 ⇒ `relu(identity) ≡ identity`，与锚点 0.29981 ≡ 官方 0.300 自洽）
  · 冻结 `bottom_up / fpn / adapter`（90.18M），训练 **D + RPN + ROI = 16.08M**
  · 链路：`res2 → adapter(冻结) → D(可训练) → bottom_up.res3/4/5 → my_fpn_forward → RPN → ROI`
  · `bs=4`（§8.21 坑#8：bs=8 会因 X-101 autograd 激活 >21 GB 报误导性 cuDNN 错）、
    AdamW `lr=1e-4` + cosine、grad clip 10
**⚠️ 无法逐字复刻**（原文件已丢）⇒ 与旧 `0.432` 的对比只能算"参考"；
**对照办法** = **同一份本脚本**内跑 `--n-train 2000`（基线）与 `--n-train 10000`（扩容），
脚本内 `--n-train` 取"文件名排序前 N" ⇒ **基线的 2000 张是新集的子集** ⇒ 数据量是唯一变量。

数据（`--data-dir`）
--------------------
`res2_<imgid>.pt`（`(1,128,H,W)` float32，q31 解码后特征，由 `b1_precompute.py` 口径产出）。
GT 取自 COCO `instances_train2017.json`，**box 按 codec 尺度换算到"补零后的缩放坐标"**：
  resize（短边 800 / 长边 ≤1333，与 `tools/main.py` 同口径）→ 补零到 64 倍数；
  **补零只加在右/下 ⇒ 内容的补零坐标 == 内容坐标**；
  `image_size` 必须传 **(PAD_H, PAD_W)**（§8.21 坑#7：传 (H,W) 会再乘 ~0.98 ⇒ 框系统性偏小 2~4%）。
断言 `4 * res2.shape[-2:] == (PAD_H, PAD_W)`，不成立即报错（口径自检）。

用法（147，CPU 编译的 detectron2 即可，训练在 GPU）
------------------------------------------------
  cd /home/jiangfeng/dcm_ft/DTM-V-1.5
  CUDA_VISIBLE_DEVICES=0 PYTHONPATH=<d2src>:<RM_SCRIPTS> \
  /home/jiangfeng/anaconda3/envs/dtm-v/bin/python -u /path/b2c_finetune.py \
      --data-dir <res2 目录> --ann-json /home/jiangfeng/datasets/coco2017/annotations/instances_train2017.json \
      --n-train 10000 --epochs 6 --out <ckpt 目录>

  # 冒烟（4 张、1 epoch、只跑 2 个 iter）
  ... --limit 4 --epochs 1 --max-iters 2
"""

import argparse
import json
import math
import os
import sys
import time
from functools import partial

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image

# `_C` 缺失（CPU 编译）时 detectron2 走 torchvision 回退；此处只需保证 import 顺序与官方一致
from detectron2.config import get_cfg
from detectron2.modeling import build_model
from detectron2.modeling.backbone import Backbone
from detectron2.checkpoint import DetectionCheckpointer
from detectron2.structures import Boxes, Instances, ImageList
from detectron2.utils.events import EventStorage
from Adapter import build_feature_adapter

XMIN, XMAX, PADF = 800, 1333, 64          # 与 tools/main.py（L139-146, L461）同口径
NUM_CLASSES = 80


# ---------------------------------------------------------------- 几何（与 b1_precompute 同）
def resize_like_rm(im):
    """短边到 XMIN、长边不超 XMAX（复刻 `tools/main.py` 的 preprocess_image_data）。"""
    h, w = im.height, im.width
    scale = XMIN * 1.0 / min(h, w)
    newh, neww = (XMIN, scale * w) if h < w else (scale * h, XMIN)
    if max(newh, neww) > XMAX:
        sc = XMAX * 1.0 / max(newh, neww)
        newh, neww = newh * sc, neww * sc
    return int(neww + 0.5), int(newh + 0.5)


def pad_to(hw, f=PADF):
    h, w = hw
    ph = f * ((h // f) + 1) - h if h % f else 0
    pw = f * ((w // f) + 1) - w if w % f else 0
    return ph, pw


# ---------------------------------------------------------------- FPN（抄 RM inference_task.py L234-268）
class _HostShim:
    """把 `model.backbone` 伪装成原 FPN 模块，供 `my_fpn_forward` 取子模块。"""

    def __init__(self, fpn):
        self.backbone = fpn


def my_fpn_forward(bottom_up_features, fpn):
    x = [bottom_up_features[f] for f in fpn.in_features[::-1]]
    results = []
    prev_features = fpn.lateral_convs[0](x[0])
    results.append(fpn.output_convs[0](prev_features))
    for features, lateral_conv, output_conv in zip(
            x[1:], fpn.lateral_convs[1:], fpn.output_convs[1:]):
        top_down_features = F.interpolate(prev_features, scale_factor=2, mode="nearest")
        lateral_features = lateral_conv(features)
        prev_features = lateral_features + top_down_features
        if fpn._fuse_type == "avg":
            prev_features /= 2
        results.insert(0, output_conv(prev_features))
    if fpn.top_block is not None:
        top_block_in_feature = bottom_up_features.get(fpn.top_block.in_feature, None)
        if top_block_in_feature is None:
            top_block_in_feature = results[fpn._out_features.index(fpn.top_block.in_feature)]
        results.extend(fpn.top_block(top_block_in_feature))
    assert len(fpn._out_features) == len(results)
    return dict(zip(fpn._out_features, results))


# ---------------------------------------------------------------- 可训练的 D 模块
def build_d_module(ch=256):
    """D = ResNet BasicBlock（256→256，GN），**末层 Conv 零初始化 ⇒ 初始恒等**。"""
    from detectron2.modeling.backbone.resnet import BasicBlock
    block = BasicBlock(ch, ch, norm="GN", stride=1)
    nn.init.zeros_(block.conv2.weight)
    if block.conv2.bias is not None:            # d2 的 Conv2d 在 norm!="" 时 bias=False
        nn.init.zeros_(block.conv2.bias)
    return block


class DCMBackbone(Backbone):
    """忽略输入图像张量，改用外部 `set_res2()` 注入的解码特征：`res2→adapter→D→res3/4/5→FPN`。"""

    def __init__(self, fpn, adapter, d_module):
        super().__init__()
        self.fpn = fpn
        self.adapter = adapter
        self.d = d_module
        self._res2 = None

    @property
    def size_divisibility(self):                 # `Backbone` 把它定义成 property ⇒ 只能覆写成 property
        return getattr(self.fpn, "size_divisibility", 32)

    def set_res2(self, res2):
        self._res2 = res2

    def output_shape(self):
        return self.fpn.output_shape()

    def forward(self, images):
        assert self._res2 is not None, "先调用 DCMBackbone.set_res2(...)"
        a = self.adapter(self._res2)
        feats = self.d(a)
        r3 = self.fpn.bottom_up.res3(feats)
        r4 = self.fpn.bottom_up.res4(r3)
        r5 = self.fpn.bottom_up.res5(r4)
        return my_fpn_forward({"res2": feats, "res3": r3, "res4": r4, "res5": r5},
                              self.fpn)


# ---------------------------------------------------------------- 配置
def setup_cfg(args):
    cfg = get_cfg()
    cfg.merge_from_file(args.config_file)
    cfg.MODEL.WEIGHTS = args.detector_weights
    cfg.MODEL.DEVICE = args.device
    cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = 0.05
    cfg.INPUT.MIN_SIZE_TEST = XMIN
    cfg.INPUT.MAX_SIZE_TEST = XMAX
    cfg.merge_from_list(args.opts)
    cfg.freeze()
    return cfg


# ---------------------------------------------------------------- 数据
class Res2Dataset(torch.utils.data.Dataset):
    """`res2_<imgid>.pt` + COCO GT（box 已换算到"补零后的缩放坐标"）。"""

    def __init__(self, data_dir, ann_json, keep=2000, val500_dir=None, limit=0,
                 skip_empty=False, scale_dirs=None):
        # **（S18-C 多尺度）** 尺度池 = [(xmin, dir), ...]；第一档用于列文件名，其余档须同集合，
        # 缺任何一张即 fail-fast（避免训练中途才报错）。**无尺度池时逐字不变。**
        self.scale_dirs = [(int(a), b) for a, b in scale_dirs] if scale_dirs else None
        if self.scale_dirs:
            data_dir = self.scale_dirs[0][1]
        self.paths = sorted(p for p in os.listdir(data_dir)
                            if p.startswith("res2_") and p.endswith(".pt"))
        if not self.paths:
            raise RuntimeError(f"{data_dir} 下没有 res2_*.pt")
        if self.scale_dirs:
            for _s, _d in self.scale_dirs:
                _have = set(q for q in os.listdir(_d) if q.startswith("res2_"))
                _miss = [q for q in self.paths if q not in _have]
                if _miss:
                    raise RuntimeError(f"尺度档 xmin={_s}（{_d}）缺 {len(_miss)} 个 res2（例 {_miss[:2]}）")
            print(f"[data] 尺度池 {[x for x, _ in self.scale_dirs]}（{len(self.paths)} 张/档）", flush=True)
        self.data_dir = data_dir
        self.ids = [p[len("res2_"):-len(".pt")] for p in self.paths]
        with open(ann_json, "r", encoding="utf-8") as fh:
            ann = json.load(fh)
        # **COCO 的 `category_id` 有空洞（1..90，跳过若干）** ⇒ 必须按 `categories` 顺序映射到 0..79，
        # 不能写 `category_id - 1`（那会给出 89 ⇒ 越界 ⇒ CUDA device-side assert；本次冒烟踩到）。
        self.cat_map = {int(c["id"]): i for i, c in enumerate(ann["categories"])}
        self.inv_cat = {i: int(c["id"]) for i, c in enumerate(ann["categories"])}
        if len(self.cat_map) != NUM_CLASSES:
            raise RuntimeError(f"categories 数 {len(self.cat_map)} != {NUM_CLASSES}")
        by_id = {str(im["id"]).zfill(12): im for im in ann["images"]}
        self.ann_by_id = by_id
        self.inst_by_img = {}
        for a in ann["annotations"]:
            if a.get("iscrowd", 0):
                continue
            key = str(a["image_id"]).zfill(12)
            self.inst_by_img.setdefault(key, []).append(a)
        missing = [i for i in self.ids if i not in by_id]
        if missing:
            raise RuntimeError(f"{len(missing)} 个 imgid 在 {ann_json} 里找不到（例：{missing[:3]}）")
        # 正式训练集 = 文件名排序前 keep 张（--n-train）；与 coco_val_500 零交集断言
        self.keep_idx = list(range(len(self.ids)))[:keep]
        if limit > 0:                                   # 冒烟
            self.keep_idx = self.keep_idx[:limit]
        # **（2026-10-16 S17d）** 跳过无 GT（非 crowd）空图 —— **旧脚本口径**（§8.21「1984/2000」）。
        # 动机：纯负样本图会稀释有效梯度；且旧脚本确实过滤（本次已用 `datasets/coco_train2000`
        # 复算：恰好 16/2000 无 GT，与记录逐字吻合）⇒ 为忠实复现必须对齐。
        if skip_empty:
            n0 = len(self.keep_idx)
            self.keep_idx = [i for i in self.keep_idx
                             if len(self.inst_by_img.get(self.ids[i], [])) > 0]
            print(f"[data] --skip-empty-gt：跳过无 GT 空图 {n0 - len(self.keep_idx)} 张"
                  f" ⇒ {len(self.keep_idx)} 张/epoch", flush=True)
        if val500_dir and os.path.isdir(val500_dir):
            val_ids = {n.split(".")[0] for n in os.listdir(val500_dir)}
            dup = sorted(set(self.ids[i] for i in self.keep_idx) & val_ids)
            if dup:
                raise RuntimeError(f"训练集与 coco_val_500 有 {len(dup)} 张交集（例：{dup[:3]}）")
            print(f"[data] {len(self.keep_idx)} 张，与 coco_val_500 零交集 OK", flush=True)

    def __len__(self):
        return len(self.keep_idx)

    def __getitem__(self, i):
        idx = self.keep_idx[i]
        img_id = self.ids[idx]
        # **（S18-C 尺度抖动）** 有尺度池时按样本随机取一档：`res2` 取自该档目录，几何（含 GT 框
        # 换算）按该档 `xmin` 同式重算 ⇒ 新尺度下 GT 与 res2 仍严格自洽（下方几何断言兜底）。
        # **无尺度池时走原路径，逐字不变。**
        sdir = self.data_dir
        if self.scale_dirs:
            xmin, sdir = self.scale_dirs[int(torch.randint(len(self.scale_dirs), (1,)).item())]
        res2 = torch.load(os.path.join(sdir, self.paths[idx]), map_location="cpu")
        res2 = res2.float()
        _, _, h4, w4 = res2.shape
        PAD_H, PAD_W = 4 * h4, 4 * w4                  # 反推模型输入（补零后）尺寸
        im = self.ann_by_id[img_id]
        W0, H0 = int(im["width"]), int(im["height"])
        if self.scale_dirs:
            xmax = int(round(XMAX * xmin / float(XMIN)))
            s0 = xmin * 1.0 / min(H0, W0)
            rh, rw = (xmin, s0 * W0) if H0 < W0 else (s0 * H0, xmin)
            if max(rh, rw) > xmax:
                s1 = xmax * 1.0 / max(rh, rw)
                rh, rw = rh * s1, rw * s1
            RW, RH = int(rw + 0.5), int(rh + 0.5)
            clamp = xmax        # **（S18-C 修正）** 长边上限随尺度而变（960⇒1600 / 640⇒1066），
                                # 不能再用单尺度常量 XMAX 夹 —— 否则与已算好的该档 res2 不自洽。
        else:
            RW, RH = resize_like_rm_image_size(H0, W0)
            clamp = XMAX
        if max(RH, RW) > clamp:                        # resize 后再按长边夹一次（与 resize_like_rm 一致）
            sc = clamp * 1.0 / max(RH, RW)
            RH, RW = int(RH * sc + 0.5), int(RW * sc + 0.5)
        ph, pw = pad_to((RH, RW))
        if (RH + ph, RW + pw) != (PAD_H, PAD_W):
            raise RuntimeError(f"{img_id}: 几何不自洽 (RH+ph,RW+pw)=({RH + ph},{RW + pw}) "
                               f"vs res2 反推 ({PAD_H},{PAD_W})")
        sx, sy = RW * 1.0 / W0, RH * 1.0 / H0
        boxes, labels = [], []
        for a in self.inst_by_img.get(img_id, []):
            x, y, w, h = a["bbox"]
            boxes.append([x * sx, y * sy, (x + w) * sx, (y + h) * sy])
            lab = self.cat_map[int(a["category_id"])]          # 0..79 连续映射（见上）
            if not 0 <= lab < NUM_CLASSES:
                raise RuntimeError(f"{img_id}: 类别越界 {lab}")
            labels.append(lab)
        gt = Instances((PAD_H, PAD_W),
                       gt_boxes=Boxes(torch.tensor(boxes, dtype=torch.float32).reshape(-1, 4)),
                       gt_classes=torch.tensor(labels, dtype=torch.int64))
        return {"img_id": img_id, "res2": res2, "instances": gt,
                "height": PAD_H, "width": PAD_W}


def resize_like_rm_image_size(H, W):
    """与 `resize_like_rm` 同式的纯尺寸版（输入原图 H,W ⇒ 输出 缩放后 RW,RH）。"""
    scale = XMIN * 1.0 / min(H, W)
    newh, neww = (XMIN, scale * W) if H < W else (scale * H, XMIN)
    if max(newh, neww) > XMAX:
        sc = XMAX * 1.0 / max(newh, neww)
        newh, neww = newh * sc, neww * sc
    return int(neww + 0.5), int(newh + 0.5)


def collate(batch):
    """把不同 HxW 的 res2 右侧/下方补零到批内最大，再拼成 (B,128,Hmax,Wmax)。

    图像张量 = 与 res2 同尺寸的**零图**（backbone 忽略它，只用于 `image_sizes` 与 `input_shape`）。
    """
    hs = max(b["height"] for b in batch)
    ws = max(b["width"] for b in batch)
    res2_list, img_list, inputs = [], [], []
    for b in batch:
        t = b["res2"]
        if t.shape[2] != hs // 4 or t.shape[3] != ws // 4:
            t = F.pad(t, (0, ws // 4 - t.shape[3], 0, hs // 4 - t.shape[2]))
        res2_list.append(t)
        img_list.append(torch.zeros(3, hs, ws))
        inputs.append({"image": torch.zeros(3, b["height"], b["width"]),
                       "height": b["height"], "width": b["width"],
                       "instances": b["instances"], "img_id": b["img_id"]})
    return {"res2": torch.cat(res2_list, 0), "images": img_list, "inputs": inputs}


# ---------------------------------------------------------------- 主
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, help="res2_<imgid>.pt 所在目录")
    ap.add_argument("--ann-json", required=True, help="instances_train2017.json")
    ap.add_argument("--config-file",
                    default="configs/COCO-Detection/faster_rcnn_X_101_32x8d_FPN_3x.yaml")
    ap.add_argument("--detector-weights", required=True, help="model_final_68b088.pkl")
    ap.add_argument("--adapter-model-path", required=True,
                    help="DTM codec 权重（取其中 feature_adapter）")
    ap.add_argument("--val500-dir", default="/home/jiangfeng/datasets/coco_val_500")
    ap.add_argument("--n-train", type=int, default=2000, help="文件名排序前 N 张")
    ap.add_argument("--limit", type=int, default=0, help="冒烟：只取前 N 张（覆盖 --n-train）")
    ap.add_argument("--epochs", type=int, default=24)
    ap.add_argument("--bs", type=int, default=4, help="§8.21 坑#8：bs=8 会 OOM/报误导错")
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--weight-decay", type=float, default=0.0)
    # **（2026-10-15 正则探针 S17c）** 默认 `adamw` + `momentum` 仅 SGD 用 ⇒ **默认路径与已跑各轮逐位相同**。
    ap.add_argument("--optim", default="adamw", choices=("adamw", "sgd"))
    ap.add_argument("--momentum", type=float, default=0.9, help="仅 --optim sgd 使用")
    # **（2026-10-16 方案重设计 / S17d）** 以下四项为"对齐 d2 惯例"的开关；**默认全部关闭 ⇒
    # 默认路径与已跑各轮逐位相同**（既有结果不受影响）。
    #   `--lr-sched iter`：**逐 iter** 余弦（d2 惯例；原实现是 `CosineAnnealingLR` 逐 epoch 步进，
    #     24 ep 只有 24 个 LR 值 —— 粗阶梯，是本轮怀疑的配方差异之一）。
    #   `--warmup-iters`：线性 warmup（0.1×lr → 1×lr）。
    #   `--no-wd-norm-bias`：GN/BN/偏置参数**免 weight decay**（d2 `get_default_optimizer_params` 惯例；
    #     否则 wd 会作用到 GN 权重与零初始化的 D 末层卷积上）。
    #   `--skip-empty-gt`：跳过无 GT（非 crowd）空图 —— **旧脚本口径**（§8.21「1984/2000」）。
    ap.add_argument("--lr-sched", default="epoch", choices=("epoch", "iter"))
    ap.add_argument("--warmup-iters", type=int, default=0)
    ap.add_argument("--no-wd-norm-bias", action="store_true")
    ap.add_argument("--skip-empty-gt", action="store_true")
    ap.add_argument("--ema-decay", type=float, default=0.0,
                    help=">0 时启用 EMA（仅可训练参数），并存 ep<N>_ema.pth")
    # **（S18-C 多尺度）** 尺度池，形如 `800:/path/res2/old2000,960:/path/res2/old2000_s960`；
    # 空 = 单尺度（走 `--data-dir`）。各档 res2 由 `b1b_precompute_res2.py --xmin S` 生成。
    ap.add_argument("--scale-dirs", default="",
                    help="尺度池 `xmin:dir` 逗号分隔；空=单尺度")
    ap.add_argument("--clip", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True, help="checkpoint 目录（ep<N>.pth）")
    ap.add_argument("--max-iters", type=int, default=0, help=">0 时每 epoch 只跑这么多 iter")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--opts", nargs=argparse.REMAINDER, default=[])
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    os.makedirs(args.out, exist_ok=True)

    cfg = setup_cfg(args)
    print(f"[cfg] {args.config_file}", flush=True)
    model = build_model(cfg)
    DetectionCheckpointer(model).load(cfg.MODEL.WEIGHTS)
    print("[model] detector weights loaded", flush=True)

    # 组装 DCMBackbone：wrap 原 FPN，接上 adapter（冻结）+ D（可训练）
    fpn = model.backbone
    adapter = build_feature_adapter(128, 256)
    ck = torch.load(args.adapter_model_path, map_location="cpu")
    adapter.load_state_dict(ck["feature_adapter"])
    d_module = build_d_module()
    model.backbone = DCMBackbone(fpn, adapter, d_module)
    model.to(torch.device(args.device))

    # 冻结全部，只解冻 D + RPN + ROI
    for p in model.parameters():
        p.requires_grad_(False)
    trainable = list(model.backbone.d.parameters())
    for m in (model.proposal_generator, model.roi_heads):
        for p in m.parameters():
            p.requires_grad_(True)
            trainable.append(p)
    n_tr = sum(p.numel() for p in trainable)
    n_all = sum(p.numel() for p in model.parameters())
    print(f"[params] 总 {n_all / 1e6:.2f}M / 可训练 {n_tr / 1e6:.2f}M"
          f"（基线记录：16.08M / 90.18M ⇒ 应一致或极接近）", flush=True)

    _sd = []
    if args.scale_dirs:
        for _tok in args.scale_dirs.split(","):
            _tok = _tok.strip()
            if _tok:
                _x, _d = _tok.split(":", 1)
                _sd.append((int(_x), _d))
    ds = Res2Dataset(args.data_dir, args.ann_json, keep=args.n_train,
                     val500_dir=args.val500_dir, limit=args.limit,
                     skip_empty=args.skip_empty_gt, scale_dirs=(_sd or None))
    print(f"[data] 训练集 {len(ds)} 张（--n-train={args.n_train}）", flush=True)
    dl = torch.utils.data.DataLoader(ds, batch_size=args.bs, shuffle=True,
                                     collate_fn=collate, num_workers=2, drop_last=True)

    # **（2026-10-16 方案重设计 / S17d）** 参数分组：`--no-wd-norm-bias` 时把 GN/BN 与偏置
    # （`ndim<=1` 或名字含 `norm`/`bn`/`bias`）放进 **wd=0** 组，其余进 `wd=args.weight_decay` 组。
    # d2 的 `get_default_optimizer_params` 即此惯例；否则 wd 会衰减 GN 权重与零初始化的 D 末层卷积。
    if args.no_wd_norm_bias:
        decay, no_decay = [], []
        for name, p in model.named_parameters():
            if not p.requires_grad:
                continue
            ln = name.lower()
            if p.ndim <= 1 or name.endswith("bias") or "norm" in ln or ".gn" in ln:
                no_decay.append(p)
            else:
                decay.append(p)
        groups = [{"params": decay, "weight_decay": args.weight_decay},
                  {"params": no_decay, "weight_decay": 0.0}]
        print(f"[wd-groups] 受 wd 参数 {sum(p.numel() for p in decay) / 1e6:.2f}M / "
              f"免 wd 参数 {sum(p.numel() for p in no_decay) / 1e6:.2f}M", flush=True)
    else:
        groups = [{"params": trainable, "weight_decay": args.weight_decay}]

    if args.optim == "adamw":
        opt = torch.optim.AdamW(groups, lr=args.lr)
    elif args.optim == "sgd":
        opt = torch.optim.SGD(groups, lr=args.lr, momentum=args.momentum)
    else:
        raise ValueError(f"未知 --optim {args.optim!r}（可选 adamw / sgd）")

    # **（S17d）** `--lr-sched iter` = **逐 iter** 余弦 + 线性 warmup（d2 惯例）；
    # `epoch`（默认）= 原实现（`CosineAnnealingLR` 逐 epoch 步进），保持既有各轮行为不变。
    if args.lr_sched == "iter":
        total_iters = args.epochs * max(1, len(dl))

        def _lr_scale(step):                       # step 从 0 计
            if args.warmup_iters > 0 and step < args.warmup_iters:
                return 0.1 + 0.9 * step / float(args.warmup_iters)
            p = (step - args.warmup_iters) / float(max(1, total_iters - args.warmup_iters))
            p = min(1.0, max(0.0, p))
            return 0.5 * (1.0 + math.cos(math.pi * p))

        sched = torch.optim.lr_scheduler.LambdaLR(opt, _lr_scale)
        step_per_iter = True
    else:
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
        step_per_iter = False
    print(f"[optim] {args.optim} lr={args.lr} wd={args.weight_decay} momentum={args.momentum} "
          f"clip={args.clip} epochs={args.epochs} lr_sched={args.lr_sched}"
          f"(warmup={args.warmup_iters}) ema={args.ema_decay}"
          f"{' +norm/bias免wd' if args.no_wd_norm_bias else ''}", flush=True)

    # **（S17d）EMA**：仅对**可训练**参数维护滑动平均（16.08M ⇒ 缓冲 ~64 MB，成本可忽略）；
    # 存盘时把 EMA 值覆盖进当前 `state_dict` ⇒ 与既有评测脚本零改动兼容（`ep<N>_ema.pth`）。
    ema, ema_pairs = {}, []
    if args.ema_decay > 0:
        for name, p in model.named_parameters():
            if p.requires_grad:
                ema[name] = p.detach().clone().float()
                ema_pairs.append((ema[name], p))
        print(f"[ema] 启用，decay={args.ema_decay}，覆盖 {len(ema)} 个张量", flush=True)
    model.train()

    # d2 的 RPN/ROI loss 内部会 `get_event_storage()` ⇒ 训练循环必须包在 `EventStorage` 上下文里
    # （d2 标准 Trainer 即如此；缺它会在 `rpn.py:401` 断言失败——本次冒烟踩到）。
    with EventStorage(0) as storage:
        for ep in range(1, args.epochs + 1):
            t0 = time.time()
            agg, n = {}, 0
            for it, batch in enumerate(dl, 1):
                res2 = batch["res2"].to(args.device)
                model.backbone.set_res2(res2)
                for inp in batch["inputs"]:
                    inp["image"] = inp["image"].to(args.device)
                    inp["instances"] = inp["instances"].to(args.device)
                losses = model(batch["inputs"])                 # d2 标准训练前向（backbone 吃注入的 res2）
                loss = sum(v for k, v in losses.items() if "loss" in k)
                opt.zero_grad(set_to_none=True)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(trainable, args.clip)
                opt.step()
                if step_per_iter:
                    sched.step()
                if ema_pairs:
                    with torch.no_grad():
                        d = args.ema_decay
                        for e, p in ema_pairs:
                            e.mul_(d).add_(p.detach(), alpha=1.0 - d)
                storage.step()
                n += 1
                for k, v in losses.items():
                    agg[k] = agg.get(k, 0.0) + float(v)
                if it % 20 == 0:
                    print(f"[ep{ep} it{it} lr={opt.param_groups[0]['lr']:.2e}] " + " ".join(
                        f"{k}={v / it:.4f}" for k, v in sorted(agg.items())), flush=True)
                if args.max_iters and it >= args.max_iters:
                    break
            if not step_per_iter:
                sched.step()
            msg = " ".join(f"{k}={v / max(n, 1):.4f}" for k, v in sorted(agg.items()))
            print(f"[ep{ep}] iters={n} {msg} {time.time() - t0:.1f}s", flush=True)
            torch.save(model.state_dict(), os.path.join(args.out, f"ep{ep}.pth"))
            if ema_pairs:                      # EMA 权重另存（与主权重同格式 ⇒ 评测脚本零改动）
                sd = model.state_dict()
                for k, v in ema.items():
                    sd[k] = v.clone()
                torch.save(sd, os.path.join(args.out, f"ep{ep}_ema.pth"))
    print("TRAIN_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
