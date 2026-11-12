"""折线压缩归档（telemetry.arch*）测试。

预期材料独立编写：测试内直接用 struct.pack / zlib.crc32 手工拼帧，
期望值手工计算，不经过 telemetry.generator / telemetry.archdemo，
以免与被测实现互相印证。
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

import pytest

from telemetry import archfmt as F
from telemetry.archbuild import ArchiveBuilder
from telemetry.archread import ArchiveReader
from telemetry.archverify import verify_archive
from telemetry.archcli import main as archcli_main
from telemetry.ingest import Ingester

MAGIC = b"\xA7\x5C\xE1\x09"
EPOCH = 1_700_000_000_000_000_000          # > 2^53，float 无法表示 1ns 差
T0, T1 = EPOCH - 1_000_000_000, EPOCH + 2_000_000_000_000


# ----------------------------------------------------------------------
# 独立的手工拼帧工具（与 telemetry.protocol 平行实现）
# ----------------------------------------------------------------------

def crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def frame(kind: int, payload: bytes) -> bytes:
    body = bytes([kind]) + struct.pack("<H", len(payload)) + payload
    return MAGIC + body + struct.pack("<I", crc(body))


def boot(device: int, seq: int, epoch_ns: int, hz: int) -> bytes:
    return frame(3, struct.pack("<HIqI", device, seq, epoch_ns, hz))


def layout(device: int, lid: int, channels) -> bytes:
    payload = struct.pack("<HHB", device, lid, len(channels))
    for ch, rt, gain, bias, unit in channels:
        payload += struct.pack("<BBddB", ch, rt, gain, bias, unit)
    return frame(1, payload)


def meas(device: int, lid: int, tick: int, values, raw_types) -> bytes:
    payload = struct.pack("<HHI", device, lid, tick & 0xFFFFFFFF)
    fmts = {1: "<h", 2: "<i", 3: "<f"}
    for v, rt in zip(values, raw_types):
        payload += struct.pack(fmts[rt], v)
    return frame(2, payload)


# ch0: int16，gain=0.5，bias=1.0，毫摄氏度→°C（物理值 = (raw*0.5+1)*1e-3）
CH0_C = [(0, 1, 0.5, 1.0, 1)]


def ingest(tmp_path: Path, streams, name="t.duckdb") -> Path:
    """把若干字节流依次接入同一库，返回库路径。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / name
    with Ingester(db) as ing:
        for i, data in enumerate(streams):
            f = tmp_path / f"s{i}.bin"
            f.write_bytes(data)
            ing.ingest_file(f)
    return db


def build(db: Path, out: Path, device: int, eps: float, channel: int = 0,
          t0: int = T0, t1: int = T1, **kw):
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    try:
        return ArchiveBuilder(out, device, channel, t0, t1, eps, **kw).build(con)
    finally:
        con.close()


def decode_all(path: Path):
    with ArchiveReader(path) as r:
        return list(r.decode())


def db_points(db: Path, device: int, channel: int = 0):
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute(
            "SELECT ts_ns, value, file_id, frame_offset, ordinal "
            "FROM tele_samples WHERE device_id = ? AND channel = ? "
            "ORDER BY ts_ns, file_id, frame_offset, ordinal",
            [device, channel]).fetchall()
    finally:
        con.close()


# ----------------------------------------------------------------------
# 1. 不等间隔精确直线：压成一段，参数区确实小于原值
# ----------------------------------------------------------------------

def test_ramp_single_segment_and_param_shrink(tmp_path):
    # t_k 间隔不规则；raw = 100 + t → 物理值 (51+0.5t)*1e-3，时间的精确线性函数
    ticks = [k + (k * k) // 50 for k in range(300)]
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += b"".join(meas(40, 1, t, [100 + t], [1]) for t in ticks)
    db = ingest(tmp_path, [stream])
    out = tmp_path / "ramp.tpla"
    stats = build(db, out, 40, 0.05, block_points=32)

    assert stats.points == 300
    assert stats.segments == 1                      # 长直线压成一段
    assert stats.blocks == 10                       # 300/32 向上取整
    # 单段跨 10 块：每块一份部件参数
    assert stats.param_bytes == 24 * 10
    assert stats.param_bytes < 8 * 300              # 值参数确实少于原样存值
    rows = decode_all(out)
    assert len(rows) == 300
    # 时刻精确往返（i64），与库中序列逐项一致
    assert [r.ts_ns for r in rows] == [EPOCH + t * 1_000_000 for t in ticks]
    assert [r.ts_ns for r in rows] == [p[0] for p in db_points(db, 40)]
    # 逐点误差（独立手算）：全部 ≤ eps
    for r, t in zip(rows, ticks):
        expect = ((100 + t) * 0.5 + 1.0) * 1e-3
        assert abs(r.value - expect) <= 0.05
        assert r.unit == "°C" and r.seg_seq == 0
    # 独立核验器确认
    rep = verify_archive(out, db)
    assert rep.ok, rep.violations
    assert rep.points_checked == 300


def test_ramp_batch_size_irrelevant(tmp_path):
    ticks = [k + (k * k) // 7 for k in range(120)]
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += b"".join(meas(40, 1, t, [100 + t], [1]) for t in ticks)
    db = ingest(tmp_path, [stream])
    build(db, tmp_path / "a.tpla", 40, 0.05, block_points=16, batch_size=7)
    build(db, tmp_path / "b.tpla", 40, 0.05, block_points=16, batch_size=10000)
    a, b = decode_all(tmp_path / "a.tpla"), decode_all(tmp_path / "b.tpla")
    assert [(r.ts_ns, r.value, r.seg_seq) for r in a] == \
           [(r.ts_ns, r.value, r.seg_seq) for r in b]


# ----------------------------------------------------------------------
# 2. 单点尖峰：不被抹掉，逐点容差
# ----------------------------------------------------------------------

def test_spike_preserved(tmp_path):
    stream = boot(42, 1, EPOCH, 1000) + layout(42, 1, CH0_C)
    stream += b"".join(meas(42, 1, k, [4000 if k == 500 else 200], [1])
                       for k in range(1000))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "spike.tpla"
    stats = build(db, out, 42, 0.05, block_points=128)
    assert stats.segments >= 3                      # 尖峰附近必须切段
    rows = decode_all(out)
    v_base, v_spike = (200 * 0.5 + 1.0) * 1e-3, (4000 * 0.5 + 1.0) * 1e-3
    spike_ts = EPOCH + 500 * 1_000_000
    for r in rows:
        expect = v_spike if r.ts_ns == spike_ts else v_base
        assert abs(r.value - expect) <= 0.05        # 含尖峰点本身
    # 尖峰点的解码值贴近尖峰（不是被削平成基线）
    (spike_row,) = [r for r in rows if r.ts_ns == spike_ts]
    assert abs(spike_row.value - v_spike) <= 0.05
    assert abs(spike_row.value - v_base) > 1.0
    rep = verify_archive(out, db)
    assert rep.ok, rep.violations


# ----------------------------------------------------------------------
# 3. 锯齿：振幅大于容差，逐点核对
# ----------------------------------------------------------------------

def test_sawtooth_pointwise(tmp_path):
    stream = boot(43, 1, EPOCH, 1000) + layout(43, 1, CH0_C)
    raws = []
    for k in range(600):
        phase = k % 40
        amp = phase if phase <= 20 else 40 - phase
        raws.append(200 + amp * 10)
    stream += b"".join(meas(43, 1, k, [r], [1]) for k, r in enumerate(raws))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "saw.tpla"
    stats = build(db, out, 43, 0.01, block_points=64)
    assert stats.segments > 1
    rows = decode_all(out)
    for r, raw in zip(rows, raws):
        assert abs(r.value - (raw * 0.5 + 1.0) * 1e-3) <= 0.01
    rep = verify_archive(out, db)
    assert rep.ok, rep.violations
    assert rep.points_checked == 600


# ----------------------------------------------------------------------
# 4. 巨大纳秒基数 + 1ns 间隔：时刻精确，直线仍为一段
# ----------------------------------------------------------------------

def test_huge_epoch_one_ns_spacing(tmp_path):
    assert float(EPOCH) == float(EPOCH + 1)         # 浮点确实存不下 1ns 差
    stream = boot(44, 1, EPOCH, 1_000_000_000) + layout(44, 1, CH0_C)
    stream += b"".join(meas(44, 1, k, [1000 + k], [1]) for k in range(1000))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "huge.tpla"
    stats = build(db, out, 44, 0.05, block_points=128)
    assert stats.segments == 1
    rows = decode_all(out)
    assert [r.ts_ns for r in rows] == [EPOCH + k for k in range(1000)]
    for k, r in enumerate(rows):
        assert abs(r.value - ((1000 + k) * 0.5 + 1.0) * 1e-3) <= 0.05
    rep = verify_archive(out, db)
    assert rep.ok, rep.violations


# ----------------------------------------------------------------------
# 5. 重复时刻：冲突必须切段，相容可同段
# ----------------------------------------------------------------------

def _dup_streams():
    # hz=3e9：tick=1,2 → ts=EPOCH+0；tick=3,4 → ts=EPOCH+1
    s1 = (boot(45, 1, EPOCH, 3_000_000_000) + layout(45, 1, CH0_C)
          + meas(45, 1, 1, [100], [1]))                      # E+0, 0.051
    s2 = (layout(45, 1, CH0_C)                               # 重声明（重复忽略）
          + meas(45, 1, 2, [200], [1])                       # E+0, 0.101 冲突
          + meas(45, 1, 3, [101], [1])                       # E+1, 0.0515
          + meas(45, 1, 4, [102], [1]))                      # E+1, 0.052 相容
    return s1, s2


def test_conflicting_duplicate_timestamps_cut(tmp_path):
    db = ingest(tmp_path, list(_dup_streams()))
    out = tmp_path / "dup.tpla"
    stats = build(db, out, 45, 0.005)
    rows = decode_all(out)
    assert stats.points == 4
    assert stats.segments == 2                    # 冲突的同一时刻被切成两段
    at_t0 = [r for r in rows if r.ts_ns == EPOCH]
    assert len(at_t0) == 2                        # 重复点不合并、不去重
    assert {r.seg_seq for r in at_t0} == {0, 1}   # 两个值分属不同线段
    vals = sorted(r.value for r in at_t0)
    assert abs(vals[0] - 0.051) <= 0.005          # 各自的值各自满足容差
    assert abs(vals[1] - 0.101) <= 0.005
    # 来源可溯：两个 E+0 点来自不同文件
    assert len({r.file_id for r in at_t0}) == 2
    rep = verify_archive(out, db)
    assert rep.ok, rep.violations


def test_compatible_duplicates_share_segment(tmp_path):
    stream = (boot(45, 1, EPOCH, 3_000_000_000) + layout(45, 1, CH0_C)
              + meas(45, 1, 1, [100], [1])                 # E+0, 0.051
              + meas(45, 1, 2, [101], [1]))                # E+0, 0.0515
    db = ingest(tmp_path, [stream])
    out = tmp_path / "ok.tpla"
    stats = build(db, out, 45, 0.005)
    assert stats.points == 2 and stats.segments == 1
    rows = decode_all(out)
    assert all(abs(r.value - 0.051) <= 0.005 for r in rows)
    rep = verify_archive(out, db)
    assert rep.ok, rep.violations


def test_duplicate_timestamps_across_block_boundary(tmp_path):
    # hz=4e9：tick=0..3 全部落在 ts=EPOCH+0；block_points=2 → 重复时刻跨块
    stream = boot(45, 1, EPOCH, 4_000_000_000) + layout(45, 1, CH0_C)
    stream += b"".join(meas(45, 1, k, [100 + (k % 2)], [1]) for k in range(4))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "xb.tpla"
    stats = build(db, out, 45, 0.005, block_points=2)
    assert stats.points == 4 and stats.blocks == 2
    with ArchiveReader(out) as r:
        res = r.query(EPOCH, EPOCH + 1)
    assert len(res.rows) == 4                     # 跨块边界的重复时刻不漏
    assert res.blocks_read == 2                   # 两个块都重叠该时刻
    assert all(row.ts_ns == EPOCH for row in res.rows)
    rep = verify_archive(out, db)
    assert rep.ok, rep.violations


def test_no_phantom_part_at_segment_block_boundary(tmp_path):
    # 段 1 恰好写满块 1，段 2 从块 2 开始：每块应恰好 1 个部件，
    # 不产生 count=0 的幽灵部件（参数区字节不虚记）
    stream = (boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
              + meas(40, 1, 0, [100], [1]) + meas(40, 1, 1, [101], [1])
              + meas(40, 1, 2, [2000], [1])          # 突变：段 1 在此收尾
              + meas(40, 1, 3, [2001], [1]))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "pp.tpla"
    stats = build(db, out, 40, 0.005, block_points=2)
    assert stats.points == 4 and stats.blocks == 2 and stats.segments == 2
    assert stats.parts == 2                          # 每块恰好一个部件
    assert stats.param_bytes == 2 * 24
    rep = verify_archive(out, db)
    assert rep.ok, rep.violations


def test_segment_slope_fixed_after_block_seal(tmp_path):
    # 块 1 被同一时刻的重复点写满（slope 未定，存占位 0.0）；
    # 块 2 才出现不同时刻点并确定 slope：同一段跨块存两份参数，各自自洽
    stream = (boot(45, 1, EPOCH, 4_000_000_000) + layout(45, 1, CH0_C)
              + meas(45, 1, 0, [100], [1])       # E+0, 0.051
              + meas(45, 1, 1, [101], [1])       # E+0, 0.0515（块 1 写满）
              + meas(45, 1, 4, [102], [1])       # E+1, 0.052（此刻定 slope）
              + meas(45, 1, 5, [103], [1]))      # E+1, 0.0525
    db = ingest(tmp_path, [stream])
    out = tmp_path / "fix.tpla"
    stats = build(db, out, 45, 0.005, block_points=2)
    assert stats.points == 4 and stats.blocks == 2 and stats.segments == 1
    rows = decode_all(out)
    assert [r.seg_seq for r in rows] == [0, 0, 0, 0]
    expects = [0.051, 0.0515, 0.052, 0.0525]
    for r, v in zip(rows, expects):
        assert abs(r.value - v) <= 0.005
    rep = verify_archive(out, db)
    assert rep.ok, rep.violations


# ----------------------------------------------------------------------
# 6. 跨启动周期 / 布局 / 单位：分组，互不共段
# ----------------------------------------------------------------------

def test_groups_split_by_boot_layout_unit(tmp_path):
    stream = boot(46, 1, EPOCH, 1000)
    stream += layout(46, 1, [(0, 1, 1.0, 0.0, 1)])         # ch0 → °C
    stream += layout(46, 2, [(0, 2, 1.0, 0.0, 2)])         # ch0 → kPa
    stream += meas(46, 1, 1, [10], [1]) + meas(46, 2, 2, [20], [2])
    stream += meas(46, 1, 3, [11], [1]) + meas(46, 2, 4, [21], [2])
    stream += boot(46, 2, EPOCH + 1_000_000_000_000, 1000)  # 新启动周期
    stream += layout(46, 1, [(0, 1, 1.0, 0.0, 1)])
    stream += meas(46, 1, 0, [50], [1]) + meas(46, 1, 1, [51], [1])
    db = ingest(tmp_path, [stream])
    out = tmp_path / "multi.tpla"
    stats = build(db, out, 46, 0.01)
    assert len(stats.groups) == 3
    keys = {(g.boot_seq, g.layout_id, g.unit) for g in stats.groups}
    assert keys == {(1, 1, "°C"), (1, 2, "kPa"), (2, 1, "°C")}
    assert all(g.segments == 1 and g.points == 2 for g in stats.groups)
    rows = decode_all(out)
    by_group = {}
    for r in rows:
        by_group.setdefault(r.group_id, []).append(r)
    assert len(by_group) == 3
    for gid, g in enumerate(stats.groups):
        assert all(r.unit == g.unit for r in by_group[gid])
        assert all(r.boot_seq == g.boot_seq for r in by_group[gid])
    rep = verify_archive(out, db)
    assert rep.ok, rep.violations


# ----------------------------------------------------------------------
# 7. 范围查询：与完整解码一致，窄范围只读少数块
# ----------------------------------------------------------------------

def test_range_query_reads_few_blocks_and_matches(tmp_path):
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += b"".join(meas(40, 1, k, [100 + k], [1]) for k in range(1000))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "q.tpla"
    stats = build(db, out, 40, 0.05, block_points=50)
    assert stats.blocks == 20
    with ArchiveReader(out) as r:
        full = list(r.decode())
        # 多个窗口（含块边界对齐、块内、单点、空窗口）
        windows = [(EPOCH + 100_000_000, EPOCH + 110_000_000),
                   (EPOCH + 250_000_000, EPOCH + 251_000_000),
                   (EPOCH + 999_000_000, EPOCH + 1_000_000_000),
                   (EPOCH - 1_000_000, EPOCH),                 # 第一个点
                   (EPOCH + 5_000_000_000, EPOCH + 6_000_000_000)]  # 空
        for a, b in windows:
            res = r.query(a, b)
            want = [row for row in full if a <= row.ts_ns < b]
            assert [row.ts_ns for row in res.rows] == [row.ts_ns for row in want]
            assert [row.value for row in res.rows] == [row.value for row in want]
        # 窄窗口实际读取块数远小于总块数
        res = r.query(EPOCH + 100_000_000, EPOCH + 110_000_000)
        assert 0 < res.blocks_read <= 2
        assert res.blocks_total == 20
        # 空窗口不读块或只读边界块
        res = r.query(EPOCH + 5_000_000_000, EPOCH + 6_000_000_000)
        assert res.rows == ()
    rep = verify_archive(out, db)
    assert rep.ok, rep.violations


# ----------------------------------------------------------------------
# 8. 空选择 / 非法参数
# ----------------------------------------------------------------------

def test_empty_selection(tmp_path):
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += meas(40, 1, 0, [100], [1])
    db = ingest(tmp_path, [stream])
    out = tmp_path / "empty.tpla"
    # 范围落在所有样本之前
    stats = build(db, out, 40, 0.05, t0=EPOCH - 9_000_000_000,
                  t1=EPOCH - 1_000_000_000)
    assert stats.points == 0 and stats.segments == 0 and stats.blocks == 0
    with ArchiveReader(out) as r:
        assert list(r.decode()) == []
        s = r.summary()
        assert s.points == 0 and s.value_param_ratio is None
        res = r.query(EPOCH - 5_000_000_000, EPOCH)
        assert res.rows == () and res.blocks_read == 0
    rep = verify_archive(out, db)
    assert rep.ok and rep.points_checked == 0


def test_invalid_epsilon(tmp_path):
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += meas(40, 1, 0, [100], [1])
    db = ingest(tmp_path, [stream])
    for bad in (0.0, -1.0, float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError, match="epsilon"):
            build(db, tmp_path / "x.tpla", 40, bad)


def test_invalid_range(tmp_path):
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += meas(40, 1, 0, [100], [1])
    db = ingest(tmp_path, [stream])
    with pytest.raises(ValueError, match="时间范围"):
        build(db, tmp_path / "x.tpla", 40, 0.05, t0=EPOCH, t1=EPOCH)
    with pytest.raises(ValueError, match="时间范围"):
        build(db, tmp_path / "x.tpla", 40, 0.05, t0=EPOCH + 1, t1=EPOCH)


# ----------------------------------------------------------------------
# 9. 损坏块 / 截断文件 / 篡改模型
# ----------------------------------------------------------------------

def _block_offsets(path: Path):
    """独立解析文件尾与索引，返回各块 (offset, len)。"""
    raw = path.read_bytes()
    ioff, ilen, _doff, _dlen, _c, _t = F.FOOTER.unpack(raw[-F.FOOTER_LEN:])
    body = raw[ioff:ioff + ilen]
    _m, n = F.INDEX_HEAD.unpack(body[:F.INDEX_HEAD.size])
    out = []
    pos = F.INDEX_HEAD.size
    for _ in range(n):
        _g, _t0, _t1, off, blen = F.INDEX_ENTRY.unpack(body[pos:pos + F.INDEX_ENTRY_LEN])
        out.append((off, blen))
        pos += F.INDEX_ENTRY_LEN
    return out


def test_corrupt_block_detected(tmp_path):
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += b"".join(meas(40, 1, k, [100 + k], [1]) for k in range(200))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "c.tpla"
    build(db, out, 40, 0.05, block_points=32)
    blocks = _block_offsets(out)
    assert len(blocks) == 7
    # 翻转第 4 块点区一个字节
    raw = bytearray(out.read_bytes())
    off, blen = blocks[3]
    raw[off + blen - 6] ^= 0xFF
    bad = tmp_path / "c_bad.tpla"
    bad.write_bytes(bytes(raw))
    with ArchiveReader(bad) as r:
        with pytest.raises(F.ArchiveError, match="CRC"):
            list(r.decode())                      # 完整解码必须报错
        # 不触碰坏块的窄查询仍可用（它不声称覆盖全文件）
        res = r.query(EPOCH, EPOCH + 10_000_000)
        assert len(res.rows) > 0 and res.blocks_read < len(blocks)
        # 触碰坏块的查询必须报错
        with pytest.raises(F.ArchiveError, match="CRC"):
            r.query(EPOCH + 96_000_000, EPOCH + 130_000_000)
        # 摘要只读头/索引/目录，仍可读
        assert r.summary().points == 200


def test_truncated_file_detected(tmp_path):
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += b"".join(meas(40, 1, k, [100 + k], [1]) for k in range(50))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "tr.tpla"
    build(db, out, 40, 0.05, block_points=16)
    raw = out.read_bytes()
    for cut in (len(raw) - 1, len(raw) * 3 // 4, 40):
        bad = tmp_path / "tr_bad.tpla"
        bad.write_bytes(raw[:cut])
        with pytest.raises(F.ArchiveError):
            with ArchiveReader(bad) as r:
                list(r.decode())


def test_corrupt_header_and_index_detected(tmp_path):
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += meas(40, 1, 0, [100], [1])
    db = ingest(tmp_path, [stream])
    out = tmp_path / "h.tpla"
    build(db, out, 40, 0.05)
    raw = bytearray(out.read_bytes())
    bad = tmp_path / "h_bad.tpla"
    raw[10] ^= 0xFF                               # 文件头字段
    bad.write_bytes(bytes(raw))
    with pytest.raises(F.ArchiveError):
        ArchiveReader(bad)
    raw = bytearray(out.read_bytes())
    ioff, _ilen, _d, _l, _c, _t = F.FOOTER.unpack(bytes(raw[-F.FOOTER_LEN:]))
    raw[ioff + 3] ^= 0xFF                         # 索引区
    bad.write_bytes(bytes(raw))
    with pytest.raises(F.ArchiveError):
        ArchiveReader(bad)


def test_verify_catches_tampered_model(tmp_path):
    """把某段 slope 改大并**重算块 CRC**（结构合法但数值错误）：
    核验器必须报出逐点误差违规，而不是被文件蒙混过去。"""
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += b"".join(meas(40, 1, k, [100 + k], [1]) for k in range(200))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "m.tpla"
    build(db, out, 40, 0.05, block_points=512)     # 单块单段
    raw = bytearray(out.read_bytes())
    # 块 0 紧跟 64B 文件头；块头 40B 之后是第一个部件，slope 在部件内偏移 32
    part_off = F.HEADER_LEN + F.BLOCK_HEAD_LEN
    slope_off = part_off + 32
    slope = struct.unpack_from("<d", raw, slope_off)[0]
    struct.pack_into("<d", raw, slope_off, slope * 10.0 + 1e-9)
    # 重算该块 CRC，使文件结构仍然合法
    _m, blen = struct.unpack_from("<II", raw, F.HEADER_LEN)
    body = bytes(raw[F.HEADER_LEN:F.HEADER_LEN + blen - 4])
    struct.pack_into("<I", raw, F.HEADER_LEN + blen - 4, crc(body))
    bad = tmp_path / "m_bad.tpla"
    bad.write_bytes(bytes(raw))
    list(decode_all(bad))                          # 结构合法：解码不报错
    rep = verify_archive(bad, db)
    assert not rep.ok                              # 但逐点误差核验必须抓住
    assert any("误差" in v for v in rep.violations)


# ----------------------------------------------------------------------
# 10. 定位键回溯、摘要口径、解码器不依赖数据库
# ----------------------------------------------------------------------

def test_locators_trace_back_to_db(tmp_path):
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += b"".join(meas(40, 1, k, [100 + k], [1]) for k in range(100))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "l.tpla"
    build(db, out, 40, 0.05, block_points=16)
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    try:
        for r in decode_all(out):
            row = con.execute(
                "SELECT ts_ns, value, unit FROM tele_samples "
                "WHERE file_id = ? AND frame_offset = ? AND ordinal = ?",
                [r.file_id, r.frame_offset, r.ordinal]).fetchone()
            assert row is not None                # 定位键能回溯到库中原行
            assert row[0] == r.ts_ns
            assert abs(row[1] - r.value) <= 0.05
            assert row[2] == r.unit
    finally:
        con.close()


def test_summary_accounting(tmp_path):
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += b"".join(meas(40, 1, k, [100 + k], [1]) for k in range(300))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "s.tpla"
    build(db, out, 40, 0.05, block_points=32)
    with ArchiveReader(out) as r:
        s = r.summary()
    assert s.points == 300 and s.segments == 1 and s.blocks == 10
    assert s.param_bytes == 24 * s.parts == 24 * 10
    assert s.point_bytes == 22 * 300              # 时刻+定位键单列，不算收益
    assert s.raw_value_bytes == 8 * 300
    assert s.value_param_ratio == pytest.approx(240 / 2400)
    # 文件字节 = 各数据块 + 头/索引/目录/尾
    blocks_b = sum(blen for _off, blen in _block_offsets(out))
    assert s.file_bytes == blocks_b + s.meta_bytes
    assert s.file_bytes == out.stat().st_size


def test_decoder_needs_only_archive(tmp_path):
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += b"".join(meas(40, 1, k, [100 + k], [1]) for k in range(50))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "d.tpla"
    build(db, out, 40, 0.05)
    db.unlink()                                    # 数据库删除后解码照常
    rows = decode_all(out)
    assert len(rows) == 50
    assert rows[0].ts_ns == EPOCH and rows[-1].ts_ns == EPOCH + 49_000_000


# ----------------------------------------------------------------------
# 11. CLI 端到端
# ----------------------------------------------------------------------

def test_cli_build_summary_verify(tmp_path):
    stream = boot(40, 1, EPOCH, 1000) + layout(40, 1, CH0_C)
    stream += b"".join(meas(40, 1, k, [100 + k], [1]) for k in range(100))
    db = ingest(tmp_path, [stream])
    out = tmp_path / "cli.tpla"
    rc = archcli_main(["build", "--db", str(db), "--device", "40",
                       "--channel", "0", "--start-ns", str(T0),
                       "--end-ns", str(T1), "--epsilon", "0.05",
                       "--out", str(out), "--block-points", "16"])
    assert rc == 0
    assert archcli_main(["summary", "--file", str(out)]) == 0
    assert archcli_main(["verify", "--file", str(out), "--db", str(db)]) == 0
    assert archcli_main(["query", "--file", str(out),
                         "--start-ns", str(EPOCH), "--end-ns",
                         str(EPOCH + 10_000_000)]) == 0
    # 非法容差：退出码 2
    rc = archcli_main(["build", "--db", str(db), "--device", "40",
                       "--channel", "0", "--start-ns", str(T0),
                       "--end-ns", str(T1), "--epsilon", "nan",
                       "--out", str(tmp_path / "bad.tpla")])
    assert rc == 2
