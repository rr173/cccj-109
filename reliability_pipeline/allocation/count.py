"""精确计数：按 cluster 向量贡献做动态规划（任意精度整数）。

状态定义
--------
把层按规范顺序编号 ``0..k-1``，未锁定 cluster 按规范顺序编号 ``0..m-1``。
设 ``targets`` 为各层 A 组配额，``locked_A`` 为锁定到 A 的 cluster 的逐层贡献，
则**剩余配额** ``residual = targets - locked_A``（锁定到 B 的 cluster 不贡献）。

``count[j][r]`` = 用未锁定 cluster ``j..m-1`` 恰好填满配额向量 ``r`` 的方案数：

```
count[m][0] = 1
count[m][r] = 0                       (r ≠ 0)
count[j][r] = count[j+1][r]                       # cluster j 选 B
            + count[j+1][r - v_j]                 # cluster j 选 A（各分量足够时）
```

其中 ``v_j`` 是 cluster j 在各层的设备台数向量。只保留 ``r ≤ residual``
（逐分量）且计数值为正的状态，因为从初始剩余配额出发的任一前缀都不会让
剩余配额超过初始值。于是 ``Z = count[0][residual]``。

**锁定的贡献先于 DP 扣除**：锁定为 A 的 cluster 不再参与 DP，其设备数直接从
对应层配额里减掉；若减出负数（锁定本身已使某层超额）或剩余需求超过未锁定
设备数，``model.reduce_locks`` 会给出逐层说明，调用方据此报告无解。

预算
----
``max_states`` 是实际创建的 DP 状态 ``(j, r)`` 的数量上限：每个阶段的新状态键
计 1，已存在的键复用不重复计。``count[m]`` 的唯一状态也计入。超出预算时抛
``StateBudgetExhausted``——这表示**精确求解未完成**，绝不等于无解，也不会
退化为近似分配。

复杂度
------
- 时间：``O(m · S · k)``，``S`` 为实际创建状态数，``k`` 为层数；加法为
  任意精度整数运算，``Z`` 的位数约与 ``m`` 和单簇规模之积同阶。
- 空间：保留全部阶段表以便按 rank 回放，``O(S)`` 个状态 + 大整数。
- 最坏状态上界 ``m · Π_i (residual_i + 1)``；层数越多、各层配额越宽，
  状态越容易爆炸。``S`` 接近 ``max_states`` 时适合提高预算或粗化分层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .model import LockedReduction, Problem

State = Tuple[int, ...]


class StateBudgetExhausted(Exception):
    """``max_states`` 预算耗尽：精确求解未完成（**不是**无解）。"""

    def __init__(self, limit: int, created: int):
        self.limit = limit
        self.created = created
        super().__init__(
            f"动态规划状态预算耗尽（max_states={limit}，已创建 {created} 个状态"
            "时仍需要更多）：**未完成精确求解**，这不表示约束无解；"
            "请调大 max_states（或减少层数/配额宽度）后重试")


@dataclass
class CountTables:
    Z: int
    n_states: int
    residual: State
    # residual_suffix[j][r] = 用未锁定 cluster j..m-1 恰好填满 r 的方案数
    suffix: List[Dict[State, int]]
    unlocked: Tuple[int, ...]
    contributions: Tuple[State, ...]      # 与 unlocked 对齐的稠密向量
    n_strata: int
    max_states: int
    nearly_exhausted: bool = False        # 状态数达到预算比例阈值
    budget_warning: str = ""              # 接近预算时的风险提示


def _dense(membership: Sequence[Tuple[int, int]], k: int) -> State:
    v = [0] * k
    for si, n in membership:
        v[si] = n
    return tuple(v)


def _sub(a: State, b: State) -> State:
    return tuple(x - y for x, y in zip(a, b))


def _add(a: State, b: State) -> State:
    return tuple(x + y for x, y in zip(a, b))


def build_tables(prob: Problem, red: LockedReduction,
                 max_states: int) -> CountTables:
    """构建后缀计数表。调用方须先确认 ``red.failures`` 为空。"""
    if not isinstance(max_states, int) or isinstance(max_states, bool) or max_states <= 0:
        raise ValueError("max_states 必须是正整数")
    k = len(prob.strata)
    residual = tuple(red.residual)
    unlocked = tuple(red.remaining_clusters)
    contributions = tuple(
        _dense(prob.membership[ci], k) for ci in unlocked)
    m = len(unlocked)
    zero: State = () if k == 0 else (0,) * k

    n_states = 0

    def insert(table: Dict[State, int], r: State, value: int) -> None:
        nonlocal n_states
        if r in table:
            table[r] += value
            return
        n_states += 1
        if n_states > max_states:
            raise StateBudgetExhausted(max_states, n_states)
        table[r] = value

    suffix: List[Dict[State, int]] = [dict() for _ in range(m + 1)]
    insert(suffix[m], zero, 1)
    for j in range(m - 1, -1, -1):
        v = contributions[j]
        cur: Dict[State, int] = {}
        for r, ways in suffix[j + 1].items():
            # 选 B：不改变剩余配额
            insert(cur, r, ways)
            # 选 A：消耗 v；超出初始剩余配额的状态不可能出现在前缀路径上
            ra = _add(r, v)
            if all(x <= residual[i] for i, x in enumerate(ra)):
                insert(cur, ra, ways)
        suffix[j] = cur

    Z = suffix[0].get(residual, 0)
    nearly = n_states * 10 >= max_states * 9
    warning = ""
    if nearly:
        warning = (
            f"状态数 {n_states} 已达预算 {max_states} 的 90%：本次精确求解已完成，"
            "但再增加层数/配额或 cluster 数很可能超出预算；"
            "如计划重复运行，请预留更大 max_states。")
    return CountTables(Z=Z, n_states=n_states, residual=residual,
                       suffix=suffix, unlocked=unlocked,
                       contributions=contributions, n_strata=k,
                       max_states=max_states, nearly_exhausted=nearly,
                       budget_warning=warning)


def per_layer_reachability(prob: Problem, red: LockedReduction,
                           cap: int = 1_000_000) -> Optional[List[Optional[set]]]:
    """逐层「只看该层时，剩余配额能否被子集和凑出」的诊断（仅用于无解说明）。

    对每层单独做子集和：可用元素是该层未锁定 cluster 的设备数。它忽略
    跨层耦合，因此「每层都可达、合起来 Z=0」正是整簇约束造成的无解。
    配额超过 ``cap`` 时跳过该层（返回 ``None``），明确表示未检查，不作结论。
    """
    k = len(prob.strata)
    out: List[Optional[set]] = []
    for i in range(k):
        need = red.residual[i]
        if need < 0 or need > cap:
            out.append(None if need > cap else set())
            continue
        avail = sum(
            n for ci in red.remaining_clusters
            for si, n in prob.membership[ci] if si == i)
        if need > avail:
            out.append(set())
            continue
        bits = 1
        mask = (1 << (need + 1)) - 1
        for ci in red.remaining_clusters:
            n = next((n for si, n in prob.membership[ci] if si == i), 0)
            if n:
                bits = (bits | (bits << n)) & mask
        out.append({s for s in range(need + 1) if (bits >> s) & 1})
    return out


def possible_subsets(prob: Problem, red: LockedReduction, layer: int,
                     cap: int = 1_000_000) -> Optional[set]:
    """单层可达子集（供诊断/测试）；``None`` 表示配额超出自检上限。"""
    if red.residual[layer] < 0 or red.residual[layer] > cap:
        return None
    need = red.residual[layer]
    bits = 1
    mask = (1 << (need + 1)) - 1
    for ci in red.remaining_clusters:
        n = next((n for si, n in prob.membership[ci] if si == layer), 0)
        if n:
            bits = (bits | (bits << n)) & mask
    return {s for s in range(need + 1) if (bits >> s) & 1}
