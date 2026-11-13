"""账本语义：规范化复用、request_id 冲突、整数预算、输出卫生。"""

from __future__ import annotations

import json

import pytest

from dp_share.errors import BudgetExceededError, LedgerConflictError, QueryValidationError
from dp_share.ledger import publish, read_ledger
from tests.dp_helpers import ScriptedNoise, build_duckdb, default_rows


@pytest.fixture()
def workspace(tmp_path):
    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    from dp_share.ledger import provision_workspace

    ws = tmp_path / "ws.sqlite"
    provision_workspace(db, "r1", ["A", "B", "C"], 1000, ws)
    return ws


def test_reuse_same_canonical_query_new_request_id_no_charge(workspace):
    r1 = publish(workspace, "req-1", ["B", "A"], ["failure", "failure"], 250,
                 noise_source=ScriptedNoise([0.1] * 6))
    assert r1["reused"] is False
    assert r1["epsilon_micro_charged"] == 250
    first_answer = r1["cells"]

    # 改序 + 去重 + 换 request_id：规范查询相同，必须复用同一份噪声答案
    r2 = publish(workspace, "req-2", ["A", "B"], ["failure"], 250)
    assert r2["reused"] is True
    assert r2["epsilon_micro_charged"] == 0
    assert r2["publication_id"] == r1["publication_id"]
    assert r2["cells"] == first_answer
    assert r2["epsilon_spent_micro"] == 250  # 总花费不变


def test_same_request_id_different_params_conflicts(workspace):
    publish(workspace, "req-x", ["A"], ["failure"], 100,
            noise_source=ScriptedNoise([0.0]))
    with pytest.raises(LedgerConflictError):
        publish(workspace, "req-x", ["A"], ["retirement"], 100)  # 改结局
    with pytest.raises(LedgerConflictError):
        publish(workspace, "req-x", ["A"], ["failure"], 200)     # 改 epsilon
    with pytest.raises(LedgerConflictError):
        publish(workspace, "req-x", ["B"], ["failure"], 100)     # 改机型
    ledger = read_ledger(workspace)
    assert ledger["n_publications"] == 1
    assert ledger["epsilon_spent_micro"] == 100  # 冲突不扣款


def test_budget_integer_arithmetic_and_exhaustion(workspace):
    publish(workspace, "r1", ["A"], ["failure"], 400,
            noise_source=ScriptedNoise([0.0]))
    publish(workspace, "r2", ["B"], ["failure"], 600,
            noise_source=ScriptedNoise([0.0]))
    ledger = read_ledger(workspace)
    assert ledger["epsilon_spent_micro"] == 1000
    assert ledger["epsilon_remaining_micro"] == 0

    # 恰好花完后再查询：拒绝且不扣款
    with pytest.raises(BudgetExceededError) as exc:
        publish(workspace, "r3", ["C"], ["failure"], 1)
    assert exc.value.details["epsilon_remaining_micro"] == 0
    assert read_ledger(workspace)["epsilon_spent_micro"] == 1000

    # 花完后对已记账请求的重放仍然成功，且不收费
    replay = publish(workspace, "r1", ["A"], ["failure"], 400)
    assert replay["reused"] is True
    assert replay["epsilon_micro_charged"] == 0
    assert read_ledger(workspace)["epsilon_spent_micro"] == 1000


def test_partial_over_budget_rejected(workspace):
    publish(workspace, "r1", ["A"], ["failure"], 800,
            noise_source=ScriptedNoise([0.0]))
    with pytest.raises(BudgetExceededError):
        publish(workspace, "r2", ["A", "B"], PUBLIC_OUTCOMES_GOOD(), 300)
    assert read_ledger(workspace)["epsilon_spent_micro"] == 800


def PUBLIC_OUTCOMES_GOOD():
    return ["failure", "retirement", "censored"]


def test_invalid_requests_never_charge(workspace):
    for kwargs in [
        dict(request_id="", machine_types=["A"], outcomes=["failure"], epsilon_micro=10),
        dict(request_id="r", machine_types=[], outcomes=["failure"], epsilon_micro=10),
        dict(request_id="r", machine_types=["A"], outcomes=[], epsilon_micro=10),
        dict(request_id="r", machine_types=["X"], outcomes=["failure"], epsilon_micro=10),
        dict(request_id="r", machine_types=["A"], outcomes=["nope"], epsilon_micro=10),
        dict(request_id="r", machine_types=["A"], outcomes=["failure"], epsilon_micro=0),
        dict(request_id="r", machine_types=["A"], outcomes=["failure"], epsilon_micro=-3),
        dict(request_id="r", machine_types=["A"], outcomes=["failure"], epsilon_micro=1.5),
    ]:
        with pytest.raises((QueryValidationError, Exception)):
            publish(workspace, **kwargs)
    assert read_ledger(workspace)["epsilon_spent_micro"] == 0
    assert read_ledger(workspace)["n_publications"] == 0


def test_outputs_never_contain_true_counts_or_raw_noise(workspace):
    resp = publish(workspace, "r1", ["A", "B"], ["failure", "retirement"], 100,
                   noise_source=ScriptedNoise([0.7] * 4))
    # 真实人数（A,failure=2；B,failure=2；A,retirement=1）与原始噪声不得出现为字段
    assert "true_count" not in resp
    assert "raw_noise" not in resp and "noise" not in resp
    assert set(resp["cells"][0].keys()) == {"machine_type", "outcome", "noised_count"}

    ledger = read_ledger(workspace)
    ledger_text = json.dumps(ledger, ensure_ascii=False)
    assert "true_count" not in ledger_text
    assert '"noise"' not in ledger_text and "raw_noise" not in ledger_text
    # 逐设备身份不得出现在账本对外视图
    for device_id in ("D01", "D02", "D05", "D13"):
        assert device_id not in ledger_text


def test_ledger_maps_request_ids_to_publications(workspace):
    publish(workspace, "alpha", ["A"], ["failure"], 100,
            noise_source=ScriptedNoise([0.0]))
    publish(workspace, "beta", ["A"], ["failure"], 100)  # 复用
    publish(workspace, "gamma", ["B"], ["censored"], 200,
            noise_source=ScriptedNoise([0.0]))
    ledger = read_ledger(workspace)
    assert ledger["n_publications"] == 2
    assert ledger["n_request_ids"] == 3
    by_pub = {p["publication_id"]: p for p in ledger["publications"]}
    assert by_pub[1]["request_ids"] == ["alpha", "beta"]
    assert by_pub[2]["request_ids"] == ["gamma"]
    assert by_pub[1]["epsilon_micro"] == 100
    assert by_pub[2]["epsilon_micro"] == 200


def test_each_device_single_cell_no_double_count(tmp_path):
    """每台设备只进一个格子：各格真实人数之和 = 队列设备数（通过加噪脚本置 0 观察）。"""
    rows = [
        {"device_id": f"D{i}", "machine_type": m, "status": s,
         "cohort_status": "included"}
        for i, (m, s) in enumerate([
            ("A", "failure"), ("A", "retirement"), ("A", "censored_loss"),
            ("B", "censored_end"), ("B", "failure"),
        ])
    ]
    db = build_duckdb(tmp_path / "m.duckdb", rows)
    from dp_share.ledger import provision_workspace

    ws = tmp_path / "ws.sqlite"
    provision_workspace(db, "r1", ["A", "B"], 1000, ws)
    resp = publish(ws, "r1", ["A", "B"],
                   ["failure", "retirement", "censored"], 100,
                   noise_source=ScriptedNoise([0.0] * 6))
    # 噪声全 0 时带噪人数就是真实人数；总和必须等于设备数 5
    assert sum(c["noised_count"] for c in resp["cells"]) == 5
