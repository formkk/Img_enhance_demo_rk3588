#!/usr/bin/env python3
"""V5：码率 ↔ 检出的平衡点测量（本机；数据来自板端 V4 扫描）。

口径（必须随行标注）：
  · 码率 = **板端**（FP16 移植版）真码流字节数 / bpp（来自 `V4` 的 sweep_report.json）
  · 检出 = 本机 `onnxruntime` 跑同一个检测器 ONNX（与板上 `rknn` 已实测一致：3 框全配）
  · **参考** = 在**原始增强帧**（未经 DCM）上的检出 ⇒ N_ref（本图 3 框）
  · **保有率** = 各档重建图上仍能配上（同类 + IoU≥0.5）的参考框数 / N_ref
  · **虚警** = 未匹配上任何参考框的检出框数
  ⚠️ 这张图**没有人工标注** ⇒ "检出率"只能是"相对原始增强帧的保有率"，不是绝对准确率。

产出 DTM/out_v1/v5/：
  table.json    逐档数据
  curve.png     码率（对数轴）vs 框数 的平衡曲线

用法：~/anaconda3/envs/dtm/bin/python DTM/code/v5_measure_rate_vs_detection.py
"""

import importlib.util
import json
import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

PROJ = "/home/jenkolux/PKU_WH/Img_enhance_demo"
CODE = os.path.join(PROJ, "DTM", "code")
ONNX = os.path.join(PROJ, "DTM", "assets", "best_ckpt_1_voc20_bayer4ch_320.onnx")
FRAME = os.path.join(PROJ, "hq_optimize/captures/noise_diag/hq_final_1.png")
SWEEP = os.path.join(PROJ, "DTM", "out_v1", "sweep")
OUT = os.path.join(PROJ, "DTM", "out_v1", "v5")
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
CONFS = (0.20, 0.30)

spec = importlib.util.spec_from_file_location("d3", os.path.join(CODE, "d3_prep_detect_input.py"))
d3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d3)


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, x2 - x1), max(0.0, y2 - y1)
    inter = iw * ih
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / (ua + 1e-9)


def main():
    import onnxruntime as ort
    os.makedirs(OUT, exist_ok=True)
    sess = ort.InferenceSession(ONNX, providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0].name

    def detect(path):
        bgr = cv2.imread(path, cv2.IMREAD_COLOR)
        net = d3.process_image(bgr)
        ratio, pad = d3.letterbox(bgr, d3.IMG_SIZE, stride=d3.STRIDE)[1:]
        out = np.asarray(sess.run(None, {inp: net[None].astype(np.float32)})[0])[0].astype(np.float32)
        res = {}
        for c in CONFS:
            res[c] = d3.decode_nms(out, ratio, pad, bgr.shape[:2], conf_thres=c)
        return res

    ref_all = detect(FRAME)
    print("参考（原始增强帧）:", {c: [(b["name"], round(b["score"], 3)) for b in ref_all[c]] for c in CONFS})
    imr = cv2.imread(FRAME, cv2.IMREAD_COLOR)
    for b in ref_all[0.2]:
        x1, y1, x2, y2 = [int(v) for v in b["xyxy"]]
        cv2.rectangle(imr, (x1, y1), (x2, y2), (60, 180, 60), 2)
        cv2.putText(imr, f"{b['name']} {b['score']:.2f}", (x1, max(18, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 180, 60), 1, cv2.LINE_AA)
    cv2.putText(imr, "REF: 原始增强帧（未过 DCM）", (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                (255, 255, 255), 2, cv2.LINE_AA)
    cv2.imwrite(os.path.join(OUT, "overlay_ref.png"), imr)

    rep = json.load(open(os.path.join(SWEEP, "sweep_report.json")))
    rows = []
    SEL = {0, 3, 8, 16, 31}                     # 存几张叠框对照图
    for r in rep["rows"]:
        q = r["q"]
        p_img = os.path.join(SWEEP, f"q{q:02d}.png")
        det = detect(p_img)
        if q in SEL:
            im = cv2.imread(p_img, cv2.IMREAD_COLOR)
            for b in ref_all[0.2]:              # 绿 = 原始增强帧上的参考框
                x1, y1, x2, y2 = [int(v) for v in b["xyxy"]]
                cv2.rectangle(im, (x1, y1), (x2, y2), (60, 180, 60), 2)
                cv2.putText(im, f"REF {b['name']} {b['score']:.2f}", (x1, max(18, y1 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 180, 60), 1, cv2.LINE_AA)
            for b in det[0.2]:                  # 红 = 重建图上的检出
                x1, y1, x2, y2 = [int(v) for v in b["xyxy"]]
                cv2.rectangle(im, (x1, y1), (x2, y2), (60, 60, 230), 2)
                cv2.putText(im, f"{b['name']} {b['score']:.2f}", (x1, min(im.shape[0] - 8, y2 + 18)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 60, 230), 1, cv2.LINE_AA)
            cv2.putText(im, f"q={q}  {r['total_bytes']}B  {r['bpp_960x544']:.4f}bpp",
                        (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.imwrite(os.path.join(OUT, f"overlay_q{q:02d}.png"), im)
        row = {"q": q, "q_factor": r["q_factor"], "total_bytes": r["total_bytes"],
               "bpp": r["bpp_960x544"]}
        for c in CONFS:
            ref, boxes = ref_all[c], det[c]
            kept = 0
            for rf in ref:
                if any(b["cls"] == rf["cls"] and iou(rf["xyxy"], b["xyxy"]) >= 0.5 for b in boxes):
                    kept += 1
            fp = sum(1 for b in boxes
                     if not any(rf["cls"] == b["cls"] and iou(rf["xyxy"], b["xyxy"]) >= 0.5 for rf in ref))
            row[f"n_ref_{c}"] = len(ref)
            row[f"kept_{c}"] = kept
            row[f"keep_rate_{c}"] = kept / max(len(ref), 1)
            row[f"fp_{c}"] = fp
            row[f"nbox_{c}"] = len(boxes)
            row[f"best_{c}"] = round(max([b["score"] for b in boxes], default=0.0), 4)
        rows.append(row)
        print(f"q={q:2d} {row['total_bytes']:6d}B {row['bpp']:.4f}bpp |"
              f" @0.2 保有 {row['kept_0.2']}/{row['n_ref_0.2']} 虚警 {row['fp_0.2']} 最高分 {row['best_0.2']:.3f}"
              f" | @0.3 保有 {row['kept_0.3']}/{row['n_ref_0.3']} 虚警 {row['fp_0.3']} 最高分 {row['best_0.3']:.3f}")

    json.dump({"frame": "hq_final_1", "confs": list(CONFS),
               "ref": {str(c): [(b["name"], round(b["score"], 3), [round(v, 1) for v in b["xyxy"]])
                                for b in ref_all[c]] for c in CONFS},
               "rows": rows}, open(os.path.join(OUT, "table.json"), "w"), indent=1, ensure_ascii=False)

    # 曲线：x = bpp（对数），左 y = 框数
    W, H, M = 900, 560, 70
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    f_t, f_b, f_s = ImageFont.truetype(FONT, 22), ImageFont.truetype(FONT, 15), ImageFont.truetype(FONT, 13)
    d.text((M, 20), "码率 ↔ 检出 平衡曲线（板端码率 / 本机检出，参考 = 原始增强帧）", font=f_t, fill=(30, 32, 36))
    x0, x1, y0, y1 = M, W - 40, 90, H - 70
    bpps = [r["bpp"] for r in rows]
    lo, hi = np.log10(min(bpps)), np.log10(max(bpps))
    maxy = max(max(r["nbox_0.3"] for r in rows), max(r["kept_0.3"] for r in rows), 3) + 1
    d.line([x0, y1, x1, y1], fill=(120, 124, 130))
    d.line([x0, y0, x0, y1], fill=(120, 124, 130))
    for g in range(0, maxy + 1, max(1, maxy // 5)):
        y = y1 - (y1 - y0) * g / maxy
        d.line([x0, y, x1, y], fill=(232, 234, 236))
        d.text((x0 - 34, y - 8), str(g), font=f_s, fill=(120, 124, 130))
    for g in range(0, 32, 8):
        b = rows[g]["bpp"]
        x = x0 + (x1 - x0) * (np.log10(b) - lo) / max(hi - lo, 1e-9)
        d.line([x, y0, x, y1], fill=(240, 241, 243))
        d.text((x - 18, y1 + 6), f"q{g}", font=f_s, fill=(120, 124, 130))
    d.text((x0, y1 + 26), "横轴 = 码率（bpp，对数；标注为档位 q）　纵轴 = 框数", font=f_s, fill=(120, 124, 130))

    nref = rows[0]["n_ref_0.2"]
    yd = y1 - (y1 - y0) * nref / maxy
    for xs in range(x0, x1, 14):
        d.line([xs, yd, min(xs + 7, x1), yd], fill=(22, 163, 74), width=2)
    d.text((x0 + 8, yd - 20), f"参考（原始增强帧）框数 = {nref}", font=f_b, fill=(22, 163, 74))

    def plot(key, color, label, dy):
        pts = [(x0 + (x1 - x0) * (np.log10(r["bpp"]) - lo) / max(hi - lo, 1e-9),
                y1 - (y1 - y0) * r[key] / maxy) for r in rows]
        d.line([(p[0], p[1]) for p in pts], fill=color, width=2)
        for p in pts:
            d.ellipse([p[0] - 2, p[1] - 2, p[0] + 2, p[1] + 2], fill=color)
        d.line([x1 - 210, y0 + dy, x1 - 186, y0 + dy], fill=color, width=3)
        d.text((x1 - 178, y0 + dy - 8), label, font=f_b, fill=(60, 64, 70))

    plot("kept_0.2", (22, 163, 74), "保有的参考框（@conf0.2）", 0)
    plot("fp_0.2", (220, 60, 60), "虚警框（@conf0.2）", 26)
    plot("nbox_0.2", (110, 116, 122), "总框数（@conf0.2）", 52)
    im.save(os.path.join(OUT, "curve.png"))
    print("V5_DONE", OUT)


if __name__ == "__main__":
    main()
