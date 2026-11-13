"""从既有 DuckDB 抽取共享队列（只读）。

真实表结构（已对 ``outputs/reliability.duckdb`` 核验）::

    run_devices(
        run_id VARCHAR, device_id VARCHAR, machine_type VARCHAR,
        commission_date VARCHAR, enroll_date VARCHAR,
        L INTEGER, T INTEGER, status VARCHAR,
        cohort_status VARCHAR NOT NULL, exclude_reason VARCHAR,
        terminal_event_id VARCHAR
    )

入选与结局映射（仅基于真实列值，不做任何推断填充）
----------------------------------------------------
- 只接纳 ``cohort_status = 'included'`` 的设备（即管线里 ``L < T``、真正
  贡献观察时长的入选设备；``rejected`` 与 ``excluded_no_time`` 一律不纳入）；
- 公共结局域固定为 ``failure / retirement / censored``，入选设备
  ``status`` 与公共结局的对应：

    ================ ============
    run_devices.status 公共结局
    ================ ============
    failure          failure
    retirement       retirement
    censored_loss    censored     （失访删失）
    censored_end     censored     （观察截止删失）
    ================ ============

- 任何无法对应的 status、公共机型字典之外的 machine_type、重复 device_id
  都直接阻断开通（:class:`ProvisionError`），绝不静默丢弃。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence

import duckdb

from .errors import ProvisionError

# 固定公共结局域，不允许研究方或私有数据扩展。
PUBLIC_OUTCOMES: tuple[str, ...] = ("failure", "retirement", "censored")

# 真实 run_devices.status -> 公共结局。
_STATUS_TO_OUTCOME: dict[str, str] = {
    "failure": "failure",
    "retirement": "retirement",
    "censored_loss": "censored",
    "censored_end": "censored",
}

_INCLUDED = "included"
_EXPECTED_COLUMNS = (
    "run_id", "device_id", "machine_type", "status", "cohort_status",
)


@dataclass(frozen=True)
class QueueRow:
    """一台入选设备在共享队列中的逻辑内容（文本身份，单机型×单结局）。"""

    device_id: str
    machine_type: str
    outcome: str

    def fingerprint_tuple(self) -> tuple[str, str, str]:
        return (self.device_id, self.machine_type, self.outcome)


@dataclass(frozen=True)
class SharedQueue:
    """绑定到某个 run_id 的共享队列逻辑内容及其指纹。"""

    run_id: str
    rows: List[QueueRow]
    fingerprint: str
    source_db: str

    def __len__(self) -> int:  # 仅供本地测试/开通使用，不进入对外输出
        return len(self.rows)


def _coerce_text(value, column: str, row_index: int) -> str:
    """设备身份按文本处理；NULL / 非文本一律拒绝。"""
    if value is None:
        raise ProvisionError(
            f"run_devices 第 {row_index} 行的 {column} 为 NULL，无法构成共享队列",
            details={"column": column, "row_index": row_index},
        )
    if not isinstance(value, str):
        raise ProvisionError(
            f"run_devices 第 {row_index} 行的 {column} 不是文本（实际类型 "
            f"{type(value).__name__}），设备身份必须按文本处理",
            details={"column": column, "row_index": row_index},
        )
    if value == "":
        raise ProvisionError(
            f"run_devices 第 {row_index} 行的 {column} 为空字符串",
            details={"column": column, "row_index": row_index},
        )
    return value


def compute_fingerprint(run_id: str, rows: Sequence[QueueRow]) -> str:
    """队列逻辑内容指纹。

    规范化：按 ``(device_id, machine_type, outcome)`` 排序后逐行哈希；
    抽取顺序不影响指纹。指纹只用于“源是否变化”的完整性比对，
    本身不携带逐设备名单之外的任何信息，且只保存在本地账本中。
    """
    h = hashlib.sha256()
    h.update(b"dp_share_queue_v1\n")
    h.update(run_id.encode("utf-8"))
    h.update(b"\n")
    for row in sorted(rows, key=QueueRow.fingerprint_tuple):
        h.update(b"row\t")
        h.update(row.device_id.encode("utf-8"))
        h.update(b"\t")
        h.update(row.machine_type.encode("utf-8"))
        h.update(b"\t")
        h.update(row.outcome.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def extract_queue(
    db_path: str | Path,
    run_id: str,
    machine_types: Sequence[str],
) -> SharedQueue:
    """从 DuckDB（只读打开）抽取并严格校验共享队列。

    校验失败全部抛 :class:`ProvisionError`，不会返回部分队列。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise ProvisionError(f"DuckDB 文件不存在: {db_path}")

    machine_list = _validate_machine_dictionary(machine_types)
    machine_set = set(machine_list)

    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except duckdb.Error as exc:  # 打不开（如被独占写锁）不做猜测
        raise ProvisionError(f"无法只读打开 DuckDB {db_path}: {exc}") from exc

    try:
        cols = {
            r[0]
            for r in con.execute("DESCRIBE run_devices").fetchall()
        }
        missing = [c for c in _EXPECTED_COLUMNS if c not in cols]
        if missing:
            raise ProvisionError(
                "run_devices 表结构与预期不符，缺少列: " + ", ".join(missing),
                details={"missing_columns": missing},
            )

        run_exists = con.execute(
            "SELECT 1 FROM run_devices WHERE run_id = ? LIMIT 1", [run_id]
        ).fetchone()
        if run_exists is None:
            raise ProvisionError(
                f"run_id {run_id!r} 在 run_devices 中不存在",
                details={"run_id": run_id},
            )

        raw = con.execute(
            """
            SELECT device_id, machine_type, status, cohort_status
            FROM run_devices
            WHERE run_id = ?
            ORDER BY device_id
            """,
            [run_id],
        ).fetchall()
    except duckdb.Error as exc:
        raise ProvisionError(f"读取 run_devices 失败: {exc}") from exc
    finally:
        con.close()

    rows: List[QueueRow] = []
    seen_ids: set[str] = set()
    for idx, (device_id, machine_type, status, cohort_status) in enumerate(raw):
        # 只接纳已入选设备；rejected / excluded_no_time 不是本次共享队列成员。
        if cohort_status != _INCLUDED:
            continue

        dev = _coerce_text(device_id, "device_id", idx)
        if dev in seen_ids:
            # run_devices 正常情况下 (run_id, device_id) 唯一；出现重复说明
            # 上游数据异常，必须阻断，不能让一台设备进入两个格子。
            raise ProvisionError(
                f"入选设备 {dev!r} 在 run_id={run_id!r} 中重复出现，拒绝开通",
                details={"device_id": dev},
            )

        mtype = _coerce_text(machine_type, "machine_type", idx)
        if mtype not in machine_set:
            raise ProvisionError(
                f"入选设备 {dev!r} 的机型 {mtype!r} 不在公共机型字典内，拒绝开通",
                details={"device_id": dev, "machine_type": mtype},
            )

        status_text = _coerce_text(status, "status", idx)
        outcome = _STATUS_TO_OUTCOME.get(status_text)
        if outcome is None:
            raise ProvisionError(
                f"入选设备 {dev!r} 的 status={status_text!r} 无法对应到公共结局域 "
                f"{list(PUBLIC_OUTCOMES)}，拒绝开通",
                details={"device_id": dev, "status": status_text},
            )

        seen_ids.add(dev)
        rows.append(QueueRow(device_id=dev, machine_type=mtype, outcome=outcome))

    if not rows:
        raise ProvisionError(
            f"run_id={run_id!r} 没有任何 cohort_status='included' 的设备，"
            "空队列不允许开通"
        )

    return SharedQueue(
        run_id=run_id,
        rows=rows,
        fingerprint=compute_fingerprint(run_id, rows),
        source_db=str(db_path.resolve()),
    )


def _validate_machine_dictionary(machine_types: Sequence[str]) -> List[str]:
    """公共机型字典校验：非空、全部为非空文本、无重复。

    字典由开通方显式提供，**不能**从私有数据里自动挑选出现过的机型；
    公共格子即使真实人数为零也照样回答，因此字典内容不依赖数据分布。
    """
    if not isinstance(machine_types, (list, tuple)) or len(machine_types) == 0:
        raise ProvisionError("公共机型字典必须是非空列表")
    cleaned: List[str] = []
    seen: set[str] = set()
    for item in machine_types:
        if not isinstance(item, str) or item == "":
            raise ProvisionError(f"公共机型字典含非法条目: {item!r}")
        if item in seen:
            raise ProvisionError(f"公共机型字典含重复机型: {item!r}")
        seen.add(item)
        cleaned.append(item)
    return sorted(cleaned)
