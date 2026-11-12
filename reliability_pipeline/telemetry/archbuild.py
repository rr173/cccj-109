"""流式构建折线压缩归档：有序游标逐批扫描 + 每组 O(1) 状态的在线分段。

分段模型与切段判据
------------------
每段保存模型 ``(t0, y0, slope)``，解码公式（解码器与本构建器逐指令一致）::

    dec(t) = y0 + slope * float(t - t0)        # 先整数差，再转浮点，再乘加

- 段以第一个点 ``(t0, v0)`` 开头，``y0 = v0``（f64 原样，首点误差恒为 0）；
- 段内第二个**不同时刻**的点确定 ``slope = (v - v0) / float(t - t0)``，
  此后模型固定，不再改变；
- 只有同一时刻的段（重复时刻点列）``slope`` 记 0.0，解码恒为 ``y0``；
- **每个新点都用上式试算解码值**，``|dec(t) - v| > epsilon`` 即切段：
  当前段收尾（其全部已接纳点相对最终存储模型的误差都已逐点验证 ≤ eps），
  新点开启新段。因此误差承诺在写入时逐点成立——含段首段尾与任何尖峰，
  不依赖「两端点接近」之类的间接推断；
- 同一时刻的重复点：解码值恒为 ``y0``，若 ``|y0 - v| > eps``（冲突的
  重复时间）必须切段——新点自成一个新段；误差相容的重复点可共存一段；
- 候选 slope 非有限（理论上仅当相邻值差溢出 f64）按试算失败处理，切段。

流式与内存上界
--------------
- 样本用**键集分页**（``WHERE (ts_ns, file_id, frame_offset, ordinal) >
  (上次末行键) ORDER BY ... LIMIT B``）逐批取出，Python 侧在途样本
  ≤ B 行；不把整组原值载入内存；
- 分段器状态每组只有 O(1)：段首 ``(t0, v0)``、``slope``、段内点数——
  一条多长的精确直线也只缓存它的模型参数，绝不缓存它的全部原值；
- 逐点记录（时刻/定位键）每块缓冲 ≤ ``block_points`` 条即顺序落盘；
  物理值的在途状态上界 = 当前段模型 + 当前点（常数）；
- 块索引与组目录随块/组各增一条定长记录，随文件结构线性增长，
  与原始值总量无关。
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, List, Optional, Tuple

from . import archfmt as F

DEFAULT_BLOCK_POINTS = 4096      # 单块目标点数（到达即切，可跨段）
DEFAULT_BATCH_SIZE = 8192        # 键集分页批大小


@dataclass(frozen=True)
class ClosedSegment:
    """一段已收尾的折线：模型参数 + 覆盖点数。"""

    t0: int
    y0: float
    slope: float
    n_points: int


class Segmenter:
    """单组在线分段器：O(1) 状态，逐点喂入，切点时吐出已收尾段。"""

    __slots__ = ("eps", "t0", "v0", "slope", "n")

    def __init__(self, eps: float) -> None:
        self.eps = eps
        self.t0 = 0
        self.v0 = 0.0
        self.slope: Optional[float] = None   # 第二个不同时刻点出现前为 None
        self.n = 0

    def current_model(self) -> Tuple[int, float, float]:
        """当前开放段此刻的模型（slope 未定时报 0.0：此时段内点全同时刻，
        解码恒为 v0，该占位参数对这些点自洽）。"""
        return self.t0, self.v0, self.slope if self.slope is not None else 0.0

    def _decode(self, t: int) -> float:
        # 与解码器完全相同的浮点运算序列
        s = self.slope if self.slope is not None else 0.0
        return self.v0 + s * float(t - self.t0)

    def _cut(self, t: int, v: float, slope: float) -> ClosedSegment:
        closed = ClosedSegment(self.t0, self.v0, slope, self.n)
        self.t0, self.v0, self.slope, self.n = t, v, None, 1
        return closed

    def feed(self, t: int, v: float) -> Optional[ClosedSegment]:
        if self.n == 0:
            self.t0, self.v0, self.slope, self.n = t, v, None, 1
            return None
        if self.slope is None:
            if t == self.t0:
                # 重复时刻：解码恒为 v0；冲突则必须切段
                if abs(self.v0 - v) <= self.eps:
                    self.n += 1
                    return None
                return self._cut(t, v, 0.0)
            cand = (v - self.v0) / float(t - self.t0)
            if (math.isfinite(cand)
                    and abs(self.v0 + cand * float(t - self.t0) - v) <= self.eps):
                self.slope = cand
                self.n += 1
                return None
            # 候选模型连定义它的第二个点都容不下（eps 小于浮点回代误差）：
            # 已有点全部同时刻，按常数段收尾
            return self._cut(t, v, 0.0)
        if abs(self._decode(t) - v) <= self.eps:
            self.n += 1
            return None
        return self._cut(t, v, self.slope)

    def finish(self) -> Optional[ClosedSegment]:
        if self.n == 0:
            return None
        closed = ClosedSegment(self.t0, self.v0,
                               self.slope if self.slope is not None else 0.0,
                               self.n)
        self.n = 0
        return closed


# ----------------------------------------------------------------------
# 构建统计
# ----------------------------------------------------------------------

@dataclass
class GroupBuildInfo:
    group_id: int
    boot_seq: int
    layout_id: int
    unit: str
    points: int = 0
    segments: int = 0
    parts: int = 0
    param_bytes: int = 0
    block_start: int = 0
    block_count: int = 0


@dataclass
class ArchiveStats:
    """构建结果统计（与 ``summary`` 命令口径一致）。"""

    path: str = ""
    device: int = 0
    channel: int = 0
    epsilon: float = 0.0
    t_start_ns: int = 0
    t_end_ns: int = 0
    groups: List[GroupBuildInfo] = field(default_factory=list)
    points: int = 0
    segments: int = 0
    parts: int = 0
    param_bytes: int = 0
    blocks: int = 0
    file_bytes: int = 0


class _BlockWriter:
    """累积一个块的逐点记录与线段部件；达到目标点数即落盘。"""

    def __init__(self, group_id: int, block_points: int) -> None:
        self.group_id = group_id
        self.block_points = block_points
        self.points: List[Tuple[int, int, int, int]] = []
        # 已封闭部件：(seg_seq, start, count, t0, y0, slope)
        self.parts: List[Tuple[int, int, int, int, float, float]] = []

    def full(self) -> bool:
        return len(self.points) >= self.block_points

    def serialize(self) -> bytes:
        n_points = len(self.points)
        t_min = self.points[0][0]
        t_max = self.points[-1][0]
        parts_b = b"".join(
            F.PART.pack(seg, start, count, 0, t0, y0, slope)
            for seg, start, count, t0, y0, slope in self.parts)
        points_b = b"".join(
            F.POINT.pack(ts, fid, off, od) for ts, fid, off, od in self.points)
        block_len = F.BLOCK_HEAD_LEN + len(parts_b) + len(points_b) + 4
        head = F.BLOCK_HEAD.pack(F.BLOCK_MAGIC, block_len, self.group_id,
                                 n_points, len(self.parts), 0, t_min, t_max)
        body = head + parts_b + points_b
        return body + F.crc32(body).to_bytes(4, "little")


class ArchiveBuilder:
    """把 DuckDB 中已接纳的 tele_samples 压缩归档为 TPLA 文件。

    选择范围：``device_id``、``channel``、``ts_ns ∈ [t_start_ns, t_end_ns)``；
    ``epsilon`` 为该通道最终物理单位下的绝对误差上限，必须为正有限值。
    组 = (boot_seq, layout_id, unit)：启动周期、布局或单位不同的读数
    绝不共用一段折线。
    """

    def __init__(self, out_path: str | Path, device: int, channel: int,
                 t_start_ns: int, t_end_ns: int, epsilon: float,
                 block_points: int = DEFAULT_BLOCK_POINTS,
                 batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        if not isinstance(epsilon, float):
            epsilon = float(epsilon)
        if not math.isfinite(epsilon) or epsilon <= 0.0:
            raise ValueError(
                f"epsilon 必须为正的有限值（该通道最终物理单位计），实际 {epsilon!r}")
        if not (isinstance(t_start_ns, int) and isinstance(t_end_ns, int)
                and t_start_ns < t_end_ns):
            raise ValueError(
                f"时间范围非法：要求 t_start_ns < t_end_ns，实际 "
                f"[{t_start_ns!r}, {t_end_ns!r})")
        if block_points <= 0 or batch_size <= 0:
            raise ValueError("block_points 与 batch_size 必须为正整数")
        self.path = Path(out_path)
        self.device = device
        self.channel = channel
        self.t_start = t_start_ns
        self.t_end = t_end_ns
        self.epsilon = epsilon
        self.block_points = block_points
        self.batch_size = batch_size

    # ---- 主流程 --------------------------------------------------------

    def build(self, con) -> ArchiveStats:
        groups = con.execute(
            "SELECT DISTINCT boot_seq, layout_id, unit FROM tele_samples "
            "WHERE device_id = ? AND channel = ? AND ts_ns >= ? AND ts_ns < ? "
            "ORDER BY boot_seq, layout_id, unit",
            [self.device, self.channel, self.t_start, self.t_end]).fetchall()

        stats = ArchiveStats(path=str(self.path), device=self.device,
                             channel=self.channel, epsilon=self.epsilon,
                             t_start_ns=self.t_start, t_end_ns=self.t_end)
        index: List[Tuple[int, int, int, int, int]] = []   # (gid, t_min, t_max, off, len)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w+b") as fh:
            self._write_header(fh, len(groups), 0)         # 块数收尾时回填
            for gid, (boot_seq, layout_id, unit) in enumerate(groups):
                info = GroupBuildInfo(gid, boot_seq, layout_id, unit,
                                      block_start=len(index))
                self._build_group(con, fh, index, info)
                stats.groups.append(info)
            index_off = fh.tell()
            index_b = self._serialize_index(index)
            fh.write(index_b)
            dir_off = fh.tell()
            dir_b = self._serialize_dir(stats.groups)
            fh.write(dir_b)
            self._write_footer(fh, index_off, len(index_b), dir_off, len(dir_b))
            fh.seek(0)
            self._write_header(fh, len(groups), len(index))
        stats.blocks = len(index)
        stats.points = sum(g.points for g in stats.groups)
        stats.segments = sum(g.segments for g in stats.groups)
        stats.parts = sum(g.parts for g in stats.groups)
        stats.param_bytes = sum(g.param_bytes for g in stats.groups)
        stats.file_bytes = os.path.getsize(self.path)
        return stats

    # ---- 单组构建 ------------------------------------------------------

    def _build_group(self, con, fh: BinaryIO, index, info: GroupBuildInfo) -> None:
        seg = Segmenter(self.epsilon)
        bw = _BlockWriter(info.group_id, self.block_points)
        seg_seq = 0
        # 当前开放段在本块中的部件：起始下标 + 已有点数
        part_start = 0
        part_count = 0

        def seal_block() -> None:
            nonlocal part_start, part_count
            if part_count:
                # 开放段跨块：以当前已知模型封闭本块部件（slope 未定时段内
                # 点全同时刻，占位 0.0 对这些点自洽；后续块续记同一 seg_seq）
                t0, y0, slope = seg.current_model()
                bw.parts.append((seg_seq, part_start, part_count, t0, y0, slope))
                info.parts += 1
                info.param_bytes += F.PARAM_BYTES_PER_PART
            if not bw.points:
                return
            blob = bw.serialize()
            index.append((info.group_id, bw.points[0][0], bw.points[-1][0],
                          fh.tell(), len(blob)))
            fh.write(blob)
            info.block_count += 1
            bw.points.clear()
            bw.parts.clear()
            part_start = 0            # 开放段跨块：新块从 0 起续记部件
            part_count = 0

        def close_segment(closed: ClosedSegment) -> None:
            nonlocal part_start, part_count, seg_seq
            # 用收尾时的最终模型封闭本块部件；段恰好在上一块边界结束时
            # 本块没有它的点（part_count=0），不产生空部件
            if part_count:
                bw.parts.append((seg_seq, part_start, part_count,
                                 closed.t0, closed.y0, closed.slope))
                info.parts += 1
                info.param_bytes += F.PARAM_BYTES_PER_PART
            info.segments += 1
            seg_seq += 1
            part_start = len(bw.points)
            part_count = 0

        last_key: Optional[Tuple[int, int, int, int]] = None
        while True:
            sql = ("SELECT ts_ns, value, file_id, frame_offset, ordinal "
                   "FROM tele_samples "
                   "WHERE device_id = ? AND channel = ? AND boot_seq = ? "
                   "AND layout_id = ? AND unit = ? AND ts_ns >= ? AND ts_ns < ?")
            params: list = [self.device, self.channel, info.boot_seq,
                            info.layout_id, info.unit, self.t_start, self.t_end]
            if last_key is not None:
                sql += (" AND (ts_ns, file_id, frame_offset, ordinal)"
                        " > (?, ?, ?, ?)")
                params.extend(last_key)
            sql += (" ORDER BY ts_ns, file_id, frame_offset, ordinal LIMIT ?")
            params.append(self.batch_size)
            rows = con.execute(sql, params).fetchall()
            if not rows:
                break
            for ts, value, file_id, frame_off, ordinal in rows:
                closed = seg.feed(ts, value)
                if closed is not None:
                    close_segment(closed)
                bw.points.append((ts, file_id, frame_off, ordinal))
                part_count += 1
                info.points += 1
                if bw.full():
                    seal_block()
            last_key = (rows[-1][0], rows[-1][2], rows[-1][3], rows[-1][4])

        closed = seg.finish()
        if closed is not None:
            close_segment(closed)
        seal_block()

    # ---- 序列化 --------------------------------------------------------

    def _write_header(self, fh: BinaryIO, group_count: int,
                      block_count: int) -> None:
        created = int(time.time())
        body = F.HEADER.pack(F.MAGIC, F.VERSION, 0, self.device, self.channel,
                             self.epsilon, self.t_start, self.t_end,
                             group_count, block_count, created, 0)
        crc = F.crc32(body[:56])       # 覆盖 crc 字段之前的全部 56 字节
        fh.write(F.HEADER.pack(F.MAGIC, F.VERSION, 0, self.device,
                               self.channel, self.epsilon, self.t_start,
                               self.t_end, group_count, block_count,
                               created, crc))

    @staticmethod
    def _serialize_index(index) -> bytes:
        out = [F.INDEX_HEAD.pack(F.INDEX_MAGIC, len(index))]
        for gid, t_min, t_max, off, blen in index:
            out.append(F.INDEX_ENTRY.pack(gid, t_min, t_max, off, blen))
        body = b"".join(out)
        return body + F.crc32(body).to_bytes(4, "little")

    @staticmethod
    def _serialize_dir(groups) -> bytes:
        out = [F.DIR_HEAD.pack(F.DIR_MAGIC, len(groups))]
        for g in groups:
            unit_b = g.unit.encode("utf-8")
            if len(unit_b) > F.MAX_UNIT_LEN:
                raise ValueError(f"单位名过长：{g.unit!r}")
            out.append(F.DIR_ENTRY_FIXED.pack(g.group_id, g.boot_seq,
                                              g.layout_id, len(unit_b), 0))
            out.append(unit_b)
            out.append(F.DIR_ENTRY_TAIL.pack(g.points, g.segments,
                                             g.param_bytes, g.block_start,
                                             g.block_count))
        body = b"".join(out)
        return body + F.crc32(body).to_bytes(4, "little")

    @staticmethod
    def _write_footer(fh: BinaryIO, index_off: int, index_len: int,
                      dir_off: int, dir_len: int) -> None:
        body = F.FOOTER.pack(index_off, index_len, dir_off, dir_len, 0,
                             F.TAIL_MAGIC)
        crc = F.crc32(body[:24])       # 覆盖 crc 字段之前的全部 24 字节
        fh.write(F.FOOTER.pack(index_off, index_len, dir_off, dir_len, crc,
                               F.TAIL_MAGIC))
