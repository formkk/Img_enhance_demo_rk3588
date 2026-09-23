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

/* RGB888 双缓冲统计（与 `osd.py` 的 `Stats` **同口径**，§3.4：降采样 + 灰度直方图 + 均值）。
 *
 * **（v3.86 裁定 O142-候选①）** 目的：把原 Python 逐样本循环（9216 点 × 双直方图）下沉到 C，
 * 且**直接吃 native 缓冲指针、零拷贝**（原路径每帧要 `to_bytes()` 两次）。
 *
 * `decimate` = 抽样步长（4 = 每轴每 4 像素取一点，与 Python 的 `range(0, w, d)` 一致）；
 * `hist_o` / `hist_e` 须各 **256** 项。灰度式与 Python **逐字一致**：`y = (77r + 150g + 29b) >> 8`。
 * 返回**采样点数**（`0` = 参数非法，调用方不得使用输出）。 */
uint32_t osd_stats_rgb888(const uint8_t* raw, const uint8_t* enh,
                          uint32_t w, uint32_t h, uint32_t decimate,
                          uint32_t* hist_o, uint32_t* hist_e,
                          double* mean_o, double* mean_e);

#ifdef __cplusplus
}
#endif

#endif /* STARLIGHT_OSD_CORE_H */
