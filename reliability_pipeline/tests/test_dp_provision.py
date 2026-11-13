"""开通阶段的阻断式校验测试：不静默丢任何异常数据。"""

from __future__ import annotations

import pytest

from dp_share.errors import ProvisionError
from dp_share.ledger import provision_workspace, read_ledger
from tests.dp_helpers import build_duckdb, default_rows


def provision(tmp_path, rows, machines=("A", "B", "C"), total=1_000_000,
              run_id="r1", ws_name="ws.sqlite"):
    db = build_duckdb(tmp_path / "m.duckdb", rows, run_id=run_id)
    return db, provision_workspace(
        db, run_id, list(machines), total, tmp_path / ws_name
    )


def test_provision_success_and_mapping(tmp_path):
    db, info = provision(tmp_path, default_rows())
    assert info["epsilon_total_micro"] == 1_000_000
    assert info["public_outcomes"] == ["failure", "retirement", "censored"]
    ledger = read_ledger(tmp_path / "ws.sqlite")
    assert ledger["epsilon_spent_micro"] == 0
    assert ledger["epsilon_remaining_micro"] == 1_000_000
    assert ledger["frozen"] is False


def test_included_only_rejected_and_excluded_ignored(tmp_path):
    # D06 rejected、D11 excluded_no_time 不进入队列，不报错也不被“纳入”
    db, info = provision(tmp_path, default_rows())
    from dp_share.source import extract_queue

    q = extract_queue(db, "r1", ["A", "B", "C"])
    assert {r.device_id for r in q.rows} == {
        "D01", "D02", "D03", "D04", "D05", "D07", "D08",
        "D09", "D10", "D12", "D13",
    }
    # censored_loss 与 censored_end 都映射到公共结局 censored
    outcomes = {r.device_id: r.outcome for r in q.rows}
    assert outcomes["D03"] == "censored"
    assert outcomes["D04"] == "censored"
    assert outcomes["D01"] == "failure"
    assert outcomes["D02"] == "retirement"


def test_duplicate_device_blocks(tmp_path):
    rows = default_rows() + [
        {"device_id": "D01", "machine_type": "A", "status": "failure",
         "cohort_status": "included"}
    ]
    with pytest.raises(ProvisionError, match="重复"):
        provision(tmp_path, rows)


def test_machine_outside_public_dictionary_blocks(tmp_path):
    rows = [
        {"device_id": "D1", "machine_type": "A", "status": "failure",
         "cohort_status": "included"},
        {"device_id": "D2", "machine_type": "Z", "status": "failure",
         "cohort_status": "included"},
    ]
    with pytest.raises(ProvisionError, match="公共机型字典"):
        provision(tmp_path, rows, machines=("A", "B"))


def test_unmappable_status_blocks(tmp_path):
    rows = [
        {"device_id": "D1", "machine_type": "A", "status": "weird_status",
         "cohort_status": "included"},
    ]
    with pytest.raises(ProvisionError, match="公共结局域"):
        provision(tmp_path, rows)


def test_unknown_run_id_blocks(tmp_path):
    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    with pytest.raises(ProvisionError, match="不存在"):
        provision_workspace(db, "ghost", ["A", "B", "C"], 10,
                            tmp_path / "ws.sqlite")


def test_empty_included_set_blocks(tmp_path):
    rows = [{"device_id": "D6", "machine_type": "B", "status": None,
             "cohort_status": "rejected"}]
    with pytest.raises(ProvisionError, match="没有任何"):
        provision(tmp_path, rows)


def test_bad_total_budget_blocks(tmp_path):
    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    for bad in (0, -5, 1.5, "100", True):
        with pytest.raises(ProvisionError):
            provision_workspace(db, "r1", ["A", "B", "C"], bad,
                                tmp_path / f"ws_{bad}.sqlite")


def test_bad_machine_dictionary_blocks(tmp_path):
    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    for bad in ([], ["A", "A"], [""], [1]):
        with pytest.raises(ProvisionError):
            provision_workspace(db, "r1", bad, 100,
                                tmp_path / f"ws_{id(bad)}.sqlite")


def test_reprovision_is_refused_and_budget_kept(tmp_path):
    db, _ = provision(tmp_path, default_rows(), total=1000)
    ws = tmp_path / "ws.sqlite"
    # 先花掉一笔
    from dp_share.ledger import publish

    publish(ws, "req-1", ["A"], ["failure"], 300)
    # 再次开通（同路径 / 换 run_id / 重传配置）一律拒绝
    with pytest.raises(ProvisionError, match="工作区已存在"):
        provision_workspace(db, "r1", ["A", "B", "C"], 999_999, ws)
    ledger = read_ledger(ws)
    assert ledger["epsilon_total_micro"] == 1000
    assert ledger["epsilon_spent_micro"] == 300  # 原预算与账本不动


def test_missing_db_blocks(tmp_path):
    with pytest.raises(ProvisionError, match="不存在"):
        provision_workspace(tmp_path / "nope.duckdb", "r1", ["A"], 100,
                            tmp_path / "ws.sqlite")


def test_failed_provision_leaves_no_workspace(tmp_path):
    rows = [{"device_id": "D1", "machine_type": "Z", "status": "failure",
             "cohort_status": "included"}]
    db = build_duckdb(tmp_path / "m.duckdb", rows)
    ws = tmp_path / "ws.sqlite"
    with pytest.raises(ProvisionError):
        provision_workspace(db, "r1", ["A"], 100, ws)
    assert not ws.exists()
