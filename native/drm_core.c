/* drm_core.c —— 见 drm_core.h。自 tools/pipeline_probe.c 的 drm_* 逐段移植。 */

#define _GNU_SOURCE
#include "drm_core.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#include <xf86drm.h>
#include <xf86drmMode.h>
#include <drm_fourcc.h>

struct dma_heap_alloc_data { uint64_t len; uint32_t fd; uint32_t fd_flags; uint64_t heap_flags; };
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

static volatile int g_flip_done = 1;
static const char* g_err = "ok";
static int g_errno = 0;
static uint64_t g_last_flip_ns = 0;
static uint64_t g_last_flip_seq = 0;

static void flip_handler(int fd, unsigned seq, unsigned tv_sec, unsigned tv_usec, void* ud) {
    (void)fd; (void)seq; (void)tv_sec; (void)tv_usec;
    /* §6.5：显示时间戳取 vblank（drmEvent），不取提交时刻；seq 经 user_data 原样取回，供按 seq 回填配对 */
    g_last_flip_ns = mono_ns();
    g_last_flip_seq = (uint64_t)(uintptr_t)ud;
    g_flip_done = 1;
}

static struct {
    int       configured;
    drm_cfg_t cfg;
    int       fd;
    uint32_t  conn_id, crtc_id;
    drmModeModeInfo mode;
    uint32_t  fb[DRM_CORE_MAX_BUF];
    int       nbuf;
    int       cur;
    int       canvas_fd[DRM_CORE_MAX_BUF];
    void*     canvas_va[DRM_CORE_MAX_BUF];
    int       flip_pending;
    uint64_t  last_submit_ns;
    uint64_t  flip_skipped;
    uint64_t  flip_count;
} g;

int drm_is_configured(void) { return g.configured; }

static int open_connector(uint32_t want_conn) {
    drmModeRes* res = drmModeGetResources(g.fd);
    if (!res) return DRM_CORE_E_DRM;
    drmModeConnector* conn = NULL;
    if (want_conn) conn = drmModeGetConnector(g.fd, want_conn);
    if (!conn) {                                  /* 按名称 DSI-1 兜底 */
        for (int i = 0; i < res->count_connectors && !conn; i++) {
            drmModeConnector* c = drmModeGetConnector(g.fd, res->connectors[i]);
            if (!c) continue;
            if (c->connector_type == DRM_MODE_CONNECTOR_DSI &&
                c->connection == DRM_MODE_CONNECTED) conn = c;
            else drmModeFreeConnector(c);
        }
    }
    if (!conn) { drmModeFreeResources(res); return DRM_CORE_E_DRM; }
    /* HDMI 缺席是可降级项：connector 未连接不阻塞 master/其余（规则 30 §2） */
    g.conn_id = conn->connector_id;

    /* 选 mode：优先画布尺寸，否则 preferred，再否则第一个 */
    int mi = -1;
    for (int i = 0; i < conn->count_modes; i++)
        if (conn->modes[i].hdisplay == (int)g.cfg.canvas_w &&
            conn->modes[i].vdisplay == (int)g.cfg.canvas_h) { mi = i; break; }
    if (mi < 0)
        for (int i = 0; i < conn->count_modes; i++)
            if (conn->modes[i].type & DRM_MODE_TYPE_PREFERRED) { mi = i; break; }
    if (mi < 0) mi = 0;
    g.mode = conn->modes[mi];

    drmModeEncoder* enc = conn->encoder_id ? drmModeGetEncoder(g.fd, conn->encoder_id) : NULL;
    if (enc) { g.crtc_id = enc->crtc_id; drmModeFreeEncoder(enc); }
    if (!g.crtc_id && res->count_crtcs > 0) g.crtc_id = res->crtcs[0];

    drmModeFreeConnector(conn);
    drmModeFreeResources(res);
    if (!g.crtc_id) return DRM_CORE_E_DRM;
    return DRM_CORE_OK;
}

static int create_fb_rgb888(int dmabuf_fd, uint32_t* out_fb) {
    uint32_t handle = 0;
    if (drmPrimeFDToHandle(g.fd, dmabuf_fd, &handle) < 0) return DRM_CORE_E_DRM;
    uint32_t handles[4] = { handle, 0, 0, 0 };
    uint32_t pitches[4] = { g.cfg.canvas_w * 3, 0, 0, 0 };
    uint32_t offsets[4] = { 0, 0, 0, 0 };
    if (drmModeAddFB2(g.fd, g.cfg.canvas_w, g.cfg.canvas_h, DRM_FORMAT_RGB888,
                      handles, pitches, offsets, out_fb, 0) < 0)
        return DRM_CORE_E_DRM;
    return DRM_CORE_OK;
}

int drm_configure(const drm_cfg_t* cfg) {
    if (g.configured) return DRM_CORE_E_STATE;
    if (!cfg || cfg->canvas_w == 0 || cfg->canvas_h == 0) return DRM_CORE_E_STATE;
    g.cfg = *cfg;
    g.nbuf = (cfg->nbuf > 0 && cfg->nbuf <= DRM_CORE_MAX_BUF) ? cfg->nbuf : 2;

    g.fd = open(cfg->card[0] ? cfg->card : "/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (g.fd < 0) { g_err = "open_card"; g_errno = errno; return DRM_CORE_E_DRM; }
    if (drmSetMaster(g.fd) != 0) { g_err = "drmSetMaster"; g_errno = errno; close(g.fd); g.fd = -1; return DRM_CORE_E_DRM; }
    if (open_connector(cfg->conn_id) < 0) { g_err = "open_connector"; return DRM_CORE_E_DRM; }

    const size_t canvas_bytes = (size_t)g.cfg.canvas_w * g.cfg.canvas_h * 3;
    for (int i = 0; i < g.nbuf; i++) {
        g.canvas_fd[i] = dma_heap_alloc("cma", canvas_bytes, &g.canvas_va[i]);
        if (g.canvas_fd[i] < 0) { g_err = "canvas_dma_heap"; return DRM_CORE_E_DRM; }
        if (create_fb_rgb888(g.canvas_fd[i], &g.fb[i]) < 0) { g_err = "AddFB2"; g_errno = errno; return DRM_CORE_E_DRM; }
        memset(g.canvas_va[i], 0, canvas_bytes);   /* 整幅清黑一次 */
    }
    /* 初次 modeset：**pageflip 前必须先 set_crtc**（否则 drmModePageFlip 无 CRTC 可翻，实测返回失败）。
       与 tools/pipeline_probe.c 的 drm_set_crtc 同口径。 */
    if (drmModeSetCrtc(g.fd, g.crtc_id, g.fb[0], 0, 0, &g.conn_id, 1, &g.mode) < 0) {
        g_err = "SetCrtc"; g_errno = errno;
        return DRM_CORE_E_DRM;
    }
    g.cur = 0;
    g_flip_done = 1;
    g.configured = 1;
    return DRM_CORE_OK;
}

void drm_shutdown(void) {
    if (g.fd >= 0) {
        if (g.configured)
            for (int i = 0; i < g.nbuf; i++)
                if (g.canvas_va[i]) munmap(g.canvas_va[i], (size_t)g.cfg.canvas_w * g.cfg.canvas_h * 3);
        /* 不 close(dmabuf_fd)；fb / card fd 由进程释放 */
        close(g.fd);
    }
    g.fd = -1;
    g.configured = 0;
}

int drm_compose_flip(int buf_idx, uint64_t seq) {
    if (!g.configured || buf_idx < 0 || buf_idx >= g.nbuf) return DRM_CORE_E_STATE;
    if (g.flip_pending) { g.flip_skipped++; return 1; }   /* 上一 flip 未完成：丢弃本帧，不排队 */

    g.last_submit_ns = mono_ns();
    g_flip_done = 0;
    /* user_data 透传本帧 seq，回调里取回 ⇒ §6.5 的按 seq 回填配对（禁止按时间就近配对） */
    if (drmModePageFlip(g.fd, g.crtc_id, g.fb[buf_idx], DRM_MODE_PAGE_FLIP_EVENT,
                        (void*)(uintptr_t)seq) < 0) {
        g_flip_done = 1;
        return DRM_CORE_E_DRM;
    }
    g.cur = buf_idx;
    g.flip_pending = 1;
    g.flip_count++;

    /* 就地泵事件直到本次 flip 回调到达（提交 → vblank 的时差由 flip_handler 记下） */
    drmEventContext ev;
    memset(&ev, 0, sizeof(ev));
    ev.version = DRM_EVENT_CONTEXT_VERSION;
    ev.page_flip_handler = flip_handler;
    int guard = 0;
    /* 退出条件必须是 `g_flip_done`（flip_handler 置位），**不是** `g.flip_pending`：
       后者直到本函数末尾才清零，若用它作条件，第一个 pageflip 事件处理完后循环仍成立，
       会再次进入 drmHandleEvent → read() 无限阻塞（实测挂死）。 */
    while (!g_flip_done && guard++ < 2000) {
        int r = drmHandleEvent(g.fd, &ev);
        if (r < 0 && errno != EINTR) break;
    }
    g.flip_pending = 0;
    return (g_flip_done) ? DRM_CORE_OK : DRM_CORE_E_DRM;
}

int drm_wait_flip(void) {
    if (!g.configured) return DRM_CORE_E_STATE;
    drmEventContext ev;
    memset(&ev, 0, sizeof(ev));
    ev.version = DRM_EVENT_CONTEXT_VERSION;
    ev.page_flip_handler = flip_handler;
    int guard = 0;
    while (!g_flip_done && guard++ < 2000) {
        int r = drmHandleEvent(g.fd, &ev);
        if (r < 0 && errno != EINTR) break;
    }
    return (g_flip_done) ? DRM_CORE_OK : -6;
}

uint32_t drm_connector_id(void) { return g.conn_id; }
uint32_t drm_crtc_id(void) { return g.crtc_id; }
uint32_t drm_mode_hdisplay(void) { return g.mode.hdisplay; }
uint32_t drm_mode_vdisplay(void) { return g.mode.vdisplay; }
uint32_t drm_mode_vrefresh(void) { return g.mode.vrefresh; }
const char* drm_mode_name(void) { return g.mode.name; }
void* drm_canvas_va(int idx) { return (idx >= 0 && idx < g.nbuf) ? g.canvas_va[idx] : NULL; }
int   drm_canvas_fd(int idx) { return (idx >= 0 && idx < g.nbuf) ? g.canvas_fd[idx] : -1; }
uint32_t drm_canvas_stride(void) { return g.cfg.canvas_w * 3; }
uint32_t drm_canvas_count(void) { return (uint32_t)g.nbuf; }
uint64_t drm_last_submit_ns(void) { return g.last_submit_ns; }
uint64_t drm_last_flip_ns(void) { return g_last_flip_ns; }
uint64_t drm_last_flip_seq(void) { return g_last_flip_seq; }
uint64_t drm_flip_skipped(void) { return g.flip_skipped; }
uint64_t drm_flip_count(void) { return g.flip_count; }
const char* drm_last_error(void) { return g_err; }
int drm_last_errno(void) { return g_errno; }