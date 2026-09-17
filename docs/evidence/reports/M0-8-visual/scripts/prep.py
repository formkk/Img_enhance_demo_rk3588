import numpy as np, cv2, rawpy, os
refs='refs/pre-research/'
# ① 18.png（真实暗场景，1080x720）
img18 = cv2.imread(refs+'m0_results/18.png')
cv2.imwrite('/tmp/vis/src_18.png', img18)
print('18.png', img18.shape, 'mean=%.2f' % img18.mean())
# ② 06.CR2（Sony RAW）：no_auto_bright=True 解拜耳 = 未增强的原始暗帧（口径同预研 §12.9.4）
with rawpy.imread(refs+'06.CR2') as raw:
    rgb16 = raw.postprocess(no_auto_bright=True, output_bps=16)
u8 = (rgb16 / 257.0).round().astype(np.uint8)          # 16bit→8bit 线性映射（不做自动亮度）
cv2.imwrite('/tmp/vis/src_06cr2.png', cv2.cvtColor(u8, cv2.COLOR_RGB2BGR))
print('06.CR2', u8.shape, 'mean=%.2f' % u8.mean())
# ③ 估计输入：缩到 480x272（RGB float32 CHW，与转换约定一致 → NHWC 裸文件供 C 探针）
for tag, bgr in [('18', img18), ('06cr2', cv2.cvtColor(u8, cv2.COLOR_RGB2BGR))]:
    small = cv2.cvtColor(cv2.resize(bgr, (480, 272), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    chw = small.astype(np.float32).transpose(2,0,1)[None]
    np.save(f'/tmp/vis/est_in_{tag}.npy', chw)
    np.ascontiguousarray(chw.transpose(0,2,3,1)).tofile(f'/tmp/vis/est_in_{tag}_nhwc.f32')
    print(f'估计输入 {tag}: {chw.shape}')
