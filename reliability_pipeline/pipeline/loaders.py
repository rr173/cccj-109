"""原始 CSV 行 -> 领域对象（严格 ISO 日期解析，非法值保留原文）。"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from .model import Device, RawEvent


def parse_date(raw: str) -> Optional[date]:
    """严格解析 YYYY-MM-DD；任何偏差（含 2024-13-40 之类）返回 None。"""
    raw = (raw or "").strip()
    if len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def devices_from_rows(rows: List[dict]) -> List[Device]:
    out = []
    for r in rows:
        out.append(Device(
            device_id=(r.get("device_id") or "").strip(),
            machine_type=(r.get("machine_type") or "").strip(),
            commission_date=parse_date(r.get("commission_date") or ""),
            enroll_date=parse_date(r.get("enroll_date") or ""),
            commission_raw=r.get("commission_date") or "",
            enroll_raw=r.get("enroll_date") or "",
        ))
    return out


def events_from_rows(rows: List[dict]) -> List[RawEvent]:
    out = []
    for i, r in enumerate(rows, start=2):  # 行号含表头
        out.append(RawEvent(
            event_id=(r.get("event_id") or "").strip(),
            device_id=(r.get("device_id") or "").strip(),
            event_type=(r.get("event_type") or "").strip(),
            event_date=parse_date(r.get("event_date") or ""),
            received_at=parse_date(r.get("received_at") or ""),
            voids_event_id=(r.get("voids_event_id") or "").strip() or None,
            event_date_raw=r.get("event_date") or "",
            received_at_raw=r.get("received_at") or "",
            note=r.get("note") or "",
            line_no=i,
        ))
    return out
