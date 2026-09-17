/* apply_probe.c —— 曲线应用「8 iter 单遍融合」微基准（M0-13：§6.1 的 ≤2 ms 行 / §11.1-13）
 *
 * 契约（主基准 §3.3 第 132 行 / §3.1 G1 第 3 条）：
 *   · 8 次迭代 y ← y + x_r·(y² − y) 必须**融合在单遍遍历内**：载入一次、寄存器内迭代 8 次、写回一次。
 *     未融合（每迭代独立读写一遍）@1080×608 @30fps ≈ 946 MB/s 额外 DDR 流量 ⇒ 直接判未达标。
 *   · 曲线场**双线性采样融合进同一循环**，**不生成独立上采样曲线图**；按目标像素中心 (i+0.5)/N
 *     的归一化坐标取样，边界 clamp。
 *
 * 用法：
 *   ./apply_probe <in.rgb> <W> <H> <curve.f32> <cw> <ch_h> <runs> [impl] [dump.rgb]
 *     in.rgb    : RGB888 交错，长 W*H*3（RGA 输出，值域 0..255）
 *     curve.f32 : 曲线场 float32，通道平面（NCHW）3 × ch_h × cw（**有效区**，已裁掉 pad 行）
 *     impl      : fused_neon（默认，C+NEON 单遍融合）/ fused_scalar（同算法标量）/ unfused（对照）
 *
 * 输出 JSON：三种 impl 各 min/median/p90、逐元素最大差（NEON vs 标量）、DDR 流量估算。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <math.h>
#ifdef __ARM_NEON
#include <arm_neon.h>
#endif

#define RUNS_MAX 64

static int W, H, CW, CH, ITERS = 8;
static const uint8_t* g_src;
static const float* g_curve;
static uint8_t* g_out;
static float* g_out_f;                      /* NEON/scalar 浮点结果，用于逐元素比对 */

typedef struct { int i0; float w; } tap_t;
static tap_t* g_xt;                          /* 每输出列 */
static tap_t* g_yt;                          /* 每输出行 */

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static int cmp_double(const void* a, const void* b) {
    double x = *(const double*)a, y = *(const double*)b;
    return (x > y) - (x < y);
}

static void stats(const char* key, double* v, int n, int last) {
    double t[RUNS_MAX];
    memcpy(t, v, sizeof(double) * n);
    qsort(t, n, sizeof(double), cmp_double);
    printf("\"%s\":{\"min\":%.3f,\"median\":%.3f,\"p90\":%.3f,\"max\":%.3f}%s", key,
           t[0], t[n / 2], t[(int)(n * 0.9)], t[n - 1], last ? "\n" : ",\n");
}

/* 采样表：目标像素中心 (i+0.5)/N 归一化 → 源坐标 (u*CW − 0.5)，i0 夹到 [0, CW−2]，w 夹到 [0,1] */
static void build_taps(tap_t* t, int N, int C) {
    for (int i = 0; i < N; i++) {
        float s = (float)((i + 0.5) * (double)C / (double)N - 0.5);
        int i0 = (int)floorf(s);
        float w = s - (float)i0;
        if (i0 < 0) { i0 = 0; w = 0.0f; }
        if (i0 > C - 2) { i0 = C - 2; w = (s > (float)(C - 1)) ? 0.0f : w; }
        if (w < 0.0f) w = 0.0f;
        if (w > 1.0f) w = 1.0f;
        t[i].i0 = i0; t[i].w = w;
    }
}

/* ---------- 标量单遍融合（数值参考） ---------- */
static void apply_fused_scalar(void) {
    const float* c0 = g_curve;
    const float* c1 = g_curve + (size_t)CH * CW;
    const float* c2 = g_curve + (size_t)2 * CH * CW;
    for (int y = 0; y < H; y++) {
        const int iy = g_yt[y].i0; const float wy = g_yt[y].w;
        const float* r0s[3] = {c0 + (size_t)iy * CW, c1 + (size_t)iy * CW, c2 + (size_t)iy * CW};
        for (int x = 0; x < W; x++) {
            const int ix = g_xt[x].i0; const float wx = g_xt[x].w;
            const uint8_t* p = g_src + ((size_t)y * W + x) * 3;
            for (int ch = 0; ch < 3; ch++) {
                const float* a = r0s[ch]; const float* b = a + CW;
                const float top = a[ix] + wx * (a[ix + 1] - a[ix]);
                const float bot = b[ix] + wx * (b[ix + 1] - b[ix]);
                const float v = top + wy * (bot - top);       /* 与 NEON 路径同形，便于逐位比对 */
                float t = (float)p[ch] * (1.0f / 255.0f);
                for (int it = 0; it < ITERS; it++) t = t + v * (t * t - t);
                if (t < 0.0f) t = 0.0f; else if (t > 1.0f) t = 1.0f;
                if (g_out_f) g_out_f[((size_t)y * W + x) * 3 + ch] = t;
                g_out[((size_t)y * W + x) * 3 + ch] = (uint8_t)lrintf(t * 255.0f);
            }
        }
    }
}

/* ---------- NEON 单遍融合（4 像素 × 3 通道一组；采样只做一次，8 次迭代在寄存器内） ---------- */
static void apply_fused_neon(void) {
#ifdef __ARM_NEON
    const float* cb[3] = {g_curve, g_curve + (size_t)CH * CW, g_curve + (size_t)2 * CH * CW};
    for (int y = 0; y < H; y++) {
        const int iy = g_yt[y].i0; const float wy = g_yt[y].w;
        const float* r[3][2];
        for (int ch = 0; ch < 3; ch++) {
            r[ch][0] = cb[ch] + (size_t)iy * CW;
            r[ch][1] = r[ch][0] + CW;
        }
        for (int x = 0; x < W; x += 4) {
            float rv[3][4];
            for (int ch = 0; ch < 3; ch++) {
                for (int l = 0; l < 4; l++) {
                    const int ix = g_xt[x + l].i0; const float wx = g_xt[x + l].w;
                    const float* a = r[ch][0]; const float* b = r[ch][1];
                    const float top = a[ix] + wx * (a[ix + 1] - a[ix]);
                    const float bot = b[ix] + wx * (b[ix + 1] - b[ix]);
                    rv[ch][l] = top + wy * (bot - top);
                }
            }
            float32x4_t C[3] = {vld1q_f32(rv[0]), vld1q_f32(rv[1]), vld1q_f32(rv[2])};
            /* 载入 4 像素并把每通道 4 个值聚成一条 4 车道向量（交错→平面） */
            float32x4_t V[3];
            {
                uint8_t tmp[3][4];
                for (int l = 0; l < 4; l++) {
                    const uint8_t* p = g_src + ((size_t)y * W + x + l) * 3;
                    tmp[0][l] = p[0]; tmp[1][l] = p[1]; tmp[2][l] = p[2];
                }
                for (int ch = 0; ch < 3; ch++) {
                    uint8x8_t u8 = vld1_u8(tmp[ch]);
                    V[ch] = vmulq_n_f32(vcvtq_f32_u32(vmovl_u16(vget_low_u16(vmovl_u8(u8)))),
                                        1.0f / 255.0f);
                }
            }
            for (int it = 0; it < ITERS; it++) {
                for (int ch = 0; ch < 3; ch++) {
                    float32x4_t t = vsubq_f32(vmulq_f32(V[ch], V[ch]), V[ch]);
                    V[ch] = vfmaq_f32(V[ch], t, C[ch]);
                }
            }
            for (int ch = 0; ch < 3; ch++) {
                V[ch] = vmaxq_f32(vminq_f32(V[ch], vdupq_n_f32(1.0f)), vdupq_n_f32(0.0f));
            }
            /* 写回：V[ch] 的 4 个车道 → 4 个像素的同名通道；先缩到 uint8 再交错 */
            uint8x8_t q[3];
            for (int ch = 0; ch < 3; ch++) {
                int32x4_t s = vcvtnq_s32_f32(vmulq_n_f32(V[ch], 255.0f));
                q[ch] = vqmovun_s16(vcombine_s16(vqmovn_s32(s), vqmovn_s32(s)));
            }
            uint8_t tmp[3][8];
            for (int ch = 0; ch < 3; ch++) vst1_u8(tmp[ch], q[ch]);
            for (int l = 0; l < 4; l++) {
                uint8_t* o = g_out + ((size_t)y * W + x + l) * 3;
                o[0] = tmp[0][l]; o[1] = tmp[1][l]; o[2] = tmp[2][l];
            }
        }
    }
#endif
}

/* ---------- 未融合对照：8 次迭代，每迭代独立载入/写回整幅（仅用于 DDR 流量对照） ---------- */
static void apply_unfused(void) {
    const float* c0 = g_curve;
    const float* c1 = g_curve + (size_t)CH * CW;
    const float* c2 = g_curve + (size_t)2 * CH * CW;
    for (int ch = 0; ch < 3; ch++) {                     /* 预展开到浮点缓冲，迭代回合都在整幅上跑 */
        const float* cc = (ch == 0) ? c0 : (ch == 1 ? c1 : c2);
        for (int y = 0; y < H; y++) {
            const int iy = g_yt[y].i0; const float wy = g_yt[y].w;
            const float* a = cc + (size_t)iy * CW; const float* b = a + CW;
            for (int x = 0; x < W; x++) {
                const int ix = g_xt[x].i0; const float wx = g_xt[x].w;
                const float top = a[ix] + wx * (a[ix + 1] - a[ix]);
                const float bot = b[ix] + wx * (b[ix + 1] - b[ix]);
                g_out_f[((size_t)y * W + x) * 3 + ch] = top + wy * (bot - top);
            }
        }
    }
    for (int it = 0; it < ITERS; it++) {                 /* 每迭代：读整幅 → 算 → 写整幅 */
        for (size_t i = 0; i < (size_t)W * H * 3; i++) {
            float t = g_out_f[i];
            g_out_f[i] = t + g_out_f[i] * (t * t - t);
        }
    }
    for (size_t i = 0; i < (size_t)W * H * 3; i++) {
        float t = g_out_f[i];
        if (t < 0.0f) t = 0.0f; else if (t > 1.0f) t = 1.0f;
        g_out[i] = (uint8_t)lrintf(t * 255.0f);
    }
}

int main(int argc, char** argv) {
    if (argc < 8) {
        fprintf(stderr, "usage: %s <in.rgb> <W> <H> <curve.f32> <cw> <ch_h> <runs> "
                        "[fused_neon|fused_scalar|unfused] [dump.rgb]\n", argv[0]);
        return 2;
    }
    const char* in_path = argv[1];
    W = atoi(argv[2]); H = atoi(argv[3]);
    const char* cv_path = argv[4];
    CW = atoi(argv[5]); CH = atoi(argv[6]);
    int runs = atoi(argv[7]);
    const char* impl = (argc > 8) ? argv[8] : "fused_neon";
    const char* dump = (argc > 9) ? argv[9] : NULL;
    if (argc > 10) ITERS = atoi(argv[10]);
    if (runs < 1) runs = 1;
    if (runs > RUNS_MAX) runs = RUNS_MAX;

    const size_t npx = (size_t)W * H * 3;
    uint8_t* src = (uint8_t*)malloc(npx);
    float* curve = (float*)malloc(sizeof(float) * 3 * CH * CW);
    uint8_t* out = (uint8_t*)malloc(npx);
    float* outf = (float*)malloc(sizeof(float) * npx);
    if (!src || !curve || !out || !outf) { fprintf(stderr, "oom\n"); return 3; }
    FILE* fi = fopen(in_path, "rb");
    if (!fi || fread(src, 1, npx, fi) != npx) { perror("read in"); return 3; }
    fclose(fi);
    FILE* fc = fopen(cv_path, "rb");
    if (!fc || fread(curve, sizeof(float), 3 * (size_t)CH * CW, fc) != 3 * (size_t)CH * CW) {
        perror("read curve"); return 3;
    }
    fclose(fc);
    g_src = src; g_curve = curve; g_out = out;

    g_xt = (tap_t*)malloc(sizeof(tap_t) * W);
    g_yt = (tap_t*)malloc(sizeof(tap_t) * H);
    build_taps(g_xt, W, CW);
    build_taps(g_yt, H, CH);

    printf("{\"geom\":{\"out\":\"%dx%d RGB888\",\"curve\":\"%dx%d x3 f32\"},\"iters\":%d,",
           W, H, CW, CH, ITERS);

    /* ① 标量单遍融合（数值参考 + 保底耗时） */
    g_out_f = NULL;
    double t_s[RUNS_MAX];
    for (int i = 0; i < runs; i++) { const double t0 = now_ms(); apply_fused_scalar(); t_s[i] = now_ms() - t0; }
    uint8_t* ref_out = (uint8_t*)malloc(npx); memcpy(ref_out, out, npx);
    stats("fused_scalar_ms", t_s, runs, 0);

    /* ② NEON 单遍融合 */
    double t_n[RUNS_MAX];
    for (int i = 0; i < runs; i++) { const double t0 = now_ms(); apply_fused_neon(); t_n[i] = now_ms() - t0; }
    stats("fused_neon_ms", t_n, runs, 0);
    {
        long diff = 0, mx = 0;
        for (size_t i = 0; i < npx; i++) { int d = abs((int)out[i] - (int)ref_out[i]); if (d) { diff++; if (d > mx) mx = d; } }
        printf("\"neon_vs_scalar\":{\"mismatch_bytes\":%ld,\"max_abs_delta\":%ld,\"total\":%zu},\n", diff, mx, npx);
    }

    /* ③ 未融合对照（DDR 流量对照，不追求速度） */
    g_out_f = outf;
    {
        const double t0 = now_ms();
        apply_unfused();
        const double t1 = now_ms() - t0;
        printf("\"unfused_ms\":%.3f,\n", t1);
    }
    g_out_f = NULL;

    /* ④ DDR 流量核算（按代码结构逐项累加，字节/帧 → MB/s @30fps） */
    {
        const double frame_b = (double)npx;                       /* 1.97 MB @1080×608 */
        const double fused = 2.0 * frame_b;                       /* 读 x 一次 + 写 y 一次 */
        const double unfused = 2.0 * frame_b * 8.0;               /* 8 次迭代各读+写一遍 */
        printf("\"ddr\":{\"frame_bytes\":%.0f,\"fused_bytes_per_frame\":%.0f,\"fused_MBps_at30\":%.1f,"
               "\"unfused_bytes_per_frame\":%.0f,\"unfused_MBps_at30\":%.1f,\"ratio\":%.1f},\n",
               frame_b, fused, fused * 30.0 / 1e6, unfused, unfused * 30.0 / 1e6, unfused / fused);
    }

    if (dump) {
        FILE* fo = fopen(dump, "wb");
        if (fo) { fwrite(out, 1, npx, fo); fclose(fo); printf("\"dump\":\"%s\",\n", dump); }
    }
    printf("\"impl\":\"%s\"}\n", impl);
    free(src); free(curve); free(out); free(outf); free(ref_out); free(g_xt); free(g_yt);
    return 0;
}