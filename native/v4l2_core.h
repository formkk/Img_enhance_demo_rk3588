/* v4l2_core.h —— TH1 采集内核（V4L2 mainpath + DMABUF 池 + lease 签发）
 *
 * 契约来源：主基准 §5 契约 B（FrameDesc 字段）、C 节（capture 模块签名）、
 *          §5 启动/退出序、O26 软复位口径（不 STREAMOFF、stream_on 计数恒为 1）。
 *
 * 硬契约：
 *   · **仅本模块（TH1）持 `QBUF` 权**；消费者只持 lease；
 *   · **任何模块不得 `close(dmabuf_fd)`**（本模块退出时也不 close，随进程释放）；
 *   · 引用计数归零后才允许 `QBUF`；**禁止强制回收仍在使用的 lease**；
 *   · `seq` 由本模块分配且单调递增，下游不得重编号。
 *
 * 算法来源：tools/pipeline_probe.c 的 cap_init / cap_qbuf / cap_stream_on / cap_dqbuf / cap_deinit。
 */

#ifndef STARLIGHT_V4L2_CORE_H
#define STARLIGHT_V4L2_CORE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define V4L2_MAX_BUFS 8
#define V4L2_MAX_CONSUMERS 4

/* §5 契约 A 的返回码（本模块只用这几项） */
#define V4L2_OK 0
#define V4L2_E_V4L2 (-1)
#define V4L2_E_IO (-4)
#define V4L2_E_STATE (-5)
#define V4L2_E_TIMEOUT (-6)

typedef enum { TS_SRC_SOE = 0, TS_SRC_EOF = 1 } ts_source_t;
typedef enum { FMT_NV12 = 0 } frame_format_t;
typedef enum { ISP_PRESET_DARKROOM = 0, ISP_PRESET_NIGHT = 1, ISP_PRESET_FACE = 2 } isp_preset_t;

/* §5 契约 B 的 FrameDesc（字段与单位逐条对齐） */
typedef struct {
    uint64_t seq;             /* TH1 分配，单调递增 */
    uint64_t t_capture_ns;    /* CLOCK_MONOTONIC 纳秒 */
    int32_t  timestamp_source;/* ts_source_t */
    uint8_t  buffer_index;    /* 0…pool_depth-1 */
    uint32_t generation;      /* 每次 QBUF 自增，防旧引用误释放 */
    int32_t  dmabuf_fd;       /* 归 TH1，消费者只读 */
    uint32_t w, h;            /* px */
    int32_t  format;          /* frame_format_t */
    uint32_t stride;          /* byte */
    int32_t  isp_preset_id;   /* isp_preset_t */
    uint32_t lease_id;        /* TH1 签发、消费者归还 */
} frame_desc_t;

/* 一次性初始化（打开节点、S_FMT/G_FMT、REQBUFS、QUERYBUF、mmap、EXPBUF）。 */
int v4l2_init(const char* dev, int nbuf, uint32_t want_w, uint32_t want_h);

/* **全生命周期只调用一次**；第二次调用返回 E_STATE 且不重复 STREAMON。 */
int v4l2_stream_on_once(void);

/* 取一帧并为 `n_consumers` 个消费者签发同一个 lease。超时返回 E_TIMEOUT。 */
int v4l2_acquire(int n_consumers, frame_desc_t* out, int timeout_ms);

/* 消费者归还 lease；引用计数归零后才 QBUF 回池。非法 lease 返回 E_STATE。 */
int v4l2_release(uint32_t lease_id);

/* 仅供进程终止路径（软复位**不得**调用）。 */
int v4l2_stream_off(void);

/* 退出序：STREAMOFF（若仍在流）+ munmap + close(fd)。**不 close dmabuf_fd**。 */
void v4l2_deinit(void);

/* 只读查询（供 Python 侧与报告用） */
int      v4l2_buffer_count(void);
int      v4l2_dmabuf_fd(int index);
void*    v4l2_buffer_va(int index);
size_t   v4l2_buffer_len(int index);
uint32_t v4l2_width(void);
uint32_t v4l2_height(void);
uint32_t v4l2_stride(void);
uint32_t v4l2_sizeimage(void);
int      v4l2_stream_on_count(void);   /* 契约：恒为 0 或 1 */

/* 计数：lease 与丢弃（进 bench.py 报告；**不得静默丢弃**） */
void v4l2_counters(uint64_t* frames, uint64_t* dropped_stale, uint64_t* lease_timeouts,
                   uint32_t* outstanding_leases);
/* **（v3.86 裁定 O24-前 2 类）** 硬错误计数 = `DQBUF` 返回**非 `EAGAIN`** 错误的次数
   （判定量之一：自上次成功取帧起连续 ≥15 次即判掉流 ⇒ 见 §10.5 矩阵与 `main.py`）。 */
uint64_t v4l2_error_count(void);
/* **[验证用钩子]** 注入 `n` 次"下一次 `acquire` 直接失败"（**仅 §15 O24 的确定性注入**；
   **不改变正常路径语义**、**不是 config 键**、不新增返回码——返回的仍是既有 `E_V4L2` / `E_TIMEOUT`）。
   `mode` = 0：「硬错误」（`v4l2_errors++`，返回 `E_V4L2`）；1：「无帧超时」（`lease_timeouts++`，
   返回 `E_TIMEOUT`）⇒ 两条判据分支（连续硬错误 ≥15 / 连续无帧 ≥1 s）可分别确定性验证。 */
void v4l2_inject_error(int n, int mode);
/* 时间戳源发生变化的次数（§6.5：仅记录，不停机、该窗口指标作废） */
uint64_t v4l2_ts_source_changes(void);
int32_t  v4l2_current_ts_source(void);

#ifdef __cplusplus
}
#endif

#endif /* STARLIGHT_V4L2_CORE_H */
