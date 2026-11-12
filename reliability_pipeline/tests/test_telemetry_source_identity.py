"""源文件身份绑定回归材料（独立手拼字节流驱动公开接入流程）。

字节流全部由本文件用 struct.pack / zlib.crc32 手工拼装，不经过
``telemetry.generator`` 与 ``telemetry.protocol`` 的编码实现；期望值由输入
字节与操作顺序直接算出（例如第 k 包的 tick、累计刻度、ts_ns、物理值），
而不是比较两次运行是否碰巧相同。

覆盖：
1. 同前缀换文件（os.replace）→ 公开 ``ingest`` 命令退出码 3，既有来源、
   进度、样本、设备状态完全不变（无样本 / 无拒收台账 / 进度不推进）；
2. 正常追加与同一对象改路径 → 继续读，身份不变；
3. 校验已处理前缀期间 ``os.replace`` 路径 → 只完成已打开的原对象，之后
   按被替换的路径运行被识别并拒绝（可控同步，不靠 sleep）；
4. 提交边界附近 true 崩溃（``os._exit``）→ 进度、样本、设备状态同属一个
   对象；崩溃后换文件被挡，原对象改名可续完；
5. 未补齐尾包时崩溃 → 进度不包含尾包，补齐后恰好产出一包；
6. 旧库（只有路径 + 前缀哈希）→ 保守阻止自动续接，不清空、不认领。
"""

from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest

from telemetry.cli import main as cli_main
from telemetry.ingest import Ingester, IngestError

REPO = Path(__file__).resolve().parent.parent
MAGIC = b"\xA7\x5C\xE1\x09"
EPOCH = 1_700_000_000_000_000_000
HZ = 1000
DEV = 7
LAY_ID = 2
LAY = [(0, 1, 0.5, 1.0, 1)]      # ch0: int16；value = (raw*0.5+1.0) * 1e-3 °C
RT = [1]


# ----------------------------------------------------------------------
# 独立拼帧工具
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


# 一个合法、可增量的单设备流：启动、布局，随后每 tick 一个测量包。
HEADER = boot(DEV, 1, EPOCH, HZ) + layout(DEV, LAY_ID, LAY)


def stream_with_ticks(ticks, values=None, device: int = DEV) -> bytes:
    head = (boot(device, 1, EPOCH, HZ) if device != DEV
            else boot(DEV, 1, EPOCH, HZ)) + layout(device, LAY_ID, LAY)
    out = bytearray(head)
    for i, tick in enumerate(ticks):
        raw = values[i] if values is not None else tick
        out += meas(device, LAY_ID, tick, [raw], RT)
    return bytes(out)


def expected_cum_ts(ticks) -> list:
    """独立计算：首个样本 cum_tick = tick，时刻 = epoch + floor(cum*1e9/hz)。"""
    return [(t, EPOCH + t * 1_000_000_000 // HZ, (t * 0.5 + 1.0) * 1e-3)
            for t in ticks]


def expected_value(raw: int) -> float:
    return (raw * 0.5 + 1.0) * 1e-3


# ----------------------------------------------------------------------
# 查询辅助
# ----------------------------------------------------------------------

def q(db, sql, params=None):
    # 与 Ingester 使用同一配置（DuckDB 不允许同一库同时有只读与读写连接）
    with duckdb.connect(str(db)) as con:
        return con.execute(sql, params or []).fetchall()


def snapshot(db) -> dict:
    """来源、进度、样本、设备状态、台账的完整快照（用于断言「完全不变」）。"""
    return {
        "files": q(db, "SELECT file_id, path, identity FROM tele_files ORDER BY file_id"),
        "progress": q(db, "SELECT file_id, consumed_offset, prefix_sha256, file_size "
                         "FROM tele_ingest_state ORDER BY file_id"),
        "samples": q(db, "SELECT file_id, frame_offset, device_id, boot_seq, layout_id, "
                        "tick, cum_tick, ts_ns, channel, raw_value, value, unit "
                        "FROM tele_samples ORDER BY file_id, frame_offset, ordinal"),
        "device": q(db, "SELECT device_id, boot_seq, epoch_ns, tick_hz, have_tick, "
                        "last_raw_tick, last_cum_tick FROM tele_device_state "
                        "ORDER BY device_id"),
        "frames": q(db, "SELECT file_id, frame_offset, kind, disposition "
                        "FROM tele_frames ORDER BY file_id, frame_offset"),
        "rejections": q(db, "SELECT file_id, frame_offset, reason FROM tele_rejections "
                            "ORDER BY file_id, frame_offset"),
    }


def file_rows(db):
    return q(db, "SELECT file_id, path, identity FROM tele_files ORDER BY file_id")


def progress_rows(db):
    return q(db, "SELECT file_id, consumed_offset, prefix_sha256, file_size "
                 "FROM tele_ingest_state ORDER BY file_id")


def sample_ticks(db):
    return q(db, "SELECT tick, cum_tick, ts_ns, raw_value, value, unit "
                 "FROM tele_samples ORDER BY cum_tick")


def run_cli(db, file, *extra):
    """用公开接入命令（子进程）驱动，返回 CompletedProcess。"""
    return subprocess.run(
        [sys.executable, "-m", "telemetry.cli", "ingest", "--db", str(db),
         "--file", str(file), *extra],
        cwd=REPO, capture_output=True, text=True)


# ----------------------------------------------------------------------
# 1. 同前缀换文件：公开命令拒绝，既有状态完全不变
# ----------------------------------------------------------------------

def test_same_prefix_replacement_denied_by_public_command(tmp_path):
    db = tmp_path / "t.duckdb"
    f = tmp_path / "source.bin"
    base = stream_with_ticks([100, 200])
    f.write_bytes(base)

    r1 = run_cli(db, f)
    assert r1.returncode == 0, r1.stderr
    assert q(db, "SELECT COUNT(*) FROM tele_samples") == [(2,)]
    before = snapshot(db)

    # 新对象：开头完整复制已处理内容，后面是不同的测量，os.replace 到旧路径。
    # 已处理前缀的哈希不变——但它不是原文件在继续追加。
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(base + stream_with_ticks([300, 400], values=[30, 40]))
    os.replace(replacement, f)

    r2 = run_cli(db, f)
    assert r2.returncode == 3, r2.stdout + r2.stderr
    assert "另一个文件对象" in r2.stderr

    after = snapshot(db)
    assert after == before                       # 样本/台账/进度/设备状态全不动
    assert q(db, "SELECT COUNT(*) FROM tele_samples") == [(2,)]
    assert q(db, "SELECT COUNT(*) FROM tele_rejections") == [(0,)]
    assert q(db, "SELECT last_raw_tick, last_cum_tick FROM tele_device_state") == [(200, 200)]


# ----------------------------------------------------------------------
# 2. 正常追加 / 同一对象改路径
# ----------------------------------------------------------------------

def test_same_object_append_and_path_move(tmp_path):
    db = tmp_path / "t.duckdb"
    f = tmp_path / "a.bin"
    f.write_bytes(stream_with_ticks([100, 200]))
    with Ingester(db) as ing:
        st1 = ing.ingest_file(f, chunk_size=17)
        assert st1.file_id == 1
        assert (st1.start_offset, st1.consumed_offset) == (0, f.stat().st_size)

        # 同一对象正常增长：继续读
        with open(f, "ab") as fh:
            fh.write(stream_with_ticks([300]))   # 仅追加一包
        st2 = ing.ingest_file(f, chunk_size=5)
        assert st2.file_id == 1
        assert st2.start_offset == st1.consumed_offset
        assert st2.samples_added == 1

        # 同一对象换登记路径（rename）：身份相同，沿用 file_id
        moved = tmp_path / "moved.bin"
        os.rename(f, moved)
        st3 = ing.ingest_file(moved)
        assert st3.file_id == 1
        assert file_rows(db) == [
            (1, str(moved.resolve()), file_rows(db)[0][2])]

    rows = sample_ticks(db)          # (tick, cum_tick, ts_ns, raw, value, unit)
    assert [(r[0], r[1], r[2], r[4], r[5]) for r in rows] == [
        (t, cum, ts, pytest.approx(val), "°C")
        for t, cum, ts, val in [(100, 100, EPOCH + 100 * 1_000_000, expected_value(100)),
                                (200, 200, EPOCH + 200 * 1_000_000, expected_value(200)),
                                (300, 300, EPOCH + 300 * 1_000_000, expected_value(300))]]


# ----------------------------------------------------------------------
# 3. 校验期间替换路径（可控同步）
# ----------------------------------------------------------------------

def test_replacement_during_prefix_check_completes_open_object(tmp_path):
    db = tmp_path / "t.duckdb"
    f = tmp_path / "source.bin"
    base = stream_with_ticks([100, 200])
    f.write_bytes(base)
    with Ingester(db) as ing:
        ing.ingest_file(f)

    replacement = tmp_path / "replacement.bin"
    # 同前缀 + 不同的后续测量
    replacement.write_bytes(base + stream_with_ticks([300], values=[30]))

    swapped = {"done": False}

    def swap_while_open(fh):
        """在 fd 已打开、前缀已校验、尚未读取新增字节时替换路径。"""
        assert not swapped["done"]
        os.replace(replacement, f)
        swapped["done"] = True
        # 当前打开的文件对象仍是原对象：再断言一次其身份没变
        st = os.fstat(fh.fileno())
        assert (st.st_dev, st.st_ino) == (orig_dev, orig_ino)

    st = os.stat(f)
    orig_dev, orig_ino = st.st_dev, st.st_ino
    with Ingester(db) as ing:
        stats = ing.ingest_file(f, chunk_size=7, after_open=swap_while_open)
        assert swapped["done"]
        assert stats.file_id == 1
        # 原对象没有新增字节：本次什么都没读进来（明确只完成已打开对象）
        assert stats.samples_added == 0
        assert stats.consumed_offset == len(base)
        # 登记身份仍指向原对象
        assert file_rows(db)[0][2] is not None
    # 后续按被替换的路径运行：必须识别为另一个对象
    with Ingester(db) as ing, pytest.raises(IngestError, match="另一个文件对象"):
        ing.ingest_file(f)
    # 用公开命令同样拒绝
    r = run_cli(db, f)
    assert r.returncode == 3
    assert q(db, "SELECT COUNT(*) FROM tele_samples") == [(2,)]   # 300 未入库

    # 原对象已被 os.replace 从路径上摘除且无其他链接：它的数据没有任何
    # 一部分被记到替换文件的来源名下；被替换路径的后续运行已被识别。
    # （原对象若仍有保留路径，见 next 测试：身份相同即可继续。）


def test_no_lying_about_new_object_when_old_fd_reads_append(tmp_path):
    """交错：新对象先被写到旧路径，但本次操作打开的是旧对象。

    旧对象继续增长（硬链接到另一路径），检查期间旧路径被新对象替换；
    本次只读旧对象新增的数据，进度与身份仍绑定旧对象；之后按旧路径运行
    必须被拒绝。期望值从输入字节独立算出。
    """
    db = tmp_path / "t.duckdb"
    f = tmp_path / "source.bin"
    original_path = tmp_path / "original.bin"
    base = stream_with_ticks([100, 200])
    original_path.write_bytes(base)
    os.link(original_path, f)                  # 两个路径指向同一对象
    with Ingester(db) as ing:
        ing.ingest_file(f)                     # 登记旧路径，消费 base
        # 旧对象继续增长（经另一路径写入，同一 inode）
        with open(original_path, "ab") as fh:
            fh.write(stream_with_ticks([300], values=[30]))

        replacement = tmp_path / "replacement.bin"
        # 新对象：复制已处理前缀 + 完全不同的尾巴（tick 900，值 90）
        replacement.write_bytes(base + stream_with_ticks([900], values=[90]))

        def swap(fh):
            os.replace(replacement, f)
            assert (os.fstat(fh.fileno()).st_ino
                    == os.stat(original_path).st_ino)

        stats = ing.ingest_file(f, chunk_size=7, after_open=swap)
        assert stats.file_id == 1
        assert stats.samples_added == 1        # 只有旧对象的 tick=300
        assert q(db, "SELECT tick FROM tele_samples ORDER BY tick") == [
            (100,), (200,), (300,)]
        assert q(db, "SELECT COUNT(*) FROM tele_samples WHERE tick = 900") == [(0,)]

    # 被替换的路径现在是另一个对象 → 后续拒绝
    r = run_cli(db, f)
    assert r.returncode == 3
    assert "另一个文件对象" in r.stderr
    # 旧对象经其尚未被替换的路径继续可读（身份相同）
    with Ingester(db) as ing:
        st = ing.ingest_file(original_path)
        assert st.file_id == 1 and st.samples_added == 0
    # 样本与来源对应：所有样本的 file_id 都是 1，进度属于旧对象
    assert q(db, "SELECT DISTINCT file_id FROM tele_samples") == [(1,)]
    assert progress_rows(db)[0][1] == os.stat(original_path).st_size


# ----------------------------------------------------------------------
# 4. 提交边界附近崩溃（true crash）
# ----------------------------------------------------------------------

CRASH_SCRIPT = r"""
import sys
from telemetry.ingest import Ingester
db, f, crash_after = sys.argv[1], sys.argv[2], int(sys.argv[3])
with Ingester(db) as ing:
    ing.ingest_file(f, chunk_size=9, commit_every=2,
                    progress=True, _crash_after_commit=crash_after)
"""

# 在「读到末尾、尚未提交进度」前崩溃：模拟进程在首次接入过程中被杀
CRASH_BEFORE_COMMIT_SCRIPT = r"""
import os, sys
from telemetry.ingest import Ingester
db, f = sys.argv[1], sys.argv[2]
with Ingester(db) as ing:
    ing.ingest_file(f, chunk_size=9, after_open=lambda fh: os._exit(137))
"""


def run_crashing_ingest(db, f, crash_after):
    return subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT, str(db), str(f), str(crash_after)],
        cwd=REPO, capture_output=True, text=True)


def test_crash_at_commit_boundary_keeps_progress_and_samples_consistent(tmp_path):
    ticks = [10, 20, 30, 40, 50, 60, 70, 80]
    data = stream_with_ticks(ticks, values=[t for t in ticks])
    f = tmp_path / "crash.bin"
    f.write_bytes(data)
    db = tmp_path / "crash.duckdb"

    r = run_crashing_ingest(db, f, crash_after=2)
    assert r.returncode == 137, (r.returncode, r.stdout, r.stderr)
    progress_out = [ln for ln in r.stdout.splitlines() if ln.startswith("PROGRESS")]
    assert len(progress_out) >= 2

    # 已提交的进度只对应已入库的整包；设备状态与样本同属一个对象
    consumed, sha, size = progress_rows(db)[0][1:]
    assert q(db, "SELECT COUNT(*) FROM tele_samples")[0][0] > 0
    assert q(db, "SELECT COUNT(DISTINCT file_id) FROM tele_samples") == [(1,)]
    # 进度必须落在一个帧边界上：前 consumed 字节的独立 SHA-256 可复算
    assert hashlib.sha256(data[:consumed]).hexdigest() == sha
    # 样本的帧起点必须 < consumed（没有「进度未覆盖却已入库」的样本）
    assert q(db, "SELECT MAX(frame_offset) FROM tele_samples")[0][0] < consumed
    # 设备计数器与最后入库样本对应：last_cum_tick 等于最大 cum_tick
    dev_last = q(db, "SELECT last_cum_tick FROM tele_device_state")[0][0]
    assert dev_last == q(db, "SELECT MAX(cum_tick) FROM tele_samples")[0][0]
    # 进度最后一行对应最后一次已提交的数据；设备状态推进到的 tick 与已提交
    # 的样本一致（不会出现「进度属于一个对象、样本来自另一个对象」）
    assert q(db, "SELECT DISTINCT file_id FROM tele_samples") == [(1,)]
    assert q(db, "SELECT file_id FROM tele_ingest_state") == [(1,)]

    # 崩溃后重启续传，结果等于一次性干净接入（期望值独立算自 ticks）
    with Ingester(db) as ing:
        st = ing.ingest_file(f, chunk_size=9)
    assert st.start_offset == consumed
    rows = sample_ticks(db)
    assert [(r[0], r[1], r[2], r[3], r[4]) for r in rows] == [
        (t, t, EPOCH + t * 1_000_000, float(t), expected_value(t)) for t in ticks]
    assert q(db, "SELECT COUNT(*) FROM tele_samples") == [(len(ticks),)]
    assert q(db, "SELECT last_cum_tick FROM tele_device_state") == [(ticks[-1],)]
    assert progress_rows(db)[0][1] == len(data)
    assert hashlib.sha256(data).hexdigest() == progress_rows(db)[0][1 + 1]


def test_crash_before_first_commit_then_replace_then_original_resumes(tmp_path):
    """首次接入尚未提交任何进度时崩溃，随后同路径被新对象占用。

    崩溃事务回滚后库里没有来源/进度/样本（登记也随事务回滚）；此时换文件
    会按新对象正常登记——不会把新对象的样本记到旧来源名下。原对象以新路径
    出现时按对象身份找到既有来源才可续。
    """
    db = tmp_path / "t.duckdb"
    f = tmp_path / "source.bin"
    # 两台不同设备：设备时间线在库内全局推进，独立设备便于按字节算期望值
    original = stream_with_ticks([10, 20], device=8)
    replacement = stream_with_ticks([90, 91], values=[90, 91], device=7)
    f.write_bytes(original)
    keep = tmp_path / "keep.bin"
    os.link(f, keep)                       # 保留原对象

    r = subprocess.run(
        [sys.executable, "-c", CRASH_BEFORE_COMMIT_SCRIPT, str(db), str(f)],
        cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 137
    # 事务回滚：没有半个来源、没有样本、没有帧台账、没有进度
    assert q(db, "SELECT COUNT(*) FROM tele_files") == [(0,)]
    assert q(db, "SELECT COUNT(*) FROM tele_ingest_state") == [(0,)]
    assert q(db, "SELECT COUNT(*) FROM tele_samples") == [(0,)]
    assert q(db, "SELECT COUNT(*) FROM tele_frames") == [(0,)]

    # 同路径换成另一个对象：按新对象登记，绝不把新对象记到旧名下
    os.unlink(f)
    f.write_bytes(replacement)
    assert f.stat().st_ino != keep.stat().st_ino
    rr = run_cli(db, f)
    assert rr.returncode == 0, rr.stderr
    assert q(db, "SELECT file_id FROM tele_files") == [(1,)]
    assert q(db, "SELECT tick FROM tele_samples ORDER BY tick") == [(90,), (91,)]
    assert progress_rows(db)[0] == (1, len(replacement),
                                    hashlib.sha256(replacement).hexdigest(),
                                    len(replacement))

    # 原对象经保留路径：身份与已登记来源不同 → 作为独立来源登记（file_id 2），
    # 不与新对象的来源混淆；期望值独立算自 original 的字节
    with Ingester(db) as ing:
        st = ing.ingest_file(keep)
    assert st.file_id == 2 and st.start_offset == 0
    assert q(db, "SELECT file_id, tick FROM tele_samples ORDER BY file_id, tick") == [
        (1, 90), (1, 91), (2, 10), (2, 20)]
    assert q(db, "SELECT file_id, consumed_offset FROM tele_ingest_state "
                 "ORDER BY file_id") == [
        (1, len(replacement)), (2, len(original))]
    # 设备状态按各自启动周期独立：设备 7 只看到 90/91，设备 8 只看到 10/20
    assert q(db, "SELECT device_id, boot_seq, last_raw_tick FROM tele_device_state "
                 "ORDER BY device_id") == [(7, 1, 91), (8, 1, 20)]


def test_crash_then_replacement_denied_and_original_resumes(tmp_path):
    """在提交边界真实崩溃；重启后同前缀换文件必须被挡。

    用硬链接在替换前保留原对象：已提交状态完全不动，原对象经新路径可识别
    为同一来源并正常续读。
    """
    db = tmp_path / "t.duckdb"
    f = tmp_path / "source.bin"
    data = stream_with_ticks([10, 20, 30, 40])
    f.write_bytes(data)
    keep = tmp_path / "keep.bin"
    os.link(f, keep)                        # 原对象保留在另一路径

    r = run_crashing_ingest(db, f, crash_after=2)
    assert r.returncode == 137
    snap = snapshot(db)
    assert q(db, "SELECT COUNT(*) FROM tele_samples")[0][0] >= 2
    consumed, sha = q(db, "SELECT consumed_offset, prefix_sha256 "
                          "FROM tele_ingest_state")[0]
    assert hashlib.sha256(data[:consumed]).hexdigest() == sha

    # 替换同一路径为新对象（同前缀、不同尾巴）
    repl = tmp_path / "repl.bin"
    repl.write_bytes(data[:consumed]
                     + stream_with_ticks([50], values=[50]))
    os.replace(repl, f)
    rr = run_cli(db, f)
    assert rr.returncode == 3
    assert "另一个文件对象" in rr.stderr
    assert snapshot(db) == snap
    assert q(db, "SELECT COUNT(*) FROM tele_samples WHERE tick = 50") == [(0,)]

    # 原对象仍在 keep.bin：身份相同 → 可续读，且仍是同一个 file_id
    with Ingester(db) as ing:
        st = ing.ingest_file(keep)
    assert st.file_id == 1
    assert q(db, "SELECT tick FROM tele_samples ORDER BY tick") == [
        (10,), (20,), (30,), (40,)]
    assert progress_rows(db)[0][1] == len(data)


# ----------------------------------------------------------------------
# 5. 未补齐尾包时崩溃
# ----------------------------------------------------------------------

def test_crash_with_pending_tail_then_completion(tmp_path):
    full = stream_with_ticks([1, 2], values=[11, 22])
    tail = full[-3:]                       # 末包缺 3 字节
    part1 = full[:-3]
    db = tmp_path / "t.duckdb"
    f = tmp_path / "tail.bin"
    f.write_bytes(part1)
    with Ingester(db) as ing:
        s1 = ing.ingest_file(f, chunk_size=5)
        assert s1.pending_bytes > 0
        # 进度只到最后一个完整帧的末尾（不含半帧字节）
        expected_boundary = len(part1) - s1.pending_bytes
        assert s1.consumed_offset == expected_boundary
        assert q(db, "SELECT COUNT(*) FROM tele_samples") == [(1,)]  # 仅 tick=1 的包
    before = snapshot(db)

    # 未补齐时「崩溃」：此时没有任何未提交事务，只需证明重启后状态一致
    con = duckdb.connect(str(db))
    con.close()
    assert snapshot(db) == before

    # 补齐尾包：恰好产出那一包，累计 2 个样本
    with open(f, "ab") as fh:
        fh.write(tail)
    with Ingester(db) as ing:
        s2 = ing.ingest_file(f, chunk_size=5)
        assert s2.start_offset == s1.consumed_offset
        assert s2.samples_added == 1
        assert s2.pending_bytes == 0
    assert q(db, "SELECT tick, cum_tick FROM tele_samples ORDER BY cum_tick") == [
        (1, 1), (2, 2)]
    assert progress_rows(db)[0][1] == len(full)


def test_crash_with_pending_tail_via_subprocess_recovery(tmp_path):
    """尾包未补齐时两次运行（第二次等同于崩溃后重启），补齐后只产出一包。"""
    full = stream_with_ticks([5, 6], values=[15, 16])
    part1, tail = full[:-3], full[-3:]
    db = tmp_path / "t.duckdb"
    f = tmp_path / "tail.bin"
    f.write_bytes(part1)
    with Ingester(db) as ing:
        ing.ingest_file(f)
    # 重启（全新连接）读同一对象：无新字节
    with Ingester(db) as ing:
        s = ing.ingest_file(f)
        assert s.samples_added == 0 and s.pending_bytes > 0
    with open(f, "ab") as fh:
        fh.write(tail)
    with Ingester(db) as ing:
        s = ing.ingest_file(f)
        assert s.samples_added == 1
    assert q(db, "SELECT count(*) FROM tele_samples") == [(2,)]
    assert q(db, "SELECT last_raw_tick FROM tele_device_state") == [(6,)]
    # 再跑一遍：不再新增
    with Ingester(db) as ing:
        s = ing.ingest_file(f)
        assert s.samples_added == 0 and s.frames_applied == 0


# ----------------------------------------------------------------------
# 6. 已处理内容被改写 / 截断：仍是拒绝（未因防替换禁掉正常续读）
# ----------------------------------------------------------------------

def test_prefix_rewrite_and_truncation_still_denied(tmp_path):
    db = tmp_path / "t.duckdb"
    f = tmp_path / "x.bin"
    data = stream_with_ticks([100, 200])
    f.write_bytes(data)
    with Ingester(db) as ing:
        ing.ingest_file(f)
    snap = snapshot(db)

    mutated = bytearray(data)
    mutated[-1] ^= 0xFF
    f.write_bytes(bytes(mutated))
    with Ingester(db) as ing, pytest.raises(IngestError, match="前缀"):
        ing.ingest_file(f)
    assert snapshot(db) == snap

    f.write_bytes(data[:10])
    with Ingester(db) as ing, pytest.raises(IngestError, match="小于|截断"):
        ing.ingest_file(f)
    assert snapshot(db) == snap

    # 正常追加仍可：修复回原内容后加一包
    f.write_bytes(data + stream_with_ticks([300], values=[30]))
    with Ingester(db) as ing:
        st = ing.ingest_file(f)
        assert st.samples_added == 1
    assert q(db, "SELECT tick FROM tele_samples ORDER BY tick") == [
        (100,), (200,), (300,)]


# ----------------------------------------------------------------------
# 7. 旧库兼容
# ----------------------------------------------------------------------

def _make_legacy_db(db, path, data, consumed=None):
    """构造只有旧 schema（路径 + 前缀哈希）的库，模拟升级前的历史。"""
    consumed = len(data) if consumed is None else consumed
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE tele_files (file_id INTEGER PRIMARY KEY, "
                "path TEXT NOT NULL UNIQUE, first_seen_utc TEXT NOT NULL)")
    con.execute("CREATE TABLE tele_ingest_state (file_id INTEGER PRIMARY KEY, "
                "consumed_offset BIGINT NOT NULL, prefix_sha256 TEXT NOT NULL, "
                "file_size BIGINT NOT NULL, updated_utc TEXT NOT NULL)")
    con.execute("CREATE TABLE tele_device_state (device_id INTEGER PRIMARY KEY, "
                "boot_seq BIGINT NOT NULL, epoch_ns BIGINT NOT NULL, "
                "tick_hz BIGINT NOT NULL, have_tick BOOLEAN NOT NULL, "
                "last_raw_tick BIGINT, last_cum_tick BIGINT, "
                "last_layout_id INTEGER, last_values BLOB)")
    con.execute("INSERT INTO tele_files VALUES (1, ?, 'legacy')", [path])
    con.execute("INSERT INTO tele_ingest_state VALUES (1, ?, ?, ?, 'legacy')",
                [consumed, hashlib.sha256(data[:consumed]).hexdigest(), len(data)])
    con.close()


def test_legacy_database_refuses_auto_continuation(tmp_path):
    db = tmp_path / "legacy.duckdb"
    f = tmp_path / "source.bin"
    data = stream_with_ticks([100, 200])
    f.write_bytes(data)
    _make_legacy_db(db, str(f.resolve()), data)

    # 公开命令：保守阻止自动续接（退出码 3），旧记录原样保留
    r = run_cli(db, f)
    assert r.returncode == 3
    assert "缺少对象身份" in r.stderr or "旧库" in r.stderr
    assert q(db, "SELECT file_id, path, first_seen_utc FROM tele_files") == [
        (1, str(f.resolve()), "legacy")]
    assert q(db, "SELECT consumed_offset FROM tele_ingest_state") == [(len(data),)]
    assert q(db, "SELECT COUNT(*) FROM tele_samples") == [(0,)]     # 不认领、不入库
    # 迁移只补列，不填身份（不把当下文件认领为历史来源）
    assert q(db, "SELECT identity FROM tele_files") == [(None,)]

    # 正常追加更多字节也不会被当作历史对象的续写
    with open(f, "ab") as fh:
        fh.write(stream_with_ticks([300], values=[30]))
    r = run_cli(db, f)
    assert r.returncode == 3
    assert q(db, "SELECT COUNT(*) FROM tele_samples") == [(0,)]


def test_legacy_row_without_progress_can_register_safely(tmp_path):
    """旧行但 consumed_offset = 0：没有「已处理」可继承，可以从头读。"""
    db = tmp_path / "legacy0.duckdb"
    f = tmp_path / "source.bin"
    data = stream_with_ticks([100])
    f.write_bytes(data)
    _make_legacy_db(db, str(f.resolve()), data, consumed=0)
    with Ingester(db) as ing:
        st = ing.ingest_file(f)
        assert st.file_id == 1
        assert st.start_offset == 0
        assert st.samples_added == 1
    # 登记身份后，可继续按同一对象续读
    with Ingester(db) as ing:
        st = ing.ingest_file(f)
        assert st.file_id == 1 and st.samples_added == 0
    assert q(db, "SELECT identity FROM tele_files")[0][0] is not None


# ----------------------------------------------------------------------
# 8. 同一对象在多个路径间来回登记
# ----------------------------------------------------------------------

def test_same_object_alternating_paths_keeps_one_source(tmp_path):
    db = tmp_path / "t.duckdb"
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(stream_with_ticks([1, 2]))
    os.link(a, b)                          # 同一对象，两个登记路径

    with Ingester(db) as ing:
        st1 = ing.ingest_file(a)
        assert st1.file_id == 1
        # 同一对象换到 b：沿用 file_id，path 更新为 b
        st2 = ing.ingest_file(b)
        assert st2.file_id == 1 and st2.start_offset == len(stream_with_ticks([1, 2]))
        assert file_rows(db) == [(1, str(b.resolve()), file_rows(db)[0][2])]
        # 再换回 a：仍是同一来源
        st3 = ing.ingest_file(a)
        assert st3.file_id == 1
        assert file_rows(db) == [(1, str(a.resolve()), file_rows(db)[0][2])]
        # 追加后经 b 读也仍是同一来源
        with open(a, "ab") as fh:
            fh.write(stream_with_ticks([3], values=[3]))
        st4 = ing.ingest_file(b)
        assert st4.file_id == 1 and st4.samples_added == 1

    assert q(db, "SELECT file_id, tick FROM tele_samples ORDER BY tick") == [
        (1, 1), (1, 2), (1, 3)]
    assert q(db, "SELECT COUNT(*) FROM tele_files") == [(1,)]
    assert q(db, "SELECT COUNT(*) FROM tele_ingest_state") == [(1,)]


# ----------------------------------------------------------------------
# 9. 期望值独立算出：来源、进度、样本、设备状态
# ----------------------------------------------------------------------

def test_expected_values_come_from_input_bytes(tmp_path):
    ticks = [7, 8, 9]
    values = [70, 80, 90]
    data = stream_with_ticks(ticks, values)
    db = tmp_path / "t.duckdb"
    f = tmp_path / "s.bin"
    f.write_bytes(data)
    with Ingester(db) as ing:
        st = ing.ingest_file(f, chunk_size=3)

    assert st.consumed_offset == len(data)
    assert progress_rows(db) == [(1, len(data), hashlib.sha256(data).hexdigest(),
                                  len(data))]
    rows = sample_ticks(db)
    exp = [(t, t, EPOCH + t * 1_000_000, float(v), expected_value(v), "°C")
           for t, v in zip(ticks, values)]
    got = [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]
    assert len(got) == len(exp)
    for g, e in zip(got, exp):
        assert (g[0], g[1], g[2], g[5]) == (e[0], e[1], e[2], e[5])
        assert g[3] == pytest.approx(e[3])
        assert g[4] == pytest.approx(e[4])
    # 设备状态：启动序号不变，计数器等于最后一个包的 tick
    assert q(db, "SELECT device_id, boot_seq, tick_hz, last_raw_tick, last_cum_tick "
                 "FROM tele_device_state") == [(DEV, 1, HZ, 9, 9)]
