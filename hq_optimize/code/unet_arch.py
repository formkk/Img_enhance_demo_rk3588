#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量 UNet（本任务对照臂用，非交付代码）。

定位：与 `RetinexFormer`（现役 HQ，960×544）做**只换架构**的对照。
设计取向 = **可部署性优先**：只用 `Conv2d / BatchNorm2d / LeakyReLU / MaxPool2d /
`F.interpolate`(bilinear) / Concat` —— RKNN 全支持、INT8 友好，无 LayerNorm / softmax /
L2-normalize 这类不友好算子。

结构：4 级编码（channels 16/32/64/128，每级 2 个 3×3 conv）→ 瓶颈 256 → 解码对称 + skip →
1×1 head 直出 3 通道（**与 RetinexFormer 同为"直出图像"语义**，不改残差/幅度约定）。
参数量 ≈ **1.45 M**（现役 RetinexFormer = 1.606 M）⇒ 本对照臂**比现役更轻**。

空间整除性：960×544 在 3 次 MaxPool 后为 120×68，整除无余 ⇒ 无需 pad 到 2 的幂。
"""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


def _cbr(cin, cout, k=3):
    return nn.Sequential(
        nn.Conv2d(cin, cout, k, padding=k // 2, bias=False),
        nn.BatchNorm2d(cout),
        nn.LeakyReLU(0.1, inplace=True),
    )


class DoubleConv(nn.Module):
    def __init__(self, cin, cout):
        super().__init__()
        self.block = nn.Sequential(_cbr(cin, cout), _cbr(cout, cout))

    def forward(self, x):
        return self.block(x)


class UNet(nn.Module):
    CHS = (16, 32, 64, 128)

    def __init__(self, in_ch=3, out_ch=3, chs=CHS):
        super().__init__()
        self.enc = nn.ModuleList()
        c = in_ch
        for ch in chs:
            self.enc.append(DoubleConv(c, ch))
            c = ch
        self.down = nn.MaxPool2d(2)
        self.mid = DoubleConv(chs[-1], chs[-1] * 2)          # 瓶颈：192 → 384
        self.dec = nn.ModuleList()
        up_in = chs[-1] * 2                              # 从瓶颈上来：384
        for ch in reversed(chs[:-1]):                    # 解码级 = 跳连级（96 / 48 / 24）
            self.dec.append(DoubleConv(up_in + ch, ch))  # concat 跳连后 → DoubleConv
            up_in = ch
        self.head = nn.Conv2d(chs[0], out_ch, 1)

    def forward(self, x):
        skips = []
        for i, blk in enumerate(self.enc):
            x = blk(x)
            if i < len(self.enc) - 1:
                skips.append(x)
                x = self.down(x)
        x = self.mid(x)
        for blk, s in zip(self.dec, reversed(skips)):
            x = F.interpolate(x, size=s.shape[-2:], mode="bilinear", align_corners=False)
            x = blk(torch.cat([x, s], 1))
        return self.head(x)


def build(in_ch=3, out_ch=3):
    return UNet(in_ch=in_ch, out_ch=out_ch)


if __name__ == "__main__":
    m = build()
    n = sum(p.numel() for p in m.parameters())
    y = m(torch.rand(1, 3, 544, 960))
    print(f"params={n/1e6:.3f}M  out={tuple(y.shape)}")