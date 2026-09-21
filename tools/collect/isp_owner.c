/*
 * tools/collect/isp_owner.c —— 采集期 ISP 3A 持有者（「关 AE + 固定曝光/增益」）
 *
 * 地位：采集子系统（`tools/collect/`）的板上辅助程序，**不属交付链路**。
 * 依据：主基准 §9.5「固定 ISP 参数与曝光」（固定曝光 1/30 s + 手动增益 + 关 AE + AWB 锁定）。
 *
 * 为什么需要它
 * ------------
 * 曝光/增益**不在** V4L2 节点上：`/dev/video11` 只有 `pixel_rate`；sensor subdev
 * （`/dev/v4l-subdev2`）虽有 `exposure` / `analogue_gain`，但**没有 `auto_exposure` 控制项**
 * ⇒ AE 完全由 rkaiq 驱动，只能经 rkaiq 的 API 关闭。
 *
 * 而 rkaiq 的 uAPI2 是**进程内** API（需要 `rk_aiq_sys_ctx_t*`），跨进程只有未公开的
 * socket 报文通道 ⇒ 本程序**自行持有 ISP 上下文**，用公开 uAPI2 固定曝光/增益。
 * 代价：采集期间 `rkaiq_3A.service` 必须**停止**（同一 ISP 只能有一个持有者）。
 *
 * 调用序**照抄**厂商 `/usr/bin/rkaiq_3A_server` 的 `engine_thread()`（板上反汇编还原）：
 *     getBindedSnsEntNmByVd(video_node)      → sensor media entity 名
 *     rk_aiq_uapi2_sysctl_init(sns, iq_dir, NULL, NULL)
 *     rk_aiq_uapi2_sysctl_prepare(ctx, w, h, RK_AIQ_WORKING_MODE_NORMAL)
 *     rk_aiq_uapi2_sysctl_start(ctx)
 * 厂商在 `start` 之前先 `open(isp-subdev)` 并订阅 STREAM_START/STOP 事件，仅在**取流期间**
 * 保持 3A 运行。本程序为单次采集会话，**不订阅事件**、`start` 后即固定参数并常驻
 * （厂商同样是在流未启动时先调 `sysctl_start`，故该顺序安全）。
 *
 * 用法
 * ----
 *   isp_owner <video-node> --probe                    # 只读：打印当前 AE 模式与曝光/增益范围
 *   isp_owner <video-node> --exp-us 33333 --gain 4.0  # 关 AE 并固定曝光/增益
 *   isp_owner <video-node> --exp-us 33333 --gain 4.0 --awb-lock --awb-lock-delay 5
 *
 * 参数
 *   --probe             只读探测，不改任何状态
 *   --exp-us N          固定曝光时间，单位**微秒**（默认 33333 = 1/30 s，见 §9.5）
 *   --gain G            固定总增益（线性倍数；**无默认值，必须显式给出**——§10.4
 *                       `camera.gain_*` 尚未定稿，不得由本程序臆造取值）
 *   --awb-lock          启动后延时锁定 AWB（`rk_aiq_user_api2_awb_Lock`）
 *   --awb-lock-delay S  锁 AWB 前的等待秒数（默认 5；**从 READY 起算**——AWB 仅在取流处理帧时更新，
 *                       故**必须在取流期间等待**，否则锁住的是 IQ 初值而非收敛值）
 *   --wb-watch          每 250 ms 打印 AWB 运行态（实际生效增益 / CCT / `awbConverged`），
 *                       用于判定"lock 是否真的冻住了 AWB"（板上无公开的 lock 状态 getter）
 *
 * 恢复步骤（退出时自动执行）
 *   收到 SIGTERM/SIGINT → `rk_aiq_uapi2_sysctl_stop(ctx, false)` → `deinit(ctx)`
 *   随后按需 `systemctl start rkaiq_3A.service` 恢复演示链路的 3A。
 *
 * 构建（板上，必须用系统 python3 同源的 aarch64 工具链；见 docs/build.md）
 *   gcc -O2 -o isp_owner isp_owner.c -lrkaiq
 *   （头文件路径由板上的 /usr/include/rkaiq 提供，无需额外 -I）
 *
 * 注意：本文件**不读写 config.yaml**，不发出任何日志 event，不涉及 §5 返回码白名单。
 */

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "rk_aiq_user_api2_awb.h"
#include "rk_aiq_user_api2_imgproc.h"
#include "rk_aiq_user_api2_sysctl.h"

/* 与厂商 rkaiq_3A_server 一致：IQ 搜索目录 */
#define IQ_FILE_DIR "/etc/iqfiles/"

/* 采集按 1080p（项目决定，见 tools/collect/README.md §5「crop 约束」） */
#define SENSOR_W 1920
#define SENSOR_H 1080

static volatile sig_atomic_t g_stop = 0;

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

static void usage(const char* prog)
{
    fprintf(stderr,
            "用法: %s <video-node> [--probe] [--exp-us N] [--gain G]\n"
            "                      [--awb-lock] [--awb-lock-delay S]\n"
            "\n"
            "  <video-node>        ISP 输出节点，如 /dev/video11\n"
            "  --probe             只读：打印当前 AE 模式与曝光/增益范围，不改状态\n"
            "  --exp-us N          固定曝光时间（微秒），默认 33333 = 1/30 s\n"
            "  --gain G            固定总增益（线性倍数），apply 模式下必填\n"
            "  --manual            调试口：切 AE manual + lock（**实测会失控，勿用于采集**）\n"
            "  --wb-watch          每 250 ms 打印 AWB 运行态（生效增益 / CCT / converged）\n"
            "  --awb-lock          启动后延时锁定 AWB（**须配合取流**，见 --awb-lock-delay）\n"
            "  --awb-lock-delay S  锁 AWB 前的等待秒数（默认 5，**从 READY 起算**；AWB 仅在取流时更新）\n",
            prog);
}

/* 打印一档状态（改前 / 改后 共用） */
static void dump_state(const char* tag, const rk_aiq_sys_ctx_t* ctx)
{
    opMode_t mode = OP_INVAL;
    paRange_t t = {0.0f, 0.0f};
    paRange_t g = {0.0f, 0.0f};

    int r_mode = (int)rk_aiq_uapi2_getExpMode(ctx, &mode);
    int r_time = (int)rk_aiq_uapi2_getExpTimeRange(ctx, &t);
    int r_gain = (int)rk_aiq_uapi2_getExpGainRange(ctx, &g);

    printf("[%s] getExpMode      rc=%d mode=%d (0=auto 1=manual)\n", tag, r_mode, (int)mode);
    printf("[%s] getExpTimeRange rc=%d max=%.6f min=%.6f  (秒)\n", tag, r_time, t.max, t.min);
    printf("[%s] getExpGainRange rc=%d max=%.4f min=%.4f  (线性倍数)\n", tag, r_gain, g.max, g.min);
    fflush(stdout);
}

/* 打印 AWB 运行态（--wb-watch 用）。
 * 观测口径：`QueryWBInfo` 给出**实际生效**的 WB 增益 + CCT + `awbConverged`；
 * 板上无公开的 lock 状态回读 API（`rk_aiq_wb_lock_state_t` 枚举存在但无导出 getter）
 * ⇒ 「lock 是否生效」只能靠**增益/CCT 是否停止变化**来判定。 */
static void dump_wb(const char* tag, const rk_aiq_sys_ctx_t* ctx)
{
    opMode_t wb_mode = OP_INVAL;
    rk_aiq_wb_querry_info_t info;
    memset(&info, 0, sizeof(info));

    int r_mode = (int)rk_aiq_uapi2_getWBMode(ctx, &wb_mode);
    int r_info = (int)rk_aiq_user_api2_awb_QueryWBInfo(ctx, &info);

    rk_aiq_wb_mwb_attrib_t mwb;
    memset(&mwb, 0, sizeof(mwb));
    int r_mwb = (int)rk_aiq_user_api2_awb_GetMwbAttrib(ctx, &mwb);

    printf("[%s] wbMode=%d(rc=%d) | QueryWBInfo(rc=%d) "
           "gain r=%.4f gr=%.4f gb=%.4f b=%.4f | CCT=%.1f CCRI=%.3f | converged=%d LV=%u\n",
           tag, (int)wb_mode, r_mode, r_info,
           info.gain.rgain, info.gain.grgain, info.gain.gbgain, info.gain.bgain,
           info.cctGloabl.CCT, info.cctGloabl.CCRI,
           (int)info.awbConverged, info.LVValue);
    /* 旁证：统计增益（与"实际生效增益"应当不同）+ manual 属性（若与 gain 相同 ⇒ 可疑为 IQ 预设而非实时值） */
    printf("        stat_glb r=%.4f gr=%.4f gb=%.4f b=%.4f | mwb(rc=%d) mode=%d r=%.4f gr=%.4f gb=%.4f b=%.4f\n",
           info.stat_gain_glb.rgain, info.stat_gain_glb.grgain,
           info.stat_gain_glb.gbgain, info.stat_gain_glb.bgain,
           r_mwb, (int)mwb.mode,
           mwb.para.gain.rgain, mwb.para.gain.grgain,
           mwb.para.gain.gbgain, mwb.para.gain.bgain);
    fflush(stdout);
}

int main(int argc, char** argv)
{
    const char* node = NULL;
    int probe = 0;
    int awb_lock = 0;
    int wb_watch = 0;
    int use_manual = 0;
    int awb_locked = 0;
    int awb_lock_delay = 5;
    double exp_us = 33333.0;   /* 1/30 s，见主基准 §9.5 */
    double gain = -1.0;        /* <0 表示未给出 */
    int have_exp_us = 0;

    if (argc < 2) {
        usage(argv[0]);
        return 2;
    }
    node = argv[1];

    for (int i = 2; i < argc; i++) {
        if (!strcmp(argv[i], "--probe")) {
            probe = 1;
        } else if (!strcmp(argv[i], "--exp-us") && i + 1 < argc) {
            exp_us = atof(argv[++i]);
            have_exp_us = 1;
        } else if (!strcmp(argv[i], "--gain") && i + 1 < argc) {
            gain = atof(argv[++i]);
        } else if (!strcmp(argv[i], "--manual")) {
            use_manual = 1;
        } else if (!strcmp(argv[i], "--wb-watch")) {
            wb_watch = 1;
        } else if (!strcmp(argv[i], "--awb-lock")) {
            awb_lock = 1;
        } else if (!strcmp(argv[i], "--awb-lock-delay") && i + 1 < argc) {
            awb_lock_delay = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            usage(argv[0]);
            return 0;
        } else {
            fprintf(stderr, "未知参数: %s\n", argv[i]);
            usage(argv[0]);
            return 2;
        }
    }
    (void)have_exp_us;

    if (!probe && gain < 0.0) {
        fprintf(stderr,
                "错误: apply 模式必须显式给出 --gain（§10.4 `camera.gain_*` 未定稿，"
                "不得由本程序臆造取值）。\n"
                "      先用 `%s %s --probe` 取当前范围作为选值依据。\n",
                argv[0], node);
        return 2;
    }
    if (exp_us <= 0.0) {
        fprintf(stderr, "错误: --exp-us 必须为正\n");
        return 2;
    }

    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);

    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    /* ---- 1) sensor media entity 名（厂商同样用「video 节点 → 实体名」这条路） ---- */
    const char* sns = rk_aiq_uapi2_sysctl_getBindedSnsEntNmByVd(node);
    if (!sns || !sns[0]) {
        fprintf(stderr, "失败: getBindedSnsEntNmByVd(%s) 未返回 sensor 实体名\n", node);
        return 1;
    }
    printf("sensor entity : %s\n", sns);

    /* ---- 2) init → prepare → start（顺序照抄 engine_thread） ---- */
    rk_aiq_sys_ctx_t* ctx = rk_aiq_uapi2_sysctl_init(sns, IQ_FILE_DIR, NULL, NULL);
    if (!ctx) {
        fprintf(stderr, "失败: rk_aiq_uapi2_sysctl_init 返回 NULL（IQ 目录 %s）\n", IQ_FILE_DIR);
        return 1;
    }
    printf("ctx           : %p (iq_dir=%s)\n", (void*)ctx, IQ_FILE_DIR);

    XCamReturn rc = rk_aiq_uapi2_sysctl_prepare(ctx, SENSOR_W, SENSOR_H,
                                                RK_AIQ_WORKING_MODE_NORMAL);
    if (rc != XCAM_RETURN_NO_ERROR) {
        fprintf(stderr, "失败: sysctl_prepare(%dx%d) rc=%d\n", SENSOR_W, SENSOR_H, (int)rc);
        rk_aiq_uapi2_sysctl_deinit(ctx);
        return 1;
    }
    printf("prepare       : ok (%dx%d, WORKING_MODE_NORMAL)\n", SENSOR_W, SENSOR_H);

    rc = rk_aiq_uapi2_sysctl_start(ctx);
    if (rc != XCAM_RETURN_NO_ERROR) {
        fprintf(stderr, "失败: sysctl_start rc=%d\n", (int)rc);
        rk_aiq_uapi2_sysctl_deinit(ctx);
        return 1;
    }
    printf("start         : ok\n");
    fflush(stdout);

    /* ---- 3) 改前基线 ---- */
    dump_state("before", ctx);

    if (probe) {
        printf("probe 模式：不修改任何状态，已确认可 init/prepare/start，直接退出。\n");
        fflush(stdout);
        goto cleanup;
    } else {
        /* ---- 4) 固定曝光/增益 ----
         * 三个 API 的实现都是 getExpSwAttr → 改 → setExpSwAttr（板上反汇编核对）。
         * 头文件注释的语义（rk_aiq_user_api2_imgproc.h §AEC）：
         *     auto 模式  : gain 在 [min, max] 之间自动调整
         *     manual 模式: gain->min == gain->max   ← 「钉死范围」本身即 manual 语义
         * 板上实测（v3.76）：`setExpMode(OP_MANUAL)` 会让 AEC 改用 **IQ 的 stManual 默认值**
         * （实测 exposure=405 / analogue_gain=0，与请求值无关），故**默认不切模式**，
         * 只把 auto 范围钉成一点；`--manual` 时才额外切模式 + AE lock。
         */
        paRange_t t = {(float)(exp_us / 1e6), (float)(exp_us / 1e6)}; /* {max, min} */
        rc = rk_aiq_uapi2_setExpTimeRange(ctx, &t);
        printf("setExpTimeRange(%.6fs) rc=%d\n", t.max, (int)rc);

        paRange_t g = {(float)gain, (float)gain};
        rc = rk_aiq_uapi2_setExpGainRange(ctx, &g);
        printf("setExpGainRange(%.4f)  rc=%d\n", g.max, (int)rc);

        if (use_manual) {
            /* 板上实测（v3.76）：切 manual 后 AEC 改用 IQ 的 stManual 默认值
             * （exposure=405 / analogue_gain=0），与 --exp-us / --gain 无关 ⇒ 失控。
             * 保留为显式调试口，**不得用于采集**。 */
            rc = rk_aiq_uapi2_setExpMode(ctx, OP_MANUAL);
            printf("setExpMode(MANUAL)      rc=%d  警告: 实测会改用 IQ stManual 默认值，勿用于采集\n", (int)rc);
            rc = rk_aiq_uapi2_setAeLock(ctx, true);
            printf("setAeLock(true)         rc=%d\n", (int)rc);
        }
        fflush(stdout);

        dump_state("after", ctx);
    }

    printf("READY: ISP 由本进程持有，可开始取流采集（Ctrl-C / SIGTERM 退出）\n");
    if (awb_lock)
        printf("       将在 %d s 后锁定 AWB —— **须在此期间取流**（AWB 只在有帧时更新，不取流则锁住的是未收敛值）\n",
               awb_lock_delay);
    fflush(stdout);

    /* ---- 5) 常驻；退出时按恢复步骤收尾 ----
     * AWB 锁定放在**取流之后**：AWB 仅在处理帧时更新，若在取流前锁，冻住的是 IQ 初值而非收敛值。
     * `--wb-watch` 每 250 ms 打印一次 WB 运行态（实际生效增益 / CCT / awbConverged），
     * 用于判定"lock 是否真的冻住了 AWB"。 */
    int tick = 0;
    while (!g_stop) {
        usleep(250000);
        tick++;

        if (wb_watch) {
            char tag[32];
            snprintf(tag, sizeof(tag), "t+%.2fs", tick * 0.25);
            dump_wb(tag, ctx);
        }
        if (awb_lock && !awb_locked && tick * 0.25 >= awb_lock_delay) {
            XCamReturn lrc = rk_aiq_user_api2_awb_Lock(ctx);
            printf("awb_Lock                rc=%d (t+%.2fs)\n", (int)lrc, tick * 0.25);
            if (wb_watch)
                dump_wb("locked", ctx);
            fflush(stdout);
            awb_locked = 1;
        }
    }

cleanup:
    printf("收尾中 ...\n");
    if (awb_locked)
        rk_aiq_user_api2_awb_Unlock(ctx);
    printf("  sysctl_stop ...\n");
    fflush(stdout);
    rk_aiq_uapi2_sysctl_stop(ctx, false);
    printf("  sysctl_stop done; deinit ...\n");
    fflush(stdout);
    rk_aiq_uapi2_sysctl_deinit(ctx);
    printf("已停止并 deinit；如需恢复演示链路 3A: systemctl start rkaiq_3A.service\n");
    return 0;
}
