"""实验分配（整簇随机化）专项回归。

覆盖：
1. 输入校验与行号定位（重复设备、空字段、表头、未知层/缺配额/越界/锁定）；
2. 精确计数：正向 DP vs 后缀 DP、枚举程序 vs Z、多解逐 rank 不重不漏；
3. 跨层「逐层够、合起来无解」；锁定超额说明；
4. Z > 2^53 的末尾 rank 精确访问、rank 越界、抽签大整数；
5. 抽签等价于均匀 rank（大量 seed 的频数与推导一致，作补充证据）；
6. 空名单、全锁定、零/全额配额、001≠1；
7. 预算刚好足够 / 少一个状态；无解与预算耗尽不覆盖既有文件；
8. 调换 CSV 行序结果不变；新进程 replay 与 run --rank 一致。

期望值均由本文件用 ``math.comb`` 等独立手算或独立的
``allocation.verify.enumerate_feasible`` 显式枚举给出，不依赖生产抽签。
"""

from __future__ import annotations

import csv
import json
import math
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from allocation import sampler
from allocation.count import (StateBudgetExhausted, build_tables,
                              per_layer_reachability)
from allocation.inputs import (AllocationInputError, Design, DeviceRow,
                               read_design_json, read_device_csv)
from allocation.model import A, B, prepare, reduce_locks
from allocation.verify import (check_outcome, check_trace, count_forward,
                               enumerate_feasible)

REPO_ROOT = Path(__file__).resolve().parent.parent
HEADER = ["device_id", "stratum", "cluster_id"]


def run_cli(args, cwd=REPO_ROOT):
    return subprocess.run([sys.executable, "-m", "allocation.cli", *args],
                          cwd=cwd, text=True, capture_output=True)


def write_csv(path: Path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)


def write_design(path: Path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def build(rows, targets, locks=()):
    prob = prepare([DeviceRow(r[0], r[1], r[2], i + 2) for i, r in enumerate(rows)],
                   Design(targets=dict(targets),
                          locks=[__import__("allocation.inputs", fromlist=["LockSpec"])
                                 .LockSpec(c, a, None, f"locks[{i}]")
                                 for i, (c, a) in enumerate(locks)]))
    red = reduce_locks(prob)
    return prob, red


def tables_of(rows, targets, locks=(), budget=1_000_000):
    prob, red = build(rows, targets, locks)
    assert not red.failures, red.failures
    return prob, red, build_tables(prob, red, budget)


# ----------------------------------------------------------------------
# 1. 输入
# ----------------------------------------------------------------------

def test_duplicate_device_reports_line(tmp_path):
    p = tmp_path / "d.csv"
    write_csv(p, [["x1", "S", "c1"], ["x1", "S", "c2"]])
    with pytest.raises(AllocationInputError) as ei:
        read_device_csv(p)
    text = str(ei.value)
    assert "第 3 行" in text and "重复" in text


def test_text_ids_are_not_numeric(tmp_path):
    p = tmp_path / "d.csv"
    write_csv(p, [["001", "S", "c1"], ["1", "S", "c2"]])
    rows, _ = read_device_csv(p)
    assert [r.device_id for r in rows] == ["001", "1"]


def test_empty_file_is_empty_list(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("", encoding="utf-8")
    rows, _ = read_device_csv(p)
    assert rows == []


def test_design_unknown_layer_missing_quota_overrange(tmp_path):
    dev = tmp_path / "d.csv"
    write_csv(dev, [["a", "S", "c1"], ["b", "S", "c2"]])
    base = {"S": 1}
    # 未知层
    des = tmp_path / "x.json"
    write_design(des, {"target_A": {"S": 1, "Z": 0}})
    with pytest.raises(AllocationInputError) as ei:
        prepare(read_device_csv(dev)[0], read_design_json(des))
    assert "未知层" in str(ei.value)
    # 缺配额
    write_design(des, {"target_A": {}})
    with pytest.raises(AllocationInputError) as ei:
        prepare(read_device_csv(dev)[0], read_design_json(des))
    assert "缺少层" in str(ei.value)
    # 越界
    write_design(des, {"target_A": {"S": 3}})
    with pytest.raises(AllocationInputError) as ei:
        prepare(read_device_csv(dev)[0], read_design_json(des))
    assert "越界" in str(ei.value)
    # 负配额
    write_design(des, {"target_A": {"S": -1}})
    with pytest.raises(AllocationInputError):
        prepare(read_device_csv(dev)[0], read_design_json(des))


def test_design_lock_errors(tmp_path):
    dev = tmp_path / "d.csv"
    write_csv(dev, [["a", "S", "c1"]])
    des = tmp_path / "x.json"
    write_design(des, {"target_A": {"S": 1},
                       "locks": [{"cluster_id": "nope", "arm": "A"}]})
    with pytest.raises(AllocationInputError) as ei:
        prepare(read_device_csv(dev)[0], read_design_json(des))
    assert "未知 cluster" in str(ei.value)
    write_design(des, {"target_A": {"S": 1},
                       "locks": [{"cluster_id": "c1", "arm": "C"}]})
    with pytest.raises(AllocationInputError) as ei:
        read_design_json(des)
    assert "方案名" in str(ei.value)
    write_design(des, {"target_A": {"S": 1},
                       "locks": [{"cluster_id": "c1", "arm": "A"},
                                 {"cluster_id": "c1", "arm": "B"}]})
    with pytest.raises(AllocationInputError) as ei:
        read_design_json(des)
    assert "同时锁定" in str(ei.value)


# ----------------------------------------------------------------------
# 2. 计数与逐 rank
# ----------------------------------------------------------------------

def test_locked_multi_all_ranks_bijective():
    rows = [[f"a{i}", "A", f"ga{i}"] for i in range(1, 7)]
    rows += [[f"b{i}", "B", f"gb{i}"] for i in range(1, 4)]
    prob, red, tables = tables_of(rows, {"A": 4, "B": 2}, [("gb3", "B")])
    assert tables.Z == math.comb(6, 4) * math.comb(2, 2) == 15
    assert count_forward(prob, red) == tables.Z
    feasible = enumerate_feasible(prob)
    assert len(feasible) == 15 and len(set(feasible)) == 15
    decoded = []
    for rank in range(tables.Z):
        o = sampler.decode(prob, tables, rank)
        assert not check_outcome(prob, red, tables, o)
        assert not check_trace(prob, red, tables, o)
        assert sampler.encode_rank(prob, tables, o.arms) == rank
        decoded.append(tuple(o.arms[c] for c in prob.clusters))
    assert decoded == feasible
    assert len(set(decoded)) == len(decoded)


def test_cross_layer_infeasible_but_each_layer_ok():
    # L1: g1×1 + g2×2 ; L2: g1×2 + g3×1 ; 两层各要 1 台进 A
    rows = [["l11", "L1", "g1"], ["l12", "L1", "g2"], ["l13", "L1", "g2"],
            ["l21", "L2", "g1"], ["l22", "L2", "g1"], ["l23", "L2", "g3"]]
    prob, red = build(rows, {"L1": 1, "L2": 1})
    tables = build_tables(prob, red, 10_000)
    assert tables.Z == 0
    reach = per_layer_reachability(prob, red)
    assert 1 in reach[0] and 1 in reach[1]          # 逐层单独都可达
    assert enumerate_feasible(prob) == []           # 合起来一个都没有


def test_lock_over_quota_explains_layer():
    rows = [["a", "S", "c1"], ["b", "S", "c2"]]
    prob, red = build(rows, {"S": 0}, [("c1", "A")])
    assert red.failures and "超过 target_A" in red.failures[0]
    assert red.lock_contrib[0] == 1


def test_zero_and_full_quota_unique():
    for target, arm in ((0, B), (2, A)):
        rows = [["a", "S", "c1"], ["b", "S", "c2"]]
        prob, red, tables = tables_of(rows, {"S": target})
        assert tables.Z == 1
        o = sampler.decode(prob, tables, 0)
        assert set(o.arms.values()) == {arm}


def test_all_locked_unique_and_trace_marks_lock():
    rows = [["x", "S", "cx"], ["y", "T", "cy"]]
    prob, red, tables = tables_of(rows, {"S": 1, "T": 0},
                                  [("cx", "A"), ("cy", "B")])
    assert tables.Z == 1
    o = sampler.decode(prob, tables, 0)
    assert all(s.locked for s in o.trace)
    assert not check_outcome(prob, red, tables, o)


def test_empty_list_single_empty_scheme(tmp_path):
    p = tmp_path / "d.csv"
    write_csv(p, [])
    des = tmp_path / "x.json"
    write_design(des, {"target_A": {}})
    prob = prepare(read_device_csv(p)[0], read_design_json(des))
    red = reduce_locks(prob)
    tables = build_tables(prob, red, 10)
    assert tables.Z == 1 and prob.devices == ()


# ----------------------------------------------------------------------
# 3. 大 Z
# ----------------------------------------------------------------------

def _huge():
    tiers = [f"T{i}" for i in range(1, 7)]
    rows = [[f"{s}-{k:02d}", s, f"{s}-c{k:03d}"] for s in tiers
            for k in range(1, 33)]
    return rows, {s: 2 for s in tiers}, math.comb(32, 2) ** 6, tiers


def test_huge_z_last_rank_exact():
    rows, targets, z, tiers = _huge()
    assert z == 496 ** 6 == 14_889_840_956_932_096
    assert z > 2 ** 53
    prob, red, tables = tables_of(rows, targets)
    assert tables.Z == z
    assert count_forward(prob, red) == z
    last = sampler.decode(prob, tables, z - 1)
    assert last.rank == z - 1
    assert not check_outcome(prob, red, tables, last)
    with pytest.raises(sampler.RankOutOfRange):
        sampler.decode(prob, tables, z)
    o = sampler.sample(prob, tables, 20261113)
    assert 0 <= o.rank < z
    assert not check_outcome(prob, red, tables, o)


# ----------------------------------------------------------------------
# 4. 预算
# ----------------------------------------------------------------------

def test_budget_exact_and_one_short():
    rows = [[f"d{i}", "S", f"c{i}"] for i in range(1, 5)]
    prob, red = build(rows, {"S": 2})
    need = build_tables(prob, red, 1_000_000).n_states
    assert build_tables(prob, red, need).Z > 0
    with pytest.raises(StateBudgetExhausted):
        build_tables(prob, red, need - 1)


def test_budget_does_not_touch_existing_output(tmp_path):
    rows = [[f"d{i}", "S", f"c{i}"] for i in range(1, 5)]
    dev = tmp_path / "d.csv"
    write_csv(dev, rows)
    des = tmp_path / "x.json"
    write_design(des, {"target_A": {"S": 2}})
    prob, red = build(rows, {"S": 2})
    need = build_tables(prob, red, 1_000_000).n_states
    out = tmp_path / "out"
    out.mkdir()
    keep = out / "allocation.csv"
    keep.write_text("ORIGINAL\n", encoding="utf-8")
    r = run_cli(["run", "--devices", str(dev), "--design", str(des),
                 "--max-states", str(need - 1), "--out-dir", str(out)])
    assert r.returncode == 3
    assert "未完成精确求解" in r.stderr
    assert keep.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_unsolvable_does_not_touch_existing_output(tmp_path):
    rows = [["l11", "L1", "g1"], ["l12", "L1", "g2"], ["l13", "L1", "g2"],
            ["l21", "L2", "g1"], ["l22", "L2", "g1"], ["l23", "L2", "g3"]]
    dev = tmp_path / "d.csv"
    write_csv(dev, rows)
    des = tmp_path / "x.json"
    write_design(des, {"target_A": {"L1": 1, "L2": 1}})
    out = tmp_path / "out"
    out.mkdir()
    keep = out / "allocation.csv"
    keep.write_text("ORIGINAL\n", encoding="utf-8")
    r = run_cli(["run", "--devices", str(dev), "--design", str(des),
                 "--out-dir", str(out)])
    assert r.returncode == 1
    assert "约束无解" in r.stderr and "跨层" in r.stderr
    assert keep.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_invalid_rank_rejected_and_no_write(tmp_path):
    dev = tmp_path / "d.csv"
    write_csv(dev, [["a", "S", "c1"], ["b", "S", "c2"]])
    des = tmp_path / "x.json"
    write_design(des, {"target_A": {"S": 1}})
    out = tmp_path / "out"
    out.mkdir()
    keep = out / "allocation.csv"
    keep.write_text("ORIGINAL\n", encoding="utf-8")
    for bad in ("-1", "2", "99"):
        r = run_cli(["run", "--devices", str(dev), "--design", str(des),
                     "--rank", bad, "--out-dir", str(out)])
        assert r.returncode == 2 and "越界" in r.stderr
        assert keep.read_text(encoding="utf-8") == "ORIGINAL\n"


# ----------------------------------------------------------------------
# 5. 复现性与跨进程回放
# ----------------------------------------------------------------------

def test_row_order_irrelevant(tmp_path):
    rows = [["a1", "A", "ga1"], ["a2", "A", "ga2"], ["a3", "A", "ga3"],
            ["a4", "A", "ga4"], ["b1", "B", "gb1"], ["b2", "B", "gb2"]]
    d1 = tmp_path / "d1.csv"
    d2 = tmp_path / "d2.csv"
    write_csv(d1, rows)
    write_csv(d2, list(reversed(rows)))
    des = tmp_path / "x.json"
    write_design(des, {"target_A": {"A": 2, "B": 1}})
    out1, out2 = tmp_path / "o1", tmp_path / "o2"
    for dev, out in ((d1, out1), (d2, out2)):
        r = run_cli(["run", "--devices", str(dev), "--design", str(des),
                     "--seed", "9", "--out-dir", str(out)])
        assert r.returncode == 0, r.stderr
    assert (out1 / "allocation.csv").read_text(encoding="utf-8") == \
        (out2 / "allocation.csv").read_text(encoding="utf-8")
    j1 = json.loads((out1 / "run.json").read_text(encoding="utf-8"))
    j2 = json.loads((out2 / "run.json").read_text(encoding="utf-8"))
    assert j1["rank"] == j2["rank"] and j1["Z"] == j2["Z"]


def test_replay_matches_rank_mode(tmp_path):
    rows = [["a1", "A", "ga1"], ["a2", "A", "ga2"], ["a3", "A", "gb"],
            ["b1", "B", "gb"], ["b2", "B", "gb2"]]
    dev = tmp_path / "d.csv"
    write_csv(dev, rows)
    des = tmp_path / "x.json"
    write_design(des, {"target_A": {"A": 1, "B": 1},
                       "locks": [{"cluster_id": "gb", "arm": "B"}]})
    out = tmp_path / "out"
    r = run_cli(["run", "--devices", str(dev), "--design", str(des),
                 "--rank", "0", "--out-dir", str(out)])
    assert r.returncode == 0, r.stderr
    norm = out / "design.normalized.json"
    doc = json.loads(norm.read_text(encoding="utf-8"))
    assert doc["Z"] > 0 and doc["rank_order"].startswith("cluster order")
    r2 = run_cli(["replay", "--normalized", str(norm), "--rank", "0",
                  "--devices-csv", str(dev)])
    assert r2.returncode == 0, r2.stderr
    replay = {}
    for line in r2.stdout.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) == 4:
            replay[parts[0]] = parts[3]
    orig = {}
    with open(out / "allocation.csv", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            orig[row["device_id"]] = row["arm"]
    assert replay == orig


def test_replay_rejects_out_of_range(tmp_path):
    rows = [["a", "S", "c1"], ["b", "S", "c2"]]
    dev = tmp_path / "d.csv"
    write_csv(dev, rows)
    des = tmp_path / "x.json"
    write_design(des, {"target_A": {"S": 1}})
    out = tmp_path / "out"
    assert run_cli(["run", "--devices", str(dev), "--design", str(des),
                    "--out-dir", str(out)]).returncode == 0
    r = run_cli(["replay", "--normalized",
                 str(out / "design.normalized.json"), "--rank", "2"])
    assert r.returncode == 2 and "越界" in r.stderr


# ----------------------------------------------------------------------
# 6. 均匀性（补充证据）
# ----------------------------------------------------------------------

def test_sample_equals_uniform_rank_and_reproducible():
    rows = [[f"d{i}", "S", f"c{i}"] for i in range(1, 4)]
    prob, red, tables = tables_of(rows, {"S": 1})
    assert tables.Z == 3
    counts = Counter()
    for seed in range(300):
        o = sampler.sample(prob, tables, seed)
        assert o.arms == sampler.sample(prob, tables, seed).arms
        counts[o.rank] += 1
    assert set(counts) == {0, 1, 2}
    # 每个 rank 的频数应接近 100；只作补充，不替代推导
    for rank, n in counts.items():
        assert abs(n - 100) < 35, (rank, n)
