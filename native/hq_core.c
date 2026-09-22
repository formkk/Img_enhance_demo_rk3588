/* hq_core.c —— 见 hq_core.h。
 *
 * 与 tools/rknn_c_probe.c 同源的 RKNPU2 调用序（保证与对齐回归的 out_hq_*.f32 可复现）：
 *   rknn_init(flag=0) → rknn_set_core_mask → rknn_inputs_set → rknn_run → rknn_outputs_get
 * 无多线程：HQ 单帧整幅推理，TH3 本身在其执行流内阻塞调用。
 */

#define _GNU_SOURCE
#include "hq_core.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "rknn_api.h"

static uint64_t mono_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static struct {
    int              ready;
    hq_cfg_t         cfg;
    rknn_context     ctx;
    uint8_t*         in_buf;    /* 输入镜像（NHWC float32 [0,255]） */
    rknn_input       input;
    rknn_output      output;
    uint64_t         last_run_ns;
    int32_t          mask_ret;
} g = { .ready = 0, .ctx = 0, .in_buf = NULL, .last_run_ns = 0, .mask_ret = -1 };

int hq_init(const char* model_path, const hq_cfg_t* cfg) {
    if (g.ready) return HQ_CORE_E_STATE;
    if (!model_path || !cfg || cfg->w == 0 || cfg->h == 0) return HQ_CORE_E_STATE;
    g.cfg = *cfg;
    g.last_run_ns = 0;
    g.mask_ret = -1;

    FILE* fm = fopen(model_path, "rb");
    if (!fm) return HQ_CORE_E_NPU;
    fseek(fm, 0, SEEK_END);
    const long msz = ftell(fm);
    fseek(fm, 0, SEEK_SET);
    if (msz <= 0) { fclose(fm); return HQ_CORE_E_NPU; }
    void* model = malloc((size_t)msz);
    if (!model || fread(model, 1, (size_t)msz, fm) != (size_t)msz) {
        fclose(fm); free(model); return HQ_CORE_E_NPU;
    }
    fclose(fm);

    /* flag = 0：不开 perf 采集（RKNN_FLAG_COLLECT_PERF_MASK 自身有开销） */
    if (rknn_init(&g.ctx, model, (uint32_t)msz, 0, NULL) < 0) {
        free(model); g.ctx = 0; return HQ_CORE_E_NPU;
    }
    free(model);

    /* 分核（O36 = RKNN_NPU_CORE_2）；失败不致命，仅记录返回值供报告 */
    if (cfg->core_mask) g.mask_ret = rknn_set_core_mask(g.ctx, (rknn_core_mask)cfg->core_mask);

    const size_t in_elems = (size_t)cfg->w * cfg->h * 3;
    g.in_buf = (uint8_t*)calloc(in_elems, sizeof(float));
    if (!g.in_buf) { rknn_destroy(g.ctx); g.ctx = 0; return HQ_CORE_E_NPU; }

    memset(&g.input, 0, sizeof(g.input));
    g.input.index = 0;
    g.input.buf = g.in_buf;
    g.input.size = (uint32_t)(in_elems * sizeof(float));
    g.input.pass_through = 0;
    g.input.type = RKNN_TENSOR_FLOAT32;
    g.input.fmt = RKNN_TENSOR_NHWC;      /* 模型实测 in_attr.fmt = NHWC, dims = [1,H,W,3] */

    memset(&g.output, 0, sizeof(g.output));
    g.output.index = 0;
    g.output.want_float = 1;             /* 输出 float32 [0,1]，与对齐回归同口径 */

    g.ready = 1;
    return HQ_CORE_OK;
}

int hq_ready(void) { return g.ready; }

int hq_run(const float* in_rgb, float* out) {
    if (!g.ready || !in_rgb || !out) return HQ_CORE_E_STATE;
    const uint64_t t0 = mono_ns();

    /* 调用方缓冲 → 输入镜像（RKNN 的 buf 生命周期只需覆盖本次调用，但保留镜像便于复用/排错） */
    const size_t in_bytes = (size_t)g.cfg.w * g.cfg.h * 3 * sizeof(float);
    memcpy(g.in_buf, in_rgb, in_bytes);

    if (rknn_inputs_set(g.ctx, 1, &g.input) < 0) return HQ_CORE_E_NPU;
    if (rknn_run(g.ctx, NULL) < 0) return HQ_CORE_E_NPU;
    if (rknn_outputs_get(g.ctx, 1, &g.output, NULL) < 0) return HQ_CORE_E_NPU;

    /* 输出 = NCHW float32（1×3×H×W），直接整块拷出 */
    memcpy(out, g.output.buf, in_bytes);
    rknn_outputs_release(g.ctx, 1, &g.output);

    g.last_run_ns = mono_ns() - t0;
    return HQ_CORE_OK;
}

uint64_t hq_last_run_ns(void) { return g.last_run_ns; }
int32_t  hq_core_mask_ret(void) { return g.mask_ret; }

void hq_pack_rgb888_nhwc_f32(const uint8_t* rgb, float* out, uint32_t w, uint32_t h) {
    if (!rgb || !out) return;
    /* RGB888 与 NHWC 的**元素序一致**（都是逐像素 R,G,B 连续），故只需逐元素升 float，
       无需重排——这也是选 NHWC 而非 NCHW 输入的一个便利（模型实测 in_attr.fmt = NHWC）。 */
    const size_t n = (size_t)w * h * 3;
    for (size_t i = 0; i < n; i++) out[i] = (float)rgb[i];
}

/* 与 tools/hq_frame_review.py `f32_nchw_to_rgb888` 逐位同口径：`min(255, int(v*255.0)) if v > 0.0 else 0`。
   用 double 做乘法（Python 侧 `v * 255.0` 亦为 double），保证截断取整结果一致；无任何非线性后处理。 */
static uint8_t f32_to_u8(float v) {
    if (!(v > 0.0f)) return 0;   /* ≤ 0（含 -0 / NaN）→ 0，与 Python `v > 0.0` 判定一致 */
    const int t = (int)((double)v * 255.0);
    return (uint8_t)(t > 255 ? 255 : t);
}

int hq_nchw_f32_to_rgb888(const float* nchw, uint8_t* rgb, uint32_t w, uint32_t h,
                          uint32_t y0, uint32_t y1) {
    if (!nchw || !rgb) return HQ_CORE_E_STATE;
    if (w == 0 || h == 0) return HQ_CORE_E_STATE;
    if (y0 >= y1 || y1 > h) return HQ_CORE_E_STATE;
    const size_t hw = (size_t)w * h;
    for (uint32_t y = y0; y < y1; y++) {
        const size_t src = (size_t)y * w;
        uint8_t* dst = rgb + (size_t)(y - y0) * w * 3;
        for (uint32_t x = 0; x < w; x++) {
            dst[x * 3 + 0] = f32_to_u8(nchw[src + x]);          /* c = 0 → R */
            dst[x * 3 + 1] = f32_to_u8(nchw[hw + src + x]);      /* c = 1 → G */
            dst[x * 3 + 2] = f32_to_u8(nchw[2 * hw + src + x]);  /* c = 2 → B */
        }
    }
    return HQ_CORE_OK;
}

void hq_deinit(void) {
    if (!g.ready && g.ctx == 0) return;
    if (g.in_buf) { free(g.in_buf); g.in_buf = NULL; }
    if (g.ctx) { rknn_destroy(g.ctx); g.ctx = 0; }
    g.ready = 0;
    g.mask_ret = -1;
}