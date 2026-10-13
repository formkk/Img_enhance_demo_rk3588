#!/usr/bin/env python3
"""ui_buttons.py —— MIPI 按钮区 2 键的命中判定（§3.4 / §13「交互」行）

布局（§3.4）：按钮区 1024×96（Y 504–599），1 行 2 键各 512 px：
    ① 触发HQ ② **退出**（**v3.86 就地裁定 O98：短按软复位自演示 UI 移除** ⇒
    该键只剩**长按 ≥ `ui.reset_hold_ms` 退出进程**；软复位机制保留于退出序 `shutdown()`；
    **v3.86 裁定 O101：取消「存图」键** ⇒ 原三键各 341 px 改为两键各 512 px）

纪律：
  · **几何唯一来源 = `curve_estim.resolve_geometry(cfg)`**（**v3.86 就地裁定 O58**：原
    `BUTTON_Y` / `BUTTON_H` / `BUTTON_W` / `LAYOUT` 硬编码**已删**；位置与宽度改取自
    `ui.button_layout`，按钮区起点取自几何单源的 `button_y`）；
  · 命中区下限由 `ui.hit_area_min_px` 给出（§13「交互」行：≥67 px ≈ 10 mm @170 PPI）；
  · 长按阈值由 `ui.reset_hold_ms` 给出（v3.14 裁定 O6 = 3000 ms）；
  · 本模块只做**命中判定与按键语义**，不含 evdev 读取（在 `pointer.py`）。

**键名保持 `reset_exit`**（§10.4 键表零改动）；仅其**语义**由"短按复位 / 长按退出"改为"长按退出"（O98）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import curve_estim as curve_mod

# §3.4 两键名（与 `ui.button_layout.keys[].name` 逐字一致；**v3.86 裁定 O101 取消 `save`**）
KEY_TRIGGER_HQ = "trigger_hq"
# **[DCM 演示路]（O166；v3.87 追加 33）** 第三视图态触发键：按它 ⇒ 跑 HQ 拿增强帧
# （DCM 的输入就是 HQ 输出）⇒ 进 DCM 编解码 + 特征域检测 ⇒ 全屏显示「重建图 + 检测框」。
# **不贴 HQ 图**（§8.23 裁定：只展示单臂 B）。
KEY_DCM_DEMO = "dcm_demo"
KEY_RESET_EXIT = "reset_exit"

# 按键语义（§10.6-C；**v3.86 裁定 O98 后只剩长按**，`press` 字段值域仍按 `docs/logging.md` §4 = short|long）
PRESS_LONG = "long"


class ButtonHit:
    """把 (x, y) 触点映射到键名。命中区 = 按钮矩形向外扩张到下限边长后居中（§3.4）。"""

    def __init__(self, cfg: Any):
        g = curve_mod.resolve_geometry(cfg)
        self.min_px = int(cfg.require_final("ui.hit_area_min_px"))
        self.hold_ms = int(cfg.require_final("ui.reset_hold_ms"))
        self.btn_y, self.btn_h = int(g["button_y"]), int(g["button_h"])
        self.btn_keys = tuple(g["button_keys"])
        names = [k[0] for k in self.btn_keys]
        for want in (KEY_TRIGGER_HQ, KEY_RESET_EXIT):
            if want not in names:
                raise ValueError(f"ui.button_layout 缺键 {want!r}（§3.4 定稿两键）：{names}")
        self.rects: Dict[str, Tuple[int, int, int, int]] = {}
        for name, bx, bw in self.btn_keys:
            cx = bx + bw // 2
            cy = self.btn_y + self.btn_h // 2
            hw = max(bw, self.min_px) // 2
            hh = max(self.btn_h, self.min_px) // 2
            self.rects[name] = (cx - hw, cy - hh, hw * 2, hh * 2)

    def hit(self, x: int, y: int) -> Optional[str]:
        for name, (rx, ry, rw, rh) in self.rects.items():
            if rx <= x < rx + rw and ry <= y < ry + rh:
                return name
        return None

    def areas(self) -> Dict[str, Tuple[int, int, int, int]]:
        return dict(self.rects)


def main() -> int:
    import config as starlight_config

    cfg = starlight_config.load()
    h = ButtonHit(cfg)
    print(f"命中区下限 = {h.min_px} px；长按阈值 = {h.hold_ms} ms（裁定 O98：仅长按退出）")
    print(f"按钮区 Y {h.btn_y}..{h.btn_y + h.btn_h - 1}")
    for name, rect in h.areas().items():
        print(f"  {name:12s} rect={rect}（{rect[2]}×{rect[3]}）")
    for name, bx, bw in h.btn_keys:
        cx, cy = bx + bw // 2, h.btn_y + h.btn_h // 2
        assert h.hit(cx, cy) == name, f"{name} 中心未命中"
    print("两键中心命中自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())