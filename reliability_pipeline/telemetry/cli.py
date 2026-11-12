"""遥测接入命令行入口（``python3 -m telemetry.cli``）。

子命令
------
- ``gen``        生成可复现的场景化样例包（``--scenario`` / ``--seed``）
- ``ingest``     把二进制包文件解码入库（``--chunk-size`` 控制读取块长，
                 ``--commit-every`` 控制每多少帧提交一次进度）
- ``samples``    按设备/通道/时间段查询测量样本
- ``trace``      由 (file-id, 帧偏移) 回溯该帧解码出的全部测量值
- ``frames``     帧处置台账（applied/duplicate/rejected）
- ``rejections`` 拒收明细（含解码异常）
- ``state``      设备周期/计数器状态与文件续读进度
- ``query``      直接执行 SQL（研究人员入口）
- ``crash-demo`` 崩溃恢复演示：随机时刻 SIGKILL 子进程后重启续传
- ``demo``       一条命令跑通：生成 → 接入 → 崩溃演示 → 查询展示
"""

from __future__ import annotations

import argparse
import random
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import generator, store as store_mod
from .errors import IngestError
from .ingest import Ingester

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


# ----------------------------------------------------------------------
# gen / ingest
# ----------------------------------------------------------------------

def _cmd_gen(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    names = list(generator.SCENARIOS) if args.scenario == "all" else [args.scenario]
    for name in names:
        for fname, content in generator.build_scenario(name, args.seed, args.count).items():
            p = out / fname
            p.write_bytes(content)
            print(f"已生成 {p}（{len(content)} 字节）")
    return 0


def _ingest_one(ing: Ingester, path: str, chunk_size: int, commit_every: int,
                progress: bool = False) -> int:
    st = ing.ingest_file(path, chunk_size=chunk_size, commit_every=commit_every,
                         progress=progress)
    print(f"[file_id={st.file_id}] {st.path}")
    print(f"  进度 {st.start_offset} -> {st.consumed_offset}"
          f"（待续 {st.pending_bytes} 字节）")
    print(f"  帧：应用 {st.frames_applied}，重复 {st.frames_duplicate}，"
          f"拒收 {st.frames_rejected}，解码异常 {st.anomalies}；"
          f"新增样本 {st.samples_added}")
    return 0


def _cmd_ingest(args) -> int:
    try:
        with Ingester(args.db) as ing:
            for f in args.file:
                _ingest_one(ing, f, args.chunk_size, args.commit_every,
                            args.progress)
    except IngestError as e:
        print(f"拒绝续接：{e}", file=sys.stderr)
        return 3
    return 0


# ----------------------------------------------------------------------
# 查询类子命令
# ----------------------------------------------------------------------

def _cmd_samples(args) -> int:
    con = store_mod.connect(args.db)
    sql = ("SELECT file_id, frame_offset, device_id, boot_seq, layout_id, channel, "
           "tick, cum_tick, ts_ns, make_timestamp_ns(ts_ns) AS ts, "
           "raw_value, value, unit FROM tele_samples WHERE 1=1")
    params: list = []
    if args.device is not None:
        sql += " AND device_id = ?"
        params.append(args.device)
    if args.channel is not None:
        sql += " AND channel = ?"
        params.append(args.channel)
    if args.start_ns is not None:
        sql += " AND ts_ns >= ?"
        params.append(args.start_ns)
    if args.end_ns is not None:
        sql += " AND ts_ns < ?"
        params.append(args.end_ns)
    sql += " ORDER BY device_id, channel, ts_ns LIMIT ?"
    params.append(args.limit)
    rows = con.execute(sql, params).fetchall()
    _print_table(["file_id", "frame_offset", "device_id", "boot_seq", "layout_id",
                  "channel", "tick", "cum_tick", "ts_ns", "ts",
                  "raw_value", "value", "unit"], rows)
    return 0


def _cmd_trace(args) -> int:
    con = store_mod.connect(args.db)
    fr = con.execute(
        "SELECT file_id, frame_offset, kind, device_id, frame_len, disposition, "
        "reason, detail "
        "FROM tele_frames WHERE file_id = ? AND frame_offset = ?",
        [args.file_id, args.offset]).fetchall()
    print("帧台账：")
    _print_table(["file_id", "frame_offset", "kind", "device_id", "frame_len",
                  "disposition", "reason", "detail"], fr)
    rows = con.execute(
        "SELECT device_id, boot_seq, layout_id, channel, tick, cum_tick, ts_ns, "
        "make_timestamp_ns(ts_ns) AS ts, raw_value, value, unit "
        "FROM tele_samples WHERE file_id = ? AND frame_offset = ? "
        "ORDER BY ordinal", [args.file_id, args.offset]).fetchall()
    print("\n该帧解码出的测量值：")
    _print_table(["device_id", "boot_seq", "layout_id", "channel", "tick",
                  "cum_tick", "ts_ns", "ts", "raw_value", "value", "unit"], rows)
    return 0


def _cmd_frames(args) -> int:
    con = store_mod.connect(args.db)
    sql = ("SELECT file_id, frame_offset, kind, device_id, frame_len, disposition, "
           "reason FROM tele_frames")
    params: list = []
    if args.file_id is not None:
        sql += " WHERE file_id = ?"
        params.append(args.file_id)
    sql += " ORDER BY file_id, frame_offset LIMIT ?"
    params.append(args.limit)
    _print_table(["file_id", "frame_offset", "kind", "device_id", "frame_len",
                  "disposition", "reason"],
                 con.execute(sql, params).fetchall())
    return 0


def _cmd_rejections(args) -> int:
    con = store_mod.connect(args.db)
    sql = ("SELECT file_id, frame_offset, kind, device_id, reason, detail "
           "FROM tele_rejections")
    params: list = []
    if args.device is not None:
        sql += " WHERE device_id = ?"
        params.append(args.device)
    sql += " ORDER BY file_id, frame_offset LIMIT ?"
    params.append(args.limit)
    _print_table(["file_id", "frame_offset", "kind", "device_id", "reason", "detail"],
                 con.execute(sql, params).fetchall())
    return 0


def _cmd_state(args) -> int:
    con = store_mod.connect(args.db)
    print("设备状态（当前周期）：")
    _print_table(["device_id", "boot_seq", "epoch_ns", "tick_hz", "have_tick",
                  "last_raw_tick", "last_cum_tick", "last_layout_id"],
                 con.execute(
                     "SELECT device_id, boot_seq, epoch_ns, tick_hz, have_tick, "
                     "last_raw_tick, last_cum_tick, last_layout_id "
                     "FROM tele_device_state ORDER BY device_id").fetchall())
    print("\n文件续读进度：")
    _print_table(["file_id", "path", "identity", "consumed_offset", "file_size",
                  "prefix_sha256"],
                 con.execute(
                     "SELECT f.file_id, f.path, "
                     "COALESCE(f.identity, '<旧库：身份未知，禁止自动续接>') AS identity, "
                     "s.consumed_offset, s.file_size, "
                     "substr(s.prefix_sha256, 1, 16) || '…' "
                     "FROM tele_files f JOIN tele_ingest_state s USING (file_id) "
                     "ORDER BY f.file_id").fetchall())
    return 0


def _cmd_query(args) -> int:
    con = store_mod.connect(args.db)
    cur = con.execute(args.sql)
    if cur.description:
        _print_table([d[0] for d in cur.description], cur.fetchall())
    return 0


# ----------------------------------------------------------------------
# 崩溃恢复演示
# ----------------------------------------------------------------------

def _sample_dump(db: str):
    con = store_mod.connect(db)
    try:
        return con.execute(
            "SELECT device_id, boot_seq, layout_id, channel, tick, cum_tick, ts_ns, "
            "raw_value, value, unit FROM tele_samples "
            "ORDER BY device_id, boot_seq, channel, cum_tick").fetchall()
    finally:
        con.close()


def _crash_loop(db: str, data: Path, chunk_size: int, commit_every: int,
                seed: int, max_attempts: int = 60, verbose: bool = True) -> int:
    """反复以子进程接入 ``data``，在随机提交点附近 SIGKILL，直到正常退出。

    返回运行次数。子进程每提交一次打印一行 PROGRESS；父进程看到随机
    若干行后杀进程，模拟"在提交进度附近崩溃"。
    """
    rng = random.Random(seed)
    attempts = 0
    while True:
        attempts += 1
        proc = subprocess.Popen(
            [sys.executable, "-m", "telemetry.cli", "ingest",
             "--db", db, "--file", str(data),
             "--chunk-size", str(chunk_size),
             "--commit-every", str(commit_every), "--progress"],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        kill_after = rng.randint(1, 4)
        seen = 0
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith("PROGRESS"):
                seen += 1
                if seen >= kill_after:
                    proc.send_signal(signal.SIGKILL)
                    break
        proc.wait()
        if proc.returncode == 0:
            if verbose:
                print(f"第 {attempts} 次运行正常完成")
            return attempts
        if verbose:
            print(f"第 {attempts} 次运行在第 {seen} 次进度提交附近被 SIGKILL"
                  f"（returncode={proc.returncode}），重启续传…")
        if attempts >= max_attempts:
            raise RuntimeError(f"超过 {max_attempts} 次仍未完成")


def _cmd_crash_demo(args) -> int:
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    data = workdir / "crash.bin"
    data.write_bytes(generator.build_scenario("crash", args.seed, args.count)["crash.bin"])
    db_live = str(workdir / "crash_live.duckdb")
    db_ref = str(workdir / "crash_ref.duckdb")
    for p in (db_live, db_ref):
        Path(p).unlink(missing_ok=True)

    print(f"崩溃演示：{data}（{data.stat().st_size} 字节，{args.count} 包），"
          f"每 {args.commit_every} 帧提交一次进度")
    attempts = _crash_loop(db_live, data, args.chunk_size, args.commit_every,
                           args.seed)
    print(f"共经历 {attempts} 次运行（含 {attempts - 1} 次崩溃续传）")

    # 参照：同一份输入一次性干净入库
    with Ingester(db_ref) as ing:
        ing.ingest_file(data, chunk_size=args.chunk_size)

    live = _sample_dump(db_live)
    ref = _sample_dump(db_ref)
    dup = len(live) - len(set(live))
    print(f"崩溃续传库样本 {len(live)} 行；干净参照库 {len(ref)} 行；"
          f"重复行 {dup}")
    if live != ref or dup != 0:
        print("不一致！", file=sys.stderr)
        return 1
    print("一致：崩溃/重启没有造成漏包或重复样本")
    return 0


# ----------------------------------------------------------------------
# 全流程演示
# ----------------------------------------------------------------------

def _cmd_demo(args) -> int:
    workdir = Path(args.workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    data_dir = workdir / "data"
    data_dir.mkdir(parents=True)
    db = str(workdir / "telemetry.duckdb")

    print("== 1. 生成可复现样例包 ==")
    ns = argparse.Namespace(out=str(data_dir), scenario="all", seed=args.seed,
                            count=args.count)
    _cmd_gen(ns)

    print("\n== 2. 接入各场景（corrupt 用 1 字节块长验证分片无关性） ==")
    with Ingester(db) as ing:
        for name in ("mixed", "two-devices", "wrap", "retransmit"):
            _ingest_one(ing, str(data_dir / f"{name.replace('-', '_')}.bin"),
                        args.chunk_size, 0)
        _ingest_one(ing, str(data_dir / "corrupt.bin"), 1, 0)
        # 尾帧补齐演示：先 part1，再追加 part2
        tail = data_dir / "tail.bin"
        tail.write_bytes((data_dir / "tail.bin.part1").read_bytes())
        print("-- 末帧缺尾 3 字节：")
        _ingest_one(ing, str(tail), args.chunk_size, 0)
        with open(tail, "ab") as fh:
            fh.write((data_dir / "tail.part2").read_bytes())
        print("-- 补齐 3 字节后再接入：")
        _ingest_one(ing, str(tail), args.chunk_size, 0)

    print("\n== 3. 崩溃恢复演示 ==")
    ns = argparse.Namespace(workdir=str(workdir / "crash"), count=args.count,
                            seed=args.seed, chunk_size=4096, commit_every=200)
    rc = _cmd_crash_demo(ns)
    if rc != 0:
        return rc

    print("\n== 4. 查询展示 ==")
    con = store_mod.connect(db)
    print("-- 研究人员筛选：设备 7、通道 0、时间段 [epoch+400ms, epoch+1s) 内的样本")
    e0 = generator.EPOCH0
    _print_table(
        ["channel", "tick", "ts_ns", "ts", "value", "unit"],
        con.execute(
            "SELECT channel, tick, ts_ns, make_timestamp_ns(ts_ns), value, unit "
            "FROM tele_samples WHERE device_id = 7 AND channel = 0 "
            "AND ts_ns >= ? AND ts_ns < ? ORDER BY ts_ns",
            [e0 + 400_000_000, e0 + 1_000_000_000]).fetchall())
    print("\n-- 原始偏移回溯：corrupt.bin 中 payload 内嵌帧头字节的那个测量包")
    row = con.execute(
        "SELECT s.file_id, s.frame_offset FROM tele_samples s "
        "JOIN tele_files f USING (file_id) "
        "WHERE f.path LIKE '%corrupt.bin' AND s.device_id = 2 AND s.tick = 30 "
        "LIMIT 1").fetchone()
    if row:
        ns = argparse.Namespace(db=db, file_id=row[0], offset=row[1])
        _cmd_trace(ns)
    print("\n-- 拒收明细（含 CRC 损坏与旧包）")
    _print_table(
        ["file_id", "frame_offset", "kind", "device_id", "reason", "detail"],
        con.execute("SELECT file_id, frame_offset, kind, device_id, reason, detail "
                    "FROM tele_rejections ORDER BY file_id, frame_offset").fetchall())
    print(f"\n数据库：{db}")
    return 0


# ----------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="telemetry",
                                 description="传感器二进制遥测接入（解码 → DuckDB）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("gen", help="生成可复现的场景化样例包")
    p.add_argument("--out", required=True)
    p.add_argument("--scenario", default="all",
                   choices=list(generator.SCENARIOS) + ["all"])
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--count", type=int, default=2000, help="crash 场景的包数")
    p.set_defaults(fn=_cmd_gen)

    p = sub.add_parser("ingest", help="解码二进制包文件并入库（可续传）")
    p.add_argument("--db", required=True)
    p.add_argument("--file", action="append", required=True)
    p.add_argument("--chunk-size", type=int, default=65536, help="读取块长（字节）")
    p.add_argument("--commit-every", type=int, default=0,
                   help="每多少帧提交一次进度（0=整批一次提交）")
    p.add_argument("--progress", action="store_true",
                   help="每次中间提交后打印一行 PROGRESS（供崩溃演示观察）")
    p.set_defaults(fn=_cmd_ingest)

    p = sub.add_parser("samples", help="按设备/通道/时间段查询样本")
    p.add_argument("--db", required=True)
    p.add_argument("--device", type=int)
    p.add_argument("--channel", type=int)
    p.add_argument("--start-ns", type=int)
    p.add_argument("--end-ns", type=int)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=_cmd_samples)

    p = sub.add_parser("trace", help="由 (file-id, 帧偏移) 回溯测量值")
    p.add_argument("--db", required=True)
    p.add_argument("--file-id", type=int, required=True)
    p.add_argument("--offset", type=int, required=True)
    p.set_defaults(fn=_cmd_trace)

    p = sub.add_parser("frames", help="帧处置台账")
    p.add_argument("--db", required=True)
    p.add_argument("--file-id", type=int)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=_cmd_frames)

    p = sub.add_parser("rejections", help="拒收明细")
    p.add_argument("--db", required=True)
    p.add_argument("--device", type=int)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=_cmd_rejections)

    p = sub.add_parser("state", help="设备周期状态与文件续读进度")
    p.add_argument("--db", required=True)
    p.set_defaults(fn=_cmd_state)

    p = sub.add_parser("query", help="直接执行 SQL")
    p.add_argument("--db", required=True)
    p.add_argument("--sql", required=True)
    p.set_defaults(fn=_cmd_query)

    p = sub.add_parser("crash-demo", help="崩溃恢复演示（SIGKILL + 重启续传）")
    p.add_argument("--workdir", required=True)
    p.add_argument("--count", type=int, default=2000)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--chunk-size", type=int, default=4096)
    p.add_argument("--commit-every", type=int, default=200)
    p.set_defaults(fn=_cmd_crash_demo)

    p = sub.add_parser("demo", help="一条命令跑完全部演示")
    p.add_argument("--workdir", required=True)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--count", type=int, default=2000)
    p.add_argument("--chunk-size", type=int, default=65536)
    p.set_defaults(fn=_cmd_demo)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
