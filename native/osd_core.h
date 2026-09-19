/* osd_core.h —— OSD 绘制原语（TH4 的 OSD 指标区与按钮区用）
 *
 * 只提供**像素级原语**（矩形 / 线段 / 字形），布局与字段内容由 Python 侧 `osd.py` 决定
 * （取值见主基准 §3.4 的 OSD 定稿布局）。
 *
 * 字形来源：系统 PSF 控制台字体（Python 侧解压解析后按 1bpp 行位图传入），
 * 故本模块**不内嵌字体表**。
 */

#ifndef STARLIGHT_OSD_CORE_H
#define STARLIGHT_OSD_CORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define OSD_OK 0
#define OSD_E_STATE (-5)

/* RGB888 画布上的矩形填充（越界自动裁剪）。 */
int osd_fill_rect(void* canvas, uint32_t cw, uint32_t ch,
                  int32_t x, int32_t y, uint32_t w, uint32_t h, uint32_t rgb);

/* 线段（Bresenham + thickness 方形笔宽）。 */
int osd_draw_line(void* canvas, uint32_t cw, uint32_t ch,
                  int32_t x0, int32_t y0, int32_t x1, int32_t y1,
                  uint32_t thickness, uint32_t rgb);

/* 1bpp 字形位图放大 blit。
   `glyph` 为 `gh` 行、每行 `((gw+7)/8)` 字节、MSB 在前的位图；`scale` 为整数放大倍数。 */
int osd_blit_glyph(void* canvas, uint32_t cw, uint32_t ch,
                   int32_t x, int32_t y, uint32_t scale,
                   const uint8_t* glyph, uint32_t gw, uint32_t gh, uint32_t rgb);

#ifdef __cplusplus
}
#endif

#endif /* STARLIGHT_OSD_CORE_H */
