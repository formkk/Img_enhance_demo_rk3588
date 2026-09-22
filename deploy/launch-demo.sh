#!/bin/sh
# launch-demo.sh —— 桌面图标入口：关闭桌面（释放 DRM master）并让 systemd 拉起演示（§10.6-D）
#
# **（v3.86 裁定 O116）** 只需**一条** `systemctl isolate multi-user.target`：
#   `starlight-demo.service` 是 `WantedBy=multi-user.target` ⇒ isolate 到该 target 时
#   systemd 会**顺带把演示单元拉起来**（不需要第二条 `start`）。这也避免了"启动请求本身
#   属于图形会话、会在会话被终止时一起被杀"的问题。
#
# `--no-block`：提交 job 后立即返回——本脚本所在的桌面会话马上会被 isolate 终止，
# 不等返回才能保证 job 已交给 systemd。
# 免密：仅授权这一条命令，见 `/etc/sudoers.d/starlight`（最小权限）。
set -eu

LOG=/home/cat/starlight_logs/launch.log

if systemctl is-active --quiet starlight-demo.service; then
    echo "$(date -Is) [launch] 演示单元已在运行，忽略本次点击" >>"$LOG" 2>/dev/null || true
    exit 0
fi

echo "$(date -Is) [launch] isolate --no-block multi-user.target（关桌面 → 单元随之拉起）" \
    >>"$LOG" 2>/dev/null || true
exec sudo -n /usr/bin/systemctl isolate --no-block multi-user.target