"""机制层测试：手工队列、可控噪声、取整截零、零人数格子、L1 敏感度。

期望值由 tests/dp_helpers 的独立验证器另行计算，不调用发布实现生成答案。
"""

from __future__ import annotations

import pytest

from dp_share.mechanism import (
    add_noised_vector,
    normalize_request,
    true_counts,
)
from dp_share.source import QueueRow, SharedQueue, compute_fingerprint
from tests.dp_helpers import (
    PUBLIC_OUTCOMES,
    ScriptedNoise,
    independent_post_process,
    independent_true_counts,
)

MACHINES = ["A", "B"]


def make_queue(rows: list[tuple[str, str, str]], run_id: str = "r1") -> SharedQueue:
    qrows = [QueueRow(d, m, o) for d, m, o in rows]
    return SharedQueue(run_id=run_id, rows=qrows,
                       fingerprint=compute_fingerprint(run_id, qrows),
                       source_db="/tmp/material.duckdb")


def test_normalization_dedup_order_and_domain_errors():
    q = normalize_request(["B", "A", "B", "A"], ["censored", "failure"], 5, MACHINES)
    assert q.machine_types == ("A", "B")
    assert q.outcomes == ("censored", "failure")  # 规范化后排序
    assert q.canonical_json() == normalize_request(
        ["A", "B"], ["censored", "failure"], 5, MACHINES
    ).canonical_json()

    with pytest.raises(Exception):
        normalize_request([], ["failure"], 5, MACHINES)           # 空子集
    with pytest.raises(Exception):
        normalize_request(["A"], [], 5, MACHINES)                # 空结局
    with pytest.raises(Exception):
        normalize_request(["X"], ["failure"], 5, MACHINES)       # 未知机型
    with pytest.raises(Exception):
        normalize_request(["A"], ["dead"], 5, MACHINES)          # 未知结局
    for bad_eps in (0, -1, 1.5, "3", True, None):
        with pytest.raises(Exception):
            normalize_request(["A"], ["failure"], bad_eps, MACHINES)


def test_zero_count_cells_are_still_answered():
    # 队列里没有任何 B 机型设备，但 B 是公共字典成员，B×* 格子必须以 0 回答。
    queue = make_queue([("D1", "A", "failure"), ("D2", "A", "failure")])
    q = normalize_request(["A", "B"], PUBLIC_OUTCOMES, 10, MACHINES)
    counts = true_counts(queue, q)
    assert counts[("B", "failure")] == 0
    assert counts[("B", "retirement")] == 0
    assert counts[("B", "censored")] == 0
    assert len(counts) == 6  # 完整笛卡尔积，一个不少


def test_post_processing_rounding_clamp_with_scripted_noise():
    queue = make_queue([
        ("D1", "A", "failure"), ("D2", "A", "failure"), ("D3", "A", "failure"),
        ("D4", "A", "retirement"),
    ])
    # epsilon=1（micro=1,000,000）→ Laplace 尺度 b=1/epsilon=1
    q = normalize_request(["A"], PUBLIC_OUTCOMES, 1_000_000, MACHINES)
    counts = true_counts(queue, q)
    # 规范化后格子顺序按结局字母序：(A,censored),(A,failure),(A,retirement)
    # 真实人数依次为 0、3、1；脚本依次给大负噪声（截零）、正噪声、负噪声
    noise = ScriptedNoise([-99.0, 0.4, -0.6])
    out = add_noised_vector(counts, q, noise)
    assert noise.calls == [1.0, 1.0, 1.0]  # 尺度只由 epsilon 决定

    expected = [
        independent_post_process(0, -99.0),  # 截零 -> 0
        independent_post_process(3, 0.4),    # floor(3.9)=3
        independent_post_process(1, -0.6),   # floor(0.9)=0
    ]
    assert [c["noised_count"] for c in out] == expected
    assert expected == [0, 3, 0]
    assert all(v >= 0 for v in expected)


def test_round_half_and_negative_boundaries():
    queue = make_queue([("D1", "A", "failure")])
    q = normalize_request(["A"], ["failure"], 1_000_000, MACHINES)  # 尺度 1
    counts = true_counts(queue, q)
    # floor(x+0.5) 的边界（b=1，noisy = 1 + 脚本值）：
    # +0.5 → floor(2.0)=2；-0.5 → floor(1.0)=1；-0.5001 → floor(0.9999)=0
    for scripted, expect in [(0.5, 2), (-0.5, 1), (-0.5001, 0), (1.2, 2)]:
        out = add_noised_vector(counts, q, ScriptedNoise([scripted]))
        assert out[0]["noised_count"] == independent_post_process(1, scripted) == expect


def test_single_epsilon_charge_per_vector_not_per_cell():
    """敏感度论证：一台设备只进一个格子，相邻队列 L1 距离恒为 1。"""
    base = [
        ("D1", "A", "failure"), ("D2", "A", "retirement"),
        ("D3", "B", "censored"), ("D4", "B", "failure"),
    ]
    q = normalize_request(MACHINES, PUBLIC_OUTCOMES, 7, MACHINES)
    counts_before = true_counts(make_queue(base), q)

    # 相邻队列 1：删除一台设备
    counts_del = true_counts(make_queue([r for r in base if r[0] != "D3"]), q)
    l1_del = sum(abs(counts_before[c] - counts_del[c]) for c in q.cells())
    assert l1_del == 1

    # 相邻队列 2：增加一台设备（落在原本零人数的 A×censored 格子）
    counts_add = true_counts(make_queue(base + [("D5", "A", "censored")]), q)
    l1_add = sum(abs(counts_add[c] - counts_before[c]) for c in q.cells())
    assert l1_add == 1

    # 尺度只由整次查询的 epsilon 决定，与格子数无关
    noise = ScriptedNoise([0.0] * len(q.cells()))
    add_noised_vector(counts_before, q, noise)
    assert len(set(noise.calls)) == 1  # 所有格子同一尺度 1/epsilon
    assert noise.calls[0] == pytest.approx(1.0 / (7 / 1_000_000))


def test_independent_verifier_counts_match(tmp_path):
    """验证器直接读 DuckDB 材料独立算真实人数，与机制计数一致。"""
    from tests.dp_helpers import build_duckdb, default_rows

    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    queue = make_queue([
        ("D01", "A", "failure"), ("D02", "A", "retirement"),
        ("D03", "A", "censored"), ("D04", "A", "censored"),
        ("D05", "B", "failure"), ("D07", "B", "failure"),
        ("D08", "B", "censored"), ("D09", "B", "censored"),
        ("D10", "B", "censored"), ("D12", "A", "failure"),
        ("D13", "C", "censored"),
    ])
    q = normalize_request(["A", "B", "C"], PUBLIC_OUTCOMES, 10, ["A", "B", "C"])
    got = true_counts(queue, q)
    want = independent_true_counts(db, "r1", ["A", "B", "C"], list(PUBLIC_OUTCOMES))
    assert got == want
    assert got[("A", "failure")] == 2
    assert got[("A", "censored")] == 2
    assert got[("B", "failure")] == 2
