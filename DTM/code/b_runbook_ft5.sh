#!/usr/bin/env bash
# 阶段 5：10000 张 x 24 ep（与旧 0.432 同协议：从头训、cosine T_max=24）+ 评测 ep6/12/18/24
# 用法：nohup bash b_runbook_ft5.sh > /home/jiangfeng/dcm_ft/run_stage5.log 2>&1 &
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
echo "[$(ts)] stage5 启动：--n-train 10000 --epochs 24"
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader
$PY -u $ROOT/scripts/b2c_finetune.py --data-dir $ROOT/res2/train10k \
    --ann-json "$ANN" --config-file "$CFG" \
    --detector-weights "$W" --adapter-model-path "$CKPT" \
    --n-train 10000 --bs 4 --epochs 24 --out $ROOT/ckpt_full10000_e24
echo "[$(ts)] 训练完成，开始评测 ep6/12/18/24"
for ep in 6 12 18 24; do
  $PY -u $ROOT/scripts/b3c_eval_official.py --img-dir "$VAL500" \
      --res2-dir $ROOT/res2/coco500_q31 --ann $COCO/annotations/instances_val2017.json \
      --config-file "$CFG" --detector-weights "$W" --adapter-model-path "$CKPT" \
      --ckpt $ROOT/ckpt_full10000_e24/ep$ep.pth --out $ROOT/eval/full10000_e24_ep$ep
done
echo "[$(ts)] === 汇总 ==="
for ep in 6 12 18 24; do
  printf "full10000_24ep_ep%-3s %s\n" $ep "$(cat $ROOT/eval/full10000_e24_ep$ep/summary.txt 2>/dev/null || echo none)"
done
echo "[$(ts)] STAGE5_DONE"
