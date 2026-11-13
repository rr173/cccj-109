"""SQLite 隐私预算账本与发布事务。

账本保证
========
- 每个工作区绑定**一个**共享队列（其逻辑指纹）与公共域，创建后不可通过
  再次开通 / 更换 run_id / 重传配置清零；工作区文件已存在即拒绝开通；
- 新发布的「扣款 + 噪声答案 + request_id 关联」在**同一个 SQLite 事务**里
  提交，提交成功后才对外输出答案；
- 写入一律 ``BEGIN IMMEDIATE`` + ``busy_timeout``，并发进程在同一剩余额度上
  竞争时由 SQLite 写锁串行化，不可能各自看到同一份余额同时放行；
- 规范查询（含 epsilon）相同则复用原答案、不再收费（换 request_id 也一样），
  靠唯一约束杜绝重试平均噪声；同一 request_id 改参数报冲突；
- 预算的加减与比较全部使用整数 micro 单位；
- 源队列指纹变化后工作区冻结，停止一切新查询/新别名，只允许已记账请求
  按原 request_id 拿回原答案（不触碰源库）；
- 账本里只存**带噪后处理答案**，不存真实人数、逐设备资料、原始噪声与
  随机源状态。

崩溃语义（测试可通过 ``fault_point`` 装配，不靠延时碰运气）
===========================================================
- ``pre_commit``：噪声已生成但事务提交前进程退出 → SQLite 自动回滚，
  余额未扣、无发布编号、无答案泄露，原请求重试是一次全新发布；
- ``post_commit``：提交后、输出前进程退出 → 重试按 request_id 命中
  已记账发布，拿回同一份答案且只扣一次。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence

from .errors import (
    BudgetExceededError,
    LedgerConflictError,
    ProvisionError,
    QueryValidationError,
    SourceChangedError,
    SourceUnavailableError,
)
from .mechanism import (
    NormalizedQuery,
    add_noised_vector,
    normalize_request,
    true_counts,
    validate_request_id,
)
from .noise import SecureLaplaceSource
from .source import PUBLIC_OUTCOMES, SharedQueue, compute_fingerprint, extract_queue

SCHEMA_VERSION = "1"
_BUSY_TIMEOUT_MS = 10_000
_FAULT_PRE_COMMIT = "pre_commit"
_FAULT_POST_COMMIT = "post_commit"

_SCHEMA = """
CREATE TABLE workspace_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE budget (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    epsilon_total_micro   INTEGER NOT NULL CHECK (epsilon_total_micro > 0),
    epsilon_spent_micro   INTEGER NOT NULL
                              CHECK (epsilon_spent_micro >= 0
                                     AND epsilon_spent_micro <= epsilon_total_micro)
);
CREATE TABLE publications (
    publication_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_query TEXT NOT NULL UNIQUE,
    machine_types   TEXT NOT NULL,
    outcomes        TEXT NOT NULL,
    epsilon_micro   INTEGER NOT NULL CHECK (epsilon_micro > 0),
    answer_json     TEXT NOT NULL,
    created_utc     TEXT NOT NULL
);
CREATE TABLE request_aliases (
    request_id     TEXT PRIMARY KEY,
    publication_id INTEGER NOT NULL REFERENCES publications(publication_id),
    created_utc    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000)
    con.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    # WAL 允许读账与并发发布共存；immediate 事务仍然串行化所有写者。
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _connect_readonly(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                          timeout=_BUSY_TIMEOUT_MS / 1000)
    con.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _load_meta(con: sqlite3.Connection) -> dict:
    rows = con.execute("SELECT key, value FROM workspace_meta").fetchall()
    if not rows:
        raise ProvisionError("工作区账本缺少 workspace_meta，无法使用")
    return dict(rows)


# --------------------------------------------------------------------------- #
# 开通
# --------------------------------------------------------------------------- #

def provision_workspace(
    db_path: str | Path,
    run_id: str,
    machine_types: Sequence[str],
    epsilon_total_micro: int,
    workspace_path: str | Path,
) -> dict:
    """从 DuckDB 选定队列并创建全新的隐私账本工作区。

    所有队列/公共域/预算校验失败都抛 :class:`ProvisionError`，不留半成品。
    """
    if isinstance(epsilon_total_micro, bool) or not isinstance(epsilon_total_micro, int):
        raise ProvisionError(
            f"epsilon_total_micro 必须是正整数（百万分之一 epsilon），收到: "
            f"{epsilon_total_micro!r}"
        )
    if epsilon_total_micro <= 0:
        raise ProvisionError(
            f"epsilon_total_micro 必须是正整数，收到: {epsilon_total_micro}"
        )

    # extract_queue 内部完成：真实表结构检查、入选过滤、status→公共结局映射、
    # 重复设备、未知机型、无法对应结局等全部阻断式校验。
    queue = extract_queue(db_path, run_id, machine_types)

    workspace = Path(workspace_path)
    if workspace.exists():
        # 既有工作区不能通过再次开通/重传配置清零。
        raise ProvisionError(
            f"工作区已存在，拒绝重新开通: {workspace}（既有预算与账本保持不变）"
        )
    workspace.parent.mkdir(parents=True, exist_ok=True)

    public_machines = sorted(set(machine_types))
    con = _connect(workspace)
    try:
        con.executescript(_SCHEMA)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "run_id": queue.run_id,
            "source_db": queue.source_db,
            "queue_fingerprint": queue.fingerprint,
            "public_machine_types": json.dumps(public_machines, ensure_ascii=False),
            "public_outcomes": json.dumps(list(PUBLIC_OUTCOMES), ensure_ascii=False),
            "created_utc": _now(),
            "frozen": "0",
            "freeze_reason": "",
        }
        con.execute("BEGIN IMMEDIATE")
        con.executemany(
            "INSERT INTO workspace_meta(key, value) VALUES (?, ?)",
            list(meta.items()),
        )
        con.execute(
            "INSERT INTO budget(id, epsilon_total_micro, epsilon_spent_micro) "
            "VALUES (1, ?, 0)",
            [epsilon_total_micro],
        )
        con.commit()
    except Exception:
        con.close()
        # 出错时清理半成品文件，避免残骸被误认为有效工作区。
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(workspace) + suffix)
            if p.exists():
                p.unlink()
        raise
    else:
        con.close()

    return {
        "workspace": str(workspace),
        "run_id": queue.run_id,
        "source_db": queue.source_db,
        "queue_fingerprint": queue.fingerprint,
        "public_machine_types": public_machines,
        "public_outcomes": list(PUBLIC_OUTCOMES),
        "epsilon_total_micro": epsilon_total_micro,
    }


# --------------------------------------------------------------------------- #
# 发布
# --------------------------------------------------------------------------- #

def _verify_source_fresh(meta: dict, public_machines: List[str]) -> SharedQueue:
    """重新抽取源队列并比对绑定指纹；异常一律失败关闭。"""
    try:
        queue = extract_queue(meta["source_db"], meta["run_id"], public_machines)
    except ProvisionError as exc:
        raise SourceUnavailableError(
            f"源队列当前无法读取或不再满足开通时的校验条件，新请求失败关闭: {exc}"
        ) from exc
    return queue


def _mark_frozen(con: sqlite3.Connection, reason: str) -> None:
    con.execute(
        "UPDATE workspace_meta SET value = '1' WHERE key = 'frozen'"
    )
    con.execute(
        "UPDATE workspace_meta SET value = ? WHERE key = 'freeze_reason'",
        [reason],
    )


def _publication_payload(pub_row: tuple, reused: bool, spent: int, total: int) -> dict:
    pub_id, canonical, machines_json, outs_json, eps, answer_json, _created = pub_row
    return {
        "publication_id": pub_id,
        "canonical_query": json.loads(canonical),
        "cells": json.loads(answer_json),
        "reused": reused,
        "epsilon_micro_charged": 0 if reused else eps,
        "epsilon_spent_micro": spent,
        "epsilon_remaining_micro": total - spent,
    }


def publish(
    workspace_path: str | Path,
    request_id: str,
    machine_types: Sequence[str],
    outcomes: Sequence[str],
    epsilon_micro: int,
    *,
    noise_source=None,
    fault_point: str | None = None,
) -> dict:
    """执行一次带账本的差分隐私计数发布。

    ``noise_source`` 仅测试内部注入；生产路径固定使用
    :class:`SecureLaplaceSource`（系统安全随机源，无 seed 开关）。
    ``fault_point`` 仅测试装配崩溃点使用，发布命令不提供确定性噪声开关。
    """
    if fault_point not in (None, _FAULT_PRE_COMMIT, _FAULT_POST_COMMIT):
        raise ValueError(f"未知 fault_point: {fault_point!r}")
    if noise_source is None:
        noise_source = SecureLaplaceSource()

    workspace = Path(workspace_path)
    if not workspace.exists():
        raise ProvisionError(f"工作区不存在，需先开通: {workspace}")

    con = _connect(workspace)
    try:
        meta = _load_meta(con)
        public_machines = json.loads(meta["public_machine_types"])
        request_id = validate_request_id(request_id)
        query: NormalizedQuery = normalize_request(
            machine_types, outcomes, epsilon_micro, public_machines
        )
        canonical = query.canonical_json()

        # --- 快路径：已知 request_id 的重放（含提交后崩溃重试）。
        # 不触碰源库、不扣款，直接给回同一份已记账答案；参数不同则冲突。
        con.execute("BEGIN IMMEDIATE")
        alias = con.execute(
            "SELECT publication_id FROM request_aliases WHERE request_id = ?",
            [request_id],
        ).fetchone()
        if alias is not None:
            pub = con.execute(
                "SELECT publication_id, canonical_query, machine_types, outcomes, "
                "epsilon_micro, answer_json, created_utc "
                "FROM publications WHERE publication_id = ?",
                [alias[0]],
            ).fetchone()
            if pub is None:  # 外键约束下不可能，防御性处理
                con.rollback()
                raise LedgerConflictError("request_id 关联的发布不存在")
            if pub[1] != canonical:
                con.rollback()
                raise LedgerConflictError(
                    f"request_id {request_id!r} 已关联不同的规范查询，"
                    "同一 request_id 不允许携带不同参数",
                    details={"request_id": request_id},
                )
            spent, total = con.execute(
                "SELECT epsilon_spent_micro, epsilon_total_micro FROM budget WHERE id = 1"
            ).fetchone()
            con.commit()
            return _publication_payload(pub, reused=True, spent=spent, total=total)

        # 未知 request_id：新查询必须通过源队列新鲜度检查。
        con.rollback()  # 释放写锁后再做可能较慢的源抽取
        if meta.get("frozen") == "1":
            raise SourceChangedError(
                "工作区已因源队列变化冻结，拒绝一切新查询（已记账请求仍可按 "
                f"原 request_id 重放）。冻结原因: {meta.get('freeze_reason', '')}"
            )
        queue = _verify_source_fresh(meta, public_machines)
        if queue.fingerprint != meta["queue_fingerprint"]:
            con.execute("BEGIN IMMEDIATE")
            _mark_frozen(
                con,
                "queue fingerprint mismatch: source cohort logical content changed",
            )
            con.commit()
            raise SourceChangedError(
                "源队列逻辑内容与开通时绑定的指纹不一致，工作区已冻结，"
                "不能在旧账本下换一批人"
            )

        # --- 主事务：查重 → 预算 → 噪声 → 扣款/答案/关联 一次提交。
        for _attempt in range(5):
            con.execute("BEGIN IMMEDIATE")
            # 锁内再次确认 request_id（拿到锁之前可能被别的进程抢先登记）。
            alias = con.execute(
                "SELECT publication_id FROM request_aliases WHERE request_id = ?",
                [request_id],
            ).fetchone()
            if alias is not None:
                pub = con.execute(
                    "SELECT publication_id, canonical_query, machine_types, outcomes, "
                    "epsilon_micro, answer_json, created_utc "
                    "FROM publications WHERE publication_id = ?",
                    [alias[0]],
                ).fetchone()
                if pub[1] != canonical:
                    con.rollback()
                    raise LedgerConflictError(
                        f"request_id {request_id!r} 已关联不同的规范查询",
                        details={"request_id": request_id},
                    )
                spent, total = con.execute(
                    "SELECT epsilon_spent_micro, epsilon_total_micro "
                    "FROM budget WHERE id = 1"
                ).fetchone()
                con.commit()
                return _publication_payload(pub, reused=True, spent=spent, total=total)

            existing = con.execute(
                "SELECT publication_id, canonical_query, machine_types, outcomes, "
                "epsilon_micro, answer_json, created_utc "
                "FROM publications WHERE canonical_query = ?",
                [canonical],
            ).fetchone()
            if existing is not None:
                # 规范查询相同：复用原答案、不再收费，只登记新 request_id 别名。
                con.execute(
                    "INSERT INTO request_aliases(request_id, publication_id, created_utc) "
                    "VALUES (?, ?, ?)",
                    [request_id, existing[0], _now()],
                )
                spent, total = con.execute(
                    "SELECT epsilon_spent_micro, epsilon_total_micro FROM budget WHERE id = 1"
                ).fetchone()
                con.commit()
                return _publication_payload(existing, reused=True, spent=spent, total=total)

            spent, total = con.execute(
                "SELECT epsilon_spent_micro, epsilon_total_micro FROM budget WHERE id = 1"
            ).fetchone()
            if spent + query.epsilon_micro > total:
                con.rollback()
                raise BudgetExceededError(
                    f"剩余预算 {total - spent} micro 不足以支付本次 "
                    f"{query.epsilon_micro} micro",
                    details={
                        "epsilon_total_micro": total,
                        "epsilon_spent_micro": spent,
                        "epsilon_remaining_micro": total - spent,
                        "requested_micro": query.epsilon_micro,
                    },
                )

            # 预算足够才生成噪声；未提交的答案只存在于本进程内存中。
            counts = true_counts(queue, query)
            answer_cells = add_noised_vector(counts, query, noise_source)
            answer_json = json.dumps(answer_cells, ensure_ascii=False)

            if fault_point == _FAULT_PRE_COMMIT:
                # 装配的“提交前中止”：直接退出，SQLite 丢弃未提交事务，
                # 余额不变、无发布记录、答案永不输出。
                os._exit(2)

            try:
                cur = con.execute(
                    "INSERT INTO publications(canonical_query, machine_types, outcomes, "
                    "epsilon_micro, answer_json, created_utc) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        canonical,
                        json.dumps(list(query.machine_types), ensure_ascii=False),
                        json.dumps(list(query.outcomes), ensure_ascii=False),
                        query.epsilon_micro,
                        answer_json,
                        _now(),
                    ],
                )
                publication_id = cur.lastrowid
                con.execute(
                    "INSERT INTO request_aliases(request_id, publication_id, created_utc) "
                    "VALUES (?, ?, ?)",
                    [request_id, publication_id, _now()],
                )
                con.execute(
                    "UPDATE budget SET epsilon_spent_micro = epsilon_spent_micro + ? "
                    "WHERE id = 1",
                    [query.epsilon_micro],
                )
                con.commit()
            except sqlite3.IntegrityError:
                # 并发下另一个进程抢先提交了同一规范查询：回滚后循环重试，
                # 走复用路径，绝不重复扣款。
                con.rollback()
                continue

            if fault_point == _FAULT_POST_COMMIT:
                # 装配的“提交后、输出前中止”：记录已持久化，
                # 原 request_id 重试将走快路径拿回同一答案，只扣一次。
                os._exit(3)

            spent += query.epsilon_micro
            return {
                "publication_id": publication_id,
                "canonical_query": json.loads(canonical),
                "cells": answer_cells,
                "reused": False,
                "epsilon_micro_charged": query.epsilon_micro,
                "epsilon_spent_micro": spent,
                "epsilon_remaining_micro": total - spent,
            }

        # 理论上 5 次竞争重试足够；仍失败则失败关闭，不扣款。
        raise LedgerConflictError("并发竞争后仍无法完成发布，请重试原 request_id")
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# 只读查账
# --------------------------------------------------------------------------- #

def read_ledger(workspace_path: str | Path) -> dict:
    """只读返回账本概览。不含真实人数、逐设备资料、原始噪声或随机状态。"""
    workspace = Path(workspace_path)
    if not workspace.exists():
        raise ProvisionError(f"工作区不存在: {workspace}")
    con = _connect_readonly(workspace)
    try:
        meta = _load_meta(con)
        spent, total = con.execute(
            "SELECT epsilon_spent_micro, epsilon_total_micro FROM budget WHERE id = 1"
        ).fetchone()
        pubs: List[dict] = []
        pub_rows = con.execute(
            "SELECT publication_id, canonical_query, machine_types, outcomes, "
            "epsilon_micro, answer_json, created_utc FROM publications "
            "ORDER BY publication_id"
        ).fetchall()
        for pub_id, canonical, machines_json, outs_json, eps, answer_json, created in pub_rows:
            request_ids = [
                r[0]
                for r in con.execute(
                    "SELECT request_id FROM request_aliases WHERE publication_id = ? "
                    "ORDER BY rowid",
                    [pub_id],
                ).fetchall()
            ]
            pubs.append(
                {
                    "publication_id": pub_id,
                    "canonical_query": json.loads(canonical),
                    "machine_types": json.loads(machines_json),
                    "outcomes": json.loads(outs_json),
                    "epsilon_micro": eps,
                    "request_ids": request_ids,
                    "cells": json.loads(answer_json),
                    "created_utc": created,
                }
            )
        n_publications = con.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
        n_requests = con.execute("SELECT COUNT(*) FROM request_aliases").fetchone()[0]
    finally:
        con.close()

    return {
        "workspace": str(workspace),
        "run_id": meta["run_id"],
        "source_db": meta["source_db"],
        "queue_fingerprint": meta["queue_fingerprint"],
        "public_machine_types": json.loads(meta["public_machine_types"]),
        "public_outcomes": json.loads(meta["public_outcomes"]),
        "frozen": meta.get("frozen") == "1",
        "freeze_reason": meta.get("freeze_reason", ""),
        "epsilon_total_micro": total,
        "epsilon_spent_micro": spent,
        "epsilon_remaining_micro": total - spent,
        "n_publications": n_publications,
        "n_request_ids": n_requests,
        "publications": pubs,
    }
