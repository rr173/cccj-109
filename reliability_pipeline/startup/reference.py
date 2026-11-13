"""独立的小规模参考判定器。

与 ``engine.py`` 刻意保持不同的实现路线：

- 不维护任何「状态机」：先把每个角色的测量整理成**事件表**（每个时刻一条：
  合法更新或非法冲突），再用 ``value_at(role, t)`` 以二分查找直接回答
  「t 时刻该角色的零阶保持值」；
- 候选的判定通过**枚举事件断点**完成：全部测量时刻、合法读数的过期时刻、
  以及候选派生时刻（s、s+C、s+W、确认时刻、连续段起点+H）；
- 允许 O（断点数²) 的朴素扫描——只用于小规模核对，不参与生产路径。

本模块不导入 ``startup.engine``；期望答案不由生产状态机生成。
两者对同一输入独立给出判定，供 ``verify`` 与测试逐条比对。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from typing import Dict, List, Optional, Tuple

from .model import (
    Attempt, SampleRow,
    REASON_CONFIRM_TIMEOUT, REASON_CONFIRM_TIMEOUT_UNKNOWN,
    REASON_OBSERVATION_INCOMPLETE, REASON_STABLE_COMPLETED,
    REASON_STABLE_TIMEOUT, REASON_STABLE_TIMEOUT_UNKNOWN,
    REASON_TEMP_UNKNOWN_BEFORE_COMPLETION, REASON_TEMPERATURE_EXCEEDED,
    STATUS_CONFIRMED, STATUS_INDETERMINATE, STATUS_REJECTED,
)
from .recipe import ROLES, Recipe


class _ChannelTimeline:
    """一个角色通道的事件表与零阶保持求值（独立实现）。"""

    def __init__(self, rows: List[SampleRow], expected_unit: str, max_age_ns: int):
        self.max_age_ns = max_age_ns
        # 同一 ts 的多条读数：值与单位全同且符合预期 → 合法更新；否则非法（未知）
        by_ts: Dict[int, List[SampleRow]] = {}
        for r in rows:
            by_ts.setdefault(r.ts_ns, []).append(r)
        self.events: List[Tuple[int, bool, float]] = []   # (ts, 合法?, 值)
        for ts in sorted(by_ts):
            group = by_ts[ts]
            values = {g.value for g in group}
            units = {g.unit for g in group}
            legal = (len(values) == 1 and units == {expected_unit}
                     and math.isfinite(next(iter(values))))
            self.events.append((ts, legal, next(iter(values)) if legal else 0.0))
        self.ts_list = [e[0] for e in self.events]

    def value_at(self, t: int) -> Optional[float]:
        """t 时刻的零阶保持值；未知（无读数/已过期/冲突）返回 None。"""
        i = bisect_right(self.ts_list, t) - 1
        if i < 0:
            return None
        ts, legal, value = self.events[i]
        if not legal or t - ts >= self.max_age_ns:
            return None
        return value

    def unknown_intervals(self) -> List[Tuple[Optional[int], Optional[int]]]:
        """未知区间序列（半开 [start, end)；None 端表示无界）。"""
        out: List[Tuple[Optional[int], Optional[int]]] = []
        prev_ts: Optional[int] = None
        prev_legal = False
        for ts, legal, _ in self.events:
            if prev_ts is None:
                out.append((None, ts))                    # 首事件之前
            else:
                gap_start = prev_ts + self.max_age_ns if prev_legal else prev_ts
                if gap_start < ts:
                    out.append((gap_start, ts))
            prev_ts, prev_legal = ts, legal
        if prev_ts is None:
            out.append((None, None))                      # 全程无读数
        else:
            tail = prev_ts + self.max_age_ns if prev_legal else prev_ts
            out.append((tail, None))
        return out


def _intersects(intervals, a: int, b: int) -> bool:
    """半开区间序列与闭区间 [a, b] 是否相交。"""
    for start, end in intervals:
        lo = start if start is not None else float("-inf")
        hi = end if end is not None else float("inf")
        if lo <= b and hi > a:
            return True
    return False


def reference_detect(rows: List[SampleRow], recipe: Recipe, analysis_end_ns: int,
                     device_id: int, boot_seq: int) -> List[Attempt]:
    """对单个 (device_id, boot_seq) 组给出独立判定（枚举事件断点）。"""
    rows = [r for r in rows
            if r.ts_ns <= analysis_end_ns
            and recipe.role_of_channel(r.channel) is not None]
    timelines: Dict[str, _ChannelTimeline] = {}
    for role in ROLES:
        ch = recipe.channels[role]
        timelines[role] = _ChannelTimeline(
            [r for r in rows if r.channel == ch.channel], ch.unit, ch.max_age_ns)
    unknowns = {role: timelines[role].unknown_intervals() for role in ROLES}

    # ---- 越线：压力由「已知 <P_start 且前值在 s 未过期」变为 >=P_start ----
    crossings: List[int] = []
    p_events = timelines["pressure"].events
    for i, (ts, legal, value) in enumerate(p_events):
        if not legal or value < recipe.P_start or i == 0:
            continue
        p_ts, p_legal, p_value = p_events[i - 1]
        if (p_legal and p_value < recipe.P_start
                and ts - p_ts < recipe.channels["pressure"].max_age_ns):
            crossings.append(ts)

    attempts: List[Attempt] = []
    for seq, s in enumerate(crossings):
        attempts.append(_judge_candidate(
            seq, s, timelines, unknowns, recipe, analysis_end_ns,
            device_id, boot_seq))
    return attempts


def _judge_candidate(seq: int, s: int, timelines, unknowns, recipe: Recipe,
                     analysis_end_ns: int, device_id: int,
                     boot_seq: int) -> Attempt:
    temp, vib, pres = (timelines["temperature"], timelines["vibration"],
                       timelines["pressure"])

    # ---- 温度首次达到上限的时刻（候选存活视角：不早于 s）----
    t_viol: Optional[int] = None
    v0 = temp.value_at(s)
    if v0 is not None and v0 >= recipe.T_max:
        t_viol = s
    else:
        horizon = min(s + recipe.W, analysis_end_ns)
        for ts, legal, value in temp.events:
            if ts <= s:
                continue
            if ts > horizon:
                break
            if legal and value >= recipe.T_max:
                t_viol = ts
                break

    # ---- 确认时刻：[s, s+C] 内振动首次 >=V_run（同刻超限优先，须严格更早）----
    t_conf: Optional[int] = None
    if t_viol is None or s < t_viol:
        v0 = vib.value_at(s)
        if v0 is not None and v0 >= recipe.V_run:
            t_conf = s
        else:
            for ts, legal, value in vib.events:
                if ts <= s:
                    continue
                if ts > s + recipe.C:
                    break
                if t_viol is not None and ts >= t_viol:
                    break
                if legal and value >= recipe.V_run:
                    t_conf = ts
                    break

    # ---- 最早稳定完成时刻 c（枚举断点上的连续段）----
    c: Optional[int] = None
    if t_conf is not None:
        c = _earliest_completion(t_conf, s, timelines, recipe)
        if c is not None:
            if c > analysis_end_ns or (t_viol is not None and c >= t_viol):
                c = None

    # ---- 候选终结：同刻优先级 超限 > 完成 > 期限 > 截断 ----
    ends: List[Tuple[int, int, str]] = []
    if t_viol is not None:
        ends.append((t_viol, 0, "violation"))
    if c is not None:
        ends.append((c, 1, "completion"))
    if s + recipe.W <= analysis_end_ns:
        ends.append((s + recipe.W, 2, "deadline"))
    ends.append((analysis_end_ns, 3, "cut"))
    ends.sort(key=lambda e: (e[0], e[1]))
    end_ns, _, how = ends[0]

    at = Attempt(device_id=device_id, boot_seq=boot_seq, attempt_seq=seq,
                 s_ns=s, confirm_ns=t_conf, end_ns=end_ns)
    if how == "violation":
        at.status, at.reason = STATUS_REJECTED, REASON_TEMPERATURE_EXCEEDED
        at.detail = "候选存活期间温度达到上限"
    elif how == "completion":
        at.stable_start_ns, at.stable_end_ns = c - recipe.H, c
        if _intersects(unknowns["temperature"], s, c):
            at.status = STATUS_INDETERMINATE
            at.reason = REASON_TEMP_UNKNOWN_BEFORE_COMPLETION
            at.detail = "已找到最早稳定完成时刻，但此前安全温度有未知区间"
        else:
            at.status, at.reason = STATUS_CONFIRMED, REASON_STABLE_COMPLETED
            at.detail = "温度全程已知且低于上限，启机确认"
    elif how == "deadline":
        if t_conf is not None:
            base, unk = REASON_STABLE_TIMEOUT, REASON_STABLE_TIMEOUT_UNKNOWN
            participating = (
                _intersects(unknowns["temperature"], s, end_ns)
                or _intersects(unknowns["vibration"], s, end_ns)
                or _intersects(unknowns["pressure"], t_conf, end_ns))
        else:
            base, unk = REASON_CONFIRM_TIMEOUT, REASON_CONFIRM_TIMEOUT_UNKNOWN
            participating = (
                _intersects(unknowns["temperature"], s, end_ns)
                or _intersects(unknowns["vibration"], s, s + recipe.C))
        if participating:
            at.status, at.reason = STATUS_INDETERMINATE, unk
            at.detail = "窗口内存在参与判断的未知状态，不能算设备启动失败"
        else:
            at.status, at.reason = STATUS_REJECTED, base
            at.detail = "资料完整前提下的超时失败"
    else:
        at.status, at.reason = STATUS_INDETERMINATE, REASON_OBSERVATION_INCOMPLETE
        at.detail = "分析截止时观察未结束，不能报超时"
    return at


def _earliest_completion(t_conf: int, s: int, timelines,
                         recipe: Recipe) -> Optional[int]:
    """确认后压力>=P_run 且振动>=V_run 连续成立 H 的最早完成时刻（<=s+W）。

    断点 = 确认时刻、期限、压力/振动的事件时刻与合法读数过期时刻。
    连续段 [a, b) 上的完成时刻为 a+H：必须 a+H < b（完成点当刻条件仍成立；
    恰好到期的点不成立）且 a+H <= s+W。
    """
    deadline = s + recipe.W
    breakpoints = {t_conf, deadline}
    for role in ("pressure", "vibration"):
        tl = timelines[role]
        for ts, legal, _ in tl.events:
            if t_conf <= ts <= deadline:
                breakpoints.add(ts)
            if legal:
                exp = ts + tl.max_age_ns
                if t_conf < exp <= deadline:
                    breakpoints.add(exp)

    def k_holds(t: int) -> bool:
        p = timelines["pressure"].value_at(t)
        v = timelines["vibration"].value_at(t)
        return (p is not None and p >= recipe.P_run
                and v is not None and v >= recipe.V_run)

    run_start: Optional[int] = None
    for b in sorted(breakpoints):
        if k_holds(b):
            if run_start is None:
                run_start = b
        else:
            if run_start is not None:
                c = run_start + recipe.H
                if c < b and c <= deadline:
                    return c
                run_start = None
    if run_start is not None:
        c = run_start + recipe.H
        if c <= deadline:
            return c
    return None
