/* apply_core.h —— 曲线应用 C/NEON 内核（交付链路；主基准 §5 契约 C 节的 realtime_apply）
 *
 * 算法来源：tools/pipeline_probe.c 的 build_taps / apply_fused_neon / apply_fused_scalar_to
 *          与 tools/apply_probe.c 的标量参考**同源**（M0-13 已证 NEON 与标量逐位一致）。
 *          本文件只做「去全局化 + 显式参数化」，不改算法、不改采样口径、不改迭代表达式。
 *
 * 硬约束（§3.3、规则 30-board-safety §3）：
 *   · 8 次迭代必须**单遍融合**——一次载入源像素、寄存器内完成全部迭代、一次写出；
 *   · 曲线场在迭代中复用同一份，每个目标像素**只采样一次**（不生成独立上采样曲线图）；
 *   · 热路径**不分配**（采样表在 cf_configure 时一次性建好，之后只读）。
 *
 * 契约缺口（如实登记，待裁定；本实现取保守解）：
 *   GAP-1 §5 的 apply_window/apply_full **未带曲线场几何**（有效区宽高 / 精度 / 迭代数）⇒
 *         本实现以 `cf_configure()` 在启动时一次性注入。该结构在启动后**只读**（进程常量，
 *         非逐帧可变状态），故计算路径仍满足「无状态、可重入」；取值源 = §10.4 的
 *         `pipeline.curve_geometry.effective` / `pipeline.curve_field_precision`（§3.3 的迭代次数）。
 *   GAP-3 `apply_full` 吃 NV12 出 RGB，但 §5 未给色彩矩阵 / 量化范围 ⇒ 由 `cf_config_t` 的
 *         `csc` / `range` 承载（取值源 = §10.4 的 `isp.csc_matrix` / `isp.rgb_range`）。
 *
 * 已澄清（非缺口）：
 *   · **stride**：§3.1 第 2 条已定「内存 stride 必须显式对齐」，且 512 与 1024 均为 16 的倍数、
 *     1920×1080 的 `w*3 = 5760` 亦 16 对齐 ⇒ 本实现按紧排 `w*3` 前进即满足契约。
 *   · **曲线场表示**：§3.1 契约第 3 条（O23 裁定）**全档 FP16**，U8 为可选优化 ⇒
 *     FP16 按 IEEE half（2 字节）存取、U8 按 0..255 存取，两者都在内核内转 float32 中间域。
 *   · **曲线场布局**：按**通道平面**（planar 3ch），与 `tools/pipeline_probe.c` 的
 *     `cb[ch] = curve + ch*CH*CW` 一致。
 */

#ifndef STARLIGHT_APPLY_CORE_H
#define STARLIGHT_APPLY_CORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 曲线场精度（§10.4 `pipeline.curve_field_precision`；O23 裁定全档 FP16，U8 为可选优化） */
typedef enum {
    CF_PREC_FP16 = 0,
    CF_PREC_U8 = 1
} cf_precision_t;

/* 色彩矩阵（§10.4 `isp.csc_matrix`；UNSET = 该键尚未定稿，此时 apply_full 返回 E_STATE） */
typedef enum {
    CF_CSC_UNSET = -1,
    CF_CSC_BT601 = 0,
    CF_CSC_BT709 = 1
} cf_csc_t;

/* 量化范围（§10.4 `isp.rgb_range`；UNSET 同上） */
typedef enum {
    CF_RANGE_UNSET = -1,
    CF_RANGE_LIMITED = 0,
    CF_RANGE_FULL = 1
} cf_range_t;

/* 进程内一次性配置。**必须在任何工作线程启动前**调用；之后只读 ⇒ 计算路径可重入。 */
typedef struct {
    uint32_t curve_w;    /* 曲线场有效区宽（§10.4 curve_geometry.effective） */
    uint32_t curve_h;    /* 曲线场有效区高 */
    uint32_t iters;      /* 迭代次数（§3.3 契约值；单遍融合） */
    cf_precision_t precision;
    cf_csc_t csc;        /* 仅 apply_full 用 */
    cf_range_t range;    /* 仅 apply_full 用 */
} cf_config_t;

/* 返回码：0 = OK；负值取主基准 §5 契约 A（本文件只用 0 / -5 E_STATE）。 */
#define CF_OK 0
#define CF_E_STATE (-5)

/* 一次性配置；返回 CF_OK 或 CF_E_STATE（参数非法 / 已配置后重复配置）。 */
int cf_configure(const cf_config_t* cfg);

/* 是否已配置（供 Python 侧给出可读错误）。 */
int cf_is_configured(void);

/* 释放配置（进程退出用；不影响任何已开始的调用）。 */
void cf_shutdown(void);

/* ── 主基准 §5 契约 C 节的两个入口（签名逐字）─────────────────────────
 * 输入 RGB888 紧排（stride = w*3）、曲线场为 CF_PREC_* 对应的表示、
 * 输出 RGB888 紧排。含曲线场双线性采样；8 次迭代在单遍内完成。
 */
int apply_window(const uint8_t* rgb, const void* curve, uint8_t* dst, uint32_t w, uint32_t h);
int apply_full(const uint8_t* nv12, const void* curve, uint8_t* dst_rgb, uint32_t w, uint32_t h);

/* ── 自检用（非契约入口）─────────────────────────────────────────────
 * 与 NEON 路径**同形**的标量参考（同样的采样口径、同样的迭代表达式），
 * 供 `--impl scalar` 逐位对账（§11.2 的数值一致性检查）。
 */
int cf_apply_scalar(const uint8_t* rgb, const void* curve, uint8_t* dst, uint32_t w, uint32_t h);

/* 内核标识串（写进报告，便于追溯实现来源）。 */
const char* cf_impl_id(void);

#ifdef __cplusplus
}
#endif

#endif /* STARLIGHT_APPLY_CORE_H */
