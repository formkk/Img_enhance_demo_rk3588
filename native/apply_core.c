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
    int preserve_ratio;      /* A/B 开关：0 = 旧语义（逐通道），1 = 保比语义（见 apply_fused_*） */
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
    g.preserve_ratio = cfg->preserve_ratio ? 1 : 0;
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

/* 亮度系数 Kr/Kg/Kb：**复用 nv12_to_rgb() 里的同一表达式**（同一三元 + 同一字面量）。
 * `isp.csc_matrix` 当前为空 ⇒ `CF_CSC_UNSET` ⇒ 走 BT.601（与 native/rga_core.c:184 的
 * `IM_YUV_TO_RGB_BT601_FULL` 一致）。仅 `preserve_ratio` 语义使用；NEON 与标量两路共用本函数
 * ⇒ 系数逐位相同。 */
static inline void luma_coeffs(float* kr, float* kg, float* kb) {
    *kr = (g.csc == CF_CSC_BT709) ? 0.2126 : 0.299;
    *kb = (g.csc == CF_CSC_BT709) ? 0.0722 : 0.114;
    *kg = 1.0f - *kr - *kb;
}

/* ── 保比语义的快速倒数（替换逐像素精确除法 L2/L）─────────────────────────
 * 原实现每像素一次 `vdivq_f32(L2, L)`（标量侧 `L2 / L`），实测使端到端 P95 中位
 * 72.91 → 111.92 ms 而函数级 apply-micro 仅 +1.6 ms ⇒ 逐像素除法代价高昂，改用快速倒数。
 *
 * 算法：recip ← FRECPE(d)（`vrecpeq_f32`，8 bit 估计），再做 **2 次 Newton–Raphson** 细化
 *       `recip ← recip·(2 − d·recip)`，最后 `k = L2·recip`。
 * · **为什么 NR 两次**：FRECPE 相对误差 ~2^-8；一次 NR 后 ~2^-16，二次后 ~2^-24（≈ IEEE float
 *   的 1 ULP，本文件实测与精确除法的相对误差 ≤1.2e-7 < 1e-6）⇒ k 的相对误差 ≤~1e-7，
 *   最终 8-bit 量化（`×255` 后 `lrint`）不产生可见偏差。**不引入任何软件查表**（Neon 无 gather）。
 * · **为什么两路必须同形**：`realtime_apply.py --selfcheck` 要求 NEON 与标量**逐字节一致**。
 *   NR 迭代在 `-ffp-contract=off` 下以 `vfmaq_f32`/`vmulq_f32` 对 `fmaf`/显式乘即可同形；但**估计量
 *   必须同源**——若标量以 `1.0f/L`（精确倒数）作种子，实测与 FRECPE 在 ~33% 的输入上相差 1 ULP，
 *   逐字节自检即失败。A64 无标量 FRECPE，故标量侧对 1 元向量调 `vrecpeq_f32` 后取 lane 0。
 * · **L==0 退化**：FRECPE(0)=+inf ⇒ 后续为 inf/nan，但对外仍以 `L==0` 掩码把 k 置 0（处理不变）。
 */

/* ── NEON 单遍融合：4 像素 × 3 通道一组（与 pipeline_probe 同形）────────── */

/* 载入一个 4 像素组：曲线双线性采样 → C；原始通道 → Raw / 归一化 V（与旧内联代码**同形**）。 */
static inline void load_group4(const uint8_t* rgb, const void* curve, uint32_t w,
                               uint32_t y, uint32_t x, uint32_t CW, uint32_t CH_OFF,
                               int iy, float wy,
                               float32x4_t C[3], float32x4_t Raw[3], float32x4_t V[3]) {
    float rv[3][4];
    for (uint32_t ch = 0; ch < 3; ch++)
        for (int l = 0; l < 4; l++)
            rv[ch][l] = cf_sample(curve, ch, g.xt[x + (uint32_t)l].i0, iy,
                                  g.xt[x + (uint32_t)l].w, wy, CW, CH_OFF);
    C[0] = vld1q_f32(rv[0]); C[1] = vld1q_f32(rv[1]); C[2] = vld1q_f32(rv[2]);
    uint8_t tmp[3][4];
    for (int l = 0; l < 4; l++) {
        const uint8_t* p = rgb + ((size_t)y * w + x + (uint32_t)l) * 3;
        tmp[0][l] = p[0]; tmp[1][l] = p[1]; tmp[2][l] = p[2];
    }
    for (int ch = 0; ch < 3; ch++) {
        uint8x8_t u8 = vld1_u8(tmp[ch]);
        /* Raw = 原始通道值（0..255，float）；V = 归一化（旧语义与保比语义共用） */
        Raw[ch] = vcvtq_f32_u32(vmovl_u16(vget_low_u16(vmovl_u8(u8))));
        V[ch] = vmulq_n_f32(Raw[ch], 1.0f / 255.0f);
    }
}

/* 保比语义前置量（NEON 与标量参考**同形**）：vmean（三通道均值）+ L（亮度）。
 * 旧语义**逐通道**施加曲线 t ← t + v(t²−t)（逐点非线性 ⇒ 通道比值被压缩 ⇒ 去饱和）；
 * 保比语义把 v 取**三通道均值**、对**亮度** L 施加同一曲线得 L2、再以**同一标量** k=L2/L
 * 回乘三通道 ⇒ 比值 R:G:B **严格不变**，仅整体亮度被提（属交付实现选择）。 */
static inline void ratio_head4(const float32x4_t C[3], const float32x4_t Raw[3],
                               float32x4_t* vmean, float32x4_t* L) {
    float kr, kg, kb;
    luma_coeffs(&kr, &kg, &kb);
    *vmean = vmulq_n_f32(vaddq_f32(vaddq_f32(C[0], C[1]), C[2]), 1.0f / 3.0f);
    *L = vmulq_n_f32(
        vaddq_f32(vaddq_f32(vmulq_n_f32(Raw[0], kr), vmulq_n_f32(Raw[1], kg)),
                  vmulq_n_f32(Raw[2], kb)),
        1.0f / 255.0f);
}

/* 保比语义后置量（NEON 与标量参考**同形**）：L2（8 次迭代）→ 快速倒数（FRECPE + 2 次 NR）
 * → k → 回乘三通道。退化点 L==0（三通道全 0 ⇒ 0/0）置 k=0，**不引入 ε 阈值**。 */
static inline void ratio_apply4(float32x4_t V[3], float32x4_t L, float32x4_t vmean) {
    float32x4_t L2 = L;
    for (uint32_t it = 0; it < g.iters; it++) {
        float32x4_t t = vsubq_f32(vmulq_f32(L2, L2), L2);
        L2 = vfmaq_f32(L2, t, vmean);      /* L2 + vmean*(L2²−L2) */
    }
    /* 快速倒数（见上方「保比语义的快速倒数」注释块）：FRECPE 起步 + 2 次 NR */
    const float32x4_t two = vdupq_n_f32(2.0f);
    float32x4_t recip = vrecpeq_f32(L);
    recip = vmulq_f32(recip, vfmaq_f32(two, vnegq_f32(L), recip));
    recip = vmulq_f32(recip, vfmaq_f32(two, vnegq_f32(L), recip));
    const uint32x4_t z = vceqq_f32(L, vdupq_n_f32(0.0f));
    const float32x4_t k = vbslq_f32(z, vdupq_n_f32(0.0f), vmulq_f32(L2, recip));
    for (int ch = 0; ch < 3; ch++) V[ch] = vmulq_f32(V[ch], k);
}

/* 写出一个 4 像素组：钳位 → 量化 → 交错存回 RGB888（与旧内联代码**同形**）。 */
static inline void store_group4(uint8_t* dst, uint32_t w, uint32_t y, uint32_t x,
                                float32x4_t V[3]) {
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

/* ── 保比语义：**批间交错**（software pipelining，恢复 ILP）─────────────────
 * 保比路径的 `L → L2(8 次) → recip(2 次 NR) → k` 是**单条串行依赖链**；逐向量独发时 CPU 只能
 * 等链尾 ⇒ 虽比旧语义少做大量曲线求值（旧 3 通道各 8 次 = 24 次 fma；新仅 8 次），却更慢约
 * 1.6 ms（函数级 apply-micro 1080×608：旧 19.69 → 新 20.84 ms）。本实现**一次交错 2 个 4 像素
 * 向量**，把两条独立链**交替发出**，让乱序/超标量核重叠它们 ⇒ 恢复 ILP。
 * **逐位一致是硬闸门（`--selfcheck`）**：交错只发生在**跨向量**之间，**每条 lane 内部的运算
 * 次序一字未改**（同一组 `vrecpeq_f32` / `vfmaq_f32`、同一迭代次数）；标量路径不变，其每像素
 * 次序仍与 NEON 每 lane 完全相同 ⇒ `mismatch_bytes=0`。
 * **尾部**：`w` 不足 8 的余数（仍为 4 的倍数，`apply_window` 只对 `w%4==0` 走 NEON）回退到
 * 单向量路径，不越界读写。
 */
static int apply_fused_neon_ratio(const uint8_t* rgb, const void* curve, uint8_t* dst,
                                  uint32_t w, uint32_t h) {
    const uint32_t CW = g.curve_w, CH_OFF = g.curve_h;
    ensure_taps(w, h);
    for (uint32_t y = 0; y < h; y++) {
        const int iy = g.yt[y].i0; const float wy = g.yt[y].w;
        uint32_t x = 0;
        for (; x + 8 <= w; x += 8) {                 /* 两向量交错（ILP） */
            float32x4_t Ca[3], Ra[3], Va[3], Cb[3], Rb[3], Vb[3];
            load_group4(rgb, curve, w, y, x, CW, CH_OFF, iy, wy, Ca, Ra, Va);
            load_group4(rgb, curve, w, y, x + 4, CW, CH_OFF, iy, wy, Cb, Rb, Vb);
            float32x4_t vma, La, vmb, Lb;
            ratio_head4(Ca, Ra, &vma, &La);
            ratio_head4(Cb, Rb, &vmb, &Lb);
            float32x4_t L2a = La, L2b = Lb;
            for (uint32_t it = 0; it < g.iters; it++) {   /* 两条链交替发出 */
                float32x4_t ta = vsubq_f32(vmulq_f32(L2a, L2a), L2a);
                float32x4_t tb = vsubq_f32(vmulq_f32(L2b, L2b), L2b);
                L2a = vfmaq_f32(L2a, ta, vma);
                L2b = vfmaq_f32(L2b, tb, vmb);
            }
            const float32x4_t two = vdupq_n_f32(2.0f);
            float32x4_t ra = vrecpeq_f32(La);
            float32x4_t rb = vrecpeq_f32(Lb);
            ra = vmulq_f32(ra, vfmaq_f32(two, vnegq_f32(La), ra));
            rb = vmulq_f32(rb, vfmaq_f32(two, vnegq_f32(Lb), rb));
            ra = vmulq_f32(ra, vfmaq_f32(two, vnegq_f32(La), ra));
            rb = vmulq_f32(rb, vfmaq_f32(two, vnegq_f32(Lb), rb));
            const uint32x4_t za = vceqq_f32(La, vdupq_n_f32(0.0f));
            const uint32x4_t zb = vceqq_f32(Lb, vdupq_n_f32(0.0f));
            const float32x4_t ka = vbslq_f32(za, vdupq_n_f32(0.0f), vmulq_f32(L2a, ra));
            const float32x4_t kb = vbslq_f32(zb, vdupq_n_f32(0.0f), vmulq_f32(L2b, rb));
            for (int ch = 0; ch < 3; ch++) {
                Va[ch] = vmulq_f32(Va[ch], ka);
                Vb[ch] = vmulq_f32(Vb[ch], kb);
            }
            store_group4(dst, w, y, x, Va);
            store_group4(dst, w, y, x + 4, Vb);
        }
        for (; x < w; x += 4) {                      /* 尾部：单向量路径 */
            float32x4_t C[3], Raw[3], V[3];
            load_group4(rgb, curve, w, y, x, CW, CH_OFF, iy, wy, C, Raw, V);
            float32x4_t vmean, L;
            ratio_head4(C, Raw, &vmean, &L);
            ratio_apply4(V, L, vmean);
            store_group4(dst, w, y, x, V);
        }
    }
    return CF_OK;
}

static int apply_fused_neon(const uint8_t* rgb, const void* curve, uint8_t* dst,
                            uint32_t w, uint32_t h) {
    const uint32_t CW = g.curve_w, CH_OFF = g.curve_h;
    ensure_taps(w, h);
    if (g.preserve_ratio) return apply_fused_neon_ratio(rgb, curve, dst, w, h);
    for (uint32_t y = 0; y < h; y++) {
        const int iy = g.yt[y].i0; const float wy = g.yt[y].w;
        for (uint32_t x = 0; x < w; x += 4) {
            float32x4_t C[3], Raw[3], V[3];
            load_group4(rgb, curve, w, y, x, CW, CH_OFF, iy, wy, C, Raw, V);
            for (uint32_t it = 0; it < g.iters; it++)
                for (int ch = 0; ch < 3; ch++) {
                    float32x4_t t = vsubq_f32(vmulq_f32(V[ch], V[ch]), V[ch]);
                    V[ch] = vfmaq_f32(V[ch], t, C[ch]);
                }
            store_group4(dst, w, y, x, V);
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
            float samp[3];
            for (uint32_t ch = 0; ch < 3; ch++)
                samp[ch] = cf_sample(curve, ch, ix, iy, wx, wy, CW, CH_OFF);
            if (g.preserve_ratio) {
                /* 保比语义：与 NEON 路径**同形**（同样的加法次序、fmaf、快速倒数）⇒ 逐位一致。
                   语义与理由见 apply_fused_neon 上方「保比语义的快速倒数」注释块。 */
                float kr, kg, kb;
                luma_coeffs(&kr, &kg, &kb);
                const float vmean = (samp[0] + samp[1] + samp[2]) * (1.0f / 3.0f);
                const float R = (float)p[0], G = (float)p[1], B = (float)p[2];
                const float L = ((kr * R + kg * G) + kb * B) * (1.0f / 255.0f);
                float L2 = L;
                for (uint32_t it = 0; it < g.iters; it++) L2 = fmaf(vmean, L2 * L2 - L2, L2);
                /* 快速倒数：估计量取自同一条 FRECPE（A64 无标量 FRECPE ⇒ 1 元向量取 lane 0），
                   NR 用显式 `fmaf`/乘（`-ffp-contract=off` 下与 NEON 的 `vfmaq_f32`/`vmulq_f32` 同形）。 */
                float recip = vgetq_lane_f32(vrecpeq_f32(vdupq_n_f32(L)), 0);
                recip = recip * fmaf(-L, recip, 2.0f);
                recip = recip * fmaf(-L, recip, 2.0f);
                const float k = (L == 0.0f) ? 0.0f : L2 * recip;   /* 仅 L==0 退化点置 0，无 ε */
                for (uint32_t ch = 0; ch < 3; ch++) {
                    float t = (float)p[ch] * (1.0f / 255.0f) * k;
                    if (t < 0.0f) t = 0.0f; else if (t > 1.0f) t = 1.0f;
                    o[ch] = (uint8_t)lrintf(t * 255.0f);
                }
            } else {
                for (uint32_t ch = 0; ch < 3; ch++) {
                    const float v = samp[ch];
                    float t = (float)p[ch] * (1.0f / 255.0f);
                    /* 与 NEON 的 `vfmaq_f32` 同形：显式融合乘加以保证两路逐位一致
                       （写成 `t = t + v*(t*t-t)` 时编译器是否融合取决于 -ffp-contract，实测会差 1 个字节）。 */
                    for (uint32_t it = 0; it < g.iters; it++) t = fmaf(v, t * t - t, t);
                    if (t < 0.0f) t = 0.0f; else if (t > 1.0f) t = 1.0f;
                    o[ch] = (uint8_t)lrintf(t * 255.0f);
                }
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
