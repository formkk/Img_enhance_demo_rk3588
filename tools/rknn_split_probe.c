/* rknn_split_probe.c —— 多 context 空间切分并行探针（用满 RK3588 的多核 NPU）
 *
 * 目的：把一帧 480×272 的曲线估计按**空间**切成 N 条带，各条带用独立 context 绑到不同 NPU 核上并行执行，
 *       绕开单核下"每层 memory-bound + 每层固定开销"造成的耗时墙（见 docs/evidence/M0-8-int8-retry.md §7）。
 * 等价性：本模型 7 次 3×3 ⇒ 感受野半径 = 7 px，故每条带输入左右各扩 HALO=7 px（外侧零填充），
 *       输出只取中间 w_out 列拼接；拼接结果可落盘，与单模型 480×272 输出逐元素比对。
 *
 * 用法：
 *   ./rknn_split_probe <slice_model.rknn> <full_uint8_nhwc.raw> <H> <W> <runs> <slices> <cores 如 0,1>
 *                      [float|nofloat] [dump_prefix]
 *   切片模型输入宽度须 ≥ W/slices + 2*HALO（本工程用 272×256）；输入 .raw = 整帧 uint8 NHWC，长 H*W*3。
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <pthread.h>
#include "rknn_api.h"

#define HALO 7
#define MAX_SLICES 4
#define MAX_RUNS 200

static int g_h = 0, g_full_w = 0, g_w_out = 0, g_w_in = 0, g_runs = 0, g_want_float = 1;
static const uint8_t* g_frame = NULL;

typedef struct {
    int idx;
    int start;                    /* 该条带缓冲区在整帧中的起始列（边缘条带与帧边界齐平） */
    rknn_context ctx;
    uint8_t* in_buf;
    rknn_input input;
    rknn_output output;
    float* out_float;
    int ret_init, ret_mask;
    double run_ms[MAX_RUNS];
    pthread_barrier_t* barrier;
} slice_t;

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static int cmp_double(const void* a, const void* b) {
    double x = *(const double*)a, y = *(const double*)b;
    return (x > y) - (x < y);
}

/* 组装本切片的输入：整帧列范围 [idx*w_out - HALO, idx*w_out - HALO + w_in)，越界补 0 */
static void build_slice_input(slice_t* s) {
    const int start = s->start;
    memset(s->in_buf, 0, (size_t)g_w_in * g_h * 3);
    for (int y = 0; y < g_h; y++) {
        for (int c = 0; c < g_w_in; c++) {
            const int xs = start + c;
            if (xs < 0 || xs >= g_full_w) continue;
            const uint8_t* src = g_frame + ((size_t)y * g_full_w + xs) * 3;
            uint8_t* dst = s->in_buf + ((size_t)y * g_w_in + c) * 3;
            dst[0] = src[0];
            dst[1] = src[1];
            dst[2] = src[2];
        }
    }
}

static void* worker(void* arg) {
    slice_t* s = (slice_t*)arg;
    build_slice_input(s);
    for (int i = 0; i < g_runs; i++) {
        pthread_barrier_wait(s->barrier);
        double t0 = now_ms();
        if (rknn_inputs_set(s->ctx, 1, &s->input) < 0 ||
            rknn_run(s->ctx, NULL) < 0 ||
            rknn_outputs_get(s->ctx, 1, &s->output, NULL) < 0) {
            s->run_ms[i] = -1;
        } else {
            s->run_ms[i] = now_ms() - t0;
            if (g_want_float && s->output.buf)
                memcpy(s->out_float, s->output.buf, (size_t)g_h * g_w_in * 3 * sizeof(float));
            rknn_outputs_release(s->ctx, 1, &s->output);
        }
        pthread_barrier_wait(s->barrier);
    }
    return NULL;
}

static void print_stats(const char* key, const double* v, int n) {
    double tmp[MAX_RUNS];
    memcpy(tmp, v, sizeof(double) * n);
    qsort(tmp, n, sizeof(double), cmp_double);
    printf("\"%s\":{\"min\":%.2f,\"median\":%.2f,\"p90\":%.2f,\"max\":%.2f}", key,
           tmp[0], tmp[n / 2], tmp[(int)(n * 0.9)], tmp[n - 1]);
}

int main(int argc, char** argv) {
    if (argc < 8) {
        fprintf(stderr, "usage: %s <slice_model.rknn> <full_uint8_nhwc.raw> <H> <W> <runs> <slices> <cores 0,1> "
                        "[float|nofloat] [dump_prefix]\n", argv[0]);
        return 2;
    }
    const char* model_path = argv[1];
    const char* frame_path = argv[2];
    g_h = atoi(argv[3]);
    g_full_w = atoi(argv[4]);
    g_runs = atoi(argv[5]);
    int n_slices = atoi(argv[6]);
    const char* cores = argv[7];
    g_want_float = !(argc > 8 && strcmp(argv[8], "nofloat") == 0);
    const char* dump_prefix = (argc > 9) ? argv[9] : NULL;
    if (n_slices < 1 || n_slices > MAX_SLICES || g_runs < 1 || g_runs > MAX_RUNS) return 2;
    if (g_full_w % n_slices) { fprintf(stderr, "W 必须能被 slices 整除\n"); return 2; }
    g_w_out = g_full_w / n_slices;
    g_w_in = (g_w_out + 2 * HALO + 7) / 8 * 8;      /* 按 8 对齐 */

    FILE* ff = fopen(frame_path, "rb");
    if (!ff) { perror("open frame"); return 3; }
    const size_t frame_bytes = (size_t)g_h * g_full_w * 3;
    uint8_t* frame = malloc(frame_bytes);
    if (!frame || fread(frame, 1, frame_bytes, ff) != frame_bytes) { perror("read frame"); return 3; }
    fclose(ff);
    g_frame = frame;

    FILE* fm = fopen(model_path, "rb");
    if (!fm) { perror("open model"); return 3; }
    fseek(fm, 0, SEEK_END);
    const long msz = ftell(fm);
    fseek(fm, 0, SEEK_SET);
    void* model = malloc(msz);
    if (!model || fread(model, 1, msz, fm) != (size_t)msz) { perror("read model"); return 3; }
    fclose(fm);

    int core_ids[MAX_SLICES] = {0, 1, 2, 0};
    {
        const char* p = cores;
        int k = 0;
        while (p && *p && k < n_slices) {
            core_ids[k++] = atoi(p);
            p = strchr(p, ',');
            if (p) p++;
        }
        for (int i = 0; i < n_slices; i++)
            if (core_ids[i] < 0 || core_ids[i] > 2) { fprintf(stderr, "核号须为 0/1/2\n"); return 2; }
    }
    const rknn_core_mask mask_of[3] = {RKNN_NPU_CORE_0, RKNN_NPU_CORE_1, RKNN_NPU_CORE_2};

    slice_t sl[MAX_SLICES];
    memset(sl, 0, sizeof(sl));
    pthread_barrier_t barrier;
    pthread_barrier_init(&barrier, NULL, n_slices);

    /* 关键：首条带左边缘、末条带右边缘与**帧边界齐平**——外侧交给模型自身的 conv padding，
       避免注入"边框零"（INT8 非对称量化下 uint8 0 ≠ 实值 0，会在边缘引入 ~0.1 的假偏差）。*/
    int starts[MAX_SLICES];
    for (int i = 0; i < n_slices; i++) {
        if (i == 0) starts[i] = 0;
        else if (i == n_slices - 1) starts[i] = g_full_w - g_w_in;
        else starts[i] = i * g_w_out + g_w_out / 2 - g_w_in / 2;
    }
    printf("{\"slice_w_in\":%d,\"slice_w_out\":%d,\"slices\":%d,\"halo\":%d,\"full_w\":%d,\"starts\":[",
           g_w_in, g_w_out, n_slices, HALO, g_full_w);
    for (int i = 0; i < n_slices; i++) printf("%s%d", i ? "," : "", starts[i]);
    printf("],\n");
    for (int i = 0; i < n_slices; i++) {
        sl[i].idx = i;
        sl[i].start = starts[i];
        if (sl[i].start < 0 || sl[i].start + g_w_in > g_full_w) { fprintf(stderr, "slice %d 越界\n", i); return 2; }
        sl[i].barrier = &barrier;
        sl[i].ret_init = rknn_init(&sl[i].ctx, model, (uint32_t)msz, 0, NULL);
        sl[i].ret_mask = (sl[i].ret_init == 0) ? rknn_set_core_mask(sl[i].ctx, mask_of[core_ids[i]]) : -1;
        sl[i].in_buf = calloc((size_t)g_w_in * g_h * 3, 1);
        memset(&sl[i].input, 0, sizeof(rknn_input));
        sl[i].input.index = 0;
        sl[i].input.buf = sl[i].in_buf;
        sl[i].input.size = (uint32_t)((size_t)g_w_in * g_h * 3);
        sl[i].input.type = RKNN_TENSOR_UINT8;
        sl[i].input.fmt = RKNN_TENSOR_NHWC;
        memset(&sl[i].output, 0, sizeof(rknn_output));
        sl[i].output.index = 0;
        sl[i].output.want_float = g_want_float ? 1 : 0;
        sl[i].out_float = malloc((size_t)g_h * g_w_in * 3 * sizeof(float));
        if (!sl[i].in_buf || !sl[i].out_float || sl[i].ret_init != 0) { fprintf(stderr, "slice %d 初始化失败\n", i); return 4; }
    }
    printf("\"init_ret\":[");
    for (int i = 0; i < n_slices; i++) printf("%s%d", i ? "," : "", sl[i].ret_init);
    printf("],\"core_mask_ret\":[");
    for (int i = 0; i < n_slices; i++) printf("%s%d", i ? "," : "", sl[i].ret_mask);
    printf("],\"cores\":[");
    for (int i = 0; i < n_slices; i++) printf("%s%d", i ? "," : "", core_ids[i]);
    printf("],\n");

    pthread_t th[MAX_SLICES];
    /* 预热（结果不计） */
    for (int i = 0; i < n_slices; i++) pthread_create(&th[i], NULL, worker, &sl[i]);
    for (int i = 0; i < n_slices; i++) pthread_join(th[i], NULL);
    /* 正式计时：整帧 wall = 同轮内最慢条带 */
    for (int i = 0; i < n_slices; i++) pthread_create(&th[i], NULL, worker, &sl[i]);
    for (int i = 0; i < n_slices; i++) pthread_join(th[i], NULL);

    double frame_wall[MAX_RUNS];
    for (int r = 0; r < g_runs; r++) {
        double mx = -1;
        for (int i = 0; i < n_slices; i++) if (sl[i].run_ms[r] > mx) mx = sl[i].run_ms[r];
        frame_wall[r] = mx;
    }
    print_stats("frame_wall_ms", frame_wall, g_runs);
    printf(",\n\"per_slice_ms\":[");
    for (int i = 0; i < n_slices; i++) {
        if (i) printf(",");
        print_stats("s", sl[i].run_ms, g_runs);
    }
    printf("],\n");

    if (dump_prefix && g_want_float) {
        const size_t n = (size_t)3 * g_h * g_full_w;
        float* stitch = calloc(n, sizeof(float));
        for (int i = 0; i < n_slices; i++) {
            for (int y = 0; y < g_h; y++) {
                for (int c = 0; c < g_w_out; c++) {
                    const int sc = i * g_w_out - sl[i].start + c;   /* 绝对值 i*w_out+c 在缓冲区中的列 */
                    if (sc < 0 || sc >= g_w_in) continue;
                    for (int ch = 0; ch < 3; ch++)
                        stitch[((size_t)ch * g_h + y) * g_full_w + i * g_w_out + c] =
                            sl[i].out_float[((size_t)ch * g_h + y) * g_w_in + sc];
                }
            }
        }
        char path[512];
        snprintf(path, sizeof(path), "%s_stitched.f32", dump_prefix);
        FILE* fo = fopen(path, "wb");
        if (fo) { fwrite(stitch, sizeof(float), n, fo); fclose(fo); printf("\"dump\":\"%s\",\n", path); }
        free(stitch);
    }
    printf("\"cfg\":{\"want_float\":%d}\n}\n", g_want_float);

    for (int i = 0; i < n_slices; i++) {
        free(sl[i].in_buf);
        free(sl[i].out_float);
        rknn_destroy(sl[i].ctx);
    }
    pthread_barrier_destroy(&barrier);
    free(model);
    free(frame);
    return 0;
}