/* drm_core.h —— 显示内核（TH4 MIPI / TH5 HDMI）：DRM **单实例 · 单 master fd · 多 CRTC**
 *               + 画布双缓冲 + pageflip
 *
 * 来源：tools/pipeline_probe.c 的 drm_open_connector / drm_create_fb_rgb888 / drm_init /
 *      drm_set_crtc / drm_flip / drm_wait_flip，**实现参数未改**。
 *
 * 契约：
 *   · **单实例多 CRTC（v3.84 第②批）**：DRM master 是 **per-fd** 的（`drmSetMaster`），
 *     故 DSI 与 HDMI 两个 CRTC **必须共用同一个 fd** ⇒ 本内核**只开一次卡**，内部按
 *     **CRTC 槽（slot）** 管理多路输出。**槽 0 = DSI（TH4）**、**槽 1 = HDMI（TH5）**；
 *     connector / CRTC **一律运行时解析，禁止写死 crtc id**（§2.3、§11.1-9）；
 *   · **既有函数签名与语义逐字不变**（规则 `40-gate.md` §2）——不带 `_slot` 后缀的函数
 *     **一律等价于槽 0（DSI）**，故 TH4 路径零回归；
 *   · 画布 = RGB888（`DRM_FORMAT_RGB888`），选它而非 XRGB8888 的理由见 pipeline_probe 注
 *     （RGA 侧 RGB888→RGB888 是同格式 blit，M0-13 已验证）；
 *   · **显示时间戳取 pageflip 完成的 vblank（drmEvent）**，不取提交时刻（§6.5）；
 *   · 过期帧丢弃（`compose_flip` 在上一 flip 未完成时不排队）——**按槽独立计数**。
 */

#ifndef STARLIGHT_DRM_CORE_H
#define STARLIGHT_DRM_CORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DRM_CORE_OK 0
#define DRM_CORE_E_STATE (-5)
#define DRM_CORE_E_DRM (-3)

#define DRM_CORE_MAX_BUF 3

/* CRTC 槽数：0 = DSI（TH4）、1 = HDMI（TH5）。槽数固定，不做动态分配。 */
#define DRM_CORE_MAX_SLOT 2
#define DRM_CORE_SLOT_DSI  0
#define DRM_CORE_SLOT_HDMI 1

typedef struct {
    char     card[64];        /* 默认 /dev/dri/card0（两槽须同一张卡） */
    uint32_t conn_id;         /* 0 = 运行时按 connector 类型解析（槽 0 → DSI、槽 1 → HDMI-A） */
    uint32_t canvas_w, canvas_h;
    uint32_t nbuf;            /* 画布缓冲数（双缓冲 = 2） */
} drm_cfg_t;

/* 打开卡、`drmSetMaster`、运行时解析 connector/CRTC、建画布 FB（**槽 0 = DSI**，签名与语义不变）。
   注意：取 master 需桌面已释放（`systemctl isolate multi-user.target`），属**有副作用操作**。 */
int  drm_configure(const drm_cfg_t* cfg);
/* **（v3.84 新增）** 按槽配置：`slot` = `DRM_CORE_SLOT_DSI` / `DRM_CORE_SLOT_HDMI`。
   首个槽配置时**开卡并取 master 一次**，后续槽**复用同一 fd**（master 是 per-fd，不可各开一份）。 */
int  drm_configure_slot(int slot, const drm_cfg_t* cfg);
void drm_shutdown(void);
int  drm_is_configured(void);                       /* 槽 0 是否就绪（语义不变） */
int  drm_is_configured_slot(int slot);              /* **（v3.84 新增）** */
int  drm_slot_count(void);                          /* **（v3.84 新增）** 已就绪的槽数（供 `crtc_count`） */

/* 合成完成后提交：若上一次 flip 未完成则**丢弃本帧**（返回 1，计入 flip_skipped），不排队。
   `seq` = 本帧 TH1 分配的 seq（§5-B FrameDesc），经 `drmModePageFlip` 的 user_data 透传，
   在 pageflip 回调里原样取回 ⇒ **§6.5 的「按 seq 回填配对」**（禁止按时间就近配对）。
   **（v3.84）`user_data` 打包为 `(seq << 2) | slot` ⇒ 约束 `seq < 2^62`**——`seq` 由 TH1 单调分配
   且实际远小于此（每帧 +1，2^62 帧在 30 fps 下需 ~48 亿年），**本约束不会在实机上触发**；
   若日后 `seq` 语义变更（如改为高位编码），须同步复核本打包方式。 */
int  drm_compose_flip(int buf_idx, uint64_t seq);               /* 槽 0（语义不变） */
int  drm_compose_flip_slot(int slot, int buf_idx, uint64_t seq);/* **（v3.84 新增）** */

/* **（v3.86 裁定 O122）外部 modeset 抢 CRTC 后的有界自愈**：内核 `fbcon`（VT 切换时）、桌面或
   热插拔都会对 CRTC 重做 `SetCrtc`，此后本内核的 `drmModePageFlip` **持续失败**（旧实现在
   `main.py` 里被静默吞掉 ⇒ 画面永久停留，见 §15 O122）。本函数以**当前画布 fb** 重发
   `drmModeSetCrtc`（fb 不变 ⇒ 不闪黑）把 CRTC 重新绑回本槽 plane，并把本槽的在途 flip 状态复位。
   返回 0 = 已重设；`DRM_CORE_E_DRM` = 仍失败（阶段串见 `drm_last_error()`，本函数失败时为
   `RecoverSetCrtc`）。**调用方负责有界重试与丢弃本帧**（机制值 `DRM_DISP_RECOVER_N` 见
   `display_mipi.py`）；不得用它取代启动序的 `drm_configure_slot`。 */
int  drm_recover_slot(int slot);                                /* **（v3.86 新增）** */
int  drm_recover(void);                                         /* 槽 0（= DSI / TH4） */

/* 阻塞等待在途 flip 完成；返回 0 = 已无在途，-6 = 超时（guard 用尽）。 */
int  drm_wait_flip(void);                           /* 槽 0（语义不变） */
int  drm_wait_flip_slot(int slot);                  /* **（v3.84 新增）** */

/* 只读查询（**不带 `_slot` 的一律 = 槽 0**，语义不变） */
uint32_t drm_connector_id(void);
uint32_t drm_crtc_id(void);
uint32_t drm_mode_hdisplay(void);
uint32_t drm_mode_vdisplay(void);
uint32_t drm_mode_vrefresh(void);
const char* drm_mode_name(void);
void*    drm_canvas_va(int idx);
int      drm_canvas_fd(int idx);
uint32_t drm_canvas_stride(void);
uint32_t drm_canvas_count(void);

/* **（v3.84 新增）按槽版本** */
uint32_t drm_connector_id_slot(int slot);
uint32_t drm_crtc_id_slot(int slot);
uint32_t drm_mode_hdisplay_slot(int slot);
uint32_t drm_mode_vdisplay_slot(int slot);
uint32_t drm_mode_vrefresh_slot(int slot);
const char* drm_mode_name_slot(int slot);
void*    drm_canvas_va_slot(int slot, int idx);
int      drm_canvas_fd_slot(int slot, int idx);
uint32_t drm_canvas_stride_slot(int slot);
uint32_t drm_canvas_count_slot(int slot);

/* **（v3.84 新增）** 槽对应 connector 的连接状态（`drmModeGetConnector` 只读查询）：
   1 = connected、0 = disconnected、负值 = 查询失败（返回 `DRM_CORE_E_DRM`）。
   **用途**：与 sysfs 轮询互为佐证（`display_hdmi.py` 的热插拔探测走 sysfs，不依赖本函数）。 */
int drm_connector_status(int slot);

/* 打点（§6.5 延迟两段分解，全部 CLOCK_MONOTONIC 绝对时刻，单位 ns）：
     · `drm_last_submit_ns` —— 最近一次 pageflip 的**提交时刻**
     · `drm_last_flip_ns`   —— 最近一次 pageflip **完成对应的 vblank 事件时刻**（§6.5 的显示时间戳）
     · 段1「采集→提交」= submit − t_capture_ns（t_capture_ns 由 TH1 的 FrameDesc 给出）
     · 段2「提交→vblank」= flip − submit
   注意：`drm_last_flip_ns` 是**绝对时刻**，不是间隔。**不带 `_slot` 的一律 = 槽 0**（语义不变）。 */
uint64_t drm_last_submit_ns(void);
uint64_t drm_last_flip_ns(void);
/* 最近一次 pageflip 回调取回的 seq（应与提交时传入的 seq 一致；不一致即配对失败，§6.5） */
uint64_t drm_last_flip_seq(void);
uint64_t drm_flip_skipped(void);
uint64_t drm_flip_count(void);

/* **（v3.84 新增）按槽版本** */
uint64_t drm_last_submit_ns_slot(int slot);
uint64_t drm_last_flip_ns_slot(int slot);
uint64_t drm_last_flip_seq_slot(int slot);
uint64_t drm_flip_skipped_slot(int slot);
uint64_t drm_flip_count_slot(int slot);

/* 最近一次**失败**（configure / 翻页 / 自愈任一路径）的阶段串与 errno（定位用）。
   **（v3.86 裁定 O122）** 翻页失败也在此留痕（`PageFlip` / `PageFlipTimeout` / `RecoverSetCrtc`）
   ——`DRM_CORE_E_DRM` 与 RGA 的 `-3` 同值，**阶段只能靠本串判别**。 */
const char* drm_last_error(void);
int drm_last_errno(void);

#ifdef __cplusplus
}
#endif

#endif /* STARLIGHT_DRM_CORE_H */
