"""DuckDB 持久化层：原始日志装载、分析结果落库、CSV 导出。

表结构
------
- ``run_input_devices`` / ``run_input_events``：每次分析所用原始输入的快照
  （可追溯：任何结果都能回到当时的原始记录）；
- ``runs``：分析运行参数（knowledge_cutoff、observation_end、代码版本）；
- ``run_devices``：入选/拒收清单 + 设备时间段（L、T、状态、决定结局的事件）；
- ``rejections``：拒收明细（记录级与设备级，含原因）；
- ``event_audit``：每条可见事件的处置台账；
- ``risk_sets``：逐分组逐结局时点的风险人数、两类结局数、删失数；
- ``risk_set_members``：风险集成员明细（追溯到设备）；
- ``estimates``：AJ 与对照 KM 的估计曲线。
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List

import duckdb

from . import __version__
from .cohort import CohortResult
from .estimate import GroupEstimate

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    knowledge_cutoff DATE NOT NULL,
    observation_end DATE NOT NULL,
    created_utc TEXT NOT NULL,
    code_version TEXT NOT NULL,
    source_dir TEXT NOT NULL,
    n_devices_raw INTEGER NOT NULL,
    n_events_raw INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS run_input_devices (
    run_id TEXT NOT NULL, device_id TEXT NOT NULL, machine_type TEXT,
    commission_date TEXT, enroll_date TEXT
);
CREATE TABLE IF NOT EXISTS run_input_events (
    run_id TEXT NOT NULL, event_id TEXT NOT NULL, device_id TEXT,
    event_type TEXT, event_date TEXT, received_at TEXT,
    voids_event_id TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS run_devices (
    run_id TEXT NOT NULL, device_id TEXT NOT NULL, machine_type TEXT,
    commission_date TEXT, enroll_date TEXT,
    L INTEGER, T INTEGER, status TEXT,
    cohort_status TEXT NOT NULL, exclude_reason TEXT, terminal_event_id TEXT
);
CREATE TABLE IF NOT EXISTS rejections (
    run_id TEXT NOT NULL, level TEXT NOT NULL, entity_id TEXT NOT NULL,
    reason TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS event_audit (
    run_id TEXT NOT NULL, event_id TEXT NOT NULL, device_id TEXT,
    event_type TEXT, event_date TEXT, received_at TEXT,
    voids_event_id TEXT, disposition TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_sets (
    run_id TEXT NOT NULL, grp TEXT NOT NULL, t INTEGER NOT NULL,
    n_risk INTEGER NOT NULL, d_failure INTEGER NOT NULL,
    d_retirement INTEGER NOT NULL, n_censored INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_set_members (
    run_id TEXT NOT NULL, grp TEXT NOT NULL, t INTEGER NOT NULL, device_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS estimates (
    run_id TEXT NOT NULL, grp TEXT NOT NULL, t INTEGER NOT NULL,
    surv DOUBLE, cif_failure DOUBLE, cif_retirement DOUBLE,
    km_surv DOUBLE, km_failure DOUBLE
);
"""


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    return con


def load_raw_csv(data_dir: str | Path):
    """读取 devices.csv / events.csv 为字符串行（解析在 cohort 层做）。"""
    data_dir = Path(data_dir)
    with open(data_dir / "devices.csv", newline="", encoding="utf-8") as fh:
        devices = list(csv.DictReader(fh))
    with open(data_dir / "events.csv", newline="", encoding="utf-8") as fh:
        events = list(csv.DictReader(fh))
    return devices, events


def save_run(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    source_dir: str,
    knowledge_cutoff: date,
    observation_end: date,
    devices_raw: List[dict],
    events_raw: List[dict],
    cohort: CohortResult,
    estimates: Dict[str, GroupEstimate],
) -> None:
    """把一次分析的输入快照与全部产出写入 DuckDB（同一 run_id 先清后写）。"""
    tables = ["runs", "run_input_devices", "run_input_events", "run_devices",
              "rejections", "event_audit", "risk_sets", "risk_set_members", "estimates"]
    for tbl in tables:
        con.execute(f"DELETE FROM {tbl} WHERE run_id = ?", [run_id])

    con.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [run_id, knowledge_cutoff, observation_end,
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         __version__, source_dir, len(devices_raw), len(events_raw)],
    )
    con.executemany(
        "INSERT INTO run_input_devices VALUES (?, ?, ?, ?, ?)",
        [[run_id, d.get("device_id") or "", d.get("machine_type") or "",
          d.get("commission_date") or "", d.get("enroll_date") or ""]
         for d in devices_raw],
    )
    con.executemany(
        "INSERT INTO run_input_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [[run_id, e.get("event_id") or "", e.get("device_id") or "",
          e.get("event_type") or "", e.get("event_date") or "",
          e.get("received_at") or "", e.get("voids_event_id") or "", e.get("note") or ""]
         for e in events_raw],
    )
    con.executemany(
        "INSERT INTO run_devices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [[run_id, r["device_id"], r["machine_type"], r["commission_date"], r["enroll_date"],
          r["L"], r["T"], r["status"], r["cohort_status"], r["exclude_reason"],
          r["terminal_event_id"]] for r in cohort.device_rows],
    )
    con.executemany(
        "INSERT INTO rejections VALUES (?, ?, ?, ?, ?)",
        [[run_id, r["level"], r["entity_id"], r["reason"], r["detail"]]
         for r in cohort.rejections],
    )
    con.executemany(
        "INSERT INTO event_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [[run_id, a["event_id"], a["device_id"], a["event_type"], a["event_date"],
          a["received_at"], a["voids_event_id"], a["disposition"]]
         for a in cohort.event_audit],
    )
    risk_rows, member_rows, est_rows = [], [], []
    for group, ge in estimates.items():
        for row in ge.rows:
            risk_rows.append([run_id, group, row["t"], row["n_risk"],
                              row["d_failure"], row["d_retirement"], row["n_censored"]])
            est_rows.append([run_id, group, row["t"], row["surv"], row["cif_failure"],
                             row["cif_retirement"], row["km_surv"], row["km_failure"]])
        for t, members in ge.risk_members.items():
            member_rows.extend([[run_id, group, t, m] for m in members])
    con.executemany("INSERT INTO risk_sets VALUES (?, ?, ?, ?, ?, ?, ?)", risk_rows)
    con.executemany("INSERT INTO risk_set_members VALUES (?, ?, ?, ?)", member_rows)
    con.executemany("INSERT INTO estimates VALUES (?, ?, ?, ?, ?, ?, ?, ?)", est_rows)


def export_run_csv(con: duckdb.DuckDBPyConnection, run_id: str, out_dir: str | Path) -> Path:
    """把一次分析的全部产出表导出为 CSV，便于人工核查。"""
    out = Path(out_dir) / run_id
    out.mkdir(parents=True, exist_ok=True)
    for tbl in ["run_devices", "rejections", "event_audit", "risk_sets",
                "risk_set_members", "estimates"]:
        cur = con.execute(f"SELECT * FROM {tbl} WHERE run_id = ?", [run_id])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        with open(out / f"{tbl}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerows(rows)
    return out
