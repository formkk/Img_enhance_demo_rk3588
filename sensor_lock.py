#!/usr/bin/env python3
"""sensor_lock.py —— IMX415 **1080p@30 锁定**（主基准 §9.5 ④ / §10.4 `camera.vblank_lines`）

**为什么需要它**：IMX415 的帧周期 = `VMAX` 行数 × 行时，而 `VMAX = height + vertical_blanking`。
设备树只提供 **1080p@60** 的 overlay（`board.overlay`，VMAX = 1080+1154 = 2234）——`/boot/dtb/overlay/`
下**没有** `imx415-1920x1080-30fps`；且 `/dev/video11` **既不枚举帧率也不支持 `set-parm`**（板上实测）
⇒ 锁定**只能**走 **sensor subdev 的 `V4L2_CID_VBLANK`**。

**实测语义**（证据 = `docs/evidence/M1-collect-tool-board.md` §12）：
  · `vertical_blanking = 3405` ⇒ `VMAX = 4485` ⇒ **30.0 fps**（两点法实测 600/1200 帧）；
    且 1/30 s 的曝光行数恰为 4485（行时 **7.433 µs**，见 §9.5 ④ 的 v3.79 更正）；
  · **曝光上限随 VMAX 联动**：`exposure max = VMAX + 8`（60 fps 时 2242、30 fps 时 4493）
    ⇒ 1/30 s（4485 行）在 30 fps 下**可设**，在 60 fps 下被钳到 2242；
  · **停流即复位**（回到 1154 / 60 fps）⇒ **每次启动都必须重新施加**，且**须在 `STREAMON` 之前**；
  · **必须在手动曝光档（采集档 / 演示档）下使用**：stock 档（AE auto）的帧率控制会在流中把
    vblank 反复写回 1154（板上实测 t=3 s 即被复位）。

**纪律**：
  · sensor subdev **运行时解析**——判据 = **唯一暴露 `V4L2_CID_VBLANK` 的 v4l-subdev**
    （板上实测：`v4l-subdev2` = `m00_b_imx415 1-001a`，其余 4 个 subdev 均无此控制）
    ⇒ **禁止写死 `/dev/v4l-subdevN`**（编号随设备树/枚举次序变化）；
  · **取值不内置**：目标值由调用方给出（config 的 `camera.vblank_lines`），本模块只负责施加与回读校验
    （规则 `10-evidence.md` §2）。
  · 失败一律返回 **`E_V4L2`（-1）**（主基准 §5 契约 A 的既有类别，**不新增返回码**）。

用法（板上；须对 sensor subdev 有**写权限**——本板 `/dev/v4l-subdev2` 属 `root:video` 0660，
`video` 组成员（如 `cat`）**无需 root**；无该组权限时才需 `sudo`）：

    python3 sensor_lock.py --check                # 只读：解析 subdev + 回读当前 vblank
    python3 sensor_lock.py --apply 3405           # 写入 + 回读校验（须在取流前执行；权限见上）

库用法：

    import sensor_lock
    rc = sensor_lock.apply(3405, logger=log)      # 0 = OK；-1 = E_V4L2
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import glob
import os
import struct
import sys
from typing import Optional

# ── V4L2 契约常量（取值来自 uapi/linux/v4l2-controls.h 与 videodev2.h）──────────
V4L2_CID_VBLANK = 0x009E0901          # = V4L2_CID_IMAGE_SOURCE_CLASS(0x009e0000) + 1

_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
_IOC_NRSHIFT, _IOC_TYPESHIFT = 0, 8
_IOC_SIZESHIFT = 16
_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(dir_, type_, nr, size):
    return ((dir_ << (_IOC_NRSHIFT + _IOC_NRBITS + _IOC_TYPEBITS + _IOC_SIZEBITS))
            | (ord(type_) << _IOC_TYPESHIFT) | (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT))


def _iowr(type_, nr, size):
    return _ioc(_IOC_READ | _IOC_WRITE, type_, nr, size)


# struct v4l2_queryctrl = id(4) type(4) name(32) min(4) max(4) step(4) def(4) flags(4) rsv[2](8) = 68
VIDIOC_QUERYCTRL = _iowr('V', 36, 68)
# struct v4l2_control = id(4) value(4) = 8
VIDIOC_G_CTRL = _iowr('V', 27, 8)
VIDIOC_S_CTRL = _iowr('V', 28, 8)

SUBDEV_GLOB = '/sys/class/video4linux/v4l-subdev*'

E_V4L2 = -1                            # 主基准 §5 契约 A：-1 E_V4L2


class SensorLockError(Exception):
    """锁定失败（subdev 无法解析 / 控制不存在 / 写入或回读校验不符）。"""


# ── 底层 ioctl ─────────────────────────────────────────────────────────────

def _queryctrl(fd: int, cid: int) -> Optional[tuple]:
    """存在则返回 (min, max)，不存在（EINVAL）返回 None。"""
    buf = bytearray(struct.pack('II', cid, 0) + b'\0' * 60)
    try:
        fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buf, True)
    except OSError as e:
        if e.errno in (errno.EINVAL, errno.ENOTTY):
            return None
        raise
    _id, _type = struct.unpack_from('II', buf, 0)
    minimum, maximum = struct.unpack_from('ii', buf, 40)
    return minimum, maximum


def _get_ctrl(fd: int, cid: int) -> int:
    buf = bytearray(struct.pack('Ii', cid, 0))
    fcntl.ioctl(fd, VIDIOC_G_CTRL, buf, True)
    return struct.unpack_from('i', buf, 4)[0]


def _set_ctrl(fd: int, cid: int, value: int) -> None:
    buf = bytearray(struct.pack('Ii', cid, value))
    fcntl.ioctl(fd, VIDIOC_S_CTRL, buf, True)


# ── subdev 解析 / 读 / 写 ───────────────────────────────────────────────────

def resolve(node: Optional[str] = None) -> str:
    """解析 sensor subdev 的**设备节点**（判据 = 唯一暴露 `V4L2_CID_VBLANK` 的 v4l-subdev）。

    显式传入 `node` 时直接返回（供离线演练/单测）；否则运行时枚举，**恰好 1 个**才算解析成功。
    """
    if node:
        return node
    hits = []
    for p in sorted(glob.glob(SUBDEV_GLOB)):
        dev = '/dev/' + os.path.basename(p)
        try:
            fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            if _queryctrl(fd, V4L2_CID_VBLANK) is not None:
                hits.append(dev)
        finally:
            os.close(fd)
    if len(hits) != 1:
        raise SensorLockError(
            'sensor subdev 解析失败：暴露 V4L2_CID_VBLANK 的 v4l-subdev 应恰有 1 个，实得 %d 个 %s'
            % (len(hits), hits))
    return hits[0]


def read_vblank(node: Optional[str] = None) -> int:
    """回读当前 `vertical_blanking`（只读）。"""
    dev = resolve(node)
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        return _get_ctrl(fd, V4L2_CID_VBLANK)
    finally:
        os.close(fd)


def apply(vblank_lines: int, node: Optional[str] = None, logger=None) -> int:
    """施加 1080p@30 锁定：写 `vertical_blanking` 并**回读校验**。

    **必须在 `STREAMON` 之前调用**（停流即复位，见模块头）。返回 `0` / `E_V4L2`（-1）。
    `logger` 给出时记 `capture/sensor_lock` 事件（`docs/logging.md` §4）。
    """
    def _log(result: str, **kw):
        if logger is not None:
            logger.event('capture', 'sensor_lock', result=result, code='OK' if result == 'ok'
                         else 'E_V4L2', **kw)

    try:
        dev = resolve(node)
    except SensorLockError as e:
        print('[sensor_lock] FAIL: %s' % e, file=sys.stderr)
        _log('fail', node=node or '', vblank_lines=vblank_lines, readback='')
        return E_V4L2

    try:
        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        print('[sensor_lock] FAIL: 无法打开 %s（需对该 subdev 有写权限：板上 = `video` 组成员，'
              '否则用 sudo）：%s' % (dev, e), file=sys.stderr)
        _log('fail', node=dev, vblank_lines=vblank_lines, readback='')
        return E_V4L2

    try:
        q = _queryctrl(fd, V4L2_CID_VBLANK)
        if q is None:
            print('[sensor_lock] FAIL: %s 无 V4L2_CID_VBLANK' % dev, file=sys.stderr)
            _log('fail', node=dev, vblank_lines=vblank_lines, readback='')
            return E_V4L2
        _set_ctrl(fd, V4L2_CID_VBLANK, vblank_lines)
        back = _get_ctrl(fd, V4L2_CID_VBLANK)
    except OSError as e:
        print('[sensor_lock] FAIL: %s 写/读 V4L2_CID_VBLANK 失败：%s' % (dev, e), file=sys.stderr)
        _log('fail', node=dev, vblank_lines=vblank_lines, readback='')
        return E_V4L2
    finally:
        os.close(fd)

    if back != vblank_lines:
        print('[sensor_lock] FAIL: 回读校验不符（请求 %d，回读 %d）' % (vblank_lines, back),
              file=sys.stderr)
        _log('fail', node=dev, vblank_lines=vblank_lines, readback=back)
        return E_V4L2

    print('[sensor_lock] %s vertical_blanking = %d（回读一致）⇒ VMAX = %d 行'
          % (dev, back, back + 1080))
    _log('ok', node=dev, vblank_lines=vblank_lines, readback=back)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='IMX415 1080p@30 锁定（sensor subdev vertical_blanking）')
    ap.add_argument('--check', action='store_true', help='只读：解析 subdev 并回读当前值')
    ap.add_argument('--apply', type=int, metavar='LINES',
                    help='写入 vertical_blanking（须在取流前执行；需对该 subdev 有写权限）')
    ap.add_argument('--node', default=None, help='显式指定 subdev（调试用；默认运行时解析）')
    a = ap.parse_args()

    if a.apply is None and not a.check:
        ap.error('需给出 --check 或 --apply LINES')
    try:
        if a.check:
            dev = resolve(a.node)
            print('%s vertical_blanking = %d' % (dev, read_vblank(a.node)))
        if a.apply is not None:
            return 0 if apply(a.apply, a.node) == 0 else 1   # CLI 出口码：0 / 1
    except SensorLockError as e:
        print('[sensor_lock] FAIL: %s' % e, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
