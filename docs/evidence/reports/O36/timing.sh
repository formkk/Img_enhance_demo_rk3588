#!/bin/bash
# O36：候选 mask 的耗时复测（加大 runs 降噪）——HQ 512x288 (8 runs) / HQ 960x544 (3 runs) / ZDE 480x272 int8 (8 runs)
P=~/rknn_ab/probe/rknn_c_probe
D=~/rknn_ab/data
for k in 0 3 4; do
  echo "{\"tag\":\"hq512\",\"mask\":$k,\"r\":$( $P ~/rknn_ab/models/hq/hq_512x288_fp16_tk232.rknn $D/hq/in_512x288_nhwc.f32 8 nhwc float nosram f32 noperf "" $k 2>/dev/null | tr -d "\n")}"
done
for k in 0 3 4; do
  echo "{\"tag\":\"hq960\",\"mask\":$k,\"r\":$( $P ~/rknn_ab/models/hq/hq_960x544_fp16_tk232.rknn $D/hq/in_960x544_nhwc.f32 3 nhwc float nosram f32 noperf "" $k 2>/dev/null | tr -d "\n")}"
done
for k in 0 1 2 3 4; do
  echo "{\"tag\":\"zde480i8\",\"mask\":$k,\"r\":$( $P ~/rknn_ab/models/zde_curve_480x272_int8_tk232.rknn $D/in_480x272_nhwc.f32 10 nhwc float nosram f32 noperf "" $k 2>/dev/null | tr -d "\n")}"
done
echo "### DONE"
