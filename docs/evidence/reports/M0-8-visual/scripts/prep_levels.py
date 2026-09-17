"""按 §3.1 几何 G1：源图等比缩到各档有效区（256×144 / 480×270 / 960×540），再上下 pad 到模型 shape。"""
import numpy as np, cv2
LEVELS = [('256x144', 256, 144), ('480x272', 480, 270), ('960x544', 960, 540)]
SRC = {'18': '/tmp/vis/src_18.png', '06cr2': '/tmp/vis/src_06cr2.png'}
for tag, path in SRC.items():
    bgr = cv2.imread(path)
    for name, W, He in LEVELS:
        r = cv2.cvtColor(cv2.resize(bgr, (W, He), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
        H = 272 if name == '480x272' else (544 if name == '960x544' else 144)
        pad = (H - He) // 2
        canvas = np.zeros((H, W, 3), dtype=np.uint8)
        canvas[pad:pad + He] = r                      # 帧外/有效区外补 0（FP16 路径：0 即实值 0，无量化陷阱）
        chw = canvas.astype(np.float32).transpose(2, 0, 1)[None]
        np.ascontiguousarray(chw.transpose(0, 2, 3, 1)).tofile(f'/tmp/vis/lv_{tag}_{name}.f32')
        print(f'{tag} {name}: {chw.shape} pad={pad}')
