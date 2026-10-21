#!/usr/bin/env bash
# b_runbook_ft.sh —— 「扩大特征域适配集」全流程 runbook（147 上跑，顺序执行、单卡）
# 顺序：[1] coco500 q31 res2 → [2] 锚点自检（官方权重+D恒等 ⇒ 期望 mAP≈0.29981）
#       → [3] val2017 余量 4500 → [4] train2017 前 5500 → [5] 硬链接合并 train10k
#       → [6] 训练：2000 张基线 6ep → 10000 张 6ep（同一份脚本 ⇒ 数据量是唯一变量）
# 用法：nohup bash b_runbook_ft.sh > /home/jiangfeng/dcm_ft/run_all.log 2>&1 &
# **（2026-10-15 首跑教训）** ① 必须 `set -euo pipefail`：首跑只用 `set -u` ⇒ 步骤 [2] 失败后**静默继续**；
#   ② `--config-file` 必须给**绝对路径**（相对路径按 CWD 解析，而本脚本 `cd $DTM` ≠ d2src 根）。
set -euo pipefail
PY=/home/jiangfeng/anaconda3/envs/dtm-v/bin/python
ROOT=/home/jiangfeng/dcm_ft
DTM=$ROOT/DTM-V-1.5
R=$DTM/scripts/DCM_Datasets_Feature_Inference_Scripts
export PYTHONPATH=$ROOT/d2src:$ROOT/pylibs:$R
export CUDA_VISIBLE_DEVICES=0
COCO=/home/jiangfeng/datasets/coco2017
VAL500=/home/jiangfeng/datasets/coco_val_500
W=$DTM/packages/Model/Detector_model/model_final_68b088.pkl
CKPT=$DTM/packages/Model/MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth
CFG=$ROOT/d2src/configs/COCO-Detection/faster_rcnn_X_101_32x8d_FPN_3x.yaml   # 绝对路径（见首跑教训②）
cd "$DTM" || exit 1
ts() { date "+%F %T"; }

echo "[$(ts)] === [1/6] coco500 q31 res2 ==="
$PY -u $ROOT/scripts/b1b_precompute_res2.py --src-dir "$VAL500" --out $ROOT/res2/coco500_q31 --q 31 --resume

echo "[$(ts)] === [2/6] 锚点（官方权重 + D 恒等）⇒ 期望 mAP≈0.29981 ==="
$PY -u $ROOT/scripts/b3c_eval_official.py --img-dir "$VAL500" --res2-dir $ROOT/res2/coco500_q31 \
    --ann $COCO/annotations/instances_val2017.json --config-file "$CFG" \
    --detector-weights "$W" --adapter-model-path "$CKPT" --identity --out $ROOT/eval/identity_recheck

echo "[$(ts)] === [3/6] val2017 余量 4500 ==="
$PY -u $ROOT/scripts/b1b_precompute_res2.py --src-dir $COCO/val2017 --out $ROOT/res2/val_rest4500 \
    --exclude-dir "$VAL500" --q 31 --resume

echo "[$(ts)] === [4/6] train2017 前 5500 ==="
$PY -u $ROOT/scripts/b1b_precompute_res2.py --src-dir $COCO/train2017 --out $ROOT/res2/train_first5500 \
    --n 5500 --q 31 --resume

echo "[$(ts)] === [5/6] 合并 train10k（硬链接） ==="
mkdir -p $ROOT/res2/train10k
for d in $ROOT/res2/val_rest4500 $ROOT/res2/train_first5500; do
  for f in "$d"/res2_*.pt; do ln -f "$f" $ROOT/res2/train10k/ 2>/dev/null || cp -n "$f" $ROOT/res2/train10k/; done
done
N=$(ls $ROOT/res2/train10k | wc -l)
echo "[$(ts)] train10k 文件数 = $N（期望 10000）"
[ "$N" -eq 10000 ] || { echo "!! 数量不符，停止"; exit 2; }

train() {
  echo "[$(ts)] --- 训练：--n-train $1 → $2 ---"
  $PY -u $ROOT/scripts/b2c_finetune.py --data-dir $ROOT/res2/train10k \
      --ann-json $COCO/annotations/instances_train2017.json --config-file "$CFG" \
      --detector-weights "$W" --adapter-model-path "$CKPT" \
      --n-train "$1" --bs 4 --epochs 6 --out "$2"
}

echo "[$(ts)] === [6/6] 训练 ==="
train 2000  $ROOT/ckpt_base2000
train 10000 $ROOT/ckpt_full10000
echo "[$(ts)] ALL_DONE"
