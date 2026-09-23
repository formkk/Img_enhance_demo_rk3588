/* v4l2_core.c —— 见 v4l2_core.h。V4L2 部分自 tools/pipeline_probe.c 的 cap_* 逐段移植。 */

#define _GNU_SOURCE
#include "v4l2_core.h"

#include <errno.h>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

typedef struct {
    void*    start;
    size_t   length;
    int      dmabuf_fd;
    uint32_t generation;   /* 每次 QBUF 自增 */
    int      queued;       /* 已 QBUF 回驱动 */
    int      filled;       /* 已 DQBUF、待消费 */
    uint64_t filled_seq;   /* 入 filled 时的 seq（用于挑最旧） */
    uint32_t lease_refs;   /* 未归还的 lease 数 */
    uint32_t lease_id;     /* 当前 lease 标识（refs>0 时有效） */
} buf_slot_t;

static struct {
    int        fd;
    int        nbuf;
    buf_slot_t slot[V4L2_MAX_BUFS];
    uint32_t   w, h, stride, sizeimage;
    int        streaming;
    int        stream_on_count;
    uint64_t   seq_next;
    uint64_t   frames, dropped_stale, lease_timeouts;
    uint64_t   v4l2_errors;      /* **（v3.86 裁定 O24-前 2 类）** DQBUF 非 EAGAIN 错误次数 */
    uint32_t   lease_next;
    uint32_t   outstanding;
    int32_t    ts_source;
    int32_t    ts_source_valid;
    uint64_t   ts_changes;
} g;

/* **[验证用钩子]** 注入剩余次数（§15 O24 的确定性注入；默认 0 = 关闭，正常路径不受影响）
   `g_inject_err_mode`：0 = 硬错误（`v4l2_errors++`）、1 = 无帧超时（`lease_timeouts++`）。 */
static int g_inject_err_left = 0;
static int g_inject_err_mode = 0;

static int xioctl(int fd, unsigned long req, void* arg) {
    int r;
    do { r = ioctl(fd, req, arg); } while (r < 0 && errno == EINTR);
    return r;
}

static int qbuf(int idx) {
    struct v4l2_plane planes[VIDEO_MAX_PLANES];
    struct v4l2_buffer b;
    memset(&b, 0, sizeof(b));
    memset(planes, 0, sizeof(planes));
    b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    b.memory = V4L2_MEMORY_MMAP;
    b.index = (uint32_t)idx;
    b.length = 1;                       /* MPLANE：plane 个数，不是字节数 */
    b.m.planes = planes;
    if (xioctl(g.fd, VIDIOC_QBUF, &b) < 0) return V4L2_E_V4L2;
    g.slot[idx].queued = 1;
    g.slot[idx].filled = 0;
    g.slot[idx].generation++;
    return V4L2_OK;
}

int v4l2_init(const char* dev, int nbuf, uint32_t want_w, uint32_t want_h) {
    memset(&g, 0, sizeof(g));
    g.fd = -1;
    for (int i = 0; i < V4L2_MAX_BUFS; i++) g.slot[i].dmabuf_fd = -1;

    g.fd = open(dev, O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (g.fd < 0) return V4L2_E_V4L2;

    struct v4l2_capability cap;
    memset(&cap, 0, sizeof(cap));
    if (xioctl(g.fd, VIDIOC_QUERYCAP, &cap) < 0) return V4L2_E_V4L2;
    const uint32_t need = V4L2_CAP_VIDEO_CAPTURE_MPLANE | V4L2_CAP_STREAMING;
    if ((cap.capabilities & need) != need) return V4L2_E_V4L2;

    struct v4l2_format f;
    memset(&f, 0, sizeof(f));
    f.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    f.fmt.pix_mp.width = want_w;
    f.fmt.pix_mp.height = want_h;
    f.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_NV12;
    f.fmt.pix_mp.field = V4L2_FIELD_NONE;
    f.fmt.pix_mp.num_planes = 1;
    f.fmt.pix_mp.plane_fmt[0].sizeimage = want_w * want_h * 3 / 2;
    if (xioctl(g.fd, VIDIOC_S_FMT, &f) < 0) return V4L2_E_V4L2;

    /* 回读：rkisp 会静默改 sizeimage / bytesperline */
    memset(&f, 0, sizeof(f));
    f.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    if (xioctl(g.fd, VIDIOC_G_FMT, &f) < 0) return V4L2_E_V4L2;
    g.w = f.fmt.pix_mp.width;
    g.h = f.fmt.pix_mp.height;
    g.stride = f.fmt.pix_mp.plane_fmt[0].bytesperline;
    g.sizeimage = f.fmt.pix_mp.plane_fmt[0].sizeimage;

    struct v4l2_requestbuffers rb;
    memset(&rb, 0, sizeof(rb));
    rb.count = (uint32_t)(nbuf > 0 ? nbuf : 4);
    rb.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    rb.memory = V4L2_MEMORY_MMAP;
    if (xioctl(g.fd, VIDIOC_REQBUFS, &rb) < 0) return V4L2_E_V4L2;
    if (rb.count < 2) return V4L2_E_V4L2;
    g.nbuf = (int)rb.count > V4L2_MAX_BUFS ? V4L2_MAX_BUFS : (int)rb.count;

    for (int i = 0; i < g.nbuf; i++) {
        struct v4l2_plane planes[VIDEO_MAX_PLANES];
        struct v4l2_buffer b;
        memset(&b, 0, sizeof(b));
        memset(planes, 0, sizeof(planes));
        b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        b.memory = V4L2_MEMORY_MMAP;
        b.index = (uint32_t)i;
        b.length = 1;
        b.m.planes = planes;
        if (xioctl(g.fd, VIDIOC_QUERYBUF, &b) < 0) return V4L2_E_V4L2;
        g.slot[i].length = planes[0].length;
        g.slot[i].start = mmap(NULL, planes[0].length, PROT_READ | PROT_WRITE,
                               MAP_SHARED, g.fd, planes[0].m.mem_offset);
        if (g.slot[i].start == MAP_FAILED) { g.slot[i].start = NULL; return V4L2_E_V4L2; }

        struct v4l2_exportbuffer eb;
        memset(&eb, 0, sizeof(eb));
        eb.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        eb.index = (uint32_t)i;
        eb.plane = 0;                   /* 必须显式写 0 */
        eb.flags = O_CLOEXEC;
        g.slot[i].dmabuf_fd = (xioctl(g.fd, VIDIOC_EXPBUF, &eb) < 0) ? -1 : eb.fd;
    }
    return V4L2_OK;
}

int v4l2_stream_on_once(void) {
    if (g.fd < 0) return V4L2_E_STATE;
    if (g.stream_on_count != 0) return V4L2_E_STATE;   /* 契约：全生命周期仅一次 */
    for (int i = 0; i < g.nbuf; i++)
        if (qbuf(i) != V4L2_OK) return V4L2_E_V4L2;
    enum v4l2_buf_type t = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    if (xioctl(g.fd, VIDIOC_STREAMON, &t) < 0) return V4L2_E_V4L2;
    g.streaming = 1;
    g.stream_on_count++;
    return V4L2_OK;
}

int v4l2_stream_off(void) {
    if (g.fd < 0) return V4L2_E_STATE;
    if (!g.streaming) return V4L2_OK;
    enum v4l2_buf_type t = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    if (xioctl(g.fd, VIDIOC_STREAMOFF, &t) < 0) return V4L2_E_V4L2;
    g.streaming = 0;
    for (int i = 0; i < g.nbuf; i++) { g.slot[i].queued = 0; g.slot[i].filled = 0; }
    return V4L2_OK;
}

void v4l2_deinit(void) {
    if (g.fd >= 0) v4l2_stream_off();
    for (int i = 0; i < g.nbuf; i++) {
        if (g.slot[i].start) munmap(g.slot[i].start, g.slot[i].length);
        /* 契约：**不 close(dmabuf_fd)**（§5 硬契约）——随进程释放 */
        g.slot[i].start = NULL;
    }
    if (g.fd >= 0) close(g.fd);
    g.fd = -1;
    g.streaming = 0;
}

int v4l2_acquire(int n_consumers, frame_desc_t* out, int timeout_ms) {
    if (g.fd < 0 || !g.streaming) return V4L2_E_STATE;
    if (n_consumers <= 0 || n_consumers > V4L2_MAX_CONSUMERS) return V4L2_E_STATE;

    /* **[验证用钩子]** 注入的"失败"（§15 O24 的确定性注入）：计数与正常失败路径**同一个计数器**
       ⇒ 下游判据（连续硬错误 ≥15 / 连续无帧 ≥1 s）与真实故障走完全相同的处置分支。 */
    if (g_inject_err_left > 0) {
        g_inject_err_left--;
        if (g_inject_err_mode == 1) { g.lease_timeouts++; return V4L2_E_TIMEOUT; }
        g.v4l2_errors++;
        return V4L2_E_V4L2;
    }

    struct pollfd pfd = { g.fd, POLLIN, 0 };
    if (poll(&pfd, 1, timeout_ms) <= 0) { g.lease_timeouts++; return V4L2_E_TIMEOUT; }

    struct v4l2_plane planes[VIDEO_MAX_PLANES];
    struct v4l2_buffer b;
    memset(&b, 0, sizeof(b));
    memset(planes, 0, sizeof(planes));
    b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    b.memory = V4L2_MEMORY_MMAP;
    b.length = 1;
    b.m.planes = planes;
    if (xioctl(g.fd, VIDIOC_DQBUF, &b) < 0) {
        if (errno == EAGAIN) { g.lease_timeouts++; return V4L2_E_TIMEOUT; }
        g.v4l2_errors++;   /* **（v3.86 裁定 O24-前 2 类）** 硬错误（掉流/接收错误）计数 */
        return V4L2_E_V4L2;
    }
    const int idx = (int)b.index;
    const uint64_t seq = ++g.seq_next;
    g.slot[idx].queued = 0;
    g.slot[idx].filled = 1;
    g.slot[idx].filled_seq = seq;
    g.frames++;

    /* 时间戳源（§6.5）：只记录变化，不停机；变化即该窗口指标作废（由 Python 侧打事件） */
    const int32_t src = (b.flags & V4L2_BUF_FLAG_TSTAMP_SRC_MASK) == V4L2_BUF_FLAG_TSTAMP_SRC_EOF
                            ? TS_SRC_EOF : TS_SRC_SOE;
    if (!g.ts_source_valid) { g.ts_source_valid = 1; g.ts_source = src; }
    else if (src != g.ts_source) { g.ts_source = src; g.ts_changes++; }

    /* 丢弃更旧的「未签发 lease」帧：只保留最新一帧（不静默——计入 dropped_stale） */
    for (int i = 0; i < g.nbuf; i++) {
        if (i == idx) continue;
        if (g.slot[i].filled && g.slot[i].lease_refs == 0) {
            g.dropped_stale++;
            qbuf(i);
        }
    }

    g.slot[idx].lease_refs = (uint32_t)n_consumers;
    g.slot[idx].lease_id = ++g.lease_next;
    g.outstanding += (uint32_t)n_consumers;

    out->seq = seq;
    out->t_capture_ns = (uint64_t)b.timestamp.tv_sec * 1000000000ull
                        + (uint64_t)b.timestamp.tv_usec * 1000ull;
    out->timestamp_source = src;
    out->buffer_index = (uint8_t)idx;
    out->generation = g.slot[idx].generation;
    out->dmabuf_fd = g.slot[idx].dmabuf_fd;
    out->w = g.w;
    out->h = g.h;
    out->format = FMT_NV12;
    out->stride = g.stride;
    out->isp_preset_id = ISP_PRESET_DARKROOM;
    out->lease_id = g.slot[idx].lease_id;
    return V4L2_OK;
}

int v4l2_release(uint32_t lease_id) {
    for (int i = 0; i < g.nbuf; i++) {
        if (g.slot[i].lease_refs > 0 && g.slot[i].lease_id == lease_id) {
            g.slot[i].lease_refs--;
            g.outstanding--;
            if (g.slot[i].lease_refs == 0) {
                g.slot[i].filled = 0;
                return qbuf(i);   /* 引用计数归零后才允许 QBUF */
            }
            return V4L2_OK;
        }
    }
    return V4L2_E_STATE;   /* 非法/已归还的 lease */
}

int      v4l2_buffer_count(void) { return g.nbuf; }
int      v4l2_dmabuf_fd(int index) { return (index >= 0 && index < g.nbuf) ? g.slot[index].dmabuf_fd : -1; }
void*    v4l2_buffer_va(int index) { return (index >= 0 && index < g.nbuf) ? g.slot[index].start : NULL; }
size_t   v4l2_buffer_len(int index) { return (index >= 0 && index < g.nbuf) ? g.slot[index].length : 0; }
uint32_t v4l2_width(void) { return g.w; }
uint32_t v4l2_height(void) { return g.h; }
uint32_t v4l2_stride(void) { return g.stride; }
uint32_t v4l2_sizeimage(void) { return g.sizeimage; }
int      v4l2_stream_on_count(void) { return g.stream_on_count; }

void v4l2_counters(uint64_t* frames, uint64_t* dropped_stale, uint64_t* lease_timeouts,
                   uint32_t* outstanding_leases) {
    if (frames) *frames = g.frames;
    if (dropped_stale) *dropped_stale = g.dropped_stale;
    if (lease_timeouts) *lease_timeouts = g.lease_timeouts;
    if (outstanding_leases) *outstanding_leases = g.outstanding;
}

uint64_t v4l2_ts_source_changes(void) { return g.ts_changes; }
int32_t  v4l2_current_ts_source(void) { return g.ts_source; }

/* **（v3.86 裁定 O24-前 2 类）** 硬错误计数（DQBUF 非 EAGAIN 错误次数） */
uint64_t v4l2_error_count(void) { return g.v4l2_errors; }

/* **[验证用钩子]** 注入 `n` 次"下一次 acquire 失败"（`mode`：0 = 硬错误 / 1 = 无帧超时）；
   `n ≤ 0` 即清空注入 */
void v4l2_inject_error(int n, int mode) {
    g_inject_err_left = (n > 0) ? n : 0;
    g_inject_err_mode = (mode == 1) ? 1 : 0;
}
