"""查询规范化、真实格子计数、Laplace 机制与后处理。

机制（每次发布）
================
1. 研究方给出机型子集、结局子集与 ``epsilon_micro``（正整数，百万分之一 epsilon）；
2. 规范化为去重排序后的公共域子集，构造完整格子向量
   ``cell = (machine_type, outcome)`` 的笛卡尔积——**公共格子即使真实人数为 0
   也照样回答**；
3. 对完整向量先计算各格子真实人数；
4. 每个格子独立加 ``Laplace(0, 1/epsilon)``，``epsilon = epsilon_micro/1e6``；
5. 后处理：``reported = max(0, floor(noisy + 0.5))``。

敏感度与计费
============
相邻队列定义为“增加或删除一台设备”。每台设备只进入一个
机型×结局格子，故相邻队列在计数向量上的 L1 距离恰为 1，
``Laplace(b=1/epsilon)`` 向量发布满足 (epsilon)-差分隐私，**整次向量查询
只收一次 epsilon**，不按格子数重复收费，也不把不同机型拆成互不相关的
预算池——全部发布共用同一个总预算账本，按基本顺序组合累加。

后处理 ``floor/.5/截零`` 不消耗额外预算（DP 的后处理性质），
也不保证各格子报告值之和等于队列人数。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .errors import QueryValidationError
from .noise import NoiseSource
from .source import PUBLIC_OUTCOMES, QueueRow, SharedQueue

Cell = Tuple[str, str]
MICRO = 1_000_000


@dataclass(frozen=True)
class NormalizedQuery:
    """规范化后的查询（顺序与重复元素不影响含义）。"""

    machine_types: Tuple[str, ...]
    outcomes: Tuple[str, ...]
    epsilon_micro: int

    def canonical_json(self) -> str:
        """确定性 JSON 字符串，用作账本里的规范查询键。"""
        return json.dumps(
            {
                "machine_types": list(self.machine_types),
                "outcomes": list(self.outcomes),
                "epsilon_micro": self.epsilon_micro,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def cells(self) -> List[Cell]:
        """完整格子向量（机型外序 × 结局外序的笛卡尔积，顺序确定）。"""
        return [(m, o) for m in self.machine_types for o in self.outcomes]


def _parse_positive_int(value, field: str) -> int:
    """epsilon 必须是正整数；拒绝 bool、浮点、数字字符串以外的类型与非正值。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise QueryValidationError(
            f"{field} 必须是整数（micro 单位），收到: {value!r}",
            details={"field": field, "value": value},
        )
    if value <= 0:
        raise QueryValidationError(
            f"{field} 必须是正整数，收到: {value}",
            details={"field": field, "value": value},
        )
    return value


def _normalize_subset(values, field: str, public_domain: Sequence[str]) -> Tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or len(values) == 0:
        raise QueryValidationError(
            f"{field} 子集必须是非空列表", details={"field": field}
        )
    domain = set(public_domain)
    result_set: set[str] = set()
    for item in values:
        if not isinstance(item, str) or item == "":
            raise QueryValidationError(
                f"{field} 子集含非法条目: {item!r}",
                details={"field": field, "item": item},
            )
        if item not in domain:
            raise QueryValidationError(
                f"{field} 子集含公共域之外的未知项: {item!r}",
                details={"field": field, "item": item,
                         "public_domain": list(public_domain)},
            )
        result_set.add(item)
    return tuple(sorted(result_set))


def normalize_request(
    machine_types: Sequence[str],
    outcomes: Sequence[str],
    epsilon_micro: int,
    public_machine_types: Sequence[str],
) -> NormalizedQuery:
    """校验并规范化研究方请求。任何非法输入都抛 QueryValidationError。"""
    machines = _normalize_subset(machine_types, "machine_types", public_machine_types)
    outs = _normalize_subset(outcomes, "outcomes", PUBLIC_OUTCOMES)
    eps = _parse_positive_int(epsilon_micro, "epsilon_micro")
    return NormalizedQuery(
        machine_types=machines, outcomes=outs, epsilon_micro=eps
    )


def validate_request_id(request_id: object) -> str:
    if not isinstance(request_id, str) or request_id == "":
        raise QueryValidationError("request_id 必须是非空字符串")
    return request_id


def true_counts(queue: SharedQueue, query: NormalizedQuery) -> Dict[Cell, int]:
    """对选中的完整格子向量计算真实人数（含零人数格子）。

    每台设备只在它自己的一个 (machine_type, outcome) 格子里贡献 1。
    此函数的结果仅用于加噪，绝不写入账本、绝不对外打印。
    """
    wanted = set(query.cells())
    counts: Dict[Cell, int] = {cell: 0 for cell in query.cells()}
    for row in queue.rows:  # type: QueueRow
        cell = (row.machine_type, row.outcome)
        if cell in wanted:
            counts[cell] += 1
    return counts


def _post_process(noisy: float) -> int:
    """floor(noisy + 0.5) 后截断到非负。"""
    value = math.floor(noisy + 0.5)
    if value < 0:
        return 0
    return value


def add_noised_vector(
    true_counts_by_cell: Dict[Cell, int],
    query: NormalizedQuery,
    noise_source: NoiseSource,
) -> List[dict]:
    """对完整格子向量逐格独立加噪并后处理。

    返回的每个字典只含公共标识与**带噪且取整截零后**的人数，
    不含真实人数与原始噪声。
    """
    epsilon = query.epsilon_micro / MICRO
    scale = 1.0 / epsilon  # L1 敏感度 = 1
    results: List[dict] = []
    for cell in query.cells():
        true_value = true_counts_by_cell[cell]
        raw_noise = noise_source.laplace(scale)
        noisy = true_value + raw_noise
        results.append(
            {
                "machine_type": cell[0],
                "outcome": cell[1],
                "noised_count": _post_process(noisy),
            }
        )
    return results
