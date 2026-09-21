#!/usr/bin/env python3
# i2c_reg_readback.py —— IMX415 sensor 寄存器**只读**回读探针（M1-2 采集模式定类证据）
#
# 用途：板上经 /dev/i2c-<n> 以 I2C_RDWR 读事务回读 sensor 寄存器（16 bit 地址 / 8 bit 数据），
#       用于判定当前 1080p 采集的读出模式（binning / subsample / crop）。
# 性质：**只读**——每个寄存器 = 一次「写 2 字节寄存器地址 + 读 1 字节数据」的读事务，
#       **不写任何配置值、不改 IQ、不改 config**；需要 root（/dev/i2c-N 属 root:i2c）。
# 口径：**必须在取流期间（s_stream=1）执行**才有效——模式寄存器组由驱动的 start_stream 写入；
#       待机时读到的是全局默认值（0x3022=0x00 / 0x30D9=0x06），不代表当前模式。
#
# 复现（板端，sudo）：
#   ssh board 'sudo python3 -' < docs/evidence/reports/M1-2/i2c_reg_readback.py
# 取流期间执行（另开一路）：
#   ssh board 'nohup sudo v4l2-ctl -d /dev/video11 --stream-mmap=4 --stream-count=900 >/tmp/st.log 2>&1 &'
#   # 等 2~3 s（确认 dmesg 出现 `s_stream: 1.`）后再跑本脚本
#
# 参数：I2C_BUS / I2C_ADDR 在下方常量（板上 sensor = i2c-1 / 0x1a，节点名 `dcphy0-imx415@1a`）。

import ctypes
import errno
import fcntl
import os

I2C_BUS = "/dev/i2c-1"
I2C_ADDR = 0x1A          # 7-bit 地址（板上 DT：dcphy0-imx415@1a）
I2C_RDWR = 0x0707        # <linux/i2c-dev.h>
I2C_M_RD = 0x0001


class Msg(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16),
                ("flags", ctypes.c_uint16),
                ("len", ctypes.c_uint16),
                ("buf", ctypes.POINTER(ctypes.c_char))]


class Data(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(Msg)),
                ("nmsgs", ctypes.c_uint32)]


FD = os.open(I2C_BUS, os.O_RDWR)


def rd(reg):
    """读一个 16 bit 地址 / 8 bit 数据的 sensor 寄存器。"""
    w = ctypes.create_string_buffer(bytes([(reg >> 8) & 0xFF, reg & 0xFF]), 2)
    r = ctypes.create_string_buffer(1)
    m = (Msg * 2)()
    m[0] = Msg(I2C_ADDR, 0, 2, ctypes.cast(w, ctypes.POINTER(ctypes.c_char)))
    m[1] = Msg(I2C_ADDR, I2C_M_RD, 1, ctypes.cast(r, ctypes.POINTER(ctypes.c_char)))
    d = Data(ctypes.cast(m, ctypes.POINTER(Msg)), 2)
    arg = ctypes.create_string_buffer(ctypes.sizeof(Data))
    ctypes.memmove(arg, ctypes.byref(d), ctypes.sizeof(Data))
    fcntl.ioctl(FD, I2C_RDWR, arg)   # arg 以 buffer 形式传入（int 形式会触发 OverflowError）
    return r.raw[0]


# 寄存器清单（含义见 docs/evidence/M1-2-isp-mode.md §3）
REGS = [
    0x311A,          # CHIP_ID（可读性自检；板上 = 0xE0，与 dmesg "Detected imx415 id 0000e0" 一致）
    0x3000, 0x3002,  # MODE（standby/operating）/ XMSTA
    0x301C,          # WINMODE
    0x3020, 0x3021, 0x3022,   # HADD / VADD / ADDMODE —— 本项的关键判据
    0x3024, 0x3025, 0x3026,   # VMAX[7:0]/[15:8]/[19:16]
    0x3028, 0x3029,           # HMAX[7:0]/[15:8]
    0x3031, 0x3033,           # ADBIT in / SYS_MODE
    0x30D9, 0x30DA,           # DIG_CLP_VSTART / DIG_CLP_VNUM（binning 2/2 专有）
    0x3050, 0x3051,           # SHR0[7:0]/[15:8]（曝光）
    0x3040, 0x3041,           # 窗口位置（本项未用于判定）
]

if __name__ == "__main__":
    # label 用于区分两种工况（决定读出值是否有效）：
    #   "IDLE (no stream)"         —— 待机态：读到的是全局默认值，**不能**用于模式判定
    #   "STREAMING (s_stream=1)"   —— 取流中：模式寄存器组已由驱动写入，**这才是判定依据**
    import sys
    label = sys.argv[1] if len(sys.argv) > 1 else "IDLE (no stream)"
    print("=== %s @ %s 0x%02x ===" % (label, I2C_BUS, I2C_ADDR))
    for reg in REGS:
        try:
            print("0x%04X = 0x%02X" % (reg, rd(reg)))
        except OSError as e:
            print("0x%04X = ERR %s" % (reg, errno.errorcode.get(e.errno, e)))