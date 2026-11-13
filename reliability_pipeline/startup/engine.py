"""生产状态机：按事件时刻推进的启机过程识别引擎。

工作方式（对应 docs/startup_detection.md 的论证）
------------------------------------------------
- 每个 ``(device_id, boot_seq)`` 组独立扫描一次有序测量批次（按 ts_ns 升序），
  布局号不参与状态键——布局更换但通道与单位仍匹配时，零阶保持状态不清空；
- 每个读数自其整数 ts_ns 起零阶保持，直到下一次更新或 ``ts_ns + max_age_ns``
  （过期点起为未知，不插值）；同刻刷新优先于同刻到期；
- 同一时刻所有通道的更新先一起生效，再统一判定越线、温度、确认、稳定完成
  与期限，结果不随数据库行顺序改变；
- 定时事件（读数过期、确认窗口关闭、稳定完成、总期限）进入堆中按时刻调度，
  只在事件时刻推进状态——不把纳秒区间展开成逐时刻网格，也不对候选反复
  扫描整库。内存开销为 O（存活候选数 + 每通道 O(1) 状态 + 本组证据日志）。

同刻处理顺序（同一 t 内）
-------------------------
1. 到期事件（过期点起为未知；同刻刷新随后覆盖，等价于刷新优先）；
2. 本时刻全部通道更新一起生效（同刻多条读数：值与单位全同才算一次合法
   更新并保留全部来源；冲突或单位不符 → 该通道转未知直到下次合法更新）；
3. 稳定条件 K = (P>=P_run 且 V>=V_run) 的跳变：成立→为搜索中的候选开始/
   重新开始累计并安排完成定时器；不再成立→打断累计并记录证据；
4. 越线：压力由「已知且 <P_start（前值在 s 仍未过期）」变为 >=P_start →
   新候选（前一次未结束也照常建立）；
5. 温度：已知且 >=T_max → 所有存活候选立即 rejected（优先于完成/期限）；
6. 确认：等待中的候选在 [s, s+C] 内首次见到振动 >=V_run（含 s 时刻）；
7. 确认窗口关闭定时器（t == s+C 仍未确认 → 转「窗口错过」，此后只观察温度）；
8. 稳定完成定时器（t == 段起点+H 且当刻 K 仍成立 → 找到完成时刻 c；
   完成点恰好过期则当刻 K 不成立，定时器失效，不能成功）；
9. 总期限定时器（t == s+W → 确认超时 / 稳定超时；有参与判断的未知状态
   则 indeterminate，否则 rejected）。

分析截止 analysis_end_ns 早于候选期限且候选尚未结束时，输出
indeterminate/observation_incomplete（观察未结束，不能报超时）。
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .model import (
    Attempt, EvidenceItem, SampleRow, SampleRef,
    EV_CONFIRM, EV_CONFIRM_WINDOW_MISSED, EV_CONFLICT, EV_CROSSING, EV_DEADLINE,
    EV_OBSERVATION_CUT, EV_STABLE_BREAK, EV_STABLE_COMPLETED, EV_TEMP_EXCEEDED,
    EV_TEMP_UNKNOWN, EV_UNIT_MISMATCH, EV_UNKNOWN_PARTICIPATING,
    REASON_CONFIRM_TIMEOUT, REASON_CONFIRM_TIMEOUT_UNKNOWN,
    REASON_OBSERVATION_INCOMPLETE, REASON_STABLE_COMPLETED,
    REASON_STABLE_TIMEOUT, REASON_STABLE_TIMEOUT_UNKNOWN,
    REASON_TEMP_UNKNOWN_BEFORE_COMPLETION, REASON_TEMPERATURE_EXCEEDED,
    STATUS_CONFIRMED, STATUS_INDETERMINATE, STATUS_REJECTED,
)
from .recipe import ROLES, Recipe

# 堆事件种类
_T_EXPIRY = "expiry"               # 读数到期（payload: (role, 读数 ts)）
_T_WINDOW_CLOSE = "window_close"   # 确认窗口关闭（payload: attempt_seq）
_T_COMPLETION = "completion"       # 稳定完成检查（payload: (attempt_seq, seg_version)）
_T_DEADLINE = "deadline"           # 总期限（payload: attempt_seq）

# 未知区间起因
_CAUSE_NO_READING = "no_reading"         # 尚无读数
_CAUSE_EXPIRED = "expired"               # 读数过期
_CAUSE_CONFLICT = "conflict"             # 同刻读数冲突
_CAUSE_UNIT_MISMATCH = "unit_mismatch"   # 单位与配方不符
_CAUSE_NONFINITE = "nonfinite_value"     # 非有限值（防御性；接入层本应拒收）


@dataclass
class _ChannelState:
    """一个角色通道的零阶保持状态。"""

    known: bool = False
    value: float = math.nan
    ts: int = 0                        # 当前读数时刻
    sources: Tuple[SampleRef, ...] = ()


@dataclass
class _Candidate:
    """一次启机尝试（候选）的独立状态。"""

    attempt_seq: int
    s: int
    state: str = "awaiting"            # awaiting / seeking / missed
    confirm_ns: Optional[int] = None
    acc_start: Optional[int] = None    # 当前连续稳定段起点（seeking 且 K 成立中）
    seg_version: int = 0               # 段版本：打断/重新开始时递增，使旧定时器失效
    stable_start: Optional[int] = None
    stable_end: Optional[int] = None
    done: bool = False
    end_ns: int = 0
    status: str = ""
    reason: str = ""
    detail: str = ""
    evidence: List[EvidenceItem] = field(default_factory=list)


class _GroupEngine:
    """单个 (device_id, boot_seq) 组的事件驱动状态机。"""

    def __init__(self, recipe: Recipe, analysis_end_ns: int,
                 device_id: int, boot_seq: int) -> None:
        self.recipe = recipe
        self.analysis_end_ns = analysis_end_ns
        self.device_id = device_id
        self.boot_seq = boot_seq
        self.states: Dict[str, _ChannelState] = {r: _ChannelState() for r in ROLES}
        # 每角色未知区间日志：(start|None=-inf, end|None=+inf, cause, refs)
        self.unknown_logs: Dict[str, List[Tuple[Optional[int], Optional[int], str,
                                                Tuple[SampleRef, ...]]]] = {
            r: [] for r in ROLES}
        # 当前开着的未知区间：(start|None, cause, refs)；初始为「尚无读数」
        self.open_unknown: Dict[str, Optional[Tuple[Optional[int], str,
                                                    Tuple[SampleRef, ...]]]] = {
            r: (None, _CAUSE_NO_READING, ()) for r in ROLES}
        self.heap: List[Tuple[int, int, str, object]] = []
        self._seq = itertools.count()
        self.candidates: List[_Candidate] = []
        self.k_prev = False

    # ---- 定时事件 ------------------------------------------------------

    def _push(self, t: int, kind: str, payload: object) -> None:
        heapq.heappush(self.heap, (t, next(self._seq), kind, payload))

    # ---- 未知区间日志 --------------------------------------------------

    def _become_unknown(self, role: str, t: int, cause: str,
                        refs: Tuple[SampleRef, ...]) -> None:
        st = self.states[role]
        st.known = False
        st.value = math.nan
        st.sources = ()
        if self.open_unknown[role] is None:
            self.open_unknown[role] = (t, cause, refs)
        # 已连续未知（如过期后又遇冲突）：保留最早起点，不重复开区间

    def _become_known(self, role: str, t: int, value: float,
                      refs: Tuple[SampleRef, ...]) -> None:
        self.states[role] = _ChannelState(True, value, t, refs)
        open_iv = self.open_unknown[role]
        if open_iv is not None:
            start, cause, cause_refs = open_iv
            if start is None or start < t:
                # 零长区间（同刻到期+刷新）不入日志
                self.unknown_logs[role].append((start, t, cause, cause_refs))
            self.open_unknown[role] = None

    def _unknown_intersect(self, role: str, a: int, b: int):
        """角色在闭区间 [a, b] 内的未知区间（含当前开着的）。"""
        out = []
        items = list(self.unknown_logs[role])
        open_iv = self.open_unknown[role]
        if open_iv is not None:
            start, cause, refs = open_iv
            items.append((start, None, cause, refs))
        for start, end, cause, refs in items:
            lo = start if start is not None else -math.inf
            hi = end if end is not None else math.inf
            if lo <= b and hi > a:      # 半开区间 [lo,hi) 与闭区间 [a,b] 相交
                out.append((start, end, cause, refs))
        return out

    # ---- 候选辅助 ------------------------------------------------------

    def _alive(self) -> Iterable[_Candidate]:
        return (c for c in self.candidates if not c.done)

    def _k_now(self) -> bool:
        p, v = self.states["pressure"], self.states["vibration"]
        return (p.known and p.value >= self.recipe.P_run
                and v.known and v.value >= self.recipe.V_run)

    def _finish(self, cand: _Candidate, t: int, status: str, reason: str,
                detail: str) -> None:
        cand.done = True
        cand.end_ns = t
        cand.status = status
        cand.reason = reason
        cand.detail = detail

    # ---- 主循环 ----------------------------------------------------------

    def run(self, rows: List[SampleRow]) -> List[Attempt]:
        instants = self._group_instants(rows)
        next_instant = next(instants, None)
        while True:
            t_heap = self.heap[0][0] if self.heap else None
            t_stream = next_instant[0] if next_instant is not None else None
            if t_heap is None and t_stream is None:
                break
            if t_stream is not None and (t_heap is None or t_stream <= t_heap):
                t = t_stream
            else:
                t = t_heap
            if t > self.analysis_end_ns:
                break
            expiries: List[Tuple[str, int]] = []
            timers: List[Tuple[str, object]] = []
            while self.heap and self.heap[0][0] == t:
                _, _, kind, payload = heapq.heappop(self.heap)
                if kind == _T_EXPIRY:
                    expiries.append(payload)
                else:
                    timers.append((kind, payload))
            updates: Dict[str, List[SampleRow]] = {}
            if t_stream == t:
                updates = next_instant[1]
                next_instant = next(instants, None)
            self._process_instant(t, expiries, updates, timers)
        # 分析截止：仍未结束的候选 → 观察未结束（不能报超时）
        for cand in self._alive():
            cand.evidence.append(EvidenceItem(
                EV_OBSERVATION_CUT, self.analysis_end_ns,
                detail=f"分析截止 {self.analysis_end_ns} 时候选仍未结束"
                       f"（期限 s+W={cand.s + self.recipe.W} 尚未到达），"
                       "观察未结束，不能报超时"))
            self._finish(cand, self.analysis_end_ns, STATUS_INDETERMINATE,
                         REASON_OBSERVATION_INCOMPLETE,
                         "分析截止时观察未结束，不能报超时")
        return [self._to_attempt(c) for c in self.candidates]

    def _group_instants(self, rows: List[SampleRow]):
        """把有序行流按 ts_ns 分组为 (t, {role: [rows]})；过滤配方外通道。"""
        cur_t: Optional[int] = None
        cur: Dict[str, List[SampleRow]] = {}
        prev_ts: Optional[int] = None
        for row in rows:
            if row.ts_ns > self.analysis_end_ns:
                break
            if prev_ts is not None and row.ts_ns < prev_ts:
                raise ValueError("测量行必须按 ts_ns 升序提供（引擎契约）")
            prev_ts = row.ts_ns
            role = self.recipe.role_of_channel(row.channel)
            if role is None:
                continue
            if cur_t is None:
                cur_t = row.ts_ns
            if row.ts_ns != cur_t:
                yield cur_t, cur
                cur_t, cur = row.ts_ns, {}
            cur.setdefault(role, []).append(row)
        if cur_t is not None and cur:
            yield cur_t, cur

    # ---- 同一时刻的处理 --------------------------------------------------

    def _process_instant(self, t: int, expiries, updates, timers) -> None:
        # 1. 到期（过期点起为未知；同刻刷新在下一步覆盖，等价于刷新优先）
        for role, version_ts in expiries:
            st = self.states[role]
            if st.known and st.ts == version_ts:
                self._become_unknown(role, t, _CAUSE_EXPIRED, st.sources)
        # 2. 本时刻全部通道更新一起生效
        pressure_prev: Optional[Tuple[bool, float, Tuple[SampleRef, ...]]] = None
        pressure_legal = False
        for role in ROLES:
            rows_at = updates.get(role)
            if not rows_at:
                continue
            if role == "pressure":
                st = self.states[role]
                pressure_prev = (st.known, st.value, st.sources)
                pressure_legal = self._apply_update(role, t, rows_at)
            else:
                self._apply_update(role, t, rows_at)
        # 3. 稳定条件跳变
        k_now = self._k_now()
        if k_now != self.k_prev:
            self._on_k_transition(t, k_now)
            self.k_prev = k_now
        # 4. 越线（前值已知 <P_start 且在 s 未过期，新值 >=P_start）
        if pressure_legal and pressure_prev is not None:
            prev_known, prev_value, prev_sources = pressure_prev
            new_value = self.states["pressure"].value
            if (prev_known and prev_value < self.recipe.P_start
                    and new_value >= self.recipe.P_start):
                self._create_candidate(t, prev_sources)
        # 5. 温度达到上限：所有存活候选立即 rejected（优先于完成/期限）
        temp = self.states["temperature"]
        if temp.known and temp.value >= self.recipe.T_max:
            for cand in list(self._alive()):
                cand.evidence.append(EvidenceItem(
                    EV_TEMP_EXCEEDED, t, role="temperature",
                    detail=f"温度 {temp.value} 达到上限 T_max="
                           f"{self.recipe.T_max}，候选存活期间立即拒绝",
                    refs=list(temp.sources)))
                self._finish(cand, t, STATUS_REJECTED,
                             REASON_TEMPERATURE_EXCEEDED,
                             f"温度 {temp.value} 达到上限 T_max={self.recipe.T_max}")
        # 6. 确认：[s, s+C] 内振动首次 >=V_run（含 s 时刻已满足）
        vib = self.states["vibration"]
        if vib.known and vib.value >= self.recipe.V_run:
            for cand in list(self._alive()):
                if cand.state == "awaiting" and t <= cand.s + self.recipe.C:
                    self._confirm(t, cand)
        # 7. 确认窗口关闭
        for kind, payload in timers:
            if kind == _T_WINDOW_CLOSE:
                self._on_window_close(t, payload)
        # 8. 稳定完成
        for kind, payload in timers:
            if kind == _T_COMPLETION:
                self._on_completion(t, payload)
        # 9. 总期限
        for kind, payload in timers:
            if kind == _T_DEADLINE:
                self._on_deadline(t, payload)

    # ---- 通道更新 --------------------------------------------------------

    def _apply_update(self, role: str, t: int, rows: List[SampleRow]) -> bool:
        """同刻多条读数：值与单位全同且符合配方才算一次合法更新。返回是否合法。"""
        expected = self.recipe.channels[role].unit
        refs = tuple(r.ref() for r in rows)
        values = {r.value for r in rows}
        units = {r.unit for r in rows}
        legal = (len(values) == 1 and units == {expected}
                 and math.isfinite(next(iter(values))))
        if legal:
            value = next(iter(values))
            self._become_known(role, t, value, refs)
            self._push(t + self.recipe.channels[role].max_age_ns,
                       _T_EXPIRY, (role, t))
            return True
        # 非法：冲突 / 单位不符 / 非有限 → 通道转未知直到下次合法更新
        if len(values) > 1:
            cause = _CAUSE_CONFLICT
            kind = EV_CONFLICT
            what = f"同刻 {len(rows)} 条读数值不一致：{sorted(values)}"
        elif units != {expected}:
            cause = _CAUSE_UNIT_MISMATCH
            kind = EV_UNIT_MISMATCH
            what = f"读数单位 {sorted(units)} 与配方预期 {expected!r} 不符"
        else:
            cause = _CAUSE_NONFINITE
            kind = EV_CONFLICT
            what = f"读数值非有限：{sorted(values)}"
        self._become_unknown(role, t, cause, refs)
        for cand in self._alive():
            cand.evidence.append(EvidenceItem(
                kind, t, role=role,
                detail=f"{what}，通道 {self.recipe.channels[role].channel} "
                       "转为未知直到下次合法更新",
                refs=list(refs)))
        return False

    # ---- 稳定条件跳变 ----------------------------------------------------

    def _break_cause(self) -> Tuple[str, List[SampleRef]]:
        """当前稳定条件不成立的原因（供打断证据）。"""
        r = self.recipe
        p, v = self.states["pressure"], self.states["vibration"]
        if not p.known:
            cause, refs = self._unknown_cause("pressure")
            return f"压力未知（{cause}）", refs
        if p.value < r.P_run:
            return f"压力 {p.value} 低于稳定下限 P_run={r.P_run}", list(p.sources)
        if not v.known:
            cause, refs = self._unknown_cause("vibration")
            return f"振动未知（{cause}）", refs
        return f"振动 {v.value} 低于下限 V_run={r.V_run}", list(v.sources)

    def _unknown_cause(self, role: str) -> Tuple[str, List[SampleRef]]:
        open_iv = self.open_unknown[role]
        if open_iv is None:
            return "未知", []
        _, cause, refs = open_iv
        return cause, list(refs)

    def _on_k_transition(self, t: int, k_now: bool) -> None:
        if not k_now:
            why, refs = self._break_cause()
            for cand in self._alive():
                if cand.state == "seeking" and cand.acc_start is not None:
                    cand.evidence.append(EvidenceItem(
                        EV_STABLE_BREAK, t,
                        detail=f"连续稳定在 {t} 被打断：{why}；累计清零", refs=refs))
                    cand.acc_start = None
                    cand.seg_version += 1
        else:
            for cand in self._alive():
                if cand.state == "seeking":
                    cand.acc_start = t
                    cand.seg_version += 1
                    self._schedule_completion(cand, t)

    def _schedule_completion(self, cand: _Candidate, acc_start: int) -> None:
        c = acc_start + self.recipe.H
        if c <= cand.s + self.recipe.W:      # 超过期限的完成无意义，期限先到
            self._push(c, _T_COMPLETION, (cand.attempt_seq, cand.seg_version))

    # ---- 候选事件 --------------------------------------------------------

    def _create_candidate(self, t: int, prev_sources: Tuple[SampleRef, ...]) -> None:
        r = self.recipe
        cand = _Candidate(attempt_seq=len(self.candidates), s=t)
        refs = list(self.states["pressure"].sources) + list(prev_sources)
        cand.evidence.append(EvidenceItem(
            EV_CROSSING, t, role="pressure",
            detail=f"压力由已知 <P_start={r.P_start} 越线为 "
                   f"{self.states['pressure'].value}（>={r.P_start}），"
                   f"建立候选 #{cand.attempt_seq}；确认窗口 [{t}, {t + r.C}]，"
                   f"期限 {t + r.W}",
            refs=refs))
        self.candidates.append(cand)
        self._push(t + r.C, _T_WINDOW_CLOSE, cand.attempt_seq)
        self._push(t + r.W, _T_DEADLINE, cand.attempt_seq)

    def _confirm(self, t: int, cand: _Candidate) -> None:
        vib = self.states["vibration"]
        cand.state = "seeking"
        cand.confirm_ns = t
        cand.evidence.append(EvidenceItem(
            EV_CONFIRM, t, role="vibration",
            detail=f"振动 {vib.value} 达到 V_run={self.recipe.V_run}，"
                   "候选确认，开始寻找连续稳定段",
            refs=list(vib.sources)))
        if self._k_now():
            cand.acc_start = t
            cand.seg_version += 1
            self._schedule_completion(cand, t)

    def _on_window_close(self, t: int, attempt_seq: int) -> None:
        cand = self.candidates[attempt_seq]
        if cand.done or cand.state != "awaiting":
            return
        cand.state = "missed"
        cand.evidence.append(EvidenceItem(
            EV_CONFIRM_WINDOW_MISSED, t, role="vibration",
            detail=f"确认窗口 [{cand.s}, {cand.s + self.recipe.C}] 内振动未"
                   f"达到 V_run={self.recipe.V_run}，窗口错过；"
                   "此后不再寻找稳定段，仅继续观察温度至总期限"))

    def _on_completion(self, t: int, payload) -> None:
        attempt_seq, seg_version = payload
        cand = self.candidates[attempt_seq]
        if (cand.done or cand.state != "seeking"
                or cand.seg_version != seg_version or cand.acc_start is None):
            return                                  # 段已被打断/重计，定时器失效
        if not self._k_now():
            return                                  # 完成点当刻条件不成立（如恰好过期）
        c = t
        cand.stable_start, cand.stable_end = c - self.recipe.H, c
        cand.evidence.append(EvidenceItem(
            EV_STABLE_COMPLETED, cand.stable_start, t_end_ns=c,
            detail=f"压力>={self.recipe.P_run} 且振动>={self.recipe.V_run} "
                   f"连续成立 H={self.recipe.H}，最早完成时刻 c={c}",
            refs=list(self.states["pressure"].sources)
                 + list(self.states["vibration"].sources)))
        unknowns = self._unknown_intersect("temperature", cand.s, c)
        if not unknowns:
            self._finish(cand, c, STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                         f"[s, c]=[{cand.s}, {c}] 温度全程已知且低于 "
                         f"T_max={self.recipe.T_max}，启机确认")
        else:
            for start, end, cause, refs in unknowns:
                cand.evidence.append(EvidenceItem(
                    EV_TEMP_UNKNOWN, start if start is not None else cand.s,
                    t_end_ns=end, role="temperature",
                    detail=f"安全温度存在未知区间（起因 {cause}），"
                           "无法判定全程低于上限",
                    refs=list(refs)))
            self._finish(cand, c, STATUS_INDETERMINATE,
                         REASON_TEMP_UNKNOWN_BEFORE_COMPLETION,
                         "已找到最早稳定完成时刻，但此前安全温度有未知区间")

    def _on_deadline(self, t: int, attempt_seq: int) -> None:
        cand = self.candidates[attempt_seq]
        if cand.done:
            return
        r = self.recipe
        if cand.state == "seeking":
            base_reason = REASON_STABLE_TIMEOUT
            unknown_reason = REASON_STABLE_TIMEOUT_UNKNOWN
            base_detail = "确认后至总期限未出现连续 H 的稳定段"
            windows = [("temperature", cand.s, t), ("vibration", cand.s, t),
                       ("pressure", cand.confirm_ns, t)]
        else:
            base_reason = REASON_CONFIRM_TIMEOUT
            unknown_reason = REASON_CONFIRM_TIMEOUT_UNKNOWN
            base_detail = (f"确认窗口 [{cand.s}, {cand.s + r.C}] 内振动未"
                           f"达到 V_run={r.V_run}")
            windows = [("temperature", cand.s, t),
                       ("vibration", cand.s, cand.s + r.C)]
        cand.evidence.append(EvidenceItem(EV_DEADLINE, t,
                                          detail=f"到达总期限 s+W={t}：{base_detail}"))
        participating = []
        for role, a, b in windows:
            for start, end, cause, refs in self._unknown_intersect(role, a, b):
                participating.append((role, start, end, cause, refs))
        if not participating:
            self._finish(cand, t, STATUS_REJECTED, base_reason,
                         f"{base_detail}；窗口内无参与判断的未知状态，资料完整")
            return
        for role, start, end, cause, refs in participating:
            cand.evidence.append(EvidenceItem(
                EV_UNKNOWN_PARTICIPATING,
                start if start is not None else cand.s, t_end_ns=end, role=role,
                detail=f"参与判定的 {role} 未知区间（起因 {cause}），"
                       "无法区分真实失败与资料缺失",
                refs=list(refs)))
        self._finish(cand, t, STATUS_INDETERMINATE, unknown_reason,
                     f"{base_detail}；但窗口内存在参与判断的未知状态，"
                     "不能算设备启动失败")

    # ---- 输出 ------------------------------------------------------------

    def _to_attempt(self, cand: _Candidate) -> Attempt:
        return Attempt(
            device_id=self.device_id, boot_seq=self.boot_seq,
            attempt_seq=cand.attempt_seq, s_ns=cand.s,
            confirm_ns=cand.confirm_ns,
            stable_start_ns=cand.stable_start, stable_end_ns=cand.stable_end,
            end_ns=cand.end_ns, status=cand.status, reason=cand.reason,
            detail=cand.detail, evidence=cand.evidence)


def detect_group(rows: List[SampleRow], recipe: Recipe, analysis_end_ns: int,
                 device_id: int, boot_seq: int) -> List[Attempt]:
    """识别单个 (device_id, boot_seq) 组内的全部启机尝试。

    ``rows`` 必须按 ts_ns 升序；只取配方通道、ts_ns <= analysis_end_ns 的行。
    """
    engine = _GroupEngine(recipe, analysis_end_ns, device_id, boot_seq)
    return engine.run(rows)


def iter_groups(con, recipe: Recipe, analysis_end_ns: int):
    """按 (device_id, boot_seq) 组流式产出 (group_key, [SampleRow])。

    只取配方通道、ts_ns <= analysis_end_ns 的行，按组与 ts_ns 升序。
    每次只物化一个组，内存与本组事件量同阶。
    """
    channels = [recipe.channels[r].channel for r in ROLES]
    sql = ("SELECT device_id, boot_seq, channel, ts_ns, value, unit, "
           "file_id, frame_offset, ordinal FROM tele_samples "
           "WHERE ts_ns <= ? AND channel IN (?, ?, ?) "
           "ORDER BY device_id, boot_seq, ts_ns")
    cur = con.execute(sql, [analysis_end_ns, *channels])
    group_key: Optional[Tuple[int, int]] = None
    group_rows: List[SampleRow] = []
    while True:
        batch = cur.fetchmany(4096)
        if not batch:
            break
        for row in batch:
            key = (row[0], row[1])
            if key != group_key:
                if group_key is not None and group_rows:
                    yield group_key, group_rows
                group_key = key
                group_rows = []
            group_rows.append(SampleRow(
                device_id=row[0], boot_seq=row[1], channel=row[2], ts_ns=row[3],
                value=row[4], unit=row[5], file_id=row[6], frame_offset=row[7],
                ordinal=row[8]))
    if group_key is not None and group_rows:
        yield group_key, group_rows


def run_detection(con, recipe: Recipe, analysis_end_ns: int) -> List[Attempt]:
    """在 DuckDB 连接上运行识别：按组流式读取 ``tele_samples`` 并逐组判定。"""
    attempts: List[Attempt] = []
    for (device_id, boot_seq), rows in iter_groups(con, recipe, analysis_end_ns):
        attempts.extend(detect_group(rows, recipe, analysis_end_ns,
                                     device_id, boot_seq))
    return attempts
