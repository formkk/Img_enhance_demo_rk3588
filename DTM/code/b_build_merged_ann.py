#!/usr/bin/env python3
"""b_build_merged_ann.py —— 合并 COCO train2017 + val2017 标注为一份 json。
用途：本次适配集 = val2017 余量 4500 + train2017 前 5500，两段 GT 分属两个 json，
而 b2c_finetune.py 只读一份 ⇒ 先合并，避免改动已验证的训练脚本。
用法：python3 b_build_merged_ann.py <out.json> <a.json> <b.json>
"""
import json, sys
def main():
    out, parts = sys.argv[1], sys.argv[2:]
    imgs, anns, cats = {}, [], None
    for p in parts:
        a = json.load(open(p, encoding="utf-8"))
        if cats is None:
            cats = a["categories"]
        elif [int(c["id"]) for c in a["categories"]] != [int(c["id"]) for c in cats]:
            raise RuntimeError(p + " categories 不一致")
        imgs.update({int(im["id"]): im for im in a["images"]})
        anns.extend(a["annotations"])
        print("[part] %s: %d 图 / %d 标注" % (p, len(a["images"]), len(a["annotations"])))
    json.dump({"info": {}, "licenses": [], "images": list(imgs.values()),
               "annotations": anns, "categories": cats},
              open(out, "w", encoding="utf-8"))
    print("[merged] %d 图 / %d 标注 => %s" % (len(imgs), len(anns), out))
    return 0
sys.exit(main())
