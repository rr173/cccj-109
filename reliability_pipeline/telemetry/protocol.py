"""帧与 payload 的字节级编解码（协议常量、CRC、单位换算）。

帧格式（多字节一律小端）::

    +-----------+------+--------+---------+-----------+
    | 帧头 4B   | kind | len    | payload | crc32     |
    | A7 5C E1 09| u8  | u16    | <=4096B | u32       |
    +-----------+------+--------+---------+-----------+

``crc32`` 为 zlib.crc32，计算范围是 ``kind || len || payload``（不含帧头）。

payload 布局
------------
- kind=3 启动锚点：``device:u16, boot_seq:u32, epoch_ns:i64, tick_hz:u32``（18B）
- kind=1 布局声明：``device:u16, layout:u16, channel_count:u8``，随后
  ``channel_count`` 个通道描述 ``channel:u8, raw_type:u8, gain:f64, bias:f64, unit:u8``
- kind=2 测量包：``device:u16, layout:u16, tick:u32``，随后按声明顺序的各通道原始值

raw_type：1=int16，2=int32，3=float32。
unit：1=毫摄氏度→°C，2=Pa→kPa，3=毫米每秒→m/s（均为 ×1e-3）。
物理量 = raw*gain + bias（声明单位下），再乘单位比例得到最终单位。
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass
from typing import List, Tuple

MAGIC = b"\xA7\x5C\xE1\x09"
KIND_LAYOUT = 1
KIND_MEASUREMENT = 2
KIND_BOOT = 3
VALID_KINDS = (KIND_LAYOUT, KIND_MEASUREMENT, KIND_BOOT)

MAX_PAYLOAD = 4096
FRAME_FIXED = len(MAGIC) + 1 + 2 + 4          # 帧头+kind+len+crc = 11
MAX_FRAME = FRAME_FIXED + MAX_PAYLOAD          # 4107

BOOT_PAYLOAD_LEN = 18                          # <HIqI
LAYOUT_HEADER_LEN = 5                          # <HHB
CHANNEL_DESC_LEN = 19                          # <BBddB
MEAS_HEADER_LEN = 8                            # <HHI

# raw_type -> (struct 格式, 字节数, 是否浮点)
RAW_TYPES = {1: ("<h", 2, False), 2: ("<i", 4, False), 3: ("<f", 4, True)}

# unit -> (最终单位名, 换算比例)
UNIT_SCALE = {1: ("°C", 1e-3), 2: ("kPa", 1e-3), 3: ("m/s", 1e-3)}

TICK_MOD = 1 << 32          # 计数器模 2^32
TICK_HALF = 1 << 31         # 半圈：前进差须严格小于它
INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1


class ProtocolError(ValueError):
    """payload 结构不合法（帧本身 CRC 正确，但内容无法解释）。"""


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def build_frame(kind: int, payload: bytes) -> bytes:
    """构造一个完整帧（生成器与测试共用）。"""
    if not 0 <= kind <= 0xFF:
        raise ValueError("kind 超出 u8")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload 超过 4096 字节")
    body = bytes([kind]) + struct.pack("<H", len(payload)) + payload
    return MAGIC + body + struct.pack("<I", crc32(body))


@dataclass(frozen=True)
class BootAnchor:
    device: int
    boot_seq: int
    epoch_ns: int          # tick=0 对应的 Unix 纳秒
    tick_hz: int


@dataclass(frozen=True)
class ChannelSpec:
    ordinal: int           # 在布局声明中的顺序（测量包按此排列）
    channel: int
    raw_type: int          # 1=int16 2=int32 3=float32
    gain: float
    bias: float
    unit: int              # 1=毫摄氏度 2=Pa 3=毫米每秒

    @property
    def raw_size(self) -> int:
        return RAW_TYPES[self.raw_type][1]

    @property
    def final_unit(self) -> str:
        return UNIT_SCALE[self.unit][0]

    def convert(self, raw: float) -> float:
        """raw*gain+bias（声明单位），再换算到最终单位。"""
        return (raw * self.gain + self.bias) * UNIT_SCALE[self.unit][1]


@dataclass(frozen=True)
class LayoutDecl:
    device: int
    layout_id: int
    channels: Tuple[ChannelSpec, ...]
    decl_bytes: bytes      # 规范化声明字节（通道描述部分），用于相同/冲突判定

    @property
    def value_bytes(self) -> int:
        return sum(c.raw_size for c in self.channels)


@dataclass(frozen=True)
class MeasHeader:
    device: int
    layout_id: int
    tick: int


def parse_boot_payload(payload: bytes) -> BootAnchor:
    if len(payload) != BOOT_PAYLOAD_LEN:
        raise ProtocolError(f"启动锚点 payload 应为 {BOOT_PAYLOAD_LEN} 字节，实际 {len(payload)}")
    device, boot_seq, epoch_ns, tick_hz = struct.unpack("<HIqI", payload)
    return BootAnchor(device, boot_seq, epoch_ns, tick_hz)


def parse_layout_payload(payload: bytes) -> LayoutDecl:
    if len(payload) < LAYOUT_HEADER_LEN:
        raise ProtocolError(f"布局声明过短：{len(payload)} 字节")
    device, layout_id, count = struct.unpack("<HHB", payload[:LAYOUT_HEADER_LEN])
    expect = LAYOUT_HEADER_LEN + count * CHANNEL_DESC_LEN
    if len(payload) != expect:
        raise ProtocolError(
            f"布局声明长度不符：channel_count={count} 应为 {expect} 字节，实际 {len(payload)}")
    channels: List[ChannelSpec] = []
    seen = set()
    off = LAYOUT_HEADER_LEN
    for ordinal in range(count):
        ch, raw_type, gain, bias, unit = struct.unpack_from("<BBddB", payload, off)
        off += CHANNEL_DESC_LEN
        if raw_type not in RAW_TYPES:
            raise ProtocolError(f"通道 {ch}：未知 raw_type={raw_type}")
        if unit not in UNIT_SCALE:
            raise ProtocolError(f"通道 {ch}：未知 unit={unit}")
        if not (math.isfinite(gain) and math.isfinite(bias)):
            raise ProtocolError(f"通道 {ch}：校准参数非有限（gain={gain}, bias={bias}）")
        if ch in seen:
            raise ProtocolError(f"通道编号重复：{ch}")
        seen.add(ch)
        channels.append(ChannelSpec(ordinal, ch, raw_type, gain, bias, unit))
    return LayoutDecl(device, layout_id, tuple(channels), payload[LAYOUT_HEADER_LEN:])


def parse_meas_header(payload: bytes) -> MeasHeader:
    if len(payload) < MEAS_HEADER_LEN:
        raise ProtocolError(f"测量包过短：{len(payload)} 字节")
    device, layout_id, tick = struct.unpack("<HHI", payload[:MEAS_HEADER_LEN])
    return MeasHeader(device, layout_id, tick)


def decode_values(layout: LayoutDecl, data: bytes) -> List[float]:
    """按声明顺序解码各通道原始值；长度必须恰好吻合。

    返回原始值列表（int16/int32 为 int，float32 为 float）。
    """
    if len(data) != layout.value_bytes:
        raise ProtocolError(
            f"测量值长度不符：布局要求 {layout.value_bytes} 字节，实际 {len(data)}")
    values: List[float] = []
    off = 0
    for spec in layout.channels:
        fmt, size, _ = RAW_TYPES[spec.raw_type]
        values.append(struct.unpack_from(fmt, data, off)[0])
        off += size
    return values


def tick_forward_diff(new_tick: int, last_tick: int) -> int:
    """模 2^32 的前进差（0 .. 2^32-1）。"""
    return (new_tick - last_tick) % TICK_MOD


def sample_ts_ns(epoch_ns: int, cum_tick: int, tick_hz: int) -> int:
    """整数计算 epoch_ns + floor(累计刻度*10^9/tick_hz)，不经过浮点。"""
    return epoch_ns + (cum_tick * 1_000_000_000) // tick_hz
