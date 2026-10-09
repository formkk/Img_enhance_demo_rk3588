/* wb_core.c —— MIPI 面板白平衡校正的逐像素内核（口径见 wb_core.h）
 *
 * 设计要点：
 *   · **构建**（一次性、可有浮点）：sRGB 解码 → 线性域乘增益 → 重新编码 → 取整存表；
 *     某通道增益恰为 1.0 时**直接写恒等项**（避免编解码往返带来 ±1 LSB 误差 ⇒ 保证逐位不变）。
 *   · **应用**（每帧、纯整数）：三次查表 ⇒ `-O3` 下可自动向量化。
 *     量级参考：1024×600×3 ≈ 1.8 MB/帧 ⇒ 亚毫秒~1 ms 级（板端实测为准）。
 *   · 触发频率：**每次合成一次**（常规视图每帧一次、全屏 HQ 视图仅在贴图那一次）
 *     ⇒ 计入 §6.1 的帧路径开销，须按 §11.2 回归确认不掉帧。
 */

#include "wb_core.h"

#include <math.h>

static double srgb_to_lin(double c)
{
    return (c <= 0.04045) ? (c / 12.92) : pow((c + 0.055) / 1.055, 2.4);
}

static double lin_to_srgb(double v)
{
    if (v <= 0.0) return 0.0;
    if (v >= 1.0) return 1.0;
    return (v <= 0.0031308) ? (v * 12.92) : (1.055 * pow(v, 1.0 / 2.4) - 0.055);
}

void wb_build_lut(const double *gain_rgb, uint8_t lut[768])
{
    for (int ch = 0; ch < 3; ++ch) {
        const double gain = (gain_rgb != NULL) ? gain_rgb[ch] : 1.0;
        for (int i = 0; i < 256; ++i) {
            uint8_t v;
            if (gain == 1.0) {
                v = (uint8_t)i;                        /* 恒等通道：逐位不变 */
            } else {
                double lin = srgb_to_lin((double)i / 255.0) * gain;
                if (lin > 1.0) lin = 1.0;
                const double e = lin_to_srgb(lin) * 255.0;
                v = (uint8_t)(e + 0.5);
            }
            lut[ch * 256 + i] = v;
        }
    }
}

void wb_apply_lut(uint8_t *rgb, size_t npix, const uint8_t lut[768])
{
    const uint8_t *lr = lut;
    const uint8_t *lg = lut + 256;
    const uint8_t *lb = lut + 512;
    for (size_t i = 0; i < npix; ++i) {
        uint8_t *p = rgb + 3 * i;
        p[0] = lr[p[0]];
        p[1] = lg[p[1]];
        p[2] = lb[p[2]];
    }
}
