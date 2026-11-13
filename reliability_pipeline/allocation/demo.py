"""可复跑材料、独立枚举程序与全部回归核对（``python3 -m allocation.cli demo``）。

材料
----
1. ``cross_layer_infeasible``  逐层看配额都够、合起来无解（跨层整簇约束）。
2. ``locked_multi``            含锁定、存在多解（逐 rank 全枚举核对）。
3. ``huge_z``                  很多单设备组，``Z`` 为可手算组合数且 > 2^53，
                               验证末尾 rank 仍能准确访问、抽签不经浮点。
4. 边界：空名单、全锁定、零/全额配额、重复设备、非法 rank、预算刚好够/少一个。

独立枚举程序
------------
``allocation.verify.enumerate_feasible`` 显式枚举全部 ``2^m`` 个候选标记、
筛出可行者并按固定组序排序；它与生产 DP 方向不同（不做后缀计数），
仅用于小实例逐 rank 核对。生产抽签路径绝不枚举指数空间。

分布检查
--------
固定小规模实例 + 大量种子抽签，统计每个方案被抽中的频率。这只是对
「每份可行方案等概率」推导的**补充证据**，不是均匀性的证明；证明见
``docs/allocation.md`` 与 ``allocation/sampler.py`` 的模块文档。
"""

from __future__ import annotations

import csv
import json
import math
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import outputs as out_mod, sampler
from .count import (StateBudgetExhausted, build_tables,
                    per_layer_reachability)
from .inputs import AllocationInputError, Design, DeviceRow, read_design_json, read_device_csv
from .model import A, B, prepare, reduce_locks
from .verify import (check_outcome, check_trace, count_forward,
                     enumerate_feasible)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXIT_OK, EXIT_UNSOLVABLE, EXIT_INPUT, EXIT_BUDGET = 0, 1, 2, 3


# ----------------------------------------------------------------------
# 材料写出
# ----------------------------------------------------------------------

DEVICE_HEADER = ["device_id", "stratum", "cluster_id"]


def _write_csv(path: Path, header: List[str], rows: List[List[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def _write_design(path: Path, targets: Dict[str, int],
                  locks: Optional[List[dict]] = None) -> None:
    doc: dict = {"target_A": targets}
    if locks is not None:
        doc["locks"] = locks
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _material_cross_layer(work: Path) -> dict:
    """逐层可行、合起来无解：三个跨层整簇。

    层 L1 有 3 台（组 g1 1 台 + g2 2 台），层 L2 有 3 台（组 g1 2 台 + g3 1 台）。
    ``target_A(L1)=1``、``target_A(L2)=1``：
    - L1 单独看可选 g1 或 g2（都能凑 1）；
    - L2 单独看可选 g1 或 g3（都能凑 1）；
    - 但 g1 一旦选 A 同时给两层各贡献 1/2 台，两层都超出/不足：
      g1→A 时 L1 已有 1（需 0 剩余，但 g2 只能 0 或 2）、L2 已有 2 > 1；
      g1→B 时 L1 只能靠 g2 给 2 台（≠1），L2 只能靠 g3 给 1 台。
    因此 Z=0，而每层单独都有「可行」子集。
    """
    rows = [
        ["L1-1", "L1", "g1"],
        ["L1-2", "L1", "g2"], ["L1-3", "L1", "g2"],
        ["L2-1", "L2", "g1"], ["L2-2", "L2", "g1"],
        ["L2-3", "L2", "g3"],
    ]
    d = work / "cross_layer_infeasible"
    d.mkdir(parents=True)
    _write_csv(d / "devices.csv", DEVICE_HEADER, rows)
    _write_design(d / "design.json", {"L1": 1, "L2": 1})
    return {"dir": d, "devices": d / "devices.csv",
            "design": d / "design.json", "targets": {"L1": 1, "L2": 1}}


def _material_locked_multi(work: Path) -> dict:
    """含锁定、多解：8 个可行方案，g3 锁定到 A。

    A 层：a1..a6 各 1 台，cluster 为 ga1..ga6（单设备组）；
    B 层：b1..b3 各 1 台，cluster 为 gb1..gb3（单设备组）。

    因为每台设备自成一组，（ga1..ga6 选 4 个）与（gb1..gb3 选 2 个）的任意
    组合都可行：Z = C(6,4)·C(3,2) = 15·3 = 45。再把 gb3 锁定到 B，
    则只剩 C(6,4)·C(2,2) = 15 个方案。该实例适合逐 rank 全枚举核对。
    """
    rows = []
    for i in range(1, 7):
        rows.append([f"a{i}", "A", f"ga{i}"])
    for i in range(1, 4):
        rows.append([f"b{i}", "B", f"gb{i}"])
    d = work / "locked_multi"
    d.mkdir(parents=True)
    _write_csv(d / "devices.csv", DEVICE_HEADER, rows)
    _write_design(d / "design.json", {"A": 4, "B": 2},
                  [{"cluster_id": "gb3", "arm": "B", "reason": "共用班组已定"}])
    return {"dir": d, "devices": d / "devices.csv",
            "design": d / "design.json",
            "expected_Z": math.comb(6, 4) * math.comb(2, 2)}


def _material_huge_z(work: Path) -> dict:
    """很多单设备组，Z > 2^53 且可手算。

    6 个层，每层 ``m`` 个单设备组，各层 target_A=2；每层有 C(m,2) 种选法，
    总方案数 Z = C(m,2)^6。取 m=12 → 66^6 ≈ 8.27e10（小于 2^53，不够大），
    取 m=30 → C(30,2)=435，Z = 435^6 ≈ 6.79e15（> 2^53 ≈ 9.0e15? 实际
    435^6 = 6.79e15 < 9.007e15）。取 m=32 → C(32,2)=496，
    Z = 496^6 ≈ 1.49e16 > 2^53，且 6 层 × 32 组 = 192 个单设备组。
    """
    m = 32
    tiers = [f"T{i}" for i in range(1, 7)]
    rows = []
    for ti, s in enumerate(tiers):
        for k in range(1, m + 1):
            rows.append([f"{s}-{k:02d}", s, f"{s}-c{k:03d}"])
    d = work / "huge_z"
    d.mkdir(parents=True)
    _write_csv(d / "devices.csv", DEVICE_HEADER, rows)
    _write_design(d / "design.json", {s: 2 for s in tiers})
    z = math.comb(m, 2) ** len(tiers)
    assert z > 2 ** 53, z
    return {"dir": d, "devices": d / "devices.csv",
            "design": d / "design.json", "m": m,
            "expected_Z": z}


def _material_edge_cases(work: Path) -> Dict[str, dict]:
    """边界材料：空名单、全锁定、零/全额配额、重复设备。"""
    out: Dict[str, dict] = {}
    d = work / "edges"
    d.mkdir(parents=True)

    # 空名单（只有表头）
    p = d / "empty_devices.csv"
    _write_csv(p, DEVICE_HEADER, [])
    des = d / "empty_design.json"
    _write_design(des, {})
    out["empty"] = {"devices": p, "design": des}

    # 空名单、设计里给了未知层 → 拒绝
    des2 = d / "empty_unknown_design.json"
    _write_design(des2, {"ghost": 1})
    out["empty_unknown_layer"] = {"devices": p, "design": des2}

    # 全锁定：所有组都锁定，配额恰好由锁定满足 → Z=1
    rows = [["x1", "S", "cx"], ["y1", "T", "cy"]]
    p = d / "all_locked_devices.csv"
    _write_csv(p, DEVICE_HEADER, rows)
    des = d / "all_locked_design.json"
    _write_design(des, {"S": 1, "T": 0},
                  [{"cluster_id": "cx", "arm": "A"}, {"cluster_id": "cy", "arm": "B"}])
    out["all_locked"] = {"devices": p, "design": des}

    # 零配额：目标 A=0，所有组只能选 B → 唯一方案
    rows = [["x1", "S", "cx1"], ["x2", "S", "cx2"]]
    p = d / "zero_quota_devices.csv"
    _write_csv(p, DEVICE_HEADER, rows)
    des = d / "zero_quota_design.json"
    _write_design(des, {"S": 0})
    out["zero_quota"] = {"devices": p, "design": des}

    # 全额配额：目标 A=全体 → 唯一方案
    p = d / "full_quota_devices.csv"
    _write_csv(p, DEVICE_HEADER, rows)
    des = d / "full_quota_design.json"
    _write_design(des, {"S": 2})
    out["full_quota"] = {"devices": p, "design": des}

    # 重复设备（第二行 device_id 与第一行相同）
    p = d / "duplicate_devices.csv"
    _write_csv(p, DEVICE_HEADER, [["x1", "S", "c1"], ["x1", "S", "c2"]])
    des = d / "duplicate_design.json"
    _write_design(des, {"S": 1})
    out["duplicate"] = {"devices": p, "design": des}

    # 重复设备但文本不同（"001" 与 "1"）——合法，是两个设备
    p = d / "text_ids_devices.csv"
    _write_csv(p, DEVICE_HEADER, [["001", "S", "c1"], ["1", "S", "c2"]])
    des = d / "text_ids_design.json"
    _write_design(des, {"S": 1})
    out["text_ids"] = {"devices": p, "design": des}

    # 锁定冲突（同一组同时 A/B）
    p = d / "lock_conflict_devices.csv"
    _write_csv(p, DEVICE_HEADER, [["x1", "S", "cx"]])
    des = d / "lock_conflict_design.json"
    _write_design(des, {"S": 1}, [{"cluster_id": "cx", "arm": "A"},
                                  {"cluster_id": "cx", "arm": "B"}])
    out["lock_conflict"] = {"devices": p, "design": des}

    # 锁定使某层超额
    rows = [["x1", "S", "cx1"], ["x2", "S", "cx2"]]
    p = d / "lock_over_devices.csv"
    _write_csv(p, DEVICE_HEADER, rows)
    des = d / "lock_over_design.json"
    _write_design(des, {"S": 0}, [{"cluster_id": "cx1", "arm": "A"}])
    out["lock_over"] = {"devices": p, "design": des}
    return out


def build_materials(workdir: Path) -> dict:
    workdir = Path(workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    mats = {
        "cross_layer": _material_cross_layer(workdir),
        "locked_multi": _material_locked_multi(workdir),
        "huge_z": _material_huge_z(workdir),
        "edges": _material_edge_cases(workdir),
    }
    return mats


# ----------------------------------------------------------------------
# 命令驱动
# ----------------------------------------------------------------------

def run_cli(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "allocation.cli"] + args,
                          cwd=REPO_ROOT, text=True, capture_output=True)


def _load_problem(devices, design):
    rows, warnings = read_device_csv(devices)
    des = read_design_json(design)
    return prepare(rows, des), warnings


def _check(problems: List[str], cond: bool, msg: str) -> None:
    if not cond:
        problems.append(msg)


# ----------------------------------------------------------------------
# 各段演示
# ----------------------------------------------------------------------

def demo_cross_layer(m: dict, problems: List[str]) -> None:
    print("\n== 1. 逐层都够、合起来无解（跨层整簇） ==")
    print(f"材料: {m['devices']}\n设计: {m['design']} → {m['targets']}")
    prob, _ = _load_problem(m["devices"], m["design"])
    red = reduce_locks(prob)
    # 独立诊断：逐层子集和
    reach = per_layer_reachability(prob, red)
    print("  逐层单独看（忽略跨层耦合）:")
    layerwise_ok = True
    for i, s in enumerate(prob.strata):
        need = red.residual[i]
        ok = need in reach[i]
        layerwise_ok &= ok
        print(f"    层 {s!r}: 需要 {need}，单层可达子集和 {sorted(reach[i])} → "
              f"{'够' if ok else '不够'}")
    _check(problems, layerwise_ok, "该材料应满足「逐层都够」")
    tables = build_tables(prob, red, 1000)
    _check(problems, tables.Z == 0, f"跨层材料应为无解，实际 Z={tables.Z}")
    print(f"  DP 精确计数 Z = {tables.Z} → 无解（不松配额、不拆组、不给近似）")
    # 显式枚举佐证：2 个非锁定组（这里 3 个组自由），全部候选都不可行
    feasible = enumerate_feasible(prob, 1 << 16)
    _check(problems, feasible == [], f"独立枚举应得 0 个可行方案，实际 {len(feasible)}")
    print(f"  独立枚举 {2 ** len(prob.clusters)} 个候选标记 → 可行 {len(feasible)} 个")
    # 命令行行为
    r = run_cli(["run", "--devices", str(m["devices"]),
                 "--design", str(m["design"])])
    _check(problems, r.returncode == EXIT_UNSOLVABLE,
           f"无解命令退出码应为 {EXIT_UNSOLVABLE}，实际 {r.returncode}")
    _check(problems, "约束无解" in r.stderr, "无解时应明确报告约束无解")
    _check(problems, "跨层" in r.stderr, "无解报告应指出跨层耦合原因")
    print("  命令行: exit=%d —— %s" % (r.returncode,
                                       r.stderr.strip().splitlines()[0]))


def demo_locked_multi(m: dict, problems: List[str]) -> None:
    print("\n== 2. 含锁定、存在多解：逐 rank 全枚举核对 ==")
    print(f"材料: {m['devices']}\n设计: {m['design']}（gb3 锁定到 B）")
    prob, _ = _load_problem(m["devices"], m["design"])
    red = reduce_locks(prob)
    tables = build_tables(prob, red, 100_000)
    Z = tables.Z
    _check(problems, Z == m["expected_Z"],
           f"Z 应为 {m['expected_Z']}，实际 {Z}")
    _check(problems, count_forward(prob, red) == Z, "正向 DP 与后缀 DP 的 Z 不一致")
    feasible = enumerate_feasible(prob, 1 << 20)
    _check(problems, len(feasible) == Z, f"枚举 {len(feasible)} ≠ Z={Z}")
    _check(problems, len(set(feasible)) == len(feasible), "枚举出现重复方案")
    print(f"  Z = {Z}（手算 C(6,4)·C(2,2) = {m['expected_Z']}）；"
          f"独立枚举 {len(feasible)} 个，无重复")

    decoded: List[Tuple[str, ...]] = []
    for rank in range(Z):
        o = sampler.decode(prob, tables, rank)
        probs = check_outcome(prob, red, tables, o) + check_trace(prob, red, tables, o)
        _check(problems, not probs, f"rank={rank} 核对失败: {probs[:3]}")
        vec = tuple(o.arms[c] for c in prob.clusters)
        _check(problems, vec not in decoded, f"rank={rank} 与之前重复")
        decoded.append(vec)
        _check(problems, sampler.encode_rank(prob, tables, o.arms) == rank,
               f"rank={rank} 重编码不为自身")
        if rank < 5 or rank == Z - 1:
            print(f"  rank {rank:>2}: {o.arms}")
    _check(problems, decoded == feasible,
           "逐 rank 解码顺序与独立枚举的字典序不一致")
    print(f"  逐 rank 0..{Z - 1} 解码：不重不漏，且与独立枚举字典序一致")

    # 每个合法 seed：整组完整、每层计数精确（命令行 + JSON 摘要）
    seed_outcomes = []
    for seed in range(5):
        o = sampler.sample(prob, tables, seed)
        seed_outcomes.append(tuple(o.arms[c] for c in prob.clusters))
        ps = check_outcome(prob, red, tables, o) + check_trace(prob, red, tables, o)
        _check(problems, not ps, f"seed={seed} 抽签核对失败: {ps[:3]}")
        _check(problems, 0 <= o.rank < Z, f"seed={seed} rank 越界")
        # 同一 cluster 的设备必须同方案（整组不可拆）
        for cid in prob.clusters:
            arms = {o.arms[cid]}
            _check(problems, len(arms) == 1, f"seed={seed} cluster {cid} 被拆开")
        # 每层计数
        for i, s in enumerate(prob.strata):
            n_a = sum(1 for d in prob.devices
                      if d.stratum == s and o.arms[d.cluster_id] == A)
            _check(problems, n_a == prob.targets[i],
                   f"seed={seed} 层 {s} 的 A 数 {n_a} ≠ {prob.targets[i]}")
        print(f"  seed {seed}: rank={o.rank} 前 3 组 "
              f"{[o.arms[c] for c in prob.clusters[:3]]}…（整组完整、逐层计数正确）")


def demo_huge_z(m: dict, problems: List[str]) -> None:
    print("\n== 3. Z > 2^53：末尾 rank 精确访问、抽签不经浮点 ==")
    print(f"材料: {m['devices']}（6 层 × {m['m']} 个单设备组，各层 target_A=2）")
    prob, _ = _load_problem(m["devices"], m["design"])
    red = reduce_locks(prob)
    tables = build_tables(prob, red, 2_000_000)
    Z = tables.Z
    _check(problems, Z == m["expected_Z"],
           f"Z 应为可手算 {m['expected_Z']}，实际 {Z}")
    _check(problems, Z > 2 ** 53, f"Z 应 > 2^53，实际 {Z}")
    print(f"  Z = 496^6 = {Z} > 2^53 = {2 ** 53}（手算一致）")
    # 末尾 rank 精确访问
    last = sampler.decode(prob, tables, Z - 1)
    _check(problems, last.rank == Z - 1, "末尾 rank 解码自报不符")
    _check(problems, not check_outcome(prob, red, tables, last),
           "末尾 rank 方案非法")
    # 固定组序下 A 在 B 前，最后一个 rank 是「按 A/B 字典序最大」的可行方案：
    # 对本材料（每层 32 个单设备组选 2 个进 A），它把每层最后两个组放进 A。
    for ti, s in enumerate(prob.strata):
        last_two = [d for d in prob.devices if d.stratum == s][-2:]
        for d in last_two:
            _check(problems, last.arms[d.cluster_id] == A,
                   f"末尾 rank：层 {s} 末尾设备 {d.device_id} 应在 A")
    print(f"  rank Z-1 = {Z - 1} → 解码成功（每层最后两个组进 A，"
          f"即字典序最后）；rank 越界 Z 被拒绝:")
    try:
        sampler.decode(prob, tables, Z)
        problems.append("rank=Z 应被拒绝却成功")
    except sampler.RankOutOfRange:
        print(f"    rank={Z} 被拒绝（合法范围 [0, {Z})）")
    # 抽签不经浮点：直接用大整数 randrange；核对 rank 分布可访问任意尾部
    o = sampler.sample(prob, tables, 12345)
    _check(problems, 0 <= o.rank < Z, "大 Z 抽签 rank 越界")
    _check(problems, not check_outcome(prob, red, tables, o), "大 Z 抽签方案非法")
    print(f"  seed 12345 → rank={o.rank}（大整数，未转 float）")
    # 命令行：rank 模式访问 Z-1，与库内一致
    r = run_cli(["run", "--devices", str(m["devices"]), "--design",
                 str(m["design"]), "--rank", str(Z - 1)])
    _check(problems, r.returncode == 0, f"大 Z rank 命令失败: {r.stderr[:200]}")
    _check(problems, f"结果 rank = {Z - 1}" in r.stdout, "命令输出 rank 不符")
    print("  命令行 --rank Z-1 成功，结果 rank 精确打印")


def demo_seed_distribution(problems: List[str], n_seeds: int) -> None:
    print("\n== 4. 固定小规模 + 大量种子的分布检查（均匀性推导的补充） ==")
    n_seeds = max(n_seeds, 300)   # 少量种子不足以观察频数，固定下限 300
    # 3 个单设备组，target_A=1 → 3 个方案，各应约 1/3
    rows = [DeviceRow(f"d{i}", "S", f"c{i}", i + 2) for i in range(1, 4)]
    prob = prepare(rows, Design(targets={"S": 1}, locks=[]))
    red = reduce_locks(prob)
    tables = build_tables(prob, red, 1000)
    _check(problems, tables.Z == 3, f"温和例子应有 3 个方案，实际 {tables.Z}")
    counts = Counter()
    for seed in range(n_seeds):
        o = sampler.sample(prob, tables, seed)
        counts[sampler.encode_rank(prob, tables, o.arms)] += 1
        for p in check_outcome(prob, red, tables, o) + check_trace(prob, red, tables, o):
            problems.append(f"seed={seed} 核对失败: {p}")
    print(f"  Z=3，{n_seeds} 个 seed 的 rank 频数：{dict(sorted(counts.items()))}")
    expect = n_seeds / 3
    worst = max(abs(counts.get(r, 0) - expect) / expect for r in range(3))
    print(f"  最大相对偏差 {worst:.1%}（仅作补充；每个方案等概率由推导保证）")
    # 抽签与「抽 rank 再解码」完全同分布：核对每个 seed 的 rank 可复现
    for seed in (0, 7, n_seeds - 1):
        a = sampler.sample(prob, tables, seed)
        b = sampler.sample(prob, tables, seed)
        _check(problems, a.arms == b.arms and a.rank == b.rank,
               f"seed={seed} 跨次抽取不可复现")


def demo_edge_cases(edges: Dict[str, dict], problems: List[str], workdir: Path):
    print("\n== 5. 边界：空名单、全锁定、零/全额配额、重复、非法 rank、预算 ==")
    cases = edges

    # 空名单 → Z=1（空积），唯一方案是空标记
    prob, _ = _load_problem(cases["empty"]["devices"], cases["empty"]["design"])
    red = reduce_locks(prob)
    tables = build_tables(prob, red, 100)
    _check(problems, tables.Z == 1 and not prob.devices,
           f"空名单应 Z=1，实际 Z={tables.Z}")
    print(f"  空名单: Z={tables.Z}（空积），设备 0 台")
    r = run_cli(["run", "--devices", str(cases["empty"]["devices"]),
                 "--design", str(cases["empty"]["design"])])
    _check(problems, r.returncode == 0, f"空名单命令失败: {r.stderr[:200]}")
    print(f"  空名单命令 exit={r.returncode}")

    # 空名单 + 未知层 → 拒绝
    r = run_cli(["run", "--devices", str(cases["empty_unknown_layer"]["devices"]),
                 "--design", str(cases["empty_unknown_layer"]["design"])])
    _check(problems, r.returncode == EXIT_INPUT and "未知层" in r.stderr,
           "空名单的未知层应被拒绝并指出")
    print(f"  空名单+未知层: exit={r.returncode}（已指出未知层）")

    # 全锁定 → Z=1，无自由组
    prob, _ = _load_problem(cases["all_locked"]["devices"], cases["all_locked"]["design"])
    red = reduce_locks(prob)
    tables = build_tables(prob, red, 100)
    o = sampler.decode(prob, tables, 0)
    _check(problems, tables.Z == 1, f"全锁定应 Z=1，实际 {tables.Z}")
    _check(problems, not check_outcome(prob, red, tables, o), "全锁定方案非法")
    _check(problems, all(s.locked for s in o.trace), "全锁定轨迹应全部标记锁定")
    print(f"  全锁定: Z={tables.Z}，轨迹 {len(o.trace)} 步全部锁定")

    # 零配额 / 全额配额：各唯一方案
    for name in ("zero_quota", "full_quota"):
        prob, _ = _load_problem(cases[name]["devices"], cases[name]["design"])
        red = reduce_locks(prob)
        tables = build_tables(prob, red, 100)
        _check(problems, tables.Z == 1, f"{name} 应 Z=1，实际 {tables.Z}")
        print(f"  {name}: Z={tables.Z}")

    # 重复设备 → 拒绝且指出行号
    r = run_cli(["run", "--devices", str(cases["duplicate"]["devices"]),
                 "--design", str(cases["duplicate"]["design"])])
    _check(problems, r.returncode == EXIT_INPUT and "重复" in r.stderr,
           "'重复设备' 应被拒绝并指出")
    _check(problems, "第 3 行" in r.stderr, "重复设备应指出物理行号")
    print(f"  重复设备: exit={r.returncode}（指出第 3 行重复）")

    # 文本身份：001 与 1 是两台不同设备 → 各 1 台、同层、target_A=1 → Z=2
    prob, _ = _load_problem(cases["text_ids"]["devices"], cases["text_ids"]["design"])
    red = reduce_locks(prob)
    tables = build_tables(prob, red, 100)
    _check(problems, len(prob.devices) == 2 and tables.Z == 2,
           "001 与 1 应按文本视为两台设备")
    # 对照：若按数字归一，二者会合并成 2 台同簇设备，只能整簇同方案 → Z=0
    merged_rows = [DeviceRow("001", "S", "c1", 2), DeviceRow("1", "S", "c1", 3)]
    merged = prepare(merged_rows, Design(targets={"S": 1}, locks=[]))
    merged_red = reduce_locks(merged)
    _check(problems, build_tables(merged, merged_red, 100).Z == 0,
           "对照：若把 001 与 1 合并成一簇则应为无解")
    print(f"  文本身份: device 数 {len(prob.devices)}，Z={tables.Z}"
          "（001≠1；若错误合并则 Z=0）")

    # 锁定冲突 → 拒绝
    r = run_cli(["run", "--devices", str(cases["lock_conflict"]["devices"]),
                 "--design", str(cases["lock_conflict"]["design"])])
    _check(problems, r.returncode == EXIT_INPUT and "同时锁定" in r.stderr,
           "锁定冲突应被拒绝")
    print(f"  锁定冲突: exit={r.returncode}（指出同时锁定 A 与 B）")

    # 锁定使某层超额 → 无解并说明
    r = run_cli(["run", "--devices", str(cases["lock_over"]["devices"]),
                 "--design", str(cases["lock_over"]["design"])])
    _check(problems, r.returncode == EXIT_UNSOLVABLE, "锁定超额应报无解")
    _check(problems, "超过 target_A" in r.stderr, "锁定超额应说明该层已超额")
    print(f"  锁定超额: exit={r.returncode}（说明层 S 锁定贡献超过 target_A）")

    # 非法 rank：-1 与 Z 都要拒绝
    prob, _ = _load_problem(cases["zero_quota"]["devices"], cases["zero_quota"]["design"])
    red = reduce_locks(prob)
    tables = build_tables(prob, red, 100)
    for bad in (-1, tables.Z, tables.Z + 10):
        r = run_cli(["run", "--devices", str(cases["zero_quota"]["devices"]),
                     "--design", str(cases["zero_quota"]["design"]),
                     "--rank", str(bad)])
        _check(problems, r.returncode == EXIT_INPUT and "越界" in r.stderr,
               f"非法 rank {bad} 应被拒绝")
    print("  非法 rank: -1、Z、Z+10 均被拒绝")

    # 预算：刚好足够 / 少一个状态
    rows = [DeviceRow(f"d{i}", "S", f"c{i}", i + 2) for i in range(1, 5)]
    prob = prepare(rows, Design(targets={"S": 2}, locks=[]))
    red = reduce_locks(prob)
    need = build_tables(prob, red, 1_000_000).n_states
    t_exact = build_tables(prob, red, need)
    _check(problems, t_exact.Z > 0, "预算刚好足够时应成功")
    try:
        build_tables(prob, red, need - 1)
        problems.append("预算少一个状态时应抛 StateBudgetExhausted")
    except StateBudgetExhausted:
        pass
    print(f"  预算: 实际状态 {need}；max_states={need} 成功"
          f"（提示接近预算），max_states={need - 1} 报『未完成精确求解』")
    _check(problems, t_exact.nearly_exhausted and t_exact.budget_warning,
           "刚好等于预算时应给出接近预算的提示")
    # 命令行预算耗尽：exit=3，且不写文件
    tmp_out = workdir / "budget_out"
    tmp_out.mkdir(exist_ok=True)
    keep = tmp_out / out_mod.DEVICES_NAME
    keep.write_text("SENTINEL\n", encoding="utf-8")
    r = run_cli(["run", "--devices", str(_write_tmp_csv(workdir, "budget.csv", rows)),
                 "--design", str(_write_tmp_design(workdir, "budget.json", {"S": 2})),
                 "--max-states", str(need - 1), "--out-dir", str(tmp_out)])
    _check(problems, r.returncode == EXIT_BUDGET, f"预算耗尽退出码应为 3，实际 {r.returncode}")
    _check(problems, "未完成精确求解" in r.stderr, "预算耗尽应报未完成精确求解")
    _check(problems, keep.read_text(encoding="utf-8") == "SENTINEL\n",
           "预算耗尽不得覆盖已有输出文件")
    print(f"  预算耗尽命令 exit={r.returncode}，既有文件保持不变")


def _write_tmp_csv(work: Path, name: str, rows: List[DeviceRow]) -> Path:
    p = work / name
    _write_csv(p, DEVICE_HEADER, [[r.device_id, r.stratum, r.cluster_id] for r in rows])
    return p


def _write_tmp_design(work: Path, name: str, targets: Dict[str, int]) -> Path:
    p = work / name
    _write_design(p, targets)
    return p


def demo_file_safety(workdir: Path, problems: List[str]) -> None:
    print("\n== 6. 失败不覆盖既有文件、行序无关、跨进程 rank 回放 ==")
    safe = workdir / "safety"
    safe.mkdir(exist_ok=True)
    keep = safe / out_mod.DEVICES_NAME
    keep.write_text("ORIGINAL-CONTENT\n", encoding="utf-8")
    # 无解
    mats = workdir / "cross_layer_infeasible"
    r = run_cli(["run", "--devices", str(mats / "devices.csv"),
                 "--design", str(mats / "design.json"), "--out-dir", str(safe)])
    _check(problems, r.returncode == EXIT_UNSOLVABLE, "无解应 exit=1")
    _check(problems, keep.read_text(encoding="utf-8") == "ORIGINAL-CONTENT\n",
           "无解不得覆盖已有输出文件")
    # 输入非法
    r = run_cli(["run", "--devices", str(workdir / "edges" / "duplicate_devices.csv"),
                 "--design", str(workdir / "edges" / "duplicate_design.json"),
                 "--out-dir", str(safe)])
    _check(problems, r.returncode == EXIT_INPUT, "输入非法应 exit=2")
    _check(problems, keep.read_text(encoding="utf-8") == "ORIGINAL-CONTENT\n",
           "输入非法不得覆盖已有输出文件")
    # rank 越界
    r = run_cli(["run", "--devices", str(workdir / "locked_multi" / "devices.csv"),
                 "--design", str(workdir / "locked_multi" / "design.json"),
                 "--rank", "999999", "--out-dir", str(safe)])
    _check(problems, r.returncode == EXIT_INPUT, "rank 越界应 exit=2")
    _check(problems, keep.read_text(encoding="utf-8") == "ORIGINAL-CONTENT\n",
           "rank 越界不得覆盖已有输出文件")
    print("  无解 / 输入非法 / rank 越界：既有 allocation.csv 内容均保持原样")

    # 行序无关：把 locked_multi 的 CSV 行顺序打乱
    src = (workdir / "locked_multi" / "devices.csv").read_text(encoding="utf-8")
    lines = src.strip().splitlines()
    shuffled = safe / "locked_multi_shuffled.csv"
    shuffled.write_text("\n".join([lines[0]] + list(reversed(lines[1:]))) + "\n",
                        encoding="utf-8")
    out1 = safe / "order1"
    out2 = safe / "order2"
    r1 = run_cli(["run", "--devices", str(workdir / "locked_multi" / "devices.csv"),
                  "--design", str(workdir / "locked_multi" / "design.json"),
                  "--seed", "42", "--out-dir", str(out1)])
    r2 = run_cli(["run", "--devices", str(shuffled),
                  "--design", str(workdir / "locked_multi" / "design.json"),
                  "--seed", "42", "--out-dir", str(out2)])
    _check(problems, r1.returncode == 0 and r2.returncode == 0,
           "行序无关检查命令失败")
    same = (out1 / out_mod.DEVICES_NAME).read_text(encoding="utf-8") == \
        (out2 / out_mod.DEVICES_NAME).read_text(encoding="utf-8")
    _check(problems, same, "调换 CSV 行序后设备名单应完全相同")
    s1 = (out1 / out_mod.SUMMARY_NAME).read_text(encoding="utf-8")
    s2 = (out2 / out_mod.SUMMARY_NAME).read_text(encoding="utf-8")
    # 摘要里两个输入路径不同，其余必须一致
    strip = lambda t: "\n".join(l for l in t.splitlines()
                                 if not l.startswith(("模式 =",)))
    _check(problems, strip(s1) == strip(s2), "调换行序后摘要（除输入路径外）应相同")
    print("  调换 CSV 行序 + 相同 seed：设备名单与摘要一致")

    # 跨进程 rank 回放：另一个进程只读 design.normalized.json + rank
    r = run_cli(["run", "--devices", str(workdir / "locked_multi" / "devices.csv"),
                 "--design", str(workdir / "locked_multi" / "design.json"),
                 "--rank", "7", "--out-dir", str(safe / "rank7")])
    _check(problems, r.returncode == 0, "rank 模式命令失败")
    norm = safe / "rank7" / out_mod.DESIGN_NAME
    r = run_cli(["replay", "--normalized", str(norm), "--rank", "7",
                 "--devices-csv", str(workdir / "locked_multi" / "devices.csv")])
    _check(problems, r.returncode == 0, f"replay 失败: {r.stderr[:300]}")
    _check(problems, "按规范化设计 + rank 复算" in r.stdout, "replay 未输出复算说明")
    # 回放结果与原 rank 模式设备名单逐行一致
    replay_arms = {}
    for line in r.stdout.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) == 4:
            replay_arms[parts[0]] = parts[3]
    orig = {}
    with open(safe / "rank7" / out_mod.DEVICES_NAME, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            orig[row["device_id"]] = row["arm"]
    _check(problems, replay_arms == orig,
           f"replay 结果与原 rank 模式不一致: {replay_arms} vs {orig}")
    print(f"  新进程 replay rank=7：{len(replay_arms)} 台设备方案与原结果逐行一致")


def demo_cli_review(workdir: Path, problems: List[str]) -> None:
    print("\n== 7. 可审阅输出：Z、seed、规范排序、锁定、逐组、逐层、轨迹 ==")
    out = workdir / "review_out"
    m = workdir / "locked_multi"
    r = run_cli(["run", "--devices", str(m / "devices.csv"),
                 "--design", str(m / "design.json"), "--seed", "7",
                 "--out-dir", str(out)])
    _check(problems, r.returncode == 0, f"审阅输出命令失败: {r.stderr[:200]}")
    for needle in ("方案总数 Z = 15", "seed = 7", "结果 rank =", "规范排序",
                   "锁定项", "每层 A/B 台数", "逐设备名单", "抽签轨迹",
                   "A完成数", "B完成数"):
        _check(problems, needle in r.stdout, f"报告缺少 {needle!r}")
    for name in (out_mod.SUMMARY_NAME, out_mod.DEVICES_NAME, out_mod.TRACE_NAME,
                 out_mod.DESIGN_NAME, out_mod.RUN_NAME):
        _check(problems, (out / name).exists(), f"缺少输出文件 {name}")
    run_doc = json.loads((out / out_mod.RUN_NAME).read_text(encoding="utf-8"))
    _check(problems, run_doc["seed"] == 7 and "rank" in run_doc,
           "run.json 应记录 seed 与 rank")
    trace_rows = (out / out_mod.TRACE_NAME).read_text(encoding="utf-8").strip().splitlines()
    _check(problems, len(trace_rows) - 1 == 9,  # 9 个 cluster
           f"trace.csv 应有 9 步，实际 {len(trace_rows) - 1}")
    print(f"  报告含 Z/seed/规范排序/锁定/逐层计数/逐设备/逐簇方案/轨迹；"
          f"输出 {len(list(out.iterdir()))} 个文件")


def demo_input_rejections(workdir: Path, problems: List[str]) -> None:
    print("\n== 8. 输入拒绝：未知层、缺配额、配额越界、未知锁定、错误方案名 ==")
    d = workdir / "edges"
    base_dev = d / "zero_quota_devices.csv"   # 两台设备，层 S

    def expect(name, devices, design, needle, make_design=None):
        p = d / f"rej_{name}.json"
        if make_design is not None:
            p.write_text(json.dumps(make_design, ensure_ascii=False), encoding="utf-8")
            design_path = p
        else:
            design_path = design
        r = run_cli(["run", "--devices", str(devices), "--design", str(design_path)])
        _check(problems, r.returncode == EXIT_INPUT and needle in r.stderr,
               f"{name} 应被拒绝并含 {needle!r}（exit={r.returncode}）")
        print(f"  {name}: exit={r.returncode} ✓")

    expect("unknown_layer", base_dev, None, "未知层", {"target_A": {"S": 0, "X": 1}})
    expect("missing_quota", base_dev, None, "缺少层", {"target_A": {}})
    expect("quota_over", base_dev, None, "越界", {"target_A": {"S": 3}})
    expect("quota_negative", base_dev, None, "负", {"target_A": {"S": -1}})
    expect("unknown_lock", base_dev, None, "未知 cluster",
           {"target_A": {"S": 0}, "locks": [{"cluster_id": "nope", "arm": "A"}]})
    expect("bad_arm", base_dev, None, "方案名",
           {"target_A": {"S": 0}, "locks": [{"cluster_id": "cx1", "arm": "C"}]})
    expect("missing_target", base_dev, None, "缺少 target_A", {"locks": []})
    # 配额字段非整数
    expect("float_quota", base_dev, None, "整数", {"target_A": {"S": 0.5}})


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def run_demo(workdir: Path, seeds: int = 20) -> int:
    workdir = Path(workdir)
    mats = build_materials(workdir)
    problems: List[str] = []

    print("=" * 78)
    print("实验分配演示（整簇随机化；只产生实验名单，不改观察日志）")
    print("=" * 78)
    print(f"材料目录: {workdir}")

    demo_cross_layer(mats["cross_layer"], problems)
    demo_locked_multi(mats["locked_multi"], problems)
    demo_huge_z(mats["huge_z"], problems)
    demo_seed_distribution(problems, seeds)
    demo_edge_cases(mats["edges"], problems, workdir)
    demo_file_safety(workdir, problems)
    demo_cli_review(workdir, problems)
    demo_input_rejections(workdir, problems)

    print("\n" + "=" * 78)
    if problems:
        print(f"核对失败：{len(problems)} 项", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1
    print("全部核对通过：精确计数、无偏抽签、rank↔方案、无解/预算失败语义、")
    print("文件原子性、行序无关与跨进程 rank 回放均符合约定。")
    print("=" * 78)
    return EXIT_OK
