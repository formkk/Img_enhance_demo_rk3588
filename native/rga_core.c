/* rga_core.c —— 见 rga_core.h。自 tools/pipeline_probe.c 的 rga_* 逐段移植（算法未改）。 */

#define _GNU_SOURCE
#include "rga_core.h"

#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#include "im2d.h"

/* ── dma-buf heap（搬自 tools/rga_probe.c）────────────────────────────── */
struct dma_heap_alloc_data {
    uint64_t len;
    uint32_t fd;
    uint32_t fd_flags;
    uint64_t heap_flags;
};
#define DMA_HEAP_IOC_MAGIC 'H'
#define DMA_HEAP_IOCTL_ALLOC _IOWR(DMA_HEAP_IOC_MAGIC, 0x0, struct dma_heap_alloc_data)

static int dma_heap_alloc(const char* heap, size_t len, void** va) {
    char path[128];
    snprintf(path, sizeof(path), "/dev/dma_heap/%s", heap);
    int hfd = open(path, O_RDWR | O_CLOEXEC);
    if (hfd < 0) return -1;
    struct dma_heap_alloc_data d;
    memset(&d, 0, sizeof(d));
    d.len = len;
    d.fd_flags = O_RDWR | O_CLOEXEC;
    if (ioctl(hfd, DMA_HEAP_IOCTL_ALLOC, &d) < 0) { close(hfd); return -1; }
    close(hfd);
    if (va) {
        void* p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, d.fd, 0);
        if (p == MAP_FAILED) { close(d.fd); return -1; }
        *va = p;
    }
    return (int)d.fd;
}

static uint64_t mono_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static int dma_heap_alloc(const char* heap, size_t len, void** va);

int dma_heap_alloc_system(size_t nbytes, void** va) {
    return dma_heap_alloc("system-dma32", nbytes, va);
}

/* RGA 不接受高度 <2 的矩形（M0-13 实测）⇒ pad 按 2 行填 */
#define RGA_FILL_H 2u

static struct {
    int   configured;
    rga_cfg_t cfg;
    int   est_fd, win_fd, enh_fd, zero_fd, stage_fd, hq_fd;
    void *est_va, *win_va, *enh_va, *zero_va, *stage_va, *hq_va;
    rga_buffer_t est_full, est_eff, win_full, enh_full, zero_full, stage_full, hq_content;
    uint64_t last_blit_ns, last_canvas_ns, last_scale_ns;
} g;

int rga_is_configured(void) { return g.configured; }

int rga_configure(const rga_cfg_t* cfg) {
    if (g.configured) return RGA_E_STATE;
    if (!cfg || cfg->eff_w < 2 || cfg->eff_h < 2 || cfg->pad_h < cfg->eff_h) return RGA_E_STATE;
    g.cfg = *cfg;

    const size_t est_bytes = (size_t)cfg->pad_w * cfg->pad_h * 3;
    const size_t win_bytes = (size_t)cfg->win_w * cfg->win_h * 3;
    const size_t cap_bytes = (size_t)cfg->cap_w * cfg->cap_h * 3 / 2;
    g.est_fd    = dma_heap_alloc("system-dma32", est_bytes, &g.est_va);
    g.win_fd    = dma_heap_alloc("system-dma32", win_bytes, &g.win_va);
    g.enh_fd    = dma_heap_alloc("system-dma32", win_bytes, &g.enh_va);
    g.zero_fd   = dma_heap_alloc("system-dma32", est_bytes, &g.zero_va);
    g.stage_fd  = dma_heap_alloc("system-dma32", cap_bytes, &g.stage_va);
    if (g.est_fd < 0 || g.win_fd < 0 || g.enh_fd < 0 || g.zero_fd < 0 || g.stage_fd < 0)
        return RGA_E_STATE;
    memset(g.zero_va, 0, est_bytes);

    /* **（v3.84）** HQ 关键帧输入目标（TH3）。`hq_w/hq_h` 为 0 时**不分配**，
       既有 TH4 调用方行为与改造前完全一致（零回归）。
       **（v3.86 追加 35）** 按 §3.1 `hq_shape_ladder` 主档：缓冲为 `hq_w×hq_h`（960×544），
       内容只写第 `hq_pad_top..hq_h-hq_pad_top-1` 行（960×540），上下各 2 行为**零 pad**
       ——pad 行在下述一次性 memset 后不再被写（`dst_of` 返回内容子视图；哨兵校验见工具
       `tools/hq_frame_review.py` 的 `pad_rows_zero`）。 */
    g.hq_fd = -1;
    g.hq_va = NULL;
    memset(&g.hq_content, 0, sizeof(g.hq_content));
    if (cfg->hq_w > 0 && cfg->hq_h > 0) {
        if (cfg->hq_pad_top * 2u >= cfg->hq_h) return RGA_E_STATE;   /* 内容高必须 > 0 */
        const size_t hq_bytes = (size_t)cfg->hq_w * cfg->hq_h * 3;
        g.hq_fd = dma_heap_alloc("system-dma32", hq_bytes, &g.hq_va);
        if (g.hq_fd < 0) return RGA_E_STATE;
        memset(g.hq_va, 0, hq_bytes);          /* pad 行 = 零；blit 只写内容子视图 ⇒ 保持零 */
        /* 内容子视图：偏移基址（drect 的 y 偏移须偶对齐；与 est_eff 同机制，O32） */
        g.hq_content = wrapbuffer_virtualaddr_t(
            (uint8_t*)g.hq_va + (size_t)cfg->hq_w * 3 * cfg->hq_pad_top,
            cfg->hq_w, cfg->hq_h - 2 * cfg->hq_pad_top,
            cfg->hq_w, cfg->hq_h, RK_FORMAT_RGB_888);
    }

    g.est_full = wrapbuffer_virtualaddr_t(g.est_va, cfg->pad_w, cfg->pad_h,
                                          cfg->pad_w, cfg->pad_h, RK_FORMAT_RGB_888);
    /* 偏移基址子视图：有效区落在整幅 pad 缓冲的第 pad_top..pad_top+eff_h-1 行
       （drect 的 y 偏移须偶对齐，dy=1 不可表达 ⇒ 只能按 wstride*hstride 偏移基址） */
    g.est_eff = wrapbuffer_virtualaddr_t((uint8_t*)g.est_va + (size_t)cfg->pad_w * 3 * cfg->pad_top,
                                         cfg->eff_w, cfg->eff_h, cfg->pad_w, cfg->pad_h,
                                         RK_FORMAT_RGB_888);
    g.win_full = wrapbuffer_virtualaddr_t(g.win_va, cfg->win_w, cfg->win_h,
                                          cfg->win_w, cfg->win_h, RK_FORMAT_RGB_888);
    g.enh_full = wrapbuffer_virtualaddr_t(g.enh_va, cfg->win_w, cfg->win_h,
                                          cfg->win_w, cfg->win_h, RK_FORMAT_RGB_888);
    g.zero_full = wrapbuffer_virtualaddr_t(g.zero_va, cfg->pad_w, cfg->pad_h,
                                           cfg->pad_w, cfg->pad_h, RK_FORMAT_RGB_888);
    g.stage_full = wrapbuffer_virtualaddr_t(g.stage_va, cfg->cap_w, cfg->cap_h,
                                            cfg->cap_w, cfg->cap_h, RK_FORMAT_YCbCr_420_SP);

    if (cfg->core_mask) {
        const IM_STATUS s = imconfig(IM_CONFIG_SCHEDULER_CORE, (uint64_t)cfg->core_mask);
        if (s != IM_STATUS_SUCCESS) return RGA_E_STATE;
    }

    /* pad 零填充：imfill 对 RGB888 失败（M0-13）⇒ 用 improcess 从零源块拷入，每缓冲各一次。
       随后的 blit 会覆盖有效区，故结果正确（M0-13 哨兵验证：blit 不越写 pad 行）。 */
    if (cfg->pad_top > 0) {
        im_rect top = {0, 0, (int)cfg->pad_w, (int)RGA_FILL_H};
        im_rect bot = {0, (int)(cfg->pad_h - RGA_FILL_H), (int)cfg->pad_w, (int)RGA_FILL_H};
        im_rect prect = {0, 0, 0, 0};
        rga_buffer_t pat;
        memset(&pat, 0, sizeof(pat));
        const IM_STATUS s1 = improcess(g.zero_full, g.est_full, pat, top, top, prect, IM_SYNC);
        const IM_STATUS s2 = improcess(g.zero_full, g.est_full, pat, bot, bot, prect, IM_SYNC);
        if (s1 != IM_STATUS_SUCCESS || s2 != IM_STATUS_SUCCESS) return RGA_E_STATE;
    }

    g.configured = 1;
    return RGA_OK;
}

void rga_shutdown(void) {
    /* 只解映射；dmabuf fd 不 close（§5 硬契约），随进程释放 */
    void* vas[] = { g.est_va, g.win_va, g.enh_va, g.zero_va, g.stage_va, g.hq_va };
    size_t lens[6];
    lens[0] = (size_t)g.cfg.pad_w * g.cfg.pad_h * 3;
    lens[1] = (size_t)g.cfg.win_w * g.cfg.win_h * 3;
    lens[2] = lens[1];
    lens[3] = lens[0];
    lens[4] = (size_t)g.cfg.cap_w * g.cfg.cap_h * 3 / 2;
    lens[5] = (size_t)g.cfg.hq_w * g.cfg.hq_h * 3;   /* hq_w/hq_h = 0 时长度 0，munmap 前已判空 */
    for (int i = 0; i < 6; i++)
        if (vas[i] && lens[i]) munmap(vas[i], lens[i]);
    memset(&g, 0, sizeof(g));
}

static rga_buffer_t* dst_of(rga_dst_t dst) {
    if (dst == RGA_DST_WIN) return &g.win_full;
    if (dst == RGA_DST_HQ)  return &g.hq_content;   /* 内容子视图（pad 行留零；§3.1） */
    return &g.est_eff;
}

static int blit_nv12(rga_buffer_t src, rga_dst_t dst) {
    /* **（v3.86 追加 39 / O54 就地裁定）NV12→RGB 的 range 约定必须显式**——
       原实现用 `improcess(...)` + `srect/drect` 且未指定色彩空间 ⇒ 走 librga 默认
       `IM_COLOR_SPACE_DEFAULT`（= YUV→RGB **BT.601 limited**），而 ISP 输出是 **full range**
       （板上实测：Y 平面 p5 = 0、U/V 均值 128.16/127.57 中性）⇒ 色度被放大 ~14% + 黑电平错位
       ⇒ **常规两窗与 HQ 模型输入同时偏绿**（同一帧四路对照：RGA 路 G/B = 1.087 ≈ limited 1.090，
       full 1.000）。
       **实测结论（同一帧，n=2）**：把 `color_space_mode` 置在缓冲上、仍走 `improcess` **不生效**
       （输出与 limited 逐通道差 ≤0.14%）⇒ 本函数改走 **`imcvtcolor(src, dst, sfmt, dfmt, mode, sync)`**
       （该 API 有显式 `mode` 形参）。
       **几何不变（故不再传 rect）**：`imcvtcolor` 按两缓冲的 full-rect 工作，而三个目标 wrapper 的
       full-rect **正好等于有效区**——`win_full` = 512×288；`est_eff` / `hq_content` 均按
       `wstride*hstride` 偏移基址且高度 = 有效高（见 `rga_configure`），pad 行仍不被写。
       ⚠️ 边界：BT.601 与 BT.709 的取舍属 **O54 定稿**（`isp.csc_matrix` / `isp.rgb_range` 仍为
       `null`，§10.4 未定稿）⇒ 本行是"与实测一致的显式值"，**不是 O54 的定稿**。 */
    const int csc = IM_YUV_TO_RGB_BT601_FULL;
    src.color_space_mode = csc;                /* 兜底：两缓冲都置（实测仅源侧置、走 improcess 无效） */
    rga_buffer_t *d = dst_of(dst);
    d->color_space_mode = csc;
    const uint64_t t0 = mono_ns();
    const IM_STATUS s = imcvtcolor(src, *d, RK_FORMAT_YCbCr_420_SP, RK_FORMAT_RGB_888, csc, 1);
    g.last_blit_ns = mono_ns() - t0;
    return (s == IM_STATUS_SUCCESS) ? RGA_OK : RGA_E_STATE;
}

int rga_blit_nv12_fd(int src_fd, rga_dst_t dst) {
    if (!g.configured || src_fd < 0) return RGA_E_STATE;
    if (dst == RGA_DST_HQ && (g.cfg.hq_w == 0 || g.cfg.hq_h == 0)) return RGA_E_STATE;
    rga_buffer_t src = wrapbuffer_fd_t(src_fd, g.cfg.cap_w, g.cfg.cap_h,
                                       g.cfg.cap_w, g.cfg.cap_h, RK_FORMAT_YCbCr_420_SP);
    return blit_nv12(src, dst);
}

int rga_blit_nv12_va(const void* src_va, rga_dst_t dst) {
    if (!g.configured || !src_va) return RGA_E_STATE;
    if (dst == RGA_DST_HQ && (g.cfg.hq_w == 0 || g.cfg.hq_h == 0)) return RGA_E_STATE;
    rga_buffer_t src = wrapbuffer_virtualaddr_t((void*)src_va, g.cfg.cap_w, g.cfg.cap_h,
                                                g.cfg.cap_w, g.cfg.cap_h, RK_FORMAT_YCbCr_420_SP);
    return blit_nv12(src, dst);
}

int rga_canvas_compose_to(void* canvas_va, uint32_t canvas_w, uint32_t canvas_h) {
    if (!g.configured || !canvas_va) return RGA_E_STATE;
    if (canvas_w < g.cfg.win_w * 2 || canvas_h < g.cfg.win_h) return RGA_E_STATE;
    rga_buffer_t canvas = wrapbuffer_virtualaddr_t(canvas_va, canvas_w, canvas_h,
                                                   canvas_w, canvas_h, RK_FORMAT_RGB_888);
    im_rect prect = {0, 0, 0, 0};
    rga_buffer_t pat;
    memset(&pat, 0, sizeof(pat));
    const im_rect s = {0, 0, (int)g.cfg.win_w, (int)g.cfg.win_h};
    const im_rect d0 = {0, 0, (int)g.cfg.win_w, (int)g.cfg.win_h};
    const im_rect d1 = {(int)g.cfg.win_w, 0, (int)g.cfg.win_w, (int)g.cfg.win_h};
    const uint64_t t0 = mono_ns();
    const IM_STATUS a = improcess(g.win_full, canvas, pat, s, d0, prect, IM_SYNC);
    const IM_STATUS b = improcess(g.enh_full, canvas, pat, s, d1, prect, IM_SYNC);
    g.last_canvas_ns = mono_ns() - t0;
    return (a == IM_STATUS_SUCCESS && b == IM_STATUS_SUCCESS) ? RGA_OK : RGA_E_STATE;
}

/* RGB888 → RGB888 缩放（见 rga_core.h）。调用口径与 `rga_blit_nv12_va` / `rga_canvas_compose_to`
   完全一致：`wrapbuffer_virtualaddr_t` + `improcess(IM_SYNC)`；core_mask 由 `rga_configure`
   一次性 `imconfig`，本函数不再重复配置。源/目标 rect 按整幅（full-rect）传。 */
int rga_scale_rgb888(const void* src_va, uint32_t src_w, uint32_t src_h,
                     void* dst_va, uint32_t dst_w, uint32_t dst_h) {
    if (!g.configured) return RGA_E_STATE;
    if (!src_va || !dst_va || src_w == 0 || src_h == 0 || dst_w == 0 || dst_h == 0)
        return RGA_E_STATE;
    rga_buffer_t src = wrapbuffer_virtualaddr_t((void*)src_va, src_w, src_h,
                                                src_w, src_h, RK_FORMAT_RGB_888);
    rga_buffer_t dst = wrapbuffer_virtualaddr_t(dst_va, dst_w, dst_h,
                                                dst_w, dst_h, RK_FORMAT_RGB_888);
    im_rect prect = {0, 0, 0, 0};
    rga_buffer_t pat;
    memset(&pat, 0, sizeof(pat));
    const im_rect srect = {0, 0, (int)src_w, (int)src_h};
    const im_rect drect = {0, 0, (int)dst_w, (int)dst_h};
    const uint64_t t0 = mono_ns();
    const IM_STATUS s = improcess(src, dst, pat, srect, drect, prect, IM_SYNC);
    g.last_scale_ns = mono_ns() - t0;
    return (s == IM_STATUS_SUCCESS) ? RGA_OK : RGA_E_STATE;   /* 与同文件既有口径一致（对外只暴露 OK/E_STATE） */
}

int rga_scale_rgb888_fd(int src_fd, uint32_t src_w, uint32_t src_h,
                        int dst_fd, uint32_t dst_w, uint32_t dst_h) {
    if (!g.configured) return RGA_E_STATE;
    if (src_fd < 0 || dst_fd < 0 || src_w == 0 || src_h == 0 || dst_w == 0 || dst_h == 0)
        return RGA_E_STATE;
    /* **fd 包裹**（dma-buf）：v3.86 A/B 实测比 VA 包裹快 1.2~1.6 ms（见 rga_core.h 注释与
       `docs/evidence/reports/display-arch-change/rga_scale_transport_probe.c`）。 */
    rga_buffer_t src = wrapbuffer_fd_t(src_fd, src_w, src_h, src_w, src_h, RK_FORMAT_RGB_888);
    rga_buffer_t dst = wrapbuffer_fd_t(dst_fd, dst_w, dst_h, dst_w, dst_h, RK_FORMAT_RGB_888);
    im_rect prect = {0, 0, 0, 0};
    rga_buffer_t pat;
    memset(&pat, 0, sizeof(pat));
    const im_rect srect = {0, 0, (int)src_w, (int)src_h};
    const im_rect drect = {0, 0, (int)dst_w, (int)dst_h};
    const uint64_t t0 = mono_ns();
    const IM_STATUS s = improcess(src, dst, pat, srect, drect, prect, IM_SYNC);
    g.last_scale_ns = mono_ns() - t0;
    return (s == IM_STATUS_SUCCESS) ? RGA_OK : RGA_E_STATE;
}

void* rga_est_va(void)     { return g.est_va; }
void* rga_est_eff_va(void) { return g.est_va ? (uint8_t*)g.est_va + (size_t)g.cfg.pad_w * 3 * g.cfg.pad_top : NULL; }
void* rga_win_va(void)     { return g.win_va; }
void* rga_enh_va(void)     { return g.enh_va; }
void* rga_stage_va(void)   { return g.stage_va; }
int   rga_est_fd(void)     { return g.est_fd; }
int   rga_win_fd(void)     { return g.win_fd; }
int   rga_enh_fd(void)   { return g.enh_fd; }
int   rga_stage_fd(void) { return g.stage_fd; }
void* rga_hq_va(void)    { return g.hq_va; }
int   rga_hq_fd(void)    { return g.hq_fd; }
uint64_t rga_last_blit_ns(void)  { return g.last_blit_ns; }
uint64_t rga_last_canvas_ns(void){ return g.last_canvas_ns; }
uint64_t rga_last_scale_ns(void) { return g.last_scale_ns; }
