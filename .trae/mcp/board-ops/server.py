#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board-ops —— RK3588 演示板只读探测 MCP server（骨架，v0.1.0）

地位：本项目工程工具，**非运行期代码**（与 .trae/rules、.trae/skills 同级定位）。
效力：本 server 只做**只读探测**；其纪律来源为主基准与工程规则，冲突时以主基准为准：
  - 主基准 §2（硬件平台）、§3.1 / §3.2（采集与显示链路）、§6.5（计量口径）、
    §10.4（config 键表）、§10.5（启动自检 9 项）、§15（O21 存储介质登记）
  - .trae/rules/30-board-safety.md §4（默认只读探测；有副作用操作须先说明影响面与恢复步骤并取得确认）
  - .trae/rules/20-contracts.md §1（键名/节点名一律以主基准为准，不得在工具里写死猜测值）

设计约束（三条硬约束，勿破坏）：
  1. **不提供任意命令执行**：本 server 不暴露 shell 透传口。需要新的探测能力时，
     **新增一个固定工具**（改本文件 + 回写主基准 §10.3/§14），不得开放万能入口。
  2. **不写死环境取值**：video 节点、rkisp 节点、connector / CRTC、介质型号一律**运行时枚举**后返回；
     本文件不得出现任何具体节点编号或 crtc id（主基准 §10.4「禁止写死 crtc id」）。
  3. **不猜连接参数**：SSH 地址/用户必须由环境提供；未配置时工具返回明确指引，**不设默认主机**。

注册方式（IDE 侧，yaml/json 由 IDE 管理，本仓库只提供被启动的进程）：
  stdio server：command = python3
                args    = [ "<本文件绝对路径>" ]
  环境变量（缺 HOST/USER 时所有探测工具返回配置指引，server 本身仍可启动）：
    BOARD_SSH_HOST   板 IP 或主机名（必填才启用探测）
    BOARD_SSH_USER   SSH 用户（必填才启用探测）
    BOARD_SSH_PORT   端口，默认 22
    BOARD_SSH_KEY    私钥路径，可选（等价 ssh -i）
    BOARD_SSH_TIMEOUT 单条命令超时秒数，默认 20

自测：
  printf '%s\\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python3 server.py
"""

import json
import os
import shlex
import subprocess
import sys

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "board-ops"
SERVER_VERSION = "0.1.0"

SSH_HOST = os.environ.get("BOARD_SSH_HOST", "").strip()
SSH_USER = os.environ.get("BOARD_SSH_USER", "").strip()
SSH_PORT = os.environ.get("BOARD_SSH_PORT", "22").strip() or "22"
SSH_KEY = os.environ.get("BOARD_SSH_KEY", "").strip()
try:
    SSH_TIMEOUT = int(os.environ.get("BOARD_SSH_TIMEOUT", "20"))
except ValueError:
    SSH_TIMEOUT = 20

NOT_CONFIGURED_HINT = (
    "板未配置：请设置环境变量 BOARD_SSH_HOST 与 BOARD_SSH_USER 后重启本 MCP server"
    "（可选 BOARD_SSH_PORT / BOARD_SSH_KEY / BOARD_SSH_TIMEOUT）。"
    "未配置前不执行任何远程命令——不猜默认主机（本文件设计约束第 3 条）。"
)


class BoardNotConfigured(Exception):
    """连接参数缺失。"""


def _ssh_argv(remote_cmd: str):
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new", "-p", SSH_PORT]
    if SSH_KEY:
        argv += ["-i", SSH_KEY]
    argv += [f"{SSH_USER}@{SSH_HOST}", remote_cmd]
    return argv


def ssh_read(remote_cmd: str) -> str:
    """在板上执行**只读**命令并返回 stdout（含 stderr 摘要）。"""
    if not SSH_HOST or not SSH_USER:
        raise BoardNotConfigured(NOT_CONFIGURED_HINT)
    try:
        proc = subprocess.run(
            _ssh_argv(remote_cmd),
            capture_output=True, text=True, timeout=SSH_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return f"[E_TIMEOUT] 命令超时（{SSH_TIMEOUT}s）：{remote_cmd}"
    except FileNotFoundError:
        return "[E_IO] 本机未找到 ssh 可执行文件"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        head = f"[exit={proc.returncode}]"
        parts = [p for p in (out, err) if p]
        return f"{head} " + ("\n".join(parts) if parts else "(无输出)")
    if not out:
        return "(无输出)"
    if err:
        out += f"\n[stderr] {err}"
    return out


def _safe_path(path: str) -> str:
    """校验路径参数，返回可直接 shlex.quote 的安全值。"""
    if not path or not path.startswith("/"):
        raise ValueError("path 必须是以 / 开头的绝对路径")
    if ".." in path or "\x00" in path or "\n" in path:
        raise ValueError("path 不得包含 '..' 或控制字符")
    if len(path) > 512:
        raise ValueError("path 过长")
    return path


# ---------------------------------------------------------------- 工具实现

def t_board_info(_args: dict) -> str:
    cmd = (
        "echo \"model: $(cat /proc/device-tree/model 2>/dev/null | tr -d '\\0')\"; "
        "echo \"kernel: $(uname -r)\"; echo \"arch: $(uname -m)\"; "
        "echo '--- os-release ---'; cat /etc/os-release 2>/dev/null; "
        "echo '--- cpu ---'; nproc; "
        # aarch64 的 /proc/cpuinfo 通常没有 Hardware/model name 行，grep 无匹配会返回 1，
        # 使整条只读命令被标成 [exit=1]（误报失败）——故补 fallback 兜底
        "cat /proc/cpuinfo 2>/dev/null | grep -m2 -E 'Hardware|Revision|model name' "
        "|| echo '(cpuinfo 无 Hardware/Revision/model name 行)'"
    )
    return ssh_read(cmd)


def t_list_video_nodes(_args: dict) -> str:
    cmd = (
        "echo '--- /dev/video* ---'; ls -l /dev/video* 2>/dev/null || echo '(无)'; "
        "echo '--- /dev/v4l-subdev* ---'; ls -1 /dev/v4l-subdev* 2>/dev/null || echo '(无)'; "
        "echo '--- /dev/media* ---'; ls -1 /dev/media* 2>/dev/null || echo '(无)'; "
        "echo '--- media 拓扑 ---'; "
        "for m in /dev/media*; do [ -e \"$m\" ] && echo \"[$m]\" && media-ctl -d \"$m\" -p 2>/dev/null; done"
    )
    return ssh_read(cmd)


def t_probe_rknn_stack(_args: dict) -> str:
    # 版本纪律（厂家资料核实）：toolkit2 与 librknnrt 必须同版本号；
    # rknpu 内核驱动只需 ≥ 下限（Rockchip RKNPU User Guide V2.3.0：建议 >= 0.9.2）。
    cmd = (
        "echo '--- librknnrt（板端 runtime，须与 PC 端 rknn-toolkit2 同版）---'; "
        "ls -l /usr/lib/librknnrt.so /usr/lib/aarch64-linux-gnu/librknnrt.so /vendor/lib64/librknnrt.so 2>/dev/null; "
        "strings /usr/lib/librknnrt.so 2>/dev/null | grep -i 'librknnrt version' | head -2; "
        "echo '--- rknpu 内核驱动版本 ---'; "
        "cat /sys/kernel/debug/rknpu/driver_version 2>/dev/null "
        "|| cat /proc/debug/rknpu/driver_version 2>/dev/null "
        "|| dmesg 2>/dev/null | grep -i rknpu | head -3 "
        "|| echo '(不可读：第三方固件可能未装 rknpu 驱动)'; "
        "echo '--- 板端 python（决定 rknn_toolkit_lite2 wheel 与 pybind11 扩展 ABI）---'; "
        "python3 -V 2>&1; "
        "python3 -c \"import rknnlite, os; print('rknnlite:', os.path.dirname(rknnlite.__file__))\" 2>/dev/null "
        "|| echo 'rknnlite(板端 Python NPU 接口): 未安装'; "
        "python3 -c \"import rknnlite; from rknnlite.api import RKNNLite; print('RKNNLite: ok')\" 2>/dev/null "
        "|| echo 'RKNNLite: 不可用'; "
        "echo '--- rknn_server（仅 PC 联板调试需要，本项目非必需）---'; "
        "ls -l /usr/bin/rknn_server /usr/bin/restart_rknn.sh 2>/dev/null || echo '(未安装)'; "
        "echo '--- librga ---'; ls -l /usr/lib/librga.so /usr/lib/aarch64-linux-gnu/librga.so 2>/dev/null; "
        "echo '--- NPU 负载 ---'; cat /sys/kernel/debug/rknpu/load 2>/dev/null || echo '(不可读)'"
    )
    return ssh_read(cmd)


def t_probe_drm(_args: dict) -> str:
    # 运行时枚举 connector / CRTC，不写死任何 id（主基准 §10.4）
    cmd = (
        "echo '--- /dev/dri ---'; ls -l /dev/dri 2>/dev/null || echo '(无)'; "
        "echo '--- connector 状态 ---'; "
        "for c in /sys/class/drm/card*-*; do "
        "[ -e \"$c/status\" ] && echo \"$(basename $c): status=$(cat $c/status) "
        "enabled=$(cat $c/enabled 2>/dev/null) modes=$(cat $c/modes 2>/dev/null | tr '\\n' ' ')\"; "
        "done; "
        "echo '--- CRTC 计数 ---'; ls -1 /sys/class/drm/card*-* 2>/dev/null | wc -l; "
        "echo '--- 已占用 ---'; "
        "for s in /sys/kernel/debug/dri/*/state /sys/kernel/debug/dri/*/crtc-*; do "
        "[ -e \"$s\" ] && echo \"$s\"; done 2>/dev/null | head -20"
    )
    return ssh_read(cmd)


def t_probe_storage(_args: dict) -> str:
    # 服务 O21（存储介质与容量上限登记，登记表见 docs/measurements.md §2）
    cmd = (
        "echo '--- lsblk ---'; lsblk -o NAME,SIZE,TYPE,MOUNTPOINT 2>/dev/null; "
        "echo '--- 设备型号 ---'; "
        "for b in /sys/block/*/device; do "
        "[ -d \"$b\" ] && echo \"$(basename $(dirname $b)): model=$(cat $b/model 2>/dev/null) "
        "rev=$(cat $b/rev 2>/dev/null)\"; done; "
        "echo '--- 挂载可用空间 ---'; df -h 2>/dev/null | head -20"
    )
    return ssh_read(cmd)


def t_list_board_dir(args: dict) -> str:
    path = _safe_path(str(args.get("path", "")))
    return ssh_read(f"ls -la -- {shlex.quote(path)} 2>&1 | head -100")


def t_read_board_file(args: dict) -> str:
    path = _safe_path(str(args.get("path", "")))
    try:
        max_bytes = int(args.get("max_bytes", 65536))
    except (TypeError, ValueError):
        max_bytes = 65536
    max_bytes = max(256, min(max_bytes, 1048576))
    return ssh_read(f"head -c {max_bytes} -- {shlex.quote(path)} 2>&1")


def t_read_board_logs(args: dict) -> str:
    """读取演示日志尾（只读）。日志规格见 docs/logging.md；指标不在此（见主基准 §6.5）。"""
    log_dir = str(args.get("dir") or "/var/log/starlight")
    log_dir = _safe_path(log_dir)
    try:
        lines = int(args.get("lines", 200))
    except (TypeError, ValueError):
        lines = 200
    lines = max(10, min(lines, 5000))
    service = str(args.get("service") or "starlight-demo")
    if not service.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ValueError("service 名不合法")
    cmd = (
        f"echo '--- journalctl -u {service}（尾 {lines} 行） ---'; "
        f"journalctl -u {service} -n {lines} --no-pager 2>&1 | tail -{lines}; "
        f"echo '--- {log_dir} 文件 ---'; ls -la {shlex.quote(log_dir)} 2>&1 | head -20"
    )
    return ssh_read(cmd)


TOOLS = [
    {
        "name": "board_info",
        "description": "读取板型/内核/架构/OS/CPU 只读信息。用于 M0-1 硬件基线登记（主基准 §11.1-1）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_video_nodes",
        "description": "枚举 /dev/video*、/dev/v4l-subdev*、/dev/media* 及 media 拓扑。节点编号运行时返回，不预设（主基准 §10.4 采集域）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "probe_rknn_stack",
        "description": "读取 NPU 软件栈版本三元组：librknnrt / rknpu 驱动 / rknn-toolkit2，另附 librga 与 NPU 负载。用于 §10.5-1 强制作业与 R7/R11 核对。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "probe_drm",
        "description": "枚举 DRM connector/CRTC/plane 与 EDID 模式列表（运行时解析，禁止写死 crtc id）。用于 M0-9 双屏打通与 §10.5-4/5。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "probe_storage",
        "description": "读取存储介质类型/型号/可用容量与挂载空间。服务 O21 登记（docs/measurements.md §2）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_board_dir",
        "description": "列出板上目录内容（只读）。用于查看 models/、日志目录、IQ 文件目录等。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "板上绝对路径"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_board_file",
        "description": "读取板上文本文件前 N 字节（只读）。用于回读 config.yaml、IQ 指纹、model 清单等。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "板上绝对路径"},
                "max_bytes": {"type": "integer", "description": "读取上限，默认 65536，范围 256~1048576"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_board_logs",
        "description": "读取演示服务日志尾与日志目录清单（只读）。日志规格见 docs/logging.md；指标一律走 bench.py 报告，不在日志（主基准 §6.5）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "systemd 单元名，默认 starlight-demo"},
                "lines": {"type": "integer", "description": "尾部行数，默认 200，范围 10~5000"},
                "dir": {"type": "string", "description": "日志目录，默认 /var/log/starlight"},
            },
            "additionalProperties": False,
        },
    },
]

DISPATCH = {
    "board_info": t_board_info,
    "list_video_nodes": t_list_video_nodes,
    "probe_rknn_stack": t_probe_rknn_stack,
    "probe_drm": t_probe_drm,
    "probe_storage": t_probe_storage,
    "list_board_dir": t_list_board_dir,
    "read_board_file": t_read_board_file,
    "read_board_logs": t_read_board_logs,
}


# ---------------------------------------------------------------- MCP 协议

def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _text(rid, text, is_error=False):
    return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": is_error})


def handle(req: dict):
    method = req.get("method")
    rid = req.get("id")
    if rid is None:  # notification（如 notifications/initialized）——无需响应
        return None

    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        return _ok(rid, {"tools": TOOLS})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = DISPATCH.get(name)
        if fn is None:
            return _err(rid, -32602, f"未知工具：{name}")
        try:
            return _text(rid, fn(args))
        except BoardNotConfigured as exc:
            return _text(rid, str(exc), is_error=True)
        except ValueError as exc:
            return _text(rid, f"参数错误：{exc}", is_error=True)
        except Exception as exc:  # 探测工具不应因单点异常中断会话
            return _text(rid, f"[E_IO] {type(exc).__name__}: {exc}", is_error=True)
    return _err(rid, -32601, f"不支持的方法：{method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())