"""锚点 CSV 的读取、校验、折叠与分组。

输入契约
--------
CSV 表头恰为 ``anchor_id, device_id, boot_seq, local_ns, reference_ns,
error_ns``（按名取列，列序可变）；所有时间字段为十进制整数纳秒，可为负。
``error_ns`` 必须为非负整数。

拒绝策略
--------
- 表头缺列/多列/重复、行字段数不符、字段为空、``anchor_id`` 为空 → 文件级
  错误（``AnchorFileError``），整份输入不采用；
- 数值非法（非十进制整数、``error_ns < 0``、``device_id``/``boot_seq`` 非整数）
  → ``AnchorRowError``，逐行列出 1-based 行号；
- 同一 ``anchor_id`` 出现两次 → ``AnchorRowError``，保留首次、指出第二次；
- ``anchor_id`` 按**文本**身份比较（不把 ``001`` 与 ``1`` 合并）。

分组规则
--------
按 ``(device_id, boot_seq)`` 分组。同组内 ``local_ns`` 相同的锚点集合：
- 若 reference_ns 与 error_ns 全部相同 → 折叠为一个锚点，``anchor_ids`` 列出
  全部来源 id（输出仍可追溯每个 anchor_id）；
- 否则整组标记 ``invalid``（``conflicting_local_anchor``），该组不参与对齐。

折叠后每组要求：≥2 个锚点；local_ns 与 reference_ns 都严格递增；否则整组
``invalid``。斜率检验在 ``group.py`` 中完成（依赖 ``max_ppm`` 配置）。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

HEADER = ("anchor_id", "device_id", "boot_seq", "local_ns",
          "reference_ns", "error_ns")

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1


class AnchorInputError(Exception):
    """锚点输入未被采用（文件级问题）。"""


class AnchorFileError(AnchorInputError):
    """CSV 结构本身不可用（表头、字段数等）。"""


class AnchorRowError(AnchorInputError):
    """数据行非法；``problems`` 为逐行说明列表。"""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class RawAnchor:
    anchor_id: str
    device_id: int
    boot_seq: int
    local_ns: int
    reference_ns: int
    error_ns: int
    line_no: int


@dataclass(frozen=True)
class Anchor:
    """折叠后的锚点（一个本地时刻一条）。"""

    local_ns: int
    reference_ns: int
    error_ns: int
    anchor_ids: Tuple[str, ...]

    @property
    def primary_id(self) -> str:
        return self.anchor_ids[0]


@dataclass
class Group:
    """一个 (device_id, boot_seq) 的锚点组；``invalid_reason`` 为 None 才可用。"""

    device_id: int
    boot_seq: int
    anchors: List[Anchor] = field(default_factory=list)
    invalid_reason: str = None
    n_raw: int = 0
    n_collapsed: int = 0

    @property
    def key(self) -> Tuple[int, int]:
        return (self.device_id, self.boot_seq)

    @property
    def valid(self) -> bool:
        return self.invalid_reason is None


@dataclass
class AnchorSet:
    groups: Dict[Tuple[int, int], Group]
    n_rows: int

    def get(self, device_id: int, boot_seq: int):
        return self.groups.get((device_id, boot_seq))


def _parse_int(text: str, what: str) -> int:
    """十进制整数（允许前导 +/-）；拒绝空白、下划线、浮点与超 int64。"""
    s = text
    if s.strip() != s or not s:
        raise ValueError(f"{what} 不是合法十进制整数：{text!r}")
    body = s[1:] if s[0] in "+-" else s
    if not body or any(c not in "0123456789" for c in body):
        raise ValueError(f"{what} 不是合法十进制整数：{text!r}")
    value = int(s)
    if not (INT64_MIN <= value <= INT64_MAX):
        raise ValueError(f"{what} 超出 int64 范围：{text}")
    return value


def read_anchor_csv(path: str | Path) -> Tuple[List[RawAnchor], List[str]]:
    """读取并校验 CSV；返回 (原始锚点, 结构警告)。

    文件级结构问题抛 ``AnchorFileError``；行级问题收集到
    ``AnchorRowError``。不会对任何行"猜测"或跳过。
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as e:
        raise AnchorFileError(f"无法读取锚点文件 {path}：{e}") from e
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        rows = list(reader)
    except csv.Error as e:
        raise AnchorFileError(f"CSV 解析失败：{e}") from e
    if not rows:
        raise AnchorFileError("锚点文件为空（没有表头）")
    header = [h.strip() for h in rows[0]]
    if len(set(header)) != len(header):
        raise AnchorFileError(f"表头含重复列：{header}")
    missing = [h for h in HEADER if h not in header]
    extra = [h for h in header if h not in HEADER]
    if missing or extra:
        raise AnchorFileError(
            f"表头必须恰为 {list(HEADER)}；缺少 {missing}，多出 {extra}")
    col = {name: i for i, name in enumerate(header)}

    anchors: List[RawAnchor] = []
    problems: List[str] = []
    seen_ids: Dict[str, int] = {}
    warnings: List[str] = []
    for line_no, row in enumerate(rows[1:], start=2):
        if not row or all(not c.strip() for c in row):
            continue                              # 空行（含行尾换行）忽略
        if len(row) != len(header):
            problems.append(
                f"第 {line_no} 行：字段数 {len(row)} != 表头 {len(header)}")
            continue
        raw_id = row[col["anchor_id"]]
        anchor_id = raw_id.strip()
        if not anchor_id:
            problems.append(f"第 {line_no} 行：anchor_id 为空")
            continue
        if raw_id != anchor_id:
            warnings.append(f"第 {line_no} 行：anchor_id {raw_id!r} 两端有空白，"
                            f"按 {anchor_id!r} 处理")
        values: Dict[str, int] = {}
        bad = False
        for name in ("device_id", "boot_seq", "local_ns", "reference_ns",
                     "error_ns"):
            cell = row[col[name]]
            if not cell:
                problems.append(f"第 {line_no} 行：{name} 为空")
                bad = True
                continue
            try:
                values[name] = _parse_int(cell, name)
            except ValueError as e:
                problems.append(f"第 {line_no} 行：{e}")
                bad = True
        if bad:
            continue
        if values["error_ns"] < 0:
            problems.append(f"第 {line_no} 行：error_ns 必须为非负整数，"
                            f"实际 {values['error_ns']}")
            continue
        if anchor_id in seen_ids:
            problems.append(
                f"第 {line_no} 行：重复 anchor_id {anchor_id!r}"
                f"（首次见于第 {seen_ids[anchor_id]} 行）")
            continue
        if values["device_id"] < 0:
            problems.append(f"第 {line_no} 行：device_id 必须为非负整数，"
                            f"实际 {values['device_id']}")
            continue
        if values["boot_seq"] < 0:
            problems.append(f"第 {line_no} 行：boot_seq 必须为非负整数，"
                            f"实际 {values['boot_seq']}")
            continue
        seen_ids[anchor_id] = line_no
        anchors.append(RawAnchor(anchor_id, values["device_id"],
                                 values["boot_seq"], values["local_ns"],
                                 values["reference_ns"], values["error_ns"],
                                 line_no))
    if problems:
        raise AnchorRowError(problems)
    return anchors, warnings


def build_anchor_set(anchors: Iterable[RawAnchor]) -> AnchorSet:
    """折叠并校验分组：同刻一致性、数量、严格递增。

    斜率检验依赖 ``max_ppm`` 配置，由 ``mapping.finalize_groups`` 在全部
    结构校验通过后统一执行（好让无效组也能给出结构原因而非斜率原因）。
    """
    anchors = list(anchors)
    groups: Dict[Tuple[int, int], Group] = {}
    for raw in anchors:
        key = (raw.device_id, raw.boot_seq)
        g = groups.get(key)
        if g is None:
            g = Group(raw.device_id, raw.boot_seq)
            groups[key] = g
        g.n_raw += 1
        if raw.device_id < 0 or raw.boot_seq < 0:
            if g.invalid_reason is None:
                g.invalid_reason = (
                    f"非法坐标：device_id={raw.device_id}、boot_seq="
                    f"{raw.boot_seq} 必须为非负整数（anchor_id={raw.anchor_id}）")

    by_local: Dict[Tuple[int, int], Dict[int, List[RawAnchor]]] = {}
    for raw in anchors:
        by_local.setdefault((raw.device_id, raw.boot_seq), {}) \
                .setdefault(raw.local_ns, []).append(raw)

    for key, g in groups.items():
        if g.invalid_reason is not None:
            continue
        for local_ns, rows in by_local[key].items():
            refs = {r.reference_ns for r in rows}
            errs = {r.error_ns for r in rows}
            if len(refs) > 1 or len(errs) > 1:
                ids = ", ".join(r.anchor_id for r in rows)
                g.invalid_reason = (
                    f"同一本地时刻 {local_ns} 的锚点参考值/误差不一致"
                    f"（anchor_id: {ids}）；该组整体不能对齐")
                break
            g.anchors.append(Anchor(local_ns, rows[0].reference_ns,
                                    rows[0].error_ns,
                                    tuple(sorted(r.anchor_id for r in rows))))
            g.n_collapsed += 1
        if g.invalid_reason is not None:
            continue
        g.anchors.sort(key=lambda a: a.local_ns)
        if len(g.anchors) < 2:
            g.invalid_reason = (f"折叠后只有 {len(g.anchors)} 个锚点，至少需要 2 个")
            continue
        for a, b in zip(g.anchors, g.anchors[1:]):
            if a.local_ns == b.local_ns:
                g.invalid_reason = f"本地坐标未严格递增（{a.local_ns}）"
                break
            if a.reference_ns >= b.reference_ns:
                g.invalid_reason = (
                    f"参考坐标未严格递增：{a.primary_id} 的 reference_ns="
                    f"{a.reference_ns} >= {b.primary_id} 的 {b.reference_ns}")
                break
    return AnchorSet(groups, len(anchors))
