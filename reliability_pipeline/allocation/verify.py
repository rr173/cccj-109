"""独立核对：验证计数、方案合法性与 rank 枚举顺序（不参与生产抽签）。

这里的方法**方向不同或方法不同**于生产 DP：

- ``count_forward``：从空前缀配额正向累加，得到 ``Z``，与后缀 DP 的 ``Z``
  交叉核对；
- ``enumerate_feasible``：仅在组数很小的实例上显式枚举全部 ``2^m`` 个候选，
  筛出可行标记向量并按固定组序、A 在 B 前排序。用于「逐 rank 核对不重不漏」，
  绝不用于生产抽签（生产路径不枚举指数空间）。
- ``check_outcome`` / ``check_trace``：逐项核对最终名单与抽签轨迹。

这些检查只读计算结果，不写文件。
"""

from __future__ import annotations

import itertools
from typing import Dict, List, Optional, Sequence, Tuple

from .count import CountTables
from .model import A, B, LockedReduction, Problem
from .sampler import Outcome, encode_rank

State = Tuple[int, ...]


class EnumerationTooLarge(Exception):
    """实例太大，不适合用枚举程序做独立核对。"""


def count_forward(prob: Problem, red: LockedReduction) -> int:
    """正向 DP：``prefix[state]`` = 已处理 cluster 贡献恰好 state 的前缀数。"""
    k = len(prob.strata)
    residual = tuple(red.residual)
    zero: State = (0,) * k
    prefix: Dict[State, int] = {zero: 1}
    for ci in red.remaining_clusters:
        v = list(prob.membership_dense(ci, k))
        nxt: Dict[State, int] = {}
        for s, ways in prefix.items():
            nxt[s] = nxt.get(s, 0) + ways                    # 选 B
            s2 = tuple(x + y for x, y in zip(s, v))
            if all(x <= residual[i] for i, x in enumerate(s2)):
                nxt[s2] = nxt.get(s2, 0) + ways              # 选 A
        prefix = nxt
    return prefix.get(residual, 0)


def enumerate_feasible(prob: Problem, max_candidates: int = 1 << 20
                       ) -> List[Tuple[str, ...]]:
    """显式枚举全部可行 cluster 标记向量，按固定组序与 A<B 字典序排序。

    仅用于小实例核对；候选数 ``2^未锁定组数`` 超过 ``max_candidates`` 时
    抛 ``EnumerationTooLarge``（明确拒绝，不做悄悄截断）。
    """
    unlocked = [ci for ci in range(len(prob.clusters))
                if ci not in prob.lock_by_cluster]
    m = len(unlocked)
    if m >= 63 or (1 << m) > max_candidates:
        raise EnumerationTooLarge(
            f"未锁定 cluster {m} 个（2^{m} 个候选），超过枚举上限 "
            f"{max_candidates}；枚举程序只用于小实例核对")
    k = len(prob.strata)
    targets = list(prob.targets)
    lock_contrib = [0] * k
    for ci, arm in prob.locks:
        if arm == A:
            for si, n in prob.membership[ci]:
                lock_contrib[si] += n

    out: List[Tuple[str, ...]] = []
    for bits in itertools.product((A, B), repeat=m):
        counts = list(lock_contrib)
        for arm, ci in zip(bits, unlocked):
            if arm == A:
                for si, n in prob.membership[ci]:
                    counts[si] += n
        if counts != targets:
            continue
        arms = [None] * len(prob.clusters)
        for ci, arm in prob.lock_by_cluster.items():
            arms[ci] = arm
        for arm, ci in zip(bits, unlocked):
            arms[ci] = arm
        out.append(tuple(arms))
    out.sort(key=lambda vec: tuple(0 if a == A else 1 for a in vec))
    return out


def check_outcome(prob: Problem, red: LockedReduction, tables: CountTables,
                  outcome: Outcome) -> List[str]:
    """核对最终方案本身：覆盖全部组、锁定生效、每层配额精确、rank 自洽。"""
    problems: List[str] = []
    if set(outcome.arms) != set(prob.clusters):
        problems.append("方案未恰好覆盖所有 cluster")
        return problems
    for ci, arm in prob.locks:
        cid = prob.clusters[ci]
        if outcome.arms[cid] != arm:
            problems.append(f"锁定未被遵守：cluster {cid!r} 应为 {arm}，实为 "
                            f"{outcome.arms[cid]}")
    for i, s in enumerate(prob.strata):
        n_a = sum(1 for d in prob.devices
                  if d.stratum == s and outcome.arms[d.cluster_id] == A)
        if n_a != prob.targets[i]:
            problems.append(
                f"层 {s!r} 的 A 组台数 {n_a} ≠ target_A={prob.targets[i]}")
    # 每个 cluster 完整（同一方案内所有设备同属一个标记，由 arms 按 cluster 定）
    for cid in prob.clusters:
        arms = {outcome.arms[cid]}
        if len(arms) != 1:
            problems.append(f"cluster {cid!r} 被拆开")
    try:
        enc = encode_rank(prob, tables, outcome.arms)
    except ValueError as e:
        problems.append(f"方案无法编码为 rank：{e}")
    else:
        if enc != outcome.rank:
            problems.append(f"rank 不自洽：记录 {outcome.rank}，重编码 {enc}")
        if not (0 <= outcome.rank < outcome.Z):
            problems.append(f"记录 rank {outcome.rank} 越出 [0, {outcome.Z})")
    if outcome.Z != tables.Z:
        problems.append("结果中的 Z 与计数表不一致")
    return problems


def check_trace(prob: Problem, red: LockedReduction, tables: CountTables,
                outcome: Outcome) -> List[str]:
    """逐步核对轨迹：剩余配额、A/B 完成数、锁定项与选择。"""
    problems: List[str] = []
    trace = outcome.trace
    if len(trace) != len(prob.clusters):
        problems.append(f"轨迹步数 {len(trace)} ≠ cluster 数 {len(prob.clusters)}")
        return problems
    r = tuple(red.residual)
    local = outcome.rank if outcome.mode == "rank" else None
    for idx, (ci, step) in enumerate(zip(range(len(prob.clusters)), trace)):
        cid = prob.clusters[ci]
        if step.cluster_id != cid:
            problems.append(f"轨迹第 {idx} 步 cluster 顺序不符："
                            f"{step.cluster_id!r} ≠ {cid!r}")
        if tuple(step.remaining_before) != r:
            problems.append(f"第 {idx} 步进入剩余配额 {step.remaining_before} ≠ {r}")
        v = prob.membership_dense(ci, len(prob.strata))
        if ci in prob.lock_by_cluster:
            arm = prob.lock_by_cluster[ci]
            if not step.locked or step.chosen != arm:
                problems.append(f"第 {idx} 步锁定组 {cid!r} 轨迹未标清/方案不符")
            if tuple(step.remaining_after) != r:
                problems.append(f"第 {idx} 步锁定组改变了剩余配额")
            continue
        if step.locked:
            problems.append(f"第 {idx} 步非锁定组被标记为锁定")
        if step.chosen not in (A, B):
            problems.append(f"第 {idx} 步方案名非法：{step.chosen!r}")
            continue
        if step.chosen == A:
            if not all(r[i] >= v[i] for i in range(len(r))):
                problems.append(f"第 {idx} 步选择 A 但剩余配额不足")
                continue
            expect_after = tuple(r[i] - v[i] for i in range(len(r)))
        else:
            expect_after = r
        if tuple(step.remaining_after) != expect_after:
            problems.append(f"第 {idx} 步选择后剩余配额 {step.remaining_after} "
                            f"≠ 应为 {expect_after}")
        if step.z_A is None or step.z_B is None:
            problems.append(f"第 {idx} 步缺少 A/B 完成数")
            continue
        if step.z_A + step.z_B <= 0:
            problems.append(f"第 {idx} 步可行完成总数为 0")
        if outcome.mode == "sample":
            if step.u is None or not (0 <= step.u < step.z_A + step.z_B):
                problems.append(f"第 {idx} 步随机整数越界")
            else:
                expect = A if step.u < step.z_A else B
                if step.chosen != expect:
                    problems.append(
                        f"第 {idx} 步实际选择 {step.chosen} 与 u={step.u} "
                        f"（A 完成数 {step.z_A}）不符")
        elif outcome.mode == "rank":
            if local is None:
                problems.append("rank 模式轨迹缺少局部 rank")
            else:
                if not (0 <= local < step.z_A + step.z_B):
                    problems.append(f"第 {idx} 步局部 rank {local} 越界")
                expect = A if local < step.z_A else B
                if step.chosen != expect:
                    problems.append(
                        f"第 {idx} 步实际选择 {step.chosen} 与 rank={local} 不符")
                local = local if step.chosen == A else local - step.z_A
        r = expect_after
    if any(r):
        problems.append(f"轨迹结束后剩余配额非零 {r}")
    if outcome.mode == "rank" and local not in (None, 0):
        problems.append(f"rank 模式结束后剩余局部 rank {local} ≠ 0")
    return problems


def check_rank_enumeration(prob: Problem, red: LockedReduction,
                           tables: CountTables, max_candidates: int = 1 << 20
                           ) -> List[str]:
    """小实例：独立枚举全部可行向量，与逐 rank 解码不重不漏地比对。"""
    from .sampler import decode

    problems: List[str] = []
    try:
        feasible = enumerate_feasible(prob, max_candidates)
    except EnumerationTooLarge as e:
        return [str(e)]
    if len(feasible) != tables.Z:
        problems.append(f"枚举可行数 {len(feasible)} ≠ DP 的 Z={tables.Z}")
    if len(set(feasible)) != len(feasible):
        problems.append("枚举结果出现重复方案")
    if len(feasible) != tables.Z:
        return problems
    decoded = []
    for rank in range(tables.Z):
        o = decode(prob, tables, rank)
        vec = tuple(o.arms[cid] for cid in prob.clusters)
        if o.rank != rank:
            problems.append(f"rank={rank} 解码后自报 rank={o.rank}")
        problems += check_outcome(prob, red, tables, o)
        problems += check_trace(prob, red, tables, o)
        decoded.append(vec)
    if decoded != feasible:
        problems.append("逐 rank 解码顺序与独立枚举的字典序不一致")
    if len(set(decoded)) != len(decoded):
        problems.append("逐 rank 解码出现重复方案")
    return problems
