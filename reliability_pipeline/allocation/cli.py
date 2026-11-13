"""``python3 -m allocation.cli``：整簇随机化的实验分配命令。

子命令
------
- ``run``     读取设备名单 CSV 与设计 JSON，精确计数后抽签或按 rank 取方案。
              默认打印完整可审阅报告；``--out-dir`` 时额外原子写出
              ``summary.txt / allocation.csv / trace.csv /
              design.normalized.json / run.json``。
- ``replay``  只用规范化设计（``design.normalized.json``）与 rank 复算方案，
              不读取 CSV、不重放算法内部状态；用于「另一进程按 rank 回放」。
- ``demo``    一条命令跑通全部演示与回归核对（含独立枚举程序逐 rank 核对）。

退出码
------
- 0 成功；
- 2 输入/设计/rank 非法（已指出输入位置；未写任何文件）；
- 1 约束无解（Z=0；绝不松配额、拆组或近似分配；未写任何文件）；
- 3 ``max_states`` 预算耗尽——**未完成精确求解**（不是无解；未写任何文件）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import demo as demo_mod, outputs as out_mod, sampler
from .count import StateBudgetExhausted, build_tables
from .inputs import AllocationInputError, read_design_json, read_device_csv
from .model import prepare, reduce_locks
from .sampler import RankOutOfRange
from .verify import check_outcome, check_trace

EXIT_OK = 0
EXIT_UNSOLVABLE = 1
EXIT_INPUT = 2
EXIT_BUDGET = 3


def _load_problem(devices_path: str, design_path: str):
    rows, warnings = read_device_csv(devices_path)
    design = read_design_json(design_path)
    prob = prepare(rows, design)          # 未知层/缺配额/越界/未知锁定 → 抛错
    return prob, warnings


def _build(prob, max_states: int):
    red = reduce_locks(prob)
    if red.failures:
        return red, None, red.failures
    tables = build_tables(prob, red, max_states)
    return red, tables, []


def _report_unsolvable(prob, red, extra: List[str]) -> None:
    print("约束无解（Z = 0）：不存在同时满足锁定、整簇不可拆与逐层精确配额的方案。",
          file=sys.stderr)
    print("不会放宽配额、不会拆开任何 cluster、也不会给出近似分配。", file=sys.stderr)
    for msg in list(red.failures) + extra:
        print(f"  ✗ {msg}", file=sys.stderr)
    if not red.failures:
        print("  锁定合并后的剩余问题：", file=sys.stderr)
        for i, s in enumerate(prob.strata):
            print(f"    层 {s!r}: 需 {red.residual[i]} 台进入 A，"
                  f"未锁定设备 {red.residual_devices[i]} 台", file=sys.stderr)
        _print_layerwise_reachability(prob, red)


def _print_layerwise_reachability(prob, red) -> None:
    """报告逐层子集和可达性：区分「逐层都不够」与「逐层够、合起来无解」。"""
    from .count import per_layer_reachability
    reach = per_layer_reachability(prob, red)
    print("  逐层单独看（忽略跨层耦合）的配额可达性：", file=sys.stderr)
    any_layer_impossible = False
    any_layer_possible = False
    for i, s in enumerate(prob.strata):
        need = red.residual[i]
        if need < 0:
            print(f"    层 {s!r}: 锁定已超额", file=sys.stderr)
            any_layer_impossible = True
            continue
        reachable = reach[i]
        if reachable is None:
            print(f"    层 {s!r}: 配额 {need} 超出诊断上限，未检查", file=sys.stderr)
            continue
        ok = need in reachable
        if ok:
            any_layer_possible = True
            print(f"    层 {s!r}: 需要 {need} 台，可用子集和能达到", file=sys.stderr)
        else:
            any_layer_impossible = True
            print(f"    层 {s!r}: 需要 {need} 台，单层内可达子集和 "
                  f"{sorted(reachable)} 不含它", file=sys.stderr)
    if any_layer_possible and not any_layer_impossible:
        print("    → 各层单独都有解，但合起来 Z=0：无解来自**跨层的同一整簇"
              "约束**（某些 cluster 必须同时给多层供货，无法任意组合）。",
              file=sys.stderr)


def _emit(prob, red, tables, outcome, args, input_info) -> int:
    text = out_mod.summary_text(prob, red, tables, outcome, args.max_states)
    if args.out_dir:
        path = out_mod.write_outputs(args.out_dir, prob, red, tables, outcome,
                                     args.max_states, input_info)
        print(text, end="")
        print(f"\n已写出（原子替换，失败不覆盖旧文件）: {path}")
    else:
        print(text, end="")
    return EXIT_OK


def cmd_run(args) -> int:
    try:
        prob, warnings = _load_problem(args.devices, args.design)
    except AllocationInputError as e:
        print("输入被拒绝（未写任何文件）：", file=sys.stderr)
        for p in e.problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return EXIT_INPUT
    for w in warnings:
        print(f"提示：{w}", file=sys.stderr)
    try:
        red, tables, failures = _build(prob, args.max_states)
    except StateBudgetExhausted as e:
        print(str(e), file=sys.stderr)
        return EXIT_BUDGET
    except ValueError as e:
        print(f"输入被拒绝：{e}", file=sys.stderr)
        return EXIT_INPUT
    if tables is None or tables.Z == 0:
        _report_unsolvable(prob, red, [])
        return EXIT_UNSOLVABLE
    if tables.budget_warning:
        print(f"提示：{tables.budget_warning}", file=sys.stderr)

    try:
        if args.rank is not None:
            outcome = sampler.decode(prob, tables, args.rank)
        else:
            outcome = sampler.sample(prob, tables, args.seed)
    except RankOutOfRange as e:
        print(f"rank 被拒绝（未写任何文件）：{e}", file=sys.stderr)
        return EXIT_INPUT
    problems = check_outcome(prob, red, tables, outcome)
    problems += check_trace(prob, red, tables, outcome)
    if problems:
        print("内部核对失败（未写任何文件）：", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return EXIT_INPUT
    input_info = {"devices": str(args.devices), "design": str(args.design)}
    return _emit(prob, red, tables, outcome, args, input_info)


def cmd_replay(args) -> int:
    """只读规范化设计 + rank：不读 CSV、不重放算法内部状态。"""
    try:
        doc = json.loads(Path(args.normalized).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"规范化设计读取失败：{e}", file=sys.stderr)
        return EXIT_INPUT
    try:
        return _replay_from_design(doc, args.rank, args.max_states,
                                   devices_csv=args.devices_csv)
    except (KeyError, TypeError, ValueError) as e:
        print(f"规范化设计内容非法：{e}", file=sys.stderr)
        return EXIT_INPUT


def replay_arms(doc: dict, rank: int, max_states: int) -> Tuple[Dict[str, str], int, int]:
    """从规范化设计重建 DP 并返回 ``(arms, Z, n_states)``；供 demo/测试复用。"""
    from .count import build_tables as _bt
    from .inputs import Design, DeviceRow, LockSpec
    from .model import prepare as _prepare
    from .model import reduce_locks as _red

    strata = list(doc["ordering"]["strata"])
    clusters = list(doc["ordering"]["clusters"])
    targets = dict(doc["target_A"])
    contrib = dict(doc["cluster_contribution"])
    locks_doc = list(doc["locks"])
    rows = []
    line = 2
    for cid in clusters:
        if cid not in contrib:
            raise ValueError(f"规范化设计缺少 cluster {cid!r} 的逐层贡献")
        for s in strata:
            n = contrib[cid].get(s, 0)
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                raise ValueError(f"cluster {cid!r} 层 {s!r} 的贡献非法: {n!r}")
            for k in range(n):
                rows.append(DeviceRow(f"{cid}#{k}", s, cid, line))
                line += 1
    design = Design(targets={s: int(targets[s]) for s in strata},
                    locks=[LockSpec(l["cluster_id"], l["arm"], None, f"locks[{i}]")
                           for i, l in enumerate(locks_doc)])
    prob = _prepare(rows, design)
    red = _red(prob)
    if red.failures:
        raise ValueError("；".join(red.failures))
    tables = _bt(prob, red, max_states)
    if tables.Z != int(doc.get("Z", tables.Z)):
        raise ValueError(f"设计文件记录的 Z={doc.get('Z')} 与复算 Z={tables.Z} 不符")
    if tables.Z == 0:
        raise ValueError("Z=0 的无解设计不能按 rank 复算")
    outcome = sampler.decode(prob, tables, rank)
    return outcome.arms, tables.Z, tables.n_states


def _replay_from_design(doc, rank: int, max_states: int,
                        devices_csv: Optional[str]) -> int:
    if rank < 0:
        raise ValueError(f"rank={rank} 越界（必须 ≥ 0）")
    try:
        arms, Z, n_states = replay_arms(doc, rank, max_states)
    except StateBudgetExhausted as e:
        print(str(e), file=sys.stderr)
        return EXIT_BUDGET
    except RankOutOfRange as e:
        print(f"rank 被拒绝：{e}", file=sys.stderr)
        return EXIT_INPUT
    print(f"按规范化设计 + rank 复算：Z={Z} rank={rank} "
          f"（DP 状态 {n_states}，未重放内部状态）")
    for cid, arm in arms.items():
        print(f"  {cid} → {arm}")
    if devices_csv:
        # 可选：把 rank 结果映射回原设备名单，逐设备核对
        from .inputs import read_device_csv as _rd
        from .inputs import AllocationInputError
        try:
            rows, _ = _rd(devices_csv)
        except AllocationInputError as e:
            print("设备名单读取失败：", file=sys.stderr)
            for p in e.problems:
                print(f"  ✗ {p}", file=sys.stderr)
            return EXIT_INPUT
        print(f"\n逐设备名单（来自 {devices_csv}）：")
        for r in sorted(rows, key=lambda x: (x.cluster_id, x.device_id)):
            print(f"  {r.device_id}\t{r.stratum}\t{r.cluster_id}\t"
                  f"{arms[r.cluster_id]}")
    return EXIT_OK


def cmd_demo(args) -> int:
    return demo_mod.run_demo(Path(args.workdir), seeds=args.seeds)


def _positive_int(text: str) -> int:
    try:
        v = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"需要十进制正整数，收到 {text!r}")
    if v <= 0:
        raise argparse.ArgumentTypeError(f"需要正整数，收到 {v}")
    return v


def _int_arg(text: str) -> int:
    try:
        return int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"需要十进制整数，收到 {text!r}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="allocation", description="整簇随机化的实验分配（精确等概率抽签）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("run", help="读取名单与设计，计数并抽签/按 rank 取方案")
    sp.add_argument("--devices", required=True, help="设备名单 CSV")
    sp.add_argument("--design", required=True, help="设计 JSON（target_A / locks）")
    sp.add_argument("--seed", type=_int_arg, default=0,
                    help="整数随机种子（默认 0；只影响抽签，不影响 Z/组序）")
    sp.add_argument("--rank", type=_int_arg, default=None,
                    help="给定 rank 时按 rank 取方案，不做随机抽签")
    sp.add_argument("--max-states", type=_positive_int, default=5_000_000,
                    help="动态规划状态预算（默认 5000000）")
    sp.add_argument("--out-dir", default=None, help="写出结果目录（原子替换）")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("replay", help="只用规范化设计与 rank 复算（不读 CSV）")
    sp.add_argument("--normalized", required=True,
                    help="design.normalized.json")
    sp.add_argument("--rank", required=True, type=_int_arg)
    sp.add_argument("--max-states", type=_positive_int, default=5_000_000)
    sp.add_argument("--devices-csv", default=None,
                    help="可选：把复算结果映射回原设备名单")
    sp.set_defaults(fn=cmd_replay)

    sp = sub.add_parser("demo", help="一条命令跑通演示与回归核对")
    sp.add_argument("--workdir", default="outputs/allocation_demo")
    sp.add_argument("--seeds", type=_positive_int, default=20,
                    help="分布检查用的种子数（仅作补充证据）")
    sp.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
