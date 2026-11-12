"""生存分析估计器：Aalen–Johansen 累积发生概率 + 对照 Kaplan–Meier。

定义（离散日，风险集 ``R(t) = {i : L_i < t <= T_i}``，``n(t) = |R(t)|``）
---------------------------------------------------------------------------
逐结局时点 ``t``（当日至少发生一起故障或退役）统计：

- ``d1(t)``：故障数（关注结局），``d2(t)``：退役数（竞争结局），``d = d1+d2``
- ``c(t)``：当日删失数（失访 + 观察截止；当日删失者仍计入当日风险集，
  在结局之后退出）

总体生存函数（两类结局合并）::

    S(t) = prod_{s <= t} (1 - d(s) / n(s))

Aalen–Johansen 累积发生概率（故障 j=1，退役 j=2）::

    CIF_j(t) = sum_{s <= t} S(s-) * d_j(s) / n(s)

恒等式 ``S(t) = 1 - CIF_1(t) - CIF_2(t)`` 在离散递推下精确成立，
也是不变量核对之一。

对照估计（偏差展示）：把竞争退役**误当普通删失**的 Kaplan–Meier::

    S_KM(t) = prod_{s <= t} (1 - d1(s) / n(s))
    F_KM(t) = 1 - S_KM(t)  >=  CIF_1(t)

由于 ``S(s-) <= S_KM(s-)`` 恒成立，``F_KM`` 系统性高估故障累积概率；
只有当退役事件从未先于 ``t`` 发生时两者才相等。

离散同日处理顺序
----------------
1. 进入：``L = t`` 的设备在 ``t`` 日**之后**才进入风险集（即 ``L < t``）；
2. 结局：``T = t`` 的故障与退役同时计入分子，当日删失者仍在风险集内；
3. 删失：``T = t`` 的删失在结局处理之后退出；
4. 同一设备同日多终止事件已在队列推导阶段按固定优先级裁决。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import numpy as np

from .model import (
    GROUP_ALL,
    STATUS_CENSORED_END,
    STATUS_CENSORED_LOSS,
    STATUS_FAILURE,
    STATUS_RETIREMENT,
    DeviceInterval,
)

CENSOR_STATUSES = (STATUS_CENSORED_LOSS, STATUS_CENSORED_END)
OUTCOME_STATUSES = (STATUS_FAILURE, STATUS_RETIREMENT)


@dataclass
class GroupEstimate:
    """一个分组（机型或 ALL）的完整估计结果。"""

    group: str
    rows: List[dict] = field(default_factory=list)          # estimates 表行
    risk_members: Dict[int, List[str]] = field(default_factory=dict)  # t -> 风险集设备


def _estimate_one_group(group: str, intervals: List[DeviceInterval]) -> GroupEstimate:
    outcome_times = sorted({iv.T for iv in intervals if iv.status in OUTCOME_STATUSES})
    ge = GroupEstimate(group=group)
    if not outcome_times:
        return ge  # 纯删失组：无结局时点，曲线为空

    n_t, d1, d2, c_t = [], [], [], []
    for t in outcome_times:
        members = [iv for iv in intervals if iv.L < t <= iv.T]
        ge.risk_members[t] = sorted(iv.device_id for iv in members)
        n_t.append(len(members))
        d1.append(sum(1 for iv in members if iv.T == t and iv.status == STATUS_FAILURE))
        d2.append(sum(1 for iv in members if iv.T == t and iv.status == STATUS_RETIREMENT))
        c_t.append(sum(1 for iv in members if iv.T == t and iv.status in CENSOR_STATUSES))

    n = np.asarray(n_t, dtype=np.float64)
    f1 = np.asarray(d1, dtype=np.float64)
    f2 = np.asarray(d2, dtype=np.float64)
    d_all = f1 + f2

    # Aalen–Johansen 递推（向量化）
    surv = np.cumprod(1.0 - d_all / n)                    # S(t)
    surv_prev = np.concatenate(([1.0], surv[:-1]))        # S(t-)
    cif1 = np.cumsum(surv_prev * f1 / n)                  # 故障累积发生概率
    cif2 = np.cumsum(surv_prev * f2 / n)                  # 退役累积发生概率

    # 对照：退役误作删失的 Kaplan–Meier
    km_surv = np.cumprod(1.0 - f1 / n)
    km_fail = 1.0 - km_surv

    for i, t in enumerate(outcome_times):
        ge.rows.append({
            "group": group,
            "t": int(t),
            "n_risk": int(n_t[i]),
            "d_failure": int(d1[i]),
            "d_retirement": int(d2[i]),
            "n_censored": int(c_t[i]),
            "surv": float(surv[i]),
            "cif_failure": float(cif1[i]),
            "cif_retirement": float(cif2[i]),
            "km_surv": float(km_surv[i]),
            "km_failure": float(km_fail[i]),
        })
    return ge


def estimate_groups(intervals: List[DeviceInterval]) -> Dict[str, GroupEstimate]:
    """按机型 + 全部队列（ALL）分别估计。"""
    groups: Dict[str, List[DeviceInterval]] = {}
    for iv in intervals:
        groups.setdefault(iv.machine_type, []).append(iv)
    result: Dict[str, GroupEstimate] = {}
    for g in sorted(groups):
        result[g] = _estimate_one_group(g, groups[g])
    result[GROUP_ALL] = _estimate_one_group(GROUP_ALL, list(intervals))
    return result
