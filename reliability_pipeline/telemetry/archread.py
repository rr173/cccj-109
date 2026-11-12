"""TPLA 归档读取器：打开校验、完整解码、带块选择性的范围查询、摘要。

解码只依赖归档文件本身（不回查数据库）::

    dec(t) = y0 + slope * float(t - t0)

其中 ``(t0, y0, slope)`` 为所属线段部件保存的模型参数，``t`` 为该点的
精确时刻（i64，先整数相减再转浮点，巨大 Unix 纳秒基数下 1 纳秒间隔
不丢失）。

损坏/截断检测
-------------
- 打开时校验文件头（魔数/版本/CRC）、文件尾（尾魔数/CRC）、块索引与
  组目录（各自 CRC）；任一不符 → ``ArchiveError``；
- 每读一个数据块都校验块魔数、声明长度与实际可读字节、整块 CRC32；
  不符 → ``ArchiveError``，指明块号与原因——绝不把部分结果当完整结果。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, List, Optional, Tuple

from . import archfmt as F


@dataclass(frozen=True)
class GroupInfo:
    group_id: int
    boot_seq: int
    layout_id: int
    unit: str
    points: int
    segments: int
    param_bytes: int
    block_start: int
    block_count: int


@dataclass(frozen=True)
class DecodedRow:
    """一个解码样本点：定位键 + 精确时刻 + 近似物理值 + 单位 + 所属线段。"""

    group_id: int
    boot_seq: int
    layout_id: int
    unit: str
    ts_ns: int
    value: float
    file_id: int
    frame_offset: int
    ordinal: int
    seg_seq: int


@dataclass(frozen=True)
class QueryResult:
    rows: Tuple[DecodedRow, ...]
    blocks_read: int
    blocks_total: int


@dataclass(frozen=True)
class ArchiveSummary:
    """归档摘要：各组点数/线段数/参数区字节 + 全文件口径说明。"""

    device: int
    channel: int
    epsilon: float
    t_start_ns: int
    t_end_ns: int
    groups: Tuple[GroupInfo, ...]
    points: int
    segments: int
    parts: int
    param_bytes: int          # 线段模型参数区总字节（24B × 部件数）
    point_bytes: int          # 逐点时刻+定位键总字节（22B × 点数，不计入压缩收益）
    blocks: int
    block_overhead_bytes: int # 块头+块 CRC+部件索引字段
    meta_bytes: int           # 文件头+索引+目录+文件尾
    file_bytes: int

    @property
    def raw_value_bytes(self) -> int:
        """对照基线：若逐点原样存 f64 物理值所需字节。"""
        return 8 * self.points

    @property
    def value_param_ratio(self) -> Optional[float]:
        """参数区字节 / 原始值字节（<1 才算真正压掉了物理值）。"""
        if self.raw_value_bytes == 0:
            return None
        return self.param_bytes / self.raw_value_bytes


class ArchiveReader:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self._fh: BinaryIO = open(self.path, "rb")
        except FileNotFoundError as e:
            raise F.ArchiveError(f"归档文件不存在：{self.path}") from e
        try:
            self._size = os.fstat(self._fh.fileno()).st_size
            self._read_header()
            self._read_footer()
            self._read_index()
            self._read_dir()
        except Exception:
            self._fh.close()
            raise

    # ---- 打开时的结构校验 ----------------------------------------------

    def _read_exact(self, off: int, n: int, what: str) -> bytes:
        self._fh.seek(off)
        data = self._fh.read(n)
        if len(data) != n:
            raise F.ArchiveError(
                f"归档被截断：读取{what}（偏移 {off}，{n} 字节）"
                f"只得到 {len(data)} 字节")
        return data

    def _read_header(self) -> None:
        if self._size < F.HEADER_LEN + F.FOOTER_LEN:
            raise F.ArchiveError(
                f"归档被截断：文件仅 {self._size} 字节，"
                f"不足文件头+文件尾（{F.HEADER_LEN + F.FOOTER_LEN} 字节）")
        raw = self._read_exact(0, F.HEADER_LEN, "文件头")
        (magic, version, _flags, device, channel, eps, t0, t1,
         gcount, bcount, _created, crc) = F.HEADER.unpack(raw)
        if magic != F.MAGIC:
            raise F.ArchiveError(f"文件头魔数不符：{magic!r}，不是 TPLA 归档")
        if version != F.VERSION:
            raise F.ArchiveError(f"不支持的归档版本：{version}")
        if F.crc32(raw[:56]) != crc:
            raise F.ArchiveError("文件头 CRC 校验不符：文件头已损坏")
        self.device = device
        self.channel = channel
        self.epsilon = eps
        self.t_start = t0
        self.t_end = t1
        self.group_count = gcount
        self.block_count = bcount

    def _read_footer(self) -> None:
        raw = self._read_exact(self._size - F.FOOTER_LEN, F.FOOTER_LEN, "文件尾")
        index_off, index_len, dir_off, dir_len, crc, tail = F.FOOTER.unpack(raw)
        if tail != F.TAIL_MAGIC:
            raise F.ArchiveError("文件尾魔数不符：归档被截断或已损坏")
        if F.crc32(raw[:24]) != crc:
            raise F.ArchiveError("文件尾 CRC 校验不符：归档已损坏")
        for name, off, ln in (("块索引", index_off, index_len),
                              ("组目录", dir_off, dir_len)):
            if off < F.HEADER_LEN or off + ln > self._size - F.FOOTER_LEN:
                raise F.ArchiveError(
                    f"{name}位置非法（偏移 {off}，长度 {ln}）：归档已损坏")
        self._index_off, self._index_len = index_off, index_len
        self._dir_off, self._dir_len = dir_off, dir_len

    def _read_index(self) -> None:
        raw = self._read_exact(self._index_off, self._index_len, "块索引")
        if len(raw) < F.INDEX_HEAD.size + 4:
            raise F.ArchiveError("块索引过短：归档已损坏")
        body, crc_raw = raw[:-4], raw[-4:]
        if F.crc32(body) != int.from_bytes(crc_raw, "little"):
            raise F.ArchiveError("块索引 CRC 校验不符：归档已损坏")
        magic, count = F.INDEX_HEAD.unpack(body[:F.INDEX_HEAD.size])
        if magic != F.INDEX_MAGIC:
            raise F.ArchiveError("块索引魔数不符：归档已损坏")
        expect = F.INDEX_HEAD.size + count * F.INDEX_ENTRY_LEN
        if len(body) != expect:
            raise F.ArchiveError(
                f"块索引长度不符：声明 {count} 项应为 {expect} 字节，"
                f"实际 {len(body)} 字节")
        self._index: List[Tuple[int, int, int, int, int]] = []
        pos = F.INDEX_HEAD.size
        for _ in range(count):
            gid, t_min, t_max, off, blen = F.INDEX_ENTRY.unpack(
                body[pos:pos + F.INDEX_ENTRY_LEN])
            self._index.append((gid, t_min, t_max, off, blen))
            pos += F.INDEX_ENTRY_LEN
        if count != self.block_count:
            raise F.ArchiveError(
                f"块索引项数 {count} 与文件头声明的块数 {self.block_count} 不符")

    def _read_dir(self) -> None:
        raw = self._read_exact(self._dir_off, self._dir_len, "组目录")
        if len(raw) < F.DIR_HEAD.size + 4:
            raise F.ArchiveError("组目录过短：归档已损坏")
        body, crc_raw = raw[:-4], raw[-4:]
        if F.crc32(body) != int.from_bytes(crc_raw, "little"):
            raise F.ArchiveError("组目录 CRC 校验不符：归档已损坏")
        magic, count = F.DIR_HEAD.unpack(body[:F.DIR_HEAD.size])
        if magic != F.DIR_MAGIC:
            raise F.ArchiveError("组目录魔数不符：归档已损坏")
        groups: List[GroupInfo] = []
        pos = F.DIR_HEAD.size
        for _ in range(count):
            if pos + F.DIR_ENTRY_FIXED.size > len(body):
                raise F.ArchiveError("组目录被截断：条目越界")
            gid, boot, layout, ulen, _fl = F.DIR_ENTRY_FIXED.unpack(
                body[pos:pos + F.DIR_ENTRY_FIXED.size])
            pos += F.DIR_ENTRY_FIXED.size
            if pos + ulen + F.DIR_ENTRY_TAIL.size > len(body):
                raise F.ArchiveError("组目录被截断：单位名或统计尾越界")
            unit = body[pos:pos + ulen].decode("utf-8", errors="strict")
            pos += ulen
            points, segs, pbytes, bstart, bcount = F.DIR_ENTRY_TAIL.unpack(
                body[pos:pos + F.DIR_ENTRY_TAIL.size])
            pos += F.DIR_ENTRY_TAIL.size
            groups.append(GroupInfo(gid, boot, layout, unit, points, segs,
                                    pbytes, bstart, bcount))
        if pos != len(body):
            raise F.ArchiveError("组目录尾部有多余字节：归档已损坏")
        if count != self.group_count:
            raise F.ArchiveError(
                f"组目录项数 {count} 与文件头声明的组数 {self.group_count} 不符")
        self._groups = tuple(groups)

    # ---- 公开属性 --------------------------------------------------------

    @property
    def groups(self) -> Tuple[GroupInfo, ...]:
        return self._groups

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "ArchiveReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 数据块读取与解码 ------------------------------------------------

    def _read_block(self, entry_index: int) -> Tuple[List, List]:
        """读取并校验一个数据块，返回 (parts, points)。"""
        gid, t_min, t_max, off, blen = self._index[entry_index]
        if off < F.HEADER_LEN or off + blen > self._size - F.FOOTER_LEN:
            raise F.ArchiveError(
                f"块 #{entry_index} 位置非法（偏移 {off}，长度 {blen}）："
                f"归档已损坏")
        raw = self._read_exact(off, blen, f"块 #{entry_index}")
        if len(raw) < F.BLOCK_HEAD_LEN + 4:
            raise F.ArchiveError(f"块 #{entry_index} 过短：归档已损坏")
        body, crc_raw = raw[:-4], raw[-4:]
        if F.crc32(body) != int.from_bytes(crc_raw, "little"):
            raise F.ArchiveError(
                f"块 #{entry_index} CRC 校验不符：数据块已损坏")
        (magic, block_len, bgid, n_points, n_parts, _flags,
         bt_min, bt_max) = F.BLOCK_HEAD.unpack(body[:F.BLOCK_HEAD_LEN])
        if magic != F.BLOCK_MAGIC:
            raise F.ArchiveError(f"块 #{entry_index} 魔数不符：归档已损坏")
        if block_len != blen:
            raise F.ArchiveError(
                f"块 #{entry_index} 长度声明不符：索引 {blen}，块头 {block_len}")
        if (bgid, bt_min, bt_max) != (gid, t_min, t_max):
            raise F.ArchiveError(
                f"块 #{entry_index} 块头与索引不一致：归档已损坏")
        expect = (F.BLOCK_HEAD_LEN + n_parts * F.PART_LEN
                  + n_points * F.POINT_LEN + 4)
        if expect != blen:
            raise F.ArchiveError(
                f"块 #{entry_index} 内容长度不符：应为 {expect}，实际 {blen}")
        parts = []
        pos = F.BLOCK_HEAD_LEN
        for _ in range(n_parts):
            seg, start, count, _r, t0, y0, slope = F.PART.unpack(
                body[pos:pos + F.PART_LEN])
            parts.append((seg, start, count, t0, y0, slope))
            pos += F.PART_LEN
        points = []
        for _ in range(n_points):
            ts, fid, foff, od = F.POINT.unpack(body[pos:pos + F.POINT_LEN])
            points.append((ts, fid, foff, od))
            pos += F.POINT_LEN
        # 部件必须恰好无缝覆盖块内全部点
        covered = 0
        for seg, start, count, _t0, _y0, _s in parts:
            if start != covered:
                raise F.ArchiveError(
                    f"块 #{entry_index} 线段部件未按序无缝覆盖点列：归档已损坏")
            covered += count
        if covered != n_points:
            raise F.ArchiveError(
                f"块 #{entry_index} 线段部件覆盖 {covered} 点，"
                f"与块内点数 {n_points} 不符")
        return parts, points

    def _decode_block(self, entry_index: int,
                      ginfo: GroupInfo) -> Iterator[DecodedRow]:
        parts, points = self._read_block(entry_index)
        ipart = 0
        seg, start, count, t0, y0, slope = parts[ipart]
        for i, (ts, fid, foff, od) in enumerate(points):
            while i >= start + count:
                ipart += 1
                seg, start, count, t0, y0, slope = parts[ipart]
            value = y0 + slope * float(ts - t0)   # 解码公式（与构建器一致）
            yield DecodedRow(ginfo.group_id, ginfo.boot_seq, ginfo.layout_id,
                             ginfo.unit, ts, value, fid, foff, od, seg)

    def decode(self, group_id: Optional[int] = None) -> Iterator[DecodedRow]:
        """完整解码（可选单组）；读取涉及的全部数据块并逐块校验。"""
        for g in self._groups:
            if group_id is not None and g.group_id != group_id:
                continue
            for b in range(g.block_start, g.block_start + g.block_count):
                yield from self._decode_block(b, g)

    def query(self, t_lo: int, t_hi: int,
              group_id: Optional[int] = None) -> QueryResult:
        """时间范围查询 ``[t_lo, t_hi)``：只读取时间窗重叠的块。

        块选择规则：``t_min < t_hi 且 t_max >= t_lo``。相邻块边界上的
        重复时刻会让两个块都被选中，各块再按点过滤，不漏不重。
        """
        if not (isinstance(t_lo, int) and isinstance(t_hi, int) and t_lo < t_hi):
            raise ValueError(f"查询范围非法：[{t_lo!r}, {t_hi!r})")
        rows: List[DecodedRow] = []
        blocks_read = 0
        for g in self._groups:
            if group_id is not None and g.group_id != group_id:
                continue
            for b in range(g.block_start, g.block_start + g.block_count):
                _gid, t_min, t_max, _off, _len = self._index[b]
                if t_min < t_hi and t_max >= t_lo:
                    blocks_read += 1
                    for row in self._decode_block(b, g):
                        if t_lo <= row.ts_ns < t_hi:
                            rows.append(row)
        return QueryResult(tuple(rows), blocks_read, self.block_count)

    # ---- 摘要 ------------------------------------------------------------

    def summary(self) -> ArchiveSummary:
        points = sum(g.points for g in self._groups)
        param_bytes = sum(g.param_bytes for g in self._groups)
        n_parts = param_bytes // F.PARAM_BYTES_PER_PART
        block_overhead = (self.block_count * (F.BLOCK_HEAD_LEN + 4)
                          + n_parts * (F.PART_LEN - F.PARAM_BYTES_PER_PART))
        meta = (F.HEADER_LEN + F.FOOTER_LEN + self._index_len + self._dir_len)
        return ArchiveSummary(
            device=self.device, channel=self.channel, epsilon=self.epsilon,
            t_start_ns=self.t_start, t_end_ns=self.t_end,
            groups=self._groups, points=points,
            segments=sum(g.segments for g in self._groups),
            parts=n_parts, param_bytes=param_bytes,
            point_bytes=F.POINT_LEN * points,
            blocks=self.block_count,
            block_overhead_bytes=block_overhead,
            meta_bytes=meta,
            file_bytes=self._size)
