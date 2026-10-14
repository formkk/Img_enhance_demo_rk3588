#!/usr/bin/env python3
"""gen_osd_zh_bitmap.py —— **开发机工具**：预渲染 OSD 图示区两行中文说明为 1bpp 位图资源。

用途（为什么需要它）
-------------------
板端 OSD 的文本渲染只有 **PSF 控制台字体**（`osd.py` 的 `Font`，stdlib `gzip` 解压 PSF1/PSF2），
**不含 CJK 字形**、也没有 FreeType / PIL 等运行时字体栈（板端已核实 `import PIL` 失败）。
故「图左上角中文说明」采用**开发机预渲染 + 静态位图入库**方案：本脚本在开发机用 PIL 把一个
CJK TTF 的所需两行文字栅格化成 **1bpp 位图**，输出到 `assets/osd_zh_labels.bin`；板端只按
相对路径读取该文件，用**既有** native 原语 `_n.osd.blit_glyph`（scale=1）blit 到画布
⇒ **运行期零字体依赖、零新增 native 接口**。

位图为何是 1bpp（而非 8bpp alpha）
----------------------------------
`native/osd_core.c` 只提供 1bpp 字形 blit（`osd_blit_glyph`：按位取 1 则 put_px），没有 alpha
混合原语；OSD 现有 PSF 字形本身也是 1bpp。为**保持 OSD 文本风格一致**且不改 native，本脚本把
PIL 的灰度/alpha 结果按阈值二值化（默认 128）后输出 1bpp。

输出格式（自描述，板端纯 stdlib 解析）
-------------------------------------
    b"SLZH1\n"                      # magic + 版本
    每行一条记录，共 2 行文本头：
    b"<name> <w> <h> <stride>\n"    # name = `LABELS` 中的记录名（与 `osd.py` 的
                                    # `ZH_LABELS` / `BUTTON_ZH` / `WIN_ZH` 映射逐字一致）；
    紧随其后为 `stride*h` 字节的 1bpp 位图（MSB 优先，与 `osd_blit_glyph` 同口径），
    记录间**紧密排列**（按出现顺序）。

用法：`python3 tools/gen_osd_zh_bitmap.py [--out assets/osd_zh_labels.bin]`
（依赖：开发机 PIL + 一个 CJK TTF，均**不**进板端运行时。）
"""

from __future__ import annotations

import argparse
import os
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

# 两行说明（顺序 = 记录顺序；name 与 `osd.py` 的 `ZH_LABELS` 键逐字一致）
# 每项 = (记录名, 文字, 墨迹高度 px)
# **（v3.86 裁定 O112 / O113）** 两个**图名**（curve / hist）的墨迹高度 28 → 26 → **25 px**（O112 缩两号、O113 再缩一点）；
# 状态行的三个标签（lat / rate / lm）保持 28 px（与状态行 ASCII 字格等高）。
LABELS: Tuple[Tuple[str, str, int], ...] = (
    ("curve", "色调映射曲线", 25),
    ("hist", "亮度直方图", 25),
    # **（v3.86 裁定 O108 / O109）** 状态行的中文标签：板端 PSF 控制台字体**无 CJK 字形** ⇒ 同样预渲染。
    # 全角冒号「：」也**烘进位图**（PSF 只有半角 `:`，宽度与风格不匹配）；「均值提升」按裁定**不带冒号**。
    ("lat", "端延时：", 28),
    ("rate", "帧率：", 28),
    ("lm", "均值提升", 28),
    # **（v3.88 界面优化 / 按钮全中文）** 三个按钮的标签改为**全中文**（原 ASCII「TRIGGER HQ /
    # DCM_DEMO / EXIT」全部替换；退出键的「(长按3秒)」并入中文位图，不再保留 ASCII 段）。
    # 按键语义（§3.4）：触发 HQ 路高质量增强 / DCM 演示路特征检测 / 长按 3 秒退出。
    # 墨迹高 28 px 与按钮区 ASCII 字格（`ui.osd_font_px[0]=36` ⇒ 字格 36 px、墨迹 ≈28 px）等高对齐。
    ("btn_hq", "高质量增强", 28),
    ("btn_dcm", "DCM 特征检测", 28),
    ("btn_exit", "退出（长按3秒）", 28),
    # **（v3.88 界面优化 / 窗口中文标签）** 上部两窗左上角的中文标签（原图窗 / 增强窗）。
    # 墨迹高 25 px 与两块图名（curve / hist）一致（O112/O113 缩号后定稿值）；标签色 `C_CAPTION`。
    # **（2026-10-14 用户裁定）** 增强窗标签由「增强图像」改为 **「ZDE++实时增强」**
    # （点名实时路实际使用的曲线模型；混排 ASCII+CJK，同一字体渲染）。
    ("win_orig", "原始图像", 25),
    ("win_enh", "ZDE++实时增强", 25),
    # **（v3.88 界面优化 / HQ 全屏视图叠层）** HQ 全屏结果图上的中文说明（§3.5 新增）：
    #   hq_title = 左上角标签（与两窗标签同规格：25 px 墨迹、琥珀色）；
    #   hq_mean / hq_dark / hq_sat = 右下角指标卡的三行中文（数值由板端 PSF 渲染）。
    # **（2026-10-14 用户裁定）** ① 卡片中文由 28 → **24 px**；② **底部「处理链」整行取消**
    # ⇒ 原 `hq_chain` / `hq_l1` / `hq_l4` 三条记录**删除**；③ **「提升倍数」删除**（`hq_gain`）；
    # ④ 追加「暗部占比 / 饱和度」后，**再删除「灰度熵」与「清晰度」**（`hq_ent` / `hq_sharp`）
    # ⇒ 卡片最终**三行**：亮度均值 / 暗部占比 / 饱和度。
    ("hq_title", "Retinexformer 增强", 25),
    ("hq_mean", "亮度均值", 24),
    ("hq_dark", "暗部占比", 24),
    ("hq_sat", "饱和度", 24),
    # **（2026-10-14 用户裁定 / DCM 全屏「压缩·带宽」叠层）** 第三视图态全屏的说明文字：
    #   dc_foot = **图像底部居中**一行的文字（**黑底绿字**）；其余 = 右下角卡片行的中文标签
    # （数值由板端小一号 PSF 渲染）。口径：原图像素 / DCM 码流(rANS 后) / 压缩倍数与节省。
    #   **（2026-10-14 用户裁定）** 卡片**不再列"检出"行** ⇒ 检测结果以**图像上画出的框**呈现。
    # **（2026-10-14 用户裁定，四次）** ① **去掉左上角标签** ⇒ `dc_title` 删除；
    # ② 卡片末行标签「检出(非精度)」→「**检出(解码特征)**」；③ 新增底部居中一行 `dc_foot`；
    # ④ **去掉「特征FP16」一行** ⇒ `dc_feat` 删除；⑤ **再去掉「检出(解码特征)」一行**
    # ⇒ `dc_det` 删除，卡片最终**三行**（原图像素 / DCM码流 / 压缩·节省）。
    ("dc_px", "原图像素", 24),
    ("dc_stream", "DCM码流", 24),
    ("dc_ratio", "压缩/节省", 24),
    ("dc_foot", "DCM码流重建图", 25),
    # **（v3.87 追加 37 / DCM 演示路进度带）** 冻结期「居中进度带」的阶段名与「已用时/秒」中文段
    # （数字由板端 PSF 渲染，见 display_mipi.mark_busy 的 stage_zh 形参）：
    #   stg_enh    = 阶段 1（TH3 RetinexFormer 推理中）
    #   stg_codec  = 阶段 2（DCM 独立进程：特征编解码，含 9 网加载）
    #   stg_detect = 阶段 3（DCM 独立进程：特征域目标检测，含 3 网加载）
    #   elapsed/sec = 第二行「已用时 X.X 秒」的中文段（X.X 为 ASCII）
    # **（2026-10-14 用户裁定：HQ 键路径也用同风格进度带）** `stg_mfa` = **HQ 路阶段 1**
    # （多帧时域平均，`--hq-mfa-frames N>1`；`N<=1` 时不做平均 ⇒ 直接进 `stg_enh`）；
    # **HQ 路阶段 2 复用 `stg_enh`**（同为 RetinexFormer 推理）。
    ("stg_mfa", "多帧时域平均中", 28),
    ("stg_enh", "RetinexFormer 网络增强中", 28),
    ("stg_codec", "DCM 特征编解码中", 28),
    ("stg_detect", "特征域目标检测中", 28),
    ("elapsed", "已用时", 28),
    ("sec", "秒", 28),
)

# 开发机 CJK 字体候选（取第一个可加载的；**仅开发机依赖**）
FONT_CANDIDATES = (
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc", 2),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/truetype/arphic/uming.ttc", 0),
)

# 目标墨迹（ink）高度 = 28 px（与 OSD 状态行/按钮文字实测墨迹高度一致，见板上 PNG 量取）
INK_H = 28
THRESHOLD = 128


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    last = None
    for path, idx in FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            return ImageFont.truetype(path, size, index=idx)
        except OSError as e:      # 字体不可加载 → 试下一个
            last = e
    raise SystemExit(f"开发机无可用 CJK 字体：{FONT_CANDIDATES}（{last}）")


def render(text: str, ink_h: int = INK_H) -> Tuple[int, int, bytes]:
    """把一行中文栅格化为紧贴墨迹的 1bpp 位图，返回 (w, h, bits)。`ink_h` = 目标墨迹高度。"""
    size = ink_h
    for _ in range(6):                       # 迭代逼近目标墨迹高度
        f = _load_font(size)
        big = Image.new("L", (len(text) * size * 2 + 8, size * 3 + 8), 0)
        ImageDraw.Draw(big).text((4, 4), text, font=f, fill=255)
        box = big.getbbox()
        if box is None:
            raise SystemExit(f"渲染为空：{text!r}")
        h = box[3] - box[1]
        if h == ink_h:
            break
        size = max(4, int(round(size * ink_h / h)))
    ink = big.crop(box)                      # 裁到墨迹包围盒
    w, h = ink.size
    mono = ink.point(lambda v: 255 if v >= THRESHOLD else 0).convert("1")
    bpr = (w + 7) // 8
    out = bytearray()
    for y in range(h):
        row = bytearray(bpr)
        for x in range(w):
            if mono.getpixel((x, y)):
                row[x // 8] |= 1 << (7 - (x % 8))
        out += row
    return w, h, bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "assets", "osd_zh_labels.bin"))
    a = ap.parse_args()

    recs: List[Tuple[str, int, int, bytes]] = []
    for name, text, ink_h in LABELS:
        w, h, bits = render(text, ink_h)
        recs.append((name, w, h, bits))

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "wb") as fp:
        fp.write(b"SLZH1\n")
        for name, w, h, bits in recs:
            stride = (w + 7) // 8
            fp.write(f"{name} {w} {h} {stride}\n".encode("ascii"))
            fp.write(bits)
    total = os.path.getsize(a.out)
    for name, w, h, _ in recs:
        print(f"{name:6s} {w}×{h} 1bpp")
    print(f"wrote {a.out} ({total} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())