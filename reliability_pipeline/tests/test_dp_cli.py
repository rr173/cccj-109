"""真实 CLI 端到端测试：系统安全随机源、错误码、非法公共域拒绝。"""

from __future__ import annotations

import json

from dp_share.ledger import read_ledger
from tests.dp_helpers import build_duckdb, default_rows, run_cli


def _provision_cli(tmp_path, total=1_000_000):
    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    ws = tmp_path / "ws.sqlite"
    proc = run_cli(
        "provision", "--db", str(db), "--run-id", "r1",
        "--machine-types", "A,B,C", "--epsilon-total-micro", str(total),
        "--workspace", str(ws),
    )
    assert proc.returncode == 0, proc.stderr
    return db, ws


def test_full_cli_flow_with_secure_randomness(tmp_path):
    db, ws = _provision_cli(tmp_path)
    proc = run_cli(
        "publish", "--workspace", str(ws), "--request-id", "p1",
        "--machine-types", "B,A", "--outcomes", "censored,failure",
        "--epsilon-micro", "500000",
    )
    assert proc.returncode == 0, proc.stderr
    resp = json.loads(proc.stdout)
    assert resp["ok"] is True
    assert resp["reused"] is False
    assert resp["epsilon_micro_charged"] == 500000
    assert resp["epsilon_spent_micro"] == 500000
    assert resp["epsilon_remaining_micro"] == 500000
    # 2 机型 × 2 结局 = 4 个公共格子，全部回答，值为非负整数
    assert len(resp["cells"]) == 4
    for cell in resp["cells"]:
        assert isinstance(cell["noised_count"], int)
        assert cell["noised_count"] >= 0
        assert set(cell.keys()) == {"machine_type", "outcome", "noised_count"}
    # 系统随机源：两次不同规范查询得到不同答案的概率极高（仅观察，不作证明）
    proc2 = run_cli(
        "publish", "--workspace", str(ws), "--request-id", "p2",
        "--machine-types", "A,B,C", "--outcomes",
        "failure,retirement,censored", "--epsilon-micro", "100000",
    )
    assert proc2.returncode == 0
    resp2 = json.loads(proc2.stdout)
    assert len(resp2["cells"]) == 9


def test_cli_rejection_codes(tmp_path):
    db, ws = _provision_cli(tmp_path, total=100)

    cases = [
        # 空子集（逗号分隔后为空）
        (["--machine-types", "", "--outcomes", "failure",
          "--epsilon-micro", "10"], 5),
        # 未知机型（公共域之外）
        (["--machine-types", "Z", "--outcomes", "failure",
          "--epsilon-micro", "10"], 5),
        # 未知结局
        (["--machine-types", "A", "--outcomes", "exploded",
          "--epsilon-micro", "10"], 5),
        # 非正/非整数预算（argparse 拦非整数 → 退出码 2；非正 → 5）
        (["--machine-types", "A", "--outcomes", "failure",
          "--epsilon-micro", "0"], 5),
        (["--machine-types", "A", "--outcomes", "failure",
          "--epsilon-micro", "-5"], 5),
    ]
    for extra, expected_rc in cases:
        proc = run_cli("publish", "--workspace", str(ws),
                       "--request-id", "bad", *extra)
        assert proc.returncode == expected_rc, (extra, proc.stdout, proc.stderr)
        err = json.loads(proc.stderr)
        assert err["ok"] is False and err["error_code"]

    # 非整数预算由 argparse 拒绝
    proc = run_cli("publish", "--workspace", str(ws), "--request-id", "bad",
                   "--machine-types", "A", "--outcomes", "failure",
                   "--epsilon-micro", "1.5")
    assert proc.returncode == 2

    # 非法输入均未扣款
    assert read_ledger(ws)["epsilon_spent_micro"] == 0


def test_cli_budget_exhausted_code(tmp_path):
    db, ws = _provision_cli(tmp_path, total=100)
    ok = run_cli("publish", "--workspace", str(ws), "--request-id", "full",
                 "--machine-types", "A", "--outcomes", "failure",
                 "--epsilon-micro", "100")
    assert ok.returncode == 0
    over = run_cli("publish", "--workspace", str(ws), "--request-id", "over",
                   "--machine-types", "A", "--outcomes", "failure",
                   "--epsilon-micro", "1")
    assert over.returncode == 6
    assert json.loads(over.stderr)["error_code"] == "budget_exceeded"


def test_cli_illegal_public_domain_at_provision(tmp_path):
    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    ws = tmp_path / "ws.sqlite"
    # 公共机型字典不含数据中实际出现的 C → 阻断，不静默丢弃 D13
    proc = run_cli(
        "provision", "--db", str(db), "--run-id", "r1",
        "--machine-types", "A,B", "--epsilon-total-micro", "100",
        "--workspace", str(ws),
    )
    assert proc.returncode == 4
    err = json.loads(proc.stderr)
    assert err["error_code"] == "provision_error"
    assert "公共机型字典" in err["error"]
    assert not ws.exists()


def test_cli_ledger_is_readonly_and_sanitized(tmp_path):
    db, ws = _provision_cli(tmp_path)
    run_cli("publish", "--workspace", str(ws), "--request-id", "r1",
            "--machine-types", "A", "--outcomes", "failure",
            "--epsilon-micro", "100")
    proc = run_cli("ledger", "--workspace", str(ws))
    assert proc.returncode == 0
    text = proc.stdout
    ledger = json.loads(text)
    assert ledger["n_publications"] == 1
    assert ledger["publications"][0]["request_ids"] == ["r1"]
    # 对外材料不含逐设备身份与真实人数字段名
    for forbidden in ("D01", "D13", "true_count", "raw_noise"):
        assert forbidden not in text
