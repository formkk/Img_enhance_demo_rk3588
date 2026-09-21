/* rga_mirror_probe.c —— C-RGA（双画布 + RGA 放大镜像）的成本实测
 *
 * 目的：量测两个放大 blit 的耗时
 *   ① 镜像：MIPI 合成画布 1024×600 RGB888 ──放大──▶ HDMI 画布 1920×1080 RGB888（常规态**每帧一次**）
 *   ② HQ  ：HQ 原生 960×540 RGB888 ──放大 2.0×──▶ 展示成品 1920×1080（每次触发一次）
 *
 * 口径：dma-buf（`fd` 模式，与交付链路的画布一致）+ `core_mask=3`（§10.4 定稿值）+ 同步提交（IM_SYNC）；
 *       计时前做 **1 次预热**（不计入），以取稳态每帧成本。
 * 性质：纯 dma-buf / RGA 操作，**不碰 DRM / V4L2 / 摄像头** ⇒ 无副作用，桌面态即可运行（规则 30 §4）。
 *
 * usage: rga_mirror_probe <src_w> <src_h> <dst_w> <dst_h> <runs> [core_mask] [json|-]
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

int main(int argc, char** argv) {
    if (argc < 6) {
        fprintf(stderr, "usage: %s <src_w> <src_h> <dst_w> <dst_h> <runs> [core_mask] [json|-]\n", argv[0]);
        return 2;
    }
    const int sw = atoi(argv[1]), sh = atoi(argv[2]);
    const int dw = atoi(argv[3]), dh = atoi(argv[4]);
    int runs = atoi(argv[5]);
    const int core_mask = (argc > 6) ? atoi(argv[6]) : 3;
    const char* json_path = (argc > 7 && strcmp(argv[7], "-") != 0) ? argv[7] : NULL;
    if (runs < 1) runs = 1;
    if (runs > 512) runs = 512;

    const size_t s_stride = (size_t)sw * 3, s_bytes = s_stride * sh;
    const size_t d_stride = (size_t)dw * 3, d_bytes = d_stride * dh;

    void *s_va = NULL, *d_va = NULL;
    int s_fd = dma_heap_alloc("system-dma32", s_bytes, &s_va);
    int d_fd = dma_heap_alloc("system-dma32", d_bytes, &d_va);
    if (s_fd < 0 || d_fd < 0) return 3;

    /* 确定性图案（内容不影响 RGA 吞吐，只为可复现） */
    uint8_t* sp = (uint8_t*)s_va;
    for (size_t i = 0; i < s_bytes; i++) sp[i] = (uint8_t)((i * 7) & 0xFF);

    rga_buffer_t src = wrapbuffer_fd_t(s_fd, sw, sh, sw, sh, RK_FORMAT_RGB_888);
    rga_buffer_t dst = wrapbuffer_fd_t(d_fd, dw, dh, dw, dh, RK_FORMAT_RGB_888);
    im_rect srect, drect, prect;
    memset(&srect, 0, sizeof(srect)); memset(&drect, 0, sizeof(drect)); memset(&prect, 0, sizeof(prect));
    srect.width = sw; srect.height = sh;
    drect.width = dw; drect.height = dh;
    rga_buffer_t pat;
    memset(&pat, 0, sizeof(pat));

    imconfig(IM_CONFIG_SCHEDULER_CORE, core_mask);

    /* 预热一次（不计入） */
    if (improcess(src, dst, pat, srect, drect, prect, IM_SYNC) != IM_STATUS_SUCCESS) {
        fprintf(stderr, "warmup improcess 失败\n");
        return 4;
    }

    double* t = (double*)malloc(sizeof(double) * runs);
    for (int i = 0; i < runs; i++) {
        const double t0 = now_ms();
        IM_STATUS st = improcess(src, dst, pat, srect, drect, prect, IM_SYNC);
        t[i] = now_ms() - t0;
        if (st != IM_STATUS_SUCCESS) { fprintf(stderr, "improcess 失败 st=%d\n", st); return 5; }
    }
    double* s = (double*)malloc(sizeof(double) * runs);
    memcpy(s, t, sizeof(double) * runs);
    qsort(s, runs, sizeof(double), cmp_d);

    const double frame_bytes = (double)s_bytes + (double)d_bytes;
    FILE* fo = json_path ? fopen(json_path, "w") : stdout;
    fprintf(fo,
        "{\"src\":\"%dx%d RGB888\",\"dst\":\"%dx%d RGB888\",\"runs\":%d,\"core_mask\":%d,"
        "\"ms\":{\"min\":%.3f,\"median\":%.3f,\"p90\":%.3f,\"max\":%.3f},"
        "\"ddr\":{\"frame_bytes\":%.0f,\"fused_MBps_at30\":%.1f,\"fused_MBps_at22\":%.1f},"
        "\"warmup\":1}\n",
        sw, sh, dw, dh, runs, core_mask,
        s[0], s[runs / 2], s[(int)(runs * 0.9)], s[runs - 1],
        frame_bytes, frame_bytes * 30.0 / 1e6, frame_bytes * 22.0 / 1e6);
    if (json_path) fclose(fo);
    free(t); free(s);
    return 0;
}