#!/usr/bin/env python3
# iq_tnr_strength_probe.py —— O87 探针：生成「3DNR 强度变体」IQ（**只生成，不安装**）
#
# 用途：确认 3DNR 强度可调后，量化「强度 →（时域噪声抑制, 亮度）」的折中。
#       `bayertnr_v2.TuningPara.Setting[*].Tuning_ISO[13].{lo,hi}_filter_strength` 是主旋钮，
#       强度**随 ISO 分 13 档**且当前生效档无法从 V4L2 读出 ⇒ **把 13 档全改**（两个 Setting、
#       两个 main_scene 一起改），保证无论落在哪档都生效。
#
# 用法（**不需要 root**，只读档库 + 写 /tmp）：
#   python3 iq_tnr_strength_probe.py <strength> <out.json>
# 例：python3 iq_tnr_strength_probe.py 1.2 /tmp/iq_tnr_1p2.json
#
# 安装 / 切档 / 还原由 shell 显式执行（含 sudo），见证据档 §11 的复现命令：
#   sudo cp -a /etc/iqfiles/<name> /tmp/o87_iq_backup_<ts>.json      # 备份（应为 collect 5cc97e06…）
#   sudo cp <out.json> /etc/iqfiles/<name> && sudo systemctl restart rkaiq_3A.service
#   python3 iq_tnr_strength_probe.py --restore /tmp/o87_iq_backup_<ts>.json /etc/iqfiles/<name>  # 见下

import json
import sys

SRC = "/etc/iqfiles_sets/demo/imx415_CMK-OT2022-PX1_IR0147-50IRC-8M-F20.json"
TARGET = ("lo_filter_strength", "hi_filter_strength")
KEYS = ("Setting",)


def patch(node, strength, log):
    """递归把 Tuning_ISO[*] 的 lo/hi_filter_strength 设为 strength。"""
    if isinstance(node, dict):
        if "Tuning_ISO" in node:
            for e in node["Tuning_ISO"]:
                for k in TARGET:
                    if k in e:
                        log.append((e.get("iso"), k, e[k]))
                        e[k] = strength
        for v in node.values():
            patch(v, strength, log)
    elif isinstance(node, list):
        for v in node:
            patch(v, strength, log)


def main():
    if sys.argv[1] == "--restore":          # 仅打印还原提示（实际 cp 由 shell 做）
        print("还原：sudo cp %s %s && sudo systemctl restart rkaiq_3A.service"
              % (sys.argv[2], sys.argv[3]))
        return
    strength = float(sys.argv[1])
    out = sys.argv[2]
    d = json.load(open(SRC))
    log = []
    patch(d, strength, log)
    json.dump(d, open(out, "w"))
    print("源 = %s（demo 档）" % SRC)
    print("目标 = %s  设定 lo/hi_filter_strength = %s" % (out, strength))
    print("改动条目数 = %d（应为 13 档 × 2 Setting × 2 scene × 2 旋钮 = 104）" % len(log))
    changed = [x for x in log if x[2] != strength]
    print("实际发生变化 = %d" % len(changed))


if __name__ == "__main__":
    main()