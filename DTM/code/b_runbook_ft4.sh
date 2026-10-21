#!/usr/bin/env bash
# 阶段 4：训练 + 评测（用**合并后的 GT** instances_trainval2017.json；适配集跨 val/train 两段）
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
cd "$DTM"
ts() { date "+%F %T"; }
echo "[$(ts)] stage4 启动（GT=$ANN；res2=$ROOT/res2/train10k）"
# GPU 空闲检查：**不要用 pgrep -f 匹配脚本名** —— 启动它的 ssh 包装进程 argv 含脚本正文，
# 会把包装进程误判为训练进程（首跑即 train=1/eval=1 空转）。这里只打印 GPU 状态。
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader
sleep 3
train() {
  echo "[$(ts)] --- 训练：--n-train $1 -> $2（bs=4 / lr=1e-4 / 6ep）---"
  $PY -u $ROOT/scripts/b2c_finetune.py --data-dir $ROOT/res2/train10k \
      --ann-json "$ANN" --config-file "$CFG" \
      --detector-weights "$W" --adapter-model-path "$CKPT" \
      --n-train "$1" --bs 4 --epochs 6 --out "$2"
}
evaluate() {
  echo "[$(ts)] --- 评测：$1 -> $2 ---"
  $PY -u $ROOT/scripts/b3c_eval_official.py --img-dir "$VAL500" \
      --res2-dir $ROOT/res2/coco500_q31 --ann $COCO/annotations/instances_val2017.json \
      --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
      --ckpt "$1" --out "$2"
}
echo "[$(ts)] === [6a] 基线 2000 张 x 6ep ==="
train 2000  $ROOT/ckpt_base2000
echo "[$(ts)] === [6b] 扩容 10000 张 x 6ep ==="
train 10000 $ROOT/ckpt_full10000
echo "[$(ts)] === [7] coco500 评测（ep3 / ep6）==="
for ep in 3 6; do
  evaluate $ROOT/ckpt_base2000/ep$ep.pth  $ROOT/eval/base2000_ep$ep
  evaluate $ROOT/ckpt_full10000/ep$ep.pth $ROOT/eval/full10000_ep$ep
done
echo "[$(ts)] === 汇总 ==="
for d in base2000_ep3 base2000_ep6 full10000_ep3 full10000_ep6; do
  printf "%-18s %s\n" "$d" "$(cat $ROOT/eval/$d/summary.txt 2>/dev/null || echo none)"
done
echo "[$(ts)] STAGE4_DONE"
