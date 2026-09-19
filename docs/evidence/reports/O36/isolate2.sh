#!/bin/bash
# O36 隔离验证 v2：**交替**顺序消除热漂移/顺序混淆，并记录 SoC 温度
P=~/rknn_ab/probe/rknn_c_probe
S=~/rknn_ab/probe/rknn_split_probe
D=~/rknn_ab/data
A() { $S ~/rknn_ab/models/zde_curve_256x272_int8_tk232.rknn $D/frame_480x272_u8.raw 272 480 30 2 0,1 nofloat; }
B() { $P ~/rknn_ab/models/hq/hq_512x288_fp16_tk232.rknn $D/hq/in_512x288_nhwc.f32 3 nhwc float nosram f32 noperf "" $1 > /dev/null 2>&1; }
T() { echo -n "$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)"; }
for r in 1 2 3; do
  echo "{\"round\":$r,\"scenario\":\"alone\",\"temp\":$(T),\"A\":$(A | tr -d "\n")}"
  for mask in 0 4; do
    B $mask & BP=$!
    sleep 1.5
    echo "{\"round\":$r,\"mask\":$mask,\"scenario\":\"hq\",\"temp\":$(T),\"A\":$(A | tr -d "\n")}"
    wait $BP
    sleep 5
  done
done
echo "### DONE"
