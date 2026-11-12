"""队列推导：作废解析、记录校验、设备时间段生成。

处理政策（全部在 README 中同样列明）
--------------------------------------
1. 知识截点：仅 ``received_at <= knowledge_cutoff`` 的事件可见；
   截点后到达的记录（包括撤销）对本次分析完全不可见，更不能改变
   更早的已保存分析结果。
2. 作废（void）：通过 ``voids_event_id`` 引用旧事件，禁止覆写原记录。
   - 作废目标在截点前不可见（不存在或尚未收到）→ 该 void 记录拒收
     （``void_target_unknown``），本次分析中无效。
   - 作废可以指向另一条 void（即"撤销的撤销"），被作废的 void 失去效力，
     其原本作废的事件恢复有效。
   - 作废引用形成循环（如 A 作废 B、B 作废 A，含自引用）→ 循环上所有
     事件一律拒收（``void_cycle``），其作废边全部忽略。
3. 记录级拒收：日期非法（``invalid_event_date`` / ``invalid_received_at``）、
   未知事件类型（``unknown_event_type``）、未知设备（``unknown_device``）、
   事件日期早于投产日（``event_before_commission``）、作废缺少目标
   （``void_missing_target``）、event_id 重复（``duplicate_event_id``，
   保留先出现的一条，其余拒收——禁止覆写）。
4. 设备级拒收：基础日期非法（``invalid_device_dates``）、入组早于投产
   （``enroll_before_commission``）、缺少机型（``missing_machine_type``）、
   入组前已存在有效终止事件（``terminated_before_enroll``）。
5. 观察窗口：``event_date > observation_end`` 的有效事件不构成结局，
   设备在 ``observation_end`` 处删失（``censored_end``）。
6. 每台设备取入组后（``event_date >= enroll_date``）第一项有效终止事件；
   同日多事件按 ``EVENT_PRIORITY`` + ``event_id`` 决胜。
7. ``L >= T`` 的设备无法贡献观察时长 → ``excluded_no_time``，不参与估计。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from .model import (
    ALL_EVENT_TYPES,
    COHORT_EXCLUDED_NO_TIME,
    COHORT_INCLUDED,
    COHORT_REJECTED,
    EVENT_PRIORITY,
    EVENT_VOID,
    EVENT_FAILURE,
    EVENT_RETIREMENT,
    EVENT_LOSS,
    STATUS_CENSORED_END,
    STATUS_CENSORED_LOSS,
    STATUS_FAILURE,
    STATUS_RETIREMENT,
    TERMINAL_TYPES,
    Device,
    DeviceInterval,
    RawEvent,
)

_TERMINAL_TO_STATUS = {
    EVENT_FAILURE: STATUS_FAILURE,
    EVENT_RETIREMENT: STATUS_RETIREMENT,
    EVENT_LOSS: STATUS_CENSORED_LOSS,
}


@dataclass
class CohortResult:
    """一次分析的队列推导结果。"""

    knowledge_cutoff: date
    observation_end: date
    device_rows: List[dict] = field(default_factory=list)   # run_devices 行
    intervals: List[DeviceInterval] = field(default_factory=list)  # 仅 included
    rejections: List[dict] = field(default_factory=list)    # 拒收明细
    event_audit: List[dict] = field(default_factory=list)   # 事件处置台账


def _reject(rejections: List[dict], level: str, entity_id: str, reason: str, detail: str) -> None:
    rejections.append(
        {"level": level, "entity_id": entity_id, "reason": reason, "detail": detail}
    )


def _resolve_voids(
    events: List[RawEvent],
    rejections: List[dict],
    audit: Dict[str, str],
) -> set:
    """在可见且记录级有效的事件集合上解析作废关系，返回被作废的 event_id 集合。

    分两步：
    1. 结构性校验：缺目标、目标不可见、循环引用 → 拒收相关 void。
    2. 在剩余有向无环的作废图上做记忆化递归：
       事件被作废 当且仅当 存在一条指向它的、来自"未被作废的 void"的边。
    """
    rejected_ids = set()
    by_id = {e.event_id: e for e in events}

    voids = [e for e in events if e.event_type == EVENT_VOID]

    # --- 结构校验：缺目标 / 目标不可见
    active: Dict[str, str] = {}  # void event_id -> target event_id
    for v in voids:
        tgt = (v.voids_event_id or "").strip()
        if not tgt:
            _reject(rejections, "event", v.event_id, "void_missing_target",
                    "作废记录缺少 voids_event_id")
            rejected_ids.add(v.event_id)
            audit[v.event_id] = "rejected:void_missing_target"
            continue
        if tgt not in by_id:
            _reject(rejections, "event", v.event_id, "void_target_unknown",
                    f"作废目标 {tgt} 在知识截点前不可见（不存在、尚未收到或已被拒收）")
            rejected_ids.add(v.event_id)
            audit[v.event_id] = "rejected:void_target_unknown"
            continue
        active[v.event_id] = tgt

    # --- 循环检测：只有 void 事件有出边（出度 <= 1），函数图染色找环
    state: Dict[str, str] = {}  # 'visiting' / 'done'
    cycle_nodes = set()
    for start in active:
        if state.get(start):
            continue
        path: List[str] = []
        cur: Optional[str] = start
        while cur is not None and cur in active and state.get(cur) is None:
            state[cur] = "visiting"
            path.append(cur)
            cur = active[cur]
        if cur is not None and cur in active and state.get(cur) == "visiting":
            cycle_nodes.update(path[path.index(cur):])
        for n in path:
            state[n] = "done"
    for n in sorted(cycle_nodes):
        _reject(rejections, "event", n, "void_cycle",
                "作废引用构成循环，循环上所有事件一并拒收")
        rejected_ids.add(n)
        audit[n] = "rejected:void_cycle"
        active.pop(n, None)

    # --- 记忆化递归求被作废集合（剩余图为 DAG，递归必终止）
    incoming: Dict[str, List[str]] = defaultdict(list)
    for vid, tgt in active.items():
        incoming[tgt].append(vid)

    voided_cache: Dict[str, bool] = {}

    def is_voided(eid: str) -> bool:
        if eid not in voided_cache:
            voided_cache[eid] = any(is_active_void(v) for v in incoming.get(eid, ()))
        return voided_cache[eid]

    def is_active_void(vid: str) -> bool:
        return vid not in rejected_ids and not is_voided(vid)

    voided = {e.event_id for e in events if is_voided(e.event_id)}
    for vid in active:
        audit[vid] = "voided" if vid in voided else "void_applied"
    return voided


def derive_cohort(
    devices: List[Device],
    events: List[RawEvent],
    knowledge_cutoff: date,
    observation_end: date,
) -> CohortResult:
    """由原始资料推导设备时间段。纯函数，不写库。"""
    result = CohortResult(knowledge_cutoff=knowledge_cutoff, observation_end=observation_end)
    rejections = result.rejections
    audit: Dict[str, str] = {}

    # ---------- 1. 设备基础资料校验 ----------
    valid_devices: Dict[str, Device] = {}
    for d in devices:
        if d.commission_date is None or d.enroll_date is None:
            _reject(rejections, "device", d.device_id, "invalid_device_dates",
                    f"投产日/入组日非法: commission={d.commission_raw!r} enroll={d.enroll_raw!r}")
            continue
        if not d.machine_type.strip():
            _reject(rejections, "device", d.device_id, "missing_machine_type", "机型为空")
            continue
        if d.enroll_date < d.commission_date:
            _reject(rejections, "device", d.device_id, "enroll_before_commission",
                    f"入组日 {d.enroll_date} 早于投产日 {d.commission_date}")
            continue
        valid_devices[d.device_id] = d

    # ---------- 2. 事件记录级校验 ----------
    # 2a. event_id 去重：保留先出现者（禁止覆写）
    seen_ids = set()
    deduped: List[RawEvent] = []
    for e in events:
        if e.event_id in seen_ids:
            _reject(rejections, "event", e.event_id, "duplicate_event_id",
                    f"event_id 重复（行 {e.line_no}），保留先出现的记录")
            audit.setdefault(e.event_id, "rejected:duplicate_event_id")
            continue
        seen_ids.add(e.event_id)
        deduped.append(e)

    # 2b. 逐条校验 + 知识截点可见性
    known_device_ids = {d.device_id for d in devices}
    visible: List[RawEvent] = []
    for e in deduped:
        if e.received_at is None:
            _reject(rejections, "event", e.event_id, "invalid_received_at",
                    f"收到日期非法: {e.received_at_raw!r}")
            audit[e.event_id] = "rejected:invalid_received_at"
            continue
        if e.received_at > knowledge_cutoff:
            # 截点后到达（含迟到撤销）：本次分析不可见
            audit[e.event_id] = "not_visible_after_cutoff"
            continue
        if e.event_type not in ALL_EVENT_TYPES:
            _reject(rejections, "event", e.event_id, "unknown_event_type",
                    f"未知事件类型: {e.event_type!r}")
            audit[e.event_id] = "rejected:unknown_event_type"
            continue
        if e.event_date is None:
            _reject(rejections, "event", e.event_id, "invalid_event_date",
                    f"事件日期非法: {e.event_date_raw!r}")
            audit[e.event_id] = "rejected:invalid_event_date"
            continue
        dev = valid_devices.get(e.device_id)
        if dev is None:
            reason = "unknown_device" if e.device_id not in known_device_ids \
                else "device_rejected"
            _reject(rejections, "event", e.event_id, reason,
                    f"设备 {e.device_id} 不存在或已被拒收")
            audit[e.event_id] = f"rejected:{reason}"
            continue
        if e.event_type != EVENT_VOID and e.event_date < dev.commission_date:
            _reject(rejections, "event", e.event_id, "event_before_commission",
                    f"事件日期 {e.event_date} 早于投产日 {dev.commission_date}")
            audit[e.event_id] = "rejected:event_before_commission"
            continue
        visible.append(e)

    # ---------- 3. 作废解析 ----------
    voided = _resolve_voids(visible, rejections, audit)

    # ---------- 4. 逐设备推导时间段 ----------
    events_by_device: Dict[str, List[RawEvent]] = defaultdict(list)
    for e in visible:
        if e.event_id not in voided and e.event_type in TERMINAL_TYPES:
            events_by_device[e.device_id].append(e)

    for dev_id in sorted(valid_devices):
        dev = valid_devices[dev_id]
        L = (dev.enroll_date - dev.commission_date).days
        terminal_events = events_by_device.get(dev_id, [])

        # 4a. 入组前已终止 → 设备拒收
        pre_enroll = [e for e in terminal_events if e.event_date < dev.enroll_date]
        if pre_enroll:
            first = min(pre_enroll, key=lambda e: (e.event_date, EVENT_PRIORITY[e.event_type], e.event_id))
            _reject(rejections, "device", dev_id, "terminated_before_enroll",
                    f"入组日 {dev.enroll_date} 前已存在有效终止事件 {first.event_id}"
                    f"（{first.event_type} @ {first.event_date}）")
            for e in terminal_events:
                audit[e.event_id] = "pre_enroll_terminal"
            result.device_rows.append({
                "device_id": dev_id, "machine_type": dev.machine_type,
                "commission_date": dev.commission_date.isoformat(),
                "enroll_date": dev.enroll_date.isoformat(),
                "L": None, "T": None, "status": None,
                "cohort_status": COHORT_REJECTED,
                "exclude_reason": "terminated_before_enroll",
                "terminal_event_id": None,
            })
            continue

        # 4b. 观察窗口内、入组后的候选终止事件
        in_window = [e for e in terminal_events if e.event_date <= observation_end]
        beyond = [e for e in terminal_events if e.event_date > observation_end]
        for e in beyond:
            audit[e.event_id] = "beyond_observation_end"

        chosen: Optional[RawEvent] = None
        if in_window:
            chosen = min(in_window,
                         key=lambda e: (e.event_date, EVENT_PRIORITY[e.event_type], e.event_id))
            for e in in_window:
                if e is chosen:
                    continue
                if e.event_date == chosen.event_date:
                    audit[e.event_id] = "superseded_same_day"
                else:
                    audit[e.event_id] = "not_selected_later"

        if chosen is not None:
            T = (chosen.event_date - dev.commission_date).days
            status = _TERMINAL_TO_STATUS[chosen.event_type]
            terminal_event_id = chosen.event_id
            audit[chosen.event_id] = "terminal_selected"
        else:
            T = (observation_end - dev.commission_date).days
            status = STATUS_CENSORED_END
            terminal_event_id = None

        # 4c. 无法贡献观察时长（需要 L < t <= T 非空，即 L < T）
        if L >= T:
            cohort_status = COHORT_EXCLUDED_NO_TIME
            exclude_reason = f"no_time_at_risk (L={L} >= T={T})"
        else:
            cohort_status = COHORT_INCLUDED
            exclude_reason = ""

        result.device_rows.append({
            "device_id": dev_id, "machine_type": dev.machine_type,
            "commission_date": dev.commission_date.isoformat(),
            "enroll_date": dev.enroll_date.isoformat(),
            "L": L, "T": T, "status": status,
            "cohort_status": cohort_status,
            "exclude_reason": exclude_reason,
            "terminal_event_id": terminal_event_id,
        })
        if cohort_status == COHORT_INCLUDED:
            result.intervals.append(DeviceInterval(
                device_id=dev_id, machine_type=dev.machine_type,
                L=L, T=T, status=status, terminal_event_id=terminal_event_id,
            ))

    # ---------- 5. 事件台账落盘 ----------
    for e in deduped:
        result.event_audit.append({
            "event_id": e.event_id,
            "device_id": e.device_id,
            "event_type": e.event_type,
            "event_date": e.event_date.isoformat() if e.event_date else e.event_date_raw,
            "received_at": e.received_at.isoformat() if e.received_at else e.received_at_raw,
            "voids_event_id": e.voids_event_id or "",
            "disposition": audit.get(e.event_id, "voided" if e.event_id in voided else "unknown"),
        })

    # 按原因排序，保证输出确定性
    result.rejections.sort(key=lambda r: (r["level"], r["reason"], r["entity_id"]))
    result.event_audit.sort(key=lambda a: a["event_id"])
    return result
