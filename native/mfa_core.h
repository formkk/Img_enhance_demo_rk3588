/* mfa_core.h —— HQ 输入侧「N 帧时域平均」的逐像素内核（§15 O158；**非 §5 契约入口**）
 *
 * 地位：交付链路原生内核的一个**辅助计算单元**，只由 `_starlight_native` 的 `mfa`
 *       子模块调用；Python 侧编排在 `main.py` 的 `[A/B 开关] --hq-mfa-frames`。
 * 效力：O158 仍为「**待裁定**」⇒ 本内核只服务**试验性 A/B**，**不构成验收依据**
 *       （规则 `10-evidence.md` §1 / `00-ssot.md` §3.5）。
 * 口径（与离线 `hq_optimize` 的 `seqmv*` 探针同式）：
 *   · 输入 = RGA 缩放后的 HQ 目标 **RGB888**（`hq_w×hq_h×3`，含上下零 pad 行）；
 *   · **8bit → 16bit「无饱和」累加**（N ≤ 8 时元素上界 8×255 = 2040 < 65536）；
 *   · 取均 = **四舍五入**：`(sum + N/2) / N`。
 * 前置（调用方保证，本内核不校验）：`n` = 元素个数；`acc` ≥ n 个 uint16；`src` / `dst` ≥ n 字节；
 *       `nframes` ≥ 1 且等于实际累加进 `acc` 的帧数（否则取均有偏）。
 */
#ifndef STARLIGHT_MFA_CORE_H
#define STARLIGHT_MFA_CORE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 累加器清零（`n` = 元素个数，**不是**字节数）。 */
void mfa_zero(uint16_t *acc, size_t n);

/* 把一帧 u8 累加进 u16 累加器（`n` = 元素个数）。 */
void mfa_accum(const uint8_t *src, uint16_t *acc, size_t n);

/* 取均并写 u8（四舍五入）；`nframes` = 已累加的帧数。 */
void mfa_mean(const uint16_t *acc, uint8_t *dst, size_t n, uint32_t nframes);

#ifdef __cplusplus
}
#endif

#endif /* STARLIGHT_MFA_CORE_H */
