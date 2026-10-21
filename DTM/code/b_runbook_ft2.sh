#!/usr/bin/env bash
# b_runbook_ft2.sh —— 阶段 2：等阶段 1 的 res2 合并完成后，跑 锚点自检 + 两次训练。
# 为什么分阶段：阶段 1 首跑时 [2] 因**相对 config 路径**静默失败（当时只 `set -u`），
# 而 [1][3][4][5]（预计算与合并）本身正确 ⇒ 不重跑预计算，只补跑 [2] 与 [6]。
# 用法：nohup bash b_runbook_ft2.sh > /home/jiangfeng/dcm_ft/run_stage2.log 2>&1 &
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
echo "[$(ts)] stage2 启动（本行必须最先出现；若日志 0 字节即脚本在首个命令就退出）"

# ---- 等待：train10k 合并到 10000 张，且没有其他训练进程在跑 ----
# **（2026-10-15 首跑教训）** 等待循环里 `N=$(ls <可能不存在的目录> | wc -l)` 在 `set -euo pipefail`
# 下会**直接退出**，而 `2>/dev/null` 把报错吞掉 ⇒ 日志 0 字节、进程消失。故：先 `mkdir -p`，
# 且每条命令都带 `|| VAR=0` 兜底。
mkdir -p "$ROOT/res2/train10k"
N=0
P=0
for i in $(seq 1 200); do
  N=$(ls "$ROOT/res2/train10k" 2>/dev/null | wc -l) || N=0
  P=$(pgrep -fc "b2c_finetune.py") || P=0
  echo "[$(ts)] 等待中：train10k=$N 训练进程=$P"
  if [ "$N" -eq 10000 ] && [ "$P" -eq 0 ]; then break; fi
  sleep 30
done
[ "$N" -eq 10000 ] || { echo "!! train10k 未达到 10000（N=$N），退出"; exit 2; }
echo "[$(ts)] 就绪：train10k=$N 张，开始锚点与训练"

echo "[$(ts)] === [2b] 锚点自检（官方权重 + D 恒等）⇒ 期望 mAP≈0.29981 ==="
$PY -u $ROOT/scripts/b3c_eval_official.py --img-dir "$VAL500" --res2-dir $ROOT/res2/coco500_q31 \
    --ann $COCO/annotations/instances_val2017.json --config-file "$CFG" \
    --detector-weights "$W" --adapter-model-path "$CKPT" --identity \
    --out $ROOT/eval/identity_recheck

train() {
  echo "[$(ts)] --- 训练：--n-train $1 → $2 ---"
  $PY -u $ROOT/scripts/b2c_finetune.py --data-dir $ROOT/res2/train10k \
      --ann-json $COCO/annotations/instances_train2017.json --config-file "$CFG" \
      --detector-weights "$W" --adapter-model-path "$CKPT" \
      --n-train "$1" --bs 4 --epochs 6 --out "$2"
}
echo "[$(ts)] === [6a] 基线 2000 张 × 6ep ==="
train 2000  $ROOT/ckpt_base2000
echo "[$(ts)] === [6b] 扩容 10000 张 × 6ep ==="
train 10000 $ROOT/ckpt_full10000
echo "[$(ts)] STAGE2_DONE"
