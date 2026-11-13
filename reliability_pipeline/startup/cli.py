"""启机过程识别命令行入口（``python3 -m startup.cli``）。

子命令
------
- ``run``       读取 DuckDB 标准遥测测量，按 JSON 配方识别启机尝试并落库
- ``attempts``  查询某次运行的尝试判定（三态 + 原因 + 关键时刻）
- ``evidence``  查询某次尝试的证据时间片与源测量定位键
- ``verify``    生产状态机 vs 独立参考判定器逐条交叉核对
- ``demo``      一条命令跑通：真实二进制接入 → 识别 → 证据回溯 → 交叉核对
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, demo as demo_mod, engine, reference, store as store_mod
from .recipe import RecipeError, load_recipe

REPO_ROOT = Path(__file__).resolve().parent.parent


def _print_table(headers, rows) -> None:
    cols = [str(h) for h in headers]
    data = [["" if v is None else str(v) for v in r] for r in rows]
    widths = [len(c) for c in cols]
    for r in data:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(v))
    line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in data:
        print("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))
    print(f"（{len(data)} 行）")


def _fmt_ns(ns):
    return "NULL" if ns is None else str(ns)


# ----------------------------------------------------------------------
# run / attempts / evidence
# ----------------------------------------------------------------------

def _cmd_run(args) -> int:
    try:
        recipe = load_recipe(args.recipe)
    except RecipeError as e:
        print(str(e), file=sys.stderr)
        return 2
    con = store_mod.connect(args.db)
    try:
        attempts = engine.run_detection(con, recipe, args.analysis_end_ns)
        recipe_json = json.dumps(json.loads(Path(args.recipe).read_text(
            encoding="utf-8")), ensure_ascii=False, sort_keys=True)
        try:
            store_mod.save_run(con, args.run_id, recipe, recipe_json,
                               args.analysis_end_ns, attempts)
        except store_mod.StartupStoreError as e:
            print(str(e), file=sys.stderr)
            return 3
        if args.jsonl:
            store_mod.write_jsonl(attempts, args.jsonl)
            print(f"JSONL 已写出：{args.jsonl}")
    finally:
        con.close()
    counts = {"confirmed": 0, "rejected": 0, "indeterminate": 0}
    for at in attempts:
        counts[at.status] += 1
    print(f"运行 {args.run_id}：配方 {recipe.name}，"
          f"analysis_end_ns={args.analysis_end_ns}")
    print(f"尝试 {len(attempts)} 次：confirmed={counts['confirmed']} "
          f"rejected={counts['rejected']} indeterminate={counts['indeterminate']}"
          "（indeterminate 不算设备启动失败）")
    _print_attempts(attempts)
    return 0


def _print_attempts(attempts) -> None:
    rows = [[at.device_id, at.boot_seq, at.attempt_seq, at.s_ns,
             _fmt_ns(at.confirm_ns),
             _fmt_ns(at.stable_start_ns), _fmt_ns(at.stable_end_ns),
             at.end_ns, at.status, at.reason] for at in attempts]
    _print_table(["device", "boot", "seq", "s_ns", "confirm_ns",
                  "stable_start", "stable_end", "end_ns", "status", "reason"],
                 rows)


def _cmd_attempts(args) -> int:
    con = store_mod.connect(args.db)
    try:
        sql = ("SELECT device_id, boot_seq, attempt_seq, s_ns, confirm_ns, "
               "stable_start_ns, stable_end_ns, end_ns, status, reason, detail "
               "FROM startup_attempts WHERE run_id = ?")
        params: list = [args.run_id]
        if args.device is not None:
            sql += " AND device_id = ?"
            params.append(args.device)
        if args.status is not None:
            sql += " AND status = ?"
            params.append(args.status)
        sql += " ORDER BY device_id, boot_seq, attempt_seq"
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    _print_table(["device", "boot", "seq", "s_ns", "confirm_ns",
                  "stable_start", "stable_end", "end_ns", "status", "reason",
                  "detail"], rows)
    return 0


def _cmd_evidence(args) -> int:
    con = store_mod.connect(args.db)
    try:
        key = [args.run_id, args.device, args.boot_seq, args.attempt_seq]
        at = con.execute(
            "SELECT status, reason, detail FROM startup_attempts "
            "WHERE run_id = ? AND device_id = ? AND boot_seq = ? AND attempt_seq = ?",
            key).fetchone()
        if at is None:
            print("没有找到该尝试", file=sys.stderr)
            return 1
        print(f"尝试 ({args.device}, {args.boot_seq}, {args.attempt_seq})："
              f"{at[0]} / {at[1]} — {at[2]}")
        ev = con.execute(
            "SELECT ev_seq, kind, t_start_ns, t_end_ns, role, detail "
            "FROM startup_evidence "
            "WHERE run_id = ? AND device_id = ? AND boot_seq = ? AND attempt_seq = ? "
            "ORDER BY ev_seq", key).fetchall()
        _print_table(["ev_seq", "kind", "t_start_ns", "t_end_ns", "role", "detail"],
                     ev)
        refs = con.execute(
            "SELECT ev_seq, ref_seq, file_id, frame_offset, ordinal, channel, "
            "ts_ns, value, unit FROM startup_evidence_refs "
            "WHERE run_id = ? AND device_id = ? AND boot_seq = ? AND attempt_seq = ? "
            "ORDER BY ev_seq, ref_seq", key).fetchall()
        print("\n源测量定位键（指向 tele_samples 真实行）：")
        _print_table(["ev_seq", "ref_seq", "file_id", "frame_offset", "ordinal",
                      "channel", "ts_ns", "value", "unit"], refs)
    finally:
        con.close()
    return 0


# ----------------------------------------------------------------------
# verify：生产状态机 vs 独立参考判定器
# ----------------------------------------------------------------------

def cross_check(con, recipe, analysis_end_ns):
    """逐组比对引擎与参考实现的判定；返回不一致列表。"""
    mismatches = []
    for (device_id, boot_seq), rows in engine.iter_groups(con, recipe,
                                                          analysis_end_ns):
        got = [a.verdict_tuple() for a in engine.detect_group(
            rows, recipe, analysis_end_ns, device_id, boot_seq)]
        want = [a.verdict_tuple() for a in reference.reference_detect(
            rows, recipe, analysis_end_ns, device_id, boot_seq)]
        if got != want:
            mismatches.append(((device_id, boot_seq), got, want))
    return mismatches


def _cmd_verify(args) -> int:
    try:
        recipe = load_recipe(args.recipe)
    except RecipeError as e:
        print(str(e), file=sys.stderr)
        return 2
    con = store_mod.connect(args.db)
    try:
        mismatches = cross_check(con, recipe, args.analysis_end_ns)
    finally:
        con.close()
    if mismatches:
        print(f"交叉核对失败：{len(mismatches)} 个组不一致", file=sys.stderr)
        for (dev, boot), got, want in mismatches[:5]:
            print(f"  组 ({dev}, {boot}):", file=sys.stderr)
            print(f"    引擎: {got}", file=sys.stderr)
            print(f"    参考: {want}", file=sys.stderr)
        return 1
    print("交叉核对通过：生产状态机与独立参考判定器逐条一致")
    return 0


# ----------------------------------------------------------------------
# demo
# ----------------------------------------------------------------------

def _check_refs_resolve(con, attempts, device: int, boot: int) -> list:
    """对真实接入的设备逐条核对：证据定位键必须能回到 tele_samples 真实行。"""
    problems = []
    for at in attempts:
        if at.device_id != device or at.boot_seq != boot:
            continue
        for ev in at.evidence:
            for ref in ev.refs:
                row = con.execute(
                    "SELECT channel, ts_ns, value, unit FROM tele_samples "
                    "WHERE file_id = ? AND frame_offset = ? AND ordinal = ?",
                    [ref.file_id, ref.frame_offset, ref.ordinal]).fetchone()
                if row is None:
                    problems.append(
                        f"尝试 {at.attempt_seq} 证据 {ev.kind}：定位键 "
                        f"({ref.file_id}, {ref.frame_offset}, {ref.ordinal}) "
                        "在 tele_samples 中不存在（伪造源样本？）")
                elif (row[0], row[1], row[2], row[3]) != (
                        ref.channel, ref.ts_ns, ref.value, ref.unit):
                    problems.append(
                        f"尝试 {at.attempt_seq} 证据 {ev.kind}：定位键指向的行 "
                        f"{row} 与引用 {ref} 不符")
    return problems


def _cmd_demo(args) -> int:
    workdir = Path(args.workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    recipe_path = workdir / "recipe.json"
    recipe_path.write_text(json.dumps(demo_mod.DEMO_RECIPE,
                                      ensure_ascii=False, indent=2),
                           encoding="utf-8")
    recipe = load_recipe(recipe_path)
    db = str(workdir / "startup.duckdb")
    problems: list = []

    print("== 1. 生成设备 7 的二进制遥测包并用真实接入命令入库 ==")
    bin_path = workdir / "device7.bin"
    bin_path.write_bytes(demo_mod.build_binary_stream())
    print(f"已生成 {bin_path}（{bin_path.stat().st_size} 字节）")
    rc = subprocess.run(
        [sys.executable, "-m", "telemetry.cli", "ingest",
         "--db", db, "--file", str(bin_path)],
        cwd=REPO_ROOT).returncode
    if rc != 0:
        print(f"接入命令失败（退出码 {rc}）", file=sys.stderr)
        return 1

    print("\n== 2. 直接建库的边界用例（dev 31..41） ==")
    con = store_mod.connect(db)
    demo_mod.insert_rows(con, demo_mod.build_main_rows())
    n = con.execute("SELECT COUNT(*) FROM tele_samples").fetchone()[0]
    print(f"tele_samples 共 {n} 行（含设备 7 真实接入与直接建库用例）")

    print("\n== 3. 运行启机识别（主库，analysis_end = E0+25s） ==")
    attempts = engine.run_detection(con, recipe, demo_mod.ANALYSIS_END_MAIN)
    store_mod.save_run(con, "demo_main", recipe,
                       json.dumps(demo_mod.DEMO_RECIPE, ensure_ascii=False,
                                  sort_keys=True),
                       demo_mod.ANALYSIS_END_MAIN, attempts)
    _print_attempts(attempts)
    problems += demo_mod.check_expected(attempts, demo_mod.EXPECT_MAIN,
                                        "主库")

    print("\n== 4. 设备 7（真实二进制接入）逐条核对结果及来源 ==")
    problems += _check_refs_resolve(con, attempts, 7, 1)
    for at in attempts:
        if at.device_id == 7:
            print(f"  尝试 #{at.attempt_seq}: s={at.s_ns} status={at.status} "
                  f"reason={at.reason} end={at.end_ns}")
            for ev in at.evidence:
                ref_txt = ",".join(
                    f"({r.file_id},{r.frame_offset},{r.ordinal})"
                    for r in ev.refs) or "-"
                print(f"    [{ev.kind}] t={ev.t_start_ns}"
                      f"{'..'+str(ev.t_end_ns) if ev.t_end_ns else ''} "
                      f"{ev.detail} 来源: {ref_txt}")
    if not problems:
        print("  全部证据定位键均可回到 tele_samples 真实行 ✓")

    print("\n== 5. 证据示例：完成点恰好过期后恢复（dev 34）与同刻冲突（dev 35） ==")
    for dev in (34, 35):
        for at in attempts:
            if at.device_id == dev:
                print(f"  dev {dev} 尝试 #{at.attempt_seq}: {at.status}/"
                      f"{at.reason}")
                for ev in at.evidence:
                    if ev.kind in ("stable_break", "conflict", "confirm"):
                        print(f"    [{ev.kind}] t={ev.t_start_ns} {ev.detail}"
                              f"（{len(ev.refs)} 条来源）")

    print("\n== 6. 生产状态机 vs 独立参考判定器（主库全部组） ==")
    mismatches = cross_check(con, recipe, demo_mod.ANALYSIS_END_MAIN)
    if mismatches:
        problems.append(f"主库交叉核对不一致：{mismatches[:3]}")
    else:
        print("  交叉核对通过 ✓")
    con.close()

    print("\n== 7. 分析截断演示（独立库，analysis_end = E0+10s） ==")
    db_cut = str(workdir / "startup_cut.duckdb")
    con2 = store_mod.connect(db_cut)
    demo_mod.insert_rows(con2, demo_mod.build_cut_rows())
    attempts_cut = engine.run_detection(con2, recipe, demo_mod.ANALYSIS_END_CUT)
    store_mod.save_run(con2, "demo_cut", recipe,
                       json.dumps(demo_mod.DEMO_RECIPE, ensure_ascii=False,
                                  sort_keys=True),
                       demo_mod.ANALYSIS_END_CUT, attempts_cut)
    _print_attempts(attempts_cut)
    problems += demo_mod.check_expected(attempts_cut, demo_mod.EXPECT_CUT,
                                        "截断库")
    mismatches = cross_check(con2, recipe, demo_mod.ANALYSIS_END_CUT)
    if mismatches:
        problems.append(f"截断库交叉核对不一致：{mismatches[:3]}")
    else:
        print("  交叉核对通过 ✓")
    con2.close()

    print()
    if problems:
        print("演示核对失败：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("演示全部核对通过：真实接入组逐条对得上来源，边界用例与手工期望一致，"
          "引擎与参考判定器一致")
    print(f"主库：{db}\n截断库：{db_cut}")
    print("可用 `python3 -m startup.cli attempts --db 主库 --run-id demo_main` "
          "与 `evidence` 子命令复查")
    return 0


# ----------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="startup",
        description="离线启机过程识别器（读取 DuckDB 标准遥测测量）")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="识别启机尝试并落库")
    p.add_argument("--db", required=True)
    p.add_argument("--recipe", required=True, help="启机配方 JSON")
    p.add_argument("--analysis-end-ns", type=int, required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--jsonl", help="可选：把尝试结果（含证据）导出为 JSONL")
    p.set_defaults(fn=_cmd_run)

    p = sub.add_parser("attempts", help="查询某次运行的尝试判定")
    p.add_argument("--db", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--device", type=int)
    p.add_argument("--status", choices=["confirmed", "rejected", "indeterminate"])
    p.set_defaults(fn=_cmd_attempts)

    p = sub.add_parser("evidence", help="查询某次尝试的证据与源测量定位键")
    p.add_argument("--db", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--device", type=int, required=True)
    p.add_argument("--boot-seq", type=int, required=True)
    p.add_argument("--attempt-seq", type=int, required=True)
    p.set_defaults(fn=_cmd_evidence)

    p = sub.add_parser("verify", help="引擎 vs 独立参考判定器交叉核对")
    p.add_argument("--db", required=True)
    p.add_argument("--recipe", required=True)
    p.add_argument("--analysis-end-ns", type=int, required=True)
    p.set_defaults(fn=_cmd_verify)

    p = sub.add_parser("demo", help="一条命令跑完全部演示")
    p.add_argument("--workdir", required=True)
    p.set_defaults(fn=_cmd_demo)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
