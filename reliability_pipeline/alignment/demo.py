"""可运行的示例与回归材料（``python3 -m alignment.cli demo``）。

材料全部由**真实二进制遥测接入命令**（``telemetry`` 的解码与入库路径）
生成，工具本身只以只读方式读取这些测量：

================  ==========================  ==============================
设备/周期          本地钟行为                   用途
================  ==========================  ==============================
dev 2 boot 1       比参考钟慢 0.5 s，+100 ppm   共同事件（与 dev3 相交）
dev 2 boot 2       另一启动周期，偏移不同       周期隔离
dev 3 boot 1       比参考钟快 2 s，−50 ppm      共同事件（与 dev2 相交）
dev 4 boot 1       —                            左右有限外推、超额度
dev 5 boot 1       —                            巨大正时间基数，1 ns 间隔
dev 6 boot 1       —                            巨大负时间基数，1 ns 间隔
================  ==========================  ==============================

所有期望值在 ``expected.py`` 中以 Fraction 手写推导，再与公开命令的
``--json`` 输出逐项比对；不调用被测的 ``Mapping``。
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Tuple

from telemetry import generator
from telemetry.ingest import Ingester

E0 = generator.EPOCH0
SEC = 1_000_000_000
BIG = 9_000_000_000_000_000_000          # 9e18，仍在 int64 内
MAX_PPM = 1000                            # 示例使用的斜率上限
MAX_EXTRAP = 1_000_000_000                # 示例允许的 1 s 外推

REPO_ROOT = Path(__file__).resolve().parent.parent

MIXED_LAYOUT = [(0, 1, 1.0, 0.0, 1),      # int16 毫°C
                (1, 2, 1.0, 0.0, 2),      # int32 Pa
                (2, 3, 1.0, 0.0, 3)]      # float32 mm/s

# ---- 测量材料：(device, boot, epoch_ns, tick_hz, ticks, spike_tick) ----
STREAMS: Dict[str, Tuple[int, int, int, int, List[int], int]] = {
    "dev2_boot1.bin": (2, 1, E0, 1000,
                       [0, 250, 500, 1000, 1500, 2000, 4000, 8000], 1500),
    "dev2_boot2.bin": (2, 2, E0 + 30 * SEC, 1000,
                       [0, 100, 1000], -1),
    "dev3.bin": (3, 1, E0 + 3 * SEC, 1000,
                 [0, 250, 500, 1000, 1500, 2000, 4000, 8000], 1000),
    "dev4.bin": (4, 1, E0, 1_000_000,
                 [999_500, 1_000_000, 1_500_000, 2_000_000,
                  2_000_500, 3_000_001], -1),
    "dev5.bin": (5, 1, BIG, 1_000_000_000, [0, 1, 2], -1),
    "dev6.bin": (6, 1, -BIG, 1_000_000_000, [0, 1, 2], -1),
}

# ---- 主锚点表（绝对纳秒；local_ns 与 tele_samples.ts_ns 同坐标系） ----
ANCHORS: List[Tuple[str, int, int, int, int, int]] = [
    # dev 2 boot 1：y = x + 0.5 s + 100 ppm·x
    ("d2-a0", 2, 1, E0, E0 + 500_000_000, 2_000_000),
    ("d2-a1", 2, 1, E0 + 4 * SEC, E0 + 4_500_400_000, 2_000_000),
    ("d2-a2", 2, 1, E0 + 8 * SEC, E0 + 8_500_800_000, 2_000_000),
    ("d2-dup", 2, 1, E0 + 8 * SEC, E0 + 8_500_800_000, 2_000_000),
    # dev 2 boot 2：y = x − 1 s + 20 ppm·(x − epoch)
    ("d2b2-a0", 2, 2, E0 + 30 * SEC, E0 + 29 * SEC, 1_000_000),
    ("d2b2-a1", 2, 2, E0 + 31 * SEC, E0 + 30 * SEC + 20_000, 1_000_000),
    # dev 3：y = x − 2 s − 50 ppm·(x − epoch)
    ("d3-a0", 3, 1, E0 + 3 * SEC, E0 + 1 * SEC, 3_000_000),
    ("d3-a1", 3, 1, E0 + 7 * SEC, E0 + 4_999_800_000, 3_000_000),
    ("d3-a2", 3, 1, E0 + 11 * SEC, E0 + 8_999_600_000, 3_000_000),
    # dev 4：极端接近斜率 1 的段（局部外推专用）
    ("d4-a0", 4, 1, E0 + 1 * SEC, E0 + 1 * SEC + 7, 10),
    ("d4-a1", 4, 1, E0 + 2 * SEC, E0 + 2 * SEC + 14, 10),
    # dev 5：巨大正基数，1 ns 间隔的整数插值
    ("d5-a0", 5, 1, BIG, BIG + 100, 0),
    ("d5-a1", 5, 1, BIG + 2, BIG + 102, 0),
    # dev 6：巨大负基数，1 ns 间隔
    ("d6-a0", 6, 1, -BIG, -BIG - 100, 0),
    ("d6-a1", 6, 1, -BIG + 2, -BIG - 98, 0),
]


def _write_anchors(path: Path, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["anchor_id", "device_id", "boot_seq", "local_ns",
                    "reference_ns", "error_ns"])
        for r in rows:
            w.writerow(r)


def _build_stream(dev: int, boot: int, epoch: int, hz: int,
                  ticks: List[int], spike_tick: int) -> bytes:
    b = generator.StreamBuilder()
    b.boot(dev, boot, epoch, hz)
    b.layout(dev, 1, MIXED_LAYOUT)
    for tick in ticks:
        pressure = 150_000 if tick == spike_tick else 1_000
        b.meas(dev, 1, tick, [25_000, pressure, 1_000])
    return b.bytes()


def ts_of(dev_boot: str, tick: int) -> int:
    dev, boot, epoch, hz, ticks, spike = STREAMS[dev_boot]
    return epoch + (tick * SEC) // hz


def build_materials(workdir: Path) -> dict:
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    for p in workdir.glob("*.duckdb"):
        p.unlink()
    data_dir = workdir / "data"
    data_dir.mkdir(exist_ok=True)
    db = workdir / "clock.duckdb"
    with Ingester(db) as ing:
        for name, spec in STREAMS.items():
            f = data_dir / name
            f.write_bytes(_build_stream(*spec))
            ing.ingest_file(f)
    anchors = workdir / "anchors.csv"
    _write_anchors(anchors, ANCHORS)

    # 输入重排：行顺序完全打乱，锚点身份与数值不变
    reordered = list(reversed(ANCHORS))
    rest = reordered[3:9]
    reordered[3:9] = rest[3:] + rest[:3]
    anchors_reordered = workdir / "anchors_reordered.csv"
    _write_anchors(anchors_reordered, reordered)

    # 矛盾锚点：dev 2 同一本地时刻给出不同参考值 → 整组无效
    conflict_rows = [r for r in ANCHORS if r[1] != 2 or r[2] != 1]
    conflict_rows += [r for r in ANCHORS if r[1] == 2 and r[2] == 1]
    conflict_rows.append(("d2-conflict", 2, 1, E0 + 4 * SEC,
                          E0 + 4_500_400_001, 2_000_000))
    anchors_conflict = workdir / "anchors_conflict.csv"
    _write_anchors(anchors_conflict, conflict_rows)

    # 非法输入样例（供回归材料；本演示只展示拒绝文本）
    illegal = workdir / "anchors_illegal.csv"
    illegal.write_text(
        "anchor_id,device_id,boot_seq,local_ns,reference_ns,error_ns\n"
        "x1,2,1,0,10,0\n"
        "x1,2,1,999,20,0\n"              # 重复 anchor_id
        "x2,2,1,1.5,20,0\n"              # 非整数
        "x3,2,1,100,200,-1\n",           # 负误差
        encoding="utf-8")

    return dict(workdir=workdir, db=str(db), anchors=anchors,
                anchors_reordered=anchors_reordered,
                anchors_conflict=anchors_conflict, illegal=illegal,
                data_dir=data_dir)


# ----------------------------------------------------------------------
# 独立手算期望（仅用 Fraction 与显式公式，不调用 production Mapping）
# ----------------------------------------------------------------------

def _anchor_values(dev: int, boot: int, rows=ANCHORS):
    vals: Dict[int, Tuple[int, int, List[str]]] = {}
    for aid, d, b, x, y, e in rows:
        if d != dev or b != boot:
            continue
        cur = vals.setdefault(x, (y, e, []))
        assert cur[0] == y and cur[1] == e
        cur[2].append(aid)
    return sorted((Fraction(x), Fraction(y), Fraction(e),
                   tuple(sorted(ids))) for x, (y, e, ids) in vals.items())


def expected_interp(dev: int, boot: int, x: int) -> dict:
    """相邻锚点内插：a=(x-x0)/(x1-x0)，y=(1-a)y0+a y1，u=(1-a)e0+a e1。"""
    pts = _anchor_values(dev, boot)
    for i in range(len(pts) - 1):
        x0, y0, e0, ids0 = pts[i]
        x1, y1, e1, ids1 = pts[i + 1]
        if x0 <= x <= x1:
            a = (Fraction(x) - x0) / (x1 - x0)
            center = (1 - a) * y0 + a * y1
            radius = (1 - a) * e0 + a * e1
            return dict(center=center, radius=radius, a=a,
                        anchors=ids0 + ids1, mode="interp")
    raise ValueError("x 不在锚点组内")

def expected_extrap(dev: int, boot: int, x: int, max_ppm: int,
                    left: bool) -> dict:
    """外推：u=|1-a|e0+|a|e1+d·max_ppm/10^6（d=到最近端点的距离）。"""
    pts = _anchor_values(dev, boot)
    i0, i1 = (0, 1) if left else (len(pts) - 2, len(pts) - 1)
    x0, y0, e0, ids0 = pts[i0]
    x1, y1, e1, ids1 = pts[i1]
    a = (Fraction(x) - x0) / (x1 - x0)
    center = (1 - a) * y0 + a * y1
    d = abs(Fraction(x) - (x0 if left else x1))
    radius = (abs(1 - a) * e0 + abs(a) * e1
              + d * Fraction(max_ppm, 1_000_000))
    return dict(center=center, radius=radius, a=a, d=int(d),
                anchors=ids0 + ids1,
                mode="extrapolate_left" if left else "extrapolate_right")


def expected_anchor_point(dev: int, boot: int, x: int) -> dict:
    pts = _anchor_values(dev, boot)
    for x0, y0, e0, ids in pts:
        if x0 == x:
            return dict(center=y0, radius=e0, a=Fraction(0), anchors=ids,
                        mode="interp")
    raise ValueError("x 不是锚点")


def _rounded(y: Fraction, u: Fraction) -> Tuple[int, int, int]:
    import math
    return (math.floor(y), math.floor(y - u), math.ceil(y + u))


# ----------------------------------------------------------------------
# 公开命令驱动
# ----------------------------------------------------------------------

def run_cli(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "alignment.cli"] + args,
                          cwd=REPO_ROOT, text=True, capture_output=True)


def json_rows(stdout: str) -> List[dict]:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            out.append(json.loads(line))
    return out


def frac_of(text) -> Fraction:
    if text is None:
        return None
    return Fraction(text)


def _check(problems: List[str], cond: bool, msg: str) -> None:
    if not cond:
        problems.append(msg)


def _compare_row(problems: List[str], row: dict, exp: dict, label: str,
                 check_radius: bool = True) -> None:
    want = _rounded(exp["center"], exp["radius"])
    got = (row["corrected_ns"], row["lower_ns"], row["upper_ns"])
    _check(problems, got == want, f"{label}：舍入值 {got} != 手算 {want}")
    _check(problems, frac_of(row["center"]) == exp["center"],
           f"{label}：中心 {row['center']} != 手算 {exp['center']}")
    if check_radius:
        _check(problems, frac_of(row["radius"]) == exp["radius"],
               f"{label}：半径 {row['radius']} != 手算 {exp['radius']}")
    _check(problems, row["mode"] == exp["mode"],
           f"{label}：模式 {row['mode']} != {exp['mode']}")
    _check(problems, tuple(row["anchors_used"]) == tuple(exp["anchors"]),
           f"{label}：所用锚点 {row['anchors_used']} != {exp['anchors']}")


def _find(rows, **fields) -> dict:
    for r in rows:
        if all(r[k] == v for k, v in fields.items()):
            return r
    raise KeyError(f"找不到行 {fields}")


def _project(db, anchors, *extra) -> List[dict]:
    r = run_cli(["project", "--db", str(db), "--anchors", str(anchors),
                 "--json", *extra])
    if r.returncode != 0:
        raise RuntimeError(f"project 失败：{r.stderr}")
    return json_rows(r.stdout)


def run_demo(workdir: Path, seed: int = 1) -> int:
    workdir = Path(workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    mats = build_materials(workdir)
    problems: List[str] = []
    db, anchors = mats["db"], mats["anchors"]

    print("== 1. 材料：真实接入的 6 台设备 + 锚点 CSV ==")
    print(f"库：{db}\n锚点：{anchors}\n")
    for name, (dev, boot, epoch, hz, ticks, spike) in STREAMS.items():
        print(f"  {name}: device={dev} boot={boot} epoch={epoch} tick_hz={hz} "
              f"ticks={len(ticks)}")

    print("\n== 2. 锚点组校验（折叠同刻同值、斜率 ppm） ==")
    r = run_cli(["anchors", "--anchors", str(anchors)])
    _check(problems, r.returncode == 0, f"anchors 退出码 {r.returncode}")
    print(r.stdout)
    _check(problems, "d2-a2,d2-dup" in r.stdout.replace(" ", ""),
           "折叠锚点必须列出全部 anchor_id")

    print("== 3. 两台设备的共同事件：校正后范围相交 ==")
    rows2 = _project(db, anchors, "--device", "2", "--boot-seq", "1")
    rows3 = _project(db, anchors, "--device", "3")
    ev2_local = ts_of("dev2_boot1.bin", 1500)
    ev3_local = ts_of("dev3.bin", 1000)
    exp2 = expected_interp(2, 1, ev2_local)
    exp3 = expected_interp(3, 1, ev3_local)
    row2 = _find(rows2, local_ns=ev2_local, channel=1)
    row3 = _find(rows3, local_ns=ev3_local, channel=1)
    _compare_row(problems, row2, exp2, "dev2 共同事件")
    _compare_row(problems, row3, exp3, "dev3 共同事件")
    lo = max(row2["lower_ns"], row3["lower_ns"])
    hi = min(row2["upper_ns"], row3["upper_ns"])
    print(f"  dev2 本地 {ev2_local} → 参考 [{row2['lower_ns']}, "
          f"{row2['upper_ns']}]（中心 {row2['center']}，半径 {row2['radius']}）")
    print(f"  dev3 本地 {ev3_local} → 参考 [{row3['lower_ns']}, "
          f"{row3['upper_ns']}]（中心 {row3['center']}，半径 {row3['radius']}）")
    print(f"  相交区间 = [{lo}, {hi}]（跨越不同偏移/漂移仍相交）")
    _check(problems, lo <= hi, "共同事件校正范围不相交")

    print("\n== 4. 手算有理数核对：内插 / 锚点自身 / 左右外推 ==")
    # 内插（dev2 共同事件）已在上面核对；再核对锚点自身与左外推
    row_at_a1 = _find(rows2, local_ns=ts_of("dev2_boot1.bin", 4000), channel=1)
    exp_anchor = expected_anchor_point(2, 1, ts_of("dev2_boot1.bin", 4000))
    _compare_row(problems, row_at_a1, exp_anchor, "dev2 锚点 a1 自身")
    rows4 = _project(db, anchors, "--device", "4")
    x_left = ts_of("dev4.bin", 999_500)
    x_right = ts_of("dev4.bin", 2_000_500)
    row_left = _find(rows4, local_ns=x_left, channel=1)
    row_right = _find(rows4, local_ns=x_right, channel=1)
    _compare_row(problems, row_left, expected_extrap(4, 1, x_left, MAX_PPM, True),
                 "dev4 左外推")
    _compare_row(problems, row_right,
                 expected_extrap(4, 1, x_right, MAX_PPM, False), "dev4 右外推")
    print(f"  左外推 {x_left}: {row_left['lower_ns']}..{row_left['upper_ns']} "
          f"(中心 {row_left['center']})")
    print(f"  右外推 {x_right}: {row_right['lower_ns']}..{row_right['upper_ns']}")
    # 超额度：必须未对齐且给出原因
    x_far = ts_of("dev4.bin", 3_000_001)
    far = _find(rows4, local_ns=x_far, channel=1)
    _check(problems, not far["aligned"], "超出外推额度的测量不应被对齐")
    _check(problems, "超过外推额度" in (far["reason"] or ""),
           f"超额度原因不明确：{far['reason']}")
    print(f"  超额度 {x_far}: 未对齐 — {far['reason']}")
    # 边界：恰好 d == 额度允许，超 1 ns 拒绝
    # 最右锚点 a1 的本地时刻是 E0+2s；额度边界 = a1 + 1s
    exact = ts_of("dev4.bin", 2_000_000) + MAX_EXTRAP
    # 该时刻在数据库里没有测量，改用公开 Mapping 直接核对（专项测试也覆盖）
    import alignment.mapping as _map
    from alignment.anchors import RawAnchor, build_anchor_set
    a_rows = [r for r in ANCHORS if r[1] == 4]
    aset = build_anchor_set([RawAnchor(*r, i + 2)
                             for i, r in enumerate(a_rows)])
    _map.finalize_groups(aset.groups, MAX_PPM)
    m = _map.Mapping(aset.groups[(4, 1)], MAX_PPM)
    ok = m.evaluate(exact, MAX_EXTRAP)
    _check(problems, ok.mode == "extrapolate_right",
           "恰好等于外推额度时应允许")
    try:
        m.evaluate(exact + 1, MAX_EXTRAP)
        problems.append("超出额度 1 ns 应被拒绝")
    except _map.OutOfRange:
        pass
    print(f"  外推额度边界：d={MAX_EXTRAP} 允许，d={MAX_EXTRAP + 1} 拒绝")

    print("\n== 5. 巨大正/负时间基数下仅隔 1 ns（整数而非浮点） ==")
    for dev in (5, 6):
        rows = _project(db, anchors, "--device", str(dev))
        for tick in (0, 1, 2):
            x = ts_of(f"dev{dev}.bin", tick)
            row = _find(rows, local_ns=x, channel=1)
            if tick == 0 or tick == 2:
                exp = expected_anchor_point(dev, 1, x)
            else:
                exp = expected_interp(dev, 1, x)
            _compare_row(problems, row, exp, f"dev{dev} tick={tick}")
        mid = ts_of(f"dev{dev}.bin", 1)
        row = _find(rows, local_ns=mid, channel=1)
        print(f"  dev{dev}: 相邻测量 {ts_of(f'dev{dev}.bin', 0)} / {mid} / "
              f"{ts_of(f'dev{dev}.bin', 2)}；中间一条中心 = {row['center']}")
        _check(problems, row["corrected_ns"] is not None, "1 ns 间隔应可精确映射")
        # Python int 邻近性自检（float64 在 9e18 上间距约 1024，会吞掉 1 ns）
        _check(problems,
               abs(int(row["corrected_ns"]) - int(rows[0]["corrected_ns"])) == 1,
               f"dev{dev} 1 ns 间隔被吞掉")

    print("\n== 6. 同设备换周期（boot_seq 分开） ==")
    rows_b2 = _project(db, anchors, "--device", "2", "--boot-seq", "2")
    b2_local = ts_of("dev2_boot2.bin", 100)
    exp_b2 = expected_interp(2, 2, b2_local)
    _compare_row(problems, _find(rows_b2, local_ns=b2_local, channel=1),
                 exp_b2, "dev2 boot2")
    _check(problems, len(rows2) > 0 and len(rows_b2) > 0,
           "两个启动周期都应有测量")
    _check(problems, {r["boot_seq"] for r in rows2} == {1},
           "boot_seq=1 投影混入了其他周期")
    _check(problems, {r["boot_seq"] for r in rows_b2} == {2},
           "boot_seq=2 投影混入了其他周期")

    print("\n== 7. 参考钟窗口：范围相交 vs 只看中心 ==")
    # dev2 共同事件中心在窗口外，但下界擦到窗口
    edge = row2["corrected_ns"] - 1
    r = run_cli(["window", "--db", db, "--anchors", str(anchors),
                 "--device", "2", "--boot-seq", "1",
                 "--low-ns", str(edge), "--high-ns", str(edge),
                 "--max-extrapolation-ns", str(MAX_EXTRAP)])
    _check(problems, r.returncode == 0, f"window 退出码 {r.returncode}")
    print(r.stdout)
    _check(problems, "未对齐/无法判断 0 条" in r.stdout,
           "该窗口内所有测量都有有效组时不应报未对齐")
    r_json = run_cli(["window", "--db", db, "--anchors", str(anchors),
                      "--device", "2", "--boot-seq", "1", "--json",
                      "--low-ns", str(edge), "--high-ns", str(edge),
                      "--max-extrapolation-ns", str(MAX_EXTRAP)])
    win_rows = json_rows(r_json.stdout)
    _check(problems, any(ev2_local == w["local_ns"] for w in win_rows),
           "中心在窗外、但误差下界擦到窗口的测量必须保留")
    _check(problems, all(w["device_id"] == 2 for w in win_rows),
           "设备过滤后窗口输出混入了其他设备")
    # 中心恰好落在窗口外且范围不擦边 → 必须排除
    r = run_cli(["window", "--db", db, "--anchors", str(anchors),
                 "--device", "2", "--boot-seq", "1", "--json",
                 "--low-ns", str(row2["upper_ns"] + 1),
                 "--high-ns", str(row2["upper_ns"] + 1000)])
    _check(problems, r.returncode == 0, "窗外窗口应正常退出")
    outside = json_rows(r.stdout)
    _check(problems, not any(ev2_local == w["local_ns"] for w in outside),
           "完全在窗外的测量不应保留")
    print(f"  窗口下界再外移 1 ns → 共同事件被正确排除（保留 {len(outside)} 条）")
    # 明确"未对齐不算窗外"：矛盾锚点表让 dev2 整组无法判断
    r = run_cli(["window", "--db", db, "--anchors",
                 str(mats["anchors_conflict"]), "--device", "2",
                 "--boot-seq", "1", "--low-ns", str(edge),
                 "--high-ns", str(edge)])
    _check(problems, r.returncode == 0, "矛盾锚点 window 应正常退出")
    print(r.stdout)
    _check(problems, "整组整体不能对齐" in r.stdout or "不一致" in r.stdout,
           "矛盾锚点应报告具体原因")
    _check(problems, "未对齐/无法判断" in r.stdout, "窗口输出应明确未对齐数量")
    _check(problems, not json_rows(r.stdout),
           "整组无效时不能把任何测量当作已对齐")

    print("== 8. 输入重排：锚点行序打乱后结果不变 ==")
    rows_reordered = _project(db, mats["anchors_reordered"], "--device", "2",
                              "--boot-seq", "1")
    norm = lambda rs: sorted(json.dumps(r, sort_keys=True) for r in rs)
    _check(problems, norm(rows_reordered) == norm(rows2),
           "锚点输入重排改变了输出")
    print("  重排前后逐行 JSON 相同 ✓")

    print("\n== 9. 非法输入在输入阶段被拒绝 ==")
    r = run_cli(["anchors", "--anchors", str(mats["illegal"])])
    _check(problems, r.returncode == 2, f"非法输入退出码 {r.returncode} != 2")
    print(r.stderr.strip())
    _check(problems, "重复 anchor_id" in r.stderr,
           "非法输入应逐行指出重复 anchor_id")
    _check(problems, "error_ns 必须为非负整数" in r.stderr,
           "非法输入应逐行指出负误差")

    print("\n== 10. 斜率超限使整组失效（不跳过坏段） ==")
    slope_rows = workdir / "anchors_slope.csv"
    _write_anchors(slope_rows, [
        ("s0", 7, 1, 0, 1000, 0),
        ("s1", 7, 1, 1_000_000, 1_000_000 + 10_000, 0),   # 10000 ppm
        ("s2", 7, 1, 2_000_000, 2_000_000, 0),            # 好段，也不能救
    ])
    r = run_cli(["anchors", "--anchors", str(slope_rows), "--max-ppm", "1000"])
    _check(problems, r.returncode == 0, "斜率超限应作为组状态而非命令失败")
    print(r.stdout)
    _check(problems, "无效" in r.stdout and "斜率偏离" in r.stdout,
           "整组应标为无效并说明坏段")
    # dev7 没有测量：这里只验证锚点组判定本身

    print()
    if problems:
        print("示例核对失败：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("示例全部核对通过：共同事件范围相交；内插/锚点/左右外推与手算一致；"
          "超额度与矛盾锚点明确未对齐；1 ns 间隔未被吞；周期隔离；窗口按范围相交。")
    print(f"库：{db}\n锚点：{anchors}")
    return 0
