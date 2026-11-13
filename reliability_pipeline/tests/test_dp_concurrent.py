"""多进程并发竞争测试：同一剩余额度上同时发起新发布。

不做顺序调用模拟——每个请求都在独立进程里通过真实 CLI 竞争同一 SQLite
写锁与同一余额，验证：
1. 成功的**不同规范查询**新发布数恰好等于余额能承担的最大数量；
2. 其余请求全部 budget_exceeded，余额绝不变负、扣款总额精确；
3. 同规范查询并发时只产生 1 条发布，其余复用不收费、答案一致；
4. 同一 request_id 并发只有一个赢家。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from dp_share.ledger import provision_workspace, read_ledger
from tests.dp_helpers import build_duckdb, default_rows, run_cli


def _provision(tmp_path, total: int):
    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    ws = tmp_path / "ws.sqlite"
    provision_workspace(db, "r1", ["A", "B", "C"], total, ws)
    return ws


def test_concurrent_distinct_new_publications_never_overspend(tmp_path):
    total, eps = 1000, 300  # 最多承受 3 笔不同的新发布
    ws = _provision(tmp_path, total)

    # 8 个互不相同的规范查询（机型×结局组合），都是真新发布，必须抢预算
    distinct = [
        ("A", "failure"), ("A", "retirement"), ("A", "censored"),
        ("B", "failure"), ("B", "retirement"), ("B", "censored"),
        ("C", "failure"), ("C", "retirement"),
    ]

    def call(i):
        machine, outcome = distinct[i]
        return run_cli(
            "publish", "--workspace", str(ws), "--request-id", f"c{i}",
            "--machine-types", machine, "--outcomes", outcome,
            "--epsilon-micro", str(eps),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(8)))

    ok, rejected = [], []
    for proc in results:
        payload = json.loads(proc.stdout or proc.stderr)
        (ok if proc.returncode == 0 else rejected).append(payload)

    assert len(ok) == total // eps                      # 恰好 3 笔
    assert all(p["error_code"] == "budget_exceeded" for p in rejected)
    assert len(rejected) == 8 - len(ok)

    ledger = read_ledger(ws)
    assert ledger["epsilon_spent_micro"] == (total // eps) * eps  # 900
    assert ledger["epsilon_remaining_micro"] == 100
    assert ledger["n_publications"] == total // eps
    assert ledger["n_request_ids"] == total // eps


def test_concurrent_same_canonical_query_charges_once(tmp_path):
    ws = _provision(tmp_path, 1000)
    args = (
        "publish", "--workspace", str(ws),
        "--machine-types", "A", "--outcomes", "failure",
        "--epsilon-micro", "300",
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [
            f.result() for f in
            (pool.submit(run_cli, *args, "--request-id", f"r{i}") for i in range(8))
        ]
    responses = [json.loads(p.stdout or p.stderr) for p in results]
    assert all(p.returncode == 0 for p in results)
    # 同规范查询：1 个新发布 + 7 个复用，答案全部相同
    new = [r for r in responses if not r["reused"]]
    reused = [r for r in responses if r["reused"]]
    assert len(new) == 1 and len(reused) == 7
    answers = {json.dumps(r["cells"]) for r in responses}
    assert len(answers) == 1
    ledger = read_ledger(ws)
    assert ledger["n_publications"] == 1
    assert ledger["n_request_ids"] == 8
    assert ledger["epsilon_spent_micro"] == 300


def test_concurrent_mixed_budget_pressure(tmp_path):
    total, eps = 500, 200  # 最多 2 笔不同新发布
    ws = _provision(tmp_path, total)
    distinct = [
        ("A", "failure"), ("B", "retirement"), ("C", "censored"),
        ("A", "censored"), ("B", "failure"), ("C", "retirement"),
    ]

    def call(i):
        machine, outcome = distinct[i]
        return run_cli(
            "publish", "--workspace", str(ws), "--request-id", f"q{i}",
            "--machine-types", machine, "--outcomes", outcome,
            "--epsilon-micro", str(eps),
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(call, range(6)))

    ok = [json.loads(p.stdout) for p in results if p.returncode == 0]
    bad = [json.loads(p.stderr) for p in results if p.returncode != 0]
    assert len(ok) == 2
    assert all(b["error_code"] == "budget_exceeded" for b in bad)
    ledger = read_ledger(ws)
    assert ledger["epsilon_spent_micro"] == 400
    assert ledger["epsilon_remaining_micro"] == 100
    assert ledger["n_publications"] == 2


def test_concurrent_same_request_id_single_winner(tmp_path):
    """同一 request_id 并发：只有一个赢家记账，其余重放拿回同一答案、不扣款。"""
    ws = _provision(tmp_path, 1000)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                run_cli, "publish", "--workspace", str(ws),
                "--request-id", "dup", "--machine-types", "A",
                "--outcomes", "failure", "--epsilon-micro", "100",
            )
            for _ in range(4)
        ]
        results = [f.result() for f in futures]
    outcomes = [json.loads(p.stdout or p.stderr) for p in results]
    assert all(p.returncode == 0 for p in results)
    winners = [o for o in outcomes if not o["reused"]]
    replays = [o for o in outcomes if o["reused"]]
    assert len(winners) == 1 and len(replays) == 3
    # 所有进程拿到同一发布编号与同一份答案，只扣一次费
    assert len({o["publication_id"] for o in outcomes}) == 1
    assert len({json.dumps(o["cells"]) for o in outcomes}) == 1
    ledger = read_ledger(ws)
    assert ledger["n_request_ids"] == 1
    assert ledger["n_publications"] == 1
    assert ledger["epsilon_spent_micro"] == 100
