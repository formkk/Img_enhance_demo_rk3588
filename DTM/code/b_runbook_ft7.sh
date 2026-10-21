#!/usr/bin/env bash
# b_runbook_ft7.sh —— 阶段 7：**训练方案重设计（S17d，2026-10-16）**
#
# ── 重设计依据（为什么换基座）─────────────────────────────────────────────
# 阶段 4~6 的"基线 2000 张"其实是 `res2/train10k` **文件名排序前 2000** = **train2017 图**，
# 而旧记录 0.432 用的适配集是 **val2017 派生的 2000 张**（147 上 `datasets/coco_train2000` 仍在）
# ⇒ 两者**交集为 0**（已核对）。本次核实：该集 **16/2000 无 GT**，与 §8.21「1984/2000（16 张跳过）」
# **逐字吻合** ⇒ 确认为旧脚本原集。其 res2 已由 `val_rest4500` 覆盖（0 缺失，符号链接目录
# `res2/old2000`，76 KB，未重算）⇒ **忠实复现协议可完整重建**。
#
# ── 协议（P2K：忠实复现旧口径）──────────────────────────────────────────
# 数据 = `res2/old2000`（旧 2000 张）/ bs=4 / clip=10 / seed=0 / **24 ep**
# （跳过空图后 1984 张 ⇒ **496 iter/epoch = 旧记录逐字一致**）/ 评测 = coco500（ep12 与 ep24）
#
# ── 四臂（每臂 ≈ 80 min 训练 + 2×6 min 评测）─────────────────────────────
# R0 复刻参照   ：**我的原配方**（lr-sched=epoch、wd=0、不跳空图）⇒ 量化"在旧数据+旧轮数下
#                 我的配方能到多少" —— **没有它就无法判断后面哪一臂算'恢复'**（判据基准）。
# R1 d2-AdamW   ：wd=**1e-2**（torch AdamW 默认值；旧脚本可能未显式传 wd）+ 逐 iter 余弦
#                 + warmup 500 + GN/偏置免 wd + 跳空图
# R2 d2-SGD     ：**sgd lr=5e-3**（d2 官方 0.02@bs16 按线性缩放折算 bs4）m0.9 wd=1e-4
#                 + 逐 iter 余弦 + warmup 500 + GN/偏置免 wd + 跳空图
# R3 R1+EMA     ：R1 配方 + EMA(0.9998)，同时评测 ep24 与 ep24_ema
#
# 用法：nohup bash b_runbook_ft7.sh > /home/jiangfeng/dcm_ft/run_stage7.log 2>&1 &
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

train() {  # $1=臂名 $2..=额外超参
  local name=$1; shift
  $PY -u $ROOT/scripts/b2c_finetune.py --data-dir "$DATA" --ann-json "$ANN" \
      --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
      --n-train 2000 --bs 4 --epochs 24 --clip 10 --seed 0 \
      --out "$ROOT/ckpt_$name" "$@"
}
evaluate() {  # $1=臂名 $2=ckpt 文件名 $3=标签
  $PY -u $ROOT/scripts/b3c_eval_official.py --img-dir "$VAL500" \
      --res2-dir $ROOT/res2/coco500_q31 --ann $COCO/annotations/instances_val2017.json \
      --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
      --ckpt "$ROOT/ckpt_$1/$2" --out "$ROOT/eval/$1_$3"
  echo "[$(ts)] $1 @ $3 ：$(cat $ROOT/eval/$1_$3/summary.txt)"
}

echo "[$(ts)] stage7 启动（P2K 忠实复现 + 四臂）；GPU："
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader

echo "[$(ts)] === R0 复刻参照（我的原配方）==="
train R0_repro
evaluate R0_repro ep12.pth ep12
evaluate R0_repro ep24.pth ep24

echo "[$(ts)] === R1 d2-AdamW（wd=1e-2 + 逐iter余弦 + warmup + 免wd + 跳空图）==="
train R1_d2adamw --optim adamw --lr 1e-4 --weight-decay 1e-2 \
      --lr-sched iter --warmup-iters 500 --no-wd-norm-bias --skip-empty-gt
evaluate R1_d2adamw ep12.pth ep12
evaluate R1_d2adamw ep24.pth ep24

echo "[$(ts)] === R2 d2-SGD（sgd lr=5e-3 + 同上调度/免wd/跳空图）==="
train R2_d2sgd --optim sgd --lr 5e-3 --momentum 0.9 --weight-decay 1e-4 \
      --lr-sched iter --warmup-iters 500 --no-wd-norm-bias --skip-empty-gt
evaluate R2_d2sgd ep12.pth ep12
evaluate R2_d2sgd ep24.pth ep24

echo "[$(ts)] === R3 R1+EMA(0.9998) ==="
train R3_r1ema --optim adamw --lr 1e-4 --weight-decay 1e-2 \
      --lr-sched iter --warmup-iters 500 --no-wd-norm-bias --skip-empty-gt \
      --ema-decay 0.9998
evaluate R3_r1ema ep24.pth ep24
evaluate R3_r1ema ep24_ema.pth ep24ema

echo "[$(ts)] === 汇总（目标：旧记录 2000张×24ep = mAP 0.432 / AP50 0.611 / small 0.305） ==="
printf "%-22s %s\n" "锚点(未微调)" "$(cat $ROOT/eval/anchor/summary.txt 2>/dev/null || echo —)"
for t in R0_repro_ep12 R0_repro_ep24 R1_d2adamw_ep12 R1_d2adamw_ep24 \
         R2_d2sgd_ep12 R2_d2sgd_ep24 R3_r1ema_ep24 R3_r1ema_ep24ema; do
  printf "%-22s %s\n" "$t" "$(cat $ROOT/eval/$t/summary.txt 2>/dev/null || echo none)"
done
echo "[$(ts)] STAGE7_DONE"
