#!/usr/bin/env python3
"""b1b_precompute_res2.py —— 批量产出「q31 解码特征 res2」缓存（训练/评测共用）。

为什么需要它（2026-10-15）
--------------------------
147 上原 `DTM-V-1.5/dtmV_output/rec/coco500/{31,train2000}` 的 res2 缓存（≈57 GB）
随 `jf_app/*` 一起被清理 ⇒ 扩大适配集必须先重建该缓存。
本脚本按 `DTM/code/b1_precompute.py`（未丢失）已验证的 codec 调用口径搬过来，
只做一件事：**逐图 → res2**，产物命名与训练脚本对齐（`res2_<imgid>.pt`，**float32**，
与旧缓存同格式、同 dtype ⇒ 与旧记录可比）。

口径（与 `tools/main.py` 一致，逐条有出处）
------------------------------------------
  · resize：短边 800 / 长边 ≤1333（复刻 `tools/main.py` L139-146 与 `preprocess_image_data`）
  · 补零：右/下补到 64 的倍数（`padding_factor=64`，L461）；**内容坐标 == 补零坐标**
  · 送入 encoder：**BGR**、`/255`、float32（同 `b1_precompute.py`）
  · 质量档：`--q 31`（`q_map_factor 0.870` = `rate_control_index_table` 上限）
  · 链路：`fe(x) → fc.compress(x, qm) → fc.decompress(strings, shape, qm)["x_hat"]`
    （== 部署链的编解码往返，即"接收端重建出来的特征"）

用法（147）
----------
  cd /home/jiangfeng/dcm_ft/DTM-V-1.5
  PYTHONPATH=/home/jiangfeng/dcm_ft/d2src:<RM_SCRIPTS> CUDA_VISIBLE_DEVICES=0 \
  /home/jiangfeng/anaconda3/envs/dtm-v/bin/python -u /path/b1b_precompute_res2.py \
      --src-dir /home/jiangfeng/datasets/coco2017/val2017 \
      --exclude-dir /home/jiangfeng/datasets/coco_val_500 \
      --n 4500 --q 31 --out /home/jiangfeng/dcm_ft/res2/val_rest4500 && echo RES2_DONE
"""

import argparse
import json
import os
import sys
import time
import types
from functools import partial

import numpy as np

# 导入顺序敏感：compressai 必须早于 models（同 b1_precompute.py）
sys.modules.setdefault("torchac", types.ModuleType("torchac"))
import compressai                                                    # noqa: F401,E402
import torch                                                         # noqa: E402
from PIL import Image                                                # noqa: E402

XMIN, XMAX, PADF = 800, 1333, 64
QFACTOR = {0: 0.200, 3: 0.265, 7: 0.351, 11: 0.438, 15: 0.524,
           19: 0.611, 23: 0.697, 27: 0.784, 31: 0.870}


def resize_like_rm(im, xmin=XMIN, xmax=XMAX):
    h, w = im.height, im.width
    scale = xmin * 1.0 / min(h, w)
    newh, neww = (xmin, scale * w) if h < w else (scale * h, xmin)
    if max(newh, neww) > xmax:
        sc = xmax * 1.0 / max(newh, neww)
        newh, neww = newh * sc, neww * sc
    return im.resize((int(neww + 0.5), int(newh + 0.5)))


def pad_to(hw, f=PADF):
    h, w = hw
    ph = f * ((h // f) + 1) - h if h % f else 0
    pw = f * ((w // f) + 1) - w if w % f else 0
    return ph, pw


def load_chain(dtm, dev):
    from models.MIK_MCM_codecs import (build_feature_extractor,
                                       build_feature_coding_Mean_MCM_Scale)
    fe = build_feature_extractor()
    fc = build_feature_coding_Mean_MCM_Scale(quality=3, parallel_computing=True)
    ck = torch.load(os.path.join(dtm, "packages/Model/MIKcodec_MCM_model/"
                                      "dtm-V_entropy_vm_1_270000.pth"), map_location="cpu")
    fe.load_state_dict(ck["feature_extractor"])
    fc.gaussian_conditional.scale_table = fc.gaussian_conditional.scale_table.to(torch.int32)
    fc.load_state_dict(ck["feature_coding"])
    fe.eval().to(dev)
    fc.eval().to(dev)
    # **（2026-10-15 修）** `per_channel_shift` 是**普通属性**（`Fix_point_layer.py:156-160` 用
    # `self.per_channel_shift = ...` 直接赋值、非 `register_buffer`）⇒ `.to(dev)` **不会搬运它**。
    # `_load_per_channel_shift_from_state_dict` 会把它放到 `self.weight.device` ⇒ **必须在 `.to(dev)`
    # 之后**再调 `_update_per_channel_shifts`，否则 `torch.bitwise_right_shift(res_cuda, shift_cpu)` 报
    # "found at least two devices, cuda:0 and cpu"（本次首跑即踩到）。
    fc._update_per_channel_shifts(ck["per_channel_shifts"]["feature_coding"])
    fc.context.quantize_func = partial(fc.quantize)
    fe.requires_grad_(False)
    fc.requires_grad_(False)
    return fe, fc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dtm", default="/home/jiangfeng/dcm_ft/DTM-V-1.5")
    ap.add_argument("--src-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--exclude-dir", default="", help="该目录内的文件名将被排除（如 coco_val_500）")
    ap.add_argument("--n", type=int, default=0, help="只取文件名排序前 N 张（0=全部）")
    ap.add_argument("--q", type=int, default=31)
    # **（2026-10-16 S18-C 多尺度）** 短边可配：`--xmin 640/960` 生成尺度变体 res2；
    # `--xmax 0` ⇒ 按 `1333 × xmin/800` 自动等比（d2 多尺度惯例），保证长边不成为新的约束。
    ap.add_argument("--xmin", type=int, default=XMIN, help="resize 短边")
    ap.add_argument("--xmax", type=int, default=0, help="长边上限；0 = 按 1333*xmin/800 自动")
    ap.add_argument("--resume", action="store_true", help="已存在的 res2_*.pt 跳过")
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    sys.path.insert(0, args.dtm)
    os.chdir(args.dtm)                       # DTM 内部用相对路径
    dev = torch.device(args.device)
    os.makedirs(args.out, exist_ok=True)

    files = sorted(f for f in os.listdir(args.src_dir)
                   if f.lower().endswith((".jpg", ".jpeg", ".png")))
    if args.exclude_dir and os.path.isdir(args.exclude_dir):
        ex = set(os.listdir(args.exclude_dir))
        files = [f for f in files if f not in ex]
        print(f"[list] 排除 {args.exclude_dir} ⇒ 余 {len(files)} 张", flush=True)
    if args.n > 0:
        files = files[:args.n]
    xmin = args.xmin
    xmax = args.xmax if args.xmax > 0 else int(round(XMAX * xmin / float(XMIN)))
    print(f"[list] 待处理 {len(files)} 张；q={args.q} qf={QFACTOR[args.q]}；"
          f"短边={xmin} 长边上限={xmax}；out={args.out}", flush=True)

    fe, fc = load_chain(args.dtm, dev)
    print("[chain] codec ready", flush=True)

    t0 = time.perf_counter()
    done = skip = 0
    tot_stream = 0
    manifest = []
    for k, name in enumerate(files, 1):
        img_id = os.path.splitext(name)[0]
        dst = os.path.join(args.out, f"res2_{img_id}.pt")
        if args.resume and os.path.exists(dst):
            skip += 1
            continue
        try:
            im = resize_like_rm(Image.open(os.path.join(args.src_dir, name)).convert("RGB"),
                                xmin, xmax)
        except Exception as e:
            print(f"[skip] {name} {type(e).__name__}", flush=True)
            continue
        W, H = im.width, im.height
        ph, pw = pad_to((H, W))
        PAD_H, PAD_W = H + ph, W + pw
        a = np.asarray(im)[:, :, ::-1].astype(np.float32).transpose(2, 0, 1) / 255.0  # BGR
        a = np.pad(a, ((0, 0), (0, ph), (0, pw)))
        x = torch.from_numpy(a).unsqueeze(0).to(dev)
        qm = torch.full((1, 1, PAD_H // 16, PAD_W // 16), QFACTOR[args.q],
                        dtype=torch.float32).to(dev)
        with torch.no_grad():
            f0 = fe(x)
            out = fc.compress(f0, qm, rate_control_flag=False,
                              after_padding_H=PAD_H, after_padding_W=PAD_W)
            res2 = fc.decompress(out["strings"], out["shape"], qm)["x_hat"]
        nb = len(out["strings"][0][0]) + len(out["strings"][1][0])
        torch.save(res2.detach().cpu().float(), dst)
        tot_stream += nb
        done += 1
        manifest.append({"img_id": img_id, "pad_h": PAD_H, "pad_w": PAD_W,
                         "content_h": H, "content_w": W, "stream_bytes": nb})
        if k % 100 == 0 or k == len(files):
            el = time.perf_counter() - t0
            print(f"[{k}/{len(files)}] ok={done} skip={skip} {el:.0f}s "
                  f"({el / max(done, 1):.2f}s/张)", flush=True)

    with open(os.path.join(args.out, "manifest.jsonl"), "w", encoding="utf-8") as fh:
        for m in manifest:
            fh.write(json.dumps(m, ensure_ascii=False) + "\n")
    el = time.perf_counter() - t0
    print(f"[done] 新增 {done} 张 / 跳过 {skip} / {el:.0f}s / "
          f"码流均值 {tot_stream / max(done, 1):.0f} B/帧", flush=True)
    print("RES2_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
