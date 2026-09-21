/* drm_core.c —— 见 drm_core.h。自 tools/pipeline_probe.c 的 drm_* 逐段移植。
 *
 * **（v3.84 第②批）单实例 · 单 master fd · 多 CRTC**：DRM master 是 **per-fd** 的，
 * DSI 与 HDMI 两个 CRTC 必须共用同一个 fd ⇒ 本文件只开一次卡，内部按 **CRTC 槽** 管理。
 * **槽 0 = DSI（TH4，语义与改造前逐字一致）**、**槽 1 = HDMI（TH5）**。
 */

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

/* ── CRTC 槽 ─────────────────────────────────────────────────────────────
   改造前这些字段散在 file-static `struct g` 里；现按槽抽出。**槽 0 的字段语义未变**。 */
struct crtc_slot {
    int       configured;
    drm_cfg_t cfg;
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
};

static const char* g_err = "ok";
static int g_errno = 0;

/* **flip 状态分槽**：`flip_handler` 按 `user_data` 解出的 slot 写入对应槽，
   故两槽的 `drmHandleEvent` 单点调用不会互相清 pending（见 §5 TH5 风险缓解）。 */
static volatile int g_flip_done[DRM_CORE_MAX_SLOT];
static uint64_t     g_last_flip_ns[DRM_CORE_MAX_SLOT];
static uint64_t     g_last_flip_seq[DRM_CORE_MAX_SLOT];

static struct {
    int          fd;              /* **单 fd 共享**（master 是 per-fd）；-1 = 未开卡 */
    int          opened;          /* 卡是否已打开并取得 master */
    struct crtc_slot slot[DRM_CORE_MAX_SLOT];
} g = { .fd = -1, .opened = 0 };

static int slot_valid(int slot) { return slot >= 0 && slot < DRM_CORE_MAX_SLOT; }

/* `user_data` 打包 `(seq << 2) | slot`（⇒ 约束 `seq < 2^62`，见 drm_core.h 注释） */
#define DRM_CORE_SLOT_BITS 2
#define DRM_CORE_SLOT_MASK ((1u << DRM_CORE_SLOT_BITS) - 1u)

static void flip_handler(int fd, unsigned seq, unsigned tv_sec, unsigned tv_usec, void* ud) {
    (void)fd; (void)seq; (void)tv_sec; (void)tv_usec;
    const uint64_t packed = (uint64_t)(uintptr_t)ud;
    const int slot = (int)(packed & DRM_CORE_SLOT_MASK);
    if (!slot_valid(slot)) return;
    /* §6.5：显示时间戳取 vblank（drmEvent），不取提交时刻；seq 经 user_data 原样取回，供按 seq 回填配对 */
    g_last_flip_ns[slot]  = mono_ns();
    g_last_flip_seq[slot] = packed >> DRM_CORE_SLOT_BITS;
    g_flip_done[slot]     = 1;
}

int drm_is_configured(void) { return g.slot[DRM_CORE_SLOT_DSI].configured; }
int drm_is_configured_slot(int slot) { return slot_valid(slot) ? g.slot[slot].configured : 0; }

int drm_slot_count(void) {
    int n = 0;
    for (int i = 0; i < DRM_CORE_MAX_SLOT; i++) if (g.slot[i].configured) n++;
    return n;
}

/* 槽 → connector 类型（槽 0 = DSI、槽 1 = HDMI-A）；`DRM_MODE_CONNECTOR_*` 为无符号位域 */
static uint32_t slot_connector_type(int slot) {
    return (slot == DRM_CORE_SLOT_HDMI) ? DRM_MODE_CONNECTOR_HDMIA : DRM_MODE_CONNECTOR_DSI;
}

static int open_connector(int slot, uint32_t want_conn) {
    struct crtc_slot* s = &g.slot[slot];
    drmModeRes* res = drmModeGetResources(g.fd);
    if (!res) return DRM_CORE_E_DRM;
    drmModeConnector* conn = NULL;
    if (want_conn) conn = drmModeGetConnector(g.fd, want_conn);
    if (!conn) {                                  /* 按类型兜底（DSI-1 / HDMI-A-1） */
        const uint32_t want_type = slot_connector_type(slot);
        for (int i = 0; i < res->count_connectors && !conn; i++) {
            drmModeConnector* c = drmModeGetConnector(g.fd, res->connectors[i]);
            if (!c) continue;
            if (c->connector_type == want_type && c->connection == DRM_MODE_CONNECTED) conn = c;
            else drmModeFreeConnector(c);
        }
    }
    if (!conn) { drmModeFreeResources(res); return DRM_CORE_E_DRM; }
    /* HDMI 缺席是可降级项：connector 未连接不阻塞 master/其余（规则 30 §2） */
    s->conn_id = conn->connector_id;

    /* 选 mode：优先画布尺寸，否则 preferred，再否则第一个 */
    int mi = -1;
    for (int i = 0; i < conn->count_modes; i++)
        if (conn->modes[i].hdisplay == (int)s->cfg.canvas_w &&
            conn->modes[i].vdisplay == (int)s->cfg.canvas_h) { mi = i; break; }
    if (mi < 0)
        for (int i = 0; i < conn->count_modes; i++)
            if (conn->modes[i].type & DRM_MODE_TYPE_PREFERRED) { mi = i; break; }
    if (mi < 0) mi = 0;
    s->mode = conn->modes[mi];

    drmModeEncoder* enc = conn->encoder_id ? drmModeGetEncoder(g.fd, conn->encoder_id) : NULL;
    if (enc) { s->crtc_id = enc->crtc_id; drmModeFreeEncoder(enc); }
    if (!s->crtc_id && res->count_crtcs > 0) s->crtc_id = res->crtcs[0];

    drmModeFreeConnector(conn);
    drmModeFreeResources(res);
    if (!s->crtc_id) return DRM_CORE_E_DRM;
    return DRM_CORE_OK;
}

static int create_fb_rgb888(int slot, int dmabuf_fd, uint32_t* out_fb) {
    struct crtc_slot* s = &g.slot[slot];
    uint32_t handle = 0;
    if (drmPrimeFDToHandle(g.fd, dmabuf_fd, &handle) < 0) return DRM_CORE_E_DRM;
    uint32_t handles[4] = { handle, 0, 0, 0 };
    uint32_t pitches[4] = { s->cfg.canvas_w * 3, 0, 0, 0 };
    uint32_t offsets[4] = { 0, 0, 0, 0 };
    if (drmModeAddFB2(g.fd, s->cfg.canvas_w, s->cfg.canvas_h, DRM_FORMAT_RGB888,
                      handles, pitches, offsets, out_fb, 0) < 0)
        return DRM_CORE_E_DRM;
    return DRM_CORE_OK;
}

int drm_configure_slot(int slot, const drm_cfg_t* cfg) {
    if (!slot_valid(slot)) return DRM_CORE_E_STATE;
    struct crtc_slot* s = &g.slot[slot];
    if (s->configured) return DRM_CORE_E_STATE;
    if (!cfg || cfg->canvas_w == 0 || cfg->canvas_h == 0) return DRM_CORE_E_STATE;
    s->cfg = *cfg;
    s->nbuf = (cfg->nbuf > 0 && cfg->nbuf <= DRM_CORE_MAX_BUF) ? cfg->nbuf : 2;

    /* 卡只开一次、master 只取一次（per-fd 语义）；后续槽复用同一 fd */
    if (!g.opened) {
        g.fd = open(cfg->card[0] ? cfg->card : "/dev/dri/card0", O_RDWR | O_CLOEXEC);
        if (g.fd < 0) { g_err = "open_card"; g_errno = errno; return DRM_CORE_E_DRM; }
        if (drmSetMaster(g.fd) != 0) { g_err = "drmSetMaster"; g_errno = errno; close(g.fd); g.fd = -1; return DRM_CORE_E_DRM; }
        g.opened = 1;
    }
    if (open_connector(slot, cfg->conn_id) < 0) { g_err = "open_connector"; return DRM_CORE_E_DRM; }

    const size_t canvas_bytes = (size_t)s->cfg.canvas_w * s->cfg.canvas_h * 3;
    for (int i = 0; i < s->nbuf; i++) {
        s->canvas_fd[i] = dma_heap_alloc("cma", canvas_bytes, &s->canvas_va[i]);
        if (s->canvas_fd[i] < 0) { g_err = "canvas_dma_heap"; return DRM_CORE_E_DRM; }
        if (create_fb_rgb888(slot, s->canvas_fd[i], &s->fb[i]) < 0) { g_err = "AddFB2"; g_errno = errno; return DRM_CORE_E_DRM; }
        memset(s->canvas_va[i], 0, canvas_bytes);   /* 整幅清黑一次 */
    }
    /* 初次 modeset：**pageflip 前必须先 set_crtc**（否则 drmModePageFlip 无 CRTC 可翻，实测返回失败）。
       与 tools/pipeline_probe.c 的 drm_set_crtc 同口径。 */
    if (drmModeSetCrtc(g.fd, s->crtc_id, s->fb[0], 0, 0, &s->conn_id, 1, &s->mode) < 0) {
        g_err = "SetCrtc"; g_errno = errno;
        return DRM_CORE_E_DRM;
    }
    s->cur = 0;
    g_flip_done[slot] = 1;
    s->configured = 1;
    return DRM_CORE_OK;
}

int drm_configure(const drm_cfg_t* cfg) { return drm_configure_slot(DRM_CORE_SLOT_DSI, cfg); }

void drm_shutdown(void) {
    if (g.opened) {
        for (int sl = 0; sl < DRM_CORE_MAX_SLOT; sl++) {
            struct crtc_slot* s = &g.slot[sl];
            if (!s->configured) continue;
            const size_t canvas_bytes = (size_t)s->cfg.canvas_w * s->cfg.canvas_h * 3;
            for (int i = 0; i < s->nbuf; i++)
                if (s->canvas_va[i]) munmap(s->canvas_va[i], canvas_bytes);
            s->configured = 0;
        }
        /* 不 close(dmabuf_fd)；fb / card fd 由进程释放 */
        close(g.fd);
    }
    g.fd = -1;
    g.opened = 0;
}

int drm_compose_flip_slot(int slot, int buf_idx, uint64_t seq) {
    if (!slot_valid(slot)) return DRM_CORE_E_STATE;
    struct crtc_slot* s = &g.slot[slot];
    if (!s->configured || buf_idx < 0 || buf_idx >= s->nbuf) return DRM_CORE_E_STATE;
    if (s->flip_pending) { s->flip_skipped++; return 1; }   /* 上一 flip 未完成：丢弃本帧，不排队 */

    s->last_submit_ns = mono_ns();
    g_flip_done[slot] = 0;
    /* user_data 打包 (seq<<2)|slot：既透传本帧 seq（§6.5 按 seq 回填配对），又让回调知道写哪个槽 */
    const uint64_t packed = (seq << DRM_CORE_SLOT_BITS) | (uint64_t)(unsigned)slot;
    if (drmModePageFlip(g.fd, s->crtc_id, s->fb[buf_idx], DRM_MODE_PAGE_FLIP_EVENT,
                        (void*)(uintptr_t)packed) < 0) {
        g_flip_done[slot] = 1;
        return DRM_CORE_E_DRM;
    }
    s->cur = buf_idx;
    s->flip_pending = 1;
    s->flip_count++;

    /* 就地泵事件直到**本槽**的 flip 回调到达（提交 → vblank 的时差由 flip_handler 记下）。
       注意：drmHandleEvent 是**单点调用**，会一并派发其它槽的事件——`flip_handler` 按 slot 分派，
       故不会误置本槽的 `g_flip_done`。 */
    drmEventContext ev;
    memset(&ev, 0, sizeof(ev));
    ev.version = DRM_EVENT_CONTEXT_VERSION;
    ev.page_flip_handler = flip_handler;
    int guard = 0;
    /* 退出条件必须是本槽的 `g_flip_done[slot]`（flip_handler 置位），**不是** `flip_pending`：
       后者直到本函数末尾才清零，若用它作条件，第一个 pageflip 事件处理完后循环仍成立，
       会再次进入 drmHandleEvent → read() 无限阻塞（实测挂死）。 */
    while (!g_flip_done[slot] && guard++ < 2000) {
        int r = drmHandleEvent(g.fd, &ev);
        if (r < 0 && errno != EINTR) break;
    }
    s->flip_pending = 0;
    return (g_flip_done[slot]) ? DRM_CORE_OK : DRM_CORE_E_DRM;
}

int drm_compose_flip(int buf_idx, uint64_t seq) {
    return drm_compose_flip_slot(DRM_CORE_SLOT_DSI, buf_idx, seq);
}

int drm_wait_flip_slot(int slot) {
    if (!slot_valid(slot)) return DRM_CORE_E_STATE;
    if (!g.slot[slot].configured) return DRM_CORE_E_STATE;
    drmEventContext ev;
    memset(&ev, 0, sizeof(ev));
    ev.version = DRM_EVENT_CONTEXT_VERSION;
    ev.page_flip_handler = flip_handler;
    int guard = 0;
    while (!g_flip_done[slot] && guard++ < 2000) {
        int r = drmHandleEvent(g.fd, &ev);
        if (r < 0 && errno != EINTR) break;
    }
    return (g_flip_done[slot]) ? DRM_CORE_OK : -6;
}

int drm_wait_flip(void) { return drm_wait_flip_slot(DRM_CORE_SLOT_DSI); }

/* ── 只读查询（**不带 `_slot` 的一律 = 槽 0**，语义不变）────────────────── */
uint32_t drm_connector_id_slot(int slot) { return slot_valid(slot) ? g.slot[slot].conn_id : 0; }
uint32_t drm_crtc_id_slot(int slot) { return slot_valid(slot) ? g.slot[slot].crtc_id : 0; }
uint32_t drm_mode_hdisplay_slot(int slot) { return slot_valid(slot) ? g.slot[slot].mode.hdisplay : 0; }
uint32_t drm_mode_vdisplay_slot(int slot) { return slot_valid(slot) ? g.slot[slot].mode.vdisplay : 0; }
uint32_t drm_mode_vrefresh_slot(int slot) { return slot_valid(slot) ? g.slot[slot].mode.vrefresh : 0; }
const char* drm_mode_name_slot(int slot) { return slot_valid(slot) ? g.slot[slot].mode.name : ""; }
void* drm_canvas_va_slot(int slot, int idx) {
    if (!slot_valid(slot)) return NULL;
    struct crtc_slot* s = &g.slot[slot];
    return (idx >= 0 && idx < s->nbuf) ? s->canvas_va[idx] : NULL;
}
int drm_canvas_fd_slot(int slot, int idx) {
    if (!slot_valid(slot)) return -1;
    struct crtc_slot* s = &g.slot[slot];
    return (idx >= 0 && idx < s->nbuf) ? s->canvas_fd[idx] : -1;
}
uint32_t drm_canvas_stride_slot(int slot) { return slot_valid(slot) ? g.slot[slot].cfg.canvas_w * 3 : 0; }
uint32_t drm_canvas_count_slot(int slot) { return slot_valid(slot) ? (uint32_t)g.slot[slot].nbuf : 0; }

int drm_connector_status(int slot) {
    if (!slot_valid(slot) || !g.opened) return DRM_CORE_E_DRM;
    const uint32_t conn_id = g.slot[slot].conn_id;
    if (!conn_id) return DRM_CORE_E_DRM;
    drmModeConnector* c = drmModeGetConnector(g.fd, conn_id);
    if (!c) return DRM_CORE_E_DRM;
    const int connected = (c->connection == DRM_MODE_CONNECTED) ? 1 : 0;
    drmModeFreeConnector(c);
    return connected;
}

uint64_t drm_last_submit_ns_slot(int slot) { return slot_valid(slot) ? g.slot[slot].last_submit_ns : 0; }
uint64_t drm_last_flip_ns_slot(int slot) { return slot_valid(slot) ? g_last_flip_ns[slot] : 0; }
uint64_t drm_last_flip_seq_slot(int slot) { return slot_valid(slot) ? g_last_flip_seq[slot] : 0; }
uint64_t drm_flip_skipped_slot(int slot) { return slot_valid(slot) ? g.slot[slot].flip_skipped : 0; }
uint64_t drm_flip_count_slot(int slot) { return slot_valid(slot) ? g.slot[slot].flip_count : 0; }

uint32_t drm_connector_id(void) { return drm_connector_id_slot(DRM_CORE_SLOT_DSI); }
uint32_t drm_crtc_id(void) { return drm_crtc_id_slot(DRM_CORE_SLOT_DSI); }
uint32_t drm_mode_hdisplay(void) { return drm_mode_hdisplay_slot(DRM_CORE_SLOT_DSI); }
uint32_t drm_mode_vdisplay(void) { return drm_mode_vdisplay_slot(DRM_CORE_SLOT_DSI); }
uint32_t drm_mode_vrefresh(void) { return drm_mode_vrefresh_slot(DRM_CORE_SLOT_DSI); }
const char* drm_mode_name(void) { return drm_mode_name_slot(DRM_CORE_SLOT_DSI); }
void* drm_canvas_va(int idx) { return drm_canvas_va_slot(DRM_CORE_SLOT_DSI, idx); }
int   drm_canvas_fd(int idx) { return drm_canvas_fd_slot(DRM_CORE_SLOT_DSI, idx); }
uint32_t drm_canvas_stride(void) { return drm_canvas_stride_slot(DRM_CORE_SLOT_DSI); }
uint32_t drm_canvas_count(void) { return drm_canvas_count_slot(DRM_CORE_SLOT_DSI); }
uint64_t drm_last_submit_ns(void) { return drm_last_submit_ns_slot(DRM_CORE_SLOT_DSI); }
uint64_t drm_last_flip_ns(void) { return drm_last_flip_ns_slot(DRM_CORE_SLOT_DSI); }
uint64_t drm_last_flip_seq(void) { return drm_last_flip_seq_slot(DRM_CORE_SLOT_DSI); }
uint64_t drm_flip_skipped(void) { return drm_flip_skipped_slot(DRM_CORE_SLOT_DSI); }
uint64_t drm_flip_count(void) { return drm_flip_count_slot(DRM_CORE_SLOT_DSI); }
const char* drm_last_error(void) { return g_err; }
int drm_last_errno(void) { return g_errno; }
