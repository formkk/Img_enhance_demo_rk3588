#!/usr/bin/env bash
# b_runbook_ft8.sh —— 阶段 8：**SGD 的 LR 扫参（S18-A 后续，2026-10-16）**
#
# 依据（草案 §8.29(十)）：S18-A 已定位「优化器家族」为最大单一变量 —— R2（sgd lr=5e-3）
# ep12 0.3290 / ep24 0.3246，比 R0（我的原配方）高 +0.024/+0.031；而 wd/调度/warmup/空图那一揽子（R1）
# 在 P2K 下几乎无效（R1≈R0）。**LR 是当前最明显未调优的旋钮**（5e-3 只是「0.02@bs16 线性折算」的
# 推断值，从未扫过）。本 runbook 在 **R2 配方不变**的前提下只改 LR，并顺带加上已验证有效的 EMA。
#
# 协议（与 S18-A 各臂完全一致，可直接比）：P2K = `res2/old2000`（旧 2000 张）/
# `--skip-empty-gt` ⇒ 496 iter/epoch / bs=4 / clip=10 / seed=0 / **24 ep** / 单卡 3090；
# 评测 coco500 @ ep12 / ep24（+ EMA 权重）。
#
# 两臂：
#   S1_sgd1e2 ：sgd **lr=1e-2** + EMA(0.9998)
#   S2_sgd2e3 ：sgd **lr=2e-3** + EMA(0.9998)   ← 反向对照（比 R2 更低）
# （R2 的 lr=5e-3 已有结果：ep12 0.3290 / ep24 0.3246 / AP_small 0.2188）
#
# 用法：nohup bash b_runbook_ft8.sh > /home/jiangfeng/dcm_ft/run_stage8.log 2>&1 &
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

cd "$DTM"
ts() { date "+%F %T"; }

COMMON="--optim sgd --momentum 0.9 --weight-decay 1e-4 --lr-sched iter --warmup-iters 500 \
--no-wd-norm-bias --skip-empty-gt --ema-decay 0.9998"

run_arm() {  # $1=臂名 $2=lr
  local name=$1 lr=$2
  echo "[$(ts)] === $name ：sgd lr=$lr + EMA(0.9998) ==="
  $PY -u $ROOT/scripts/b2c_finetune.py --data-dir "$DATA" --ann-json "$ANN" \
      --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
      --n-train 2000 --bs 4 --epochs 24 --clip 10 --seed 0 $COMMON --lr "$lr" \
      --out "$ROOT/ckpt_$name"
  for spec in "ep12.pth ep12" "ep24.pth ep24" "ep24_ema.pth ep24ema"; do
    set -- $spec
    $PY -u $ROOT/scripts/b3c_eval_official.py --img-dir "$VAL500" \
        --res2-dir $ROOT/res2/coco500_q31 --ann $COCO/annotations/instances_val2017.json \
        --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
        --ckpt "$ROOT/ckpt_$name/$1" --out "$ROOT/eval/${name}_$2"
    echo "[$(ts)] $name @ $2 ：$(cat $ROOT/eval/${name}_$2/summary.txt)"
  done
}

echo "[$(ts)] stage8 启动（SGD LR 扫参）；GPU："
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader

run_arm S1_sgd1e2 1e-2
run_arm S2_sgd2e3 2e-3

echo "[$(ts)] === 汇总（对照 R2 = sgd 5e-3：ep12 0.3290 / ep24 0.3246 / AP_small 0.2188）==="
for t in R2_d2sgd_ep12 R2_d2sgd_ep24 \
         S1_sgd1e2_ep12 S1_sgd1e2_ep24 S1_sgd1e2_ep24ema \
         S2_sgd2e3_ep12 S2_sgd2e3_ep24 S2_sgd2e3_ep24ema; do
  printf "%-22s %s\n" "$t" "$(cat $ROOT/eval/$t/summary.txt 2>/dev/null || echo none)"
done
echo "[$(ts)] STAGE8_DONE"
