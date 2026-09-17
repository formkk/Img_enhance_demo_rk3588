/* rga_probe.c —— RGA 几何链路探针（M0-13：§11.1-2 零填充实现参数 + §11.1-13 RGA 多核确认）
 *
 * 目标几何（服务主基准 §3.1 G1）：
 *   1920×1080 NV12 --(scale + CSC，一次 blit)--> 有效区 480×270 RGB888（落在 480×272 缓冲的第 1..270 行）
 *   480×272 缓冲的上下各 1 行 pad = 零填充
 *
 * 为什么用 DMA-BUF（fd）而不是虚拟地址：
 *   生产链路是 `采集 1080p NV12 (DMABUF)`（§3.3），且实测 VA 路径会触发
 *   `rga_mm: RGA_MMU unsupported memory larger than 4G!` / `scheduler core[4] unsupported mm_flag[0x8]`
 *   —— VA 路径在 RGA3 上的映射能力受限，用它测出的分核/时序不代表生产。故本探针默认走 dma_heap(fd)。
 *
 * 用法：
 *   ./rga_probe <in_nv12.raw> <in_w> <in_h> <out_w> <out_h> <runs>
 *               [core_mask 0|1|2|3] [buf va|fd] [heap system-dma32|system|cma|cma-uncached]
 *               [dual sync|async] [dump.rgb|-] [json|-]
 *
 *   core_mask : 0 = 不调用 imconfig（沿用库默认 = RGA3 CORE0 单核）；1 = CORE0；2 = CORE1；3 = CORE0|CORE1
 *   buf       : fd（默认，生产口径）/ va（仅作对照）
 *   dual      : 双 job 并发测试的提交方式；async = A 线程非阻塞提交、B 线程同步提交（探并行是否被提交方式限制）
 *
 * 实测要点（本平台 librga 2.2.0 / 运行时 2.1.0，api 1.9.2_[1]）：
 *   · imfill（colorfill）对 RGB888 **直接失败**（IM_STATUS_FAILED，驱动 RGA_COLORFILL Invalid argument）
 *     ⇒ 零填充只能走「improcess 从零源块拷入」；且 RGA 不接受高度 < 2 的矩形，故 pad 按 2 行填。
 *   · blit 只写 drect 覆盖的行 ⇒ pad 行**只在缓冲初始化时填一次**，每帧零成本（哨兵实测验证）。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <pthread.h>
#include "im2d.h"

#define RUNS_MAX   512
#define PAD_TOP    1
#define PAD_BOT    1
#define FILL_H     2                 /* RGA 不接受高度 <2 的矩形 */

/* dma-buf heap 分配（避免依赖 linux/dma-heap.h 的可用性） */
struct dma_heap_alloc_data {
    uint64_t len;
    uint32_t fd;
    uint32_t fd_flags;
    uint64_t heap_flags;
};
#define DMA_HEAP_IOC_MAGIC  'H'
#define DMA_HEAP_IOCTL_ALLOC _IOWR(DMA_HEAP_IOC_MAGIC, 0x0, struct dma_heap_alloc_data)

static FILE* g_out = NULL;
static int g_in_w, g_in_h, g_out_w, g_out_h, g_eff_h, g_runs;
static rga_buffer_t g_src, g_dst_full, g_zero_full, g_dst_eff;
static int g_use_eff_view = 0;   /* 1 = blit 打到「偏移基址子视图」(drect dy=0)；0 = 打到整幅 + drect dy 偏移 */
static uint8_t* g_dst_va = NULL;              /* 仅用于伪造哨兵/校验（fd 模式下是 mmap 映射） */
static size_t g_dst_stride = 0;
static rga_buffer_t g_job_dst[2];
static int g_dual_async = 0;
static int g_blit_dy = PAD_TOP;      /* blit 的 drect y 偏移（=有效区起始行）；实测 RGA 可能要求偶对齐 */
static pthread_barrier_t g_bar;

/* ---------- dma-buf ---------- */
static int dma_heap_alloc(const char* heap, size_t len, void** va) {
    char path[128];
    snprintf(path, sizeof(path), "/dev/dma_heap/%s", heap);
    int hfd = open(path, O_RDWR | O_CLOEXEC);
    if (hfd < 0) { perror(path); return -1; }
    struct dma_heap_alloc_data d;
    memset(&d, 0, sizeof(d));
    d.len = len;
    d.fd_flags = O_RDWR | O_CLOEXEC;
    if (ioctl(hfd, DMA_HEAP_IOCTL_ALLOC, &d) < 0) { perror("DMA_HEAP_IOCTL_ALLOC"); close(hfd); return -1; }
    close(hfd);
    if (va) {
        void* p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, d.fd, 0);
        if (p == MAP_FAILED) { perror("mmap dmabuf"); close(d.fd); return -1; }
        *va = p;
    }
    return (int)d.fd;
}

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static int cmp_double(const void* a, const void* b) {
    double x = *(const double*)a, y = *(const double*)b;
    return (x > y) - (x < y);
}

static void print_stats(const char* key, const double* v, int n) {
    double tmp[RUNS_MAX];
    memcpy(tmp, v, sizeof(double) * n);
    qsort(tmp, n, sizeof(double), cmp_double);
    fprintf(g_out, "\"%s\":{\"min\":%.3f,\"median\":%.3f,\"p90\":%.3f,\"max\":%.3f}", key,
            tmp[0], tmp[n / 2], tmp[(int)(n * 0.9)], tmp[n - 1]);
}

/* 一次 blit：源全幅 --scale+CSC--> 目标有效区。
   实测：drect 的 y 偏移必须**偶对齐**（y=1 直接 -3 拒绝），故 dy=1 的「上下各 1 行 pad」布局
   只能用偏移基址子视图（drect dy=0）实现，不能用 drect dy=1。 */
static IM_STATUS blit_to(rga_buffer_t dst, int usage) {
    im_rect srect = {0, 0, g_in_w, g_in_h};
    im_rect drect = g_use_eff_view ? (im_rect){0, 0, g_out_w, g_eff_h}
                                   : (im_rect){0, g_blit_dy, g_out_w, g_out_h - PAD_TOP - PAD_BOT};
    im_rect prect = {0, 0, 0, 0};
    rga_buffer_t pat;
    memset(&pat, 0, sizeof(pat));
    return improcess(g_src, dst, pat, srect, drect, prect, usage);
}

static IM_STATUS blit_once(void) {
    return blit_to(g_use_eff_view ? g_dst_eff : g_dst_full, IM_SYNC);
}

/* pad 零填充：从零源块拷入上下各 2 行（重叠行随后被 blit 覆盖）——初始化时一次性完成 */
static IM_STATUS pad_fill_once(void) {
    im_rect top = {0, 0, g_out_w, FILL_H};
    im_rect bot = {0, g_out_h - FILL_H, g_out_w, FILL_H};
    im_rect prect = {0, 0, 0, 0};
    rga_buffer_t pat;
    memset(&pat, 0, sizeof(pat));
    IM_STATUS s = improcess(g_zero_full, g_dst_full, pat, top, top, prect, IM_SYNC);
    if (s != IM_STATUS_SUCCESS) return s;
    return improcess(g_zero_full, g_dst_full, pat, bot, bot, prect, IM_SYNC);
}

/* ---------- 双 job 并发 ---------- */
typedef struct {
    int idx;
    rga_buffer_t dst;
    int async_submit;
    double ms[RUNS_MAX];
} job_t;

static void* job_worker(void* arg) {
    job_t* j = (job_t*)arg;
    for (int i = 0; i < g_runs; i++) {
        pthread_barrier_wait(&g_bar);
        const double t0 = now_ms();
        const int usage = (g_dual_async && j->async_submit) ? IM_ASYNC : IM_SYNC;
        IM_STATUS st = blit_to(j->dst, usage);
        j->ms[i] = (st == IM_STATUS_SUCCESS) ? (now_ms() - t0) : -1.0;
        pthread_barrier_wait(&g_bar);
        if (g_dual_async && j->async_submit) usleep(4000);   /* 给非阻塞 job 留出完成窗口，防积压 */
    }
    return NULL;
}

int main(int argc, char** argv) {
    if (argc < 7) {
        fprintf(stderr, "usage: %s <in_nv12.raw> <in_w> <in_h> <out_w> <out_h> <runs> "
                        "[core_mask] [va|fd] [heap] [sync|async] [dump.rgb|-] [json|-]\n", argv[0]);
        return 2;
    }
    const char* in_path = argv[1];
    g_in_w  = atoi(argv[2]);
    g_in_h  = atoi(argv[3]);
    g_out_w = atoi(argv[4]);
    g_out_h = atoi(argv[5]);
    g_runs  = atoi(argv[6]);
    const int core_mask   = (argc > 7) ? atoi(argv[7]) : 0;
    const char* buf_mode  = (argc > 8) ? argv[8] : "fd";          /* va | fd | fdva */
    const int use_dmabuf  = (strcmp(buf_mode, "va") != 0);        /* fd / fdva → 走 dma_heap */
    g_use_eff_view        = (strcmp(buf_mode, "fd") != 0);        /* va / fdva → 偏移基址子视图 */
    const int use_fd      = use_dmabuf;
    const char* heap      = (argc > 9) ? argv[9] : "system-dma32";
    g_dual_async          = (argc > 10 && strcmp(argv[10], "async") == 0);
    const char* dump_path = (argc > 11 && strcmp(argv[11], "-") != 0) ? argv[11] : NULL;
    const char* json_path = (argc > 12 && strcmp(argv[12], "-") != 0) ? argv[12] : NULL;
    if (argc > 13) g_blit_dy = atoi(argv[13]);
    if (g_runs < 1) g_runs = 1;
    if (g_runs > RUNS_MAX) g_runs = RUNS_MAX;
    g_eff_h = g_out_h - PAD_TOP - PAD_BOT;

    g_out = stdout;
    if (json_path) {
        g_out = fopen(json_path, "w");
        if (!g_out) { perror("open json"); return 3; }
    }

    const size_t src_bytes = (size_t)g_in_w * g_in_h * 3 / 2;
    const size_t dst_stride = (size_t)g_out_w * 3;
    const size_t dst_bytes  = dst_stride * g_out_h;

    FILE* fi = fopen(in_path, "rb");
    if (!fi) { perror("open input"); return 3; }

    /* ---- src ---- */
    int src_fd = -1;
    uint8_t* src = NULL;
    if (use_fd) {
        src_fd = dma_heap_alloc(heap, src_bytes, (void**)&src);
        if (src_fd < 0) return 4;
    } else {
        src = (uint8_t*)malloc(src_bytes);
    }
    if (fread(src, 1, src_bytes, fi) != src_bytes) { perror("read input"); return 3; }
    fclose(fi);

    /* ---- dst A/B + 零源块 ---- */
    int dst_fd[2] = {-1, -1}, zero_fd = -1;
    uint8_t* dstB = NULL;
    uint8_t* zero = NULL;
    if (use_fd) {
        dst_fd[0] = dma_heap_alloc(heap, dst_bytes, (void**)&g_dst_va);
        dst_fd[1] = dma_heap_alloc(heap, dst_bytes, (void**)&dstB);
        zero_fd   = dma_heap_alloc(heap, dst_bytes, (void**)&zero);
        if (dst_fd[0] < 0 || dst_fd[1] < 0 || zero_fd < 0) return 4;
    } else {
        g_dst_va = (uint8_t*)malloc(dst_bytes);
        dstB     = (uint8_t*)malloc(dst_bytes);
        zero     = (uint8_t*)calloc(1, dst_bytes);
    }
    memset(g_dst_va, 0xAA, dst_bytes);      /* 哨兵：未写入处保持 0xAA，可证明 blit 的写入范围 */
    memset(dstB, 0xAA, dst_bytes);
    if (use_fd) memset(zero, 0, dst_bytes);
    g_dst_stride = dst_stride;

    if (use_fd) {
        g_src       = wrapbuffer_fd_t(src_fd, g_in_w, g_in_h, g_in_w, g_in_h, RK_FORMAT_YCbCr_420_SP);
        g_dst_full  = wrapbuffer_fd_t(dst_fd[0], g_out_w, g_out_h, g_out_w, g_out_h, RK_FORMAT_RGB_888);
        g_zero_full = wrapbuffer_fd_t(zero_fd, g_out_w, g_out_h, g_out_w, g_out_h, RK_FORMAT_RGB_888);
    } else {
        g_src       = wrapbuffer_virtualaddr_t(src, g_in_w, g_in_h, g_in_w, g_in_h, RK_FORMAT_YCbCr_420_SP);
        g_dst_full  = wrapbuffer_virtualaddr_t(g_dst_va, g_out_w, g_out_h, g_out_w, g_out_h, RK_FORMAT_RGB_888);
        g_zero_full = wrapbuffer_virtualaddr_t(zero, g_out_w, g_out_h, g_out_w, g_out_h, RK_FORMAT_RGB_888);
    }

    if (g_use_eff_view) {
        /* 偏移基址子视图：有效区 480×270 落在整幅 480×272 的第 1..270 行
           （drect 无法做 dy=1，实测 dy 必须偶对齐；故用基址偏移 + hstride 报整幅高） */
        const size_t off = dst_stride * PAD_TOP;
        g_dst_eff = wrapbuffer_virtualaddr_t(g_dst_va + off, g_out_w, g_eff_h,
                                             g_out_w, g_out_h, RK_FORMAT_RGB_888);
        g_job_dst[0] = g_dst_eff;
        g_job_dst[1] = wrapbuffer_virtualaddr_t(dstB + off, g_out_w, g_eff_h,
                                                g_out_w, g_out_h, RK_FORMAT_RGB_888);
    } else if (use_fd) {
        g_job_dst[0] = g_dst_full;
        g_job_dst[1] = wrapbuffer_fd_t(dst_fd[1], g_out_w, g_out_h, g_out_w, g_out_h, RK_FORMAT_RGB_888);
    } else {
        g_job_dst[0] = g_dst_full;
        g_job_dst[1] = wrapbuffer_virtualaddr_t(dstB, g_out_w, g_out_h, g_out_w, g_out_h, RK_FORMAT_RGB_888);
    }

    fprintf(g_out, "{\"api_header\":\"%s\",", RGA_API_VERSION);
    fprintf(g_out, "\"geom\":{\"src\":\"%dx%d NV12\",\"dst\":\"%dx%d RGB888\",\"eff\":\"%dx%d\",\"pad_rows\":%d},",
            g_in_w, g_in_h, g_out_w, g_out_h, g_out_w, g_eff_h, PAD_TOP);
    fprintf(g_out, "\"core_mask\":%d,\"buf\":\"%s\",\"heap\":\"%s\",\"dual\":\"%s\",\"blit_dy\":%d,",
            core_mask, buf_mode, use_fd ? heap : "-", g_dual_async ? "async" : "sync", g_blit_dy);

    if (core_mask) {
        const IM_STATUS cfg = imconfig(IM_CONFIG_SCHEDULER_CORE, (uint64_t)core_mask);
        fprintf(g_out, "\"imconfig_ret\":%d,\"imconfig_str\":\"%s\",", (int)cfg, imStrError_t(cfg));
    } else {
        fprintf(g_out, "\"imconfig_ret\":null,\"imconfig_str\":\"skipped(mask=0)\",");
    }

    /* ---- ① pad 零填充（一次性；实测 blit 不越写 pad 行，故每帧无需重填） ---- */
    double pad_ms[RUNS_MAX], blit_ms[RUNS_MAX], path_ms[RUNS_MAX];
    IM_STATUS st = pad_fill_once();
    fprintf(g_out, "\"pad_fill_ret\":%d,\"pad_fill_str\":\"%s\",", (int)st, imStrError_t(st));
    for (int i = 0; i < g_runs; i++) {
        const double t0 = now_ms();
        pad_fill_once();
        pad_ms[i] = now_ms() - t0;
    }
    print_stats("pad_fill_ms", pad_ms, g_runs);
    fprintf(g_out, ",");
    pad_fill_once();                         /* 恢复到已填状态，再测纯 blit */

    /* ---- ② 单次 blit（scale+CSC；生产实现：pad 已由缓冲初始化填好） ---- */
    IM_STATUS last = IM_STATUS_SUCCESS;
    for (int i = 0; i < g_runs; i++) {
        const double t0 = now_ms();
        last = blit_once();
        blit_ms[i] = now_ms() - t0;
    }
    fprintf(g_out, "\"blit_ret\":%d,\"blit_str\":\"%s\",", (int)last, imStrError_t(last));
    print_stats("blit_ms", blit_ms, g_runs);
    fprintf(g_out, ",");

    /* ---- ③ 每帧路径（fill + blit，仅作对照：生产不在每帧填 pad） ---- */
    for (int i = 0; i < g_runs; i++) {
        const double t0 = now_ms();
        pad_fill_once();
        blit_once();
        path_ms[i] = now_ms() - t0;
    }
    print_stats("frame_path_ms", path_ms, g_runs);
    fprintf(g_out, ",");

    /* ---- ④ 校验：pad 行须为 0；blit 不得越写；有效区不退化 ---- */
    {
        pad_fill_once();
        blit_once();
        long nz_top = 0, nz_bot = 0, nz_out = 0;
        for (size_t k = 0; k < dst_stride; k++) {
            if (g_dst_va[k] != 0) nz_top++;
            if (g_dst_va[dst_stride * (g_out_h - 1) + k] != 0) nz_bot++;
        }
        /* 0xAA 哨兵残留 = 该行未被 blit 触碰（pad 行已填 0，不参与此项） */
        for (int y = PAD_TOP; y < g_out_h - PAD_BOT; y++)
            for (size_t k = 0; k < dst_stride; k++)
                if (g_dst_va[(size_t)y * dst_stride + k] == 0xAA) nz_out++;
        double sum = 0; int mn = 255, mx = 0;
        for (int y = PAD_TOP; y < g_out_h - PAD_BOT; y++)
            for (size_t k = 0; k < dst_stride; k++) {
                const int v = g_dst_va[(size_t)y * dst_stride + k];
                sum += v; if (v < mn) mn = v; if (v > mx) mx = v;
            }
        fprintf(g_out, "\"pad_row_top_nonzero\":%ld,\"pad_row_bot_nonzero\":%ld,\"eff_sentinel_left\":%ld,",
                nz_top, nz_bot, nz_out);
        fprintf(g_out, "\"eff_min\":%d,\"eff_max\":%d,\"eff_mean\":%.2f,", mn, mx,
                sum / ((double)g_eff_h * dst_stride));
    }

    /* ---- ⑤ 双 job 并发 ---- */
    {
        job_t ja, jb;
        memset(&ja, 0, sizeof(ja));
        memset(&jb, 0, sizeof(jb));
        ja.idx = 0; ja.dst = g_job_dst[0]; ja.async_submit = 1;
        jb.idx = 1; jb.dst = g_job_dst[1]; jb.async_submit = 0;
        pthread_barrier_init(&g_bar, NULL, 2);
        pthread_t th[2];
        pthread_create(&th[0], NULL, job_worker, &ja);
        pthread_create(&th[1], NULL, job_worker, &jb);
        pthread_join(th[0], NULL);
        pthread_join(th[1], NULL);

        double batch[RUNS_MAX]; int nb = 0, na = 0, nc = 0;
        double sa[RUNS_MAX], sb[RUNS_MAX];
        for (int i = 0; i < g_runs; i++) {
            if (ja.ms[i] >= 0 && jb.ms[i] >= 0) { batch[nb++] = (ja.ms[i] > jb.ms[i]) ? ja.ms[i] : jb.ms[i]; }
            if (ja.ms[i] >= 0) sa[na++] = ja.ms[i];
            if (jb.ms[i] >= 0) sb[nc++] = jb.ms[i];
        }
        if (nb) print_stats("dual_job_batch_ms", batch, nb); else fprintf(g_out, "\"dual_job_batch_ms\":null");
        fprintf(g_out, ",");
        if (na) print_stats("dual_job_A_ms", sa, na); else fprintf(g_out, "\"dual_job_A_ms\":null");
        fprintf(g_out, ",");
        if (nc) print_stats("dual_job_B_ms", sb, nc); else fprintf(g_out, "\"dual_job_B_ms\":null");
        fprintf(g_out, ",");
    }

    /* ---- ⑥ 落盘 ---- */
    if (dump_path) {
        FILE* fo = fopen(dump_path, "wb");
        if (fo) {
            if (fwrite(g_dst_va, 1, dst_bytes, fo) != dst_bytes) perror("write dump");
            fclose(fo);
            fprintf(g_out, "\"dump\":\"%s\",\"dump_bytes\":%zu,", dump_path, dst_bytes);
        } else { perror("open dump"); }
    }

    fprintf(g_out, "\"runs\":%d}\n", g_runs);
    if (g_out != stdout) fclose(g_out);
    return 0;
}