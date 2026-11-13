"""无偏抽签与 rank ↔ 方案的双向映射。

方案空间
--------
一个**方案**是给每个 cluster 的最终标记（A/B）向量：规范化组序
``c_0 < c_1 < ... < c_{q-1}`` 上的 ``(arm_0, ..., arm_{q-1})``。锁定 cluster 的
分量固定，其余分量自由。不同方案按这个向量区分；设备先后顺序、同一层内
「先挑谁」都不构成不同方案，因此不存在按设备排列数加权的偏差。

固定组序与字典序
----------------
统一用「A 在 B 前」的字典序：编码 ``A=0, B=1``，方案即
``(b_0, ..., b_{q-1}) ∈ {0,1}^q``；rank 是它在可行方案集合中的 0-based 序号。
锁定分量在所有方案中相同，故 rank 的枚举顺序等价于「按未锁定 cluster 的
A/B 选择、A 在前」。

抽签 = 无偏生成 rank
--------------------
``count.suffix[j][r]`` = 前缀已定、剩余配额 ``r`` 时，后缀 cluster ``j..m-1``
能填满的**方案数**。从 ``j = 0, r = residual`` 出发：

```
zA = suffix[j+1][r - v_j]     （cluster j 选 A 的可行后缀数；分量不足则 0）
zB = suffix[j+1][r]           （选 B 的可行后缀数）
```

取 ``u ← uniform_int(0, zA + zB - 1)``（任意精度整数，不经浮点）：``u < zA``
选 A，否则选 B 并 ``u ← u - zA``。每步后继方案集合大小恰为
``zA + zB = suffix[j][r]``，被划分为两个互不相交的块；对块大小成比例的整数
抽取命中任一块的概率等于该块占比。由乘法法则：每个完整方案对应唯一分支
路径，被选中概率 ``Π 1/(zA+zB) = 1/Z``。因此抽签与「在 ``[0, Z)`` 均匀抽一个
rank 再解码」严格等价。

伪随机种子
----------
``seed`` 只用于初始化 ``random.Random``（Mersenne Twister），产生上述
``uniform_int``；它不参与方案空间的定义，不改变 ``Z``、组序或 rank ↔ 方案的
对应。相同「规范化输入内容 + seed」在任意进程中给出相同抽签；同一文件仅调换
CSV 行序结果也相同（规范排序与行序无关）。只承诺可复现的均匀随机化，不承诺
密码学安全：seed 不是密钥，对攻击者可预测性有要求的场合不要使用。

rank 模式
---------
给定 ``rank ∈ [0, Z)``，按同一固定组序与 A 在 B 前的字典序走分支：
``rank < zA`` 选 A，否则选 B 并 ``rank ← rank - zA``。越界 rank 在入口拒绝。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .count import CountTables
from .model import A, B, Problem

State = Tuple[int, ...]


class RankOutOfRange(Exception):
    """rank 不在 ``[0, Z)`` 内。"""


@dataclass
class TraceStep:
    """一次分支决策的完整上下文（可审阅抽签是否靠谱）。"""
    cluster_id: str
    locked: bool
    locked_arm: Optional[str]
    z_A: Optional[int]            # 选 A 的可行后缀数；锁定组不分支，为 None
    z_B: Optional[int]            # 选 B 的可行后缀数
    u: Optional[int]              # 抽签：抽到的随机整数；rank 模式：进入时的局部 rank
    chosen: str
    rank_before: int              # 进入本步时在该块内的局部 rank
    rank_after: int               # 选择后剩余（子块内的局部 rank）
    remaining_before: State       # 进入本步前的剩余配额
    remaining_after: State
    contribution: Tuple[Tuple[str, int], ...]
    note: str = ""


@dataclass
class Outcome:
    Z: int
    seed: Optional[int]
    mode: str                     # "sample" | "rank"
    rank: int
    arms: Dict[str, str]          # cluster_id → "A"/"B"（全部 cluster）
    trace: List[TraceStep]


def _sub(a: State, v: State) -> State:
    return tuple(x - y for x, y in zip(a, v))


def _labels(prob: Problem, ci: int) -> Tuple[Tuple[str, int], ...]:
    return tuple((prob.strata[si], n) for si, n in prob.membership[ci])


def branch_counts(tables: CountTables, j: int, r: State) -> Tuple[int, int]:
    """(选 A 的可行后缀数, 选 B 的可行后缀数)。"""
    v = tables.contributions[j]
    zA = 0
    if all(r[i] >= x for i, x in enumerate(v)):
        zA = tables.suffix[j + 1].get(_sub(r, v), 0)
    zB = tables.suffix[j + 1].get(r, 0)
    return zA, zB


def _walk(prob: Problem, tables: CountTables,
          pick: Callable[[int, int, int], int]) -> Tuple[Dict[str, str], List[TraceStep], int]:
    """按规范组序走完所有 cluster。

    ``pick(j, zA, zB)`` 返回 ``u ∈ [0, zA+zB)``；``u < zA`` 走 A，否则走 B。
    返回 ``(arms, trace, local_rank_after_each_step)``。
    """
    arms: Dict[str, str] = {}
    trace: List[TraceStep] = []
    r = tables.residual
    unlocked_pos = {ci: j for j, ci in enumerate(tables.unlocked)}
    local_rank = 0
    for ci in range(len(prob.clusters)):
        cid = prob.clusters[ci]
        before = r
        if ci in prob.lock_by_cluster:
            arm = prob.lock_by_cluster[ci]
            arms[cid] = arm
            trace.append(TraceStep(
                cluster_id=cid, locked=True, locked_arm=arm,
                z_A=None, z_B=None, u=None, chosen=arm,
                rank_before=local_rank, rank_after=local_rank,
                remaining_before=before, remaining_after=before,
                contribution=_labels(prob, ci),
                note="锁定项：方案已固定，不参与抽签，不改剩余配额"))
            continue
        pos = unlocked_pos[ci]
        zA, zB = branch_counts(tables, pos, r)
        u = pick(pos, zA, zB)
        if not (0 <= u < zA + zB):
            raise RuntimeError(
                f"内部错误：第 {pos} 步挑选值 {u} 不在 [0, {zA + zB})")
        if u < zA:
            chosen, local_rank = A, u
            new_r = _sub(r, tables.contributions[pos])
        else:
            chosen, local_rank = B, u - zA
            new_r = r
        arms[cid] = chosen
        trace.append(TraceStep(
            cluster_id=cid, locked=False, locked_arm=None,
            z_A=zA, z_B=zB, u=u, chosen=chosen,
            rank_before=u, rank_after=local_rank,
            remaining_before=before, remaining_after=new_r,
            contribution=_labels(prob, ci)))
        r = new_r
    if any(r):
        raise RuntimeError(f"内部错误：走完后剩余配额非零 {r}")
    return arms, trace, local_rank


def sample(prob: Problem, tables: CountTables, seed: int) -> Outcome:
    """无偏抽一个方案；等价于均匀抽 rank 再解码。"""
    if tables.Z <= 0:
        raise ValueError("Z=0：没有可行方案，不能抽签")
    rng = random.Random(seed)

    def pick(_j: int, zA: int, zB: int) -> int:
        total = zA + zB
        if total <= 0:
            raise RuntimeError("内部错误：当前步无可选分支（Z>0 时不应发生）")
        return rng.randrange(total)      # 任意精度整数；不经过浮点

    arms, trace, _ = _walk(prob, tables, pick)
    rank = encode_rank(prob, tables, arms)
    return Outcome(Z=tables.Z, seed=seed, mode="sample", rank=rank,
                   arms=arms, trace=trace)


def encode_rank(prob: Problem, tables: CountTables,
                arms: Dict[str, str]) -> int:
    """方案在固定组序、A 在 B 前字典序下的 0-based rank。"""
    r = tables.residual
    rank = 0
    for j, ci in enumerate(tables.unlocked):
        cid = prob.clusters[ci]
        zA, _zB = branch_counts(tables, j, r)
        if arms[cid] == A:
            r = _sub(r, tables.contributions[j])
        else:
            rank += zA
    if any(r):
        raise ValueError("给定方案未满足配额（无法编码为可行 rank）")
    return rank


def decode(prob: Problem, tables: CountTables, rank: int) -> Outcome:
    """按 rank 取唯一方案（等价于抽签固定了 rank）。"""
    if not isinstance(rank, int) or isinstance(rank, bool):
        raise RankOutOfRange(f"rank 必须是整数，收到 {rank!r}")
    if rank < 0 or rank >= tables.Z:
        raise RankOutOfRange(
            f"rank={rank} 越界：合法范围是 [0, {tables.Z})（Z={tables.Z}）")

    # 逐句柄走：每一步以「当前局部 rank」作为抽取结果（< zA 走 A，否则走 B）
    arms: Dict[str, str] = {}
    trace: List[TraceStep] = []
    r = tables.residual
    unlocked_pos = {ci: j for j, ci in enumerate(tables.unlocked)}
    local = rank
    for ci in range(len(prob.clusters)):
        cid = prob.clusters[ci]
        before = r
        if ci in prob.lock_by_cluster:
            arm = prob.lock_by_cluster[ci]
            arms[cid] = arm
            trace.append(TraceStep(
                cluster_id=cid, locked=True, locked_arm=arm,
                z_A=None, z_B=None, u=None, chosen=arm,
                rank_before=local, rank_after=local,
                remaining_before=before, remaining_after=before,
                contribution=_labels(prob, ci),
                note="锁定项：方案已固定，不参与抽签，不改剩余配额"))
            continue
        pos = unlocked_pos[ci]
        zA, zB = branch_counts(tables, pos, r)
        if not (0 <= local < zA + zB):
            raise RuntimeError(
                f"内部错误：局部 rank {local} 越出块大小 {zA + zB}")
        if local < zA:
            chosen = A
            new_r = _sub(r, tables.contributions[pos])
            local_after = local
        else:
            chosen = B
            new_r = r
            local_after = local - zA
        arms[cid] = chosen
        trace.append(TraceStep(
            cluster_id=cid, locked=False, locked_arm=None,
            z_A=zA, z_B=zB, u=local, chosen=chosen,
            rank_before=local, rank_after=local_after,
            remaining_before=before, remaining_after=new_r,
            contribution=_labels(prob, ci)))
        r = new_r
        local = local_after
    if local != 0 or any(r):
        raise RuntimeError(
            f"内部错误：rank 解码未落到唯一方案（余 rank={local}, 余配额={r}）")
    return Outcome(Z=tables.Z, seed=None, mode="rank", rank=rank,
                   arms=arms, trace=trace)
