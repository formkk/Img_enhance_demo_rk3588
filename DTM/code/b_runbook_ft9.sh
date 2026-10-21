#!/usr/bin/env bash
# b_runbook_ft9.sh —— 阶段 9：**S3（10k 数据 × 胜出配方）+ S4（多尺度臂）**（S18-B / S18-C）
#
# 依赖：先由 stage8（S1/S2 LR 扫参）选出胜出 LR。用法：
#   nohup env LR=1e-2 bash b_runbook_ft9.sh > /home/jiangfeng/dcm_ft/run_stage9.log 2>&1 &
#
# ── S3（S18-B 复验）────────────────────────────────────────────────────
# 目的：① 把 P2K 上的胜出配方**迁移到 10k 数据**（`res2/train10k`，与评测集**跨 split**）；
#       ② 在**同数据同轮数**下与既有 AdamW 基线对照（10k×6ep、AdamW lr=1e-4 ⇒ mAP **0.3424**）。
# 协议：`--data-dir res2/train10k --n-train 10000` × **6 ep**（2500 iter/ep = 15k iter，与 P2K 24 ep 的
#       11.9k iter 同量级）、bs=4 / clip=10 / seed=0 / 逐 iter 余弦 + warmup 500 + 免 wd + 跳空图 + EMA。
# 评测：coco500 @ ep3 / ep6 / ep6_ema。
#
# ── S4（S18-C 多尺度）────────────────────────────────────────────────
# 目的：尺度抖动（训练侧）能否再抬一档 —— §8.21 记录的短板是 `AP_small = 0.305`。
# 协议：与 S18-A 完全同协议（P2K × 24 ep × 496 iter/epoch），**只加尺度池**
#       `{800, 960, 640}`（三档 res2 已预计算：`old2000` / `old2000_s960` / `old2000_s640`；
#       逐样本随机取一档，GT 按该档几何同式重算 ⇒ 与 res2 严格自洽）。
# 评测：coco500 @ ep12 / ep24（评测仍在 800 档，符合"抖动只在训练"的惯例）。
set -euo pipefail

PY=/home/jiangfeng/anaconda3/envs/dtm-v/bin/python
ROOT=/home/jiangfeng/dcm_ft
DTM=$ROOT/DTM-V-1.5
R=$DTM/scripts/DCM_Datasets_Feature_Inference_Scripts
export PYTHONPATH=$ROOT/d2src:$ROOT/pylibs:$R
export CUDA_VISIBLE_DEVICES=0

LR=${LR:?需要指定胜出 LR，例如 LR=1e-2}
COCO=/home/jiangfeng/datasets/coco2017
VAL500=/home/jiangfeng/datasets/coco_val_500
ANN=$ROOT/ann/instances_trainval2017.json
W=$DTM/packages/Model/Detector_model/model_final_68b088.pkl
CKPT=$DTM/packages/Model/MIKcodec_MCM_model/dtm-V_entropy_vm_1_270000.pth
CFG=$ROOT/d2src/configs/COCO-Detection/faster_rcnn_X_101_32x8d_FPN_3x.yaml
COMMON="--optim sgd --momentum 0.9 --weight-decay 1e-4 --lr-sched iter --warmup-iters 500 \
--no-wd-norm-bias --skip-empty-gt --ema-decay 0.9998"

cd "$DTM"
ts() { date "+%F %T"; }
eval_one() {  # $1=臂名 $2=ckpt 文件名 $3=标签
  $PY -u $ROOT/scripts/b3c_eval_official.py --img-dir "$VAL500" \
      --res2-dir $ROOT/res2/coco500_q31 --ann $COCO/annotations/instances_val2017.json \
      --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
      --ckpt "$ROOT/ckpt_$1/$2" --out "$ROOT/eval/$1_$3"
  echo "[$(ts)] $1 @ $3 ：$(cat $ROOT/eval/$1_$3/summary.txt)"
}

echo "[$(ts)] stage9 启动（S3 10k 复验 + S4 多尺度）；胜出 LR=$LR；GPU："
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader

echo "[$(ts)] === S3：10k × 6 ep（胜出配方）==="
$PY -u $ROOT/scripts/b2c_finetune.py --data-dir $ROOT/res2/train10k --ann-json "$ANN" \
    --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
    --n-train 10000 --bs 4 --epochs 6 --clip 10 --seed 0 $COMMON --lr "$LR" \
    --out "$ROOT/ckpt_S3_10k"
eval_one S3_10k ep3.pth ep3
eval_one S3_10k ep6.pth ep6
eval_one S3_10k ep6_ema.pth ep6ema

echo "[$(ts)] === S4：多尺度 {800,960,640} × P2K × 24 ep（胜出配方）==="
$PY -u $ROOT/scripts/b2c_finetune.py --data-dir $ROOT/res2/old2000 --ann-json "$ANN" \
    --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
    --n-train 2000 --bs 4 --epochs 24 --clip 10 --seed 0 $COMMON --lr "$LR" \
    --scale-dirs "800:$ROOT/res2/old2000,960:$ROOT/res2/old2000_s960,640:$ROOT/res2/old2000_s640" \
    --out "$ROOT/ckpt_S4_ms"
eval_one S4_ms ep12.pth ep12
eval_one S4_ms ep24.pth ep24
eval_one S4_ms ep24_ema.pth ep24ema

echo "[$(ts)] === 汇总（对照：10k×6ep AdamW = 0.3424；P2K 单尺度胜出臂见 stage8）==="
for t in full10000_ep6 S3_10k_ep3 S3_10k_ep6 S3_10k_ep6ema S4_ms_ep12 S4_ms_ep24 S4_ms_ep24ema; do
  printf "%-20s %s\n" "$t" "$(cat $ROOT/eval/$t/summary.txt 2>/dev/null || echo none)"
done
echo "[$(ts)] STAGE9_DONE"
