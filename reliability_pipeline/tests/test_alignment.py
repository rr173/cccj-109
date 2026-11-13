"""时钟对齐工具的专项回归。

期望值全部由本文件用 ``Fraction`` 与显式公式独立推导（不导入被测的
``alignment.mapping``）；测量材料尽量经**公开的遥测接入命令**入库，
另有一组直接建库的小例子用于精确控制边界（巨大基数、1 ns 间隔、
同刻多源）。

覆盖：
1. 锚点 CSV 输入校验（非法行、重复 anchor_id、表头、负误差）；
2. 同刻折叠与矛盾锚点整组失效、严格递增、至少两点、斜率 ppm；
3. 内插/锚点自身/左右外推的中心与误差传播（有理数手算）；
4. 外推额度边界（恰好允许 / 超 1 ns 拒绝）、窗口范围相交而非只看中心；
5. 巨大正负时间基数下相邻 1 ns 不被浮点吞掉；
6. 同设备换周期、重复时间多来源保留、输入重排稳定；
7. 只读库（不建表、不回写）、公开命令输出可回到真实测量行；
8. 原有 telemetry / startup / pipeline 入口不受影响（见各自测试文件）。
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from alignment import aligner, anchors as anchors_mod, mapping as mapping_mod
from telemetry import store as tele_store
from telemetry.ingest import Ingester

REPO_ROOT = Path(__file__).resolve().parent.parent
E0 = 1_700_000_000_000_000_000
SEC = 1_000_000_000
BIG = 9_000_000_000_000_000_000
MAX_PPM = 1000
MAX_EXTRAP = 1_000_000_000

MIXED = [(0, 1, 1.0, 0.0, 1), (1, 2, 1.0, 0.0, 2), (2, 3, 1.0, 0.0, 3)]

HEADER = ("anchor_id,device_id,boot_seq,local_ns,reference_ns,error_ns\n")


# ----------------------------------------------------------------------
# 帮助函数：独立手算（只依赖 Fraction 与显式公式）
# ----------------------------------------------------------------------

def manual_points(rows, dev=None, boot=None):
    """[(x, y, e, ids)]，同刻按 (y,e) 折叠；本函数不检查合法性。

    ``dev``/``boot`` 给出时只取该组（对应工具按 (device_id, boot_seq) 分组）。
    """
    by_x = {}
    for aid, d, b, x, y, e in rows:
        if dev is not None and d != dev:
            continue
        if boot is not None and b != boot:
            continue
        by_x.setdefault(x, []).append((aid, y, e))
    out = {}
    for x, group in by_x.items():
        ys = {g[1] for g in group}
        es = {g[2] for g in group}
        if len(ys) != 1 or len(es) != 1:
            return None
        out[x] = (Fraction(group[0][1]), Fraction(group[0][2]),
                  tuple(sorted(g[0] for g in group)))
    return [(Fraction(x),) + out[x] for x in sorted(out)]


def manual_map(points, x, max_ppm, max_extrap):
    """手算分段映射；返回 (center, radius, mode, ids, d) 或抛 ValueError。"""
    x = Fraction(x)
    n = len(points)
    xs = [p[0] for p in points]
    if x <= xs[0]:
        i0, i1, d, mode = 0, 1, xs[0] - x, "extrapolate_left"
    elif x >= xs[-1]:
        i0, i1 = n - 2, n - 1
        d = x - xs[-1]
        mode = "extrapolate_right"
    else:
        # 内插：找到 x 所在段
        i0 = max(i for i in range(n - 1) if xs[i] <= x)
        i1 = i0 + 1
        d = Fraction(0)
        mode = "interp"
    x0, y0, e0, ids0 = points[i0]
    x1, y1, e1, ids1 = points[i1]
    if x == x0 or x == x1:
        idx = i0 if x == x0 else i1
        p = points[idx]
        return p[1], p[2], "interp", p[3], Fraction(0)
    if d > max_extrap:
        raise ValueError(f"超出外推额度：d={d} > {max_extrap}")
    a = (x - x0) / (x1 - x0)
    center = (1 - a) * y0 + a * y1
    if d == 0:
        radius = (1 - a) * e0 + a * e1
    else:
        radius = (abs(1 - a) * e0 + abs(a) * e1
                  + d * Fraction(max_ppm, 1_000_000))
    return center, radius, mode, ids0 + ids1, d


def rounded(center, radius):
    return (math.floor(center), math.floor(center - radius),
            math.ceil(center + radius))


def write_anchors(path: Path, rows, header=HEADER):
    path.write_text(header + "".join(",".join(str(v) for v in r) + "\n"
                                     for r in rows), encoding="utf-8")


def _build(dev, boot, epoch, hz, ticks, spike_tick=-1):
    from telemetry.generator import StreamBuilder
    b = StreamBuilder()
    b.boot(dev, boot, epoch, hz)
    b.layout(dev, 1, MIXED)
    for tick in ticks:
        p = 150_000 if tick == spike_tick else 1_000
        b.meas(dev, 1, tick, [25_000, p, 1_000])
    return b.bytes()


def ingest(tmp_path, name, data):
    f = tmp_path / name
    f.write_bytes(data)
    return f


# ----------------------------------------------------------------------
# 1. 输入校验
# ----------------------------------------------------------------------

def test_csv_structure_errors_are_file_level(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("anchor_id,device_id,local_ns\n", encoding="utf-8")
    with pytest.raises(anchors_mod.AnchorFileError) as ei:
        anchors_mod.read_anchor_csv(p)
    assert "表头" in str(ei.value)

    p.write_text("anchor_id,anchor_id,device_id,boot_seq,local_ns,"
                 "reference_ns,error_ns\n", encoding="utf-8")
    with pytest.raises(anchors_mod.AnchorFileError):
        anchors_mod.read_anchor_csv(p)

    p.write_text("", encoding="utf-8")
    with pytest.raises(anchors_mod.AnchorFileError):
        anchors_mod.read_anchor_csv(p)


def test_row_errors_rejected_with_line_numbers(tmp_path):
    p = tmp_path / "bad.csv"
    write_anchors(p, [])   # 只有表头
    p.write_text(HEADER + "\n".join([
        "a,1,1,0,10,0",
        "a,1,1,5,15,0",            # 重复 anchor_id
        "b,1,1,1.0,20,0",          # 浮点
        "c,1,1,10,20,-1",          # 负误差
        "d,1,1,20,30",             # 字段数不足
        "e,1,1,,30,0",             # 空字段
        "f,-1,1,0,10,0",           # 负 device_id
    ]) + "\n", encoding="utf-8")
    with pytest.raises(anchors_mod.AnchorRowError) as ei:
        anchors_mod.read_anchor_csv(p)
    text = "\n".join(ei.value.problems)
    assert "第 3 行" in text and "重复 anchor_id" in text
    assert "第 4 行" in text and "local_ns" in text
    assert "第 5 行" in text and "error_ns" in text
    assert "第 6 行" in text and "字段数" in text
    assert "第 7 行" in text and "为空" in text
    assert "第 8 行" in text and "device_id" in text


def test_anchor_id_is_text_identity(tmp_path):
    p = tmp_path / "ids.csv"
    write_anchors(p, [("001", 1, 1, 0, 10, 0), ("01", 1, 1, 10, 20, 0),
                      ("002", 1, 1, 20, 30, 0)])
    raw, _ = anchors_mod.read_anchor_csv(p)
    aset = anchors_mod.build_anchor_set(raw)
    g = aset.get(1, 1)
    assert g.valid
    assert g.anchors[0].anchor_ids == ("001",)


# ----------------------------------------------------------------------
# 2. 折叠 / 矛盾 / 递增 / 数量 / 斜率
# ----------------------------------------------------------------------

def test_same_local_collapse_keeps_all_ids():
    raw = [anchors_mod.RawAnchor("a", 1, 1, 0, 10, 0, 2),
           anchors_mod.RawAnchor("b", 1, 1, 0, 10, 0, 3),
           anchors_mod.RawAnchor("c", 1, 1, 10, 20, 1, 4)]
    aset = anchors_mod.build_anchor_set(raw)
    g = aset.get(1, 1)
    assert g.valid and g.n_collapsed == 2
    assert g.anchors[0].anchor_ids == ("a", "b")
    mapping_mod.finalize_groups(aset.groups, 1000)
    assert g.valid


def test_conflicting_same_local_invalidates_whole_group():
    raw = [anchors_mod.RawAnchor("a", 1, 1, 0, 10, 0, 2),
           anchors_mod.RawAnchor("b", 1, 1, 0, 10, 1, 3),   # 误差不同
           anchors_mod.RawAnchor("c", 1, 1, 10, 20, 0, 4)]
    aset = anchors_mod.build_anchor_set(raw)
    g = aset.get(1, 1)
    assert not g.valid and "不一致" in g.invalid_reason
    assert "a, b" in g.invalid_reason
    with pytest.raises(ValueError):
        mapping_mod.Mapping(g, 1000)


def test_single_anchor_and_non_increasing_rejected():
    raw = [anchors_mod.RawAnchor("only", 1, 1, 0, 10, 0, 2)]
    aset = anchors_mod.build_anchor_set(raw)
    assert not aset.get(1, 1).valid
    assert "至少需要 2 个" in aset.get(1, 1).invalid_reason

    raw = [anchors_mod.RawAnchor("a", 1, 1, 0, 10, 0, 2),
           anchors_mod.RawAnchor("b", 1, 1, 10, 10, 0, 3)]   # 参考不递增
    aset = anchors_mod.build_anchor_set(raw)
    assert not aset.get(1, 1).valid
    assert "参考坐标未严格递增" in aset.get(1, 1).invalid_reason

    raw = [anchors_mod.RawAnchor("a", 1, 1, 0, 10, 0, 2),
           anchors_mod.RawAnchor("b", 1, 1, -5, 20, 0, 3)]   # 本地不递增（排序后负值在前）
    aset = anchors_mod.build_anchor_set(raw)
    # 排序后 -5 在 0 之前，参考 20 >= 10，这是"参考坐标未严格递增"
    assert not aset.get(1, 1).valid


def test_slope_ppm_exact_boundary():
    # dx=1e6, dy-dx=1 → 1 ppm；上限 1 ppm 允许，0 ppm 不允许
    raw = [anchors_mod.RawAnchor("a", 1, 1, 0, 0, 0, 2),
           anchors_mod.RawAnchor("b", 1, 1, 1_000_000, 1_000_001, 0, 3)]
    aset = anchors_mod.build_anchor_set(raw)
    g = aset.get(1, 1)
    mapping_mod.finalize_groups(aset.groups, 1)
    assert g.valid
    raw2 = [anchors_mod.RawAnchor("a", 1, 1, 0, 0, 0, 2),
            anchors_mod.RawAnchor("b", 1, 1, 1_000_000, 1_000_002, 0, 3)]
    aset2 = anchors_mod.build_anchor_set(raw2)
    mapping_mod.finalize_groups(aset2.groups, 1)
    assert not aset2.get(1, 1).valid
    assert "斜率" in aset2.get(1, 1).invalid_reason

    # 坏段 + 好段：整组无效，绝不跳过坏段继续连线
    raw3 = [anchors_mod.RawAnchor("a", 1, 1, 0, 0, 0, 2),
            anchors_mod.RawAnchor("b", 1, 1, 1_000_000, 1_010_000, 0, 3),
            anchors_mod.RawAnchor("c", 1, 1, 2_000_000, 2_000_000, 0, 4)]
    aset3 = anchors_mod.build_anchor_set(raw3)
    mapping_mod.finalize_groups(aset3.groups, 1000)
    g3 = aset3.get(1, 1)
    assert not g3.valid and "b" in g3.invalid_reason
    # 若实现"跳过坏段"，c 仍会被映射；这里必须找不到 Mapping
    with pytest.raises(ValueError):
        mapping_mod.Mapping(g3, 1000)


# ----------------------------------------------------------------------
# 3. 有理数手算：内插 / 锚点 / 外推
# ----------------------------------------------------------------------

def _mapping_from(rows, max_ppm):
    raw = [anchors_mod.RawAnchor(*r, i + 2) for i, r in enumerate(rows)]
    aset = anchors_mod.build_anchor_set(raw)
    mapping_mod.finalize_groups(aset.groups, max_ppm)
    return aset, aset.get(rows[0][1], rows[0][2])


def test_manual_interp_and_anchor_and_extrap():
    rows = [("a0", 2, 1, 0, 1000, 3),
            ("a1", 2, 1, 3_000_000, 3_000_000 + 30, 5),
            ("a2", 2, 1, 7_000_000, 7_000_000 + 70, 7)]
    aset, g = _mapping_from(rows, 1000)
    pts = manual_points(rows)
    for x in (0, 1, 1_500_000, 3_000_000, 3_000_001, 5_000_000,
              7_000_000, -500_000, 7_500_000):
        exp = manual_map(pts, x, 1000, MAX_EXTRAP)
        got = mapping_mod.Mapping(g, 1000).evaluate(x, MAX_EXTRAP)
        assert got.center == exp[0], x
        assert got.radius == exp[1], x
        assert got.mode == exp[2], x
        assert got.rounded() == rounded(exp[0], exp[1]), x


def test_reason_about_error_radius_not_claiming_more():
    # 外推半径随距离 d 增长：d 越大越宽，且内插半径按 a 加权
    rows = [("a0", 2, 1, 0, 0, 10),
            ("a1", 2, 1, 10, 10, 20)]
    aset, g = _mapping_from(rows, 7)
    m = mapping_mod.Mapping(g, 7)
    r0 = m.evaluate(0, 100)
    r1 = m.evaluate(10, 100)
    assert (r0.radius, r1.radius) == (10, 20)
    mid = m.evaluate(5, 100)
    assert mid.radius == 15 and mid.center == 5
    # a = (x-x0)/(x1-x0) = (-10-0)/10 = -1；|1-a|=2、|a|=1，d=10
    out10 = m.evaluate(-10, 100)
    assert out10.radius == (2 * 10 + 1 * 20
                            + 10 * Fraction(7, 1_000_000))


# ----------------------------------------------------------------------
# 4. 外推额度边界与窗口语义
# ----------------------------------------------------------------------

def test_extrapolation_limit_boundary():
    rows = [("a0", 2, 1, 0, 0, 0), ("a1", 2, 1, 100, 100, 0)]
    aset, g = _mapping_from(rows, 0)
    m = mapping_mod.Mapping(g, 0)
    res = m.evaluate(-MAX_EXTRAP, MAX_EXTRAP)      # 恰好 d == 额度：允许
    assert res.mode == "extrapolate_left"
    with pytest.raises(mapping_mod.OutOfRange):
        m.evaluate(-MAX_EXTRAP - 1, MAX_EXTRAP)     # 超 1 ns：拒绝
    assert m.evaluate(mapping_mod.Mapping(g, 0).x[-1] + MAX_EXTRAP,
                      MAX_EXTRAP).mode == "extrapolate_right"


def test_window_uses_range_not_center():
    base = aligner.AlignedRow
    import alignment.aligner as al
    aset, g = _mapping_from([("a0", 2, 1, 0, 100, 10),
                             ("a1", 2, 1, 100, 200, 10)], 1000)
    row = aligner.SampleRow(1, 0, 2, 1, 1, 1, 0, 0, 50, 0, 0.0, 1.0, "kPa")
    aligned = aligner.align_rows([row], aset,
                                 {(2, 1): mapping_mod.Mapping(g, 1000)}, 100)
    a = aligned[0]
    assert a.corrected_ns == 150
    # 中心 150 在窗外，但范围 [140,160] 与 [140,140] 相交 → 保留
    assert a.within_window(140, 140) is True
    assert a.within_window(160, 160) is True
    assert a.within_window(141, 159) is True
    assert a.within_window(160 + 1, 1000) is False
    assert a.within_window(0, 139) is False


def test_window_unknown_is_none_not_false():
    aset, g = _mapping_from([("a0", 2, 1, 0, 0, 0), ("a1", 2, 1, 10, 10, 0)],
                            1000)
    bad = mapping_mod.Mapping(g, 1000)
    # 制造一个无效组：通过手工把组设为无效后 align_rows
    g.invalid_reason = "矛盾锚点"
    row = aligner.SampleRow(1, 0, 2, 1, 1, 1, 0, 0, 5, 0, 0.0, 1.0, "kPa")
    aligned = aligner.align_rows([row], aset, {}, 100)[0]
    assert not aligned.aligned
    assert aligned.within_window(0, 100) is None
    assert aligned.corrected_ns is None


# ----------------------------------------------------------------------
# 5. 巨大基数 1 ns / 重复来源 / 周期隔离
# ----------------------------------------------------------------------

def test_big_base_one_ns_without_float_loss():
    rows = [("a0", 5, 1, BIG, BIG + 100, 0),
            ("a1", 5, 1, BIG + 2, BIG + 102, 0)]
    aset, g = _mapping_from(rows, 1000)
    m = mapping_mod.Mapping(g, 1000)
    r = m.evaluate(BIG + 1, 100)
    assert r.center == Fraction(BIG + 101)
    assert r.radius == 0
    assert r.rounded() == (BIG + 101, BIG + 101, BIG + 101)
    # float64 在 9e18 上已无法区分相邻整数（间距约 1024），
    # 因此任何"先转浮点再比较"的实现都会把这里 1 ns 的差异吞掉。
    assert float(BIG + 1) == float(BIG)
    assert math.isclose(float(BIG), float(BIG + 1), rel_tol=1e-12)
    neg = [("b0", 6, 1, -BIG, -BIG - 100, 0),
           ("b1", 6, 1, -BIG + 2, -BIG - 98, 0)]
    aset2, g2 = _mapping_from(neg, 1000)
    r2 = mapping_mod.Mapping(g2, 1000).evaluate(-BIG + 1, 100)
    assert r2.center == -BIG - 99 and r2.radius == 0


def test_same_time_different_sources_kept(tmp_path):
    """同刻、不同来源（不同设备/通道）的测量全部保留，逐条带定位键。"""
    data = (_build(9, 1, E0, 1_000_000, [5])
            + _build(10, 1, E0, 1_000_000, [5]))
    db = tmp_path / "t.duckdb"
    f = ingest(tmp_path, "s.bin", data)
    with Ingester(db) as ing:
        ing.ingest_file(f)
    con = aligner.connect_read_only(str(db))
    try:
        rows = [r for b in aligner.iter_samples(con, batch=2) for r in b]
    finally:
        con.close()
    times = {r.ts_ns for r in rows}
    assert len(times) == 1
    assert {r.device_id for r in rows} == {9, 10}
    # 同一测量帧的多通道（同 ts）也在稳定次序里逐条保留
    refs = [(r.file_id, r.frame_offset, r.ordinal, r.channel) for r in rows]
    assert len(refs) == len(set(refs)) == 6


def test_boot_seq_separated(tmp_path):
    data2 = (_build(2, 1, E0, 1000, [0, 100])
             + _build(2, 2, E0 + 30 * SEC, 1000, [0, 100]))
    db = tmp_path / "t.duckdb"
    f = ingest(tmp_path, "d.bin", data2)
    with Ingester(db) as ing:
        ing.ingest_file(f)
    anc = tmp_path / "a.csv"
    write_anchors(anc, [
        ("b1-0", 2, 1, E0, E0 + 500, 0),
        ("b1-1", 2, 1, E0 + 100_000, E0 + 100_500, 0),
        ("b2-0", 2, 2, E0 + 30 * SEC, E0 + 29 * SEC, 0),
        ("b2-1", 2, 2, E0 + 30 * SEC + 100_000,
         E0 + 29 * SEC + 100_000, 0),
    ])
    con = aligner.connect_read_only(str(db))
    try:
        chunks = list(aligner.iter_samples(con, batch=2))
        allrows = [r for c in chunks for r in c]
    finally:
        con.close()
    aset = anchors_mod.build_anchor_set(anchors_mod.read_anchor_csv(anc)[0])
    mapping_mod.finalize_groups(aset.groups, 1000)
    maps = {k: mapping_mod.Mapping(g, 1000) for k, g in aset.groups.items()
            if g.valid}
    aligned = aligner.align_rows(allrows, aset, maps, 0)
    out = {(a.boot_seq, a.ts_ns): a for a in aligned}
    assert out[(1, E0)].corrected_ns == E0 + 500
    assert out[(2, E0 + 30 * SEC)].corrected_ns == E0 + 29 * SEC
    assert {a.boot_seq for a in aligned} == {1, 2}


def test_input_reordering_is_stable(tmp_path):
    data = _build(2, 1, E0, 1000, [0, 100, 200])
    db = tmp_path / "t.duckdb"
    f = ingest(tmp_path, "e.bin", data)
    with Ingester(db) as ing:
        ing.ingest_file(f)
    rows = [("a0", 2, 1, E0, E0 + 1_000, 5),
            ("a1", 2, 1, E0 + 100_000, E0 + 101_000, 5),
            ("a2", 2, 1, E0 + 200_000, E0 + 201_000, 5)]
    p1, p2 = tmp_path / "o1.csv", tmp_path / "o2.csv"
    write_anchors(p1, rows)
    write_anchors(p2, list(reversed(rows)))
    out = []
    for p in (p1, p2):
        aset = anchors_mod.build_anchor_set(anchors_mod.read_anchor_csv(p)[0])
        mapping_mod.finalize_groups(aset.groups, 1000)
        maps = {k: mapping_mod.Mapping(g, 1000) for k, g in aset.groups.items()
                if g.valid}
        con = aligner.connect_read_only(str(db))
        try:
            batches = list(aligner.iter_samples(con, batch=1))
        finally:
            con.close()
        allrows = [r for c in batches for r in c]
        aligned = aligner.align_rows(allrows, aset, maps, 0)
        out.append([(a.ref, a.corrected_ns, a.lower_ns, a.upper_ns,
                     a.anchors_used) for a in aligned])
    assert out[0] == out[1]


# ----------------------------------------------------------------------
# 6. 公开命令：真实测量 + 来源可回溯 + 只读
# ----------------------------------------------------------------------

def _prepare(tmp_path):
    data = (_build(2, 1, E0, 1000, [0, 500, 1500])
            + _build(3, 1, E0 + 3 * SEC, 1000, [0, 500, 1000]))
    db = tmp_path / "d.duckdb"
    f = ingest(tmp_path, "g.bin", data)
    with Ingester(db) as ing:
        ing.ingest_file(f)
    rows = [("d2-a0", 2, 1, E0, E0 + 500_000_000, 2_000_000),
            ("d2-a1", 2, 1, E0 + 4 * SEC, E0 + 4_500_400_000, 2_000_000),
            ("d3-a0", 3, 1, E0 + 3 * SEC, E0 + 1 * SEC, 3_000_000),
            ("d3-a1", 3, 1, E0 + 7 * SEC, E0 + 4_999_800_000, 3_000_000)]
    anc = tmp_path / "h.csv"
    write_anchors(anc, rows)
    return db, anc, rows


def _cli(*args, cwd=REPO_ROOT):
    return subprocess.run([sys.executable, "-m", "alignment.cli", *args],
                          cwd=cwd, text=True, capture_output=True)


def _json_lines(stdout):
    return [json.loads(l) for l in stdout.splitlines() if l.startswith("{")]


def test_project_matches_hand_calc_and_source(tmp_path):
    db, anc, arows = _prepare(tmp_path)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    r = _cli("project", "--db", str(db), "--anchors", str(anc),
             "--device", "2", "--json", "--max-ppm", str(MAX_PPM))
    assert r.returncode == 0, r.stderr
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert before == after, "对齐工具不得回写测量库"
    rows = _json_lines(r.stdout)
    # 每帧 3 个通道：按 (local_ns, ordinal) 稳定输出，全部保留
    assert [row["local_ns"] for row in rows] == [
        E0, E0, E0, E0 + 500_000_000, E0 + 500_000_000, E0 + 500_000_000,
        E0 + 1_500_000_000, E0 + 1_500_000_000, E0 + 1_500_000_000]
    pts = manual_points(arows, dev=2, boot=1)
    for row in rows:
        exp = manual_map(pts, row["local_ns"], MAX_PPM, MAX_EXTRAP)
        assert row["corrected_ns"] == rounded(exp[0], exp[1])[0]
        assert row["lower_ns"] == rounded(exp[0], exp[1])[1]
        assert row["upper_ns"] == rounded(exp[0], exp[1])[2]
        assert Fraction(row["center"]) == exp[0]
        assert Fraction(row["radius"]) == exp[1]
        assert row["mode"] == exp[2]
        assert tuple(row["anchors_used"]) == exp[3]
        # 原测量定位键可回到真实行
        con = tele_store.connect(db)
        try:
            src = con.execute(
                "SELECT channel, ts_ns, value, unit FROM tele_samples "
                "WHERE file_id = ? AND frame_offset = ? AND ordinal = ?",
                [row["file_id"], row["frame_offset"], row["ordinal"]]).fetchone()
        finally:
            con.close()
        assert src == (row["channel"], row["local_ns"], row["value"],
                       row["unit"])


def test_project_reports_unaligned_with_reason(tmp_path):
    db, anc, _ = _prepare(tmp_path)
    # 只给 dev 3 锚点：dev 2 的测量必须列出"没有锚点组"且不给 corrected_ns
    anc2 = tmp_path / "only3.csv"
    write_anchors(anc2, [("d3-a0", 3, 1, E0 + 3 * SEC, E0 + 1 * SEC, 0),
                         ("d3-a1", 3, 1, E0 + 7 * SEC,
                          E0 + 4_999_800_000, 0)])
    r = _cli("project", "--db", str(db), "--anchors", str(anc2), "--json")
    assert r.returncode == 0, r.stderr
    rows = _json_lines(r.stdout)
    dev2 = [x for x in rows if x["device_id"] == 2]
    assert dev2 and all(not x["aligned"] for x in dev2)
    assert all("没有锚点组" in x["reason"] for x in dev2)
    assert all(x["corrected_ns"] is None for x in dev2)


def test_window_cli_intersection_and_unknown_count(tmp_path):
    db, anc, arows = _prepare(tmp_path)
    pts2 = manual_points(arows, dev=2, boot=1)
    center = rounded(*manual_map(pts2, E0 + 1_500_000_000, MAX_PPM,
                                 MAX_EXTRAP)[:2])[0]
    # 中心在窗外 1 ns，但范围（半径 ≥ 2e6）必然擦到
    r = _cli("window", "--db", str(db), "--anchors", str(anc),
             "--device", "2", "--low-ns", str(center - 1),
             "--high-ns", str(center - 1), "--json")
    assert r.returncode == 0, r.stderr
    rows = _json_lines(r.stdout)
    assert any(w["local_ns"] == E0 + 1_500_000_000 for w in rows)
    # 单独把下界推到范围之外：必须排除
    r = _cli("window", "--db", str(db), "--anchors", str(anc),
             "--device", "2", "--low-ns", str(center + 10_000_000),
             "--high-ns", str(center + 20_000_000), "--json")
    assert r.returncode == 0
    assert _json_lines(r.stdout) == []
    # 无效组：明确报告未对齐数量，且不把任何行当窗外/窗内
    anc_bad = tmp_path / "bad.csv"
    write_anchors(anc_bad, [("a0", 2, 1, E0, E0, 0),
                            ("a0x", 2, 1, E0, E0 + 1, 0),   # 同刻矛盾
                            ("a1", 2, 1, E0 + 1_000_000, E0 + 1_000_000, 0)])
    r = _cli("window", "--db", str(db), "--anchors", str(anc_bad),
             "--device", "2", "--low-ns", "0", "--high-ns", "1")
    assert r.returncode == 0, r.stderr
    assert "未对齐/无法判断" in r.stdout and "整体不能对齐" in r.stdout
    assert "window_in" not in r.stdout


def test_iter_samples_batches_bounded(tmp_path):
    data = _build(2, 1, E0, 1000, list(range(50)))
    db = tmp_path / "t.duckdb"
    f = ingest(tmp_path, "big.bin", data)
    with Ingester(db) as ing:
        ing.ingest_file(f)
    con = aligner.connect_read_only(str(db))
    try:
        batches = list(aligner.iter_samples(con, batch=7))
    finally:
        con.close()
    assert all(len(b) <= 7 for b in batches)
    assert sum(len(b) for b in batches) == 150
    # 稳定次序：与一次性 LIMIT 大数的结果一致
    con = aligner.connect_read_only(str(db))
    try:
        allrows = [r for b in aligner.iter_samples(con, batch=1_000)
                   for r in b]
    finally:
        con.close()
    keys = [(r.device_id, r.boot_seq, r.ts_ns, r.file_id, r.frame_offset,
             r.ordinal, r.channel) for r in allrows]
    assert keys == sorted(keys)


def test_cli_rejects_bad_config():
    r = _cli("project", "--db", "x.duckdb", "--anchors", "x.csv",
             "--max-ppm", "-1")
    assert r.returncode == 2
    assert "max-ppm" in r.stderr


# ----------------------------------------------------------------------
# 7. 段边界 / 闭区间窗口 / 分批一致性 / 缺表
# ----------------------------------------------------------------------

def test_segment_boundaries_use_anchor_itself():
    rows = [("a0", 2, 1, 0, 100, 4),
            ("a1", 2, 1, 10, 110, 9),
            ("a2", 2, 1, 20, 120, 14)]
    aset, g = _mapping_from(rows, 1000)
    m = mapping_mod.Mapping(g, 1000)
    for idx, x in enumerate((0, 10, 20)):
        r = m.evaluate(x, 1000)
        assert r.center == m.y[idx] and r.radius == m.e[idx]
        assert r.mode == "interp"
        assert m.anchor_ids_for_segment(r.segment) == list(
            g.anchors[idx].anchor_ids)
    # 段内两侧都走相邻锚点；段中点取平均
    lo = m.evaluate(1, 1000)
    hi = m.evaluate(19, 1000)
    assert lo.center == Fraction(101) and hi.center == Fraction(119)
    assert lo.radius == Fraction(4) + Fraction(9 - 4, 10) * 1
    # 向左越界用最左段，向右越界用最右段（不跨段延长）
    left = m.evaluate(-1, 1000)
    right = m.evaluate(21, 1000)
    assert left.segment == (0, 1) and right.segment == (1, 2)


def test_window_closed_interval_edges():
    from alignment.aligner import AlignedRow, RowRef
    import alignment.aligner as al
    aset, g = _mapping_from([("a0", 2, 1, 0, 100, 10),
                             ("a1", 2, 1, 100, 200, 10)], 1000)
    row = aligner.SampleRow(1, 0, 2, 1, 1, 1, 0, 0, 50, 0, 0.0, 1.0, "kPa")
    a = aligner.align_rows([row], aset,
                           {(2, 1): mapping_mod.Mapping(g, 1000)}, 100)[0]
    # 范围 [140, 160]；闭区间端点相交即算
    assert a.within_window(140, 160) is True
    assert a.within_window(139, 140) is True
    assert a.within_window(160, 161) is True
    assert a.within_window(0, 139) is False
    assert a.within_window(161, 1000) is False


def test_batch_size_does_not_change_result(tmp_path):
    db, anc, _ = _prepare(tmp_path)
    outs = []
    for batch in ("1", "2", "7", "4096"):
        r = _cli("project", "--db", str(db), "--anchors", str(anc),
                 "--json", "--batch", batch)
        assert r.returncode == 0, r.stderr
        outs.append(_json_lines(r.stdout))
    assert outs[0] == outs[1] == outs[2] == outs[3]


def test_missing_tele_samples_table(tmp_path):
    import duckdb
    p = tmp_path / "empty.duckdb"
    duckdb.connect(str(p)).close()
    anc = tmp_path / "a.csv"
    write_anchors(anc, [("a0", 1, 1, 0, 0, 0), ("a1", 1, 1, 10, 10, 0)])
    r = _cli("project", "--db", str(p), "--anchors", str(anc))
    assert r.returncode == 1
    assert "tele_samples" in r.stderr
    r = _cli("window", "--db", str(p), "--anchors", str(anc),
             "--low-ns", "0", "--high-ns", "1")
    assert r.returncode == 1



def test_negative_values_floor_ceil_math():
    # 斜率≈1 的负坐标锚点：x=0→y=-1e9，x=2→y=-1e9+2
    # y = -3/2，u = 1/2 → floor(y) = -2，floor(y-u) = -2，ceil(y+u) = -1
    rows = [("n0", 7, 1, 0, -10 ** 9, 0),
            ("n1", 7, 1, 2, -10 ** 9 + 2, 0)]
    aset, g = _mapping_from(rows, 1000)
    pts = manual_points(rows)
    # x=1：a=1/2，中心 -1e9+1，内插半径 0 → 三值都是 -999999999
    r = mapping_mod.Mapping(g, 1000).evaluate(1, 1000)
    assert r.center == Fraction(-10 ** 9 + 1) and r.radius == 0
    assert r.rounded() == (-10 ** 9 + 1,) * 3
    # 带误差：中心 -3/2，半径 1/2 → floor(-2)=-2，floor(-2)=-2，ceil(-1)=-1
    rows2 = [("m0", 7, 1, 0, -10 ** 9, 0),
             ("m1", 7, 1, 2, -10 ** 9 + 2, 1)]
    aset2, g2 = _mapping_from(rows2, 1000)
    r2 = mapping_mod.Mapping(g2, 1000).evaluate(1, 1000)
    assert r2.center == Fraction(-10 ** 9 + 1)
    assert r2.radius == Fraction(1, 2)
    # floor(-999999999.5) = -1000000000；ceil(-999999998.5) = -999999998
    # 负值按数学定义向外取整，误差范围绝不被向内舍入
    assert r2.rounded() == (-10 ** 9 + 1, -10 ** 9, -10 ** 9 + 2)


# ----------------------------------------------------------------------
# 8. 重复来源（同一 ts_ns 多行）在 project/window 中逐条保留
# ----------------------------------------------------------------------

def test_duplicate_timestamps_are_not_collapsed(tmp_path):
    # 同一设备周期内同一 tick 的两个不同布局通道帧 → 同 ts_ns 多行
    data = _build(9, 1, E0, 1_000_000, [5])
    import struct
    from telemetry.generator import StreamBuilder
    b = StreamBuilder()
    b.boot(9, 1, E0, 1_000_000)
    b.layout(9, 1, MIXED)
    b.layout(9, 2, [(0, 1, 1.0, 0.0, 1)])
    b.meas(9, 1, 5, [25_000, 1_000, 1_000])
    b.meas(9, 2, 5, [30_000])            # 同 tick、不同布局 → tick_conflict
    db = tmp_path / "dup.duckdb"
    f = ingest(tmp_path, "dup.bin", b.bytes())
    with Ingester(db) as ing:
        ing.ingest_file(f)
    con = tele_store.connect(db)
    try:
        rows = con.execute("SELECT COUNT(*), COUNT(DISTINCT ts_ns) "
                           "FROM tele_samples").fetchone()
    finally:
        con.close()
    assert rows == (3, 1)               # 3 个通道行都保留在同一 ts（冲突帧被拒）
