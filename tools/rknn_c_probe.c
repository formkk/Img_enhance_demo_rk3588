/* rknn_c_probe.c —— 用 RKNPU2 C API 做单模型性能剖析（M0-2 / 后续 C 运行时的最小骨架）
 *
 * 与 tools/rknn_probe.py 的分工：
 *   python 版（lite2）用于“能否加载 / 输出对齐 / 端到端耗时”；
 *   本 C 版用于“时间去哪了”——`rknn_run` 的真实 NPU 时长（RKNN_QUERY_PERF_RUN）与 wall 的差值
 *   即模型外的开销（输入/输出转换、格式转换、内存分配、cache flush），并用
 *   RKNN_QUERY_PERF_DETAIL 给出**逐层**耗时，直接判定瓶颈层。
 *
 * 用法：
 *   ./rknn_c_probe <model.rknn> <input.f32> <runs> [nhwc|nchw] [float|nofloat] [sram|nosram]
 *   input.f32 = 原始 float32 数据，NHWC 或 NCHW 由第 4 参数决定，值域 [0,255]（与转换时的 mean/std 约定一致）
 *
 * 输出：JSON（wall 统计、run_duration 统计、逐层明细字符串、内存统计）
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include "rknn_api.h"

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static int cmp_double(const void* a, const void* b) {
    double x = *(const double*)a, y = *(const double*)b;
    return (x > y) - (x < y);
}

int main(int argc, char** argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: %s <model.rknn> <input.f32> <runs> [nhwc|nchw] [float|nofloat] [sram|nosram] [u8|f32] [perf|noperf]\n", argv[0]);
        return 2;
    }
    const char* model_path = argv[1];
    const char* input_path = argv[2];
    int runs = atoi(argv[3]);
    int use_nhwc = (argc > 4 && strcmp(argv[4], "nchw") != 0);
    int want_float = !(argc > 5 && strcmp(argv[5], "nofloat") == 0);
    int use_sram = (argc > 6 && strcmp(argv[6], "sram") == 0);
    int use_u8 = (argc > 7 && strcmp(argv[7], "u8") == 0);   /* 输入按 uint8 提供（4× 少数据） */
    int noperf = (argc > 8 && strcmp(argv[8], "noperf") == 0);

    FILE* fp = fopen(model_path, "rb");
    if (!fp) { perror("open model"); return 3; }
    fseek(fp, 0, SEEK_END);
    long model_size = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    void* model = malloc(model_size);
    if (fread(model, 1, model_size, fp) != (size_t)model_size) { perror("read model"); return 3; }
    fclose(fp);

    uint32_t flag = 0;
    if (!noperf) flag |= RKNN_FLAG_COLLECT_PERF_MASK;   /* perf 采集本身有开销，测真实耗时时关掉 */
    if (use_sram) flag |= RKNN_FLAG_ENABLE_SRAM;

    rknn_context ctx = 0;
    int ret = rknn_init(&ctx, model, (uint32_t)model_size, flag, NULL);
    if (ret < 0) { fprintf(stderr, "rknn_init failed: %d\n", ret); return 4; }

    rknn_sdk_version ver;
    if (rknn_query(ctx, RKNN_QUERY_SDK_VERSION, &ver, sizeof(ver)) == 0)
        printf("{\"api\":\"%s\",\"drv\":\"%s\",\n", ver.api_version, ver.drv_version);

    rknn_input_output_num io_num;
    rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM, &io_num, sizeof(io_num));
    rknn_tensor_attr in_attr, out_attr;
    memset(&in_attr, 0, sizeof(in_attr));  in_attr.index = 0;
    memset(&out_attr, 0, sizeof(out_attr)); out_attr.index = 0;
    rknn_query(ctx, RKNN_QUERY_INPUT_ATTR, &in_attr, sizeof(in_attr));
    rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &out_attr, sizeof(out_attr));
    printf("\"in_elems\":%u,\"out_elems\":%u,\"n_input\":%u,\"n_output\":%u,\n",
           in_attr.n_elems, out_attr.n_elems, io_num.n_input, io_num.n_output);

    rknn_mem_size mem_size;
    if (rknn_query(ctx, RKNN_QUERY_MEM_SIZE, &mem_size, sizeof(mem_size)) == 0)
        printf("\"mem\":{\"weight\":%u,\"internal\":%u,\"dma\":%lu,\"sram\":%u,\"sram_free\":%u},\n",
               mem_size.total_weight_size, mem_size.total_internal_size,
               (unsigned long)mem_size.total_dma_allocated_size,
               mem_size.total_sram_size, mem_size.free_sram_size);

    /* 读输入数据（float32） */
    FILE* fi = fopen(input_path, "rb");
    if (!fi) { perror("open input"); return 5; }
    fseek(fi, 0, SEEK_END);
    long in_bytes = ftell(fi);
    fseek(fi, 0, SEEK_SET);
    void* in_buf = malloc(in_bytes);
    if (fread(in_buf, 1, in_bytes, fi) != (size_t)in_bytes) { perror("read input"); return 5; }
    fclose(fi);

    rknn_input inputs[1];
    memset(inputs, 0, sizeof(inputs));
    inputs[0].index = 0;
    inputs[0].buf = in_buf;
    inputs[0].pass_through = 0;
    inputs[0].type = use_u8 ? RKNN_TENSOR_UINT8 : RKNN_TENSOR_FLOAT32;
    if (use_u8) {                                           /* float32 的 0..255 → uint8（值等价） */
        uint8_t* u8 = (uint8_t*)malloc(in_bytes / 4);
        const float* f = (const float*)in_buf;
        for (long i = 0; i < in_bytes / 4; i++) u8[i] = (uint8_t)(f[i] < 0 ? 0 : (f[i] > 255 ? 255 : f[i]));
        free(in_buf);
        in_buf = u8;
        in_bytes /= 4;
        inputs[0].buf = in_buf;                             /* 必须指向新缓冲，否则 use-after-free */
    }
    inputs[0].fmt = use_nhwc ? RKNN_TENSOR_NHWC : RKNN_TENSOR_NCHW;
    inputs[0].size = (uint32_t)in_bytes;
    rknn_output outputs[1];
    memset(outputs, 0, sizeof(outputs));
    outputs[0].index = 0;
    outputs[0].want_float = want_float ? 1 : 0;

    double wall[512], runus[512];
    if (runs > 512) runs = 512;
    int ok = 0;
    for (int i = 0; i < runs; i++) {
        double t0 = now_ms();
        if (rknn_inputs_set(ctx, 1, inputs) < 0) { fprintf(stderr, "inputs_set failed\n"); return 6; }
        if (rknn_run(ctx, NULL) < 0) { fprintf(stderr, "run failed\n"); return 6; }
        if (rknn_outputs_get(ctx, 1, outputs, NULL) < 0) { fprintf(stderr, "outputs_get failed\n"); return 6; }
        wall[i] = now_ms() - t0;
        rknn_perf_run perf_run;
        if (rknn_query(ctx, RKNN_QUERY_PERF_RUN, &perf_run, sizeof(perf_run)) == 0)
            runus[i] = (double)perf_run.run_duration / 1000.0;   /* µs → ms */
        else
            runus[i] = -1;
        rknn_outputs_release(ctx, 1, outputs);
        ok++;
    }

    double w[512], r[512];
    memcpy(w, wall, sizeof(double) * ok);
    memcpy(r, runus, sizeof(double) * ok);
    qsort(w, ok, sizeof(double), cmp_double);
    qsort(r, ok, sizeof(double), cmp_double);
    (void)runs;
    printf("\"n\":%d,\n", ok);
    printf("\"wall_ms\":{\"min\":%.2f,\"median\":%.2f,\"p90\":%.2f,\"max\":%.2f},\n",
           w[0], w[ok / 2], w[(int)(ok * 0.9)], w[ok - 1]);
    printf("\"npu_run_ms\":{\"min\":%.2f,\"median\":%.2f,\"p90\":%.2f,\"max\":%.2f},\n",
           r[0], r[ok / 2], r[(int)(ok * 0.9)], r[ok - 1]);
    printf("\"overhead_ms_median\":%.2f,\n", w[ok / 2] - r[ok / 2]);
    printf("\"cfg\":{\"fmt\":\"%s\",\"want_float\":%d,\"sram\":%d,\"in_type\":\"%s\",\"perf_collect\":%d}\n}\n",
           use_nhwc ? "nhwc" : "nchw", want_float, use_sram, use_u8 ? "uint8" : "float32", !noperf);

    if (argc > 9 && want_float) {          /* 落盘输出（float32），便于与切分结果逐元素比对 */
        FILE* fo = fopen(argv[9], "wb");
        if (fo) {
            /* 末尾一次循环的输出 buf 已 release，故此处重跑一次并保留 buf */
            rknn_inputs_set(ctx, 1, inputs);
            rknn_run(ctx, NULL);
            if (rknn_outputs_get(ctx, 1, outputs, NULL) == 0) {
                fwrite(outputs[0].buf, 1, outputs[0].size, fo);
                printf("\"dump\":\"%s\",\n", argv[9]);
            }
            fclose(fo);
        }
    }

    rknn_perf_detail detail;
    if (!noperf && rknn_query(ctx, RKNN_QUERY_PERF_DETAIL, &detail, sizeof(detail)) == 0 && detail.perf_data) {
        printf("---PERF_DETAIL---\n%s\n", detail.perf_data);
    }

    free(in_buf);
    free(model);
    rknn_destroy(ctx);
    return 0;
}