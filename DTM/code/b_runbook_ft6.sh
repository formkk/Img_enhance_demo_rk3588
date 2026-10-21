#!/usr/bin/env bash
# b_runbook_ft6.sh —— 阶段 6：**正则探针（S17c，2026-10-15）**
#
# 动机（本草案 §8.28）：10k×24ep 观察到**过拟合**（训练 loss_cls 0.1707→0.0651 而降，
# 而 coco500 mAP 0.3208(ep6)→0.3146(ep24) 持平/下滑）⇒ 首要嫌疑 = 重写脚本自选的
# `weight_decay=0.0`。本 runbook 在**与最佳参照完全相同**的协议下（10k / 6 ep / T_max=6 /
# bs=4 / seed 0 / clip 10）**只改一项正则变量**，逐臂评测 coco500。
#
# 参照（阶段 4 已跑）：`full10000_ep6` = **adamw lr=1e-4 wd=0 ⇒ mAP 0.3424**（本轮最高）。
#
# 四臂（**单卡串行**，臂间只差 --optim/--lr/--weight-decay）：
#   p_wd1e4        adamw lr=1e-4 wd=1e-4
#   p_wd5e4        adamw lr=1e-4 wd=5e-4
#   p_lr5e5_wd1e4  adamw lr=5e-5 wd=1e-4
#   p_sgd1e3_wd1e4 sgd   lr=1e-3 wd=1e-4 momentum=0.9
#
# **未加 warmup**（SGD 臂的残余混淆项，已在 §8.28 登记）；**不改任何板端资产**。
#
# 用法：nohup bash b_runbook_ft6.sh > /home/jiangfeng/dcm_ft/run_stage6.log 2>&1 &
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
DATA=$ROOT/res2/train10k

cd "$DTM"
ts() { date "+%F %T"; }

run_arm() {   # $1=臂名 $2..=额外超参
  local name=$1; shift
  echo "[$(ts)] === 臂 $name ：$* ==="
  $PY -u $ROOT/scripts/b2c_finetune.py --data-dir "$DATA" --ann-json "$ANN" \
      --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
      --n-train 10000 --bs 4 --epochs 6 --out "$ROOT/ckpt_$name" "$@"
  $PY -u $ROOT/scripts/b3c_eval_official.py --img-dir "$VAL500" \
      --res2-dir $ROOT/res2/coco500_q31 --ann $COCO/annotations/instances_val2017.json \
      --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
      --ckpt "$ROOT/ckpt_$name/ep6.pth" --out "$ROOT/eval/$name"
  echo "[$(ts)] 臂 $name 结果：$(cat $ROOT/eval/$name/summary.txt)"
}

echo "[$(ts)] stage6 启动（正则探针 S17c）；GPU："
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader

run_arm p_wd1e4        --optim adamw --lr 1e-4 --weight-decay 1e-4
run_arm p_wd5e4        --optim adamw --lr 1e-4 --weight-decay 5e-4
run_arm p_lr5e5_wd1e4  --optim adamw --lr 5e-5 --weight-decay 1e-4
run_arm p_sgd1e3_wd1e4 --optim sgd   --lr 1e-3 --weight-decay 1e-4 --momentum 0.9

echo "[$(ts)] === 汇总（参照：full10000_ep6 adamw lr1e-4 wd0 = 0.3424） ==="
printf "%-18s %s\n" full10000_ep6 "$(cat $ROOT/eval/full10000_ep6/summary.txt)"
for n in p_wd1e4 p_wd5e4 p_lr5e5_wd1e4 p_sgd1e3_wd1e4; do
  printf "%-18s %s\n" "$n" "$(cat $ROOT/eval/$n/summary.txt 2>/dev/null || echo none)"
done
echo "[$(ts)] STAGE6_DONE"
