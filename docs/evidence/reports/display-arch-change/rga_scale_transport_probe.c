/* rga_scale_transport_probe.c —— RGA 放大原语的**包裹方式 / 内存类型** A/B 实测
 *
 * 目的：v3.86 打通 HQ 通路后，链内镜像（MIPI 画布 → HDMI 画布整幅放大）实测
 *       **P50 2.90~2.94 ms / P90 4.18~4.20 ms**，而 v3.85 的 `rga_mirror_probe.c`
 *       （**dma-buf `fd` 包裹 + `system-dma32` 堆**）为 **中位 2.009 / p90 2.166 ms**。
 *       ⇒ 差在两个变量上，必须分离：
 *         ① **包裹方式**：`wrapbuffer_virtualaddr_t`（交付链路的 `rga_scale_rgb888`，与
 *            同文件 `rga_canvas_compose_to` 同法） vs `wrapbuffer_fd_t`（v3.85 探针）；
 *         ② **内存类型**：`cma` 堆（DRM 画布实际来源，见 `native/drm_core.c` 的
 *            `dma_heap_alloc("cma", …)`） vs `system-dma32` 堆（v3.85 探针）。
 *
 * 本探针在**同一对缓冲**上依次跑两组，只换包裹方式，故两个变量的贡献可分离。
 *
 * 性质：纯 dma-buf + RGA，**不碰 DRM / V4L2 / 摄像头**（规则 30-board-safety §4）⇒ 无副作用、
 *       桌面态即可运行。
 *
 * 口径：与 `rga_mirror_probe.c` 一致 —— `improcess` + 空 pat + srect/drect 全幅 + **IM_SYNC**、
 *       每档 **1 次预热不计入**、计时用 `CLOCK_MONOTONIC`。
 *
 * usage: rga_scale_transport_probe <heap> <src_w> <src_h> <dst_w> <dst_h> <runs> [core_mask]
 *   e.g. rga_scale_transport_probe cma 1024 600 1920 1080 200 3
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
#include "im2d.h"

struct dma_heap_alloc_data {
    uint64_t len;
    uint32_t fd;
    uint32_t fd_flags;
    uint64_t heap_flags;
};
#define DMA_HEAP_IOC_MAGIC  'H'
#define DMA_HEAP_IOCTL_ALLOC _IOWR(DMA_HEAP_IOC_MAGIC, 0x0, struct dma_heap_alloc_data)

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

static int cmp_d(const void* a, const void* b) {
    const double x = *(const double*)a, y = *(const double*)b;
    return (x < y) ? -1 : (x > y) ? 1 : 0;
}

/* 单档计时；返回是否成功，并回填 min/median/p90/max */
static int bench(const char* tag, int use_fd, const rga_buffer_t* src_va, const rga_buffer_t* dst_va,
                 int s_fd, int d_fd, int sw, int sh, int dw, int dh, int runs, double* out) {
    rga_buffer_t src, dst;
    if (use_fd) {
        src = wrapbuffer_fd_t(s_fd, sw, sh, sw, sh, RK_FORMAT_RGB_888);
        dst = wrapbuffer_fd_t(d_fd, dw, dh, dw, dh, RK_FORMAT_RGB_888);
    } else {
        src = *src_va;
        dst = *dst_va;
    }
    im_rect srect, drect, prect;
    memset(&srect, 0, sizeof(srect)); memset(&drect, 0, sizeof(drect)); memset(&prect, 0, sizeof(prect));
    srect.width = sw; srect.height = sh;
    drect.width = dw; drect.height = dh;
    rga_buffer_t pat;
    memset(&pat, 0, sizeof(pat));

    if (improcess(src, dst, pat, srect, drect, prect, IM_SYNC) != IM_STATUS_SUCCESS) {
        fprintf(stderr, "[%s] 预热 improcess 失败\n", tag);
        return -1;
    }
    double* t = (double*)malloc(sizeof(double) * runs);
    for (int i = 0; i < runs; i++) {
        const double t0 = now_ms();
        IM_STATUS st = improcess(src, dst, pat, srect, drect, prect, IM_SYNC);
        t[i] = now_ms() - t0;
        if (st != IM_STATUS_SUCCESS) { fprintf(stderr, "[%s] improcess 失败 st=%d\n", tag, st); free(t); return -1; }
    }
    double* s = (double*)malloc(sizeof(double) * runs);
    memcpy(s, t, sizeof(double) * runs);
    qsort(s, runs, sizeof(double), cmp_d);
    out[0] = s[0]; out[1] = s[runs / 2]; out[2] = s[(int)(runs * 0.9)]; out[3] = s[runs - 1];
    free(t); free(s);
    return 0;
}

int main(int argc, char** argv) {
    if (argc < 7) {
        fprintf(stderr, "usage: %s <heap> <src_w> <src_h> <dst_w> <dst_h> <runs> [core_mask]\n", argv[0]);
        return 2;
    }
    const char* heap = argv[1];
    const int sw = atoi(argv[2]), sh = atoi(argv[3]);
    const int dw = atoi(argv[4]), dh = atoi(argv[5]);
    int runs = atoi(argv[6]);
    const int core_mask = (argc > 7) ? atoi(argv[7]) : 3;
    if (runs < 1) runs = 1;
    if (runs > 2000) runs = 2000;

    const size_t s_bytes = (size_t)sw * 3 * sh, d_bytes = (size_t)dw * 3 * dh;
    void *s_va = NULL, *d_va = NULL;
    int s_fd = dma_heap_alloc(heap, s_bytes, &s_va);
    int d_fd = dma_heap_alloc(heap, d_bytes, &d_va);
    if (s_fd < 0 || d_fd < 0) return 3;

    /* 确定性图案（内容不影响 RGA 吞吐，只为可复现） */
    uint8_t* sp = (uint8_t*)s_va;
    for (size_t i = 0; i < s_bytes; i++) sp[i] = (uint8_t)((i * 7) & 0xFF);

    rga_buffer_t src_va = wrapbuffer_virtualaddr_t(s_va, sw, sh, sw, sh, RK_FORMAT_RGB_888);
    rga_buffer_t dst_va = wrapbuffer_virtualaddr_t(d_va, dw, dh, dw, dh, RK_FORMAT_RGB_888);
    imconfig(IM_CONFIG_SCHEDULER_CORE, core_mask);

    double va[4], fd[4];
    /* 顺序：先 VA 后 fd（两档各自预热；同一对缓冲，只换包裹方式） */
    if (bench("va", 0, &src_va, &dst_va, s_fd, d_fd, sw, sh, dw, dh, runs, va) != 0) return 4;
    if (bench("fd", 1, &src_va, &dst_va, s_fd, d_fd, sw, sh, dw, dh, runs, fd) != 0) return 5;

    printf("{\"heap\":\"%s\",\"geom\":{\"src\":\"%dx%d\",\"dst\":\"%dx%d\"},\"runs\":%d,"
           "\"core_mask\":%d,\"warmup\":1,"
           "\"va_ms\":{\"min\":%.3f,\"median\":%.3f,\"p90\":%.3f,\"max\":%.3f},"
           "\"fd_ms\":{\"min\":%.3f,\"median\":%.3f,\"p90\":%.3f,\"max\":%.3f},"
           "\"delta_median_ms\":%.3f,\"delta_p90_ms\":%.3f}\n",
           heap, sw, sh, dw, dh, runs, core_mask,
           va[0], va[1], va[2], va[3], fd[0], fd[1], fd[2], fd[3],
           va[1] - fd[1], va[2] - fd[2]);
    return 0;
}