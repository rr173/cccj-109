"""读取真实测量并按锚点组映射到参考钟。

- 数据库以 **read_only** 打开：本工具不建表、不写回、不改原始时间；
- 测量按稳定次序 **分批** 读取（keyset 分页，默认每批 4096 行），整库不会
  一次性进入 Python；锚点数量通常远小于测量，可完整驻留内存；
- 同一条测量的 ``(file_id, frame_offset, ordinal)`` 是稳定定位键，重复时间
  的不同测量全部保留，不合并、不排序到"最近一条"。

稳定次序键：``(device_id, boot_seq, local_ns, file_id, frame_offset, ordinal,
channel)``——同一测量帧的多个通道靠 ``ordinal`` 区分；不同来源的同刻测量
靠 ``file_id/frame_offset`` 区分，读取顺序在数据库和内存中一致。
"""

from __future__ import annotations

import duckdb
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Tuple

from .anchors import AnchorSet
from .mapping import Mapping, OutOfRange

DEFAULT_BATCH = 4096

ORDER_KEY = ("device_id", "boot_seq", "ts_ns", "file_id", "frame_offset",
             "ordinal", "channel")


@dataclass(frozen=True)
class SampleRow:
    file_id: int
    frame_offset: int
    device_id: int
    boot_seq: int
    layout_id: int
    channel: int
    tick: int
    cum_tick: int
    ts_ns: int
    ordinal: int
    raw_value: float
    value: float
    unit: str


@dataclass(frozen=True)
class RowRef:
    """原测量定位键。"""

    file_id: int
    frame_offset: int
    ordinal: int
    channel: int

    def text(self) -> str:
        return f"{self.file_id}:{self.frame_offset}:{self.ordinal}:{self.channel}"


@dataclass(frozen=True)
class AlignedRow:
    ref: RowRef
    device_id: int
    boot_seq: int
    layout_id: int
    ts_ns: int
    value: float
    unit: str
    correction: object                    # mapping.MappingResult
        # 未对齐时为 None，原因在 ``reason``
    reason: Optional[str]
    anchors_used: Tuple[str, ...]
    mode: Optional[str]

    @property
    def aligned(self) -> bool:
        return self.reason is None

    @property
    def corrected_ns(self) -> Optional[int]:
        return None if self.reason else self.correction.rounded()[0]

    @property
    def lower_ns(self) -> Optional[int]:
        return None if self.reason else self.correction.rounded()[1]

    @property
    def upper_ns(self) -> Optional[int]:
        return None if self.reason else self.correction.rounded()[2]

    def within_window(self, low: int, high: int) -> Optional[bool]:
        """校正范围 [lower, upper] 是否与闭窗口 [low, high] 相交。

        未对齐返回 None（无法判断，不是"窗外"）。比较在有理数上进行。
        """
        if self.reason:
            return None
        return self.correction.lower <= high and self.correction.upper >= low


def connect_read_only(db_path: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db_path, read_only=True)
    tables = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()}
    if "tele_samples" not in tables:
        con.close()
        raise AlignDbError(f"{db_path} 中没有 tele_samples 表，无法读取测量")
    return con


class AlignDbError(Exception):
    pass


_SELECT = ("SELECT file_id, frame_offset, device_id, boot_seq, layout_id, "
           "channel, tick, cum_tick, ts_ns, ordinal, raw_value, value, unit "
           "FROM tele_samples")
_ROW_KEY = "(device_id, boot_seq, ts_ns, file_id, frame_offset, ordinal, channel)"


def iter_samples(con: duckdb.DuckDBPyConnection,
                 batch: int = DEFAULT_BATCH,
                 device_id: Optional[int] = None,
                 boot_seq: Optional[int] = None,
                 local_lo: Optional[int] = None,
                 local_hi: Optional[int] = None,
                 ) -> Iterator[List[SampleRow]]:
    """按稳定次序分批产出测量；不会把全库一次读入内存。"""
    if batch <= 0:
        raise ValueError("batch 必须为正整数")
    where = []
    base_params: list = []
    if device_id is not None:
        where.append("device_id = ?")
        base_params.append(device_id)
    if boot_seq is not None:
        where.append("boot_seq = ?")
        base_params.append(boot_seq)
    if local_lo is not None:
        where.append("ts_ns >= ?")
        base_params.append(local_lo)
    if local_hi is not None:
        where.append("ts_ns <= ?")
        base_params.append(local_hi)
    base_where = (" WHERE " + " AND ".join(where)) if where else ""
    last = None
    while True:
        if last is None:
            sql = (_SELECT + base_where + " ORDER BY " + ", ".join(ORDER_KEY)
                   + " LIMIT ?")
            params = list(base_params) + [batch]
        else:
            sep = " AND " if base_where else " WHERE "
            sql = (_SELECT + base_where + sep + _ROW_KEY + " > (?, ?, ?, ?, ?, ?, ?)"
                   + " ORDER BY " + ", ".join(ORDER_KEY) + " LIMIT ?")
            params = list(base_params) + list(last) + [batch]
        rows = con.execute(sql, params).fetchall()
        if not rows:
            return
        chunk = [SampleRow(*r) for r in rows]
        yield chunk
        if len(chunk) < batch:
            return
        last = (chunk[-1].device_id, chunk[-1].boot_seq, chunk[-1].ts_ns,
                chunk[-1].file_id, chunk[-1].frame_offset, chunk[-1].ordinal,
                chunk[-1].channel)


def align_rows(rows: Sequence[SampleRow], anchors: AnchorSet,
               mappings: dict, max_extrapolation_ns: int) -> List[AlignedRow]:
    """把一批测量映射到参考钟；不可对齐的行带明确原因。"""
    out: List[AlignedRow] = []
    for r in rows:
        ref = RowRef(r.file_id, r.frame_offset, r.ordinal, r.channel)
        g = anchors.get(r.device_id, r.boot_seq)
        mapping = mappings.get((r.device_id, r.boot_seq))
        if g is None:
            reason = (f"设备 {r.device_id} 启动周期 {r.boot_seq} 没有锚点组，"
                      "无法映射到参考钟")
            out.append(AlignedRow(ref, r.device_id, r.boot_seq, r.layout_id,
                                  r.ts_ns, r.value, r.unit, None, reason, (), None))
            continue
        if mapping is None:
            reason = f"设备 {r.device_id} 启动周期 {r.boot_seq} 的锚点组无效：" \
                     f"{g.invalid_reason}"
            out.append(AlignedRow(ref, r.device_id, r.boot_seq, r.layout_id,
                                  r.ts_ns, r.value, r.unit, None, reason, (), None))
            continue
        try:
            res = mapping.evaluate(r.ts_ns, max_extrapolation_ns)
        except OutOfRange as e:
            out.append(AlignedRow(ref, r.device_id, r.boot_seq, r.layout_id,
                                  r.ts_ns, r.value, r.unit, None, str(e), (), None))
            continue
        ids = tuple(mapping.anchor_ids_for_segment(res.segment))
        out.append(AlignedRow(ref, r.device_id, r.boot_seq, r.layout_id,
                              r.ts_ns, r.value, r.unit, res, None, ids,
                              res.mode))
    return out
