/* rga_core.h —— RGA 几何链路与画布合成（TH2a 的几何段 + TH4 的画布段）
 *
 * 来源：tools/pipeline_probe.c 的 rga_init / rga_blit_nv12 / canvas_compose
 *      与 tools/rga_probe.c 的 dma_heap_alloc，**算法与实现参数未改**：
 *   · 缓冲 = `dma_heap(system-dma32)` + **偏移基址子视图**（`drect` 的 y 须偶对齐，故有效区
 *     落在 pad 缓冲第 `pad_top` 行只能用基址偏移表达）；
 *   · pad 零填充 = 缓冲池初始化时由 `improcess` 从零源块拷入（`imfill` 对 RGB888 失败）；
 *   · 每帧只做一次 scale+CSC blit（§3.1 契约 G1；v3.27 裁定 O32）。
 */

#ifndef STARLIGHT_RGA_CORE_H
#define STARLIGHT_RGA_CORE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define RGA_OK 0
#define RGA_E_STATE (-5)
#define RGA_E_RGA (-3)   /* 本模块把 RGA 失败归到 §5 契约 A 的 E_DRM 语义之外；
                            实际对外只暴露 OK/E_STATE，blit 失败经返回值上报 */

/* 目标缓冲选择 */
typedef enum {
    RGA_DST_EST = 0,   /* 曲线估计输入（pad 缓冲的有效区子视图） */
    RGA_DST_WIN = 1    /* 原图窗（win_w×win_h） */
} rga_dst_t;

typedef struct {
    uint32_t cap_w, cap_h;       /* 采集域（1920×1080） */
    uint32_t eff_w, eff_h;       /* 曲线场有效区（§10.4 curve_geometry.effective） */
    uint32_t pad_w, pad_h;       /* 模型 shape（§10.4 curve_geometry.pad_to） */
    uint32_t pad_top;            /* 有效区在 pad 缓冲中的起始行（0 或 1..2） */
    uint32_t win_w, win_h;       /* 应用域/显示窗（512×288） */
    uint32_t canvas_w, canvas_h; /* MIPI 画布（1024×600） */
    uint32_t core_mask;          /* §10.4 rga.core_mask；0 = 不调用 imconfig */
} rga_cfg_t;

/* 通用 dma-buf 分配（system-dma32）。供**按精度定尺寸**的发布缓冲使用（如曲线场）。 */
int dma_heap_alloc_system(size_t nbytes, void** va);

int  rga_configure(const rga_cfg_t* cfg);
void rga_shutdown(void);
int  rga_is_configured(void);

/* 采集 NV12 → RGB888 缩放+CSC。`src_fd` 为 V4L2 导出的 dmabuf fd；失败时由调用方退化 staging。 */
int rga_blit_nv12_fd(int src_fd, rga_dst_t dst);
/* 同上，源为内存虚拟地址（staging 兜底路径）。 */
int rga_blit_nv12_va(const void* src_va, rga_dst_t dst);

/* 画布合成：win → 左窗（0,0），enh → 右窗（win_w,0）。
   **画布缓冲由 drm_core 持有**（它才是 DRM FB 的所有者），本函数只往调用方给的 VA 上合成。 */
int rga_canvas_compose_to(void* canvas_va, uint32_t canvas_w, uint32_t canvas_h);

/* 缓冲访问（虚拟地址；Python 侧以只读缓冲读取或转交扩展） */
void* rga_est_va(void);        /* pad 缓冲整幅 */
void* rga_est_eff_va(void);    /* 有效区子视图基址（= est + pad_top 行） */
void* rga_win_va(void);
void* rga_enh_va(void);
void* rga_stage_va(void);
int   rga_est_fd(void);
int   rga_win_fd(void);
int   rga_enh_fd(void);
/* 最近一次 blit 的耗时（ns，CLOCK_MONOTONIC 差值；供函数级打点） */
uint64_t rga_last_blit_ns(void);
uint64_t rga_last_canvas_ns(void);

#ifdef __cplusplus
}
#endif

#endif /* STARLIGHT_RGA_CORE_H */
