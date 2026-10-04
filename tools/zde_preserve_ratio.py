#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zde_preserve_ratio.py —— 「保比语义」的 PyTorch 可微前向（**仅训练期使用**）

地位：离线训练工具（§10.3 登记）。效力：**仅训练期使用，非交付链路、不作为验收依据**
（规则 `00-ssot.md` §3.5）。**禁任何色调 / 色彩后处理**（规则 `10-evidence.md` §2）——本文件只做
「复刻板端 `apply_core.c` 保比内核语义」的逐点运算，**不引入任何滤镜 / γ / 直方图均衡**。

────────────────────────────────────────────────────────────────────────────
口径出处（唯一权威 = `native/apply_core.c`，逐条给行号；不复刻取值，均为代码事实）
────────────────────────────────────────────────────────────────────────────
1. 曲线场输入取**三通道均值** `vmean`：`vmean = (C0 + C1 + C2) * (1/3)`
   —— NEON `ratio_head4` `native/apply_core.c:183`；标量 `:328`。C = 曲线场在目标像素处的双线性采样值。
2. 亮度 `L` 用 **BT.601 三元式**（`luma_coeffs`）：`kr=0.299 / kb=0.114 / kg=1-kr-kb`
   —— `native/apply_core.c:127-131`（`csc` 非 BT709 即 BT601；本工程 `isp.csc_matrix = "bt601"`）。
   表达次序 = `((kr*R + kg*G) + kb*B) * (1/255)`
   —— NEON `:184-187`；标量 `:330`（R/G/B = 原始通道，0..255）。
3. `L2 = f^N(L, vmean)`：`f(t, v) = t + v*(t*t - t)`（NEON 写作 `L2 = fma(L2, t, vmean)`，
   `t = L2*L2 - L2`）—— NEON `:194-197`（含批间交错版 `:252-257`）；标量 `:332`。
   **迭代次数 N = `g.iters`**（`cf_config_t.iters`，`native/apply_core.h:61`）—— 交付侧取值
   = `realtime_apply.CURVE_ITERS = 8`（`realtime_apply.py:36`，契约值 §3.3 / §4.1）。
   **无任何中间 clamp**（只有末端 `store_group4` 钳位）—— `native/apply_core.c:194-197` / `:332`。
4. `k = (L == 0) ? 0 : L2 / L`（**仅 L==0 退化点置 0，无 ε 阈值**）
   —— NEON `:203-204`；标量 `:338`。**板端用 FRECPE + 2 次 Newton–Raphson 快速倒数**（`:198-202`），
   **本文件默认用精确除法**（差异源①，见下）。
5. 三通道按 `k` **同比缩放**（输出 clip + 8-bit 量化）：`t = (raw/255) * k`，`clip(t, 0, 1)`，
   `o = lrintf(t * 255)` —— `store_group4` `native/apply_core.c:209-224`（NEON）；标量 `:339-343`。
   曲线场**双线性采样**（板端 taps 口径）：`build_taps` `:57-69` + `cf_sample` `:112-121`
   （每目标像素每通道采样一次，坐标归一化 `(i+0.5)*C/N - 0.5`，边界 clamp 到 `[0,C-2]`）。
   曲线场表示：FP16 按 IEEE half、U8 按 `v/255` 转 float32 —— `cf_load` `:100-109`。

────────────────────────────────────────────────────────────────────────────
与板端 `native/apply_core.c` 的**已知差异源**（训练/部署语义错配的来源，逐项列明，不掩盖）
────────────────────────────────────────────────────────────────────────────
① **精确除法 vs FRECPE 快速倒数**：板端 `k = L2 * recip`（`recip` = `vrecpeq_f32` 起步 + 2 次 NR，
   `native/apply_core.c:198-204`）；本文件默认 `k = L2 / L`（精确除法，可微且稳定）。
   板端注释称 NR 两次后相对误差 ≈ IEEE float 的 1 ULP（该文件实测与精确除法相对误差 ≤1.2e-7）。
   ⇒ 本前向可用于训练反传，但**逐位不等价**；对拍实测见 §14 追加 179。
② **双线性插值实现**：本文件**复刻板端 `build_taps` / `cf_sample` 的确切形式**（同一归一化坐标、
   同一边界 clamp、同一 float32 运算）；但 PyTorch 的 `mul`/`add` 与 NEON 的 FMA 融合、以及硬件
   舍入次序可能不同 ⇒ 残余为 float32 级舍入（≤ 1 ULP 量级）。
③ **8-bit 量化时机**：板端在**末端一次性** `clip` 后 `lrintf(t*255)`（round-half-to-even）
   —— `store_group4` `:209-224`。本文件 `quantize=True` 时同样末端量化以对齐板端；`quantize=False`
   （**默认**）时保留 [0,1] 连续值 ⇒ **可微**，供训练反传。
（另有 FMA 融合 vs 分离乘加的 float32 舍入差，并入②，不单列。）

接口（均逐点运算，无额外算子；`curve` 可微 ⇒ 梯度可回传到曲线估计器）：
    apply_preserve_ratio(x_rgb01, curve, *, iters=8, luma_coeffs=LUMA_BT601,
                         fast_recip=False, quantize=False) -> Tensor (..., H, W, 3)
    load_curve_field(raw, precision, ch, cw) -> Tensor (3, ch, cw)   # 与 cf_load 同口径
    luma_from_rgb01(x_rgb01, luma_coeffs) -> Tensor (..., H, W)      # 与 ratio_head4 同口径
张量布局：`x_rgb01` = (..., H, W, 3)（与板端 RGB888 交错缓冲同序）；`curve` = (3, ch, cw)
（与板端 planar 通道平面布局同序，`native/apply_core.c:100-109`）。
"""
from __future__ import annotations

from typing import Sequence, Tuple

import torch

# ── 亮度系数常量（口径 = native/apply_core.c:127-131，kg = 1 - kr - kb）──
LUMA_BT601: Tuple[float, float, float] = (0.299, 1.0 - 0.299 - 0.114, 0.114)
LUMA_BT709: Tuple[float, float, float] = (0.2126, 1.0 - 0.2126 - 0.0722, 0.0722)

# 迭代次数契约值（native/apply_core.h:61 cf_config_t.iters ← realtime_apply.CURVE_ITERS = 8）
CURVE_ITERS = 8


def load_curve_field(raw: bytes, precision: str, ch: int, cw: int) -> torch.Tensor:
    """原始字节曲线场 → float32 张量 (3, ch, cw)。与 `cf_load`（native/apply_core.c:100-109）同口径：
    FP16 = IEEE half、U8 = v/255。布局 = 通道平面（ch 维在前）。"""
    n = ch * cw
    if precision == "fp16":
        import numpy as np

        arr = torch.from_numpy(np.frombuffer(raw, dtype="<f2").astype("float32").copy())
    elif precision == "u8":
        arr = torch.tensor(list(raw), dtype=torch.float32) * (1.0 / 255.0)
    else:
        raise ValueError(f"precision 只允许 'fp16' / 'u8'，实得 {precision!r}")
    if arr.numel() != n * 3:
        raise ValueError(f"曲线场字节数不符：{arr.numel()} ≠ {n * 3}")
    return arr.reshape(3, ch, cw).contiguous()


def luma_from_rgb01(x_rgb01: torch.Tensor, luma_coeffs: Sequence[float]) -> torch.Tensor:
    """亮度 L（口径 = ratio_head4, native/apply_core.c:184-187 / 标量 :330）：
    L = ((kr*R + kg*G) + kb*B)，其中 R/G/B = x_rgb01（已在 [0,1]，等价板端 raw/255）。"""
    kr, kg, kb = luma_coeffs
    r, g, b = x_rgb01[..., 0], x_rgb01[..., 1], x_rgb01[..., 2]
    return (kr * r + kg * g) + kb * b


def build_taps(n: int, c: int, device, dtype=torch.float32):
    """复刻 `build_taps`（native/apply_core.c:57-69）：目标像素中心 (i+0.5)/N 归一化 → 源坐标
    (u*C - 0.5)，`i0` 夹到 [0, C-2]、权重夹到 [0,1]。返回 (i0: LongTensor, w: FloatTensor)。"""
    i = torch.arange(n, dtype=torch.float64, device=device)
    s = (i + 0.5) * (float(c) / float(n)) - 0.5          # 板端用 double 计算 s
    i0 = torch.floor(s).to(torch.long)
    w = s - i0.to(torch.float64)
    neg = i0 < 0
    i0 = torch.where(neg, torch.zeros_like(i0), i0)
    w = torch.where(neg, torch.zeros_like(w), w)
    hi = i0 > (c - 2)
    w = torch.where(hi, torch.where(s > (c - 1), torch.zeros_like(w), w), w)
    i0 = torch.where(hi, torch.full_like(i0, c - 2), i0)
    w = torch.clamp(w, 0.0, 1.0)
    return i0, w.to(dtype)


def _bilinear_curves(curve: torch.Tensor, h: int, w: int) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """把 (3, ch, cw) 曲线场双线性采样到目标 (h, w)。返回 C0/C1/C2，各 (h, w)。
    与 `cf_sample`（native/apply_core.c:112-121）同形式：先 x 方向 top/bot，再 y 方向插值。"""
    _, ch, cw = curve.shape
    ix, wx = build_taps(w, cw, curve.device)             # (w,)
    iy, wy = build_taps(h, ch, curve.device)             # (h,)
    ix1 = (ix + 1).clamp(max=cw - 1)
    iy1 = (iy + 1).clamp(max=ch - 1)
    outs = []
    for c in range(3):
        f = curve[c]                                     # (ch, cw)
        v00 = f[iy][:, ix]                               # (h, w)
        v01 = f[iy][:, ix1]
        v10 = f[iy1][:, ix]
        v11 = f[iy1][:, ix1]
        wxb = wx.view(1, -1)
        wyb = wy.view(-1, 1)
        top = v00 + wxb * (v01 - v00)
        bot = v10 + wxb * (v11 - v10)
        outs.append(top + wyb * (bot - top))
    return outs[0], outs[1], outs[2]


def _fast_recip(l: torch.Tensor) -> torch.Tensor:
    """FRECPE + 2 次 Newton–Raphson 的**近似**模拟（非逐位；见差异源①）。
    FRECPE 的相对误差 ~2^-8，此处用「量化到 8 位有效位」的倒数作估计量，再 2 次 NR。"""
    est = 1.0 / l
    # 8-bit 相对精度估计量的粗模拟：把尾数量化到 8 位（|δ| ≲ 2^-8）
    mant = torch.frexp(est)[0]                           # [0.5,1)
    q = torch.round(mant * 256.0) / 256.0
    seed = torch.ldexp(q, torch.frexp(est)[1])
    r = seed
    r = r * (2.0 - l * r)
    r = r * (2.0 - l * r)
    return r


def apply_preserve_ratio(
    x_rgb01: torch.Tensor,
    curve: torch.Tensor,
    *,
    iters: int = CURVE_ITERS,
    luma_coeffs: Sequence[float] = LUMA_BT601,
    fast_recip: bool = False,
    quantize: bool = False,
) -> torch.Tensor:
    """「保比语义」可微前向（复刻 native/apply_core.c 的 vmean→L→f^N→k→同比缩放→clip/量化）。

    参数
    ----
    x_rgb01 : (..., H, W, 3) float tensor，值域 [0,1]（= 板端 RGB888 / 255）。
    curve   : (3, ch, cw) 或 (B, 3, ch, cw) float tensor（曲线场，planar 通道平面布局）。
    iters   : 迭代次数（契约值 8；见 realtime_apply.CURVE_ITERS）。
    luma_coeffs : (kr, kg, kb)，默认 BT.601（luma_coeffs 口径，native/apply_core.c:127-131）。
    fast_recip  : True 用 FRECPE+2NR 近似倒数（差异源①）；False（默认）= 精确除法，可微。
    quantize    : True = 末端 clip + 8-bit 量化（对齐板端 store_group4）；False（默认）= 连续 [0,1]（可微）。

    返回 : (..., H, W, 3) float tensor，[0,1]（quantize=False）或 8-bit 量化后的值（quantize=True）。
    """
    if x_rgb01.shape[-1] != 3:
        raise ValueError("x_rgb01 末维须为 3（RGB，通道交错）")
    squeeze = x_rgb01.dim() == 3
    x = x_rgb01.unsqueeze(0) if squeeze else x_rgb01
    if curve.dim() == 3:
        curve = curve.unsqueeze(0)
    if curve.shape[0] != x.shape[0]:
        if curve.shape[0] == 1:
            curve = curve.expand(x.shape[0], -1, -1, -1)
        else:
            raise ValueError("curve 的批维须与 x_rgb01 一致或为 1")
    b, h, w, _ = x.shape

    c0 = torch.empty(b, h, w, device=x.device, dtype=x.dtype)
    c1 = torch.empty_like(c0)
    c2 = torch.empty_like(c0)
    for bi in range(b):
        a0, a1, a2 = _bilinear_curves(curve[bi], h, w)
        c0[bi], c1[bi], c2[bi] = a0, a1, a2
    vmean = (c0 + c1 + c2) * (1.0 / 3.0)                 # :183 / :328

    l = luma_from_rgb01(x, luma_coeffs)                  # :184-187 / :330
    l2 = l
    for _ in range(iters):                               # :194-197 / :332（无中间 clamp）
        l2 = l2 + vmean * (l2 * l2 - l2)

    zero = l == 0.0
    if fast_recip:
        k = torch.where(zero, torch.zeros_like(l), l2 * _fast_recip(l))
    else:
        l_safe = torch.where(zero, torch.ones_like(l), l)
        k = torch.where(zero, torch.zeros_like(l), l2 / l_safe)   # :203-204 / :338（仅 L==0，无 ε）

    y = x * k.unsqueeze(-1)                              # :205 / :339-340（同比缩放三通道）
    y = torch.clamp(y, 0.0, 1.0)                         # store_group4 钳位 :212 / 标量 :341
    if quantize:
        y = torch.clamp(torch.round(y * 255.0), 0.0, 255.0) / 255.0   # lrintf :215 / :342
    return y[0] if squeeze else y


def _selftest() -> None:
    torch.manual_seed(0)
    h, w, ch, cw = 8, 12, 5, 7
    x = torch.rand(2, h, w, 3, requires_grad=True)
    curve = (-torch.rand(2, 3, ch, cw)).requires_grad_(True)   # 曲线场典型为负（提亮）
    y = apply_preserve_ratio(x, curve)
    assert y.shape == (2, h, w, 3), y.shape
    # L==0（全黑输入）⇒ k=0 ⇒ 输出恒 0，无 NaN
    z = apply_preserve_ratio(torch.zeros(1, h, w, 3), curve[:1])
    assert torch.isfinite(z).all() and float(z.abs().max()) == 0.0
    # 保比：未削顶像素三通道比值不变（构造性）
    xp = torch.full((1, 4, 4, 3), 0.2); xp[..., 1] = 0.1; xp[..., 2] = 0.05
    yp = apply_preserve_ratio(xp, torch.full((1, 3, 2, 2), -0.3))
    r = (yp[..., 0] / yp[..., 1]) - (xp[..., 0] / xp[..., 1])
    assert float(r.abs().max()) < 1e-6
    # 可微：反传不报错、梯度非空
    y.sum().backward()
    assert curve.grad is not None and torch.isfinite(curve.grad).all()
    assert x.grad is not None and torch.isfinite(x.grad).all()
    print("selftest OK：shape / L==0 退化 / 保比构造性 / 可微 反传 均通过")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    else:
        print(__doc__)