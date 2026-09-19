#!/usr/bin/env python3
"""ui_buttons.py —— MIPI 按钮区 3 键的命中判定（§3.4 / §13「交互」行）

布局（§3.4）：按钮区 1024×96（Y 504–599），1 行 3 键各 341 px：
    ① 触发HQ ② 存图 ③ 复位·退出（**短按 = 软复位**、**长按 ≥3 s = 退出进程**，§10.6-C）

纪律：
  · 命中区下限由 `ui.hit_area_min_px` 给出（§13「交互」行：≥67 px ≈ 10 mm @170 PPI）；
  · 复位键的**长按阈值**由 `ui.reset_hold_ms` 给出（v3.14 裁定 O6 = 3000 ms）；
  · 本模块只做**命中判定与按键语义**，不含 evdev 读取（在 `capture.TouchReader`）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# §3.4 定稿布局
BUTTON_Y = 504
BUTTON_H = 96
BUTTON_W = 341

KEY_TRIGGER_HQ = "trigger_hq"
KEY_SAVE = "save"
KEY_RESET_EXIT = "reset_exit"

LAYOUT: Tuple[Tuple[str, int], ...] = (
    (KEY_TRIGGER_HQ, 0),
    (KEY_SAVE, BUTTON_W),
    (KEY_RESET_EXIT, BUTTON_W * 2),
)

# 按键语义（§13「交互」行 / §10.6-C）
PRESS_SHORT = "short"
PRESS_LONG = "long"


class ButtonHit:
    """把 (x, y) 触点映射到键名。命中区 = 按钮矩形向外扩张到下限边长后居中。"""

    def __init__(self, cfg: Any):
        self.min_px = int(cfg.require_final("ui.hit_area_min_px"))
        self.hold_ms = int(cfg.require_final("ui.reset_hold_ms"))
        self.rects: Dict[str, Tuple[int, int, int, int]] = {}
        for name, bx in LAYOUT:
            cx = bx + BUTTON_W // 2
            cy = BUTTON_Y + BUTTON_H // 2
            hw = max(BUTTON_W, self.min_px) // 2
            hh = max(BUTTON_H, self.min_px) // 2
            self.rects[name] = (cx - hw, cy - hh, hw * 2, hh * 2)

    def hit(self, x: int, y: int) -> Optional[str]:
        for name, (rx, ry, rw, rh) in self.rects.items():
            if rx <= x < rx + rw and ry <= y < ry + rh:
                return name
        return None

    def areas(self) -> Dict[str, Tuple[int, int, int, int]]:
        return dict(self.rects)

    def classify_reset(self, press_ms: int) -> str:
        """复位键短按/长按判定（§10.6-C：短按软复位、长按 ≥ `ui.reset_hold_ms` 退出）。"""
        return PRESS_LONG if press_ms >= self.hold_ms else PRESS_SHORT


def main() -> int:
    import config as starlight_config

    cfg = starlight_config.load()
    h = ButtonHit(cfg)
    print(f"命中区下限 = {h.min_px} px；复位长按阈值 = {h.hold_ms} ms")
    for name, rect in h.areas().items():
        print(f"  {name:12s} rect={rect}（{rect[2]}×{rect[3]}）")
    # 三键中心自检
    for name, bx in LAYOUT:
        cx, cy = bx + BUTTON_W // 2, BUTTON_Y + BUTTON_H // 2
        assert h.hit(cx, cy) == name, f"{name} 中心未命中"
    print("三键中心命中自检通过")
    print(f"复位键 1000ms -> {h.classify_reset(1000)}；3000ms -> {h.classify_reset(3000)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
