"""对齐结果的输出格式（表格与说明文本）。

输出绝不把未对齐行当作"窗外"或"已对齐"：每条未对齐测量都带具体原因；
窗口模式单独统计未对齐数量及原因。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Dict, List, Sequence

from .aligner import AlignedRow
from .mapping import MODE_EXTRAP_LEFT, MODE_EXTRAP_RIGHT, MODE_INTERP

MODE_LABEL = {
    MODE_INTERP: "interp",
    MODE_EXTRAP_LEFT: "extrap-left",
    MODE_EXTRAP_RIGHT: "extrap-right",
}


def fmt_fraction(v: Fraction) -> str:
    """有理数的可读形式：整数或 ``n/d``（保留精确值便于手算核对）。"""
    if v.denominator == 1:
        return str(v.numerator)
    return f"{v.numerator}/{v.denominator}"


def print_table(headers: Sequence[str], rows: Sequence[Sequence]) -> None:
    cols = [str(h) for h in headers]
    data = [["" if v is None else str(v) for v in r] for r in rows]
    widths = [len(c) for c in cols]
    for r in data:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(v))
    print("  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)))
    print("  ".join("-" * w for w in widths))
    for r in data:
        print("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))
    print(f"（{len(data)} 行）")


def _row_cells(r: AlignedRow) -> List:
    if r.aligned:
        lo, mid, hi = r.lower_ns, r.corrected_ns, r.upper_ns
        return [r.ref.file_id, r.ref.frame_offset, r.ref.ordinal,
                r.ref.channel, r.device_id, r.boot_seq, r.ts_ns, mid,
                lo, hi, fmt_fraction(r.correction.center),
                fmt_fraction(r.correction.radius),
                MODE_LABEL.get(r.mode, r.mode), ",".join(r.anchors_used),
                r.value, r.unit, ""]
    return [r.ref.file_id, r.ref.frame_offset, r.ref.ordinal, r.ref.channel,
            r.device_id, r.boot_seq, r.ts_ns, None, None, None, None, None,
            None, "", r.value, r.unit, r.reason]


HEADERS = ["file_id", "frame_offset", "ordinal", "channel", "device", "boot",
           "local_ns", "corrected_ns", "lower_ns", "upper_ns",
           "center(y)", "radius(u)", "mode", "anchors_used", "value", "unit",
           "reason"]


def print_rows(rows: Sequence[AlignedRow], show_value: bool = True) -> None:
    headers = list(HEADERS)
    data = []
    for r in rows:
        cells = _row_cells(r)
        if not show_value:
            cells = cells[:-3] + [cells[-1]]
            headers = headers[:-3] + [headers[-1]]
        data.append(cells)
    print_table(headers, data)


def print_reasons_summary(count: int, by_reason: Dict[str, int],
                          examples: Sequence[AlignedRow],
                          note: str = "") -> None:
    """未对齐统计 + 前 10 条示例；不把未对齐行当作窗外或已对齐。"""
    print(f"\n未对齐的 {count} 条测量不能当作窗外；原因：")
    for reason in sorted(by_reason):
        print(f"  - {reason}（{by_reason[reason]} 条）")
    if note:
        print(note)
    if examples:
        print("  示例（前 10 条）：")
        print_table(
            ["file_id", "frame_offset", "ordinal", "channel", "device",
             "boot", "local_ns", "value", "unit", "reason"],
            [[r.ref.file_id, r.ref.frame_offset, r.ref.ordinal,
              r.ref.channel, r.device_id, r.boot_seq, r.ts_ns,
              r.value, r.unit, r.reason] for r in examples])
    print("  未对齐行不输出 corrected_ns；不能用本地时刻冒充参考钟。")
