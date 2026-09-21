#!/usr/bin/env python3
# iq_diff_collect_demo.py —— 只读：比对 collect / demo 两套 IQ 的全部差异
#
# 目的：O87 的 3DNR A/B 结论若要归因于"3DNR 开关"，必须先排除"两档还有别的差异"这一混淆。
# 复现：ssh board 'python3 -' < docs/evidence/reports/O87/iq_diff_collect_demo.py
# ⚠️ 路径 `/etc/iqfiles/` 是**当前生效档**，故本脚本固定读 `/etc/iqfiles_sets/{collect,demo}/`（档库存放处）。

import json

BASE = "/etc/iqfiles_sets"
NAME = "imx415_CMK-OT2022-PX1_IR0147-50IRC-8M-F20.json"

a = json.load(open("%s/collect/%s" % (BASE, NAME)))
b = json.load(open("%s/demo/%s" % (BASE, NAME)))

diff = []


def walk(x, y, p=""):
    if type(x) is not type(y):
        diff.append((p, x, y))
        return
    if isinstance(x, dict):
        for k in set(x) | set(y):
            if k not in x or k not in y:
                diff.append((p + "/" + k, x.get(k, "<缺>"), y.get(k, "<缺>")))
            else:
                walk(x[k], y[k], p + "/" + k)
    elif isinstance(x, list):
        if len(x) != len(y):
            diff.append((p + "(len)", len(x), len(y)))
        else:
            for i, (u, v) in enumerate(zip(x, y)):
                walk(u, v, p + "[%d]" % i)
    elif x != y:
        diff.append((p, x, y))


walk(a, b)
print("# O87 collect vs demo IQ 差异比对（只读）")
print("# 路径: %s/{collect,demo}/%s" % (BASE, NAME))
print("差异条数: %d" % len(diff))
for p, u, v in diff:
    print("  %-72s collect=%s  demo=%s" % (p[-72:], str(u)[:28], str(v)[:28]))
if len(diff) == 2 and all("bayertnr_v2" in p for p, _, _ in diff):
    print("判定: 两档差异**仅为 3DNR 使能**（bayertnr_v2/enable 0→1，两个 scene 各一处）")
    print("      ⇒ A/B 的观测差可归因于 3DNR 开关本身，无其它混淆项")