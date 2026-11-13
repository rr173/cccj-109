"""源队列变化后冻结工作区：不能在旧账本下换一批人。"""

from __future__ import annotations

import json

import pytest

from dp_share.errors import SourceChangedError
from dp_share.ledger import provision_workspace, publish, read_ledger
from tests.dp_helpers import build_duckdb, default_rows, run_cli


def _rebuild(db_path, rows, run_id="r1"):
    if db_path.exists():
        db_path.unlink()
    return build_duckdb(db_path, rows, run_id=run_id)


def test_source_change_freezes_new_queries_but_keeps_replay(tmp_path):
    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    ws = tmp_path / "ws.sqlite"
    provision_workspace(db, "r1", ["A", "B", "C"], 1000, ws)
    publish(ws, "keep", ["A"], ["failure"], 200)

    # 源队列变化：D01 从 failure 变成 retirement（逻辑内容改变）
    changed = []
    for r in default_rows():
        if r["device_id"] == "D01":
            r = {**r, "status": "retirement"}
        changed.append(r)
    _rebuild(db, changed)

    with pytest.raises(SourceChangedError):
        publish(ws, "new-after-change", ["A"], ["failure"], 100)

    ledger = read_ledger(ws)
    assert ledger["frozen"] is True
    assert "fingerprint" in ledger["freeze_reason"]
    assert ledger["epsilon_spent_micro"] == 200  # 旧账本不被改写

    # 已记账请求仍可重放，拿回原答案，不收费
    replay = publish(ws, "keep", ["A"], ["failure"], 200)
    assert replay["reused"] is True
    assert replay["epsilon_micro_charged"] == 0
    assert read_ledger(ws)["n_publications"] == 1


def test_device_addition_is_also_detected(tmp_path):
    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    ws = tmp_path / "ws.sqlite"
    provision_workspace(db, "r1", ["A", "B", "C"], 1000, ws)

    rows = default_rows() + [
        {"device_id": "D99", "machine_type": "A", "status": "failure",
         "cohort_status": "included"}
    ]
    _rebuild(db, rows)
    with pytest.raises(SourceChangedError):
        publish(ws, "x", ["A"], ["failure"], 10)
    assert read_ledger(ws)["frozen"] is True


def test_identical_reextract_stays_fresh(tmp_path):
    """抽取顺序等非逻辑差异不影响指纹：逐行相同内容重建后仍可发布。"""
    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    ws = tmp_path / "ws.sqlite"
    provision_workspace(db, "r1", ["A", "B", "C"], 1000, ws)
    _rebuild(db, default_rows())  # 同样的逻辑内容重建
    resp = publish(ws, "fresh", ["B"], ["censored"], 50)
    assert resp["reused"] is False
    assert read_ledger(ws)["frozen"] is False


def test_source_unavailable_fails_closed_but_replay_survives(tmp_path):
    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    ws = tmp_path / "ws.sqlite"
    provision_workspace(db, "r1", ["A", "B", "C"], 1000, ws)
    publish(ws, "keep", ["A"], ["failure"], 100)

    db.unlink()  # 源宿主文件消失
    from dp_share.errors import SourceUnavailableError

    with pytest.raises(SourceUnavailableError):
        publish(ws, "new", ["A"], ["failure"], 10)
    # 账本未被扣款；已记账请求不依赖源库，仍可重放
    assert read_ledger(ws)["epsilon_spent_micro"] == 100
    replay = publish(ws, "keep", ["A"], ["failure"], 100)
    assert replay["reused"] is True
    assert replay["epsilon_micro_charged"] == 0


def test_run_id_switch_cannot_reuse_workspace(tmp_path):
    db = build_duckdb(tmp_path / "m.duckdb", default_rows(), run_id="r1")
    ws = tmp_path / "ws.sqlite"
    provision_workspace(db, "r1", ["A", "B", "C"], 1000, ws)

    # 库里换成另一个 run_id 的同结构数据：旧工作区绑定的 r1 消失，
    # 新请求失败关闭，且重新开通被“工作区已存在”挡住。
    _rebuild(db, default_rows(), run_id="r2")
    proc = run_cli(
        "publish", "--workspace", str(ws), "--request-id", "x",
        "--machine-types", "A", "--outcomes", "failure", "--epsilon-micro", "10",
    )
    assert proc.returncode != 0
    err = json.loads(proc.stderr)
    assert err["error_code"] in {"source_unavailable", "provision_error"}

    proc2 = run_cli(
        "provision", "--db", str(db), "--run-id", "r2",
        "--machine-types", "A,B,C", "--epsilon-total-micro", "9999",
        "--workspace", str(ws),
    )
    assert proc2.returncode != 0
    assert json.loads(proc2.stderr)["error_code"] == "provision_error"
    assert read_ledger(ws)["epsilon_total_micro"] == 1000
