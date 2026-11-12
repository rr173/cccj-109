"""折线压缩归档命令行入口（``python3 -m telemetry.archcli``）。

子命令
------
- ``build``    从 DuckDB tele_samples 构建 TPLA 归档（设备/通道/纳秒范围/容差）
- ``summary``  归档摘要：各组点数、线段数、参数区字节与口径说明
- ``decode``   完整解码（逐点：时刻、定位键、近似值、单位、所属线段）
- ``query``    时间范围查询：只读取相关块，并报告实际读取块数
- ``verify``   独立核验：单独解码 + 回库逐点误差枚举 + 范围交叉核对
- ``demo``     一条命令跑通：材料生成 → 接入 → 归档 → 核验 → 损坏演示
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import archdemo
from .archbuild import ArchiveBuilder
from .archfmt import ArchiveError
from .archread import ArchiveReader
from .archverify import verify_archive
from .errors import IngestError
from .ingest import Ingester


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


def _connect_ro(db: str):
    import duckdb
    try:
        return duckdb.connect(str(db), read_only=True)
    except Exception as e:
        print(f"无法打开数据库 {db}：{e}", file=sys.stderr)
        sys.exit(2)


def _print_summary(s) -> None:
    print(f"设备 {s.device} 通道 {s.channel}  eps={s.epsilon!r}  "
          f"范围 [{s.t_start_ns}, {s.t_end_ns})")
    print(f"点数 {s.points}，线段 {s.segments}，块 {s.blocks}，"
          f"文件 {s.file_bytes} 字节")
    if s.groups:
        _print_table(
            ["group", "boot_seq", "layout_id", "unit", "points", "segments",
             "param_bytes", "blocks"],
            [(g.group_id, g.boot_seq, g.layout_id, g.unit, g.points,
              g.segments, g.param_bytes, g.block_count) for g in s.groups])
    ratio = s.value_param_ratio
    ratio_s = f"{ratio:.3f}" if ratio is not None else "—（无点）"
    print(f"物理值参数区 {s.param_bytes} B  vs  原样存值 {s.raw_value_bytes} B"
          f"（8B/点）→ 值参数比 {ratio_s}")
    print(f"逐点时刻+定位键 {s.point_bytes} B（22B/点，精确时刻与来源回溯必需，"
          f"不计入压缩收益）；块结构开销 {s.block_overhead_bytes} B；"
          f"头/索引/目录/尾 {s.meta_bytes} B")


# ----------------------------------------------------------------------
# build / summary / decode / query / verify
# ----------------------------------------------------------------------

def _cmd_build(args) -> int:
    con = _connect_ro(args.db)
    try:
        builder = ArchiveBuilder(
            args.out, device=args.device, channel=args.channel,
            t_start_ns=args.start_ns, t_end_ns=args.end_ns,
            epsilon=args.epsilon, block_points=args.block_points,
            batch_size=args.batch_size)
        stats = builder.build(con)
    except ValueError as e:
        print(f"参数非法：{e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"构建失败：{e}", file=sys.stderr)
        return 2
    finally:
        con.close()
    with ArchiveReader(args.out) as reader:
        _print_summary(reader.summary())
    print(f"已写出 {stats.path}（{stats.file_bytes} 字节）")
    return 0


def _cmd_summary(args) -> int:
    try:
        with ArchiveReader(args.file) as reader:
            _print_summary(reader.summary())
    except ArchiveError as e:
        print(f"归档损坏：{e}", file=sys.stderr)
        return 4
    return 0


def _cmd_decode(args) -> int:
    try:
        with ArchiveReader(args.file) as reader:
            rows = []
            for i, r in enumerate(reader.decode(group_id=args.group)):
                if i >= args.limit:
                    break
                rows.append((r.group_id, r.boot_seq, r.layout_id, r.ts_ns,
                             f"{r.value:.9g}", r.unit, r.file_id,
                             r.frame_offset, r.ordinal, r.seg_seq))
            _print_table(["group", "boot_seq", "layout_id", "ts_ns", "value≈",
                          "unit", "file_id", "frame_offset", "ordinal", "seg"],
                         rows)
    except ArchiveError as e:
        print(f"归档损坏：{e}", file=sys.stderr)
        return 4
    return 0


def _cmd_query(args) -> int:
    try:
        with ArchiveReader(args.file) as reader:
            res = reader.query(args.start_ns, args.end_ns, group_id=args.group)
    except ArchiveError as e:
        print(f"归档损坏：{e}", file=sys.stderr)
        return 4
    except ValueError as e:
        print(f"参数非法：{e}", file=sys.stderr)
        return 2
    print(f"范围 [{args.start_ns}, {args.end_ns})：命中 {len(res.rows)} 点，"
          f"实际读取块 {res.blocks_read} / {res.blocks_total}")
    _print_table(["group", "ts_ns", "value≈", "unit", "file_id",
                  "frame_offset", "ordinal", "seg"],
                 [(r.group_id, r.ts_ns, f"{r.value:.9g}", r.unit, r.file_id,
                   r.frame_offset, r.ordinal, r.seg_seq)
                  for r in res.rows[:args.limit]])
    return 0


def _cmd_verify(args) -> int:
    try:
        rep = verify_archive(args.file, args.db)
    except ArchiveError as e:
        print(f"归档损坏：{e}", file=sys.stderr)
        return 4
    print(f"核验 {rep.path}：设备 {rep.device} 通道 {rep.channel}，"
          f"eps={rep.epsilon!r}")
    for g in rep.groups:
        print(f"  组 {g.group_id}（boot={g.boot_seq} layout={g.layout_id} "
              f"{g.unit}）：{g.points} 点 {g.segments} 段，"
              f"f64 最大误差 {g.max_abs_err:.3e}，"
              f"精确最大误差 {float(g.max_exact_err):.3e}")
    print(f"  逐点核对 {rep.points_checked} 点；范围交叉探测 "
          f"{rep.range_probes} 个（总块数 {rep.blocks_total}）")
    if rep.ok:
        print("核验通过：逐点误差均在容差内，定位键/时刻与库中一致，"
              "范围查询与完整解码一致")
        return 0
    print(f"核验失败（{len(rep.violations)} 项）：", file=sys.stderr)
    for v in rep.violations[:20]:
        print(f"  - {v}", file=sys.stderr)
    return 1


# ----------------------------------------------------------------------
# demo
# ----------------------------------------------------------------------

def _cmd_demo(args) -> int:
    workdir = Path(args.workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    data_dir = workdir / "data"
    data_dir.mkdir(parents=True)
    db = workdir / "archive_demo.duckdb"

    print("== 1. 生成演示材料并接入 ==")
    mats = archdemo.build_materials()
    for name, content in mats.items():
        (data_dir / name).write_bytes(content)
    with Ingester(db) as ing:
        for name in sorted(mats):
            st = ing.ingest_file(data_dir / name)
            print(f"  {name}: 应用 {st.frames_applied} 帧，"
                  f"样本 {st.samples_added}，拒收 {st.frames_rejected}")

    print("\n== 2. 逐材料构建归档并核验 ==")
    archives = {}
    for spec in archdemo.SPECS:
        out = workdir / f"{spec.name}.tpla"
        con = _connect_ro(str(db))
        try:
            ArchiveBuilder(out, spec.device, spec.channel,
                           archdemo.T_START, archdemo.T_END, spec.epsilon,
                           block_points=spec.block_points).build(con)
        finally:
            con.close()
        archives[spec.name] = out
        print(f"\n-- {spec.name}：{spec.note}")
        with ArchiveReader(out) as reader:
            _print_summary(reader.summary())
        rep = verify_archive(out, db)
        status = "通过" if rep.ok else "失败"
        print(f"独立核验：{status}（{rep.points_checked} 点，"
              f"{rep.range_probes} 个范围探测）")
        if not rep.ok:
            for v in rep.violations[:10]:
                print(f"  - {v}", file=sys.stderr)
            return 1

    print("\n== 3. 窄时间查询只读相关块（ramp 归档） ==")
    with ArchiveReader(archives["ramp"]) as reader:
        all_rows = list(reader.decode())
        pivot = all_rows[len(all_rows) // 2].ts_ns     # 取一个真实采样时刻
        t0, t1 = pivot - 2_000_000, pivot + 3_000_000  # ±几毫秒的窄窗口
        res = reader.query(t0, t1)
        print(f"  窗口 [{t0}, {t1})：{len(res.rows)} 点，"
              f"读取块 {res.blocks_read} / {res.blocks_total}")
        full = [r for r in all_rows if t0 <= r.ts_ns < t1]
        assert [r.ts_ns for r in res.rows] == [r.ts_ns for r in full]
        assert len(res.rows) > 0 and res.blocks_read < res.blocks_total
        print("  与完整解码过滤结果一致 ✓，且未读取无关块 ✓")

    print("\n== 4. 损坏与截断检测 ==")
    good = archives["ramp"]
    raw = bytearray(good.read_bytes())
    flip_at = len(raw) // 2
    raw[flip_at] ^= 0xFF
    bad = workdir / "corrupt.tpla"
    bad.write_bytes(bytes(raw))
    try:
        with ArchiveReader(bad) as reader:
            list(reader.decode())
        print("  损坏块未被检出！", file=sys.stderr)
        return 1
    except ArchiveError as e:
        print(f"  翻转一字节 → 完整解码明确报错：{e}")
    trunc = workdir / "truncated.tpla"
    trunc.write_bytes(good.read_bytes()[: len(raw) * 3 // 4])
    try:
        with ArchiveReader(trunc):
            pass
        print("  截断文件未被检出！", file=sys.stderr)
        return 1
    except ArchiveError as e:
        print(f"  截断到 3/4 → 打开即明确报错：{e}")

    print(f"\n全部演示完成。工作目录：{workdir}")
    return 0


# ----------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="telemetry.archcli",
        description="tele_samples 折线压缩归档（绝对误差上限）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="从 DuckDB 构建归档")
    p.add_argument("--db", required=True)
    p.add_argument("--device", type=int, required=True)
    p.add_argument("--channel", type=int, required=True)
    p.add_argument("--start-ns", type=int, required=True)
    p.add_argument("--end-ns", type=int, required=True)
    p.add_argument("--epsilon", type=float, required=True,
                   help="绝对误差上限（该通道最终物理单位），正的有限值")
    p.add_argument("--out", required=True)
    p.add_argument("--block-points", type=int, default=4096,
                   help="单块目标点数（默认 4096）")
    p.add_argument("--batch-size", type=int, default=8192,
                   help="键集分页批大小（默认 8192）")
    p.set_defaults(fn=_cmd_build)

    p = sub.add_parser("summary", help="归档摘要")
    p.add_argument("--file", required=True)
    p.set_defaults(fn=_cmd_summary)

    p = sub.add_parser("decode", help="完整解码")
    p.add_argument("--file", required=True)
    p.add_argument("--group", type=int)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=_cmd_decode)

    p = sub.add_parser("query", help="时间范围查询（只读相关块）")
    p.add_argument("--file", required=True)
    p.add_argument("--start-ns", type=int, required=True)
    p.add_argument("--end-ns", type=int, required=True)
    p.add_argument("--group", type=int)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=_cmd_query)

    p = sub.add_parser("verify", help="独立核验（单独解码 + 回库逐点比对）")
    p.add_argument("--file", required=True)
    p.add_argument("--db", required=True)
    p.set_defaults(fn=_cmd_verify)

    p = sub.add_parser("demo", help="一条命令跑完全部演示")
    p.add_argument("--workdir", required=True)
    p.set_defaults(fn=_cmd_demo)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except IngestError as e:
        print(f"接入失败：{e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
