/* rknn_core.c —— 见 rknn_core.h。自 tools/pipeline_probe.c 的 npu_* / slice_* 逐段移植。 */

#define _GNU_SOURCE
#include "rknn_core.h"

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "rknn_api.h"

typedef struct {
    int      idx;
    int      start;          /* 本条带在全局宽度上的起始列 */
    rknn_context ctx;
    uint8_t* in_buf;
    rknn_input  input;
    rknn_output output;
    float*   out_float;
    int      ret_init;
    int      ret_mask;
} slice_t;

static struct {
    int       ready;
    rknn_cfg_t cfg;
    slice_t   sl[RKNN_CORE_MAX_SLICES];
    pthread_t th[RKNN_CORE_MAX_SLICES];
    /* 会合协议：mutex + cond + 代次（停止信号与工作信号同锁发布，无竞态窗口） */
    pthread_mutex_t mtx;
    pthread_cond_t  cv;
    int gen, done, stop;
    const uint8_t* est_rgb;
    uint64_t last_estimate_ns;
    float*  slice_field;      /* 3 × slice_h × est_w 拼接缓冲（float32 中间域） */
    float*  ema_state;        /* 3 × eff_h × est_w，float32 中间域的 EMA 状态 */
} g;

static uint64_t mono_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void slice_build_input(slice_t* s) {
    const uint32_t W = g.cfg.est_w, WIN = g.cfg.slice_w_in, H = g.cfg.slice_h;
    for (uint32_t y = 0; y < H; y++) {
        const uint8_t* srcrow = g.est_rgb + (size_t)y * W * 3;
        uint8_t* dstrow = s->in_buf + (size_t)y * WIN * 3;
        for (uint32_t c = 0; c < WIN; c++) {
            const int xs = s->start + (int)c;
            if (xs < 0 || xs >= (int)W) {
                dstrow[c * 3 + 0] = 0; dstrow[c * 3 + 1] = 0; dstrow[c * 3 + 2] = 0;
            } else {
                dstrow[c * 3 + 0] = srcrow[xs * 3 + 0];
                dstrow[c * 3 + 1] = srcrow[xs * 3 + 1];
                dstrow[c * 3 + 2] = srcrow[xs * 3 + 2];
            }
        }
    }
}

static void* slice_worker(void* arg) {
    slice_t* s = (slice_t*)arg;
    int seen = 0;
    pthread_mutex_lock(&g.mtx);
    for (;;) {
        while (g.gen == seen && !g.stop) pthread_cond_wait(&g.cv, &g.mtx);
        if (g.stop) break;
        seen = g.gen;
        pthread_mutex_unlock(&g.mtx);

        slice_build_input(s);
        if (rknn_inputs_set(s->ctx, 1, &s->input) < 0 ||
            rknn_run(s->ctx, NULL) < 0 ||
            rknn_outputs_get(s->ctx, 1, &s->output, NULL) < 0) {
            /* 失败时把该条带输出清零，避免残留上一轮结果被当成新结果 */
            memset(s->out_float, 0, (size_t)g.cfg.slice_h * g.cfg.slice_w_in * 3 * sizeof(float));
        } else {
            memcpy(s->out_float, s->output.buf,
                   (size_t)g.cfg.slice_h * g.cfg.slice_w_in * 3 * sizeof(float));
            rknn_outputs_release(s->ctx, 1, &s->output);
        }

        pthread_mutex_lock(&g.mtx);
        g.done++;
        pthread_cond_broadcast(&g.cv);
    }
    pthread_mutex_unlock(&g.mtx);
    return NULL;
}

int rknn_estimate_init(const char* model_path, const rknn_cfg_t* cfg) {
    if (g.ready) return RKNN_CORE_E_STATE;
    if (!model_path || !cfg || cfg->n_slices == 0 || cfg->n_slices > RKNN_CORE_MAX_SLICES)
        return RKNN_CORE_E_STATE;
    g.cfg = *cfg;
    g.est_rgb = NULL;
    g.gen = g.done = g.stop = 0;
    g.last_estimate_ns = 0;
    pthread_mutex_init(&g.mtx, NULL);
    pthread_cond_init(&g.cv, NULL);

    FILE* fm = fopen(model_path, "rb");
    if (!fm) return RKNN_CORE_E_NPU;
    fseek(fm, 0, SEEK_END);
    const long msz = ftell(fm);
    fseek(fm, 0, SEEK_SET);
    void* model = malloc((size_t)msz);
    if (!model || fread(model, 1, (size_t)msz, fm) != (size_t)msz) { fclose(fm); free(model); return RKNN_CORE_E_NPU; }
    fclose(fm);

    g.slice_field = (float*)calloc((size_t)3 * cfg->slice_h * cfg->est_w, sizeof(float));
    g.ema_state = (float*)calloc((size_t)3 * cfg->eff_h * cfg->est_w, sizeof(float));
    if (!g.slice_field || !g.ema_state) { free(model); rknn_estimate_deinit(); return RKNN_CORE_E_NPU; }

    for (uint32_t i = 0; i < cfg->n_slices; i++) {
        slice_t* s = &g.sl[i];
        memset(s, 0, sizeof(*s));
        s->idx = (int)i;
        /* 首条带左边缘、末条带右边缘与帧边界齐平（外侧交给模型自身 padding） */
        s->start = (i == 0) ? 0 : (int)(cfg->est_w - cfg->slice_w_in);
        s->ret_init = rknn_init(&s->ctx, model, (uint32_t)msz, 0, NULL);
        s->ret_mask = (s->ret_init == 0 && cfg->core_mask[i])
                          ? rknn_set_core_mask(s->ctx, (rknn_core_mask)cfg->core_mask[i])
                          : -1;
        s->in_buf = (uint8_t*)calloc((size_t)cfg->slice_w_in * cfg->slice_h * 3, 1);
        s->input.index = 0;
        s->input.buf = s->in_buf;
        s->input.size = (uint32_t)((size_t)cfg->slice_w_in * cfg->slice_h * 3);
        s->input.type = RKNN_TENSOR_UINT8;
        s->input.fmt = RKNN_TENSOR_NHWC;
        s->output.index = 0;
        s->output.want_float = 1;
        s->out_float = (float*)malloc((size_t)cfg->slice_h * cfg->slice_w_in * 3 * sizeof(float));
        if (s->ret_init != 0 || !s->in_buf || !s->out_float) {
            free(model);
            rknn_estimate_deinit();
            return RKNN_CORE_E_NPU;
        }
        pthread_create(&g.th[i], NULL, slice_worker, s);
    }
    free(model);
    g.ready = 1;
    return RKNN_CORE_OK;
}

int rknn_estimate_ready(void) { return g.ready; }

int rknn_estimate(const uint8_t* est_rgb, void* dst, int dst_prec,
                  float alpha, int use_ema, int first) {
    if (!g.ready || !est_rgb || !dst) return RKNN_CORE_E_STATE;
    const rknn_cfg_t* c = &g.cfg;
    const uint64_t t0 = mono_ns();

    g.est_rgb = est_rgb;
    pthread_mutex_lock(&g.mtx);              /* 放开 worker：各自 build + rknn_run */
    g.done = 0;
    g.gen++;
    pthread_cond_broadcast(&g.cv);
    while (g.done < (int)c->n_slices) pthread_cond_wait(&g.cv, &g.mtx);
    pthread_mutex_unlock(&g.mtx);
    g.last_estimate_ns = mono_ns() - t0;

    /* 条带拼接（HALO 重叠区按 slice_w_out 归属切分） */
    memset(g.slice_field, 0, (size_t)3 * c->slice_h * c->est_w * sizeof(float));
    for (uint32_t i = 0; i < c->n_slices; i++) {
        slice_t* s = &g.sl[i];
        for (uint32_t y = 0; y < c->slice_h; y++) {
            for (uint32_t col = 0; col < c->slice_w_out; col++) {
                const int sc = (int)(i * c->slice_w_out) - s->start + (int)col;
                if (sc < 0 || sc >= (int)c->slice_w_in) continue;
                for (int ch = 0; ch < 3; ch++)
                    g.slice_field[((size_t)ch * c->slice_h + y) * c->est_w + i * c->slice_w_out + col] =
                        s->out_float[((size_t)ch * c->slice_h + y) * c->slice_w_in + (uint32_t)sc];
            }
        }
    }

    /* 只取有效区（丢弃 pad 行），在 **float32 中间域**按 §9.1 做 EMA（α 为历史权重） */
    const size_t plane = (size_t)c->eff_h * c->est_w;
    for (int ch = 0; ch < 3; ch++) {
        const float* src = g.slice_field + ((size_t)ch * c->slice_h + c->pad_top) * c->est_w;
        float* e = g.ema_state + (size_t)ch * plane;
        if (!use_ema || first) {
            memcpy(e, src, sizeof(float) * plane);
        } else {
            for (size_t k = 0; k < plane; k++) e[k] = alpha * e[k] + (1.0f - alpha) * src[k];
        }
    }
    /* 按发布域精度转换写出（§3.1 契约第 3 条：全档 FP16；U8 为可选） */
    if (dst_prec == RKNN_FIELD_FP16) {
        __fp16* o = (__fp16*)dst;
        for (size_t k = 0; k < 3 * plane; k++) o[k] = (__fp16)g.ema_state[k];
    } else if (dst_prec == RKNN_FIELD_U8) {
        uint8_t* o = (uint8_t*)dst;
        for (size_t k = 0; k < 3 * plane; k++) {
            float v = g.ema_state[k] * 255.0f + 0.5f;
            o[k] = (uint8_t)(v < 0.0f ? 0.0f : (v > 255.0f ? 255.0f : v));
        }
    } else {
        memcpy(dst, g.ema_state, sizeof(float) * 3 * plane);
    }
    return RKNN_CORE_OK;
}

void rknn_estimate_deinit(void) {
    if (g.ready) {
        pthread_mutex_lock(&g.mtx);          /* 停止信号与工作信号同锁发布 */
        g.stop = 1;
        pthread_cond_broadcast(&g.cv);
        pthread_mutex_unlock(&g.mtx);
        for (uint32_t i = 0; i < g.cfg.n_slices; i++) {
            pthread_join(g.th[i], NULL);
            if (g.sl[i].in_buf) free(g.sl[i].in_buf);
            if (g.sl[i].out_float) free(g.sl[i].out_float);
            if (g.sl[i].ctx) rknn_destroy(g.sl[i].ctx);
        }
        g.ready = 0;
    }
    free(g.slice_field);
    free(g.ema_state);
    g.slice_field = NULL;
    g.ema_state = NULL;
    pthread_mutex_destroy(&g.mtx);
    pthread_cond_destroy(&g.cv);
    g.est_rgb = NULL;
}

int32_t rknn_slice_mask_ret(uint32_t idx) {
    return (idx < g.cfg.n_slices) ? (int32_t)g.sl[idx].ret_mask : -1;
}
uint64_t rknn_last_estimate_ns(void) { return g.last_estimate_ns; }
uint32_t rknn_slice_count(void) { return g.ready ? g.cfg.n_slices : 0; }
