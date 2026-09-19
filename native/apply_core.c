/* apply_core.c —— 见 apply_core.h。
 *
 * 算法来源：tools/pipeline_probe.c 的 build_taps / apply_fused_neon / apply_fused_scalar_to
 *          （与 tools/apply_probe.c 的标量参考同源）。本文件只做「去全局化 + 显式参数化 +
 *          曲线场按 §3.1 契约以 FP16/U8 存取」，**不改算法、不改采样口径、不改迭代表达式**。
 *
 * 采样表：configure 时按**目标域上界**预分配（1920×1080），每帧只重填不 malloc
 *         ⇒ 满足「热路径不分配」（docs/design/realtime_apply.md §2）。
 */

#define _GNU_SOURCE
#include "apply_core.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#ifndef __ARM_NEON
#error "须以 -march=armv8.2-a+fp16 编译（NEON + FP16 转换指令）；本内核不提供无 NEON 后备"
#endif
#include <arm_neon.h>

/* 目标域上界（§3.1：显示域 512×288、存盘域 1920×1080） */
#define CF_MAX_W 1920u
#define CF_MAX_H 1080u

typedef struct { int i0; float w; } tap_t;

static struct {
    tap_t xt[CF_MAX_W];
    tap_t yt[CF_MAX_H];
    uint32_t tap_w, tap_h;   /* 采样表当前对应的目标域尺寸（0 = 未建） */
    uint32_t curve_w, curve_h;
    uint32_t iters;
    cf_precision_t precision;
    cf_csc_t csc;
    cf_range_t range;
} g;

static int g_configured = 0;

static void build_taps(tap_t* t, uint32_t N, uint32_t C);

/* 采样表只在**目标域尺寸变化**时重建：实时链路里 w/h 恒定 ⇒ 等价于「初始化时建一次」，
   与 M0-13 microbenchmark 的口径一致（探针亦只在 apply_init 建一次）。 */
static inline void ensure_taps(uint32_t w, uint32_t h) {
    if (g.tap_w == w && g.tap_h == h) return;
    build_taps(g.xt, w, g.curve_w);
    build_taps(g.yt, h, g.curve_h);
    g.tap_w = w;
    g.tap_h = h;
}

/* 采样表：目标像素中心 (i+0.5)/N 归一化 → 源坐标 (u*C − 0.5)，i0 夹到 [0,C−2]、w 夹到 [0,1]。
   与 tools/pipeline_probe.c 的 build_taps 逐行同源。 */
static void build_taps(tap_t* t, uint32_t N, uint32_t C) {
    for (uint32_t i = 0; i < N; i++) {
        float s = (float)((i + 0.5) * (double)C / (double)N - 0.5);
        int i0 = (int)floorf(s);
        float w = s - (float)i0;
        if (i0 < 0) { i0 = 0; w = 0.0f; }
        if (i0 > (int)C - 2) { i0 = (int)C - 2; w = (s > (float)(C - 1)) ? 0.0f : w; }
        if (w < 0.0f) w = 0.0f;
        if (w > 1.0f) w = 1.0f;
        t[i].i0 = i0;
        t[i].w = w;
    }
}

int cf_configure(const cf_config_t* cfg) {
    if (g_configured) return CF_E_STATE;
    if (!cfg || cfg->curve_w < 2 || cfg->curve_h < 2 || cfg->iters == 0) return CF_E_STATE;
    g.curve_w = cfg->curve_w;
    g.curve_h = cfg->curve_h;
    g.iters = cfg->iters;
    g.precision = cfg->precision;
    g.csc = cfg->csc;
    g.range = cfg->range;
    g.tap_w = g.tap_h = 0;   /* 曲线场几何变了 ⇒ 采样表作废 */
    g_configured = 1;
    return CF_OK;
}

int cf_is_configured(void) { return g_configured; }

void cf_shutdown(void) {
    memset(&g, 0, sizeof(g));
    g_configured = 0;
}

const char* cf_impl_id(void) {
    return "realtime_apply/apply_core v1 (NEON single-pass fusion; FP16/U8 curve field)";
}

/* ── 曲线场存取（§3.1 契约第 3 条：全档 FP16；U8 为可选优化）──────────────
 * 布局 = **通道平面**（planar 3ch），与 pipeline_probe 的 cb[ch] = curve + ch*CH*CW 一致。
 */
static inline float cf_load(const void* curve, uint32_t ch, uint32_t cw, uint32_t ch_off,
                            size_t idx) {
    const size_t base = (size_t)ch * ch_off * cw;
    if (g.precision == CF_PREC_FP16) {
        const __fp16* p = (const __fp16*)curve;
        return (float)p[base + idx];
    }
    const uint8_t* p = (const uint8_t*)curve;
    return (float)p[base + idx] * (1.0f / 255.0f);
}

/* 双线性采样一次（每目标像素每通道一次；融合进迭代循环） */
static inline float cf_sample(const void* curve, uint32_t ch, int ix, int iy, float wx, float wy,
                              uint32_t cw, uint32_t ch_off) {
    const float v00 = cf_load(curve, ch, cw, ch_off, (size_t)iy * cw + (uint32_t)ix);
    const float v01 = cf_load(curve, ch, cw, ch_off, (size_t)iy * cw + (uint32_t)ix + 1);
    const float v10 = cf_load(curve, ch, cw, ch_off, (size_t)(iy + 1) * cw + (uint32_t)ix);
    const float v11 = cf_load(curve, ch, cw, ch_off, (size_t)(iy + 1) * cw + (uint32_t)ix + 1);
    const float top = v00 + wx * (v01 - v00);
    const float bot = v10 + wx * (v11 - v10);
    return top + wy * (bot - top);
}

/* ── NEON 单遍融合：4 像素 × 3 通道一组（与 pipeline_probe 同形）────────── */
static int apply_fused_neon(const uint8_t* rgb, const void* curve, uint8_t* dst,
                            uint32_t w, uint32_t h) {
    const uint32_t CW = g.curve_w, CH_OFF = g.curve_h;
    ensure_taps(w, h);
    for (uint32_t y = 0; y < h; y++) {
        const int iy = g.yt[y].i0; const float wy = g.yt[y].w;
        for (uint32_t x = 0; x < w; x += 4) {
            float rv[3][4];
            for (uint32_t ch = 0; ch < 3; ch++)
                for (int l = 0; l < 4; l++)
                    rv[ch][l] = cf_sample(curve, ch, g.xt[x + (uint32_t)l].i0, iy,
                                          g.xt[x + (uint32_t)l].w, wy, CW, CH_OFF);
            float32x4_t C[3] = { vld1q_f32(rv[0]), vld1q_f32(rv[1]), vld1q_f32(rv[2]) };
            float32x4_t V[3];
            {
                uint8_t tmp[3][4];
                for (int l = 0; l < 4; l++) {
                    const uint8_t* p = rgb + ((size_t)y * w + x + (uint32_t)l) * 3;
                    tmp[0][l] = p[0]; tmp[1][l] = p[1]; tmp[2][l] = p[2];
                }
                for (int ch = 0; ch < 3; ch++) {
                    uint8x8_t u8 = vld1_u8(tmp[ch]);
                    V[ch] = vmulq_n_f32(vcvtq_f32_u32(vmovl_u16(vget_low_u16(vmovl_u8(u8)))),
                                        1.0f / 255.0f);
                }
            }
            for (uint32_t it = 0; it < g.iters; it++)
                for (int ch = 0; ch < 3; ch++) {
                    float32x4_t t = vsubq_f32(vmulq_f32(V[ch], V[ch]), V[ch]);
                    V[ch] = vfmaq_f32(V[ch], t, C[ch]);
                }
            for (int ch = 0; ch < 3; ch++)
                V[ch] = vmaxq_f32(vminq_f32(V[ch], vdupq_n_f32(1.0f)), vdupq_n_f32(0.0f));
            uint8x8_t q[3];
            for (int ch = 0; ch < 3; ch++) {
                int32x4_t s = vcvtnq_s32_f32(vmulq_n_f32(V[ch], 255.0f));
                q[ch] = vqmovun_s16(vcombine_s16(vqmovn_s32(s), vqmovn_s32(s)));
            }
            uint8_t tmp[3][8];
            for (int ch = 0; ch < 3; ch++) vst1_u8(tmp[ch], q[ch]);
            for (int l = 0; l < 4; l++) {
                uint8_t* o = dst + ((size_t)y * w + x + (uint32_t)l) * 3;
                o[0] = tmp[0][l]; o[1] = tmp[1][l]; o[2] = tmp[2][l];
            }
        }
    }
    return CF_OK;
}

/* ── 标量参考：与 NEON **同形**（同采样口径、同迭代表达式），供逐位自检 ──── */
static int apply_fused_scalar(const uint8_t* rgb, const void* curve, uint8_t* dst,
                              uint32_t w, uint32_t h) {
    const uint32_t CW = g.curve_w, CH_OFF = g.curve_h;
    ensure_taps(w, h);
    for (uint32_t y = 0; y < h; y++) {
        const int iy = g.yt[y].i0; const float wy = g.yt[y].w;
        for (uint32_t x = 0; x < w; x++) {
            const int ix = g.xt[x].i0; const float wx = g.xt[x].w;
            const uint8_t* p = rgb + ((size_t)y * w + x) * 3;
            uint8_t* o = dst + ((size_t)y * w + x) * 3;
            for (uint32_t ch = 0; ch < 3; ch++) {
                const float v = cf_sample(curve, ch, ix, iy, wx, wy, CW, CH_OFF);
                float t = (float)p[ch] * (1.0f / 255.0f);
                /* 与 NEON 的 `vfmaq_f32` 同形：显式融合乘加以保证两路逐位一致
                   （写成 `t = t + v*(t*t-t)` 时编译器是否融合取决于 -ffp-contract，实测会差 1 个字节）。 */
                for (uint32_t it = 0; it < g.iters; it++) t = fmaf(v, t * t - t, t);
                if (t < 0.0f) t = 0.0f; else if (t > 1.0f) t = 1.0f;
                o[ch] = (uint8_t)lrintf(t * 255.0f);
            }
        }
    }
    return CF_OK;
}

/* ── NV12 → RGB888（BT.601 / BT.709，limited / full range）──────────────
 * 仅 `apply_full`（触发存盘集合 B）使用，不在实时热路径上。
 */
static void nv12_to_rgb(const uint8_t* ybase, const uint8_t* uvbase, uint8_t* out,
                        uint32_t w, uint32_t h) {
    const double Kr = (g.csc == CF_CSC_BT709) ? 0.2126 : 0.299;
    const double Kb = (g.csc == CF_CSC_BT709) ? 0.0722 : 0.114;
    const double Kg = 1.0 - Kr - Kb;
    const double y_min = (g.range == CF_RANGE_LIMITED) ? 16.0 : 0.0;
    const double y_span = (g.range == CF_RANGE_LIMITED) ? (235.0 - 16.0) : 255.0;
    const double c_min = (g.range == CF_RANGE_LIMITED) ? 16.0 : 0.0;
    const double c_span = (g.range == CF_RANGE_LIMITED) ? (240.0 - 16.0) : 255.0;
    const double c_bias = 128.0;

    for (uint32_t j = 0; j < h; j++) {
        const size_t uv_row = (size_t)(j >> 1) * (w >> 1);
        for (uint32_t i = 0; i < w; i++) {
            const double Y = ((double)ybase[(size_t)j * w + i] - y_min) / y_span;
            const size_t uv = uv_row + (i >> 1);
            const double Cb = ((double)uvbase[uv] - c_min) / c_span - c_bias / 255.0;
            const double Cr = ((double)uvbase[uv + 1] - c_min) / c_span - c_bias / 255.0;
            const double r = Y + 2.0 * (1.0 - Kr) * Cr;
            const double b = Y + 2.0 * (1.0 - Kb) * Cb;
            const double gg = Y - (2.0 * Kb * (1.0 - Kb) / Kg) * Cb
                              - (2.0 * Kr * (1.0 - Kr) / Kg) * Cr;
            uint8_t* o = out + ((size_t)j * w + i) * 3;
            o[0] = (uint8_t)(r < 0.0 ? 0.0 : (r > 1.0 ? 255.0 : lrint(r * 255.0)));
            o[1] = (uint8_t)(gg < 0.0 ? 0.0 : (gg > 1.0 ? 255.0 : lrint(gg * 255.0)));
            o[2] = (uint8_t)(b < 0.0 ? 0.0 : (b > 1.0 ? 255.0 : lrint(b * 255.0)));
        }
    }
}

/* ── 契约入口（§5 契约 C 节，签名逐字）───────────────────────────────── */
int apply_window(const uint8_t* rgb, const void* curve, uint8_t* dst, uint32_t w, uint32_t h) {
    if (!g_configured || !rgb || !curve || !dst || w == 0 || h == 0) return CF_E_STATE;
    if (w > CF_MAX_W || h > CF_MAX_H) return CF_E_STATE;
    if (w % 4 == 0) return apply_fused_neon(rgb, curve, dst, w, h);
    return apply_fused_scalar(rgb, curve, dst, w, h);
}

int apply_full(const uint8_t* nv12, const void* curve, uint8_t* dst_rgb, uint32_t w, uint32_t h) {
    if (!g_configured || !nv12 || !curve || !dst_rgb || w == 0 || h == 0) return CF_E_STATE;
    if ((w & 1u) || (h & 1u) || w > CF_MAX_W || h > CF_MAX_H) return CF_E_STATE;
    /* 色彩矩阵 / 量化范围未定稿（§10.4 isp.csc_matrix / isp.rgb_range = M1 键）时不得猜测取值 */
    if (g.csc == CF_CSC_UNSET || g.range == CF_RANGE_UNSET) return CF_E_STATE;
    /* 满幅 NV12 → RGB888（§3.1：1080p 全幅应用仅服务触发存盘），再做 8 iter 应用。
       stride 按 §3.1 第 2 条：1920 与 512 均 16 对齐 ⇒ 紧排 w*3 即对齐 stride。 */
    uint8_t* rgb = (uint8_t*)malloc((size_t)w * h * 3);
    if (!rgb) return CF_E_STATE;
    nv12_to_rgb(nv12, nv12 + (size_t)w * h, rgb, w, h);
    int rc = apply_fused_neon(rgb, curve, dst_rgb, w, h);
    free(rgb);
    return rc;
}

int cf_apply_scalar(const uint8_t* rgb, const void* curve, uint8_t* dst, uint32_t w, uint32_t h) {
    if (!g_configured || !rgb || !curve || !dst || w == 0 || h == 0) return CF_E_STATE;
    if (w > CF_MAX_W || h > CF_MAX_H) return CF_E_STATE;
    return apply_fused_scalar(rgb, curve, dst, w, h);
}
