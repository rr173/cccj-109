"""输入读取与拒绝：设备名单 CSV + 设计文件 JSON。

两个输入都**整份校验**后才进入算法：任何一处非法都抛出 ``AllocationInputError``，
其 ``problems`` 逐条给出输入位置（CSV 的 1-based 物理行号、JSON 的字段路径）
与原因。校验阶段不写任何文件。

CSV
---
表头须按名包含 ``device_id, stratum, cluster_id``（列序可变，多余列忽略）。
三个字段都是非空文本；同一 ``device_id`` 只能出现一次。标识按**原文本**比较，
不把 ``001`` 当成 ``1``，也不去除空白后的值替换原值。

JSON 设计文件
-------------
- ``target_A``：对象，键为层名，值为该层进入 A 的设备台数（整数，按文本键）；
- ``locks``：可选数组，每项 ``{"cluster_id": ..., "arm": "A"|"B", "reason": ...}``；
  ``reason`` 可选（可为任意 JSON 值，仅用于审计展示）。``arm`` 名称必须恰为
  ``"A"`` 或 ``"B"``。同一 cluster 不能重复锁定。
- 未知层（未出现在设备名单里的层）→ 拒绝，并指出在设备名单中未见；
- 实际出现的层都必须有配额；配额须为整数且 ``0 <= target_A <= 该层设备数``；
- 锁定项引用不存在的 cluster → 拒绝；
- 同一 cluster 被同时锁定到 A 与 B → 拒绝（冲突无解，不猜）。

设计文件读入后即转成规范形式：``targets`` 只含实际出现的层（按原文本键）；
未知层与其余键都作为错误报告。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

DEVICE_COLUMNS = ("device_id", "stratum", "cluster_id")
DESIGN_TOP_KEYS = ("target_A", "locks")


class AllocationInputError(Exception):
    """输入未被采用；``problems`` 每条都带输入位置。"""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class DeviceRow:
    device_id: str
    stratum: str
    cluster_id: str
    line: int          # CSV 1-based 物理行号（表头为第 1 行）


@dataclass
class LockSpec:
    cluster_id: str
    arm: str
    reason: object = None
    position: str = ""     # 例如 "locks[0]"


@dataclass
class Design:
    targets: Dict[str, int] = field(default_factory=dict)
    locks: List[LockSpec] = field(default_factory=list)


def _text(value) -> Optional[str]:
    """非空文本；否则返回 None。JSON 数字/布尔不是文本。

    仅用于 JSON 设计文件（``cluster_id``/``arm`` 等标识）。CSV 取值按原文本
    保留，见 ``read_device_csv``。
    """
    if isinstance(value, str) and value != "":
        return value
    return None


def read_device_csv(path: str | Path) -> Tuple[List[DeviceRow], List[str]]:
    """读取设备名单。返回 ``(rows, warnings)``；非法时抛 ``AllocationInputError``。

    空文件（0 字节）或只有空行视为**空名单**（合法）。有表头但无数据行也合法。
    """
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            raw_rows = list(reader)
    except FileNotFoundError:
        raise AllocationInputError([f"设备名单文件不存在: {path}"])
    except UnicodeDecodeError as e:
        raise AllocationInputError([f"设备名单不是 UTF-8 文本: {path}: {e}"])
    except csv.Error as e:
        raise AllocationInputError([f"设备名单 CSV 解析失败: {path}: {e}"])

    problems: List[str] = []
    warnings: List[str] = []

    # 定位表头：跳过开头完全空白的行；其余首个非空行必须是表头。
    header_index = None
    for i, row in enumerate(raw_rows):
        if any(cell.strip() for cell in row):
            header_index = i
            break
    if header_index is None:
        return [], warnings          # 空名单

    header = raw_rows[header_index]
    header_line = header_index + 1
    names = [cell.strip() for cell in header]
    for col in DEVICE_COLUMNS:
        if names.count(col) == 0:
            problems.append(f"设备名单第 {header_line} 行表头缺列 {col!r}"
                            f"（需包含 {', '.join(DEVICE_COLUMNS)}，列序可变，按名取列）")
        elif names.count(col) > 1:
            problems.append(f"设备名单第 {header_line} 行表头重复列 {col!r}")
    if problems:
        raise AllocationInputError(problems)
    col_idx = {c: names.index(c) for c in DEVICE_COLUMNS}
    extra = [n for n in names if n and n not in DEVICE_COLUMNS]
    if extra:
        warnings.append(f"设备名单表头含未使用列 {extra!r}（按名取列，已忽略）")

    rows: List[DeviceRow] = []
    seen: Dict[str, int] = {}
    for offset, raw in enumerate(raw_rows[header_index + 1:]):
        line_no = header_index + 2 + offset        # 1-based
        if not any(cell.strip() for cell in raw):
            continue                                # 跳过空行；不改变行号对应关系
        if len(raw) < len(names):
            problems.append(f"设备名单第 {line_no} 行字段数 {len(raw)} 少于表头 {len(names)}")
            continue
        values = {}
        bad = False
        for col, idx in col_idx.items():
            v = raw[idx]
            # 空串或纯空白视为空；否则保留原文本（不做去空白/数字归一）
            if v == "" or v.strip() == "":
                problems.append(f"设备名单第 {line_no} 行 {col} 为空或纯空白: {v!r}")
                bad = True
            else:
                values[col] = v
        if bad:
            continue
        dev = values["device_id"]
        if dev in seen:
            problems.append(f"设备名单第 {line_no} 行 device_id={dev!r} 重复"
                            f"（首次见第 {seen[dev]} 行；按原文本比较，不做数字归一）")
            continue
        seen[dev] = line_no
        rows.append(DeviceRow(dev, values["stratum"], values["cluster_id"], line_no))
    if problems:
        raise AllocationInputError(problems)
    return rows, warnings


def read_design_json(path: str | Path) -> Design:
    """读取设计文件。非法时抛 ``AllocationInputError``（带 JSON 字段路径）。"""
    import json

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AllocationInputError([f"设计文件不存在: {path}"])
    except UnicodeDecodeError as e:
        raise AllocationInputError([f"设计文件不是 UTF-8 文本: {path}: {e}"])
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise AllocationInputError(
            [f"设计文件 JSON 解析失败: {path}:{e.lineno}:{e.colno}: {e.msg}"])

    problems: List[str] = []
    if not isinstance(doc, dict):
        raise AllocationInputError(["设计文件顶层必须是 JSON 对象（含 target_A，可选 locks）"])

    for key in doc:
        if key not in DESIGN_TOP_KEYS:
            problems.append(f"设计文件出现未知字段 {key!r}（只允许 {', '.join(DESIGN_TOP_KEYS)}）")

    targets: Dict[str, int] = {}
    if "target_A" not in doc:
        problems.append("设计文件缺少 target_A（每个实际出现的层都需要 A 组台数配额）")
    else:
        ta = doc["target_A"]
        if not isinstance(ta, dict):
            problems.append("target_A 必须是对象：层名 → A 组台数")
        else:
            for stratum, val in ta.items():
                if not isinstance(stratum, str) or stratum == "":
                    problems.append(f"target_A 的层名必须是非空文本: {stratum!r}")
                    continue
                if isinstance(val, bool) or not isinstance(val, int):
                    problems.append(f"target_A.{stratum} 必须是整数: {val!r}")
                    continue
                if val < 0:
                    problems.append(f"target_A.{stratum}={val} 为负，配额不能为负")
                    continue
                targets[stratum] = val

    locks: List[LockSpec] = []
    if "locks" in doc:
        lk = doc["locks"]
        if not isinstance(lk, list):
            problems.append("locks 必须是数组")
        else:
            by_cluster: Dict[str, LockSpec] = {}
            for i, item in enumerate(lk):
                pos = f"locks[{i}]"
                if not isinstance(item, dict):
                    problems.append(f"{pos} 必须是对象 {{cluster_id, arm, reason?}}")
                    continue
                extra_keys = [k for k in item if k not in ("cluster_id", "arm", "reason")]
                if extra_keys:
                    problems.append(f"{pos} 出现未知字段 {extra_keys!r}")
                cid = _text(item.get("cluster_id"))
                arm = item.get("arm")
                if cid is None:
                    problems.append(f"{pos}.cluster_id 必须是非空文本")
                if arm not in ("A", "B"):
                    problems.append(f"{pos}.arm 必须是方案名 \"A\" 或 \"B\"，收到 {arm!r}")
                if cid is None or arm not in ("A", "B"):
                    continue
                spec = LockSpec(cid, arm, item.get("reason"), pos)
                if cid in by_cluster:
                    prev = by_cluster[cid]
                    if prev.arm == arm:
                        problems.append(
                            f"{pos} 与 {prev.position} 重复锁定同一 cluster {cid!r} 到 {arm}")
                    else:
                        problems.append(
                            f"{pos} 与 {prev.position} 把 cluster {cid!r} 同时锁定到 "
                            f"A 与 B（冲突，无解；请只保留一项）")
                else:
                    by_cluster[cid] = spec
                    locks.append(spec)

    if problems:
        raise AllocationInputError(problems)
    return Design(targets, locks)
