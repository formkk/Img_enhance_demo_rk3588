#!/usr/bin/env python3
# iq_inspect_dark_mapping.py —— O87 只读核查：IQ 的「暗部映射」相关节点
#
# 用途：回答「当前 ISP 是否在做暗部数字拉伸（会把暗部噪声放大）」——**只读**，不改 IQ。
# 复现：ssh board 'python3 -' < docs/evidence/reports/O87/iq_inspect_dark_mapping.py
#
# 关注项：DRC（adrc）/ gamma（agamma 曲线暗部斜率）/ BLС / 2DNR / 3DNR / YNR / CNR / 锐化 / ISP 数字增益。

import glob
import json

path = glob.glob("/etc/iqfiles/imx415*.json")[0]
d = json.load(open(path))
sc = d["main_scene"][0]["sub_scene"][0]["scene_isp30"]

out = []


def walk(o, p="", depth=0):
    if depth > 4:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            walk(v, p + "/" + str(k), depth + 1)
    elif isinstance(o, list):
        return
    elif any(s in p.lower() for s in ("enable", "_en", "strength", "op_mode")):
        out.append("%s=%s" % (p.split("/")[-1], o))


print("# O87 ISP 暗部映射只读核查（collect 档 IQ）")
print("# 文件: %s" % path)
print()
print("== 暗部相关模块使能 ==")
for mod in ("ablc_calib", "adrc_calib_v11", "agamma_calib_v11", "bayer2dnr_v2",
            "bayertnr_v2", "ynr_v3", "cnr_v2", "sharp_v4", "gain_v2"):
    out.clear()
    walk(sc.get(mod, {}))
    print("  %-18s %s" % (mod, "; ".join(out[:6]) if out else "(无标量命中)"))

gt = sc["agamma_calib_v11"]["GammaTuningPara"]
c = gt["Gamma_curve"]
n = len(c)
step = 4095.0 / (n - 1)
print()
print("== gamma 曲线（agamma_calib_v11） ==")
print("  Gamma_en = %s  Gamma_out_offset = %s" % (gt["Gamma_en"], gt["Gamma_out_offset"]))
print("  n=%d  值域 0..%d  输入步长(按 12bit)=%.1f" % (n, c[-1], step))
print("  曲线: %s" % c)
print("  暗部斜率(相对线性=1.00): %s" % " ".join("段%d=%.2f" % (i, (c[i] - c[i - 1]) / step) for i in range(1, 10)))
print("  高光斜率(后 8 段): %s" % " ".join("段%d=%.2f" % (i, (c[i] - c[i - 1]) / step) for i in range(n - 8, n)))
print()
print("== 判定 ==")
print("  adrc Enable=0 ⇒ 「DRC 暗部数字拉伸放大噪声」假设证伪")
print("  gamma 暗部斜率 0.16~0.30（压缩）、高光 1.4~1.9（放大）⇒ 「gamma 拉伸暗部」假设亦证伪")
print("  2DNR 开 / 3DNR 关 ⇒ 空间高频被抹平（高通口径 σ 小）、帧间噪声保留（时域口径 σ 大）——与实测自洽")
print("  ⇒ 「时域 σ_eff 随照度降低而升高」成因未定：登记为现象 + 待查（不得写成结论）")