/* rga_csc_probe.c —— M0-7 实验用：同尺寸 NV12 → RGB888（一次 CSC，无缩放、无 pad）
 *
 * 用途：对照 RKNN `inputs_yuv_fmt` 内置的 yuv2rgb（BT601_l）与 RGA 的 NV12→RGB888 CSC，
 *       判定"NV12 直喂"能否无损替换主线"RGA → RGB"。本工具只做一次 CSC 并落盘 RGB888。
 * 用法：./rga_csc_probe <in_nv12.raw> <w> <h> <out.rgb888.raw>
 * 输入：uint8 NV12，尺寸 (h*3/2, w)；输出：uint8 RGB888，尺寸 (h, w*3)。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <fcntl.h>
#include <sys/mman.h>
#include "im2d.h"

int main(int argc, char** argv) {
    if (argc < 5) { fprintf(stderr, "usage: %s <in_nv12.raw> <w> <h> <out.rgb.raw>\n", argv[0]); return 2; }
    const char* in_path = argv[1];
    int W = atoi(argv[2]), H = atoi(argv[3]);
    const char* out_path = argv[4];

    size_t src_bytes = (size_t)W * H * 3 / 2;
    size_t dst_bytes = (size_t)W * H * 3;

    FILE* fi = fopen(in_path, "rb");
    if (!fi) { perror("open input"); return 3; }
    uint8_t* cpu_src = (uint8_t*)malloc(src_bytes);
    if (fread(cpu_src, 1, src_bytes, fi) != src_bytes) { perror("read input"); return 3; }
    fclose(fi);

    /* 用虚拟地址（VA）路径——480×272 尺寸下 RGA3 的 VA 映射无问题 */
    uint8_t* dst = (uint8_t*)malloc(dst_bytes);

    rga_buffer_t s = wrapbuffer_virtualaddr_t(cpu_src, W, H, W, H, RK_FORMAT_YCbCr_420_SP);
    rga_buffer_t d = wrapbuffer_virtualaddr_t(dst, W, H, W, H, RK_FORMAT_RGB_888);
    im_rect srect = {0, 0, W, H};
    im_rect drect = {0, 0, W, H};
    im_rect prect = {0, 0, 0, 0};
    rga_buffer_t pat; memset(&pat, 0, sizeof(pat));

    IM_STATUS st = improcess(s, d, pat, srect, drect, prect, IM_SYNC);
    fprintf(stderr, "improcess=%d (%s)\n", (int)st, imStrError_t(st));

    FILE* fo = fopen(out_path, "wb");
    if (!fo) { perror("open out"); return 6; }
    fwrite(dst, 1, dst_bytes, fo);
    fclose(fo);
    fprintf(stderr, "wrote %zu bytes -> %s\n", dst_bytes, out_path);

    free(cpu_src);
    free(dst);
    return 0;
}