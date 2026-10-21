#!/usr/bin/env bash
# b_runbook_ft3.sh —— 阶段 3：**训练 + 评测一把跑完**（147，单卡顺序）
#   前置：res2 缓存已备（res2/train10k = 10000 张、res2/coco500_q31 = 500 张）
#   产出：ckpt_base2000/ep{1..6}.pth、ckpt_full10000/ep{1..6}.pth
#         + eval/{base2000,full10000}_ep{3,6}/summary.txt（coco500 mAP，官方口径）
# 用法：nohup bash b_runbook_ft3.sh > /home/jiangfeng/dcm_ft/run_stage3.log 2>&1 &
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
CFG=$ROOT/d2src/configs/COCO-Detection/faster_rcnn_X_101_32x8d_FPN_3x.yaml
cd "$DTM"
ts() { date "+%F %T"; }
echo "[$(ts)] stage3 启动"

# ---- 等 GPU 空闲（锚点评测 / 其它训练跑完）----
for i in $(seq 1 120); do
  P=$(pgrep -fc "b3c_eval_official.py") || P=0
  Q=$(pgrep -fc "b2c_finetune.py") || Q=0
  if [ "$P" -eq 0 ] && [ "$Q" -eq 0 ]; then break; fi
  echo "[$(ts)] 等 GPU：eval=$P train=$Q"
  sleep 30
done

train() {
  echo "[$(ts)] --- 训练：--n-train $1 → $2（bs=4 / lr=1e-4 / 6ep）---"
  $PY -u $ROOT/scripts/b2c_finetune.py --data-dir $ROOT/res2/train10k \
      --ann-json $COCO/annotations/instances_train2017.json --config-file "$CFG" \
      --detector-weights "$W" --adapter-model-path "$CKPT" \
      --n-train "$1" --bs 4 --epochs 6 --out "$2"
}

evaluate() {  # $1 = ckpt, $2 = out dir
  echo "[$(ts)] --- 评测：$1 → $2 ---"
  $PY -u $ROOT/scripts/b3c_eval_official.py --img-dir "$VAL500" \
      --res2-dir $ROOT/res2/coco500_q31 --ann $COCO/annotations/instances_val2017.json \
      --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
      --ckpt "$1" --out "$2"
}

echo "[$(ts)] === [6a] 基线 2000 张 × 6ep ==="
train 2000  $ROOT/ckpt_base2000
echo "[$(ts)] === [6b] 扩容 10000 张 × 6ep ==="
train 10000 $ROOT/ckpt_full10000

echo "[$(ts)] === [7] coco500 评测（ep3 / ep6，两组同尺子）==="
for ep in 3 6; do
  evaluate $ROOT/ckpt_base2000/ep$ep.pth  $ROOT/eval/base2000_ep$ep
  evaluate $ROOT/ckpt_full10000/ep$ep.pth $ROOT/eval/full10000_ep$ep
done
echo "[$(ts)] === 汇总 ==="
for d in base2000_ep3 base2000_ep6 full10000_ep3 full10000_ep6; do
  printf "%-18s %s\n" "$d" "$(cat $ROOT/eval/$d/summary.txt 2>/dev/null || echo '(无)')"
done
echo "[$(ts)] STAGE3_DONE"
