"""提交前/提交后中止点测试（真实进程退出，不靠延时）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dp_share.ledger import publish, read_ledger, provision_workspace
from tests.dp_helpers import build_duckdb, default_rows, run_cli

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPO_ROOT / "tests" / "dp_fault_driver.py"


@pytest.fixture()
def workspace(tmp_path):
    db = build_duckdb(tmp_path / "m.duckdb", default_rows())
    ws = tmp_path / "ws.sqlite"
    provision_workspace(db, "r1", ["A", "B", "C"], 1000, ws)
    return ws


def _driver(workspace, request_id, env):
    from tests.dp_helpers import _clean_env

    return subprocess.run(
        [sys.executable, str(DRIVER), str(workspace), request_id,
         "A", "failure", "250"],
        cwd=REPO_ROOT, capture_output=True, text=True,
        env=_clean_env(env), timeout=30,
    )


def test_pre_commit_abort_rolls_back_everything(workspace):
    proc = _driver(workspace, "crash-1",
                   {"DP_FAULT": "pre_commit", "DP_SCRIPTED_NOISE": "0.3"})
    assert proc.returncode == 2
    assert proc.stdout == ""  # 未记账答案绝不输出

    ledger = read_ledger(workspace)
    assert ledger["epsilon_spent_micro"] == 0
    assert ledger["n_publications"] == 0
    assert ledger["n_request_ids"] == 0

    # 原请求重试：全新发布，正常扣款一次
    resp = publish(workspace, "crash-1", ["A"], ["failure"], 250)
    assert resp["reused"] is False
    assert resp["epsilon_micro_charged"] == 250
    assert read_ledger(workspace)["epsilon_spent_micro"] == 250


def test_post_commit_abort_retry_returns_same_answer_single_charge(workspace):
    proc = _driver(workspace, "crash-2",
                   {"DP_FAULT": "post_commit", "DP_SCRIPTED_NOISE": "0.3"})
    assert proc.returncode == 3
    assert proc.stdout == ""  # 输出前退出

    ledger = read_ledger(workspace)
    assert ledger["epsilon_spent_micro"] == 250       # 扣款已落盘
    assert ledger["n_publications"] == 1
    assert ledger["n_request_ids"] == 1
    committed_answer = ledger["publications"][0]["cells"]

    # 用真实发布命令（系统随机源）重试同一 request_id：拿回同一份答案
    proc2 = run_cli(
        "publish", "--workspace", str(workspace), "--request-id", "crash-2",
        "--machine-types", "A", "--outcomes", "failure", "--epsilon-micro", "250",
    )
    assert proc2.returncode == 0
    resp = json.loads(proc2.stdout)
    assert resp["reused"] is True
    assert resp["epsilon_micro_charged"] == 0
    assert resp["cells"] == committed_answer
    assert read_ledger(workspace)["epsilon_spent_micro"] == 250  # 只扣一次
    assert read_ledger(workspace)["n_publications"] == 1


def test_restart_preserves_budget_and_answers(workspace):
    publish(workspace, "keep-1", ["A"], ["failure"], 300)
    # “重启” = 新进程只读打开同一工作区
    proc = run_cli("ledger", "--workspace", str(workspace))
    assert proc.returncode == 0
    ledger = json.loads(proc.stdout)
    assert ledger["epsilon_spent_micro"] == 300
    assert ledger["epsilon_remaining_micro"] == 700
    assert ledger["publications"][0]["request_ids"] == ["keep-1"]


def test_no_deterministic_switch_on_publish_command(workspace):
    """发布命令不接受 seed/噪声开关类参数。"""
    proc = run_cli(
        "publish", "--workspace", str(workspace), "--request-id", "x",
        "--machine-types", "A", "--outcomes", "failure",
        "--epsilon-micro", "10", "--seed", "42",
    )
    assert proc.returncode != 0
    assert "unrecognized arguments" in proc.stderr
