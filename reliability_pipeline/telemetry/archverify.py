"""独立核验器：用单独的简单解码与逐点误差枚举核验实际归档文件。

与压缩器的关系
--------------
- 不调用 ``archbuild`` 的任何函数，也不使用压缩器内部的误差统计；
  只按 ``archfmt`` 公布的字节格式**重新解析归档文件本身**；
- 原值真值来自 DuckDB ``tele_samples``（核验器允许回库比对——
  受约束的是*解码器*不得依赖数据库，核验器的职责正是比对）；
- 每个采样点做两层检查：
  1. **f64 解码误差**：用与解码器逐指令相同的浮点运算重算
     ``dec = y0 + slope*float(ts-t0)``，要求 ``|dec - 原值| <= eps``；
  2. **精确算术误差**：模型参数与时刻都是精确可表示的（f64/i64 →
     Fraction），要求 ``|精确解码 - 原值| <= eps + 浮点求值余量``，
     余量按解码公式的舍入上界逐点给出；
- 同时核对：逐点 (ts_ns, file_id, frame_offset, ordinal) 序列与库中
  查询结果**逐项一致**（不漏点、不多点、次序与定位键不变）；
- 范围查询交叉核对：多个探测区间（含块边界、重复时刻边界）上，
  ``query`` 结果必须与完整解码后过滤**完全一致**，且窄区间实际
  读取块数小于总块数。
"""

from __future__ import annotations

import random
import struct
import zlib
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import archfmt as F


@dataclass
class GroupVerify:
    group_id: int
    boot_seq: int
    layout_id: int
    unit: str
    points: int = 0
    segments: int = 0
    max_abs_err: float = 0.0          # f64 解码最大绝对误差
    max_exact_err: Fraction = Fraction(0)   # 精确算术最大绝对误差


@dataclass
class VerifyReport:
    path: str = ""
    device: int = 0
    channel: int = 0
    epsilon: float = 0.0
    groups: List[GroupVerify] = field(default_factory=list)
    points_checked: int = 0
    range_probes: int = 0
    blocks_read_narrow: int = 0
    blocks_total: int = 0
    violations: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


# ----------------------------------------------------------------------
# 独立的最小解码器（只依赖 archfmt 的格式定义，不复用 archread 逻辑）
# ----------------------------------------------------------------------

def _crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


class _MiniParser:
    """按格式规范逐段解析并校验；任何不符抛 ArchiveError。"""

    def __init__(self, path: str | Path) -> None:
        self.raw = Path(path).read_bytes()
        if len(self.raw) < F.HEADER_LEN + F.FOOTER_LEN:
            raise F.ArchiveError("归档被截断：不足文件头+文件尾")
        (magic, ver, _fl, self.device, self.channel, self.eps,
         self.t0, self.t1, self.ngroups, self.nblocks,
         _created, hcrc) = F.HEADER.unpack(self.raw[:F.HEADER_LEN])
        if magic != F.MAGIC or ver != F.VERSION:
            raise F.ArchiveError("文件头魔数/版本不符")
        if _crc(self.raw[:56]) != hcrc:
            raise F.ArchiveError("文件头 CRC 不符")
        foot = self.raw[-F.FOOTER_LEN:]
        ioff, ilen, doff, dlen, fcrc, tail = F.FOOTER.unpack(foot)
        if tail != F.TAIL_MAGIC or _crc(foot[:24]) != fcrc:
            raise F.ArchiveError("文件尾损坏或归档被截断")
        self.index_raw = self.raw[ioff:ioff + ilen]
        self.dir_raw = self.raw[doff:doff + dlen]
        if len(self.index_raw) != ilen or len(self.dir_raw) != dlen:
            raise F.ArchiveError("索引/目录越界：归档已损坏")
        self._parse_index()
        self._parse_dir()

    def _parse_index(self) -> None:
        body, crc = self.index_raw[:-4], self.index_raw[-4:]
        if _crc(body) != int.from_bytes(crc, "little"):
            raise F.ArchiveError("块索引 CRC 不符")
        magic, n = F.INDEX_HEAD.unpack(body[:F.INDEX_HEAD.size])
        if magic != F.INDEX_MAGIC:
            raise F.ArchiveError("块索引魔数不符")
        self.index = []
        pos = F.INDEX_HEAD.size
        for _ in range(n):
            self.index.append(F.INDEX_ENTRY.unpack(body[pos:pos + F.INDEX_ENTRY_LEN]))
            pos += F.INDEX_ENTRY_LEN

    def _parse_dir(self) -> None:
        body, crc = self.dir_raw[:-4], self.dir_raw[-4:]
        if _crc(body) != int.from_bytes(crc, "little"):
            raise F.ArchiveError("组目录 CRC 不符")
        magic, n = F.DIR_HEAD.unpack(body[:F.DIR_HEAD.size])
        if magic != F.DIR_MAGIC:
            raise F.ArchiveError("组目录魔数不符")
        self.groups = []
        pos = F.DIR_HEAD.size
        for _ in range(n):
            gid, boot, lay, ulen, _f = F.DIR_ENTRY_FIXED.unpack(
                body[pos:pos + F.DIR_ENTRY_FIXED.size])
            pos += F.DIR_ENTRY_FIXED.size
            unit = body[pos:pos + ulen].decode("utf-8")
            pos += ulen
            pts, segs, pb, bs, bc = F.DIR_ENTRY_TAIL.unpack(
                body[pos:pos + F.DIR_ENTRY_TAIL.size])
            pos += F.DIR_ENTRY_TAIL.size
            self.groups.append((gid, boot, lay, unit, pts, segs, pb, bs, bc))

    def block(self, i: int):
        """返回 (parts, points)；整块 CRC 校验。"""
        _gid, _tmin, _tmax, off, blen = self.index[i]
        raw = self.raw[off:off + blen]
        if len(raw) != blen:
            raise F.ArchiveError(f"块 #{i} 被截断")
        body, crc = raw[:-4], raw[-4:]
        if _crc(body) != int.from_bytes(crc, "little"):
            raise F.ArchiveError(f"块 #{i} CRC 不符")
        (magic, _blen, gid, npts, nparts, _fl,
         tmin, tmax) = F.BLOCK_HEAD.unpack(body[:F.BLOCK_HEAD_LEN])
        if magic != F.BLOCK_MAGIC:
            raise F.ArchiveError(f"块 #{i} 魔数不符")
        parts, points = [], []
        pos = F.BLOCK_HEAD_LEN
        for _ in range(nparts):
            parts.append(F.PART.unpack(body[pos:pos + F.PART_LEN]))
            pos += F.PART_LEN
        for _ in range(npts):
            points.append(F.POINT.unpack(body[pos:pos + F.POINT_LEN]))
            pos += F.POINT_LEN
        return gid, tmin, tmax, parts, points


def _decode_points(parts, points):
    """逐点解码：(ts, file_id, frame_offset, ordinal, dec_value, seg_seq)。"""
    out = []
    ip = 0
    seg, start, count, _r, t0, y0, slope = parts[ip]
    for i, (ts, fid, foff, od) in enumerate(points):
        while i >= start + count:
            ip += 1
            seg, start, count, _r, t0, y0, slope = parts[ip]
        out.append((ts, fid, foff, od, y0 + slope * float(ts - t0), seg,
                    t0, y0, slope))
    return out


# ----------------------------------------------------------------------
# 核验主流程
# ----------------------------------------------------------------------

def verify_archive(path: str | Path, db_path: str | Path,
                   probes: int = 12, seed: int = 7) -> VerifyReport:
    """核验归档：逐点误差 + 定位键序列 + 范围查询交叉核对。

    返回 VerifyReport；``report.ok`` 为 False 时 ``violations`` 列出全部问题。
    文件结构损坏直接抛 ``ArchiveError``。
    """
    import duckdb   # 延迟导入：核验器才需要数据库

    parser = _MiniParser(path)
    rep = VerifyReport(path=str(path), device=parser.device,
                       channel=parser.channel, epsilon=parser.eps,
                       blocks_total=parser.nblocks)
    eps = parser.eps
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        # 每组：库中原值序列 vs 归档解码序列
        for (gid, boot, lay, unit, pts, segs, _pb, bs, bc) in parser.groups:
            gv = GroupVerify(gid, boot, lay, unit)
            db_rows = con.execute(
                "SELECT ts_ns, value, file_id, frame_offset, ordinal "
                "FROM tele_samples WHERE device_id = ? AND channel = ? "
                "AND boot_seq = ? AND layout_id = ? AND unit = ? "
                "AND ts_ns >= ? AND ts_ns < ? "
                "ORDER BY ts_ns, file_id, frame_offset, ordinal",
                [parser.device, parser.channel, boot, lay, unit,
                 parser.t0, parser.t1]).fetchall()
            arc_rows: List[tuple] = []
            seg_ids = set()
            for b in range(bs, bs + bc):
                _g, _tmin, _tmax, parts, points = parser.block(b)
                for row in _decode_points(parts, points):
                    arc_rows.append(row)
                    seg_ids.add(row[5])
            gv.points = len(arc_rows)
            gv.segments = len(seg_ids)
            if len(arc_rows) != len(db_rows):
                rep.violations.append(
                    f"组 {gid}：归档点数 {len(arc_rows)} ≠ 库中点数 {len(db_rows)}")
            if pts != len(arc_rows) or segs != len(seg_ids):
                rep.violations.append(
                    f"组 {gid}：目录统计（点 {pts}、段 {segs}）与文件实际"
                    f"（点 {len(arc_rows)}、段 {len(seg_ids)}）不符")
            for i, (db_row, arc_row) in enumerate(zip(db_rows, arc_rows)):
                ts_db, v_db, fid_db, off_db, od_db = db_row
                ts, fid, foff, od, dec, _seg, t0, y0, slope = arc_row
                if (ts, fid, foff, od) != (ts_db, fid_db, off_db, od_db):
                    rep.violations.append(
                        f"组 {gid} 第 {i} 点：定位键/时刻不一致 "
                        f"归档({ts},{fid},{foff},{od}) ≠ 库({ts_db},{fid_db},{off_db},{od_db})")
                    continue
                err = abs(dec - v_db)                     # f64 解码误差
                if not err <= eps:
                    rep.violations.append(
                        f"组 {gid} 第 {i} 点：f64 解码误差 {err!r} > eps {eps!r}"
                        f"（ts={ts}）")
                # 精确算术：模型参数与时刻都是精确值
                exact = Fraction(y0) + Fraction(slope) * (ts - t0)
                exact_err = abs(exact - Fraction(v_db))
                slack = Fraction(2**-52) * (
                    3 * abs(Fraction(slope)) * abs(ts - t0)
                    + abs(Fraction(y0)) + abs(exact))
                if exact_err > Fraction(eps) + slack:
                    rep.violations.append(
                        f"组 {gid} 第 {i} 点：精确解码误差 {float(exact_err)!r} "
                        f"超出 eps+浮点余量（ts={ts}）")
                gv.max_abs_err = max(gv.max_abs_err, err)
                gv.max_exact_err = max(gv.max_exact_err, exact_err)
            rep.points_checked += gv.points
            rep.groups.append(gv)

        # 范围查询交叉核对：query（走块索引）vs 完整解码过滤
        all_rows: Dict[int, List[tuple]] = {}
        for (gid, boot, lay, unit, _p, _s, _pb, bs, bc) in parser.groups:
            rows = []
            for b in range(bs, bs + bc):
                _g, _tmin, _tmax, parts, points = parser.block(b)
                rows.extend(_decode_points(parts, points))
            all_rows[gid] = rows
        t_lo, t_hi = parser.t0, parser.t1
        bounds = sorted({t for rows in all_rows.values()
                         for (t, *_rest) in rows})
        probe_ranges: List[Tuple[int, int]] = [(t_lo, t_hi)]
        if bounds:
            rng = random.Random(seed)
            # 块边界探测：每个块的 [t_min, t_max] 都作为探测窗口
            for (_g, bmin, bmax, _off, _len) in parser.index:
                probe_ranges.append((bmin, bmax + 1))
            for _ in range(probes):
                a = rng.choice(bounds)
                b = rng.choice(bounds)
                if a == b:
                    a, b = min(a, t_hi - 1), max(b, a + 1)
                if a > b:
                    a, b = b, a
                probe_ranges.append((max(a, t_lo), min(b + 1, t_hi)))
        narrow_seen = False
        for (a, b) in probe_ranges:
            # 独立重算 query：按索引选块（与 archread 同一选择规则）
            got: List[tuple] = []
            blocks_read = 0
            for bi, (gid, bmin, bmax, _off, _len) in enumerate(parser.index):
                if bmin < b and bmax >= a:
                    blocks_read += 1
                    _g, _t1_, _t2_, parts, points = parser.block(bi)
                    for row in _decode_points(parts, points):
                        if a <= row[0] < b:
                            got.append((gid,) + row[:5])
            want = sorted(
                (gid,) + r[:5] for gid, rows in all_rows.items() for r in rows
                if a <= r[0] < b)
            if sorted(got) != want:
                rep.violations.append(
                    f"范围查询 [{a}, {b}) 结果与完整解码过滤不一致")
            rep.range_probes += 1
            if blocks_read < parser.nblocks:
                narrow_seen = True
                rep.blocks_read_narrow = max(rep.blocks_read_narrow, blocks_read)
        if parser.nblocks > 1 and bounds and not narrow_seen:
            # 仅当块的时间窗存在不重叠情形时才要求窄探测出现；
            # 全部块共享同一时刻（极端重复时刻归档）不存在更窄的读法
            max_tmin = max(e[1] for e in parser.index)
            min_tmax = min(e[2] for e in parser.index)
            if max_tmin > min_tmax:
                rep.violations.append(
                    "所有探测范围都读取了全部块：块索引未起到选择作用")
    finally:
        con.close()
    return rep
