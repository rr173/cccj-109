"""结果输出：全量校验通过后**原子**写出，失败绝不覆盖既有文件。

写出契约
--------
- 只在 ``Z > 0``、所有内部核对（方案合法性、轨迹、rank 枚举）通过后才调用；
  无解、未完成精确求解、输入非法或核对失败都不会写任何文件；
- 写入使用「同目录临时文件 + ``os.replace``」：先在同一目录写 ``.tmp-`` 前缀
  的临时文件，校验内容完整后 ``os.replace`` 到最终路径。``os.replace`` 是
  原子的，因此既有文件要么保持旧内容，要么整体变成新内容，不会出现半截文件；
- 若写临时文件失败，删除临时文件并原样保留既有文件；
- 每次运行不带 ``--out-dir`` 时只打印到标准输出，不落盘。

输出文件（``--out-dir`` 给出时）
--------------------------------
- ``summary.txt``        可审阅摘要：Z、seed、模式、rank、规范排序、锁定项、
                         逐层计数、每组方案与逐层 A/B 台数、预算与状态数；
- ``allocation.csv``     逐设备名单：``device_id,stratum,cluster_id,arm,locked``；
- ``trace.csv``          抽签/解码每一步：面对的两个完成数、随机数/局部 rank、
                         实际选择、选前/选后剩余配额；
- ``design.normalized.json`` 与输入无关的规范化设计（层序、组序、配额、锁定），
                         便于另一进程只凭「设计 + rank」复算，而无需重放算法
                         内部状态；
- ``run.json``           运行参数与结果 rank（供复算与追溯）。

所有文件内容只来自已算出的结果；本工具不写观察日志、不写 DuckDB。
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .count import CountTables
from .model import A, B, LockedReduction, Problem
from .sampler import Outcome

SUMMARY_NAME = "summary.txt"
DEVICES_NAME = "allocation.csv"
TRACE_NAME = "trace.csv"
DESIGN_NAME = "design.normalized.json"
RUN_NAME = "run.json"


def normalized_design(prob: Problem, red: LockedReduction,
                      tables: CountTables) -> dict:
    """与 CSV 行序无关的规范化设计：层序、组序、每层配额、锁定。"""
    return {
        "ordering": {
            "strata": list(prob.strata),
            "clusters": list(prob.clusters),
        },
        "target_A": {s: prob.targets[i] for i, s in enumerate(prob.strata)},
        "stratum_sizes": {s: prob.stratum_sizes[i]
                          for i, s in enumerate(prob.strata)},
        "cluster_contribution": {
            prob.clusters[ci]: {prob.strata[si]: n for si, n in prob.membership[ci]}
            for ci in range(len(prob.clusters))
        },
        "locks": [
            {"cluster_id": prob.clusters[ci], "arm": arm}
            for ci, arm in prob.locks
        ],
        "locked_A_contribution": {
            prob.strata[i]: red.lock_contrib[i] for i in range(len(prob.strata))
        },
        "residual": {prob.strata[i]: red.residual[i]
                     for i in range(len(prob.strata))},
        "unlocked_clusters": [prob.clusters[ci] for ci in red.remaining_clusters],
        "Z": tables.Z,
        "rank_order": "cluster order above, A before B (lexicographic)",
    }


def _device_rows(prob: Problem, outcome: Outcome) -> List[List[str]]:
    rows = []
    for d in sorted(prob.devices, key=lambda x: (x.cluster_id, x.device_id)):
        ci = prob.cluster_index[d.cluster_id]
        rows.append([d.device_id, d.stratum, d.cluster_id,
                     outcome.arms[d.cluster_id],
                     "yes" if ci in prob.lock_by_cluster else "no"])
    return rows


def _layer_rows(prob: Problem, outcome: Outcome) -> List[str]:
    lines = [f"{'层':<24}{'设备数':>8}{'target_A':>10}{'实际A':>8}{'实际B':>8}"]
    for i, s in enumerate(prob.strata):
        n_a = sum(1 for d in prob.devices
                  if d.stratum == s and outcome.arms[d.cluster_id] == A)
        n_b = prob.stratum_sizes[i] - n_a
        flag = "" if n_a == prob.targets[i] else "  ← 不符"
        lines.append(f"{s:<24}{prob.stratum_sizes[i]:>8}{prob.targets[i]:>10}"
                     f"{n_a:>8}{n_b:>8}{flag}")
    return lines


def summary_text(prob: Problem, red: LockedReduction, tables: CountTables,
                 outcome: Outcome, max_states: int) -> str:
    lines: List[str] = []
    lines.append("实验分配结果（整簇随机化；精确等概率抽签）")
    lines.append("=" * 78)
    lines.append(f"方案总数 Z = {tables.Z}")
    lines.append(f"模式 = {outcome.mode}；seed = {outcome.seed}；"
                 f"结果 rank = {outcome.rank}")
    lines.append(f"动态规划状态数 = {tables.n_states}（预算 max_states = {max_states}）")
    lines.append("")
    lines.append("规范排序（按 Python 字符串码点顺序，与 CSV 行序无关）：")
    lines.append(f"  层序:   {', '.join(repr(s) for s in prob.strata)}")
    lines.append(f"  cluster 序: {', '.join(repr(c) for c in prob.clusters)}")
    lines.append("  rank 按上述 cluster 序、A 在 B 前的字典序编号")
    lines.append("")
    lines.append("锁定项（先计入配额推理）：")
    if not prob.lock_specs:
        lines.append("  （无）")
    for spec in prob.lock_specs:
        ci = prob.cluster_index[spec.cluster_id]
        contrib = ", ".join(f"{prob.strata[si]}×{n}" for si, n in prob.membership[ci])
        reason = "" if spec.reason is None else f"；理由: {spec.reason!r}"
        lines.append(f"  {spec.cluster_id!r} → {spec.arm}（逐层设备数 {contrib or '—'}）"
                     f"{reason}")
    if red.lock_A_clusters or red.lock_B_clusters:
        lines.append("  锁定后剩余配额（target_A − 锁定 A 贡献）:")
        for i, s in enumerate(prob.strata):
            lines.append(f"    {s!r}: {prob.targets[i]} − {red.lock_contrib[i]} "
                         f"= {red.residual[i]}（未锁定设备 {red.residual_devices[i]} 台）")
    lines.append("")
    lines.append("每层 A/B 台数：")
    lines += _layer_rows(prob, outcome)
    lines.append("")
    lines.append("每个 cluster 的方案（按规范组序）：")
    lines.append(f"  {'cluster':<20}{'方案':<6}{'锁定':<6}逐层设备数")
    for ci, cid in enumerate(prob.clusters):
        contrib = ", ".join(f"{prob.strata[si]}×{n}" for si, n in prob.membership[ci])
        locked = "是" if ci in prob.lock_by_cluster else ""
        lines.append(f"  {cid:<20}{outcome.arms[cid]:<6}{locked:<6}{contrib}")
    lines.append("")
    lines.append("逐设备名单（按 cluster、device_id 排序）：")
    lines.append(f"  {'device_id':<20}{'stratum':<20}{'cluster_id':<20}{'方案':<6}{'锁定'}")
    for dev, stratum, cid, arm, locked in _device_rows(prob, outcome):
        lines.append(f"  {dev:<20}{stratum:<20}{cid:<20}{arm:<6}{locked}")
    lines.append("")
    lines.append("抽签轨迹（每一步面对的 A/B 完成数与实际选择）：")
    lines.append(f"  {'#':>3} {'cluster':<18}{'锁定':<5}{'A完成数':>8}{'B完成数':>8}"
                 f"{'随机数/局部rank':>16}{'选择':>5}  剩余配额")
    for idx, step in enumerate(outcome.trace, start=1):
        zA = "-" if step.z_A is None else str(step.z_A)
        zB = "-" if step.z_B is None else str(step.z_B)
        u = "-" if step.u is None else str(step.u)
        rem = "[" + ",".join(str(x) for x in step.remaining_after) + "]"
        lines.append(f"  {idx:>3} {step.cluster_id:<18}"
                     f"{'是' if step.locked else '':<5}{zA:>8}{zB:>8}{u:>16}"
                     f"{step.chosen:>5}  {rem}")
    lines.append("")
    lines.append("说明：抽签等价于在 [0,Z) 上均匀抽一个 rank 再按同一字典序解码；"
                 "seed 只用于产生分支随机数，不改变 Z、组序或 rank↔方案对应。")
    return "\n".join(lines) + "\n"


def _stage(path: Path, text: str) -> Path:
    """把内容写进同目录临时文件并返回其路径（尚未替换目标）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    return Path(tmp)


def _commit_all(pairs: List[Tuple[Path, str]]) -> None:
    """先全部暂存，再逐个 ``os.replace``；暂存阶段失败则清理且不动既有文件。"""
    staged: List[Tuple[Path, Path]] = []
    try:
        for path, text in pairs:
            staged.append((_stage(path, text), path))
        for tmp, path in staged:
            os.replace(tmp, path)
    except BaseException:
        for tmp, _ in staged:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        raise


def write_outputs(out_dir: str | Path, prob: Problem, red: LockedReduction,
                  tables: CountTables, outcome: Outcome, max_states: int,
                  input_info: Optional[dict] = None) -> Path:
    """全部内容先在内存中格式化，再逐个原子写出。"""
    out = Path(out_dir)
    device_rows = _device_rows(prob, outcome)
    trace_rows = []
    for idx, step in enumerate(outcome.trace, start=1):
        trace_rows.append([
            idx, step.cluster_id, "yes" if step.locked else "no",
            step.locked_arm or "",
            "" if step.z_A is None else step.z_A,
            "" if step.z_B is None else step.z_B,
            "" if step.u is None else step.u,
            step.chosen,
            json.dumps(list(step.remaining_before)),
            json.dumps(list(step.remaining_after)),
            step.contribution and ";".join(f"{s}={n}" for s, n in step.contribution),
        ])
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["device_id", "stratum", "cluster_id", "arm", "locked"])
    w.writerows(device_rows)
    devices_text = buf.getvalue()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["step", "cluster_id", "locked", "locked_arm", "z_A", "z_B",
                "u_or_local_rank", "chosen", "remaining_before",
                "remaining_after", "contribution"])
    w.writerows(trace_rows)
    trace_text = buf.getvalue()

    summary = summary_text(prob, red, tables, outcome, max_states)
    design_text = json.dumps(normalized_design(prob, red, tables),
                             ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    run_doc = {
        "mode": outcome.mode,
        "seed": outcome.seed,
        "rank": outcome.rank,
        "Z": tables.Z,
        "max_states": max_states,
        "n_states": tables.n_states,
        "rank_order": "canonical cluster order, A before B",
        "replay": "use design.normalized.json + rank; no algorithm state needed",
    }
    if input_info:
        run_doc["inputs"] = input_info
    run_text = json.dumps(run_doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    out.mkdir(parents=True, exist_ok=True)
    _commit_all([(out / name, text) for name, text in (
        (SUMMARY_NAME, summary), (DEVICES_NAME, devices_text),
        (TRACE_NAME, trace_text), (DESIGN_NAME, design_text),
        (RUN_NAME, run_text))])
    return out
