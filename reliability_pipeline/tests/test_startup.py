"""startup 子系统测试。

期望值手工计算（对照配方语义逐步推导），不经过被测实现；
引擎与独立参考判定器在全部用例与随机输入上交叉核对。
"""

from __future__ import annotations

import json
import random
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from startup import engine, reference, store as startup_store
from startup.cli import main as cli_main
from startup.demo import (ANALYSIS_END_CUT, ANALYSIS_END_MAIN, DEMO_RECIPE,
                          build_binary_stream, build_cut_rows, build_main_rows,
                          insert_rows)
from startup.engine import detect_group
from startup.model import SampleRow
from startup.recipe import RecipeError, parse_recipe
from startup.reference import reference_detect
from telemetry import store as tele_store

E0 = 1_700_000_000_000_000_000
REPO_ROOT = Path(__file__).resolve().parent.parent

# 小尺度配方：max_age 1000/1000/2000 ns，C=2000，H=3000，W=10000
RECIPE_OBJ = {
    "name": "test",
    "channels": {
        "pressure":    {"channel": 1, "unit": "kPa", "max_age_ns": 1000},
        "vibration":   {"channel": 2, "unit": "m/s", "max_age_ns": 1000},
        "temperature": {"channel": 0, "unit": "°C",  "max_age_ns": 2000},
    },
    "thresholds": {"P_start": 100.0, "P_run": 90.0, "V_run": 5.0, "T_max": 80.0},
    "durations": {"C": 2000, "H": 3000, "W": 10000},
}
UNITS = {0: "°C", 1: "kPa", 2: "m/s"}


def recipe():
    return parse_recipe(RECIPE_OBJ)


class Rows:
    """手工搭建单组测量行（device=1, boot=1）。"""

    def __init__(self, device=1, boot=1):
        self.device, self.boot = device, boot
        self.rows: list[SampleRow] = []
        self.seq = 0

    def add(self, channel, ts, value, unit=None):
        self.rows.append(SampleRow(
            self.device, self.boot, channel, ts, float(value),
            unit or UNITS[channel], 1, self.seq * 64, 0))
        self.seq += 1
        return self

    def series(self, channel, t0, t1, step, value, unit=None):
        t = t0
        while t <= t1:
            self.add(channel, t, value, unit)
            t += step
        return self

    def done(self, shuffle_same_ts=False, rng=None):
        rows = list(self.rows)
        if shuffle_same_ts:
            (rng or random.Random(0)).shuffle(rows)
        return sorted(rows, key=lambda r: r.ts_ns)


def run(rows, analysis_end=100_000, device=1, boot=1):
    return detect_group(rows, recipe(), analysis_end, device, boot)


def verdicts(attempts):
    return [(a.status, a.reason, a.s_ns, a.confirm_ns, a.stable_start_ns,
             a.stable_end_ns, a.end_ns) for a in attempts]


def check_vs_reference(rows, analysis_end=100_000, device=1, boot=1):
    got = [a.verdict_tuple() for a in detect_group(rows, recipe(),
                                                   analysis_end, device, boot)]
    want = [a.verdict_tuple() for a in reference_detect(rows, recipe(),
                                                        analysis_end, device, boot)]
    assert got == want


# ----------------------------------------------------------------------
# 配方校验
# ----------------------------------------------------------------------

def test_recipe_valid():
    r = recipe()
    assert r.C == 2000 and r.H == 3000 and r.W == 10000
    assert r.channels["pressure"].channel == 1
    assert r.role_of_channel(0) == "temperature"
    assert r.role_of_channel(9) is None


@pytest.mark.parametrize("mutate,frag", [
    (lambda o: o["channels"].pop("pressure"), "缺少角色"),
    (lambda o: o["channels"].__setitem__("vibration", {"channel": 1, "unit": "m/s", "max_age_ns": 1000}), "重复"),
    (lambda o: o["channels"]["pressure"].__setitem__("channel", 300), "<= 255"),
    (lambda o: o["channels"]["pressure"].__setitem__("channel", -1), ">= 0"),
    (lambda o: o["channels"]["pressure"].__setitem__("unit", ""), "非空字符串"),
    (lambda o: o["channels"]["pressure"].__setitem__("max_age_ns", 0), ">= 1"),
    (lambda o: o["channels"]["pressure"].__setitem__("max_age_ns", 1.5), "整数"),
    (lambda o: o["channels"]["pressure"].__setitem__("max_age_ns", True), "整数"),
    (lambda o: o["thresholds"].__setitem__("P_start", float("nan")), "有限"),
    (lambda o: o["thresholds"].__setitem__("T_max", float("inf")), "有限"),
    (lambda o: o["thresholds"].__setitem__("V_run", "5"), "数值"),
    (lambda o: o["durations"].__setitem__("C", 0), ">= 1"),
    (lambda o: o["durations"].__setitem__("H", 2.5), "整数"),
    (lambda o: o["durations"].__setitem__("C", 20000), "C<=W"),
    (lambda o: o["durations"].__setitem__("H", 20000), "H<=W"),
    (lambda o: o.__setitem__("unknown_key", 1), "不认识"),
    (lambda o: o["channels"]["pressure"].__setitem__("extra", 1), "不认识"),
    (lambda o: o["thresholds"].pop("P_run"), "缺少"),
    (lambda o: o.pop("durations"), "缺少顶层键"),
])
def test_recipe_invalid(mutate, frag):
    obj = json.loads(json.dumps(RECIPE_OBJ))
    mutate(obj)
    with pytest.raises(RecipeError, match=frag):
        parse_recipe(obj)


def test_recipe_boundary_c_eq_w_h_eq_w_ok():
    obj = json.loads(json.dumps(RECIPE_OBJ))
    obj["durations"] = {"C": 10000, "H": 10000, "W": 10000}
    r = parse_recipe(obj)
    assert r.C == r.W and r.H == r.W


# ----------------------------------------------------------------------
# 基本流程：正常完成 / 确认超时 / 稳定超时
# ----------------------------------------------------------------------

def test_normal_completion():
    rows = (Rows()
            .series(1, 0, 500, 500, 50).add(1, 900, 60)
            .series(1, 1000, 6000, 500, 120)
            .series(2, 0, 1000, 500, 0.5)
            .series(2, 1500, 6000, 500, 6.0)
            .series(0, 0, 9000, 1500, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("confirmed", "stable_completed", 1000, 1500,
                             1500, 4500, 4500)]
    check_vs_reference(rows)


def test_confirm_timeout_rejected_full_data():
    # 振动始终 0.5（已知但不到位），温度全程已知 → 资料完整的确认超时
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 12000, 500, 120)
            .series(2, 0, 12000, 500, 0.5)
            .series(0, 0, 12000, 1500, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("rejected", "confirm_timeout", 1000, None,
                             None, None, 11000)]
    check_vs_reference(rows)


def test_stable_timeout_rejected_full_data():
    # 确认后压力一直在 P_run 之下（已知），无未知状态 → 稳定超时 rejected
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60).add(1, 1000, 120)
            .series(1, 1500, 12000, 500, 80)     # 低于 P_run=90
            .series(2, 0, 1000, 500, 0.5)
            .series(2, 1500, 12000, 500, 6.0)
            .series(0, 0, 12000, 1500, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("rejected", "stable_timeout", 1000, 1500,
                             None, None, 11000)]
    check_vs_reference(rows)


def test_crossing_requires_fresh_previous_value():
    # 前一个在 1000 的读数到 2000 恰好过期：2000 的高值不算越线
    rows = (Rows()
            .add(1, 1000, 60).add(1, 2000, 150)
            .add(1, 2500, 70).add(1, 3000, 150)   # 这才是越线
            .series(1, 3500, 8000, 500, 150)
            .series(2, 2500, 8000, 500, 6.0)
            .series(0, 2000, 9000, 1500, 25)
            .done())
    at = run(rows)
    assert len(at) == 1 and at[0].s_ns == 3000
    assert at[0].status == "confirmed"
    check_vs_reference(rows)


def test_crossing_one_ns_before_expiry_counts():
    # 前值在越线时刻还差 1ns 才过期 → 算越线
    rows = (Rows()
            .add(1, 1000, 60).add(1, 1999, 150)
            .series(1, 2500, 8000, 500, 150)
            .series(2, 1500, 8000, 500, 6.0)
            .series(0, 1000, 9000, 1500, 25)
            .done())
    at = run(rows)
    assert len(at) == 1 and at[0].s_ns == 1999
    check_vs_reference(rows)


def test_confirm_exactly_at_window_end():
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 8000, 500, 120)
            .series(2, 0, 2500, 500, 0.5)
            .add(2, 3000, 6.0)                    # s+C = 3000，恰好确认
            .series(2, 3500, 8000, 500, 6.0)
            .series(0, 0, 9000, 1500, 25)
            .done())
    at = run(rows)
    assert at[0].confirm_ns == 3000
    assert at[0].status == "confirmed" and at[0].stable_end_ns == 6000
    check_vs_reference(rows)


def test_completion_exactly_at_deadline():
    # 确认后先被打断，恢复点 +H 恰好等于 s+W
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .add(1, 1000, 120).add(1, 1500, 120)
            .series(1, 2000, 7500, 500, 80)       # 低于 P_run，打断
            .series(1, 8000, 11500, 500, 120)     # 8000 恢复，c=11000=s+W
            .series(2, 0, 1000, 500, 0.5)
            .series(2, 1500, 11500, 500, 6.0)
            .series(0, 0, 12000, 1500, 25)
            .done())
    at = run(rows)
    assert at[0].status == "confirmed"
    assert (at[0].stable_start_ns, at[0].stable_end_ns, at[0].end_ns) == (
        8000, 11000, 11000)
    check_vs_reference(rows)


def test_completion_point_exactly_expires_fails_then_recovers():
    # 第一次完成点恰好过期 → 不能成功；恢复后第二次完成
    rows = (Rows()
            .add(1, 0, 60).add(1, 500, 120)
            .add(1, 1000, 120).add(1, 1500, 120)
            .add(1, 2500, 120)                    # 保持到 3500 恰好过期
            # c 本应是 500+3000=3500：当刻压力未知 → 不能完成
            .series(1, 4000, 9000, 500, 120)      # 4000 恢复，c'=7000
            .series(2, 0, 9000, 500, 6.0)
            .series(0, 0, 10000, 1500, 25)
            .done())
    at = run(rows)
    assert at[0].status == "confirmed"
    assert (at[0].stable_start_ns, at[0].stable_end_ns) == (4000, 7000)
    breaks = [e for a in at for e in a.evidence if e.kind == "stable_break"]
    assert breaks and breaks[0].t_start_ns == 3500
    check_vs_reference(rows)


def test_completion_point_refreshed_same_instant_succeeds():
    # 完成点当刻旧读数到期但有同刻刷新 → 可以成功
    rows = (Rows()
            .add(1, 0, 60).add(1, 500, 120)
            .series(1, 1000, 2500, 500, 120)      # 2500 的读数保持到 3500 恰好过期
            .add(1, 3500, 120)                    # 同刻刷新：c=3500 当刻仍成立
            .series(1, 4000, 8000, 500, 120)
            .series(2, 0, 8000, 500, 6.0)
            .series(0, 0, 9000, 1500, 25)
            .done())
    at = run(rows)
    assert at[0].status == "confirmed" and at[0].stable_end_ns == 3500
    check_vs_reference(rows)


# ----------------------------------------------------------------------
# 温度：超限立即拒绝（含完成点当刻优先）、缺口 → indeterminate
# ----------------------------------------------------------------------

def test_temperature_exceeded_rejects_immediately():
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 6000, 500, 120)
            .series(2, 0, 1000, 500, 0.5)
            .series(2, 1500, 6000, 500, 6.0)
            .add(0, 0, 25).add(0, 1500, 25)
            .add(0, 2000, 85)                     # 达到上限 → 立即拒绝
            .add(0, 2500, 25).add(0, 4000, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("rejected", "temperature_exceeded", 1000, 1500,
                             None, None, 2000)]
    ev = [e for e in at[0].evidence if e.kind == "temperature_exceeded"][0]
    assert ev.refs and ev.refs[0].value == 85.0
    check_vs_reference(rows)


def test_temperature_violation_at_completion_point_wins():
    # 完成点当刻温度升至上限：拒绝优先于确认
    rows = (Rows()
            .add(1, 0, 60).add(1, 500, 120)
            .series(1, 1000, 6000, 500, 120)
            .series(2, 0, 6000, 500, 6.0)
            .add(0, 0, 25).add(0, 1500, 25).add(0, 3000, 25)
            .add(0, 3500, 90)                     # c=3500 当刻超限
            .add(0, 5000, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("rejected", "temperature_exceeded", 500, 500,
                             None, None, 3500)]
    check_vs_reference(rows)


def test_candidate_born_into_violation_rejected_at_s():
    rows = (Rows()
            .add(0, 0, 90)                        # 候选建立前温度已超限
            .add(1, 500, 60).add(1, 1000, 120)    # s=1000：温度仍超限（保持到 2000）
            .series(1, 1500, 6000, 500, 120)
            .series(2, 0, 6000, 500, 6.0)
            .add(0, 2000, 25).add(0, 4000, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("rejected", "temperature_exceeded", 1000, None,
                             None, None, 1000)]
    check_vs_reference(rows)


def test_short_temperature_gap_makes_completion_indeterminate():
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 7000, 500, 120)
            .series(2, 0, 1000, 500, 0.5)
            .series(2, 1500, 7000, 500, 6.0)
            .add(0, 0, 25).add(0, 1500, 25)       # 保持到 3500
            .add(0, 4000, 25)                     # [3500, 4000) 温度未知
            .add(0, 5500, 25).add(0, 7000, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("indeterminate",
                             "temperature_unknown_before_completion",
                             1000, 1500, 1500, 4500, 4500)]
    gaps = [e for e in at[0].evidence if e.kind == "temperature_unknown"]
    assert gaps and gaps[0].t_start_ns == 3500 and gaps[0].t_end_ns == 4000
    check_vs_reference(rows)


def test_missed_candidate_still_watches_temperature():
    # 确认窗口错过后仍观察温度：窗口后超温 → rejected（而非 confirm_timeout）
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 6000, 500, 120)
            .series(2, 0, 6000, 500, 0.5)         # 窗口 [1000,3000] 内未到位
            .add(0, 0, 25).add(0, 1500, 25)
            .add(0, 3500, 85)                     # 窗口错过之后超温
            .add(0, 5500, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("rejected", "temperature_exceeded", 1000, None,
                             None, None, 3500)]
    check_vs_reference(rows)


# ----------------------------------------------------------------------
# 未知状态的参与判定
# ----------------------------------------------------------------------

def test_confirm_window_vibration_unknown_gives_indeterminate():
    # 确认窗口内振动有一段未知（缺读数），其余完整 → 超时判 indeterminate
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 12000, 500, 120)
            .add(2, 0, 0.5)                       # 保持到 1000
            .series(2, 2500, 12000, 500, 0.5)     # (1000, 2500) 振动未知
            .series(0, 0, 12000, 1500, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("indeterminate", "confirm_timeout_unknown",
                             1000, None, None, None, 11000)]
    parts = [e for e in at[0].evidence if e.kind == "unknown_participating"]
    assert any(e.role == "vibration" for e in parts)
    check_vs_reference(rows)


def test_stable_timeout_with_pressure_unknown_gives_indeterminate():
    # 确认后压力有一段过期未知，直到期限也没稳定段 → indeterminate
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60).add(1, 1000, 120)
            .add(1, 1500, 120)                    # 保持到 2500
            .series(1, 4000, 12000, 500, 80)      # (2500,4000) 未知；之后低于 P_run
            .series(2, 0, 1000, 500, 0.5)
            .series(2, 1500, 12000, 500, 6.0)
            .series(0, 0, 12000, 1500, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("indeterminate", "stable_timeout_unknown",
                             1000, 1500, None, None, 11000)]
    check_vs_reference(rows)


def test_missed_candidate_temperature_unknown_gives_indeterminate():
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 12000, 500, 120)
            .series(2, 0, 12000, 500, 0.5)
            .add(0, 0, 25).add(0, 1500, 25)       # 保持到 3500
            .add(0, 5000, 25)                     # [3500,5000) 温度未知
            .series(0, 6500, 12500, 1500, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("indeterminate", "confirm_timeout_unknown",
                             1000, None, None, None, 11000)]
    check_vs_reference(rows)


def test_candidate_born_into_temperature_gap():
    # 候选建立时温度正处于未知缺口（跨越 s 的未知区间也算参与）
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 7000, 500, 120)
            .series(2, 0, 7000, 500, 6.0)
            .add(0, 0, 25)                        # 保持到 2000
            .add(0, 4500, 25)                     # [2000, 4500) 温度未知，覆盖 s
            .add(0, 6000, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("indeterminate",
                             "temperature_unknown_before_completion",
                             1000, 1000, 1000, 4000, 4000)]
    check_vs_reference(rows)


def test_no_temperature_reading_at_all():
    # 全程无温度读数：完成点判 indeterminate；超时也判 indeterminate
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 7000, 500, 120)
            .series(2, 0, 7000, 500, 6.0)
            .done())
    at = run(rows)
    assert verdicts(at) == [("indeterminate",
                             "temperature_unknown_before_completion",
                             1000, 1000, 1000, 4000, 4000)]
    check_vs_reference(rows)

    rows2 = (Rows()
             .add(1, 0, 50).add(1, 500, 60)
             .series(1, 1000, 12000, 500, 120)
             .series(2, 0, 12000, 500, 0.5)
             .done())
    at2 = run(rows2)
    assert verdicts(at2) == [("indeterminate", "confirm_timeout_unknown",
                              1000, None, None, None, 11000)]
    check_vs_reference(rows2)


# ----------------------------------------------------------------------
# 同刻读数：冲突 / 重复 / 单位不符 / 乱序无关
# ----------------------------------------------------------------------

def test_same_instant_conflict_blocks_crossing():
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .add(1, 1000, 125).add(1, 1000, 120)  # 同刻冲突（乱序插入）
            .add(1, 1500, 120)                    # 未知→高：不算越线
            .add(1, 2000, 70).add(1, 2500, 120)   # 低→高：越线
            .series(1, 3000, 8000, 500, 120)
            .series(2, 2000, 8000, 500, 6.0)
            .series(0, 0, 9000, 1500, 25)
            .done())
    at = run(rows)
    assert len(at) == 1 and at[0].s_ns == 2500
    check_vs_reference(rows)


def test_conflict_evidence_locates_both_parties():
    # 确认窗口内振动同刻冲突 → 振动未知参与判定 → 冲突双方都可定位
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 12000, 500, 120)
            .add(2, 0, 0.5).add(2, 500, 0.5)
            .add(2, 1500, 6.0).add(2, 1500, 4.0)  # 同刻冲突 → 振动未知
            .series(2, 2000, 12000, 500, 0.5)     # 恢复后仍不到位
            .series(0, 0, 12000, 1500, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("indeterminate", "confirm_timeout_unknown",
                             1000, None, None, None, 11000)]
    conflicts = [e for e in at[0].evidence if e.kind == "conflict"]
    assert conflicts and len(conflicts[0].refs) == 2   # 冲突双方都可定位
    assert {r.value for r in conflicts[0].refs} == {4.0, 6.0}
    check_vs_reference(rows)


def test_same_instant_identical_duplicates_keep_all_sources():
    rows = (Rows()
            .add(1, 0, 60).add(1, 500, 120)
            .series(1, 1000, 6000, 500, 120)
            .add(2, 0, 6.0)
            .add(2, 500, 6.0).add(2, 500, 6.0)    # 同刻一致重复 → 一次更新
            .series(2, 1000, 6000, 500, 6.0)
            .series(0, 0, 7000, 1500, 25)
            .done())
    at = run(rows)
    assert at[0].status == "confirmed" and at[0].confirm_ns == 500
    confirm = [e for e in at[0].evidence if e.kind == "confirm"][0]
    assert len(confirm.refs) == 2                        # 保留全部来源
    assert {r.ts_ns for r in confirm.refs} == {500}
    assert len({r.frame_offset for r in confirm.refs}) == 2
    check_vs_reference(rows)


def test_unit_mismatch_makes_channel_unknown():
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 7000, 500, 120)
            .series(2, 0, 1000, 500, 0.5)
            .series(2, 1500, 7000, 500, 6.0)
            .add(0, 0, 25).add(0, 1500, 25)
            .add(0, 2000, 25, unit="Pa")          # 单位不符 → 温度未知
            .add(0, 3500, 25).add(0, 5000, 25).add(0, 6500, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [("indeterminate",
                             "temperature_unknown_before_completion",
                             1000, 1500, 1500, 4500, 4500)]
    mm = [e for e in at[0].evidence if e.kind == "unit_mismatch"]
    assert mm and mm[0].refs[0].unit == "Pa"
    check_vs_reference(rows)


def test_row_order_within_same_instant_irrelevant():
    def build(shuffle):
        r = (Rows()
             .add(1, 0, 50).add(1, 500, 60)
             .series(1, 1000, 6000, 500, 120)
             .add(2, 1500, 6.0).add(2, 1500, 6.0)
             .series(2, 2000, 6000, 500, 6.0)
             .series(0, 0, 7000, 1500, 25))
        return r.done(shuffle_same_ts=shuffle, rng=random.Random(7))
    a = verdicts(run(build(False)))
    b = verdicts(run(build(True)))
    assert a == b


# ----------------------------------------------------------------------
# 重叠候选 / 截断 / 跨周期隔离 / 布局连续
# ----------------------------------------------------------------------

def test_overlapping_candidates_different_verdicts():
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .add(1, 1000, 120)                    # 候选 1：s=1000
            .add(1, 1500, 70)
            .add(1, 2000, 130)                    # 候选 2：s=2000
            .series(1, 2500, 13000, 500, 130)
            .series(2, 0, 3000, 500, 0.5)         # 候选 1 窗口 [1000,3000] 错过
            .series(2, 3500, 13000, 500, 6.0)     # 候选 2 在 3500 确认
            .series(0, 0, 13000, 1500, 25)
            .done())
    at = run(rows)
    assert verdicts(at) == [
        ("rejected", "confirm_timeout", 1000, None, None, None, 11000),
        ("confirmed", "stable_completed", 2000, 3500, 3500, 6500, 6500),
    ]
    check_vs_reference(rows)


def test_analysis_cutoff_observation_incomplete():
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 9000, 500, 120)
            .series(2, 0, 9000, 500, 6.0)
            .series(0, 0, 9000, 1500, 25)
            .done())
    # c 本应是 1000+3000=4000；截止 3500 < 4000 → 观察未结束
    at = run(rows, analysis_end=3500)
    assert verdicts(at) == [("indeterminate", "observation_incomplete",
                             1000, 1000, None, None, 3500)]
    assert "不能报超时" in at[0].detail
    check_vs_reference(rows, analysis_end=3500)


def test_analysis_cutoff_after_deadline_is_normal_timeout():
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 12000, 500, 120)
            .series(2, 0, 12000, 500, 0.5)
            .series(0, 0, 12000, 1500, 25)
            .done())
    at = run(rows, analysis_end=20000)            # 截止晚于期限 11000
    assert at[0].reason == "confirm_timeout" and at[0].status == "rejected"
    check_vs_reference(rows, analysis_end=20000)


def test_confirm_at_deadline_when_c_eq_w():
    # C == W：确认恰好落在 s+C == s+W → 确认有效，随后按稳定超时判
    r = parse_recipe({
        "name": "t",
        "channels": {
            "pressure":    {"channel": 1, "unit": "kPa", "max_age_ns": 1000},
            "vibration":   {"channel": 2, "unit": "m/s", "max_age_ns": 1000},
            "temperature": {"channel": 0, "unit": "°C",  "max_age_ns": 2000}},
        "thresholds": {"P_start": 100.0, "P_run": 90.0, "V_run": 5.0,
                       "T_max": 80.0},
        "durations": {"C": 2000, "H": 2000, "W": 2000}})
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .add(1, 1000, 120)
            .series(1, 1500, 5000, 500, 80)       # 低于 P_run
            .series(2, 0, 2500, 500, 0.5)
            .add(2, 3000, 6.0)                    # 恰好 s+C == s+W 确认
            .series(2, 3500, 5000, 500, 6.0)
            .series(0, 0, 5000, 1500, 25)
            .done())
    got = detect_group(rows, r, 100_000, 1, 1)
    want = reference_detect(rows, r, 100_000, 1, 1)
    assert [(a.status, a.reason, a.confirm_ns, a.end_ns) for a in got] == [
        ("rejected", "stable_timeout", 3000, 3000)]
    assert [a.verdict_tuple() for a in got] == [a.verdict_tuple() for a in want]


def test_crossing_exactly_at_analysis_end():
    rows = (Rows()
            .add(1, 0, 50).add(1, 3500, 60).add(1, 4000, 120)
            .series(2, 3000, 4000, 500, 6.0)
            .series(0, 2000, 4000, 1500, 25)
            .done())
    at = run(rows, analysis_end=4000)
    assert verdicts(at) == [("indeterminate", "observation_incomplete",
                             4000, 4000, None, None, 4000)]
    check_vs_reference(rows, analysis_end=4000)


def test_temperature_violation_at_deadline_wins():
    rows = (Rows()
            .add(1, 0, 50).add(1, 500, 60)
            .series(1, 1000, 12000, 500, 120)
            .series(2, 0, 12000, 500, 0.5)
            .add(0, 0, 25).add(0, 9500, 25)
            .add(0, 11000, 85)                    # 期限当刻超温 → 拒绝优先
            .done())
    at = run(rows)
    assert verdicts(at) == [("rejected", "temperature_exceeded", 1000, None,
                             None, None, 11000)]
    check_vs_reference(rows)


def test_boot_seq_isolation():
    # boot 1 的低值历史不得泄漏到 boot 2：boot 2 首个高读数不算越线
    rows_b1 = Rows(device=1, boot=1).add(1, 0, 50).add(1, 500, 60).done()
    rows_b2 = (Rows(device=1, boot=2)
               .add(1, 1000, 150)                 # 新周期首个读数即高：不算越线
               .add(1, 1500, 70).add(1, 2000, 150)
               .series(1, 2500, 7000, 500, 150)
               .series(2, 1500, 7000, 500, 6.0)
               .series(0, 1000, 8000, 1500, 25)
               .done())
    assert run(rows_b1, device=1, boot=1) == []
    at2 = run(rows_b2, device=1, boot=2)
    assert len(at2) == 1 and at2[0].s_ns == 2000
    check_vs_reference(rows_b2, device=1, boot=2)


def test_layout_change_does_not_clear_state():
    # 布局号更换（通道/单位仍匹配）不清空零阶保持状态：跨布局算越线
    rows = [
        SampleRow(1, 1, 1, 500, 60.0, "kPa", 1, 0, 0),      # layout 5 的读数
        SampleRow(1, 1, 1, 1000, 120.0, "kPa", 1, 64, 0),   # layout 6 的读数
    ]
    rows += [SampleRow(1, 1, 1, t, 120.0, "kPa", 1, t, 0)
             for t in range(1500, 7000, 500)]
    rows += [SampleRow(1, 1, 2, t, 6.0, "m/s", 1, t, 0)
             for t in range(500, 7000, 500)]
    rows += [SampleRow(1, 1, 0, t, 25.0, "°C", 1, t, 0)
             for t in range(0, 7000, 1500)]
    rows.sort(key=lambda r: r.ts_ns)
    at = run(rows)
    assert len(at) == 1 and at[0].s_ns == 1000 and at[0].status == "confirmed"
    check_vs_reference(rows)


def test_unsorted_rows_rejected():
    rows = [SampleRow(1, 1, 1, 1000, 60.0, "kPa", 1, 0, 0),
            SampleRow(1, 1, 1, 500, 50.0, "kPa", 1, 64, 0)]
    with pytest.raises(ValueError, match="升序"):
        run(rows)


# ----------------------------------------------------------------------
# 随机交叉核对：引擎 vs 独立参考判定器
# ----------------------------------------------------------------------

def _random_group_rows(rng, device, boot):
    """场景化随机组：在脚本化启机阶段上叠加随机间隔/抖动/异常（冲突、
    重复、单位不符、缺测），使各类判定路径都被覆盖。"""
    rows = []
    seq = 0

    def add(channel, t, v, unit):
        nonlocal seq
        rows.append(SampleRow(device, boot, channel, t, float(v), unit,
                              1, seq * 64, 0))
        seq += 1
        r = rng.random()
        if r < 0.04:      # 同刻冲突
            rows.append(SampleRow(device, boot, channel, t, float(v) + 1.0,
                                  unit, 1, seq * 64, 0))
            seq += 1
        elif r < 0.07:    # 同刻一致重复
            rows.append(SampleRow(device, boot, channel, t, float(v), unit,
                                  1, seq * 64, 0))
            seq += 1
        elif r < 0.09:    # 单位不符
            rows.append(SampleRow(device, boot, channel, t, float(v), "Pa",
                                  1, seq * 64, 0))
            seq += 1

    def series(channel, t0, t1, value_fn, unit, gaps=(400, 500, 900, 1000)):
        t = t0
        while t <= t1:
            add(channel, t, value_fn() if callable(value_fn) else value_fn, unit)
            t += rng.choice(gaps + (() if rng.random() > 0.15 else (2500,)))

    kind = rng.choice(["success", "confirm_timeout", "stable_timeout",
                       "overtemp", "noise"])
    if kind == "noise":
        for channel, unit, values in (
                (1, "kPa", [50.0, 60.0, 80.0, 95.0, 100.0, 120.0, 150.0]),
                (2, "m/s", [0.5, 4.9, 5.0, 6.0]),
                (0, "°C", [25.0, 25.0, 25.0, 79.9, 80.0, 85.0])):
            t = 0
            for _ in range(rng.randint(4, 16)):
                t += rng.choice([100, 200, 400, 500, 900, 1000, 1100, 2000, 2500])
                add(channel, t, rng.choice(values), unit)
        rows.sort(key=lambda r: r.ts_ns)
        return rows

    s = rng.randint(1500, 4000)
    end = s + rng.randint(9000, 12000)
    # 越线前低值（保持新鲜）
    series(1, 0, s - 500, lambda: rng.choice([50.0, 60.0, 80.0]), "kPa")
    add(1, s, rng.choice([120.0, 150.0]), "kPa")          # 越线
    # 越线后压力
    if kind == "stable_timeout":
        series(1, s + 500, end, 80.0, "kPa")              # 持续低于 P_run
    else:
        series(1, s + 500, end, lambda: rng.choice([120.0, 130.0]), "kPa")
    # 振动
    series(2, 0, s, 0.5, "m/s")
    if kind == "confirm_timeout":
        series(2, s + 500, end, 0.5, "m/s")               # 始终不到位
    else:
        v_up = s + rng.choice([0, 500, 1500])             # 窗口内某刻到位
        series(2, s + 500, v_up, 0.5, "m/s")
        series(2, v_up + 1 if v_up < s + 500 else v_up, end, 6.0, "m/s")
    # 温度
    if kind == "overtemp":
        series(0, 0, s + 1500, 25.0, "°C", gaps=(1000, 1500))
        add(0, s + 2000, rng.choice([80.0, 85.0]), "°C")  # 达到上限
        series(0, s + 2500, end, 25.0, "°C", gaps=(1000, 1500))
    else:
        series(0, 0, end, 25.0, "°C", gaps=(1000, 1500))
    rows.sort(key=lambda r: r.ts_ns)
    return rows


@pytest.mark.parametrize("seed", range(40))
def test_random_cross_check(seed):
    rng = random.Random(seed)
    analysis_end = rng.choice([8000, 12000, 20000, 100_000])
    for device, boot in ((1, 1), (1, 2), (2, 1)):
        rows = _random_group_rows(rng, device, boot)
        got = [a.verdict_tuple() for a in detect_group(rows, recipe(),
                                                       analysis_end, device, boot)]
        want = [a.verdict_tuple() for a in reference_detect(rows, recipe(),
                                                            analysis_end,
                                                            device, boot)]
        assert got == want, f"seed={seed} 组=({device},{boot})"
        # 不变量：结束时刻不早于 s；confirmed 必有完整稳定段
        for a in detect_group(rows, recipe(), analysis_end, device, boot):
            assert a.end_ns >= a.s_ns
            if a.status == "confirmed":
                assert a.stable_end_ns - a.stable_start_ns == 3000
                assert a.confirm_ns is not None


# ----------------------------------------------------------------------
# 端到端：真实二进制接入命令 → 识别 → 公开查询命令
# ----------------------------------------------------------------------

def _ingest_via_cli(db: str, bin_path: Path) -> None:
    rc = subprocess.run(
        [sys.executable, "-m", "telemetry.cli", "ingest",
         "--db", db, "--file", str(bin_path)],
        cwd=REPO_ROOT).returncode
    assert rc == 0


def test_end_to_end_binary_ingest_then_detect(tmp_path):
    db = str(tmp_path / "e2e.duckdb")
    bin_path = tmp_path / "device7.bin"
    bin_path.write_bytes(build_binary_stream())
    _ingest_via_cli(db, bin_path)

    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_text(json.dumps(DEMO_RECIPE, ensure_ascii=False),
                           encoding="utf-8")
    rc = cli_main(["run", "--db", db, "--recipe", str(recipe_path),
                   "--analysis-end-ns", str(ANALYSIS_END_MAIN),
                   "--run-id", "e2e",
                   "--jsonl", str(tmp_path / "out.jsonl")])
    assert rc == 0

    con = startup_store.connect(db)
    rows = con.execute(
        "SELECT device_id, boot_seq, attempt_seq, s_ns, confirm_ns, "
        "stable_start_ns, stable_end_ns, end_ns, status, reason "
        "FROM startup_attempts WHERE run_id = 'e2e' "
        "ORDER BY attempt_seq").fetchall()
    assert [(r[8], r[9]) for r in rows] == [
        ("rejected", "confirm_timeout"), ("confirmed", "stable_completed")]
    assert rows[0][3] == E0 + 500_000_000 and rows[0][7] == E0 + 10_500_000_000
    assert rows[1][3] == E0 + 11_500_000_000
    assert rows[1][4] == E0 + 12_000_000_000     # confirm
    assert rows[1][5:7] == (E0 + 12_000_000_000, E0 + 15_000_000_000)
    # 证据定位键逐条回到 tele_samples 真实行
    refs = con.execute(
        "SELECT file_id, frame_offset, ordinal, channel, ts_ns, value, unit "
        "FROM startup_evidence_refs WHERE run_id = 'e2e'").fetchall()
    assert refs
    for ref in refs:
        hit = con.execute(
            "SELECT 1 FROM tele_samples WHERE file_id = ? AND frame_offset = ? "
            "AND ordinal = ? AND channel = ? AND ts_ns = ? AND value = ? "
            "AND unit = ?", ref).fetchone()
        assert hit is not None, f"证据定位键无法回溯：{ref}"
    # 确认用的读数：振动通道在确认时刻的值 6.0
    confirm_ref = con.execute(
        "SELECT r.channel, r.ts_ns, r.value FROM startup_evidence_refs r "
        "JOIN startup_evidence e USING (run_id, device_id, boot_seq, "
        "attempt_seq, ev_seq) "
        "WHERE r.run_id = 'e2e' AND e.kind = 'confirm'").fetchone()
    assert confirm_ref == (2, E0 + 12_000_000_000, 6.0)
    con.close()

    # JSONL 导出可读且字段齐全
    lines = (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    obj = json.loads(lines[1])
    assert obj["status"] == "confirmed" and obj["evidence"]

    # 公开查询命令
    assert cli_main(["attempts", "--db", db, "--run-id", "e2e"]) == 0
    assert cli_main(["evidence", "--db", db, "--run-id", "e2e",
                     "--device", "7", "--boot-seq", "1",
                     "--attempt-seq", "1"]) == 0
    # 交叉核对命令
    assert cli_main(["verify", "--db", db, "--recipe", str(recipe_path),
                     "--analysis-end-ns", str(ANALYSIS_END_MAIN)]) == 0
    # run_id 不可覆写
    assert cli_main(["run", "--db", db, "--recipe", str(recipe_path),
                     "--analysis-end-ns", str(ANALYSIS_END_MAIN),
                     "--run-id", "e2e"]) == 3


def test_cli_rejects_invalid_recipe_before_compute(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"channels": {}, "thresholds": {},
                               "durations": {}}), encoding="utf-8")
    db = str(tmp_path / "x.duckdb")
    rc = cli_main(["run", "--db", db, "--recipe", str(bad),
                   "--analysis-end-ns", "1000", "--run-id", "x"])
    assert rc == 2
    assert not Path(db).exists()      # 计算前报错：连库都不建


def test_demo_scenarios_match_hand_computed(tmp_path):
    """演示材料（真实接入 + 直接建库）与手工期望逐条一致，且引擎=参考。"""
    from startup.demo import EXPECT_CUT, EXPECT_MAIN, check_expected
    db = str(tmp_path / "main.duckdb")
    bin_path = tmp_path / "device7.bin"
    bin_path.write_bytes(build_binary_stream())
    _ingest_via_cli(db, bin_path)
    con = startup_store.connect(db)
    insert_rows(con, build_main_rows())
    attempts = engine.run_detection(con, parse_recipe(DEMO_RECIPE),
                                    ANALYSIS_END_MAIN)
    assert check_expected(attempts, EXPECT_MAIN, "主库") == []
    mismatches = []
    for (dev, boot), rows in engine.iter_groups(con, parse_recipe(DEMO_RECIPE),
                                                ANALYSIS_END_MAIN):
        got = [a.verdict_tuple() for a in detect_group(
            rows, parse_recipe(DEMO_RECIPE), ANALYSIS_END_MAIN, dev, boot)]
        want = [a.verdict_tuple() for a in reference_detect(
            rows, parse_recipe(DEMO_RECIPE), ANALYSIS_END_MAIN, dev, boot)]
        if got != want:
            mismatches.append((dev, boot))
    assert mismatches == []
    con.close()

    db2 = str(tmp_path / "cut.duckdb")
    con2 = startup_store.connect(db2)
    insert_rows(con2, build_cut_rows())
    attempts2 = engine.run_detection(con2, parse_recipe(DEMO_RECIPE),
                                     ANALYSIS_END_CUT)
    assert check_expected(attempts2, EXPECT_CUT, "截断库") == []
    con2.close()


def test_existing_entries_still_work(tmp_path):
    """原有接入/查询/归档/可靠性分析入口仍可用（冒烟）。"""
    from pipeline.cli import main as pipeline_main
    from telemetry.archcli import main as arch_main
    from telemetry.cli import main as tele_main

    # 遥测：生成 → 接入 → 查询
    data_dir = tmp_path / "tele"
    assert tele_main(["gen", "--out", str(data_dir), "--scenario", "mixed",
                      "--seed", "1"]) == 0
    db = str(tmp_path / "t.duckdb")
    assert tele_main(["ingest", "--db", db,
                      "--file", str(data_dir / "mixed.bin")]) == 0
    assert tele_main(["samples", "--db", db, "--device", "7"]) == 0
    # 归档：构建 → 核验
    arch = tmp_path / "a.tpla"
    assert arch_main(["build", "--db", db, "--device", "7", "--channel", "0",
                      "--start-ns", "0", "--end-ns", str(2**62),
                      "--epsilon", "0.5", "--out", str(arch)]) == 0
    assert arch_main(["verify", "--file", str(arch), "--db", db]) == 0
    # 可靠性分析：金标准 → 分析 → 核对
    gold = tmp_path / "gold"
    assert pipeline_main(["gen-gold", "--out", str(gold)]) == 0
    rdb = str(tmp_path / "r.duckdb")
    assert pipeline_main(["analyze", "--data", str(gold), "--db", rdb,
                          "--run-id", "g1", "--knowledge-cutoff", "2024-02-15",
                          "--observation-end", "2024-01-31"]) == 0
    assert pipeline_main(["verify-gold", "--data", str(gold), "--db", rdb]) == 0
