#!/usr/bin/env python3
"""pointer.py —— 指针输入（TH1 域）：触控 + 鼠标 → 统一的「MIPI 画布域坐标 + 点击边沿」

契约出处：§8.1（触发源**唯一 = 指针点击**）、§3.4（增强窗 ≡ 底栏「触发HQ」键）、
§3.5（全屏态下**内容区任意点击即退出**）、**v3.85 裁定 O81**（指针 = 触控 + 鼠标；
光标坐标进 `ControlState.cursor_pos`；新增键 `ui.pointer_mouse_node` 与事件 `capture/pointer_device`）。

取值（**键表零增删**，规则 `20-contracts.md` §1）：
  · 触控节点 = `TOUCH_NODE_DEFAULT`（板端 evdev 枚举实测 = `event2`「Goodix Capacitive TouchScreen」），
    可用构造参数覆盖（调试用；先例 = `main.py --trigger-at`）；
  · 鼠标节点 = `ui.pointer_mouse_node`（v3.86 落值 = `/dev/input/event7`「TESORO H3E Gaming Mouse」；
    依据 = 板端 `/proc/bus/input/devices` 枚举）。**取值为空或节点打不开 ⇒ 该源停用**，
    不阻塞主链路（规则 `30-board-safety.md` §2 的可降级纪律）。

低层读取**复用** `capture.TouchReader`（evdev 非阻塞 16 B 解析），本模块只做协议解析与坐标统一：
  · 触控：`EV_ABS ABS_X/ABS_Y`（缺则 `ABS_MT_POSITION_X/Y`）+ `EV_KEY BTN_TOUCH` **按下边沿**；
    按 `EVIOCGABS` 读到的范围线性折算到画布域（范围不可得则按 1:1 处理，只记日志不臆造）。
  · 鼠标：`EV_REL REL_X/REL_Y` 累加（初值 = 画布中心）→ clamp 进画布；`EV_KEY BTN_LEFT`
    **按下边沿** = 点击。光标位置每帧由 `cursor()` 交给 TH4 **画进画布**（O81：镜像下两屏都出现）。

纪律：本模块**不做防抖**（`trigger.debounce_ms=50` 由 TH1 触发 FSM 承担）；**不自造事件**。
"""

from __future__ import annotations

import fcntl
import os
import struct
from typing import Any, Dict, List, Optional, Tuple

import capture as cap_mod
import curve_estim as curve_mod
import log as starlog

# 板端 evdev 枚举实测（2026-09-21）：触控 = event2；鼠标 = event7（另有同名 event11 接口）
TOUCH_NODE_DEFAULT = "/dev/input/event2"

# evdev 常量（`linux/input-event-codes.h`；此处只列本模块用到的）
EV_KEY, EV_REL, EV_ABS = 0x01, 0x02, 0x03
REL_X, REL_Y = 0x00, 0x01
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
BTN_LEFT, BTN_TOUCH = 0x110, 0x14A

# `EVIOCGABS(abs)`：_IOC(_IOC_READ, 'E', 0x40+abs, sizeof(struct input_absinfo=6×s32=24))
_ABSINFO_SIZE = 24


def _eviocgabs(abs_code: int) -> int:
    return (2 << 30) | (_ABSINFO_SIZE << 16) | (ord("E") << 8) | (0x40 + abs_code)


class Pointer:
    """触控 + 鼠标两源的统一入口（单线程调用；非阻塞）。"""

    def __init__(self, cfg: Any, logger: starlog.Logger,
                 touch_node: Optional[str] = None, mouse_node: Optional[str] = None):
        g = curve_mod.resolve_geometry(cfg)          # 坐标域 = MIPI 画布（几何单源，§15 O58）
        self.cfg = cfg
        self.log = logger
        self.cw, self.ch = int(g["canvas_w"]), int(g["canvas_h"])
        self.touch = cap_mod.TouchReader(touch_node or TOUCH_NODE_DEFAULT, logger)
        mnode = mouse_node if mouse_node is not None else (cfg.get("ui.pointer_mouse_node") or "")
        self.mouse: Optional[cap_mod.TouchReader] = (
            cap_mod.TouchReader(str(mnode), logger) if mnode else None)
        self.cursor: Optional[Tuple[int, int]] = None
        self.present: Dict[str, bool] = {"touch": False, "mouse": False}
        self._mx, self._my = self.cw // 2, self.ch // 2     # 鼠标初值 = 画布中心（O81）
        self._tx, self._ty = 0, 0
        self._touch_down = False
        self._mouse_down = False
        self._abs = (0, 0, self.cw, self.ch)                # (min_x, min_y, span_x, span_y)

    # ── 生命周期 ───────────────────────────────────────────────────────
    def init(self) -> int:
        """打开两源并各发一次 `capture/pointer_device`（§4 事件清单；`kind/node/present`）。"""
        self.present["touch"] = self.touch.open()
        self.present["mouse"] = bool(self.mouse and self.mouse.open())
        self._abs = self._abs_range()
        self.log.event("capture", "pointer_device", kind="touch", node=self.touch.path,
                       present=self.present["touch"])
        if self.mouse is None:
            self.log.event("capture", "pointer_device", kind="mouse", node="", present=False)
        else:
            self.log.event("capture", "pointer_device", kind="mouse", node=self.mouse.path,
                           present=self.present["mouse"])
        return 0

    def close(self) -> None:
        self.touch.close()
        if self.mouse is not None:
            self.mouse.close()

    def available(self) -> bool:
        return bool(self.present["touch"] or self.present["mouse"])

    # ── 触控绝对范围（`EVIOCGABS`；不可得则按 1:1）───────────────────────
    def _abs_range(self) -> Tuple[int, int, int, int]:
        if self.touch.fd is None:
            return (0, 0, self.cw, self.ch)
        for code_x, code_y in ((ABS_X, ABS_Y), (ABS_MT_POSITION_X, ABS_MT_POSITION_Y)):
            try:
                bx = fcntl.ioctl(self.touch.fd, _eviocgabs(code_x), b"\0" * _ABSINFO_SIZE)
                by = fcntl.ioctl(self.touch.fd, _eviocgabs(code_y), b"\0" * _ABSINFO_SIZE)
            except OSError:
                continue
            min_x, max_x = struct.unpack_from("ii", bx, 4)
            min_y, max_y = struct.unpack_from("ii", by, 4)
            if max_x > min_x and max_y > min_y:
                return (min_x, min_y, max_x - min_x, max_y - min_y)
        # 范围不可得 ⇒ 1:1（不臆造取值，只记事实）
        self.log.event("capture", "pointer_device", kind="touch", node=self.touch.path,
                       present=True)
        return (0, 0, self.cw, self.ch)

    def _map_touch(self) -> Tuple[int, int]:
        min_x, min_y, span_x, span_y = self._abs
        x = int((self._tx - min_x) * (self.cw - 1) / max(1, span_x))
        y = int((self._ty - min_y) * (self.ch - 1) / max(1, span_y))
        return (min(max(x, 0), self.cw - 1), min(max(y, 0), self.ch - 1))

    # ── 轮询 ───────────────────────────────────────────────────────────
    def poll(self) -> List[Dict[str, Any]]:
        """读两源事件；返回**按键边沿**列表 `[{x, y, src, edge}]`（src ∈ touch|mouse，edge ∈ down|up）。

        触控位置事件、鼠标相对位移都会更新 `cursor()`（每帧画进画布，O81）；
        **按下边沿**（`edge="down"`）用于命中判定与触发，**抬起边沿**（`edge="up"`）用于算按压时长
        （§10.6-C 复位键短按/长按）。（防连发由 TH1 的 `trigger.debounce_ms` 承担。）
        **v3.86**：原实现只上报按下边沿 ⇒ 无法区分短按/长按 ⇒ 补 `edge` 字段（两源都补）。
        """
        clicks: List[Dict[str, Any]] = []
        touch_moved = False
        for etype, code, value in self.touch.read_events():
            if etype == EV_ABS and code in (ABS_X, ABS_MT_POSITION_X):
                self._tx = value
                touch_moved = True
            elif etype == EV_ABS and code in (ABS_Y, ABS_MT_POSITION_Y):
                self._ty = value
                touch_moved = True
            elif etype == EV_KEY and code == BTN_TOUCH:
                if value and not self._touch_down:
                    self._touch_down = True
                    x, y = self._map_touch()
                    self.cursor = (x, y)
                    clicks.append({"x": x, "y": y, "src": "touch", "edge": "down"})
                elif not value and self._touch_down:
                    self._touch_down = False
                    x, y = self._map_touch()
                    clicks.append({"x": x, "y": y, "src": "touch", "edge": "up"})
        if touch_moved:
            self.cursor = self._map_touch()

        if self.mouse is not None:
            moved = False
            for etype, code, value in self.mouse.read_events():
                if etype == EV_REL and code == REL_X:
                    self._mx += value
                    moved = True
                elif etype == EV_REL and code == REL_Y:
                    self._my += value
                    moved = True
                elif etype == EV_KEY and code == BTN_LEFT:
                    x = min(max(self._mx, 0), self.cw - 1)
                    y = min(max(self._my, 0), self.ch - 1)
                    if value and not self._mouse_down:
                        self._mouse_down = True
                        self.cursor = (x, y)
                        clicks.append({"x": x, "y": y, "src": "mouse", "edge": "down"})
                    elif not value and self._mouse_down:
                        self._mouse_down = False
                        clicks.append({"x": x, "y": y, "src": "mouse", "edge": "up"})
            if moved:
                self._mx = min(max(self._mx, 0), self.cw - 1)
                self._my = min(max(self._my, 0), self.ch - 1)
                self.cursor = (self._mx, self._my)
        return clicks

    def cursor_pos(self) -> Optional[Tuple[int, int]]:
        """最新指针位置（画布域）；无输入设备或无位移事件时为 None（TH4 据此决定是否画光标）。"""
        return self.cursor


def main() -> int:
    import config as starlight_config

    cfg = starlight_config.load()
    logger = starlog.from_config(cfg)
    p = Pointer(cfg, logger)
    print(f"画布域 = {p.cw}×{p.ch}（几何单源）")
    print(f"触控节点 = {p.touch.path}（默认 {TOUCH_NODE_DEFAULT}）")
    print(f"鼠标节点 = {None if p.mouse is None else p.mouse.path}（键 ui.pointer_mouse_node）")
    p.init()
    print(f"present = {p.present}；绝对范围 = {p._abs}")
    n = 0
    for _ in range(3):
        for c in p.poll():
            print(f"  click {c}")
            n += 1
    print(f"3 次轮询内点击 = {n}（无设备时为 0，属预期）")
    p.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())