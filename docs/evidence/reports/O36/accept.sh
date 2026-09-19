#!/bin/bash
# O36：core_mask 可接受性扫描（每个 mask 1 次，安静板）——输出为**良构 JSON 行**
P=~/rknn_ab/probe/rknn_c_probe
D=~/rknn_ab/data
emit() { # model input mask tag
  local out
  out=$($P "$1" "$2" 1 nhwc float nosram f32 noperf "" "$3" 2>&1)
  out=$(echo "$out" | tr -d "\n" | sed -E "s/^E RKNN: \[[^]]*\] //g; s/E RKNN: \[[^]]*\] //g")
  echo "{\"tag\":\"$4\",\"mask\":$3,\"probe\":$out}"
}
for k in 0 1 2 3 4 5 6 7; do
  emit ~/rknn_ab/models/hq/hq_512x288_fp16_tk232.rknn $D/hq/in_512x288_nhwc.f32 $k hq512x288
done
for k in 0 1 2 3 4 5 6 7; do
  emit ~/rknn_ab/models/zde_curve_480x272_int8_tk232.rknn $D/in_480x272_nhwc.f32 $k zde480x272_int8
done
echo "### DONE"
