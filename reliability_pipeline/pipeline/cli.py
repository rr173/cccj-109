"""命令行入口。

子命令
------
- ``gen-gold``      生成手算金标准日志（devices.csv / events.csv）
- ``gen-random``    生成种子可复现的随机合成日志
- ``analyze``       运行一次分析（队列推导 + AJ/KM 估计 + 核对 + 落库 + 导出）
- ``verify-gold``   用金标准手算期望值核对（内存重算 + 可选比对库中已存结果）
- ``verify-random`` 随机小规模输入 vs 逐日枚举参考实现交叉核对
- ``show-bias``     展示"退役误作删失"的 KM 相对 AJ-CIF 的高估
- ``demo``          一条命令跑完全部流程
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import checks, gold_data, store
from .cohort import derive_cohort
from .estimate import estimate_groups
from .loaders import devices_from_rows, events_from_rows
from .reference import reference_estimate
from .synth import generate
from .verify import verify_gold_run


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"非法日期（需 YYYY-MM-DD）: {s!r}")


def _run_pipeline(data_dir: str, cutoff: date, obs_end: date):
    """装载 → 队列推导 → 估计 → 参考实现 + 不变量核对。返回各阶段产物。"""
    devices_raw, events_raw = store.load_raw_csv(data_dir)
    devices = devices_from_rows(devices_raw)
    events = events_from_rows(events_raw)
    cohort = derive_cohort(devices, events, cutoff, obs_end)
    estimates = estimate_groups(cohort.intervals)
    reference = reference_estimate(cohort.intervals)
    problems = checks.check_all(estimates)
    problems += checks.compare_with_reference(estimates, reference)
    return devices_raw, events_raw, cohort, estimates, problems


def _print_estimate_table(estimates, group: str, title: str) -> None:
    ge = estimates.get(group)
    print(f"\n--- {title}（分组 {group}）---")
    print(f"{'t':>4} {'n_risk':>7} {'d_故障':>7} {'d_退役':>7} {'删失':>5} "
          f"{'S(t)':>9} {'CIF_故障':>9} {'CIF_退役':>9} {'F_KM(对照)':>11}")
    if not ge or not ge.rows:
        print("   （无结局时点：纯删失组）")
        return
    for r in ge.rows:
        print(f"{r['t']:>4} {r['n_risk']:>7} {r['d_failure']:>7} {r['d_retirement']:>7} "
              f"{r['n_censored']:>5} {r['surv']:>9.4f} {r['cif_failure']:>9.4f} "
              f"{r['cif_retirement']:>9.4f} {r['km_failure']:>11.4f}")


def cmd_gen_gold(args) -> int:
    out = gold_data.write_gold_csvs(args.out)
    print(f"金标准日志已生成: {out}（devices.csv / events.csv）")
    return 0


def cmd_gen_random(args) -> int:
    devices, events, obs_end, cutoff = generate(args.seed, args.devices, args.out)
    print(f"随机日志已生成: {args.out}（设备 {len(devices)}，事件 {len(events)}）")
    print(f"建议参数: --observation-end {obs_end} --knowledge-cutoff {cutoff}")
    return 0


def cmd_analyze(args) -> int:
    cutoff, obs_end = args.knowledge_cutoff, args.observation_end
    devices_raw, events_raw, cohort, estimates, problems = _run_pipeline(
        args.data, cutoff, obs_end)

    n_in = sum(1 for r in cohort.device_rows if r["cohort_status"] == "included")
    n_notime = sum(1 for r in cohort.device_rows if r["cohort_status"] == "excluded_no_time")
    n_rej = sum(1 for r in cohort.device_rows if r["cohort_status"] == "rejected")
    print(f"[{args.run_id}] 设备: 原始 {len(devices_raw)} → 入选 {n_in}、"
          f"无观察时长 {n_notime}、拒收 {n_rej}；事件拒收 "
          f"{sum(1 for r in cohort.rejections if r['level'] == 'event')} 条")
    for g in sorted(estimates):
        _print_estimate_table(estimates, g, f"run={args.run_id}")

    if problems:
        print("\n不变量/参考核对未通过:", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1
    print("\n不变量核对与逐日枚举参考比对: 全部通过")

    con = store.connect(args.db)
    store.save_run(con, args.run_id, args.data, cutoff, obs_end,
                   devices_raw, events_raw, cohort, estimates)
    out = store.export_run_csv(con, args.run_id, args.export)
    con.close()
    print(f"结果已落库 {args.db}（run_id={args.run_id}），CSV 导出至 {out}")
    return 0


def cmd_verify_gold(args) -> int:
    gold_dir = args.data
    if not (Path(gold_dir) / "devices.csv").exists():
        gold_data.write_gold_csvs(gold_dir)
    con = store.connect(args.db) if args.db else None
    ok = True
    for run_id, exp in gold_data.EXPECTED.items():
        cutoff = _parse_date(exp["knowledge_cutoff"])
        obs_end = _parse_date(exp["observation_end"])
        _, _, cohort, estimates, problems = _run_pipeline(gold_dir, cutoff, obs_end)
        problems += verify_gold_run(run_id, cohort, estimates)
        # 若库中已存该 run，比对已存结果与手算期望（证明迟到撤销不改写历史）
        if con is not None:
            stored = con.execute(
                "SELECT grp, t, n_risk, d_failure, d_retirement, n_censored, "
                "cif_failure, cif_retirement, km_failure "
                "FROM risk_sets r JOIN estimates e USING (run_id, grp, t) "
                "WHERE run_id = ? ORDER BY grp, t", [run_id]).fetchall()
            if stored:
                flat_exp = []
                for g, rows in exp["groups"].items():
                    for (t, n, d1, d2, c, s, c1, c2, skm) in rows:
                        flat_exp.append((g, t, n, d1, d2, c,
                                         float(c1), float(c2), float(1 - skm)))
                flat_exp.sort()
                stored_f = [tuple(row) for row in stored]
                if len(stored_f) != len(flat_exp) or any(
                    a[:6] != b[:6] or any(abs(x - y) > 1e-12 for x, y in zip(a[6:], b[6:]))
                    for a, b in zip(stored_f, flat_exp)
                ):
                    problems.append(f"[{run_id}] 库中已存结果与手算期望不一致")
        if problems:
            ok = False
            print(f"✗ {run_id}: 金标准核对失败")
            for p in problems:
                print(f"    - {p}")
        else:
            print(f"✓ {run_id}: 队列、拒收明细、风险集、AJ/KM 曲线与手算金标准完全一致"
                  + ("（含库中已存结果比对）" if con is not None else ""))
    if con is not None:
        con.close()
    return 0 if ok else 1


def cmd_verify_random(args) -> int:
    n_fail = 0
    for seed in range(args.seed_start, args.seed_start + args.seeds):
        devices_rows, events_rows, obs_end, cutoff = generate(seed, args.devices)
        devices = devices_from_rows(devices_rows)
        events = events_from_rows(events_rows)
        cohort = derive_cohort(devices, events, cutoff, obs_end)
        estimates = estimate_groups(cohort.intervals)
        reference = reference_estimate(cohort.intervals)
        problems = checks.check_all(estimates)
        problems += checks.compare_with_reference(estimates, reference)
        n_in = len(cohort.intervals)
        n_rej = len(cohort.rejections)
        if problems:
            n_fail += 1
            print(f"✗ seed={seed}: {len(problems)} 处违规（入选 {n_in}，拒收 {n_rej}）")
            for p in problems[:10]:
                print(f"    - {p}")
        else:
            print(f"✓ seed={seed}: 入选 {n_in} 台、拒收 {n_rej} 条，"
                  f"主估计器 vs 逐日枚举参考一致，不变量全部通过")
    if n_fail:
        print(f"\n{args.seeds} 个种子中 {n_fail} 个失败", file=sys.stderr)
        return 1
    print(f"\n{args.seeds} 个随机种子全部通过")
    return 0


def cmd_show_bias(args) -> int:
    con = store.connect(args.db)
    rows = con.execute(
        "SELECT t, n_risk, d_failure, d_retirement, cif_failure, km_failure "
        "FROM risk_sets r JOIN estimates e USING (run_id, grp, t) "
        "WHERE run_id = ? AND grp = ? ORDER BY t",
        [args.run_id, args.group]).fetchall()
    con.close()
    if not rows:
        print(f"未找到 run_id={args.run_id} 分组={args.group} 的结果", file=sys.stderr)
        return 1
    print(f"竞争退役误作普通删失的偏差（run={args.run_id}, 分组={args.group}）")
    print(f"{'t':>4} {'n_risk':>7} {'d_故障':>7} {'d_退役':>7} "
          f"{'CIF_故障(AJ)':>12} {'F_KM(误作删失)':>14} {'高估':>9}")
    max_gap = 0.0
    for t, n, d1, d2, cif1, kmf in rows:
        gap = kmf - cif1
        max_gap = max(max_gap, gap)
        print(f"{t:>4} {n:>7} {d1:>7} {d2:>7} {cif1:>12.4f} {kmf:>14.4f} {gap:>9.4f}")
    if max_gap > 1e-12:
        print(f"\n存在竞争退役时两种估计确实不同：最大高估 {max_gap:.4f}"
              f"（KM 把退役者当作仍在险，系统性高估故障累积概率）")
        return 0
    print("\n该队列中两种估计一致（无先发生的竞争退役）")
    return 2


def cmd_demo(args) -> int:
    root = Path(args.workdir)
    data_gold = root / "data" / "gold"
    db = root / "outputs" / "reliability.duckdb"
    export_dir = root / "outputs"

    print("=" * 72)
    print("步骤 1/6  生成金标准观察日志")
    print("=" * 72)
    gold_data.write_gold_csvs(data_gold)
    print(f"已写入 {data_gold}")

    ns = argparse.Namespace
    for i, (run_id, cutoff) in enumerate(
            [("gold_run1", gold_data.GOLD_CUTOFF_RUN1),
             ("gold_run2", gold_data.GOLD_CUTOFF_RUN2)], start=2):
        print("\n" + "=" * 72)
        print(f"步骤 {i}/6  分析 {run_id}"
              f"（knowledge_cutoff={cutoff}, observation_end={gold_data.GOLD_OBSERVATION_END}）")
        print("=" * 72)
        rc = cmd_analyze(ns(data=str(data_gold), db=str(db), run_id=run_id,
                            knowledge_cutoff=_parse_date(cutoff),
                            observation_end=_parse_date(gold_data.GOLD_OBSERVATION_END),
                            export=str(export_dir)))
        if rc != 0:
            return rc

    print("\n" + "=" * 72)
    print("步骤 4/6  金标准核对（含库中已存结果：迟到撤销不改写历史）")
    print("=" * 72)
    rc = cmd_verify_gold(ns(data=str(data_gold), db=str(db)))
    if rc != 0:
        return rc

    print("\n" + "=" * 72)
    print("步骤 5/6  竞争风险偏差展示（gold_run1, 分组 ALL）")
    print("=" * 72)
    rc = cmd_show_bias(ns(db=str(db), run_id="gold_run1", group="ALL"))
    if rc == 1:
        return rc

    print("\n" + "=" * 72)
    print("步骤 6/6  随机小规模输入 vs 逐日枚举参考核对")
    print("=" * 72)
    rc = cmd_verify_random(ns(seeds=args.seeds, seed_start=1, devices=40))
    if rc != 0:
        return rc

    print("\n" + "=" * 72)
    print(f"演示完成。数据库: {db}；CSV 导出: {export_dir}/<run_id>/")
    print("=" * 72)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="reliability-pipeline",
                                description="工业设备可靠性生存分析管线")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("gen-gold", help="生成手算金标准日志")
    sp.add_argument("--out", required=True)
    sp.set_defaults(fn=cmd_gen_gold)

    sp = sub.add_parser("gen-random", help="生成随机合成日志")
    sp.add_argument("--out", required=True)
    sp.add_argument("--seed", type=int, default=1)
    sp.add_argument("--devices", type=int, default=40)
    sp.set_defaults(fn=cmd_gen_random)

    sp = sub.add_parser("analyze", help="运行一次分析")
    sp.add_argument("--data", required=True, help="含 devices.csv / events.csv 的目录")
    sp.add_argument("--db", required=True)
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--knowledge-cutoff", required=True, type=_parse_date)
    sp.add_argument("--observation-end", required=True, type=_parse_date)
    sp.add_argument("--export", default="outputs")
    sp.set_defaults(fn=cmd_analyze)

    sp = sub.add_parser("verify-gold", help="金标准核对")
    sp.add_argument("--data", required=True)
    sp.add_argument("--db", default=None, help="可选：同时比对库中已存结果")
    sp.set_defaults(fn=cmd_verify_gold)

    sp = sub.add_parser("verify-random", help="随机交叉核对")
    sp.add_argument("--seeds", type=int, default=20)
    sp.add_argument("--seed-start", type=int, default=1)
    sp.add_argument("--devices", type=int, default=40)
    sp.set_defaults(fn=cmd_verify_random)

    sp = sub.add_parser("show-bias", help="展示 KM 误作删失的高估")
    sp.add_argument("--db", required=True)
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--group", default="ALL")
    sp.set_defaults(fn=cmd_show_bias)

    sp = sub.add_parser("demo", help="一条命令跑完全部流程")
    sp.add_argument("--workdir", default=".")
    sp.add_argument("--seeds", type=int, default=15)
    sp.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
