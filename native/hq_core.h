/* hq_core.h —— HQ（Retinexformer）关键帧推理引擎（TH3 的 NPU 段）
 *
 * TH3 在触发后把触发的原始暗帧缩放到 `hq.active_shape`（960×544），送 HQ 模型推理，
 * 结果供 §3.5 的 HDMI 承接。本引擎做 **单次整帧** 推理——**不做** ZDE++ 那样的多核切分：
 * O36 裁定 HQ 分核 = `RKNN_NPU_CORE_2`（值 4），单核运行。
 *
 * 契约（张量属性实测自 `rknn_c_probe`，见 `docs/evidence/M2-2-hq-align.md` §3）：
 *   · **输入 = NHWC float32，值域 [0,255]，RGB 序**（`dims = [1, H, W, 3]`）。
 *     `mean = [0,0,0]` / `std = [255,255,255]` 已烘进 `.rknn`（`tools/convert_models.py`），
 *     故**板端喂 [0,255] 即可，不要预除 255**（预除会二次归一化）。
 *   · **输出 = NCHW float32，值域 [0,1]**（`dims = [1, 3, H, W]`，`want_float = 1`）——
 *     与对齐回归所用的 `out_hq_*.f32` 同口径。
 *   · **阻塞式**：`hq_run` 返回时推理已完成、结果已写入调用方缓冲。
 *   · 板上实测（960×544 / core2 / `rknn_c_probe` 口径）：npu run ≈7.9 s。
 *     §6.2 的触发→HDMI ≤2 s 预算**不适用**（O37 已按实测重排为 ≤12 / ≤7 / ≤4 s 分档）。
 *
 * RKNPU2 调用约定（与 `tools/rknn_c_probe.c` 同源，保证与对齐回归可复现）：
 *   · `rknn_init(ctx, model, size, 0, NULL)` —— flag = 0，**不开 perf 采集**（其自身有开销）；
 *   · `rknn_set_core_mask(ctx, RKNN_NPU_CORE_2)` —— 失败**不致命**（记录返回值供查）；
 *   · 输入 `rknn_input{index 0, pass_through 0, type FLOAT32, fmt NHWC}`；
 *   · 输出 `rknn_output{index 0, want_float 1}`。
 */

#ifndef STARLIGHT_HQ_CORE_H
#define STARLIGHT_HQ_CORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define HQ_CORE_OK 0
#define HQ_CORE_E_STATE (-5)
#define HQ_CORE_E_NPU (-2)

typedef struct {
    uint32_t w, h;       /* 模型输入宽/高（如 960×544） */
    int32_t  core_mask;  /* 分核（O36 = RKNN_NPU_CORE_2 = 4）；0 = 不调用 rknn_set_core_mask（AUTO） */
} hq_cfg_t;

/* 载入模型并绑定核。成功 return 0；重复 init 返 E_STATE。 */
int  hq_init(const char* model_path, const hq_cfg_t* cfg);
void hq_deinit(void);
int  hq_ready(void);

/* 推理一帧。`in_rgb` = H×W×3 的 **NHWC float32，值域 [0,255]**；
   `out` = 1×3×H×W 的 **NCHW float32，值域 [0,1]**（由调用方分配，大小 = 3·H·W·4 字节）。
   **阻塞式**。返回 HQ_CORE_OK / E_STATE / E_NPU。 */
int  hq_run(const float* in_rgb, float* out);

/* 最近一次推理的 wall 耗时（mono_ns，含 inputs_set + run + outputs_get） */
uint64_t hq_last_run_ns(void);
/* 最近一次 rknn_set_core_mask 的返回值（-1 = 未调用或失败；供报告） */
int32_t  hq_core_mask_ret(void);

/* **（v3.84 新增）** RGB888 → NHWC float32 打包（值域保持 [0,255]）。
   RGA 只能输出 RGB888（uint8），而模型输入是 NHWC float32 [0,255] ⇒ 需要本转换。
   `rgb` = w×h×3 的 uint8（RGB 序）；`out` = h×w×3 的 float32（NHWC）。
   **放在 C 层**：960×544×3 ≈ 1.57M 次转换，Python 侧逐元素做不可接受。 */
void hq_pack_rgb888_nhwc_f32(const uint8_t* rgb, float* out, uint32_t w, uint32_t h);

/* NCHW f32 [0,1] → RGB888（uint8，HWC 紧凑），只取行区间 [y0, y1)（用于裁掉 §3.1 的 pad 行）。
   值域映射 = clip(0,1)*255 截断取整（与 tools/hq_frame_review.py 的 f32_nchw_to_rgb888 逐位同口径，
   **无任何非线性后处理**）。返回 0 = OK，非 0 = 入参非法。 */
int hq_nchw_f32_to_rgb888(const float* nchw, uint8_t* rgb, uint32_t w, uint32_t h,
                          uint32_t y0, uint32_t y1);

#ifdef __cplusplus
}
#endif

#endif /* STARLIGHT_HQ_CORE_H */