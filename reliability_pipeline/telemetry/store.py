"""DuckDB 持久化层：遥测样本、帧台账、拒收明细、设备/布局/进度状态。

表结构（一律 ``tele_`` 前缀，与 ``pipeline`` 包的生存分析表共存于同一库）
------------------------------------------------------------------------
- ``tele_files``           源文件身份（路径 → file_id，以及上次读到的对象身份
                           ``identity`` = 打开用于读取的 fd 的 (dev, ino)）
- ``tele_ingest_state``    每文件的可续读位置：已消费偏移 + 已消费前缀 SHA-256
- ``tele_device_state``    每台设备当前启动周期：boot_seq、epoch_ns、tick_hz、
                           上一被接纳 tick（原始与累计）、末包内容（重传判定用）
- ``tele_layouts`` / ``tele_layout_channels``
                           布局声明，隶属于 (device, boot_seq)，历史周期保留
- ``tele_samples``         测量样本：物理值、最终单位、ts_ns（Unix 纳秒）、
                           源 file_id + 帧起始偏移 + 通道对应
- ``tele_frames``          每个 CRC 合法帧的处置台账（applied/duplicate/rejected）
- ``tele_rejections``      拒收明细（含解码异常：坏 kind/长度/CRC）
- ``tele_samples_view``    样本 + ``make_timestamp_ns(ts_ns)`` 的时刻列

一致性
------
``Ingester`` 把「帧应用 + 状态推进 + 进度保存」放在同一个 DuckDB 事务里提交；
崩溃后 DuckDB 回滚未提交事务，重读上一进度不会产生重复样本。
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import duckdb

from . import protocol as P
from .decoder import Anomaly, Frame
from .errors import IngestError

SCHEMA = """
CREATE TABLE IF NOT EXISTS tele_files (
    file_id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    first_seen_utc TEXT NOT NULL,
    identity TEXT
);
CREATE TABLE IF NOT EXISTS tele_ingest_state (
    file_id INTEGER PRIMARY KEY,
    consumed_offset BIGINT NOT NULL,
    prefix_sha256 TEXT NOT NULL,
    file_size BIGINT NOT NULL,
    updated_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tele_device_state (
    device_id INTEGER PRIMARY KEY,
    boot_seq BIGINT NOT NULL,
    epoch_ns BIGINT NOT NULL,
    tick_hz BIGINT NOT NULL,
    have_tick BOOLEAN NOT NULL,
    last_raw_tick BIGINT,
    last_cum_tick BIGINT,
    last_layout_id INTEGER,
    last_values BLOB
);
CREATE TABLE IF NOT EXISTS tele_layouts (
    device_id INTEGER NOT NULL,
    boot_seq BIGINT NOT NULL,
    layout_id INTEGER NOT NULL,
    channel_count INTEGER NOT NULL,
    value_bytes INTEGER NOT NULL,
    decl BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS tele_layout_channels (
    device_id INTEGER NOT NULL,
    boot_seq BIGINT NOT NULL,
    layout_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    channel INTEGER NOT NULL,
    raw_type INTEGER NOT NULL,
    gain DOUBLE NOT NULL,
    bias DOUBLE NOT NULL,
    unit INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tele_samples (
    file_id INTEGER NOT NULL,
    frame_offset BIGINT NOT NULL,
    device_id INTEGER NOT NULL,
    boot_seq BIGINT NOT NULL,
    layout_id INTEGER NOT NULL,
    tick BIGINT NOT NULL,
    cum_tick BIGINT NOT NULL,
    ts_ns BIGINT NOT NULL,
    ordinal INTEGER NOT NULL,
    channel INTEGER NOT NULL,
    raw_type INTEGER NOT NULL,
    raw_value DOUBLE NOT NULL,
    value DOUBLE NOT NULL,
    unit TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tele_frames (
    file_id INTEGER NOT NULL,
    frame_offset BIGINT NOT NULL,
    kind INTEGER NOT NULL,
    device_id INTEGER,
    frame_len INTEGER NOT NULL,
    disposition TEXT NOT NULL,
    reason TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS tele_rejections (
    file_id INTEGER NOT NULL,
    frame_offset BIGINT NOT NULL,
    kind INTEGER,
    device_id INTEGER,
    reason TEXT NOT NULL,
    detail TEXT
);
CREATE OR REPLACE VIEW tele_samples_view AS
    SELECT *, make_timestamp_ns(ts_ns) AS ts FROM tele_samples;
"""

# 处置类型
APPLIED = "applied"
DUPLICATE = "duplicate"
REJECTED = "rejected"


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    _migrate_files_identity(con)
    return con


# file identity：把实际打开用于读取的对象 (dev, ino) 存成可比较的文本。
IDENTITY_UNKNOWN = "unknown"      # 已消费进度存在但无法证明属于哪个对象（旧库）


def _migrate_files_identity(con: duckdb.DuckDBPyConnection) -> None:
    """旧库兼容：``tele_files`` 只有 path 时补出可空的 ``identity`` 列。

    历史行保持 ``NULL``——绝不把当下路径指向的文件认领为历史来源；是否
    可以自动续接由 ``ingest`` 依据「旧行 + 已有进度」保守判定。
    """
    cols = {row[1] for row in con.execute("PRAGMA table_info('tele_files')").fetchall()}
    if "identity" not in cols:
        con.execute("ALTER TABLE tele_files ADD COLUMN identity TEXT")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DeviceState:
    """设备当前启动周期的可变状态（内存缓存，随事务持久化）。"""

    device_id: int
    boot_seq: int
    epoch_ns: int
    tick_hz: int
    have_tick: bool = False
    last_raw_tick: Optional[int] = None
    last_cum_tick: Optional[int] = None
    last_layout_id: Optional[int] = None
    last_values: Optional[bytes] = None
    layouts: Dict[int, P.LayoutDecl] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplyResult:
    disposition: str           # applied / duplicate / rejected
    reason: Optional[str] = None
    detail: Optional[str] = None
    n_samples: int = 0


@dataclass(frozen=True)
class FileSource:
    """一个源文件在库中的身份与上次已提交进度。"""

    file_id: int
    path: str
    identity: Optional[str]          # 上次读取对象 (dev, ino)；NULL = 旧库未知
    consumed_offset: int
    prefix_sha256: str
    file_size: int


def encode_identity(dev: int, ino: int) -> str:
    return f"{dev}:{ino}"


class TelemetryStore:
    """把解码事件应用到 DuckDB；调用方负责事务边界（begin/commit）。"""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        self._devices: Dict[int, DeviceState] = {}

    # ---- 事务 ----------------------------------------------------------

    def begin(self) -> None:
        self.con.execute("BEGIN TRANSACTION")

    def commit(self) -> None:
        self.con.execute("COMMIT")

    def rollback(self) -> None:
        self.con.execute("ROLLBACK")
        self._devices.clear()      # 内存缓存已脏，弃之

    # ---- 文件身份与进度 ------------------------------------------------

    def lookup_file(self, path: str) -> Optional[FileSource]:
        """按登记路径查来源；没有登记则返回 None。"""
        row = self.con.execute(
            "SELECT f.file_id, f.path, f.identity, s.consumed_offset, "
            "s.prefix_sha256, s.file_size "
            "FROM tele_files f JOIN tele_ingest_state s USING (file_id) "
            "WHERE f.path = ?", [path]).fetchone()
        if row is None:
            return None
        return FileSource(row[0], row[1], row[2], row[3], row[4], row[5])

    def lookup_file_by_identity(self, identity: str) -> Optional[FileSource]:
        """按对象身份找已登记来源（同一文件被移动/改名时沿用旧 file_id）。"""
        rows = self.con.execute(
            "SELECT f.file_id, f.path, f.identity, s.consumed_offset, "
            "s.prefix_sha256, s.file_size "
            "FROM tele_files f JOIN tele_ingest_state s USING (file_id) "
            "WHERE f.identity = ? ORDER BY f.file_id", [identity]).fetchall()
        if not rows:
            return None
        row = rows[0]
        return FileSource(row[0], row[1], row[2], row[3], row[4], row[5])

    def update_file_path(self, file_id: int, path: str) -> None:
        self.con.execute("UPDATE tele_files SET path = ? WHERE file_id = ?",
                         [path, file_id])

    def register_file(self, path: str, identity: str, file_size: int) -> FileSource:
        """登记一个新来源（新文件对象），进度从 0 开始。"""
        file_id = self.con.execute(
            "SELECT COALESCE(MAX(file_id), 0) + 1 FROM tele_files").fetchone()[0]
        self.con.execute(
            "INSERT INTO tele_files (file_id, path, first_seen_utc, identity) "
            "VALUES (?, ?, ?, ?)", [file_id, path, _utcnow(), identity])
        empty_sha = ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        self.con.execute("INSERT INTO tele_ingest_state VALUES (?, 0, ?, ?, ?)",
                         [file_id, empty_sha, file_size, _utcnow()])
        return FileSource(file_id, path, identity, 0, empty_sha, file_size)

    def claim_unregistered(self, path: str, identity: str, size: int) -> FileSource:
        """接手旧库中「已登记路径但从未接入成功」的行（consumed_offset=0）。

        只补身份与当前大小，不改路径、不新增行；进度仍是 0，因此不涉及任何
        已处理字节归属。若该行已被其他对象接手（identity 已非 NULL），按
        正常冲突规则拒绝。
        """
        row = self.lookup_file(path)
        if row is None:
            return self.register_file(path, identity, size)
        if row.identity is not None and row.identity != identity:
            raise IngestError(
                f"路径 {path} 已被另一个文件对象占用：登记的来源 file_id="
                f"{row.file_id} 身份为 {row.identity}，当前打开对象身份为 "
                f"{identity}；文件被替换，拒绝把新对象的数据记到旧来源名下")
        if row.consumed_offset != 0:
            raise IngestError(
                f"来源 file_id={row.file_id}（{row.path}）已有进度 "
                f"{row.consumed_offset}，但缺少对象身份信息（旧库格式）；"
                f"保守阻止自动续接。旧记录与样本保持原样，可用前缀哈希审计")
        # 进度为 0 仍可能留有旧数据（异常/手工库）：只要该来源名下有样本或
        # 帧台账，就不能把当下路径的对象认领为它的后续。
        existing = self.con.execute(
            "SELECT (SELECT COUNT(*) FROM tele_samples WHERE file_id = ?) + "
            "(SELECT COUNT(*) FROM tele_frames WHERE file_id = ?)",
            [row.file_id, row.file_id]).fetchone()[0]
        if existing:
            raise IngestError(
                f"来源 file_id={row.file_id}（{row.path}）没有对象身份信息，"
                f"但名下已有 {existing} 条样本/帧台账；无法证明它们属于当前"
                f"打开的文件，保守阻止自动续接（不认领、不清空）")
        self.con.execute(
            "UPDATE tele_files SET identity = ? WHERE file_id = ?",
            [identity, row.file_id])
        self.con.execute(
            "UPDATE tele_ingest_state SET file_size = ?, updated_utc = ? "
            "WHERE file_id = ?", [size, _utcnow(), row.file_id])
        return FileSource(row.file_id, row.path, identity, 0,
                          row.prefix_sha256, size)

    def update_source(self, file_id: int, identity: str, consumed_offset: int,
                      prefix_sha256: str, file_size: int) -> None:
        """进度与来源身份一起保存：身份也绑定到「读到这个偏移的对象」。"""
        self.con.execute(
            "UPDATE tele_files SET identity = ? WHERE file_id = ?",
            [identity, file_id])
        self.con.execute(
            "UPDATE tele_ingest_state SET consumed_offset = ?, prefix_sha256 = ?, "
            "file_size = ?, updated_utc = ? WHERE file_id = ?",
            [consumed_offset, prefix_sha256, file_size, _utcnow(), file_id])

    # ---- 设备状态 ------------------------------------------------------

    def _device(self, device_id: int) -> Optional[DeviceState]:
        if device_id in self._devices:
            return self._devices[device_id]
        row = self.con.execute(
            "SELECT boot_seq, epoch_ns, tick_hz, have_tick, last_raw_tick, "
            "last_cum_tick, last_layout_id, last_values "
            "FROM tele_device_state WHERE device_id = ?", [device_id]).fetchone()
        if row is None:
            return None
        st = DeviceState(device_id, row[0], row[1], row[2], bool(row[3]),
                         row[4], row[5], row[6],
                         bytes(row[7]) if row[7] is not None else None)
        st.layouts = self._load_layouts(device_id, st.boot_seq)
        self._devices[device_id] = st
        return st

    def _load_layouts(self, device_id: int, boot_seq: int) -> Dict[int, P.LayoutDecl]:
        layouts: Dict[int, P.LayoutDecl] = {}
        rows = self.con.execute(
            "SELECT layout_id, decl FROM tele_layouts "
            "WHERE device_id = ? AND boot_seq = ?", [device_id, boot_seq]).fetchall()
        for layout_id, decl in rows:
            ch_rows = self.con.execute(
                "SELECT ordinal, channel, raw_type, gain, bias, unit "
                "FROM tele_layout_channels WHERE device_id = ? AND boot_seq = ? "
                "AND layout_id = ? ORDER BY ordinal",
                [device_id, boot_seq, layout_id]).fetchall()
            channels = tuple(P.ChannelSpec(*r) for r in ch_rows)
            layouts[layout_id] = P.LayoutDecl(device_id, layout_id, channels, bytes(decl))
        return layouts

    def _persist_device(self, st: DeviceState) -> None:
        self.con.execute(
            "INSERT INTO tele_device_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (device_id) DO UPDATE SET "
            "boot_seq = excluded.boot_seq, epoch_ns = excluded.epoch_ns, "
            "tick_hz = excluded.tick_hz, have_tick = excluded.have_tick, "
            "last_raw_tick = excluded.last_raw_tick, "
            "last_cum_tick = excluded.last_cum_tick, "
            "last_layout_id = excluded.last_layout_id, "
            "last_values = excluded.last_values",
            [st.device_id, st.boot_seq, st.epoch_ns, st.tick_hz, st.have_tick,
             st.last_raw_tick, st.last_cum_tick, st.last_layout_id, st.last_values])

    # ---- 事件应用 ------------------------------------------------------

    def record_anomaly(self, file_id: int, an: Anomaly) -> None:
        self.con.execute(
            "INSERT INTO tele_rejections VALUES (?, ?, NULL, NULL, ?, ?)",
            [file_id, an.offset, an.reason, an.detail])

    def apply_frame(self, file_id: int, frame: Frame) -> ApplyResult:
        if frame.kind == P.KIND_BOOT:
            res = self._apply_boot(frame)
        elif frame.kind == P.KIND_LAYOUT:
            res = self._apply_layout(frame)
        else:
            res = self._apply_measurement(file_id, frame)
        device_id = (struct.unpack("<H", frame.payload[:2])[0]
                     if len(frame.payload) >= 2 else None)
        self.con.execute(
            "INSERT INTO tele_frames VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [file_id, frame.offset, frame.kind, device_id, len(frame.raw),
             res.disposition, res.reason, res.detail])
        if res.disposition == REJECTED:
            self.con.execute(
                "INSERT INTO tele_rejections VALUES (?, ?, ?, ?, ?, ?)",
                [file_id, frame.offset, frame.kind, device_id, res.reason, res.detail])
        return res

    # ---- kind=3 启动锚点 ----------------------------------------------

    def _apply_boot(self, frame: Frame) -> ApplyResult:
        try:
            boot = P.parse_boot_payload(frame.payload)
        except P.ProtocolError as e:
            return ApplyResult(REJECTED, "invalid_boot", str(e))
        if boot.tick_hz <= 0:
            return ApplyResult(REJECTED, "invalid_boot",
                               f"tick_hz 必须为正，实际 {boot.tick_hz}")
        st = self._device(boot.device)
        if st is None or boot.boot_seq > st.boot_seq:
            # 新设备或新的启动周期：重置计时与布局（历史周期数据保留在库中）
            st = DeviceState(boot.device, boot.boot_seq, boot.epoch_ns, boot.tick_hz)
            self._devices[boot.device] = st
            self._persist_device(st)
            return ApplyResult(APPLIED)
        if boot.boot_seq < st.boot_seq:
            return ApplyResult(REJECTED, "stale_boot",
                               f"boot_seq={boot.boot_seq} 落后于当前周期 {st.boot_seq}")
        if boot.epoch_ns != st.epoch_ns or boot.tick_hz != st.tick_hz:
            return ApplyResult(REJECTED, "boot_conflict",
                               f"同 boot_seq={boot.boot_seq} 但参数不同："
                               f"epoch_ns {boot.epoch_ns}!={st.epoch_ns} 或 "
                               f"tick_hz {boot.tick_hz}!={st.tick_hz}")
        return ApplyResult(DUPLICATE, detail="相同启动锚点重传")

    # ---- kind=1 布局声明 ----------------------------------------------

    def _apply_layout(self, frame: Frame) -> ApplyResult:
        try:
            decl = P.parse_layout_payload(frame.payload)
        except P.ProtocolError as e:
            return ApplyResult(REJECTED, "invalid_layout", str(e))
        st = self._device(decl.device)
        if st is None:
            return ApplyResult(REJECTED, "unknown_device",
                               f"设备 {decl.device} 尚未收到启动锚点")
        old = st.layouts.get(decl.layout_id)
        if old is not None:
            if old.decl_bytes == decl.decl_bytes:
                return ApplyResult(DUPLICATE, detail="相同布局声明重传")
            return ApplyResult(REJECTED, "layout_conflict",
                               f"布局 {decl.layout_id} 在当前周期已有不同声明，"
                               f"不替换既有解释")
        st.layouts[decl.layout_id] = decl
        self.con.execute(
            "INSERT INTO tele_layouts VALUES (?, ?, ?, ?, ?, ?)",
            [decl.device, st.boot_seq, decl.layout_id, len(decl.channels),
             decl.value_bytes, decl.decl_bytes])
        if decl.channels:
            self.con.executemany(
                "INSERT INTO tele_layout_channels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [[decl.device, st.boot_seq, decl.layout_id, c.ordinal, c.channel,
                  c.raw_type, c.gain, c.bias, c.unit] for c in decl.channels])
        return ApplyResult(APPLIED)

    # ---- kind=2 测量包 --------------------------------------------------

    def _apply_measurement(self, file_id: int, frame: Frame) -> ApplyResult:
        try:
            head = P.parse_meas_header(frame.payload)
        except P.ProtocolError as e:
            return ApplyResult(REJECTED, "invalid_measurement", str(e))
        st = self._device(head.device)
        if st is None:
            return ApplyResult(REJECTED, "unknown_device",
                               f"设备 {head.device} 尚未收到启动锚点")
        layout = st.layouts.get(head.layout_id)
        if layout is None:
            return ApplyResult(REJECTED, "unknown_layout",
                               f"设备 {head.device} 当前周期未知布局 {head.layout_id}")
        value_bytes = frame.payload[P.MEAS_HEADER_LEN:]
        try:
            raw_values = P.decode_values(layout, value_bytes)
        except P.ProtocolError as e:
            return ApplyResult(REJECTED, "bad_payload_length", str(e))
        # 值合法性：浮点原始值不得为 NaN/Inf；换算结果必须为有限值
        for spec, raw in zip(layout.channels, raw_values):
            if not math.isfinite(raw):
                return ApplyResult(REJECTED, "nonfinite_value",
                                   f"通道 {spec.channel} 原始值非有限：{raw!r}，整包拒收")
            if not math.isfinite(spec.convert(raw)):
                return ApplyResult(REJECTED, "nonfinite_value",
                                   f"通道 {spec.channel} 换算结果非有限，整包拒收")
        # tick 展开（模 2^32）
        if not st.have_tick:
            cum_tick = head.tick                      # 首个样本以其无符号 tick 为累计刻度
        else:
            diff = P.tick_forward_diff(head.tick, st.last_raw_tick)
            if diff == 0:
                if (head.layout_id == st.last_layout_id
                        and value_bytes == st.last_values):
                    return ApplyResult(DUPLICATE, detail="相同测量包重传")
                return ApplyResult(REJECTED, "tick_conflict",
                                   f"tick={head.tick} 与上一被接纳包相同但内容冲突")
            if diff >= P.TICK_HALF:
                return ApplyResult(REJECTED, "stale_tick",
                                   f"tick={head.tick} 相对上一接纳 tick "
                                   f"{st.last_raw_tick} 前进差 {diff} ≥ 2^31，按旧包拒收")
            cum_tick = st.last_cum_tick + diff
        ts_ns = P.sample_ts_ns(st.epoch_ns, cum_tick, st.tick_hz)
        if not (P.INT64_MIN <= ts_ns <= P.INT64_MAX):
            return ApplyResult(REJECTED, "timestamp_overflow",
                               f"ts_ns={ts_ns} 超出 int64 范围")
        rows = []
        for spec, raw in zip(layout.channels, raw_values):
            rows.append([file_id, frame.offset, head.device, st.boot_seq,
                         head.layout_id, head.tick, cum_tick, ts_ns,
                         spec.ordinal, spec.channel, spec.raw_type,
                         float(raw), spec.convert(raw), spec.final_unit])
        if rows:
            self.con.executemany("INSERT INTO tele_samples VALUES "
                                 "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        st.have_tick = True
        st.last_raw_tick = head.tick
        st.last_cum_tick = cum_tick
        st.last_layout_id = head.layout_id
        st.last_values = value_bytes
        self._persist_device(st)
        return ApplyResult(APPLIED, n_samples=len(rows))
