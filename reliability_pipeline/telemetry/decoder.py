"""流式帧解码器：任意大小分块喂入，跨块半帧保留状态，损坏后逐字节重同步。

工作方式
--------
内部维护一个字节缓冲与它的绝对文件偏移 ``base``。每次 ``feed`` 后尽可能多地
产出事件：

1. 在缓冲中找帧头魔数；魔数之前的字节是垃圾，直接丢弃（静默消费）；
2. 缓冲中魔数已现但不足 7 字节（kind+len 未齐）→ 等待更多数据；
3. kind 非法或 payload_len>4096 → 记一条 ``Anomaly``，从魔数后一字节重新扫描；
4. 完整帧（11+payload_len 字节）尚未到齐 → 等待更多数据（截断尾帧的待续状态）；
5. CRC 不符 → 记 ``Anomaly``，从魔数后一字节重新扫描；
6. CRC 正确 → 产出 ``Frame``，整帧消费——payload 内即使出现魔数字节也不会
   被误当边界，因为只有 CRC 校验通过的候选才被当作帧。

解码结果只取决于字节流本身，与分块大小无关：换不同的读取块长，产出的事件
序列完全相同。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Union

from . import protocol as P


@dataclass(frozen=True)
class Frame:
    """一个 CRC 校验通过的完整帧。"""

    kind: int
    payload: bytes
    offset: int            # 帧起始（魔数首字节）在源文件中的绝对偏移
    raw: bytes             # 完整帧字节（含帧头与 CRC）


@dataclass(frozen=True)
class Anomaly:
    """候选帧非法（kind/length 越界或 CRC 不符），已按逐字节重同步跳过。"""

    offset: int            # 候选帧头（魔数首字节）的绝对偏移
    reason: str            # invalid_kind / invalid_length / crc_mismatch
    detail: str


Event = Union[Frame, Anomaly]


class StreamDecoder:
    """可增量喂入的帧解码器。"""

    def __init__(self, base: int = 0) -> None:
        self._buf = bytearray()
        self.base = base   # _buf[0] 的绝对偏移

    @property
    def pending_bytes(self) -> int:
        """尚未消费的字节数（半帧或未决候选）。"""
        return len(self._buf)

    @property
    def buffer(self) -> bytes:
        return bytes(self._buf)

    def feed(self, data: bytes) -> List[Event]:
        if data:
            self._buf += data
        return self._drain()

    def _drain(self) -> List[Event]:
        events: List[Event] = []
        buf = self._buf
        n = len(buf)
        pos = 0                       # 扫描游标；pos 之前的字节本轮将被消费
        while True:
            i = buf.find(P.MAGIC, pos)
            if i < 0:
                # 没有帧头：未消费区域的末尾 3 字节可能是跨块的半个魔数，
                # 保留；其余为垃圾（pos 之前本就已消费，不受影响）
                pos = max(pos, n - 3)
                break
            if i + 7 > n:
                pos = i               # kind/len 未齐：i 之前是垃圾，等待剩余
                break
            kind = buf[i + 4]
            plen = buf[i + 5] | (buf[i + 6] << 8)
            if kind not in P.VALID_KINDS:
                events.append(Anomaly(self.base + i, "invalid_kind",
                                      f"kind={kind} 不在 {{1,2,3}}"))
                pos = i + 1
                continue
            if plen > P.MAX_PAYLOAD:
                events.append(Anomaly(self.base + i, "invalid_length",
                                      f"payload_len={plen} 超过 {P.MAX_PAYLOAD}"))
                pos = i + 1
                continue
            frame_len = P.FRAME_FIXED + plen
            if i + frame_len > n:
                pos = i               # 半帧：等待补齐
                break
            raw = bytes(buf[i:i + frame_len])
            stored = int.from_bytes(raw[-4:], "little")
            if stored != P.crc32(raw[4:-4]):
                events.append(Anomaly(self.base + i, "crc_mismatch",
                                      f"kind={kind} payload_len={plen} CRC 校验不符"))
                pos = i + 1
                continue
            events.append(Frame(kind, raw[7:-4], self.base + i, raw))
            pos = i + frame_len
        if pos:
            del self._buf[:pos]
            self.base += pos
        return events
