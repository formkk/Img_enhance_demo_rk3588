# O87 低照度可用性实验 —— 测量条件（随行，规则 `.trae/rules/10-evidence.md` §1）

板　　型：Embedfire LubanCat-5 V2（RK3588），内核 `5.10.160`，SSH 别名 `board`
模　　式：当前 1080p 采集（`camera.mode_class = "binning"`；1944×1096 读出 → 驱动裁至 1920×1080）
I　　Q：**采集档** `imx415_CMK-OT2022-PX1_IR0147-50IRC-8M-F20.json`，sha256 = **`5cc97e06b76e8971…`**
　　　　　（`TimeValue = 0.033333 s` / `GainValue = 8.0` / `AecOpType = MANUAL` / **3DNR 关** / `FpsValue = 30`）
曝光/时序（**全程固定**）：`exposure = 4474` 行（≈1/30 s）、`vertical_blanking = 3404`（VMAX ≈4500 ⇒ 30 fps）
镜　　头：**1:1.6、3.6–10 mm 变焦**（用户 2026-09-20 口头告知；**变焦环位置未记**）
采　　样：每点 **300 帧连续**（`v4l2-ctl -d /dev/video11 --stream-mmap=8 --stream-count=300 --stream-to=…`）
　　　　　实测 **30.00 fps**、**933,120,000 B / 次**（= 300 × 3,110,400）
增益改法（**运行期微调**，规则 `20-contracts.md` §4 明文允许）：
　　　　`v4l2-ctl -d /dev/v4l-subdev2 --set-ctrl=analogue_gain=<reg>`
　　　　寄存器映射（v3.83 已定）：`reg = 20·log₁₀(gain)/0.3` ⇒ **8× = 60 / 16× = 80 / 32× = 100**
　　　　生效证据：`dmesg` 在 `s_stream: 1` 时写入 `set analog gain 0x3c / 0x50 / 0x64`（见 `dmesg_gain_writes.out`）；
　　　　**流结束后驱动恢复为 IQ 手动值 `0x3c`** ⇒ **每个采样点前必须重设**
照度点（**用户改灯并按口头口径给值，未用照度计**）：① 关灯 **0.2 Lux** ② **1 Lux**（用户裁定的硬目标）③ 开灯 **4 Lux**
采集时点：板时 `87083–87548 s`（2026-09-20 23:0x CST）
原始件：**9 × 933 MB NV12 未入库**，仍在开发机 `/tmp/il{0,1,2}_g{60,80,100}.nv12`（可复现命令在上表）

> 本轮**未变更**：`config.yaml`、DT / overlay、IQ 档位、sensor 模式；**未重启 3A**、**未停桌面**。
> 副作用仅"运行期增益微调 + 每次 10 s 取流"。