/* mfa_core.c —— HQ 输入侧「N 帧时域平均」的逐像素内核（§15 O158；口径见 mfa_core.h）
 *
 * 说明：本文件**不含浮点**，故不参与 CMakeLists 的 `-ffp-contract=off` 列表；`-O3` 下
 *       循环可被自动向量化（每帧 157 万个 8bit→16bit 累加，量级 ~1 ms）。
 * 触发频率：**每次 HQ 触发一次**（不在帧路径上）⇒ 不构成 §6.1 的帧路径开销。
 */

#include "mfa_core.h"

void mfa_zero(uint16_t *acc, size_t n)
{
    for (size_t i = 0; i < n; ++i) {
        acc[i] = 0u;
    }
}

void mfa_accum(const uint8_t *src, uint16_t *acc, size_t n)
{
    for (size_t i = 0; i < n; ++i) {
        acc[i] = (uint16_t)(acc[i] + (uint16_t)src[i]);
    }
}

void mfa_mean(const uint16_t *acc, uint8_t *dst, size_t n, uint32_t nframes)
{
    const uint32_t half = nframes / 2u;
    for (size_t i = 0; i < n; ++i) {
        dst[i] = (uint8_t)(((uint32_t)acc[i] + half) / nframes);
    }
}
