"""种子可复现的场景化包生成器。

每个场景产出确定性的字节流（同一 seed 字节级一致），用于演示与回归：
``python3 -m telemetry.cli gen --out DIR --scenario all --seed 1``

场景
----
- ``mixed``       单设备三通道（int16 毫摄氏度 / int32 Pa / float32 毫米每秒）
- ``two-devices`` 两台设备交错，各自独立的启动周期与 tick 序列
- ``wrap``        tick 从 2^32-1 回绕到 0，随后旧包拒收
- ``retransmit``  相同/冲突布局重声明、相同/冲突测量包重传
- ``corrupt``     垃圾字节、CRC 损坏帧、payload 内嵌帧头字节后的重新同步
- ``tail``        末帧缺尾 3 字节（两个文件：part1 + 待补齐的 part2）
- ``crash``       大流量双设备流（供断点续传/崩溃恢复压测，--count 控制包数）
"""

from __future__ import annotations

import random
import struct
from typing import Dict, List, Sequence, Tuple

from . import protocol as P

# 通道描述：(channel, raw_type, gain, bias, unit)
ChannelTuple = Tuple[int, int, float, float, int]

MIXED_LAYOUT: List[ChannelTuple] = [
    (0, 1, 0.5, 1.0, 1),        # int16  毫摄氏度 -> °C
    (1, 2, 2.0, -3.0, 2),       # int32  Pa       -> kPa
    (2, 3, 1.0, 0.0, 3),        # float32 毫米每秒 -> m/s
]


class StreamBuilder:
    """按声明顺序拼装帧流；``meas`` 依据已声明布局打包原始值。"""

    def __init__(self) -> None:
        self._parts: List[bytes] = []
        self._layouts: Dict[Tuple[int, int], List[ChannelTuple]] = {}

    # -- 帧构造 ------------------------------------------------------
    def frame(self, kind: int, payload: bytes) -> bytes:
        data = P.build_frame(kind, payload)
        self._parts.append(data)
        return data

    def boot(self, device: int, boot_seq: int, epoch_ns: int, tick_hz: int) -> bytes:
        return self.frame(P.KIND_BOOT, struct.pack("<HIqI", device, boot_seq,
                                                   epoch_ns, tick_hz))

    def layout(self, device: int, layout_id: int,
               channels: Sequence[ChannelTuple]) -> bytes:
        payload = struct.pack("<HHB", device, layout_id, len(channels))
        for ch, raw_type, gain, bias, unit in channels:
            payload += struct.pack("<BBddB", ch, raw_type, gain, bias, unit)
        self._layouts[(device, layout_id)] = list(channels)
        return self.frame(P.KIND_LAYOUT, payload)

    def meas(self, device: int, layout_id: int, tick: int,
             values: Sequence[float]) -> bytes:
        channels = self._layouts[(device, layout_id)]
        assert len(values) == len(channels), "值个数须与通道数一致"
        payload = struct.pack("<HHI", device, layout_id, tick & 0xFFFFFFFF)
        for (ch, raw_type, gain, bias, unit), v in zip(channels, values):
            payload += struct.pack(P.RAW_TYPES[raw_type][0], v)
        return self.frame(P.KIND_MEASUREMENT, payload)

    # -- 非帧内容 ----------------------------------------------------
    def raw(self, data: bytes) -> "StreamBuilder":
        self._parts.append(data)
        return self

    def garbage(self, n: int, seed: int) -> "StreamBuilder":
        rng = random.Random(seed)
        self._parts.append(rng.randbytes(n))
        return self

    def corrupt_crc(self, frame: bytes) -> "StreamBuilder":
        """把一个好帧的 CRC 末字节取反后追加（模拟 CRC 损坏帧）。"""
        bad = frame[:-1] + bytes([frame[-1] ^ 0xFF])
        self._parts.append(bad)
        return self

    def bytes(self) -> bytes:
        return b"".join(self._parts)


# ----------------------------------------------------------------------
# 场景
# ----------------------------------------------------------------------

EPOCH0 = 1_700_000_000_000_000_000   # 2023-11-14T22:13:20Z


def _mixed(b: StreamBuilder, seed: int) -> None:
    b.boot(7, 1, EPOCH0, 1000)
    b.layout(7, 5, MIXED_LAYOUT)
    b.meas(7, 5, 0, [100, 1000, 1.5])
    b.meas(7, 5, 500, [234, -1500, 12.5])
    b.meas(7, 5, 1000, [-40, 200000, -0.25])


def _two_devices(b: StreamBuilder, seed: int) -> None:
    # 设备号与其他场景错开（同一库内设备时间线是按设备全局推进的）
    b.boot(17, 1, EPOCH0, 1000)
    b.boot(19, 3, EPOCH0 + 5_000_000_000, 250)
    b.layout(17, 5, MIXED_LAYOUT)
    b.layout(19, 1, [(0, 3, 1.0, 0.0, 3), (4, 1, 0.25, 0.0, 1)])
    for i in range(6):
        b.meas(17, 5, i * 100, [10 * i, 100 * i, 0.5 * i])
        b.meas(19, 1, i * 40, [2.5 * i, 8 + i])


def _wrap(b: StreamBuilder, seed: int) -> None:
    b.boot(3, 1, EPOCH0, 10_000_000)
    b.layout(3, 2, [(0, 1, 1.0, 0.0, 1)])
    b.meas(3, 2, 0xFFFFFFFD, [1])
    b.meas(3, 2, 0xFFFFFFFF, [2])
    b.meas(3, 2, 0, [3])                    # 回绕：累计刻度 2^32
    b.meas(3, 2, 1, [4])
    b.meas(3, 2, 0xFFFFFFFE, [9])           # 旧包：前进差 >= 2^31
    b.meas(3, 2, 1, [4])                    # 重传：相同内容，不重复入库
    b.meas(3, 2, 2, [5])


def _retransmit(b: StreamBuilder, seed: int) -> None:
    b.boot(5, 1, EPOCH0, 1000)
    f_layout = b.layout(5, 8, MIXED_LAYOUT)
    b._parts.append(f_layout)               # 完全相同声明：重复，不生效也不报错
    b.layout(5, 8, [(0, 1, 9.9, 0.0, 1),    # 同 id 不同声明：冲突拒收
                    (1, 2, 2.0, -3.0, 2), (2, 3, 1.0, 0.0, 3)])
    f_meas = b.meas(5, 8, 100, [1, 2, 3.0])
    b._parts.append(f_meas)                 # 相同包重传：不重复入库
    b.meas(5, 8, 100, [1, 2, 4.0])          # 同 tick 内容冲突：拒收
    b.meas(5, 8, 101, [7, 8, 9.0])          # 正常前进


def _corrupt(b: StreamBuilder, seed: int) -> None:
    b.boot(2, 1, EPOCH0, 1000)
    b.layout(2, 4, [(0, 2, 1.0, 0.0, 2), (1, 3, 1.0, 0.0, 3)])
    b.meas(2, 4, 10, [100, 1.0])
    b.garbage(17, seed)                     # 随机垃圾
    b.raw(b"\xa7\x5c")                      # 半个魔数
    bad = b.meas(2, 4, 20, [200, 2.0])
    b._parts.pop()
    b.corrupt_crc(bad)                      # CRC 损坏帧：拒收并重同步
    b.meas(2, 4, 30, [0x09E15CA7, 3.0])     # payload 内嵌帧头字节 A7 5C E1 09
    b.meas(2, 4, 40, [400, 4.0])


def _tail(parts: List[bytes]) -> None:
    b = StreamBuilder()
    b.boot(4, 1, EPOCH0, 1000)
    b.layout(4, 6, [(0, 1, 1.0, 0.0, 1)])
    b.meas(4, 6, 1, [10])
    last = b.meas(4, 6, 2, [20])
    whole = b.bytes()
    parts.append(whole[:-3])                # 末帧缺 3 字节尾
    parts.append(whole[-3:])                # 待补齐的尾巴


def _crash(b: StreamBuilder, seed: int, count: int) -> None:
    rng = random.Random(seed)
    b.boot(11, 1, EPOCH0, 1000)
    b.boot(12, 1, EPOCH0 + 1_000_000_000, 5000)
    b.layout(11, 1, MIXED_LAYOUT)
    b.layout(12, 2, [(0, 3, 0.1, -1.0, 3), (1, 1, 2.0, 0.0, 1)])
    last_frames: List[bytes] = []
    for i in range(count):
        if i % 97 == 96 and last_frames:
            b._parts.append(last_frames[-1])          # 偶发重传：不重复入库
        if i % 2 == 0:
            f = b.meas(11, 1, i // 2, [i % 500, 1000 + i, 0.25 * (i % 40)])
        else:
            f = b.meas(12, 2, i // 2, [0.5 * (i % 17), i % 900])
        last_frames.append(f)


def build_scenario(name: str, seed: int = 1, count: int = 2000) -> Dict[str, bytes]:
    """返回 {相对文件名: 内容}；同一 (name, seed, count) 字节级可复现。"""
    b = StreamBuilder()
    if name == "mixed":
        _mixed(b, seed)
        return {"mixed.bin": b.bytes()}
    if name == "two-devices":
        _two_devices(b, seed)
        return {"two_devices.bin": b.bytes()}
    if name == "wrap":
        _wrap(b, seed)
        return {"wrap.bin": b.bytes()}
    if name == "retransmit":
        _retransmit(b, seed)
        return {"retransmit.bin": b.bytes()}
    if name == "corrupt":
        _corrupt(b, seed)
        return {"corrupt.bin": b.bytes()}
    if name == "tail":
        parts: List[bytes] = []
        _tail(parts)
        return {"tail.bin.part1": parts[0], "tail.part2": parts[1]}
    if name == "crash":
        _crash(b, seed, count)
        return {"crash.bin": b.bytes()}
    raise ValueError(f"未知场景：{name!r}（可选：{', '.join(SCENARIOS)}）")


SCENARIOS = ("mixed", "two-devices", "wrap", "retransmit",
             "corrupt", "tail", "crash")
