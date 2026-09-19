/* rknn_core.h —— ZDE++ 曲线估计（TH2a 的 NPU 段）：2 条带空间切分 + EMA
 *
 * 来源：tools/pipeline_probe.c 的 slice_build_input / slice_worker / npu_init / npu_estimate
 *      与 tools/rknn_split_probe.c 的 HALO 拼接，**算法与实现参数未改**。
 *
 * 契约：
 *   · 分核（O29 裁定 = 选项 (a)）：常规态估计用 **core0+core1**（2 条带空间切分）；HQ 期间退化单核；
 *   · 会合协议用 **mutex + cond + 代次**（**不用 `pthread_barrier`**——探针首版即因它退出死锁）；
 *   · 首条带左边缘、末条带右边缘与帧边界齐平，外侧交给模型自身 padding（避免注入"边框零"：
 *     INT8 非对称量化下 uint8 0 ≠ 实值 0）；
 *   · 输出只取有效区（丢弃 pad 行），EMA `d = α·d + (1−α)·src`（§9.1）。
 */

#ifndef STARLIGHT_RKNN_CORE_H
#define STARLIGHT_RKNN_CORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define RKNN_CORE_OK 0
#define RKNN_CORE_E_STATE (-5)
#define RKNN_CORE_E_NPU (-2)

#define RKNN_CORE_MAX_SLICES 3

typedef struct {
    uint32_t est_w, est_h;    /* 模型 shape 宽/高（含 pad 行），如 480×272 */
    uint32_t eff_h;           /* 有效区高（输出只取此区），如 270 */
    uint32_t pad_top;         /* 有效区在 pad 缓冲中的起始行 */
    uint32_t n_slices;        /* 条带数（本设计 = 2） */
    uint32_t slice_h;         /* 条带模型输入高（= est_h） */
    uint32_t slice_w_in;      /* 条带模型输入宽（如 256） */
    uint32_t slice_w_out;     /* 每条带贡献的输出列数（= est_w / n_slices） */
    int32_t  core_mask[RKNN_CORE_MAX_SLICES]; /* 每条带绑定的 NPU 核（rknn_core_mask 取值） */
} rknn_cfg_t;

/* 载入切片模型并起 worker 线程。`core_mask[i]` 为 0 表示不调用 rknn_set_core_mask（AUTO）。 */
int  rknn_estimate_init(const char* model_path, const rknn_cfg_t* cfg);
void rknn_estimate_deinit(void);
int  rknn_estimate_ready(void);

/* 发布域精度（§3.1 契约第 3 条：全档 FP16；U8 为可选优化；FP32 仅供自检） */
typedef enum {
    RKNN_FIELD_FP32 = 0,
    RKNN_FIELD_FP16 = 1,
    RKNN_FIELD_U8 = 2
} rknn_field_t;

/* 跑一轮估计：`est_rgb` = pad 缓冲整幅（est_w×est_h RGB888）。
   **中间计算域统一 float32**（EMA 在 float32 上做，见 docs/design/realtime_apply.md §6），
   再按 `dst_prec` 转换写入 `dst`（3 × eff_h × est_w，**通道平面**，`dst` 大小按精度而定：
   FP32 = 4·3·eff_h·est_w / FP16 = 2·… / U8 = 1·…）。`use_ema=0` 或首轮时直接拷贝。 */
int  rknn_estimate(const uint8_t* est_rgb, void* dst, int dst_prec,
                   float alpha, int use_ema, int first);

/* 每条带最近一次 rknn_set_core_mask 的返回值（供报告；-1 = 未调用或失败） */
int32_t rknn_slice_mask_ret(uint32_t idx);
uint64_t rknn_last_estimate_ns(void);
uint32_t rknn_slice_count(void);

#ifdef __cplusplus
}
#endif

#endif /* STARLIGHT_RKNN_CORE_H */
