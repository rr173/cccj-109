"""启机识别结果的 DuckDB 持久化层。

表结构（``startup_`` 前缀，与 ``tele_*`` 遥测表、``pipeline`` 生存分析表
共存于同一库；本层只追加新表，不改既有表）
------------------------------------------------------------------------
- ``startup_runs``           一次识别运行：配方 JSON、analysis_end_ns、计数
- ``startup_attempts``       每一次启机尝试的判定：越线/确认/稳定段/结束时刻、
                             三态状态与原因
- ``startup_evidence``       支持判断的时间片证据（点或半开区间 + 说明）
- ``startup_evidence_refs``  证据引用的源测量定位键
                             （file_id, frame_offset, ordinal —— 指向
                             ``tele_samples`` 真实行，绝不伪造源样本）

任何判定都可沿 ``startup_attempts → startup_evidence →
startup_evidence_refs → tele_samples`` 回溯到源文件的确切字节
（``tele_frames.frame_len`` 给出帧长）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import duckdb

from . import __version__
from .model import Attempt
from .recipe import Recipe

SCHEMA = """
CREATE TABLE IF NOT EXISTS startup_runs (
    run_id TEXT PRIMARY KEY,
    recipe_json TEXT NOT NULL,
    analysis_end_ns BIGINT NOT NULL,
    created_utc TEXT NOT NULL,
    code_version TEXT NOT NULL,
    n_attempts INTEGER NOT NULL,
    n_confirmed INTEGER NOT NULL,
    n_rejected INTEGER NOT NULL,
    n_indeterminate INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS startup_attempts (
    run_id TEXT NOT NULL,
    device_id INTEGER NOT NULL,
    boot_seq BIGINT NOT NULL,
    attempt_seq INTEGER NOT NULL,
    s_ns BIGINT NOT NULL,
    confirm_ns BIGINT,
    stable_start_ns BIGINT,
    stable_end_ns BIGINT,
    end_ns BIGINT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY (run_id, device_id, boot_seq, attempt_seq)
);
CREATE TABLE IF NOT EXISTS startup_evidence (
    run_id TEXT NOT NULL,
    device_id INTEGER NOT NULL,
    boot_seq BIGINT NOT NULL,
    attempt_seq INTEGER NOT NULL,
    ev_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    t_start_ns BIGINT NOT NULL,
    t_end_ns BIGINT,
    role TEXT,
    detail TEXT,
    PRIMARY KEY (run_id, device_id, boot_seq, attempt_seq, ev_seq)
);
CREATE TABLE IF NOT EXISTS startup_evidence_refs (
    run_id TEXT NOT NULL,
    device_id INTEGER NOT NULL,
    boot_seq BIGINT NOT NULL,
    attempt_seq INTEGER NOT NULL,
    ev_seq INTEGER NOT NULL,
    ref_seq INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    frame_offset BIGINT NOT NULL,
    ordinal INTEGER NOT NULL,
    channel INTEGER NOT NULL,
    ts_ns BIGINT NOT NULL,
    value DOUBLE NOT NULL,
    unit TEXT NOT NULL,
    PRIMARY KEY (run_id, device_id, boot_seq, attempt_seq, ev_seq, ref_seq)
);
"""


class StartupStoreError(RuntimeError):
    """结果库写入前提被破坏（如 run_id 已存在）。"""


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    """打开库并保证 startup_* 表存在（同时保证 tele_* 表结构已建立）。"""
    from telemetry import store as telemetry_store
    con = telemetry_store.connect(db_path)      # 建 tele_* 表（幂等）
    con.execute(SCHEMA)
    return con


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_run(con: duckdb.DuckDBPyConnection, run_id: str, recipe: Recipe,
             recipe_json: str, analysis_end_ns: int,
             attempts: List[Attempt]) -> None:
    """把一次识别运行与全部尝试/证据落库。run_id 已存在时拒绝覆盖。"""
    exists = con.execute(
        "SELECT COUNT(*) FROM startup_runs WHERE run_id = ?", [run_id]
    ).fetchone()[0]
    if exists:
        raise StartupStoreError(
            f"run_id {run_id!r} 已存在：识别结果不可覆写，请换一个 run_id")
    counts: Dict[str, int] = {"confirmed": 0, "rejected": 0, "indeterminate": 0}
    for at in attempts:
        counts[at.status] += 1
    con.execute(
        "INSERT INTO startup_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [run_id, recipe_json, analysis_end_ns, _utcnow(), __version__,
         len(attempts), counts["confirmed"], counts["rejected"],
         counts["indeterminate"]])
    con.executemany(
        "INSERT INTO startup_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [[run_id, at.device_id, at.boot_seq, at.attempt_seq, at.s_ns,
          at.confirm_ns, at.stable_start_ns, at.stable_end_ns, at.end_ns,
          at.status, at.reason, at.detail] for at in attempts])
    ev_rows, ref_rows = [], []
    for at in attempts:
        for ev_seq, ev in enumerate(at.evidence):
            ev_rows.append([run_id, at.device_id, at.boot_seq, at.attempt_seq,
                            ev_seq, ev.kind, ev.t_start_ns, ev.t_end_ns,
                            ev.role, ev.detail])
            for ref_seq, ref in enumerate(ev.refs):
                ref_rows.append([run_id, at.device_id, at.boot_seq,
                                 at.attempt_seq, ev_seq, ref_seq, ref.file_id,
                                 ref.frame_offset, ref.ordinal, ref.channel,
                                 ref.ts_ns, ref.value, ref.unit])
    if ev_rows:
        con.executemany(
            "INSERT INTO startup_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ev_rows)
    if ref_rows:
        con.executemany(
            "INSERT INTO startup_evidence_refs VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ref_rows)


def attempts_to_jsonable(attempts: List[Attempt]) -> List[dict]:
    """把尝试结果（含证据与定位键）转成可 JSON 序列化的结构。"""
    out = []
    for at in attempts:
        out.append({
            "device_id": at.device_id,
            "boot_seq": at.boot_seq,
            "attempt_seq": at.attempt_seq,
            "s_ns": at.s_ns,
            "confirm_ns": at.confirm_ns,
            "stable_start_ns": at.stable_start_ns,
            "stable_end_ns": at.stable_end_ns,
            "end_ns": at.end_ns,
            "status": at.status,
            "reason": at.reason,
            "detail": at.detail,
            "evidence": [{
                "kind": ev.kind,
                "t_start_ns": ev.t_start_ns,
                "t_end_ns": ev.t_end_ns,
                "role": ev.role,
                "detail": ev.detail,
                "refs": [{
                    "file_id": ref.file_id,
                    "frame_offset": ref.frame_offset,
                    "ordinal": ref.ordinal,
                    "channel": ref.channel,
                    "ts_ns": ref.ts_ns,
                    "value": ref.value,
                    "unit": ref.unit,
                } for ref in ev.refs],
            } for ev in at.evidence],
        })
    return out


def write_jsonl(attempts: List[Attempt], path: str | Path) -> None:
    """把尝试结果逐行写为 JSONL（每行一次尝试，含证据）。"""
    with open(path, "w", encoding="utf-8") as fh:
        for obj in attempts_to_jsonable(attempts):
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
