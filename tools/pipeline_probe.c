/* pipeline_probe.c —— MIPI 屏上 ZDE++ 全流程打通（首版单体 C 程序）
 *
 * 目标（用户 2026-09-18 指示："先实现 MIPI 屏上 ZDE++ 网络的全流程实现，画质相关的
 * 在打通全流程后再进行优化"）：把此前彼此独立、只吃文件的四段探针串成**一条实时链路**，
 * 在 7" MIPI 屏（DSI-1，1024×600）上出图。
 *
 * 链路（主基准 §3.3）：
 *   V4L2 采集 1920×1080 NV12 (DMABUF)
 *     ├─ RGA → 有效区 480×270（落 480×272 缓冲第 1..270 行，上下 pad 零填充）→ ZDE++ INT8 双条带
 *     │    （core0+core1，2 条带空间切分，HALO=7）→ 3ch 曲线场（取有效区 480×270）→ EMA
 *     ├─ RGA → 512×288 RGB888（原图窗）
 *     └─ 曲线应用（8 iter 单遍融合 + 曲线场双线性采样）→ 512×288 RGB888（增强窗）
 *   两窗合成 1024×600 画布（左原图 / 右增强）→ DRM DSI pageflip
 *
 * 与交付链路的关系：**本程序是探针**（同 tools/rga_probe.c / apply_probe.c / rknn_split_probe.c 的
 * 惯例），不是 §10.3 的 capture.py / curve_estim.py / realtime_apply.py / display_mipi.py 交付模块。
 * M2 再按 §10.3 拆分与 pybind 化。故本文件**不吃 config.yaml**（键表定稿须走 §15 裁定），
 * 一律走 CLI 参数。
 *
 * 复用（逐段来自已实测的探针，未改算法）：
 *   · dma_heap 分配 + 偏移基址子视图 + improcess scale/CSC —— tools/rga_probe.c
 *   · 8 iter 单遍融合 NEON + 双线性采样 build_taps —— tools/apply_probe.c
 *   · 2 条带 RKNN 空间切分 + 拼接 + HALO=7 —— tools/rknn_split_probe.c
 *
 * 实测依据（本轮 S0 预测量，主基准 §15 的 O42 / O43）：
 *   · 曲线应用 @512×288 ×8 iter 单遍融合 NEON = 3.985 ms 中位（NEON 与标量逐位一致）
 *   · RGA 1920×1080 NV12 → 512×288 RGB888 = 2.079 ms 中位
 *
 * 用法：
 *   pipeline_probe --smoke                     # DRM dumb buffer 色条冒烟（只验 master/pageflip）
 *   pipeline_probe --offline [--dump-dir DIR]  # 离线单帧全链路（不出屏），落图供数值对账
 *   pipeline_probe --display [--frames N] [--est-every N] [--alpha F] [--report J.json]
 *
 * 通用参数：--device /dev/video11 · --model PATH · --skip-frames N · --copy-cap · --no-ema
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <math.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <pthread.h>
#include <linux/videodev2.h>

#include <xf86drm.h>
#include <xf86drmMode.h>
#include <drm_fourcc.h>

#include "im2d.h"
#include "rknn_api.h"

#ifdef __ARM_NEON
#include <arm_neon.h>
#endif

/* ───────────────────────── 几何常量（主基准 §3.1 / §3.4） ───────────────────────── */
#define CAP_W        1920
#define CAP_H        1080
#define EST_W        480            /* 模型 shape 宽 */
#define EST_H        272            /* 模型 shape 高 */
#define PAD_TOP      1              /* 有效区 480×270 落第 1..270 行 */
#define PAD_BOT      1
#define EST_EFF_H    (EST_H - PAD_TOP - PAD_BOT)   /* 270 */
#define FILL_H       2              /* RGA 不接受高度 <2 的矩形 */
#define WIN_W        512            /* 显示窗 = 应用域（§3.1 第 2 条：精确 3.75× 等比） */
#define WIN_H        288
#define CV_W         1024           /* MIPI 画布 */
#define CV_H         600

#define N_SLICES     2
#define HALO         7              /* 感受野半径（7 次 3×3） */
#define SLICE_W_OUT  (EST_W / N_SLICES)   /* 240 */
#define SLICE_W_IN   256            /* ≥ 240 + 2*HALO，按 8 对齐 */
#define SLICE_H      EST_H

#define CURVE_ITERS  8

/* 默认参数（**非** config 键；探针口径） */
#define DEF_MODEL    "/home/cat/rknn_ab/models/zde_curve_256x272_int8_tk232.rknn"
#define DEF_DEVICE   "/dev/video11"
#define DEF_CARD     "/dev/dri/card0"
#define DEF_EST_EVERY 16
#define DEF_ALPHA    0.63           /* N = ln0.1/lnα ≈ 5 @10 Hz（§9.1，≤500 ms 收敛） */
#define DEF_SKIP     10
#define CAP_NBUF     4

/* ───────────────────────── 小工具 ───────────────────────── */
static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static int cmp_double(const void* a, const void* b) {
    double x = *(const double*)a, y = *(const double*)b;
    return (x > y) - (x < y);
}

#define STAT_MAX 4096
typedef struct { const char* key; double v[STAT_MAX]; int n; } stat_t;

static void stat_add(stat_t* s, double ms) { if (s->n < STAT_MAX) s->v[s->n++] = ms; }

static void stat_print(FILE* f, stat_t* s, int last) {
    double t[STAT_MAX];
    if (s->n == 0) { fprintf(f, "\"%s\":null%s", s->key, last ? "\n" : ",\n"); return; }
    memcpy(t, s->v, sizeof(double) * s->n);
    qsort(t, s->n, sizeof(double), cmp_double);
    fprintf(f, "\"%s\":{\"n\":%d,\"min\":%.3f,\"median\":%.3f,\"p90\":%.3f,\"max\":%.3f}%s",
            s->key, s->n, t[0], t[s->n / 2], t[(int)(s->n * 0.9)], t[s->n - 1], last ? "\n" : ",\n");
}

static stat_t S_cap, S_est, S_win, S_apply, S_canvas, S_flip, S_frame, S_npu;

/* ───────────────────────── dma-buf heap（搬自 tools/rga_probe.c） ───────────────────────── */
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
    if (hfd < 0) { fprintf(stderr, "open %s: %s\n", path, strerror(errno)); return -1; }
    struct dma_heap_alloc_data d;
    memset(&d, 0, sizeof(d));
    d.len = len;
    d.fd_flags = O_RDWR | O_CLOEXEC;
    if (ioctl(hfd, DMA_HEAP_IOCTL_ALLOC, &d) < 0) {
        fprintf(stderr, "DMA_HEAP_IOCTL_ALLOC(%s): %s\n", heap, strerror(errno));
        close(hfd);
        return -1;
    }
    close(hfd);
    if (va) {
        void* p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, d.fd, 0);
        if (p == MAP_FAILED) { fprintf(stderr, "mmap dmabuf: %s\n", strerror(errno)); close(d.fd); return -1; }
        *va = p;
    }
    return (int)d.fd;
}

/* ───────────────────────── V4L2 采集（MPLANE + DMABUF 导出） ─────────────────────────
 * 节点 /dev/video11 只支持 V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE（caps 无 V4L2_CAP_VIDEO_CAPTURE）。
 * 易错处：v4l2_buffer.length 在 MPLANE 下 = **plane 个数**（本例恒为 1），不是字节数；
 *         VIDIOC_EXPBUF 的 plane 字段必须显式写 0。
 */
typedef struct {
    int fd;
    int nbuf;
    struct {
        void*  start;
        size_t length;
        int    dmabuf_fd;
    } buf[CAP_NBUF];
    int w, h;
    uint32_t sizeimage;
} cap_t;

static int xioctl(int fd, unsigned long req, void* arg) {
    int r;
    do { r = ioctl(fd, req, arg); } while (r < 0 && errno == EINTR);
    return r;
}

static int cap_init(cap_t* c, const char* dev) {
    memset(c, 0, sizeof(*c));
    c->fd = open(dev, O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (c->fd < 0) { fprintf(stderr, "open %s: %s\n", dev, strerror(errno)); return -1; }

    struct v4l2_capability cap;
    memset(&cap, 0, sizeof(cap));
    if (xioctl(c->fd, VIDIOC_QUERYCAP, &cap) < 0) { perror("VIDIOC_QUERYCAP"); return -1; }
    const uint32_t need = V4L2_CAP_VIDEO_CAPTURE_MPLANE | V4L2_CAP_STREAMING;
    if ((cap.capabilities & need) != need) {
        fprintf(stderr, "%s: 不支持 MPLANE+STREAMING（caps=0x%08x）\n", dev, cap.capabilities);
        return -1;
    }
    fprintf(stderr, "[cap] %s  driver=%s card=%s caps=0x%08x\n", dev, cap.driver, cap.card, cap.capabilities);

    struct v4l2_format f;
    memset(&f, 0, sizeof(f));
    f.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    f.fmt.pix_mp.width       = CAP_W;
    f.fmt.pix_mp.height      = CAP_H;
    f.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_NV12;
    f.fmt.pix_mp.field       = V4L2_FIELD_NONE;
    f.fmt.pix_mp.num_planes  = 1;
    f.fmt.pix_mp.plane_fmt[0].sizeimage = CAP_W * CAP_H * 3 / 2;
    if (xioctl(c->fd, VIDIOC_S_FMT, &f) < 0) { perror("VIDIOC_S_FMT"); return -1; }
    /* 回读：rkisp 会静默改 sizeimage / bytesperline */
    memset(&f, 0, sizeof(f));
    f.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    if (xioctl(c->fd, VIDIOC_G_FMT, &f) < 0) { perror("VIDIOC_G_FMT"); return -1; }
    c->w = f.fmt.pix_mp.width;
    c->h = f.fmt.pix_mp.height;
    c->sizeimage = f.fmt.pix_mp.plane_fmt[0].sizeimage;
    fprintf(stderr, "[cap] fmt=%c%c%c%c %dx%d num_planes=%u bytesperline=%u sizeimage=%u\n",
            (f.fmt.pix_mp.pixelformat) & 0xff, (f.fmt.pix_mp.pixelformat >> 8) & 0xff,
            (f.fmt.pix_mp.pixelformat >> 16) & 0xff, (f.fmt.pix_mp.pixelformat >> 24) & 0xff,
            c->w, c->h, f.fmt.pix_mp.num_planes,
            f.fmt.pix_mp.plane_fmt[0].bytesperline, c->sizeimage);
    if (c->w != CAP_W || c->h != CAP_H) {
        fprintf(stderr, "[cap] 警告：驱动给出的尺寸 %dx%d 与预期 %dx%d 不同\n", c->w, c->h, CAP_W, CAP_H);
    }

    struct v4l2_requestbuffers rb;
    memset(&rb, 0, sizeof(rb));
    rb.count  = CAP_NBUF;
    rb.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    rb.memory = V4L2_MEMORY_MMAP;
    if (xioctl(c->fd, VIDIOC_REQBUFS, &rb) < 0) { perror("VIDIOC_REQBUFS"); return -1; }
    if (rb.count < 2) { fprintf(stderr, "[cap] REQBUFS 只给了 %u 个缓冲\n", rb.count); return -1; }
    c->nbuf = rb.count;
    if (c->nbuf > CAP_NBUF) c->nbuf = CAP_NBUF;
    fprintf(stderr, "[cap] REQBUFS count=%d\n", c->nbuf);

    for (int i = 0; i < c->nbuf; i++) {
        struct v4l2_plane planes[VIDEO_MAX_PLANES];
        struct v4l2_buffer b;
        memset(&b, 0, sizeof(b));
        memset(planes, 0, sizeof(planes));
        b.type     = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        b.memory   = V4L2_MEMORY_MMAP;
        b.index    = i;
        b.length   = 1;                     /* MPLANE：plane 个数 */
        b.m.planes = planes;
        if (xioctl(c->fd, VIDIOC_QUERYBUF, &b) < 0) { perror("VIDIOC_QUERYBUF"); return -1; }
        c->buf[i].length = planes[0].length;
        c->buf[i].start = mmap(NULL, planes[0].length, PROT_READ | PROT_WRITE,
                               MAP_SHARED, c->fd, planes[0].m.mem_offset);
        if (c->buf[i].start == MAP_FAILED) { perror("mmap v4l2"); return -1; }

        struct v4l2_exportbuffer eb;
        memset(&eb, 0, sizeof(eb));
        eb.type  = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        eb.index = i;
        eb.plane = 0;                       /* 必须显式写 0 */
        eb.flags = O_CLOEXEC;
        if (xioctl(c->fd, VIDIOC_EXPBUF, &eb) < 0) {
            fprintf(stderr, "[cap] VIDIOC_EXPBUF(buf %d) 失败：%s ⇒ 退化为 staging 拷贝\n",
                    i, strerror(errno));
            c->buf[i].dmabuf_fd = -1;
        } else {
            c->buf[i].dmabuf_fd = eb.fd;
        }
    }
    fprintf(stderr, "[cap] %d 缓冲就绪（length=%zu，dmabuf_fd[0]=%d）\n",
            c->nbuf, c->buf[0].length, c->buf[0].dmabuf_fd);
    return 0;
}

static int cap_qbuf(cap_t* c, int idx) {
    struct v4l2_plane planes[VIDEO_MAX_PLANES];
    struct v4l2_buffer b;
    memset(&b, 0, sizeof(b));
    memset(planes, 0, sizeof(planes));
    b.type     = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    b.memory   = V4L2_MEMORY_MMAP;
    b.index    = idx;
    b.length   = 1;
    b.m.planes = planes;
    if (xioctl(c->fd, VIDIOC_QBUF, &b) < 0) { perror("VIDIOC_QBUF"); return -1; }
    return 0;
}

static int cap_stream_on(cap_t* c) {
    enum v4l2_buf_type t = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    for (int i = 0; i < c->nbuf; i++) if (cap_qbuf(c, i) < 0) return -1;
    if (xioctl(c->fd, VIDIOC_STREAMON, &t) < 0) { perror("VIDIOC_STREAMON"); return -1; }
    return 0;
}

/* 取一帧；返回 buffer index，*bytesused / *ts_ns / *ts_src 回填。超时返回 -1。 */
static int cap_dqbuf(cap_t* c, size_t* bytesused, uint64_t* ts_ns, uint32_t* ts_src, int timeout_ms) {
    struct pollfd pfd = { c->fd, POLLIN, 0 };
    int pr = poll(&pfd, 1, timeout_ms);
    if (pr <= 0) return -1;

    struct v4l2_plane planes[VIDEO_MAX_PLANES];
    struct v4l2_buffer b;
    memset(&b, 0, sizeof(b));
    memset(planes, 0, sizeof(planes));
    b.type     = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    b.memory   = V4L2_MEMORY_MMAP;
    b.length   = 1;
    b.m.planes = planes;
    if (xioctl(c->fd, VIDIOC_DQBUF, &b) < 0) {
        if (errno != EAGAIN) perror("VIDIOC_DQBUF");
        return -1;
    }
    if (bytesused) *bytesused = planes[0].bytesused;
    if (ts_ns)     *ts_ns = (uint64_t)b.timestamp.tv_sec * 1000000000ull + b.timestamp.tv_usec * 1000ull;
    if (ts_src)    *ts_src = b.flags & V4L2_BUF_FLAG_TSTAMP_SRC_MASK;
    return (int)b.index;
}

static void cap_deinit(cap_t* c) {
    enum v4l2_buf_type t = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    if (c->fd >= 0) xioctl(c->fd, VIDIOC_STREAMOFF, &t);
    for (int i = 0; i < c->nbuf; i++) {
        if (c->buf[i].start && c->buf[i].start != MAP_FAILED) munmap(c->buf[i].start, c->buf[i].length);
        /* 契约：任何模块不得 close(dmabuf_fd)（§5 硬契约）——退出时随进程释放 */
    }
    if (c->fd >= 0) close(c->fd);
    c->fd = -1;
}

/* ───────────────────────── RGA 几何段（搬自 tools/rga_probe.c） ───────────────────────── */
static int   g_est_fd = -1, g_win_fd = -1, g_enh_fd = -1, g_zero_fd = -1, g_stage_fd = -1;
static void *g_est_va = NULL, *g_win_va = NULL, *g_enh_va = NULL, *g_zero_va = NULL, *g_stage_va = NULL;
static rga_buffer_t g_est_full, g_est_eff, g_win_full, g_enh_full, g_zero_full, g_stage_full;
static int g_copy_cap = 0;

static const size_t EST_BYTES = (size_t)EST_W * EST_H * 3;
static const size_t WIN_BYTES = (size_t)WIN_W * WIN_H * 3;
static const size_t CAP_BYTES = (size_t)CAP_W * CAP_H * 3 / 2;

static int rga_init(void) {
    g_est_fd  = dma_heap_alloc("system-dma32", EST_BYTES, &g_est_va);
    g_win_fd  = dma_heap_alloc("system-dma32", WIN_BYTES, &g_win_va);
    g_enh_fd  = dma_heap_alloc("system-dma32", WIN_BYTES, &g_enh_va);
    g_zero_fd = dma_heap_alloc("system-dma32", EST_BYTES, &g_zero_va);
    g_stage_fd= dma_heap_alloc("system-dma32", CAP_BYTES, &g_stage_va);
    if (g_est_fd < 0 || g_win_fd < 0 || g_enh_fd < 0 || g_zero_fd < 0 || g_stage_fd < 0) return -1;
    memset(g_zero_va, 0, EST_BYTES);

    g_est_full  = wrapbuffer_virtualaddr_t(g_est_va, EST_W, EST_H, EST_W, EST_H, RK_FORMAT_RGB_888);
    /* 偏移基址子视图：有效区 480×270 落在整幅 480×272 的第 1..270 行。
       （drect 的 y 偏移必须偶对齐，dy=1 不可表达 ⇒ 只能按 wstride*hstride 偏移基址） */
    g_est_eff   = wrapbuffer_virtualaddr_t((uint8_t*)g_est_va + (size_t)EST_W * 3 * PAD_TOP,
                                           EST_W, EST_EFF_H, EST_W, EST_H, RK_FORMAT_RGB_888);
    g_win_full  = wrapbuffer_virtualaddr_t(g_win_va, WIN_W, WIN_H, WIN_W, WIN_H, RK_FORMAT_RGB_888);
    g_enh_full  = wrapbuffer_virtualaddr_t(g_enh_va, WIN_W, WIN_H, WIN_W, WIN_H, RK_FORMAT_RGB_888);
    g_zero_full = wrapbuffer_virtualaddr_t(g_zero_va, EST_W, EST_H, EST_W, EST_H, RK_FORMAT_RGB_888);
    g_stage_full= wrapbuffer_virtualaddr_t(g_stage_va, CAP_W, CAP_H, CAP_W, CAP_H, RK_FORMAT_YCbCr_420_SP);

    /* pad 零填充：imfill 对 RGB888 直接失败（M0-13），只能用 improcess 从零源块拷入。
       RGA 不接受高度 <2 的矩形 ⇒ 按 2 行填；随后的 blit 会覆盖有效区，故结果正确。 */
    im_rect top = {0, 0, EST_W, FILL_H};
    im_rect bot = {0, EST_H - FILL_H, EST_W, FILL_H};
    im_rect prect = {0, 0, 0, 0};
    rga_buffer_t pat;
    memset(&pat, 0, sizeof(pat));
    IM_STATUS s1 = improcess(g_zero_full, g_est_full, pat, top, top, prect, IM_SYNC);
    IM_STATUS s2 = improcess(g_zero_full, g_est_full, pat, bot, bot, prect, IM_SYNC);
    if (s1 != IM_STATUS_SUCCESS || s2 != IM_STATUS_SUCCESS) {
        fprintf(stderr, "[rga] pad 零填充失败：%s / %s\n", imStrError_t(s1), imStrError_t(s2));
        return -1;
    }
    fprintf(stderr, "[rga] 就绪（est %dx%d eff %dx%d / win %dx%d）\n",
            EST_W, EST_H, EST_W, EST_EFF_H, WIN_W, WIN_H);
    return 0;
}

/* 源 NV12（V4L2 dmabuf fd 或 staging）→ 目标 RGB888。 */
static IM_STATUS rga_blit_nv12(int src_fd, rga_buffer_t dst, int dw, int dh) {
    rga_buffer_t src = wrapbuffer_fd_t(src_fd, CAP_W, CAP_H, CAP_W, CAP_H, RK_FORMAT_YCbCr_420_SP);
    im_rect srect = {0, 0, CAP_W, CAP_H};
    im_rect drect = {0, 0, dw, dh};
    im_rect prect = {0, 0, 0, 0};
    rga_buffer_t pat;
    memset(&pat, 0, sizeof(pat));
    return improcess(src, dst, pat, srect, drect, prect, IM_SYNC);
}

/* ───────────────────────── 曲线应用（搬自 tools/apply_probe.c，算法未改） ───────────────────────── */
typedef struct { int i0; float w; } tap_t;
static tap_t* g_xt = NULL;
static tap_t* g_yt = NULL;
static const uint8_t* g_ap_src = NULL;
static const float*   g_ap_curve = NULL;
static uint8_t*       g_ap_out = NULL;
static int AP_W = WIN_W, AP_H = WIN_H, AP_CW = EST_W, AP_CH = EST_EFF_H;

/* 采样表：目标像素中心 (i+0.5)/N 归一化 → 源坐标 (u*C − 0.5)，i0 夹到 [0, C−2]，w 夹到 [0,1] */
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

/* NEON 单遍融合（4 像素 × 3 通道一组；曲线场双线性采样每像素只做一次，8 次迭代在寄存器内） */
static void apply_fused_neon(void) {
#ifdef __ARM_NEON
    const int W = AP_W, H = AP_H, CW = AP_CW, CH = AP_CH;
    const float* cb[3] = { g_ap_curve, g_ap_curve + (size_t)CH * CW, g_ap_curve + (size_t)2 * CH * CW };
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
            float32x4_t C[3] = { vld1q_f32(rv[0]), vld1q_f32(rv[1]), vld1q_f32(rv[2]) };
            float32x4_t V[3];
            {
                uint8_t tmp[3][4];
                for (int l = 0; l < 4; l++) {
                    const uint8_t* p = g_ap_src + ((size_t)y * W + x + l) * 3;
                    tmp[0][l] = p[0]; tmp[1][l] = p[1]; tmp[2][l] = p[2];
                }
                for (int ch = 0; ch < 3; ch++) {
                    uint8x8_t u8 = vld1_u8(tmp[ch]);
                    V[ch] = vmulq_n_f32(vcvtq_f32_u32(vmovl_u16(vget_low_u16(vmovl_u8(u8)))),
                                        1.0f / 255.0f);
                }
            }
            for (int it = 0; it < CURVE_ITERS; it++) {
                for (int ch = 0; ch < 3; ch++) {
                    float32x4_t t = vsubq_f32(vmulq_f32(V[ch], V[ch]), V[ch]);
                    V[ch] = vfmaq_f32(V[ch], t, C[ch]);
                }
            }
            for (int ch = 0; ch < 3; ch++) {
                V[ch] = vmaxq_f32(vminq_f32(V[ch], vdupq_n_f32(1.0f)), vdupq_n_f32(0.0f));
            }
            uint8x8_t q[3];
            for (int ch = 0; ch < 3; ch++) {
                int32x4_t s = vcvtnq_s32_f32(vmulq_n_f32(V[ch], 255.0f));
                q[ch] = vqmovun_s16(vcombine_s16(vqmovn_s32(s), vqmovn_s32(s)));
            }
            uint8_t tmp[3][8];
            for (int ch = 0; ch < 3; ch++) vst1_u8(tmp[ch], q[ch]);
            for (int l = 0; l < 4; l++) {
                uint8_t* o = g_ap_out + ((size_t)y * W + x + l) * 3;
                o[0] = tmp[0][l]; o[1] = tmp[1][l]; o[2] = tmp[2][l];
            }
        }
    }
#endif
}

static void apply_init(void) {
    g_xt = (tap_t*)malloc(sizeof(tap_t) * AP_W);
    g_yt = (tap_t*)malloc(sizeof(tap_t) * AP_H);
    build_taps(g_xt, AP_W, AP_CW);
    build_taps(g_yt, AP_H, AP_CH);
}

/* 标量参考：与 NEON 路径**同形**（同样的采样口径与同样的迭代表达式），用于逐位自检。
   与 tools/apply_probe.c 的 apply_fused_scalar 同源——M0-13 已证该标量路径与 NEON 逐位一致。 */
static void apply_fused_scalar_to(uint8_t* dst) {
    const int W = AP_W, H = AP_H, CW = AP_CW, CH = AP_CH;
    const float* cb[3] = { g_ap_curve, g_ap_curve + (size_t)CH * CW, g_ap_curve + (size_t)2 * CH * CW };
    for (int y = 0; y < H; y++) {
        const int iy = g_yt[y].i0; const float wy = g_yt[y].w;
        const float* r0s[3] = { cb[0] + (size_t)iy * CW, cb[1] + (size_t)iy * CW, cb[2] + (size_t)iy * CW };
        for (int x = 0; x < W; x++) {
            const int ix = g_xt[x].i0; const float wx = g_xt[x].w;
            const uint8_t* p = g_ap_src + ((size_t)y * W + x) * 3;
            for (int ch = 0; ch < 3; ch++) {
                const float* a = r0s[ch]; const float* b = a + CW;
                const float top = a[ix] + wx * (a[ix + 1] - a[ix]);
                const float bot = b[ix] + wx * (b[ix + 1] - b[ix]);
                const float v = top + wy * (bot - top);
                float t = (float)p[ch] * (1.0f / 255.0f);
                for (int it = 0; it < CURVE_ITERS; it++) t = t + v * (t * t - t);
                if (t < 0.0f) t = 0.0f; else if (t > 1.0f) t = 1.0f;
                dst[((size_t)y * W + x) * 3 + ch] = (uint8_t)lrintf(t * 255.0f);
            }
        }
    }
}

/* ───────────────────────── ZDE++ INT8 双条带（搬自 tools/rknn_split_probe.c） ───────────────────────── */
typedef struct {
    int idx, start;
    rknn_context ctx;
    uint8_t* in_buf;
    rknn_input input;
    rknn_output output;
    float* out_float;
    int ret_init, ret_mask;
} slice_t;

static slice_t g_sl[N_SLICES];
static pthread_t g_sl_th[N_SLICES];
/* 主线程与两条带 worker 的会合协议。
 * 为什么不用 pthread_barrier：barrier 只能表达"都到了"，无法表达"停止"——
 * 若主线程在 worker 已越过 stop 检查之后才置位，worker 会回到下一次 barrier_wait 而主线程
 * 已不再参与 ⇒ pthread_join 永久阻塞（本文件 v1 实测踩到：报告已写出、进程却卡住不退）。
 * 故改用 mutex + cond + 代次：停止信号与工作信号在同一把锁下发布，无竞态窗口。 */
static pthread_mutex_t g_sl_mtx = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_sl_cv  = PTHREAD_COND_INITIALIZER;
static int g_sl_gen = 0;         /* 主线程递增 = 请求一轮估计 */
static int g_sl_done = 0;        /* 本轮已完成的 worker 数 */
static int g_sl_stop = 0;
static const uint8_t* g_est_rgb = NULL;      /* 480×272 RGB888（含 pad 行） */
static int g_npu_ready = 0;

static void slice_build_input(slice_t* s) {
    const int start = s->start;
    for (int y = 0; y < SLICE_H; y++) {
        const uint8_t* srcrow = g_est_rgb + (size_t)y * EST_W * 3;
        uint8_t* dstrow = s->in_buf + (size_t)y * SLICE_W_IN * 3;
        for (int c = 0; c < SLICE_W_IN; c++) {
            const int xs = start + c;
            if (xs < 0 || xs >= EST_W) {
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
    pthread_mutex_lock(&g_sl_mtx);
    for (;;) {
        while (g_sl_gen == seen && !g_sl_stop) pthread_cond_wait(&g_sl_cv, &g_sl_mtx);
        if (g_sl_stop) break;
        seen = g_sl_gen;
        pthread_mutex_unlock(&g_sl_mtx);

        slice_build_input(s);
        if (rknn_inputs_set(s->ctx, 1, &s->input) < 0 ||
            rknn_run(s->ctx, NULL) < 0 ||
            rknn_outputs_get(s->ctx, 1, &s->output, NULL) < 0) {
            fprintf(stderr, "[npu] slice %d 推理失败\n", s->idx);
        } else {
            memcpy(s->out_float, s->output.buf, (size_t)SLICE_H * SLICE_W_IN * 3 * sizeof(float));
            rknn_outputs_release(s->ctx, 1, &s->output);
        }

        pthread_mutex_lock(&g_sl_mtx);
        g_sl_done++;
        pthread_cond_broadcast(&g_sl_cv);
    }
    pthread_mutex_unlock(&g_sl_mtx);
    return NULL;
}

static int npu_init(const char* model_path) {
    FILE* fm = fopen(model_path, "rb");
    if (!fm) { fprintf(stderr, "open model %s: %s\n", model_path, strerror(errno)); return -1; }
    fseek(fm, 0, SEEK_END);
    long msz = ftell(fm);
    fseek(fm, 0, SEEK_SET);
    void* model = malloc(msz);
    if (!model || fread(model, 1, msz, fm) != (size_t)msz) { perror("read model"); return -1; }
    fclose(fm);

    const rknn_core_mask mask_of[2] = { RKNN_NPU_CORE_0, RKNN_NPU_CORE_1 };
    /* 首条带左边缘、末条带右边缘与帧边界齐平——外侧交给模型自身 padding，
       避免注入"边框零"（INT8 非对称量化下 uint8 0 ≠ 实值 0，会在边缘引入 ~0.1 假偏差）。 */
    const int starts[N_SLICES] = { 0, EST_W - SLICE_W_IN };

    for (int i = 0; i < N_SLICES; i++) {
        slice_t* s = &g_sl[i];
        memset(s, 0, sizeof(*s));
        s->idx = i;
        s->start = starts[i];
        s->ret_init = rknn_init(&s->ctx, model, (uint32_t)msz, 0, NULL);
        s->ret_mask = (s->ret_init == 0) ? rknn_set_core_mask(s->ctx, mask_of[i]) : -1;
        s->in_buf = (uint8_t*)calloc((size_t)SLICE_W_IN * SLICE_H * 3, 1);
        s->input.index = 0;
        s->input.buf = s->in_buf;
        s->input.size = (uint32_t)((size_t)SLICE_W_IN * SLICE_H * 3);
        s->input.type = RKNN_TENSOR_UINT8;
        s->input.fmt  = RKNN_TENSOR_NHWC;
        s->output.index = 0;
        s->output.want_float = 1;
        s->out_float = (float*)malloc((size_t)SLICE_H * SLICE_W_IN * 3 * sizeof(float));
        if (s->ret_init != 0 || !s->in_buf || !s->out_float) {
            fprintf(stderr, "[npu] slice %d 初始化失败 (ret=%d)\n", i, s->ret_init);
            return -1;
        }
        fprintf(stderr, "[npu] slice %d start=%d core_mask_ret=%d\n", i, s->start, s->ret_mask);
        pthread_create(&g_sl_th[i], NULL, slice_worker, s);
    }
    free(model);
    g_npu_ready = 1;
    fprintf(stderr, "[npu] 模型 %s（%ld B）2 条带就绪\n", model_path, msz);
    return 0;
}

/* 跑一轮估计，把结果（含 EMA）写入 dst（3 × EST_EFF_H × EST_W，float32，通道平面）。 */
static int npu_estimate(float* dst, float* ema_state, float alpha, int use_ema, int first) {
    static float slice_field[3 * SLICE_H * EST_W];
    const double t0 = now_ms();

    pthread_mutex_lock(&g_sl_mtx);           /* 放开 worker：各自 build + rknn_run */
    g_sl_done = 0;
    g_sl_gen++;
    pthread_cond_broadcast(&g_sl_cv);
    while (g_sl_done < N_SLICES) pthread_cond_wait(&g_sl_cv, &g_sl_mtx);
    pthread_mutex_unlock(&g_sl_mtx);
    const double t1 = now_ms();
    stat_add(&S_npu, t1 - t0);

    memset(slice_field, 0, sizeof(slice_field));
    for (int i = 0; i < N_SLICES; i++) {
        slice_t* s = &g_sl[i];
        for (int y = 0; y < SLICE_H; y++) {
            for (int c = 0; c < SLICE_W_OUT; c++) {
                const int sc = i * SLICE_W_OUT - s->start + c;
                if (sc < 0 || sc >= SLICE_W_IN) continue;
                for (int ch = 0; ch < 3; ch++)
                    slice_field[((size_t)ch * SLICE_H + y) * EST_W + i * SLICE_W_OUT + c] =
                        s->out_float[((size_t)ch * SLICE_H + y) * SLICE_W_IN + sc];
            }
        }
    }

    /* 只取有效区（丢弃 pad 行），并按 §9.1 做 EMA（α 为历史权重） */
    for (int ch = 0; ch < 3; ch++) {
        const float* src = slice_field + ((size_t)ch * SLICE_H + PAD_TOP) * EST_W;
        float* d = dst + (size_t)ch * EST_EFF_H * EST_W;
        if (!use_ema || first) {
            memcpy(d, src, sizeof(float) * EST_EFF_H * EST_W);
        } else {
            for (int k = 0; k < EST_EFF_H * EST_W; k++)
                d[k] = alpha * d[k] + (1.0f - alpha) * src[k];
        }
    }
    (void)ema_state;
    return 0;
}

static void npu_deinit(void) {
    if (!g_npu_ready) return;
    pthread_mutex_lock(&g_sl_mtx);           /* 停止信号与工作信号同锁发布，无竞态窗口 */
    g_sl_stop = 1;
    pthread_cond_broadcast(&g_sl_cv);
    pthread_mutex_unlock(&g_sl_mtx);
    for (int i = 0; i < N_SLICES; i++) {
        pthread_join(g_sl_th[i], NULL);
        if (g_sl[i].in_buf) free(g_sl[i].in_buf);
        if (g_sl[i].out_float) free(g_sl[i].out_float);
        if (g_sl[i].ctx) rknn_destroy(g_sl[i].ctx);
    }
    g_npu_ready = 0;
}

/* ───────────────────────── DRM 显示（DSI-1 / CRTC 142 / 1024×600） ───────────────────────── */
typedef struct {
    int fd;
    uint32_t conn_id, crtc_id;
    drmModeModeInfo mode;
    uint32_t fb[2];
    int nbuf, cur;
    int canvas_fd[2];
    void* canvas_va[2];
    rga_buffer_t canvas_rga[2];
    int flip_pending;
} drm_t;

static volatile int g_flip_done = 1;

static void flip_handler(int fd, unsigned seq, unsigned tv_sec, unsigned tv_usec, void* ud) {
    (void)fd; (void)seq; (void)tv_sec; (void)tv_usec; (void)ud;
    g_flip_done = 1;
}

static int drm_open_connector(drm_t* d, uint32_t want_conn) {
    drmModeRes* res = drmModeGetResources(d->fd);
    if (!res) { fprintf(stderr, "[drm] drmModeGetResources 失败\n"); return -1; }
    drmModeConnector* conn = NULL;
    if (want_conn) conn = drmModeGetConnector(d->fd, want_conn);
    if (!conn) {                                  /* 按名字 DSI-1 兜底 */
        for (int i = 0; i < res->count_connectors && !conn; i++) {
            drmModeConnector* c = drmModeGetConnector(d->fd, res->connectors[i]);
            if (!c) continue;
            if (c->connector_type == DRM_MODE_CONNECTOR_DSI &&
                c->connection == DRM_MODE_CONNECTED) conn = c;
            else drmModeFreeConnector(c);
        }
    }
    if (!conn) { fprintf(stderr, "[drm] 未找到 DSI connector\n"); drmModeFreeResources(res); return -1; }
    if (conn->connection != DRM_MODE_CONNECTED) {
        fprintf(stderr, "[drm] connector %u 未连接\n", conn->connector_id);
    }
    d->conn_id = conn->connector_id;

    /* 选 mode：优先 1024×600，否则 preferred */
    int mi = -1;
    for (int i = 0; i < conn->count_modes; i++) {
        if (conn->modes[i].hdisplay == CV_W && conn->modes[i].vdisplay == CV_H) { mi = i; break; }
    }
    if (mi < 0) for (int i = 0; i < conn->count_modes; i++) if (conn->modes[i].type & DRM_MODE_TYPE_PREFERRED) { mi = i; break; }
    if (mi < 0) mi = 0;
    d->mode = conn->modes[mi];
    fprintf(stderr, "[drm] connector %u  mode=%s %ux%u@%u\n", d->conn_id, d->mode.name,
            d->mode.hdisplay, d->mode.vdisplay, d->mode.vrefresh);

    drmModeEncoder* enc = conn->encoder_id ? drmModeGetEncoder(d->fd, conn->encoder_id) : NULL;
    if (enc) { d->crtc_id = enc->crtc_id; drmModeFreeEncoder(enc); }
    if (!d->crtc_id) {
        /* 兜底：找第一个能配该 connector 的 CRTC */
        for (int i = 0; i < res->count_crtcs; i++) { d->crtc_id = res->crtcs[i]; break; }
    }
    fprintf(stderr, "[drm] crtc %u\n", d->crtc_id);
    drmModeFreeConnector(conn);
    drmModeFreeResources(res);
    return 0;
}

/* 画布格式：RGB888（DRM_FORMAT_RGB888）。选它而非 XRGB8888 的理由——RGA 侧
 * RK_FORMAT_RGB_888 → RK_FORMAT_RGB_888 是同格式 blit（已在 M0-13 验证），
 * 完全绕开"RGA 能否一次完成 RGB888→XRGB8888"这一未验证项。 */
static int drm_create_fb_rgb888(drm_t* d, int fd, uint32_t* out_fb) {
    uint32_t handle = 0;
    if (drmPrimeFDToHandle(d->fd, fd, &handle) < 0) {
        fprintf(stderr, "[drm] drmPrimeFDToHandle 失败：%s\n", strerror(errno));
        return -1;
    }
    uint32_t handles[4] = { handle, 0, 0, 0 };
    uint32_t pitches[4] = { CV_W * 3, 0, 0, 0 };
    uint32_t offsets[4] = { 0, 0, 0, 0 };
    if (drmModeAddFB2(d->fd, CV_W, CV_H, DRM_FORMAT_RGB888, handles, pitches, offsets, out_fb, 0) < 0) {
        fprintf(stderr, "[drm] drmModeAddFB2(RGB888) 失败：%s\n", strerror(errno));
        return -1;
    }
    return 0;
}

static int drm_init(drm_t* d, int want_conn, int with_canvas) {
    memset(d, 0, sizeof(*d));
    d->fd = open(DEF_CARD, O_RDWR | O_CLOEXEC);
    if (d->fd < 0) { fprintf(stderr, "open %s: %s\n", DEF_CARD, strerror(errno)); return -1; }
    if (drmSetMaster(d->fd) != 0) {
        fprintf(stderr, "[drm] drmSetMaster 失败：%s（需先释放桌面：systemctl isolate multi-user.target）\n",
                strerror(errno));
        return -1;
    }
    if (drm_open_connector(d, (uint32_t)want_conn) < 0) return -1;
    if (!with_canvas) return 0;

    d->nbuf = 2;
    for (int i = 0; i < d->nbuf; i++) {
        d->canvas_fd[i] = dma_heap_alloc("cma", (size_t)CV_W * CV_H * 3, &d->canvas_va[i]);
        if (d->canvas_fd[i] < 0) return -1;
        d->canvas_rga[i] = wrapbuffer_virtualaddr_t(d->canvas_va[i], CV_W, CV_H, CV_W, CV_H, RK_FORMAT_RGB_888);
        if (drm_create_fb_rgb888(d, d->canvas_fd[i], &d->fb[i]) < 0) return -1;
        /* 整幅清黑一次（两窗每帧都会被重写，其余区域保持黑） */
        memset(d->canvas_va[i], 0, (size_t)CV_W * CV_H * 3);
    }
    fprintf(stderr, "[drm] 画布 %d 块就绪（RGB888 %dx%d，stride %d）\n", d->nbuf, CV_W, CV_H, CV_W * 3);
    return 0;
}

static int drm_set_crtc(drm_t* d) {
    if (drmModeSetCrtc(d->fd, d->crtc_id, d->fb[0], 0, 0, &d->conn_id, 1, &d->mode) < 0) {
        fprintf(stderr, "[drm] drmModeSetCrtc 失败：%s\n", strerror(errno));
        return -1;
    }
    d->cur = 0;
    return 0;
}

/* 非阻塞 pageflip：只在上一次 flip 回调完成后才发下一次 */
static int drm_flip(drm_t* d, int idx) {
    if (!g_flip_done) {
        drmEventContext ev;
        memset(&ev, 0, sizeof(ev));
        ev.version = DRM_EVENT_CONTEXT_VERSION;
        ev.page_flip_handler = flip_handler;
        int guard = 0;
        while (!g_flip_done && guard++ < 1000) {
            int r = drmHandleEvent(d->fd, &ev);
            if (r < 0 && errno != EINTR) break;
        }
    }
    g_flip_done = 0;
    if (drmModePageFlip(d->fd, d->crtc_id, d->fb[idx], DRM_MODE_PAGE_FLIP_EVENT, NULL) < 0) {
        fprintf(stderr, "[drm] drmModePageFlip 失败：%s\n", strerror(errno));
        g_flip_done = 1;
        return -1;
    }
    d->cur = idx;
    return 0;
}

static void drm_wait_flip(drm_t* d) {
    drmEventContext ev;
    memset(&ev, 0, sizeof(ev));
    ev.version = DRM_EVENT_CONTEXT_VERSION;
    ev.page_flip_handler = flip_handler;
    int guard = 0;
    while (!g_flip_done && guard++ < 1000) {
        int r = drmHandleEvent(d->fd, &ev);
        if (r < 0 && errno != EINTR) break;
    }
}

/* ───────────────────────── dumb buffer 冒烟（只验 master/pageflip 通路） ───────────────────────── */
static int drm_smoke(void) {
    drm_t d;
    if (drm_init(&d, 0, 0) < 0) return 1;

    const uint32_t W = d.mode.hdisplay, H = d.mode.vdisplay;
    struct drm_mode_create_dumb cd;
    memset(&cd, 0, sizeof(cd));
    cd.width = W; cd.height = H; cd.bpp = 32;
    if (ioctl(d.fd, DRM_IOCTL_MODE_CREATE_DUMB, &cd) < 0) { perror("CREATE_DUMB"); return 1; }
    uint32_t fb;
    if (drmModeAddFB(d.fd, W, H, 24, 32, cd.pitch, cd.handle, &fb) < 0) {
        perror("drmModeAddFB"); return 1;
    }
    struct drm_mode_map_dumb md;
    memset(&md, 0, sizeof(md));
    md.handle = cd.handle;
    if (ioctl(d.fd, DRM_IOCTL_MODE_MAP_DUMB, &md) < 0) { perror("MAP_DUMB"); return 1; }
    uint32_t* map = mmap(NULL, cd.size, PROT_READ | PROT_WRITE, MAP_SHARED, d.fd, md.offset);
    if (map == MAP_FAILED) { perror("mmap dumb"); return 1; }

    /* 8 条竖色条 */
    for (uint32_t y = 0; y < H; y++)
        for (uint32_t x = 0; x < W; x++) {
            uint32_t band = x * 8 / W;
            uint32_t r = (band & 1) ? 255 : 0, g = (band & 2) ? 255 : 0, b = (band & 4) ? 255 : 0;
            map[(size_t)y * (cd.pitch / 4) + x] = (r << 16) | (g << 8) | b;
        }
    if (drmModeSetCrtc(d.fd, d.crtc_id, fb, 0, 0, &d.conn_id, 1, &d.mode) < 0) {
        fprintf(stderr, "[smoke] drmModeSetCrtc 失败：%s\n", strerror(errno));
        return 1;
    }
    g_flip_done = 1;
    printf("{\"smoke\":\"ok\",\"connector\":%u,\"crtc\":%u,\"mode\":\"%s\",\"canvas\":\"%ux%u XRGB8888 dumb\"}\n",
           d.conn_id, d.crtc_id, d.mode.name, W, H);
    return 0;
}

/* ───────────────────────── 画布合成 ───────────────────────── */
static int canvas_compose(drm_t* d, int idx) {
    im_rect prect = {0, 0, 0, 0};
    rga_buffer_t pat;
    memset(&pat, 0, sizeof(pat));
    im_rect s = {0, 0, WIN_W, WIN_H};
    im_rect d0 = {0, 0, WIN_W, WIN_H};
    im_rect d1 = {WIN_W, 0, WIN_W, WIN_H};
    IM_STATUS a = improcess(g_win_full, d->canvas_rga[idx], pat, s, d0, prect, IM_SYNC);
    IM_STATUS b = improcess(g_enh_full, d->canvas_rga[idx], pat, s, d1, prect, IM_SYNC);
    if (a != IM_STATUS_SUCCESS || b != IM_STATUS_SUCCESS) {
        fprintf(stderr, "[canvas] 合成失败：%s / %s\n", imStrError_t(a), imStrError_t(b));
        return -1;
    }
    return 0;
}

/* ───────────────────────── main ───────────────────────── */
static void usage(const char* p) {
    fprintf(stderr,
        "用法：%s <--smoke | --offline | --display> [选项]\n"
        "  --smoke              DRM dumb buffer 色条冒烟（只验 master/pageflip）\n"
        "  --offline            离线单帧全链路（不出屏），配合 --dump-dir 落图对账\n"
        "  --display            实时出屏（左右并排双窗）\n"
        "  --device PATH        采集节点（默认 %s）\n"
        "  --model PATH         ZDE++ 切片模型（默认 %s）\n"
        "  --dump-dir DIR       落盘 raw_win/enh_win/curve/est/canvas\n"
        "  --report PATH        写 JSON 报告\n"
        "  --frames N           处理 N 帧后退出（0 = 无限，默认 0）\n"
        "  --est-every N        每 N 帧做一次曲线估计（默认 %d）\n"
        "  --alpha F            EMA 历史权重（默认 %.2f）\n"
        "  --no-ema             关闭 EMA（曲线场每帧直通）\n"
        "  --skip-frames N      开流后先丢弃 N 帧（默认 %d）\n"
        "  --copy-cap           强制把 NV12 拷到 staging 缓冲再交给 RGA\n"
        "  --connector ID       DRM connector id（默认 0 = 运行时解析 DSI）\n",
        p, DEF_DEVICE, DEF_MODEL, DEF_EST_EVERY, DEF_ALPHA, DEF_SKIP);
}

static void dump_file(const char* dir, const char* name, const void* p, size_t n) {
    char path[512];
    snprintf(path, sizeof(path), "%s/%s", dir, name);
    FILE* f = fopen(path, "wb");
    if (!f) { fprintf(stderr, "[dump] 打不开 %s：%s\n", path, strerror(errno)); return; }
    if (fwrite(p, 1, n, f) != n) perror("dump write");
    fclose(f);
    fprintf(stderr, "[dump] %s (%zu B)\n", path, n);
}

int main(int argc, char** argv) {
    int mode_smoke = 0, mode_offline = 0, mode_display = 0;
    const char* device = DEF_DEVICE;
    const char* model  = DEF_MODEL;
    const char* dump_dir = NULL;
    const char* report = NULL;
    long frames = 0;
    int est_every = DEF_EST_EVERY;
    float alpha = DEF_ALPHA;
    int use_ema = 1;
    int skip = DEF_SKIP;
    int want_conn = 0;

    for (int i = 1; i < argc; i++) {
        const char* a = argv[i];
        if      (!strcmp(a, "--smoke"))        mode_smoke = 1;
        else if (!strcmp(a, "--offline"))      mode_offline = 1;
        else if (!strcmp(a, "--display"))      mode_display = 1;
        else if (!strcmp(a, "--no-ema"))       use_ema = 0;
        else if (!strcmp(a, "--copy-cap"))     g_copy_cap = 1;
        else if (!strcmp(a, "--device") && i + 1 < argc)    device = argv[++i];
        else if (!strcmp(a, "--model") && i + 1 < argc)     model = argv[++i];
        else if (!strcmp(a, "--dump-dir") && i + 1 < argc)  dump_dir = argv[++i];
        else if (!strcmp(a, "--report") && i + 1 < argc)    report = argv[++i];
        else if (!strcmp(a, "--frames") && i + 1 < argc)    frames = atol(argv[++i]);
        else if (!strcmp(a, "--est-every") && i + 1 < argc) est_every = atoi(argv[++i]);
        else if (!strcmp(a, "--alpha") && i + 1 < argc)     alpha = (float)atof(argv[++i]);
        else if (!strcmp(a, "--skip-frames") && i + 1 < argc) skip = atoi(argv[++i]);
        else if (!strcmp(a, "--connector") && i + 1 < argc) want_conn = atoi(argv[++i]);
        else if (!strcmp(a, "-h") || !strcmp(a, "--help")) { usage(argv[0]); return 0; }
        else { fprintf(stderr, "未知参数：%s\n", a); usage(argv[0]); return 2; }
    }
    if (!mode_smoke && !mode_offline && !mode_display) { usage(argv[0]); return 2; }
    if (est_every < 1) est_every = 1;

    S_cap.key = "cap_dqbuf_ms"; S_est.key = "rga_est_ms"; S_win.key = "rga_win_ms";
    S_apply.key = "apply_ms"; S_npu.key = "npu_est_ms"; S_canvas.key = "canvas_ms";
    S_flip.key = "pageflip_ms"; S_frame.key = "frame_ms";

    if (mode_smoke) return drm_smoke();

    /* ---- 采集 ---- */
    cap_t cap;
    if (cap_init(&cap, device) < 0) return 3;
    if (cap_stream_on(&cap) < 0) return 3;
    fprintf(stderr, "[main] 已开流（进程内只开一次）\n");

    /* ---- RGA / 应用 / NPU ---- */
    if (rga_init() < 0) return 4;
    apply_init();
    if (npu_init(model) < 0) return 4;
    g_est_rgb = (const uint8_t*)g_est_va;

    /* ---- DRM ---- */
    drm_t drm;
    int have_drm = 0;
    if (mode_display) {
        if (drm_init(&drm, want_conn, 1) < 0) return 5;
        if (drm_set_crtc(&drm) < 0) return 5;
        have_drm = 1;
        g_flip_done = 1;
    }

    /* ---- 曲线场（EMA 状态） ---- */
    const size_t CVN = (size_t)3 * EST_EFF_H * EST_W;
    float* curve = (float*)calloc(CVN, sizeof(float));
    if (!curve) { fprintf(stderr, "oom curve\n"); return 6; }
    int first_est = 1;

    /* ---- 主循环 ---- */
    long n = 0, n_est = 0, n_timeout = 0, n_flip_skip = 0;
    long selfcheck_bad = -1, selfcheck_max = -1;
    const double t_start = now_ms();
    int quit = 0;
    while (!quit) {
        size_t used = 0;
        uint64_t ts = 0;
        uint32_t tsrc = 0;
        const double t_frame0 = now_ms();
        double t0 = now_ms();
        int idx = cap_dqbuf(&cap, &used, &ts, &tsrc, 1000);
        if (idx < 0) { if (++n_timeout > 50) { fprintf(stderr, "[main] DQBUF 连续超时，退出\n"); break; } continue; }
        n_timeout = 0;
        stat_add(&S_cap, now_ms() - t0);

        if (n < skip) { cap_qbuf(&cap, idx); n++; continue; }

        /* 源 NV12：优先直接用 V4L2 dmabuf fd（零拷贝），失败则 staging 拷贝兜底 */
        int src_fd = cap.buf[idx].dmabuf_fd;
        if (src_fd < 0 || g_copy_cap) {
            memcpy(g_stage_va, cap.buf[idx].start, CAP_BYTES);
            src_fd = g_stage_fd;
        }

        /* ① RGA → 480×272（有效区） */
        t0 = now_ms();
        IM_STATUS st = rga_blit_nv12(src_fd, g_est_eff, EST_W, EST_EFF_H);
        if (st != IM_STATUS_SUCCESS && !g_copy_cap) {
            fprintf(stderr, "[rga] 直接喂 V4L2 dmabuf 失败（%s）⇒ 切 staging 拷贝\n", imStrError_t(st));
            g_copy_cap = 1;
            memcpy(g_stage_va, cap.buf[idx].start, CAP_BYTES);
            st = rga_blit_nv12(g_stage_fd, g_est_eff, EST_W, EST_EFF_H);
        }
        if (st != IM_STATUS_SUCCESS) { fprintf(stderr, "[rga] est blit 失败：%s\n", imStrError_t(st)); cap_qbuf(&cap, idx); continue; }
        stat_add(&S_est, now_ms() - t0);

        /* ② 曲线估计（每 est_every 帧一次；EMA 用 d[k] 自身作历史） */
        if (first_est || (n % est_every) == 0) {
            npu_estimate(curve, curve, alpha, use_ema, first_est);
            first_est = 0; n_est++;
        }

        /* ③ RGA → 512×288 原图窗 */
        t0 = now_ms();
        st = rga_blit_nv12(src_fd, g_win_full, WIN_W, WIN_H);
        if (st != IM_STATUS_SUCCESS) { fprintf(stderr, "[rga] win blit 失败：%s\n", imStrError_t(st)); cap_qbuf(&cap, idx); continue; }
        stat_add(&S_win, now_ms() - t0);

        /* ④ 曲线应用（8 iter 单遍融合，含双线性采样） */
        t0 = now_ms();
        g_ap_src = (const uint8_t*)g_win_va;
        g_ap_curve = curve;
        g_ap_out = (uint8_t*)g_enh_va;
        apply_fused_neon();
        stat_add(&S_apply, now_ms() - t0);

        /* ⑤ 画布合成 + pageflip */
        if (have_drm) {
            int nb = 1 - drm.cur;
            t0 = now_ms();
            if (canvas_compose(&drm, nb) == 0) {
                stat_add(&S_canvas, now_ms() - t0);
                t0 = now_ms();
                drm_wait_flip(&drm);
                if (drm_flip(&drm, nb) < 0) n_flip_skip++;
                else stat_add(&S_flip, now_ms() - t0);
            }
        }
        cap_qbuf(&cap, idx);

        stat_add(&S_frame, now_ms() - t_frame0);
        n++;

        /* 离线模式：只跑一帧（落图与自检在循环外统一做） */
        if (mode_offline) quit = 1;
        if (frames > 0 && n >= frames) quit = 1;
    }

    /* ---- 落图 + NEON/标量逐位自检（两种模式都做，便于留证与目视核对） ---- */
    if (n > 0) {
        uint8_t* sc = (uint8_t*)malloc(WIN_BYTES);
        if (sc) {
            apply_fused_scalar_to(sc);
            long bad = 0, mx = 0;
            const uint8_t* ne = (const uint8_t*)g_enh_va;
            for (size_t k = 0; k < WIN_BYTES; k++) {
                int dd = (int)ne[k] - (int)sc[k];
                if (dd < 0) dd = -dd;
                if (dd) { bad++; if (dd > mx) mx = dd; }
            }
            selfcheck_bad = bad; selfcheck_max = mx;
            fprintf(stderr, "[selfcheck] NEON vs 标量：mismatch_bytes=%ld max_abs=%ld / %zu\n",
                    bad, mx, WIN_BYTES);
            if (dump_dir) dump_file(dump_dir, "enh_scalar.rgb", sc, WIN_BYTES);
            free(sc);
        }
        if (dump_dir) {
            dump_file(dump_dir, "est.rgb", g_est_va, EST_BYTES);
            dump_file(dump_dir, "raw_win.rgb", g_win_va, WIN_BYTES);
            dump_file(dump_dir, "enh_win.rgb", g_enh_va, WIN_BYTES);
            dump_file(dump_dir, "curve.f32", curve, CVN * sizeof(float));
            if (have_drm) dump_file(dump_dir, "canvas.rgb", drm.canvas_va[drm.cur], (size_t)CV_W * CV_H * 3);
        }
    }

    const double wall = now_ms() - t_start;

    /* ---- 报告 ---- */
    FILE* out = report ? fopen(report, "w") : stdout;
    if (!out) { fprintf(stderr, "open report %s: %s\n", report, strerror(errno)); out = stdout; }
    fprintf(out, "{\n\"tool\":\"tools/pipeline_probe.c\",\"mode\":\"%s\",\n",
            mode_display ? "display" : "offline");
    fprintf(out, "\"geom\":{\"cap\":\"%dx%d NV12\",\"est\":\"%dx%d (eff %dx%d)\",\"win\":\"%dx%d RGB888\",\"canvas\":\"%dx%d RGB888\"},\n",
            CAP_W, CAP_H, EST_W, EST_H, EST_W, EST_EFF_H, WIN_W, WIN_H, CV_W, CV_H);
    fprintf(out, "\"params\":{\"est_every\":%d,\"alpha\":%.3f,\"ema\":%d,\"copy_cap\":%d,\"n_slices\":%d,\"halo\":%d},\n",
            est_every, alpha, use_ema, g_copy_cap, N_SLICES, HALO);
    fprintf(out, "\"counters\":{\"frames\":%ld,\"estimates\":%ld,\"flip_skipped\":%ld,\"wall_s\":%.2f,\"fps\":%.2f},\n",
            n, n_est, n_flip_skip, wall / 1000.0, wall > 0 ? n * 1000.0 / wall : 0.0);
    fprintf(out, "\"selfcheck_neon_vs_scalar\":{\"mismatch_bytes\":%ld,\"max_abs\":%ld,\"total\":%zu},\n",
            selfcheck_bad, selfcheck_max, WIN_BYTES);
    stat_print(out, &S_cap, 0);
    stat_print(out, &S_est, 0);
    stat_print(out, &S_win, 0);
    stat_print(out, &S_apply, 0);
    stat_print(out, &S_npu, 0);
    stat_print(out, &S_canvas, 0);
    stat_print(out, &S_flip, 0);
    stat_print(out, &S_frame, 1);
    fprintf(out, "}\n");
    if (out != stdout) fclose(out);

    /* ---- 收尾 ---- */
    npu_deinit();
    cap_deinit(&cap);
    free(curve);
    free(g_xt); free(g_yt);
    fprintf(stderr, "[main] 结束：%ld 帧 / %ld 次估计 / %.2f s / %.2f fps\n",
            n, n_est, wall / 1000.0, wall > 0 ? n * 1000.0 / wall : 0.0);
    return 0;
}
