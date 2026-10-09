/* wb_core.h —— MIPI 面板白平衡校正的逐像素内核（**非 §5 契约入口**）
 *
 * 地位：交付链路原生内核的一个**辅助计算单元**，只由 `_starlight_native` 的 `wb`
 *       子模块调用；Python 侧编排在 `display_mipi.py`（**只作用于 DSI 槽的画布**）。
 * 效力：只服务**演示级显示校正**（面板白点压蓝），**不构成验收依据**（规则 `10-evidence.md` §1）；
 *       **不碰 ISP / IQ / AWB**，**不改内容侧的链路语义**。
 * 口径：LUT 由**线性域**增益构建（sRGB EOTF 解码 → 乘增益 → 重新编码），产出**整数表 768 字节**
 *       （平面布局：0..255 = R、256..511 = G、512..767 = B）；**应用为纯整数查表**
 *       ⇒ 帧路径**无浮点**（不参与 CMakeLists 的 `-ffp-contract=off` 列表）。
 *       三通道增益全为 1.0 时**写入恒等表**（逐位不变）⇒ 调用方可直接跳过应用。
 * **不新增日志事件 / 返回码 / config 键**。
 */
#ifndef WB_CORE_H
#define WB_CORE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 由线性域增益构建 768 字节 LUT。`gain_rgb` 为 NULL 或某通道恰为 1.0 ⇒ 该通道**恒等**（逐位不变）。 */
void wb_build_lut(const double *gain_rgb, uint8_t lut[768]);

/* 就地把 RGB888 缓冲（`npix` 个像素）按 LUT 变换。纯整数、可自动向量化。 */
void wb_apply_lut(uint8_t *rgb, size_t npix, const uint8_t lut[768]);

#ifdef __cplusplus
}
#endif

#endif /* WB_CORE_H */
