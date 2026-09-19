/* osd_core.c —— 见 osd_core.h。 */

#define _GNU_SOURCE
#include "osd_core.h"

#include <stddef.h>

static inline uint8_t* addr_at(void* canvas, uint32_t cw, uint32_t x, uint32_t y) {
    return (uint8_t*)canvas + ((size_t)y * cw + x) * 3;
}

static inline void put_px(void* canvas, uint32_t cw, uint32_t ch,
                          int32_t x, int32_t y, uint32_t rgb) {
    if (x < 0 || y < 0 || (uint32_t)x >= cw || (uint32_t)y >= ch) return;
    uint8_t* p = addr_at(canvas, cw, (uint32_t)x, (uint32_t)y);
    p[0] = (uint8_t)(rgb >> 16);
    p[1] = (uint8_t)(rgb >> 8);
    p[2] = (uint8_t)(rgb);
}

int osd_fill_rect(void* canvas, uint32_t cw, uint32_t ch,
                  int32_t x, int32_t y, uint32_t w, uint32_t h, uint32_t rgb) {
    if (!canvas || cw == 0 || ch == 0) return OSD_E_STATE;
    if (x < 0) { w = (uint32_t)(w + x); x = 0; }
    if (y < 0) { h = (uint32_t)(h + y); y = 0; }
    if ((uint32_t)x >= cw || (uint32_t)y >= ch) return OSD_OK;
    if ((uint32_t)x + w > cw) w = cw - (uint32_t)x;
    if ((uint32_t)y + h > ch) h = ch - (uint32_t)y;
    uint8_t r = (uint8_t)(rgb >> 16), g = (uint8_t)(rgb >> 8), b = (uint8_t)(rgb);
    for (uint32_t j = 0; j < h; j++) {
        uint8_t* p = addr_at(canvas, cw, (uint32_t)x, (uint32_t)y + j);
        for (uint32_t i = 0; i < w; i++) { p[0] = r; p[1] = g; p[2] = b; p += 3; }
    }
    return OSD_OK;
}

int osd_draw_line(void* canvas, uint32_t cw, uint32_t ch,
                  int32_t x0, int32_t y0, int32_t x1, int32_t y1,
                  uint32_t thickness, uint32_t rgb) {
    if (!canvas || cw == 0 || ch == 0) return OSD_E_STATE;
    const int dx = x1 > x0 ? (x1 - x0) : (x0 - x1);
    const int dy = y1 > y0 ? (y1 - y0) : (y0 - y1);
    const int sx = x0 < x1 ? 1 : -1;
    const int sy = y0 < y1 ? 1 : -1;
    int err = dx - dy, cx = x0, cy = y0;
    const uint32_t t2 = thickness / 2;
    for (;;) {
        for (uint32_t oy = 0; oy < thickness; oy++)
            for (uint32_t ox = 0; ox < thickness; ox++)
                put_px(canvas, cw, ch, cx + (int)ox - (int)t2, cy + (int)oy - (int)t2, rgb);
        if (cx == x1 && cy == y1) break;
        const int e2 = 2 * err;
        if (e2 > -dy) { err -= dy; cx += sx; }
        if (e2 < dx) { err += dx; cy += sy; }
    }
    return OSD_OK;
}

int osd_blit_glyph(void* canvas, uint32_t cw, uint32_t ch,
                   int32_t x, int32_t y, uint32_t scale,
                   const uint8_t* glyph, uint32_t gw, uint32_t gh, uint32_t rgb) {
    if (!canvas || !glyph || cw == 0 || ch == 0 || gw == 0 || gh == 0) return OSD_E_STATE;
    if (scale == 0) scale = 1;
    const uint32_t bytes_per_row = (gw + 7) / 8;
    for (uint32_t ry = 0; ry < gh; ry++) {
        for (uint32_t rx = 0; rx < gw; rx++) {
            const uint8_t byte = glyph[(size_t)ry * bytes_per_row + (rx / 8)];
            const int on = (byte >> (7 - (rx % 8))) & 1;
            if (!on) continue;
            for (uint32_t sy = 0; sy < scale; sy++)
                for (uint32_t sx = 0; sx < scale; sx++)
                    put_px(canvas, cw, ch, x + (int32_t)(rx * scale + sx),
                           y + (int32_t)(ry * scale + sy), rgb);
        }
    }
    return OSD_OK;
}