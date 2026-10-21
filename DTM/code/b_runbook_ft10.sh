#!/usr/bin/env bash
# b_runbook_ft10.sh —— 阶段 10：**LR 扫参补点（lr=1e-3）**（S18-A 后续，2026-10-16）
#
# 依据：stage8 的 LR 扫参结果**单调**（越低越好）——
#   1e-2 → ep24 0.3153 / EMA 0.3308（AP_small 0.2126）
#   5e-3 → ep24 0.3246 / ep12 0.3290（AP_small 0.2188）
#   2e-3 → ep24 0.3360 / ep12 0.3418 / EMA 0.3412（AP_small 0.2431 ← 目前最好）
# ⇒ 2e-3 可能不是最优 ⇒ 本臂补测 **lr=1e-3**（配方与其它一切不变，只改 LR）。
#
# 协议与 stage8 完全一致（可直接比）：P2K = `res2/old2000`（旧 2000 张）/ `--skip-empty-gt`
# （496 iter/epoch）/ bs=4 / clip=10 / seed=0 / 24 ep / 逐 iter 余弦 + warmup 500 + 免 wd + EMA(0.9998)。
# 评测 coco500 @ ep12 / ep24 / ep24_ema。
#
# 用法：nohup bash b_runbook_ft10.sh > /home/jiangfeng/dcm_ft/run_stage10.log 2>&1 &
set -euo pipefail

PY=/home/jiangfeng/anaconda3/envs/dtm-v/bin/python
ROOT=/home/jiangfeng/dcm_ft
DTM=$ROOT/DTM-V-1.5
R=$DTM/scripts/DCM_Datasets_Feature_Inference_Scripts
export PYTHONPATH=$ROOT/d2src:$ROOT/pylibs:$R
export CUDA_VISIBLE_DEVICES=0

COCO=/home/jiangfeng/datasets/coco2017
VAL500=/home/jiangfeng/datasets/coco_val_500
ANN=$ROOT/ann/instances_trainval2017.json
W=$DTM/packages/Model/Detector_model/model_final_68b088.pkl
CKPT=$DTM/packages/Model/MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth
CFG=$ROOT/d2src/configs/COCO-Detection/faster_rcnn_X_101_32x8d_FPN_3x.yaml
DATA=$ROOT/res2/old2000
NAME=S3b_sgd1e3

cd "$DTM"
ts() { date "+%F %T"; }

echo "[$(ts)] stage10 启动（lr=1e-3 补点）；GPU："
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader

$PY -u $ROOT/scripts/b2c_finetune.py --data-dir "$DATA" --ann-json "$ANN" \
    --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
    --n-train 2000 --bs 4 --epochs 24 --clip 10 --seed 0 \
    --optim sgd --momentum 0.9 --weight-decay 1e-4 --lr-sched iter --warmup-iters 500 \
    --no-wd-norm-bias --skip-empty-gt --ema-decay 0.9998 --lr 1e-3 \
    --out "$ROOT/ckpt_$NAME"

for spec in "ep12.pth ep12" "ep24.pth ep24" "ep24_ema.pth ep24ema"; do
  set -- $spec
  $PY -u $ROOT/scripts/b3c_eval_official.py --img-dir "$VAL500" \
      --res2-dir $ROOT/res2/coco500_q31 --ann $COCO/annotations/instances_val2017.json \
      --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
      --ckpt "$ROOT/ckpt_$NAME/$1" --out "$ROOT/eval/${NAME}_$2"
  echo "[$(ts)] $NAME @ $2 ：$(cat $ROOT/eval/${NAME}_$2/summary.txt)"
done

echo "[$(ts)] === LR 扫参全景（P2K × 24 ep，配方相同只改 LR）==="
for t in R2_d2sgd_ep12 R2_d2sgd_ep24 S1_sgd1e2_ep24ema S2_sgd2e3_ep12 S2_sgd2e3_ep24ema \
         ${NAME}_ep12 ${NAME}_ep24 ${NAME}_ep24ema; do
  printf "%-22s %s\n" "$t" "$(cat $ROOT/eval/$t/summary.txt 2>/dev/null || echo none)"
done
echo "[$(ts)] STAGE10_DONE"
