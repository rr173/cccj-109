"""``python3 -m alignment.cli``：离线时钟对齐工具的公开命令。

子命令
------
- ``project``  展示选定设备的完整投影：每条测量一行（含未对齐原因），
               并列出各锚点组是否可用及原因；可带本地时间段筛选。
- ``window``   按参考钟闭窗口 [L, R] 选取测量：校正范围与窗口相交即保留
               （不只看中心）；同时报告有多少测量未对齐、为什么无法判断。
- ``anchors``  展示锚点组的校验结果（折叠、递增、斜率），便于排查输入。
- ``demo``     一条命令跑通示例与全部回归核对。

``project`` / ``window`` 支持 ``--json``：每行一条 JSON，含精确的有理数
中心/半径、舍入后的三个整数、模式与所用 anchor_id，便于自动核对。

退出码：0 成功；锚点输入或配置非法时 2；测量库无法只读打开时 1。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__
from .aligner import (AlignDbError, AlignedRow, align_rows, connect_read_only,
                      iter_samples)
from .anchors import (AnchorInputError, AnchorSet, build_anchor_set,
                      read_anchor_csv)
from .mapping import AlignmentConfigError, Mapping, finalize_groups
from .report import (MODE_LABEL, fmt_fraction,
                     print_reasons_summary, print_rows, print_table)


def _load(anchor_path: str, max_ppm: int):
    """读取锚点、折叠分组、执行斜率检验；返回 (AnchorSet, mappings, 提示)。"""
    raw, warnings = read_anchor_csv(anchor_path)
    aset = build_anchor_set(raw)
    finalize_groups(aset.groups, max_ppm)
    mappings: Dict[Tuple[int, int], Mapping] = {}
    for key, g in aset.groups.items():
        if g.valid:
            mappings[key] = Mapping(g, max_ppm)
    return aset, mappings, warnings


def _print_group_status(aset: AnchorSet) -> None:
    rows = []
    for key, g in sorted(aset.groups.items()):
        ids = sorted(aid for a in g.anchors for aid in a.anchor_ids)
        rows.append([g.device_id, g.boot_seq, g.n_raw, g.n_collapsed,
                     "有效" if g.valid else "无效",
                     g.invalid_reason or "", ",".join(ids)])
    print_table(["device", "boot", "raw_anchors", "collapsed", "status",
                 "reason", "anchor_ids"], rows)


def _anchor_rows(aset: AnchorSet) -> None:
    for key, g in sorted(aset.groups.items()):
        print(f"\n-- 锚点组 device={g.device_id} boot={g.boot_seq}: "
              f"{'有效' if g.valid else '无效 — ' + str(g.invalid_reason)}")
        print_table(["anchor_ids", "local_ns", "reference_ns", "error_ns"],
                    [[",".join(a.anchor_ids), a.local_ns, a.reference_ns,
                      a.error_ns] for a in g.anchors])


def _json_row(r: AlignedRow) -> dict:
    base = {
        "file_id": r.ref.file_id, "frame_offset": r.ref.frame_offset,
        "ordinal": r.ref.ordinal, "channel": r.ref.channel,
        "device_id": r.device_id, "boot_seq": r.boot_seq,
        "layout_id": r.layout_id, "local_ns": r.ts_ns,
        "value": r.value, "unit": r.unit,
    }
    if r.reason:
        base.update({"aligned": False, "reason": r.reason,
                     "corrected_ns": None, "lower_ns": None,
                     "upper_ns": None, "center": None, "radius": None,
                     "mode": None, "anchors_used": None})
        return base
    base.update({
        "aligned": True,
        "corrected_ns": r.corrected_ns,
        "lower_ns": r.lower_ns,
        "upper_ns": r.upper_ns,
        "center": fmt_fraction(r.correction.center),
        "radius": fmt_fraction(r.correction.radius),
        "mode": r.mode,
        "mode_label": MODE_LABEL.get(r.mode, r.mode),
        "anchors_used": list(r.anchors_used),
        "segment": list(r.correction.segment),
        "distance_beyond_ns": r.correction.distance_beyond_ns,
    })
    return base


def _iter_aligned(con, args, aset, mappers):
    """流式读取并逐批映射；每批只保留在内存中处理。"""
    for batch in iter_samples(con, batch=args.batch, device_id=args.device,
                              boot_seq=args.boot_seq,
                              local_lo=args.local_start_ns,
                              local_hi=args.local_end_ns):
        yield align_rows(batch, aset, mappers, args.max_extrapolation_ns)


def _emit(rows: List[AlignedRow], args) -> None:
    if args.json:
        for r in rows:
            print(json.dumps(_json_row(r), ensure_ascii=False, sort_keys=True))
    else:
        print_rows(rows, show_value=not args.no_value)


def _cmd_anchors(args) -> int:
    try:
        aset, _, warnings = _load(args.anchors, args.max_ppm)
    except AnchorInputError as e:
        print(f"锚点输入被拒绝：{e}", file=sys.stderr)
        return 2
    except AlignmentConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    for w in warnings:
        print(f"提示：{w}")
    print(f"锚点行 {aset.n_rows} 条，分组 {len(aset.groups)} 个：")
    _print_group_status(aset)
    _anchor_rows(aset)
    return 0


def _open_db(path: str):
    try:
        return connect_read_only(path), None
    except (AlignDbError, Exception) as e:   # duckdb 异常类型随版本而异
        return None, f"无法只读打开测量库 {path}：{e}"


def _cmd_project(args) -> int:
    try:
        aset, mappers, warnings = _load(args.anchors, args.max_ppm)
    except (AnchorInputError, AlignmentConfigError) as e:
        print(f"锚点输入/配置被拒绝：{e}", file=sys.stderr)
        return 2
    for w in warnings:
        print(f"提示：{w}", file=sys.stderr)
    con, err = _open_db(args.db)
    if con is None:
        print(err, file=sys.stderr)
        return 1
    try:
        if not args.json:
            print(f"设备 {args.device if args.device is not None else '全部'} / "
                  f"周期 {args.boot_seq if args.boot_seq is not None else '全部'} "
                  f"的完整投影（max_ppm={args.max_ppm}，"
                  f"max_extrapolation_ns={args.max_extrapolation_ns}）")
            print("\n锚点组状态：")
            _print_group_status(aset)
            lo = args.local_start_ns if args.local_start_ns is not None else "-inf"
            hi = args.local_end_ns if args.local_end_ns is not None else "+inf"
            print(f"\n投影（本地时间段 [{lo}, {hi}]）：")
        rows_total = 0
        bad_count = 0
        bad_by_reason: Dict[str, int] = {}
        bad_examples: List[AlignedRow] = []
        for rows in _iter_aligned(con, args, aset, mappers):
            rows_total += len(rows)
            for r in rows:
                if not r.aligned:
                    bad_count += 1
                    bad_by_reason[r.reason] = bad_by_reason.get(r.reason, 0) + 1
                    if len(bad_examples) < 10:
                        bad_examples.append(r)
            _emit(rows, args)
        if not args.json:
            print(f"\n共 {rows_total} 条测量，其中未对齐 {bad_count} 条。")
        if bad_count and not args.json:
            print_reasons_summary(bad_count, bad_by_reason, bad_examples)
        elif bad_count and args.json:
            print(json.dumps({"unaligned_summary": {
                "count": bad_count, "reasons": bad_by_reason}},
                ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 0
    finally:
        con.close()


def _cmd_window(args) -> int:
    if args.low_ns > args.high_ns:
        print("参考钟窗口非法：L 必须 <= R", file=sys.stderr)
        return 2
    try:
        aset, mappers, warnings = _load(args.anchors, args.max_ppm)
    except (AnchorInputError, AlignmentConfigError) as e:
        print(f"锚点输入/配置被拒绝：{e}", file=sys.stderr)
        return 2
    for w in warnings:
        print(f"提示：{w}", file=sys.stderr)
    con, err = _open_db(args.db)
    if con is None:
        print(err, file=sys.stderr)
        return 1
    try:
        bad_by_reason: Dict[str, int] = {}
        bad_examples: List[AlignedRow] = []
        n_total = 0
        n_bad = 0
        n_kept = 0
        emitted_header = False
        if not args.json:
            print(f"参考钟窗口 [{args.low_ns}, {args.high_ns}]（闭区间，"
                  f"校正范围 [lower, upper] 相交即保留）")
        for rows in _iter_aligned(con, args, aset, mappers):
            n_total += len(rows)
            kept: List[AlignedRow] = []
            for r in rows:
                hit = r.within_window(args.low_ns, args.high_ns)
                if hit is None:
                    n_bad += 1
                    bad_by_reason[r.reason] = bad_by_reason.get(r.reason, 0) + 1
                    if len(bad_examples) < 10:
                        bad_examples.append(r)
                elif hit:
                    kept.append(r)
                # 未判断的行（hit is False）直接丢弃，不保留在内存
            n_kept += len(kept)
            if kept:
                if not emitted_header and not args.json:
                    print()                          # 与摘要行分开
                emitted_header = True
                _emit(kept, args)
        if not args.json:
            print(f"扫描测量 {n_total} 条，窗口内 {n_kept} 条，"
                  f"未对齐/无法判断 {n_bad} 条。")
            if n_kept == 0:
                print("（窗口内没有可判断的测量）")
        if n_bad and not args.json:
            print_reasons_summary(
                n_bad, bad_by_reason, bad_examples,
                note="\n这些测量的参考钟时刻无法由所给锚点推断，窗口归属未知。")
        elif n_bad and args.json:
            print(json.dumps({"unaligned_summary": {
                "count": n_bad, "reasons": bad_by_reason}},
                ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 0
    finally:
        con.close()


def _cmd_demo(args) -> int:
    from . import demo
    return demo.run_demo(Path(args.workdir), args.seed)


def _validate_common(args) -> Optional[str]:
    if getattr(args, "max_ppm", 0) < 0:
        return "--max-ppm 必须为非负整数"
    if getattr(args, "max_extrapolation_ns", 0) < 0:
        return "--max-extrapolation-ns 必须为非负整数"
    if getattr(args, "batch", 1) <= 0:
        return "--batch 必须为正整数"
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="alignment",
        description="离线时钟对齐（读取真实测量 + 锚点 CSV → 共同参考钟）")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--db", required=True)
        p.add_argument("--anchors", required=True, help="锚点 CSV")
        p.add_argument("--max-ppm", type=int, default=1000,
                       help="斜率偏离 1 的最大 ppm（非负整数）")
        p.add_argument("--max-extrapolation-ns", type=int, default=10 ** 9,
                       help="允许外推的最大距离（纳秒，非负整数）")
        p.add_argument("--device", type=int)
        p.add_argument("--boot-seq", type=int)
        p.add_argument("--local-start-ns", type=int)
        p.add_argument("--local-end-ns", type=int)
        p.add_argument("--batch", type=int, default=4096,
                       help="每批读取的测量行数（内存上限与它成正比）")
        p.add_argument("--no-value", action="store_true",
                       help="省略物理值/单位列")
        p.add_argument("--json", action="store_true",
                       help="逐行输出 JSON（含精确有理数中心/半径）")

    p = sub.add_parser("project", help="展示选定设备的完整投影与未对齐原因")
    common(p)
    p.set_defaults(fn=_cmd_project)

    p = sub.add_parser("window", help="按参考钟窗口 [L,R] 选取测量（范围相交）")
    common(p)
    p.add_argument("--low-ns", type=int, required=True, help="窗口下界 L（含）")
    p.add_argument("--high-ns", type=int, required=True, help="窗口上界 R（含）")
    p.set_defaults(fn=_cmd_window)

    p = sub.add_parser("anchors", help="展示锚点组校验结果")
    p.add_argument("--anchors", required=True)
    p.add_argument("--max-ppm", type=int, default=1000)
    p.set_defaults(fn=_cmd_anchors)

    p = sub.add_parser("demo", help="运行示例与全部回归核对")
    p.add_argument("--workdir", required=True)
    p.add_argument("--seed", type=int, default=1)
    p.set_defaults(fn=_cmd_demo)

    args = ap.parse_args(argv)
    if hasattr(args, "max_ppm"):
        msg = _validate_common(args)
        if msg:
            print(msg, file=sys.stderr)
            return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
