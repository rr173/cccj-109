"""dp_share 测试共用辅助：构造材料库、确定性噪声源、独立验证器。

独立验证器刻意**不导入** dp_share 的发布实现来生成期望值：
- 直接用 duckdb SQL 重新读 run_devices 并自行做入选过滤与结局映射；
- 自行实现 Laplace 逆变换（含端点处理）与 floor/截零后处理；
只把发布实现产出的答案拿来与独立期望值比对。
"""

from __future__ import annotations

import math
import sqlite3  # noqa: F401  (保留：部分测试直接检查 SQLite 文件)
import subprocess
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[1]

STATUS_TO_OUTCOME = {
    "failure": "failure",
    "retirement": "retirement",
    "censored_loss": "censored",
    "censored_end": "censored",
}
PUBLIC_OUTCOMES = ("failure", "retirement", "censored")


def build_duckdb(path: Path, rows: list[dict], run_id: str = "r1") -> Path:
    """按真实 run_devices 结构造一个测试 DuckDB。

    rows 每行至少含 device_id/machine_type/status/cohort_status。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(
        """
        CREATE TABLE run_devices (
            run_id VARCHAR, device_id VARCHAR, machine_type VARCHAR,
            commission_date VARCHAR, enroll_date VARCHAR,
            L INTEGER, T INTEGER, status VARCHAR,
            cohort_status VARCHAR NOT NULL, exclude_reason VARCHAR,
            terminal_event_id VARCHAR
        )
        """
    )
    con.executemany(
        "INSERT INTO run_devices VALUES (?, ?, ?, NULL, NULL, 1, 2, ?, ?, NULL, NULL)",
        [
            (run_id, r["device_id"], r["machine_type"], r["status"], r["cohort_status"])
            for r in rows
        ],
    )
    con.close()
    return path


def default_rows() -> list[dict]:
    """11 台入选设备 + 1 拒收 + 1 无观察时长，模拟真实库的状态分布。"""
    included = [
        ("D01", "A", "failure"),
        ("D02", "A", "retirement"),
        ("D03", "A", "censored_loss"),
        ("D04", "A", "censored_end"),
        ("D05", "B", "failure"),
        ("D07", "B", "failure"),
        ("D08", "B", "censored_end"),
        ("D09", "B", "censored_end"),
        ("D10", "B", "censored_end"),
        ("D12", "A", "failure"),
        ("D13", "C", "censored_end"),
    ]
    rows = [
        {"device_id": d, "machine_type": m, "status": s, "cohort_status": "included"}
        for d, m, s in included
    ]
    rows.append({"device_id": "D06", "machine_type": "B", "status": None,
                 "cohort_status": "rejected"})
    rows.append({"device_id": "D11", "machine_type": "B", "status": "failure",
                 "cohort_status": "excluded_no_time"})
    return rows


class ScriptedNoise:
    """按脚本返回 Laplace 抽样的噪声源（引擎 NoiseSource 协议的测试替身）。

    与生产源实现同一协议：入参是尺度 ``scale``，返回最终 Laplace 抽样。
    脚本值是单位尺度抽样，内部乘以 scale，因此脚本值 0.4 在 b=1 时
    即实际噪声 0.4，便于直接手核对取整/截零。
    """

    def __init__(self, values: list[float]):
        self._values = list(values)
        self._i = 0
        self.calls: list[float] = []

    def laplace(self, scale: float) -> float:
        if self._i >= len(self._values):
            raise AssertionError("脚本噪声已用完")
        value = self._values[self._i] * scale
        self._i += 1
        self.calls.append(scale)
        return value


class ScriptedUniformNoise:
    """给定均匀网格值 u∈[0,1)，自行按逆变换产生 Laplace 的测试源。

    与生产 SecureLaplaceSource 使用同一数学公式，但实现独立编写，
    用于交叉核对。
    """

    def __init__(self, u_values: list[float]):
        self._u = list(u_values)
        self._i = 0

    def laplace(self, scale: float) -> float:
        u = self._u[self._i]
        self._i += 1
        assert 0.0 <= u < 1.0
        v = abs(2.0 * u - 1.0)
        assert v < 1.0  # 端点必须由调用侧保证
        x = -math.copysign(1.0, u - 0.5) * math.log1p(-v)
        return scale * x


# --------------------------------------------------------------------------- #
# 独立验证器：不使用 dp_share 的机制/账本代码生成答案
# --------------------------------------------------------------------------- #

def independent_true_counts(db_path: Path, run_id: str,
                            machines: list[str], outcomes: list[str]) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT device_id, machine_type, status FROM run_devices "
            "WHERE run_id = ? AND cohort_status = 'included'",
            [run_id],
        ).fetchall()
    finally:
        con.close()
    cells = [(m, o) for m in sorted(machines) for o in sorted(outcomes)]
    counts = {c: 0 for c in cells}
    for device_id, mtype, status in rows:
        outcome = STATUS_TO_OUTCOME[status]
        if (mtype, outcome) in counts:
            counts[(mtype, outcome)] += 1
    return counts


def independent_laplace(u: float, scale: float) -> float:
    """验证器自己的逆变换实现（含端点拒绝语义）。"""
    if not 0.0 <= u < 1.0:
        raise ValueError("u 必须落在 [0,1)")
    v = abs(2.0 * u - 1.0)
    if v >= 1.0:
        raise ValueError("端点 v=1 必须重采样，不能取 log(0)")
    x = -math.copysign(1.0, u - 0.5) * math.log1p(-v)
    return scale * x


def independent_post_process(true_value: int, noise: float) -> int:
    return max(0, math.floor(true_value + noise + 0.5))


def _clean_env(extra: dict | None) -> dict:
    """构造干净子进程环境：保留系统 PATH，显式指定 PYTHONPATH 到仓库根。

    不使用 ``-I``（它会同时忽略 PYTHONPATH，导致无法定位本仓库包）；
    改为清掉 PYTHONHOME/用户 site 相关变量，避免宿主环境污染。
    """
    import os

    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT)}
    for key in ("PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"):
        if key in os.environ:
            env[key] = os.environ[key]
    if extra:
        env.update(extra)
    return env


def run_cli(*cli_args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """以子进程方式运行发布命令（并发/崩溃测试用）。"""
    return subprocess.run(
        [sys.executable, "-m", "dp_share.cli", *cli_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=_clean_env(env),
        timeout=60,
    )
