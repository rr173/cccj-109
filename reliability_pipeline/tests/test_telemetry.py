"""telemetry 子系统测试。

预期材料独立编写：测试内直接用 struct.pack / zlib.crc32 手工拼帧，
期望值手工计算，不经过 telemetry.generator，以免与实现互相印证。
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

import pytest

from telemetry import store as store_mod
from telemetry.cli import _crash_loop, main as cli_main
from telemetry.ingest import Ingester, IngestError
from telemetry import generator

MAGIC = b"\xA7\x5C\xE1\x09"
EPOCH = 1_700_000_000_000_000_000


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


# 常用布局：ch0 int16 毫摄氏度(×0.5,+1.0)；ch1 int32 Pa(×2,-3)；ch2 float32 mm/s(×1,+0)
MIXED = [(0, 1, 0.5, 1.0, 1), (1, 2, 2.0, -3.0, 2), (2, 3, 1.0, 0.0, 3)]
MIXED_RT = [1, 2, 3]


def ingest_bytes(tmp_path: Path, data: bytes, chunk_size: int = 65536,
                 name: str = "stream.bin"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "t.duckdb"
    f = tmp_path / name
    f.write_bytes(data)
    with Ingester(db) as ing:
        stats = ing.ingest_file(f, chunk_size=chunk_size)
    return db, f, stats


def query(db, sql, params=None):
    con = store_mod.connect(db)
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def samples(db, where="1=1", params=None):
    return query(db, "SELECT device_id, boot_seq, layout_id, channel, tick, "
                     "cum_tick, ts_ns, raw_value, value, unit "
                     f"FROM tele_samples WHERE {where} "
                     "ORDER BY device_id, channel, cum_tick", params)


# ----------------------------------------------------------------------
# 1. 混合整数/浮点通道的单位换算与时刻
# ----------------------------------------------------------------------

def test_mixed_channels_unit_conversion(tmp_path):
    stream = (boot(7, 1, EPOCH, 1000) + layout(7, 5, MIXED)
              + meas(7, 5, 1000, [234, -1500, 12.5], MIXED_RT)
              + meas(7, 5, 2000, [-40, 200000, -0.25], MIXED_RT))
    db, _, st = ingest_bytes(tmp_path, stream)
    assert (st.frames_applied, st.frames_rejected, st.samples_added) == (4, 0, 6)
    rows = samples(db)
    # (channel, tick, cum_tick, ts_ns, raw, 期望值, 单位)
    exp = [
        (0, 1000, 1000, EPOCH + 1_000_000_000, 234.0, (234 * 0.5 + 1.0) * 1e-3, "°C"),
        (0, 2000, 2000, EPOCH + 2_000_000_000, -40.0, (-40 * 0.5 + 1.0) * 1e-3, "°C"),
        (1, 1000, 1000, EPOCH + 1_000_000_000, -1500.0, (-1500 * 2.0 - 3.0) * 1e-3, "kPa"),
        (1, 2000, 2000, EPOCH + 2_000_000_000, 200000.0, (200000 * 2.0 - 3.0) * 1e-3, "kPa"),
        (2, 1000, 1000, EPOCH + 1_000_000_000, 12.5, 12.5 * 1e-3, "m/s"),
        (2, 2000, 2000, EPOCH + 2_000_000_000, -0.25, -0.25 * 1e-3, "m/s"),
    ]
    assert len(rows) == len(exp)
    for row, (ch, tick, cum, ts, raw, val, unit) in zip(rows, exp):
        (dev, seq, lid, r_ch, r_tick, r_cum, r_ts, r_raw, r_val, r_unit) = row
        assert (dev, seq, lid) == (7, 1, 5)
        assert (r_ch, r_tick, r_cum, r_ts) == (ch, tick, cum, ts)
        assert r_raw == pytest.approx(raw)
        assert r_val == pytest.approx(val)
        assert r_unit == unit
    # 首个样本以其无符号 tick 为累计刻度：tick=1000 → cum_tick=1000
    assert rows[0][5] == 1000


# ----------------------------------------------------------------------
# 2. 两台设备交错
# ----------------------------------------------------------------------

def test_two_devices_interleaved(tmp_path):
    e9 = EPOCH + 5_000_000_000
    stream = (boot(7, 1, EPOCH, 1000) + boot(9, 3, e9, 250)
              + layout(7, 5, MIXED)
              + layout(9, 1, [(0, 3, 1.0, 0.0, 3), (4, 1, 0.25, 0.0, 1)])
              + meas(7, 5, 1, [1, 2, 3.0], MIXED_RT)
              + meas(9, 1, 1, [2.5, 8], [3, 1])
              + meas(7, 5, 2, [4, 5, 6.0], MIXED_RT)
              + meas(9, 1, 2, [5.0, 9], [3, 1]))
    db, _, st = ingest_bytes(tmp_path, stream)
    assert st.frames_applied == 8
    r7 = samples(db, "device_id = 7")
    r9 = samples(db, "device_id = 9")
    assert len(r7) == 6 and len(r9) == 4
    # 各自独立的计时周期
    assert {r[6] for r in r7} == {EPOCH + 1_000_000, EPOCH + 2_000_000}
    assert {r[6] for r in r9} == {e9 + 4_000_000, e9 + 8_000_000}
    assert {r[1] for r in r7} == {1} and {r[1] for r in r9} == {3}
    # 设备状态各自推进
    states = query(db, "SELECT device_id, boot_seq, tick_hz, last_raw_tick "
                       "FROM tele_device_state ORDER BY device_id")
    assert states == [(7, 1, 1000, 2), (9, 3, 250, 2)]


# ----------------------------------------------------------------------
# 3. tick 回绕与旧包
# ----------------------------------------------------------------------

def test_tick_wraparound_and_stale(tmp_path):
    hz = 10_000_000
    lay = [(0, 1, 1.0, 0.0, 1)]
    m = 0xFFFFFFFF
    stream = (boot(3, 1, EPOCH, hz) + layout(3, 2, lay)
              + meas(3, 2, m - 1, [1], [1])
              + meas(3, 2, m, [2], [1])
              + meas(3, 2, 0, [3], [1])          # 回绕
              + meas(3, 2, 1, [4], [1])
              + meas(3, 2, m - 2, [9], [1])      # 旧包：前进差 >= 2^31
              + meas(3, 2, 2, [5], [1]))
    db, _, st = ingest_bytes(tmp_path, stream)
    assert (st.frames_applied, st.frames_rejected) == (7, 1)
    rows = samples(db)
    cum = [r[5] for r in rows]
    assert cum == [m - 1, m, m + 1, m + 2, m + 3]     # 累计刻度越过 2^32
    tick_to_cum = {r[4]: r[5] for r in rows}
    assert tick_to_cum[0] == 1 << 32                  # 回绕后的 0 = 2^32
    # 时刻整数计算：epoch + floor(cum*1e9/hz)
    for r in rows:
        assert r[6] == EPOCH + (r[5] * 1_000_000_000) // hz
    rej = query(db, "SELECT reason, detail FROM tele_rejections")
    assert len(rej) == 1 and rej[0][0] == "stale_tick"
    assert "4294967293" in rej[0][1]                  # 前进差 2^32-3 >= 2^31


def test_tick_half_circle_boundary(tmp_path):
    """前进差恰好 2^31-1 可接纳，恰好 2^31 按旧包拒收。"""
    lay = [(0, 1, 1.0, 0.0, 1)]
    half = 1 << 31
    stream = (boot(3, 1, EPOCH, 1000) + layout(3, 2, lay)
              + meas(3, 2, 0, [1], [1])
              + meas(3, 2, half - 1, [2], [1])        # 差 2^31-1：接纳
              + meas(3, 2, (2 * half - 1) & 0xFFFFFFFF, [3], [1]))  # 差 2^31：拒收
    db, _, _ = ingest_bytes(tmp_path, stream)
    rows = samples(db)
    assert [r[5] for r in rows] == [0, half - 1]
    assert query(db, "SELECT reason FROM tele_rejections") == [("stale_tick",)]


# ----------------------------------------------------------------------
# 4. 重传：相同不重复入库，冲突说明原因
# ----------------------------------------------------------------------

def test_retransmit_duplicate_and_conflict(tmp_path):
    lay2 = [(0, 1, 2.0, 0.0, 1)]
    good = meas(5, 8, 100, [1, 2, 3.0], MIXED_RT)
    stream = (boot(5, 1, EPOCH, 1000) + layout(5, 8, MIXED) + layout(5, 9, lay2)
              + good + good                                   # 完全相同重传
              + meas(5, 8, 100, [1, 2, 4.0], MIXED_RT)        # 同 tick 内容冲突
              + meas(5, 9, 100, [7], [1])                     # 同 tick 布局不同
              + meas(5, 8, 101, [4, 5, 6.0], MIXED_RT))
    db, _, st = ingest_bytes(tmp_path, stream)
    assert (st.frames_applied, st.frames_duplicate, st.frames_rejected) == (5, 1, 2)
    rows = samples(db)
    assert len(rows) == 6          # tick=100 与 tick=101 各 3 通道，仅一次
    reasons = [r[0] for r in query(db, "SELECT reason FROM tele_rejections "
                                       "ORDER BY frame_offset")]
    assert reasons == ["tick_conflict", "tick_conflict"]
    disp = [r[0] for r in query(db, "SELECT disposition FROM tele_frames "
                                    "WHERE kind = 2 ORDER BY frame_offset")]
    assert disp == ["applied", "duplicate", "rejected", "rejected", "applied"]


# ----------------------------------------------------------------------
# 5. 布局：相同/冲突声明、非法声明
# ----------------------------------------------------------------------

def test_layout_redeclare_identical_and_conflict(tmp_path):
    good = layout(5, 8, MIXED)
    conflict = layout(5, 8, [(0, 1, 9.9, 0.0, 1), (1, 2, 2.0, -3.0, 2),
                             (2, 3, 1.0, 0.0, 3)])
    stream = (boot(5, 1, EPOCH, 1000) + good + good + conflict
              + meas(5, 8, 1, [100, 1000, 1.0], MIXED_RT))
    db, _, st = ingest_bytes(tmp_path, stream)
    assert (st.frames_applied, st.frames_duplicate, st.frames_rejected) == (3, 1, 1)
    assert query(db, "SELECT reason FROM tele_rejections") == [("layout_conflict",)]
    # 既有解释不被悄悄更换：ch0 仍按 gain=0.5 换算
    row = samples(db, "channel = 0")[0]
    assert row[8] == pytest.approx((100 * 0.5 + 1.0) * 1e-3)
    # 冲突声明没有入库
    assert query(db, "SELECT COUNT(*) FROM tele_layouts") == [(1,)]


def test_layout_invalid_declarations(tmp_path):
    dup_ch = layout(5, 1, [(0, 1, 1.0, 0.0, 1), (0, 2, 1.0, 0.0, 2)])   # 通道号重复
    nan_gain = layout(5, 2, [(0, 1, math.nan, 0.0, 1)])                 # 非有限校准
    inf_bias = layout(5, 3, [(0, 1, 1.0, math.inf, 1)])
    bad_raw = layout(5, 4, [(0, 9, 1.0, 0.0, 1)])                       # 未知 raw_type
    bad_unit = layout(5, 5, [(0, 1, 1.0, 0.0, 9)])                      # 未知 unit
    short = frame(1, struct.pack("<HHB", 5, 6, 2) + b"\x00" * 19)       # 长度不符
    stream = (boot(5, 1, EPOCH, 1000) + dup_ch + nan_gain + inf_bias
              + bad_raw + bad_unit + short + layout(5, 7, [(0, 1, 1.0, 0.0, 1)])
              + meas(5, 7, 1, [3], [1]))
    db, _, st = ingest_bytes(tmp_path, stream)
    assert st.frames_rejected == 6
    assert query(db, "SELECT COUNT(*) FROM tele_layouts") == [(1,)]     # 仅 layout 7
    assert query(db, "SELECT COUNT(*) FROM tele_samples") == [(1,)]
    assert set(r[0] for r in query(db, "SELECT reason FROM tele_rejections")) == {
        "invalid_layout"}


def test_layout_before_boot_and_meas_before_layout(tmp_path):
    stream = (layout(5, 1, [(0, 1, 1.0, 0.0, 1)])     # 设备未启动
              + meas(5, 1, 1, [3], [1])               # 设备未启动
              + boot(5, 1, EPOCH, 1000)
              + meas(5, 1, 1, [3], [1])               # 布局未知
              + layout(5, 1, [(0, 1, 1.0, 0.0, 1)])
              + meas(5, 1, 1, [3], [1]))              # 终于合法
    db, _, st = ingest_bytes(tmp_path, stream)
    reasons = [r[0] for r in query(db, "SELECT reason FROM tele_rejections "
                                       "ORDER BY frame_offset")]
    assert reasons == ["unknown_device", "unknown_device", "unknown_layout"]
    assert query(db, "SELECT COUNT(*) FROM tele_samples") == [(1,)]


# ----------------------------------------------------------------------
# 6. 启动锚点语义
# ----------------------------------------------------------------------

def test_boot_semantics(tmp_path):
    lay = [(0, 1, 1.0, 0.0, 1)]
    stream = (boot(5, 0, EPOCH, 0)                          # hz=0 非法
              + boot(5, 1, EPOCH, 1000)
              + boot(5, 1, EPOCH, 1000)                     # 相同：重复
              + boot(5, 1, EPOCH + 1, 1000)                 # 同 seq 参数不同：冲突
              + layout(5, 1, lay)
              + meas(5, 1, 100, [1], [1])
              + boot(5, 0, EPOCH, 1000)                     # 旧 seq：拒收
              + boot(5, 2, EPOCH + 10**9, 500)              # 新周期
              + meas(5, 1, 7, [2], [1])                     # 旧周期布局不可见
              + layout(5, 1, lay)                           # 新周期重新声明
              + meas(5, 1, 7, [2], [1]))                    # 新周期首样本：cum=7
    db, _, st = ingest_bytes(tmp_path, stream)
    reasons = [r[0] for r in query(db, "SELECT reason FROM tele_rejections "
                                       "ORDER BY frame_offset")]
    assert reasons == ["invalid_boot", "boot_conflict", "stale_boot", "unknown_layout"]
    rows = samples(db)
    # 旧周期样本保持原解释（cum=100, epoch=EPOCH）；新周期首样本 cum=7
    # （samples 按 cum_tick 排序：7 在 100 之前）
    assert [(r[1], r[4], r[5], r[6]) for r in rows] == [
        (2, 7, 7, EPOCH + 10**9 + 14_000_000),
        (1, 100, 100, EPOCH + 100_000_000),
    ]
    # 当前状态指向新周期
    assert query(db, "SELECT boot_seq, tick_hz, last_raw_tick FROM tele_device_state") \
        == [(2, 500, 7)]


# ----------------------------------------------------------------------
# 7. CRC 损坏后的重新同步 / 垃圾 / 非法候选
# ----------------------------------------------------------------------

def test_crc_corruption_resync(tmp_path):
    lay = [(0, 2, 1.0, 0.0, 2)]
    good1 = meas(2, 4, 10, [100], [2])
    good2 = meas(2, 4, 20, [200], [2])
    good3 = meas(2, 4, 30, [300], [2])
    corrupted = good2[:-1] + bytes([good2[-1] ^ 0xFF])          # CRC 末字节取反
    bad_kind = frame(9, b"\x01\x02\x03")                        # kind 非法
    bad_len = MAGIC + bytes([1]) + struct.pack("<H", 5000) + b"x" * 20  # 长度越界
    garbage = b"\x00\x11\x22\xa7\x5c" + b"\x99" * 11            # 垃圾 + 半个魔数
    stream = (boot(2, 1, EPOCH, 1000) + layout(2, 4, lay) + good1
              + garbage + corrupted + bad_kind + bad_len + good3)
    db, _, st = ingest_bytes(tmp_path, stream)
    assert st.frames_applied == 4                                # boot/layout/2 测量
    rows = samples(db)
    assert [r[4] for r in rows] == [10, 30]                      # tick=20 的包丢失
    reasons = sorted(r[0] for r in query(db, "SELECT reason FROM tele_rejections"))
    assert reasons == ["crc_mismatch", "invalid_kind", "invalid_length"]
    # 损坏帧的偏移可追溯
    off = query(db, "SELECT frame_offset FROM tele_rejections "
                    "WHERE reason = 'crc_mismatch'")[0][0]
    assert stream[off:off + 4] == MAGIC


def test_magic_bytes_inside_payload_not_a_boundary(tmp_path):
    lay = [(0, 2, 1.0, 0.0, 2), (1, 3, 1.0, 0.0, 3)]
    # int32 原始值 0x09E15CA7 的小端字节即帧头 A7 5C E1 09
    m1 = meas(2, 4, 30, [0x09E15CA7, 3.0], [2, 3])
    assert MAGIC in m1[11:-4]                                    # 魔数在 payload 内
    stream = (boot(2, 1, EPOCH, 1000) + layout(2, 4, lay) + m1
              + meas(2, 4, 40, [400, 4.0], [2, 3]))
    db, _, st = ingest_bytes(tmp_path, stream)
    assert st.frames_applied == 4 and st.anomalies == 0
    rows = samples(db)
    assert len(rows) == 4
    assert rows[0][7] == float(0x09E15CA7)                       # 原值完整解码
    assert rows[0][8] == pytest.approx(0x09E15CA7 * 1e-3)


# ----------------------------------------------------------------------
# 8. 分块无关性（含逐字节）与尾帧补齐
# ----------------------------------------------------------------------

def _rich_stream() -> bytes:
    lay = [(0, 2, 1.0, 0.0, 2), (1, 3, 1.0, 0.0, 3)]
    good = meas(2, 4, 20, [200, 2.0], [2, 3])
    corrupted = good[:-2] + bytes([good[-2] ^ 0x01]) + good[-1:]
    return (boot(2, 1, EPOCH, 1000) + b"\xde\xad\xbe\xef" + layout(2, 4, lay)
            + meas(2, 4, 10, [100, 1.0], [2, 3]) + b"\xa7\x5c" + corrupted
            + meas(2, 4, 30, [0x09E15CA7, 3.0], [2, 3])
            + meas(2, 4, 40, [400, 4.0], [2, 3]))


def _dump(db, table):
    return query(db, f"SELECT * FROM {table} ORDER BY 1, 2, 3, 4, 5, 6")


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 64, 4096, 1 << 20])
def test_chunk_size_invariance(tmp_path, chunk_size):
    data = _rich_stream()
    db, _, _ = ingest_bytes(tmp_path, data, chunk_size=chunk_size,
                            name=f"s{chunk_size}.bin")
    got = (_dump(db, "tele_samples"), _dump(db, "tele_rejections"),
           _dump(db, "tele_frames"))
    ref_db, _, _ = ingest_bytes(tmp_path / "ref", data, name="ref.bin")
    ref = (_dump(ref_db, "tele_samples"), _dump(ref_db, "tele_rejections"),
           _dump(ref_db, "tele_frames"))
    # file_id 不同（各自库内唯一），其余字段应一致
    def norm(dump):
        return [[row[1:] for row in tbl] for tbl in dump]
    assert norm(got) == norm(ref)


def test_tail_frame_completion(tmp_path):
    data = _rich_stream()
    part1, part2 = data[:-3], data[-3:]
    db = tmp_path / "t.duckdb"
    f = tmp_path / "tail.bin"
    f.write_bytes(part1)
    with Ingester(db) as ing:
        st1 = ing.ingest_file(f, chunk_size=5)
        assert st1.pending_bytes > 0                     # 末帧待续
        n1 = query(db, "SELECT COUNT(*) FROM tele_samples")[0][0]
        with open(f, "ab") as fh:                        # 补齐尾字节
            fh.write(part2)
        st2 = ing.ingest_file(f, chunk_size=5)
        assert st2.frames_applied == 1                   # 只产出应有的那一包
        assert st2.pending_bytes == 0
    n2 = query(db, "SELECT COUNT(*) FROM tele_samples")[0][0]
    assert n2 - n1 == 2                                  # 该包两个通道
    # 与一次性接入结果一致
    ref_db, _, _ = ingest_bytes(tmp_path / "ref", data, name="r.bin")
    assert [r[1:] for r in _dump(db, "tele_samples")] == \
           [r[1:] for r in _dump(ref_db, "tele_samples")]


def test_append_batches_equal_one_shot(tmp_path):
    data = _rich_stream()
    cuts = [len(data) // 3, 2 * len(data) // 3, len(data)]
    db = tmp_path / "t.duckdb"
    f = tmp_path / "app.bin"
    with Ingester(db) as ing:
        prev = 0
        for end in cuts:
            with open(f, "ab") as fh:
                fh.write(data[prev:end])
            ing.ingest_file(f, chunk_size=13)
            prev = end
    ref_db, _, _ = ingest_bytes(tmp_path / "ref", data, name="r.bin")
    assert [r[1:] for r in _dump(db, "tele_samples")] == \
           [r[1:] for r in _dump(ref_db, "tele_samples")]
    # 再跑一遍无新数据：已有样本不重入
    with Ingester(db) as ing:
        st = ing.ingest_file(f)
    assert st.samples_added == 0 and st.frames_applied == 0


# ----------------------------------------------------------------------
# 9. 崩溃恢复：子进程在提交进度附近被 SIGKILL
# ----------------------------------------------------------------------

def test_crash_recovery_no_loss_no_duplicates(tmp_path):
    count = 600
    data = generator.build_scenario("crash", seed=3, count=count)["crash.bin"]
    f = tmp_path / "crash.bin"
    f.write_bytes(data)
    db_live = tmp_path / "live.duckdb"
    attempts = _crash_loop(str(db_live), f, chunk_size=1024, commit_every=50,
                           seed=5, verbose=False)
    assert attempts >= 2                                   # 确实发生过崩溃续传
    # 参照：同输入一次性干净入库
    db_ref = tmp_path / "ref.duckdb"
    with Ingester(db_ref) as ing:
        ing.ingest_file(f, chunk_size=1024)
    live = query(db_live, "SELECT device_id, boot_seq, layout_id, channel, tick, "
                          "cum_tick, ts_ns, raw_value, value, unit FROM tele_samples "
                          "ORDER BY device_id, boot_seq, channel, cum_tick")
    ref = query(db_ref, "SELECT device_id, boot_seq, layout_id, channel, tick, "
                        "cum_tick, ts_ns, raw_value, value, unit FROM tele_samples "
                        "ORDER BY device_id, boot_seq, channel, cum_tick")
    assert live == ref
    assert len(live) == len(set(live))                     # 无重复样本
    # 独立手算期望：count 包交替两设备（3 通道 / 2 通道），重传不入库
    expect = (count // 2) * 3 + (count - count // 2) * 2
    assert len(live) == expect
    # 计数器状态与干净运行一致
    assert query(db_live, "SELECT device_id, last_cum_tick FROM tele_device_state "
                          "ORDER BY device_id") == \
           query(db_ref, "SELECT device_id, last_cum_tick FROM tele_device_state "
                         "ORDER BY device_id")


# ----------------------------------------------------------------------
# 10. 前缀改写 / 文件替换：明确拒绝续接
# ----------------------------------------------------------------------

def test_prefix_rewrite_refused(tmp_path):
    data = _rich_stream()
    db = tmp_path / "t.duckdb"
    f = tmp_path / "x.bin"
    f.write_bytes(data)
    with Ingester(db) as ing:
        ing.ingest_file(f)
    n0 = query(db, "SELECT COUNT(*) FROM tele_samples")[0][0]

    # 改写已处理前缀中的一个字节
    mutated = bytearray(data)
    mutated[20] ^= 0xFF
    f.write_bytes(bytes(mutated) + b"\x00" * 8)
    with Ingester(db) as ing, pytest.raises(IngestError, match="前缀"):
        ing.ingest_file(f)
    assert query(db, "SELECT COUNT(*) FROM tele_samples")[0][0] == n0

    # 整个文件换成另一条合法流
    f.write_bytes(_rich_stream()[:50] + b"\x01\x02")
    with Ingester(db) as ing, pytest.raises(IngestError):
        ing.ingest_file(f)

    # 截断到进度之下
    f.write_bytes(data[:10])
    with Ingester(db) as ing, pytest.raises(IngestError):
        ing.ingest_file(f)

    # CLI 层：退出码 3
    rc = cli_main(["ingest", "--db", str(db), "--file", str(f)])
    assert rc == 3


# ----------------------------------------------------------------------
# 11. NaN/Inf 与非法负载
# ----------------------------------------------------------------------

def test_nan_inf_and_bad_length_rejected(tmp_path):
    lay = [(0, 2, 1.0, 0.0, 2), (1, 3, 1.0, 0.0, 3)]
    nan_meas = meas(2, 4, 10, [1, math.nan], [2, 3])
    inf_meas = meas(2, 4, 11, [2, math.inf], [2, 3])
    long_meas = frame(2, struct.pack("<HHI", 2, 4, 12) + b"\x00" * 9)   # 多 1 字节
    short_meas = frame(2, struct.pack("<HHI", 2, 4, 13) + b"\x00" * 7)  # 少 1 字节
    stream = (boot(2, 1, EPOCH, 1000) + layout(2, 4, lay)
              + nan_meas + inf_meas + long_meas + short_meas
              + meas(2, 4, 14, [3, 3.0], [2, 3]))
    db, _, st = ingest_bytes(tmp_path, stream)
    reasons = [r[0] for r in query(db, "SELECT reason FROM tele_rejections "
                                       "ORDER BY frame_offset")]
    assert reasons == ["nonfinite_value", "nonfinite_value",
                       "bad_payload_length", "bad_payload_length"]
    rows = samples(db)
    # 整包拒收：NaN 包的 int32 通道也没有入库；坏包不推进时间状态
    assert [r[4] for r in rows] == [14, 14]
    assert query(db, "SELECT last_raw_tick FROM tele_device_state") == [(14,)]


# ----------------------------------------------------------------------
# 12. 查询展示：原始偏移回溯 + 时间段/通道筛选
# ----------------------------------------------------------------------

def test_trace_and_researcher_queries(tmp_path):
    stream = (boot(7, 1, EPOCH, 1000) + layout(7, 5, MIXED)
              + meas(7, 5, 100, [100, 1000, 1.5], MIXED_RT)
              + meas(7, 5, 500, [234, -1500, 12.5], MIXED_RT)
              + meas(7, 5, 1000, [-40, 200000, -0.25], MIXED_RT))
    db, _, _ = ingest_bytes(tmp_path, stream)
    # 原始偏移 → 测量值
    fid, off = query(db, "SELECT file_id, frame_offset FROM tele_samples "
                         "WHERE device_id = 7 AND tick = 500 LIMIT 1")[0]
    traced = query(db, "SELECT channel, raw_value, value, unit FROM tele_samples "
                       "WHERE file_id = ? AND frame_offset = ? ORDER BY ordinal",
                   [fid, off])
    assert len(traced) == 3
    assert traced[0][1] == 234.0 and traced[0][2] == pytest.approx(0.118)
    assert traced[0][3] == "°C" and traced[1][3] == "kPa" and traced[2][3] == "m/s"
    # 研究人员筛选：设备 7、通道 0、[epoch+400ms, epoch+1s)
    rows = query(db, "SELECT tick, ts_ns, value FROM tele_samples "
                     "WHERE device_id = 7 AND channel = 0 "
                     "AND ts_ns >= ? AND ts_ns < ? ORDER BY ts_ns",
                 [EPOCH + 400_000_000, EPOCH + 1_000_000_000])
    assert [r[0] for r in rows] == [500]
    # 时刻视图（make_timestamp_ns）与 ts_ns 一致
    ts = query(db, "SELECT epoch_ns(make_timestamp_ns(ts_ns)) FROM tele_samples "
                   "WHERE device_id = 7 AND tick = 500 LIMIT 1")
    assert ts == [(EPOCH + 500_000_000,)]


# ----------------------------------------------------------------------
# 13. 生成器可复现性
# ----------------------------------------------------------------------

def test_generator_deterministic():
    for name in generator.SCENARIOS:
        a = generator.build_scenario(name, seed=42, count=300)
        b = generator.build_scenario(name, seed=42, count=300)
        assert a == b
    assert generator.build_scenario("corrupt", seed=1) != \
        generator.build_scenario("corrupt", seed=2)


# ----------------------------------------------------------------------
# 14. 帧长边界、原始值边界、零通道布局
# ----------------------------------------------------------------------

def test_max_payload_frame_accepted(tmp_path):
    """payload 恰好 4096 字节的帧合法；4097 即 invalid_length。"""
    lay = [(0, 1, 1.0, 0.0, 1)]
    big_payload = struct.pack("<HHI", 3, 2, 5) + b"\x00" * (4096 - 8)
    big = frame(2, big_payload)                       # 恰好 4096：合法帧
    too_big = MAGIC + bytes([2]) + struct.pack("<H", 4097) + b"\x00" * 50
    stream = boot(3, 1, EPOCH, 1000) + layout(3, 2, lay) + big + too_big
    db, _, st = ingest_bytes(tmp_path, stream)
    # 大帧是合法帧（长度不符布局 → bad_payload_length），不是 invalid_length
    reasons = [r[0] for r in query(db, "SELECT reason FROM tele_rejections "
                                       "ORDER BY frame_offset")]
    assert reasons == ["bad_payload_length", "invalid_length"]


def test_raw_value_extremes_and_zero_channel_layout(tmp_path):
    lay = [(0, 1, 1.0, 0.0, 1), (1, 2, 1.0, 0.0, 2)]
    stream = (boot(6, 1, EPOCH, 1000) + layout(6, 1, lay)
              + meas(6, 1, 1, [-32768, 2147483647], [1, 2])
              + layout(6, 2, [])                       # 零通道布局
              + meas(6, 2, 2, [], []))                 # 无值包：推进 tick
    db, _, st = ingest_bytes(tmp_path, stream)
    rows = samples(db)
    assert rows[0][7] == -32768.0 and rows[0][8] == pytest.approx(-32.768)
    assert rows[1][7] == 2147483647.0
    assert rows[1][8] == pytest.approx(2147483647 * 1e-3)
    # 零通道包被接纳（0 行样本）且推进 tick
    assert st.frames_applied == 5
    assert query(db, "SELECT last_raw_tick FROM tele_device_state") == [(2,)]


# ----------------------------------------------------------------------
# 15. 同一设备的帧可跨文件到达（设备时间线全局推进）
# ----------------------------------------------------------------------

def test_device_frames_across_files(tmp_path):
    lay = [(0, 1, 1.0, 0.0, 1)]
    f1 = boot(8, 1, EPOCH, 1000) + layout(8, 1, lay) + meas(8, 1, 1, [10], [1])
    f2 = meas(8, 1, 2, [20], [1])                       # 无锚点：依赖已有状态
    db = tmp_path / "t.duckdb"
    (tmp_path / "a.bin").write_bytes(f1)
    (tmp_path / "b.bin").write_bytes(f2)
    with Ingester(db) as ing:
        ing.ingest_file(tmp_path / "a.bin")
        st2 = ing.ingest_file(tmp_path / "b.bin")
    assert st2.frames_applied == 1
    rows = samples(db)
    assert [r[5] for r in rows] == [1, 2]               # 累计刻度跨文件连续


# ----------------------------------------------------------------------
# 16. CLI 冒烟
# ----------------------------------------------------------------------

def test_cli_smoke(tmp_path, capsys):
    data = _rich_stream()
    f = tmp_path / "s.bin"
    f.write_bytes(data)
    db = str(tmp_path / "cli.duckdb")
    assert cli_main(["ingest", "--db", db, "--file", str(f),
                     "--chunk-size", "3"]) == 0
    assert cli_main(["samples", "--db", db, "--device", "2",
                     "--channel", "0"]) == 0
    assert cli_main(["frames", "--db", db]) == 0
    assert cli_main(["rejections", "--db", db]) == 0
    assert cli_main(["state", "--db", db]) == 0
    off = query(db, "SELECT frame_offset FROM tele_samples LIMIT 1")[0][0]
    assert cli_main(["trace", "--db", db, "--file-id", "1",
                     "--offset", str(off)]) == 0
    out = capsys.readouterr().out
    assert "crc_mismatch" in out and "°C" not in out      # 本流单位是 kPa/m/s
    assert "kPa" in out
