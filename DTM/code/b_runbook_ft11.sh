#!/usr/bin/env bash
# b_runbook_ft11.sh —— 阶段 11：**S4 多尺度重跑（含 S18-C 几何修正）+ 串接 lr=1e-3 补点**
#
# 事故与修正（2026-10-16）：stage9 的 S4 于首个 batch 崩于
#   `000000077595: 几何不自洽 (896,1344) vs res2 反推 (960,1472)`
# 根因 = `b2c_finetune.py::__getitem__` 里**保留下来的"长边再夹一次"旧代码仍硬编码 `XMAX=1333`**，
# 把按尺度算好的长边上限（960 档应为 **1600**；640 档 1066）又夹回 1333 ⇒ 与预计算的该档 res2 不自洽。
# 修正 = 该夹取的界改为随尺度而变的 `clamp`（单尺度分支仍 = XMAX，**行为逐位不变**）。
# 验证 = 40 图 × 3 档 = 120 组几何比对，**0 处不一致**。
#
# 本 runbook：① 重跑 S4（协议与 stage9 完全一致）；② 完成后串接 `b_runbook_ft10.sh`（lr=1e-3 补点）。
# 用法：nohup bash b_runbook_ft11.sh > /home/jiangfeng/dcm_ft/run_stage9b.log 2>&1 &
set -euo pipefail

PY=/home/jiangfeng/anaconda3/envs/dtm-v/bin/python
ROOT=/home/jiangfeng/dcm_ft
DTM=$ROOT/DTM-V-1.5
R=$DTM/scripts/DCM_Datasets_Feature_Inference_Scripts
export PYTHONPATH=$ROOT/d2src:$ROOT/pylibs:$R
export CUDA_VISIBLE_DEVICES=0

LR=${LR:-2e-3}
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
eval_one() {
  $PY -u $ROOT/scripts/b3c_eval_official.py --img-dir "$VAL500" \
      --res2-dir $ROOT/res2/coco500_q31 --ann $COCO/annotations/instances_val2017.json \
      --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
      --ckpt "$ROOT/ckpt_$1/$2" --out "$ROOT/eval/$1_$3"
  echo "[$(ts)] $1 @ $3 ：$(cat $ROOT/eval/$1_$3/summary.txt)"
}

echo "[$(ts)] stage11 启动（S4 重跑 + lr=1e-3）；胜出 LR=$LR；GPU："
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader

echo "[$(ts)] === S4（重跑）：多尺度 {800,960,640} × P2K × 24 ep ==="
$PY -u $ROOT/scripts/b2c_finetune.py --data-dir $ROOT/res2/old2000 --ann-json "$ANN" \
    --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
    --n-train 2000 --bs 4 --epochs 24 --clip 10 --seed 0 $COMMON --lr "$LR" \
    --scale-dirs "800:$ROOT/res2/old2000,960:$ROOT/res2/old2000_s960,640:$ROOT/res2/old2000_s640" \
    --out "$ROOT/ckpt_S4_ms"
eval_one S4_ms ep12.pth ep12
eval_one S4_ms ep24.pth ep24
eval_one S4_ms ep24_ema.pth ep24ema

echo "[$(ts)] === S4 汇总（对照：同配方单尺度 P2K = ep12 0.3418 / EMA 0.3412）==="
for t in S2_sgd2e3_ep12 S2_sgd2e3_ep24ema S4_ms_ep12 S4_ms_ep24 S4_ms_ep24ema; do
  printf "%-18s %s\n" "$t" "$(cat $ROOT/eval/$t/summary.txt 2>/dev/null || echo none)"
done

echo "[$(ts)] === 串接 stage10（lr=1e-3 补点）==="
bash $ROOT/scripts/b_runbook_ft10.sh
echo "[$(ts)] STAGE11_DONE"
