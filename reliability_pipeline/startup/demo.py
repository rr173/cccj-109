"""演示材料：真实二进制接入 + 直接建库的边界用例。

覆盖（每一项都有手工算定的期望判定，演示会逐条核对）：

- 设备 7（**真实二进制接入命令**生成的测量）：确认超时被拒绝 + 正常完成；
- dev 31：确认恰好落在 s+C（闭区间端点算确认）；
- dev 32：稳定完成恰好落在 s+W（期限当刻完成算成功）；
- dev 33：越线前值只差 1ns 过期（算越线）vs 恰好过期（不算越线）；
- dev 34：完成点恰好过期（不能成功）→ 恢复后完成点恰好刷新（可以成功）；
- dev 35：同刻读数乱序与冲突（冲突→未知→不算越线）、同刻一致重复（保留全部来源）；
- dev 36：单位与配方不符 → 温度未知 → 完成点判 indeterminate；
- dev 37：重叠候选共用后段稳定读数，确认期限不同 → 不同结论；
- dev 38：短温度缺口（indeterminate）vs 真实超温（rejected）；
- dev 39：分析截断（独立库）：已完成的不受影响，未结束的报观察未结束；
- dev 40：跨 boot_seq 状态隔离（新周期首个高读数不算越线）；
- dev 41：布局更换但通道/单位匹配，零阶保持状态不清空。
"""

from __future__ import annotations

import itertools
from typing import Dict, List, Tuple

from telemetry.generator import EPOCH0 as E0  # 演示统一时间基准

from .model import (
    REASON_CONFIRM_TIMEOUT, REASON_OBSERVATION_INCOMPLETE,
    REASON_STABLE_COMPLETED, REASON_TEMP_UNKNOWN_BEFORE_COMPLETION,
    REASON_TEMPERATURE_EXCEEDED, STATUS_CONFIRMED, STATUS_INDETERMINATE,
    STATUS_REJECTED,
)

SEC = 1_000_000_000
ANALYSIS_END_MAIN = E0 + 25 * SEC      # 主库分析截止
ANALYSIS_END_CUT = E0 + 10 * SEC       # 截断演示库分析截止

DEMO_RECIPE = {
    "name": "demo-start",
    "channels": {
        "pressure":    {"channel": 1, "unit": "kPa", "max_age_ns": SEC},
        "vibration":   {"channel": 2, "unit": "m/s", "max_age_ns": SEC},
        "temperature": {"channel": 0, "unit": "°C",  "max_age_ns": 2 * SEC},
    },
    "thresholds": {"P_start": 100.0, "P_run": 90.0, "V_run": 5.0, "T_max": 80.0},
    "durations": {"C": 2 * SEC, "H": 3 * SEC, "W": 10 * SEC},
}

# 期望判定：(device, boot, attempt_seq) -> (status, reason, s, confirm,
#                                          stable_start, stable_end, end)
# 时刻均为相对 E0 的纳秒偏移；None 表示该字段应为 NULL。
EXPECT_MAIN: Dict[Tuple[int, int, int], tuple] = {}
EXPECT_CUT: Dict[Tuple[int, int, int], tuple] = {}


def _frange(start: float, stop: float, step: float) -> List[float]:
    """[start, stop] 闭区间的等距浮点时刻（步长 0.5/1.5 等二进制可精确表示）。"""
    out = []
    t = start
    while t <= stop + 1e-9:
        out.append(round(t, 9))
        t += step
    return out


def T(sec: float) -> int:
    """相对 E0 的秒数 → 绝对 ts_ns。"""
    return E0 + int(round(sec * SEC))


# ----------------------------------------------------------------------
# 设备 7：真实二进制接入（tick_hz=1000，1 tick = 1ms）
# ----------------------------------------------------------------------

def build_binary_stream() -> bytes:
    """设备 7 的帧流：确认超时（资料完整的失败）+ 正常完成。"""
    from telemetry.generator import StreamBuilder
    b = StreamBuilder()
    b.boot(7, 1, E0, 1000)
    # ch0 温度 int16 毫°C→°C；ch1 压力 int32 Pa→kPa；ch2 振动 float32 mm/s→m/s
    b.layout(7, 5, [(0, 1, 1.0, 0.0, 1), (1, 2, 1.0, 0.0, 2), (2, 3, 1.0, 0.0, 3)])
    for t_ms in range(0, 22001, 250):
        if t_ms < 250:
            p = 50.0
        elif t_ms < 500:
            p = 60.0
        elif t_ms < 11000:
            p = 120.0
        elif t_ms < 11500:
            p = 70.0
        else:
            p = 130.0
        v = 0.5 if t_ms < 12000 else 6.0
        b.meas(7, 5, t_ms, [25000, int(p * 1000), v * 1000.0])
    return b.bytes()


# 设备 7 期望（毫秒换算为纳秒偏移）
EXPECT_MAIN[(7, 1, 0)] = (STATUS_REJECTED, REASON_CONFIRM_TIMEOUT,
                          500_000_000, None, None, None, 10_500_000_000)
EXPECT_MAIN[(7, 1, 1)] = (STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                          11_500_000_000, 12_000_000_000,
                          12_000_000_000, 15_000_000_000, 15_000_000_000)


# ----------------------------------------------------------------------
# 直接建库的小例子
# ----------------------------------------------------------------------

def _row_factory():
    counter = itertools.count()

    def mk(device: int, boot: int, channel: int, ts_ns: int, value: float,
           unit: str, layout: int = 1) -> tuple:
        seq = next(counter)
        # (file_id, frame_offset, device_id, boot_seq, layout_id, tick,
        #  cum_tick, ts_ns, ordinal, channel, raw_type, raw_value, value, unit)
        return (900, seq * 64, device, boot, layout, 0, 0, ts_ns, 0,
                channel, 3, float(value), float(value), unit)

    return mk


def build_main_rows() -> List[tuple]:
    """主库直接建库场景（dev 31..41）的全部测量行。"""
    mk = _row_factory()
    R: List[tuple] = []
    P, V, T_ = 1, 2, 0
    kPa, ms, deg = "kPa", "m/s", "°C"

    # --- dev 31：确认恰好落在 s+C ---
    R += [mk(31, 1, P, T(0), 50, kPa), mk(31, 1, P, T(0.5), 60, kPa)]
    R += [mk(31, 1, P, T(t), 120, kPa) for t in _frange(1.0, 7.0, 0.5)]
    R += [mk(31, 1, V, T(t), 0.5, ms) for t in _frange(0.0, 2.5, 0.5)]
    R += [mk(31, 1, V, T(t), 6.0, ms) for t in _frange(3.0, 7.0, 0.5)]
    R += [mk(31, 1, T_, T(t), 25, deg) for t in _frange(0.0, 7.5, 1.5)]
    EXPECT_MAIN[(31, 1, 0)] = (STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                               T(1.0) - E0, T(3.0) - E0, T(3.0) - E0,
                               T(6.0) - E0, T(6.0) - E0)

    # --- dev 32：稳定完成恰好落在 s+W（中途有一次打断）---
    R += [mk(32, 1, P, T(0), 50, kPa), mk(32, 1, P, T(0.5), 60, kPa)]
    R += [mk(32, 1, P, T(t), 120, kPa) for t in _frange(1.0, 2.0, 0.5)]
    R += [mk(32, 1, P, T(t), 80, kPa) for t in _frange(2.5, 7.5, 0.5)]
    R += [mk(32, 1, P, T(t), 120, kPa) for t in _frange(8.0, 11.5, 0.5)]
    R += [mk(32, 1, V, T(t), 0.5, ms) for t in _frange(0.0, 1.0, 0.5)]
    R += [mk(32, 1, V, T(t), 6.0, ms) for t in _frange(1.5, 11.5, 0.5)]
    R += [mk(32, 1, T_, T(t), 25, deg) for t in _frange(0.0, 12.0, 1.5)]
    EXPECT_MAIN[(32, 1, 0)] = (STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                               T(1.0) - E0, T(1.5) - E0, T(8.0) - E0,
                               T(11.0) - E0, T(11.0) - E0)
    # 压力 80→120 的回升本身也是一次越线：候选 #1 与 #0 共用后段稳定读数
    EXPECT_MAIN[(32, 1, 1)] = (STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                               T(8.0) - E0, T(8.0) - E0, T(8.0) - E0,
                               T(11.0) - E0, T(11.0) - E0)

    # --- dev 33：越线前值 1ns 之差（未过期算越线 / 恰好过期不算）---
    # 阶段 A：前值在 1ns 后才过期 → 越线 s1 = 1.5e9 - 1
    R += [mk(33, 1, P, T(0), 50, kPa), mk(33, 1, P, T(0.5), 60, kPa),
          mk(33, 1, P, T(1.5) - 1, 120, kPa)]
    R += [mk(33, 1, P, T(t), 120, kPa) for t in _frange(2.0, 5.5, 0.5)]
    R += [mk(33, 1, V, T(t), 0.5, ms) for t in _frange(0.0, 1.0, 0.5)]
    R += [mk(33, 1, V, T(t), 6.0, ms) for t in _frange(2.0, 5.5, 0.5)]
    R += [mk(33, 1, T_, T(t), 25, deg) for t in _frange(0.0, 6.0, 1.5)]
    EXPECT_MAIN[(33, 1, 0)] = (STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                               T(1.5) - 1 - E0, T(2.0) - E0, T(2.0) - E0,
                               T(5.0) - E0, T(5.0) - E0)
    # 阶段 B：前值恰好过期 → 不算越线；随后 低→高 才算
    R += [mk(33, 1, P, T(8.0), 60, kPa), mk(33, 1, P, T(9.0), 150, kPa),
          mk(33, 1, P, T(9.5), 70, kPa)]
    R += [mk(33, 1, P, T(t), 150, kPa) for t in _frange(10.0, 13.5, 0.5)]
    R += [mk(33, 1, V, T(t), 6.0, ms) for t in _frange(8.0, 13.5, 0.5)]
    R += [mk(33, 1, T_, T(t), 25, deg) for t in _frange(8.0, 14.0, 1.5)]
    EXPECT_MAIN[(33, 1, 1)] = (STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                               T(10.0) - E0, T(10.0) - E0, T(10.0) - E0,
                               T(13.0) - E0, T(13.0) - E0)

    # --- dev 34：完成点恰好过期（失败）→ 恢复后完成点恰好刷新（成功）---
    R += [mk(34, 1, P, T(14.5), 60, kPa)]
    R += [mk(34, 1, P, T(t), 120, kPa) for t in _frange(15.0, 17.0, 0.5)]
    # 17.0 的读数保持到 18.0 恰好过期：c=15+3=18.0 当刻压力未知 → 不能完成
    R += [mk(34, 1, P, T(18.4), 120, kPa), mk(34, 1, P, T(18.9), 120, kPa),
          mk(34, 1, P, T(19.8), 120, kPa), mk(34, 1, P, T(20.4), 120, kPa)]
    # 20.4 的读数在 21.4 恰好过期，21.4 同刻刷新 → c'=21.4 当刻仍成立 → 成功
    R += [mk(34, 1, P, T(t), 120, kPa) for t in _frange(21.4, 22.0, 0.5)]
    R += [mk(34, 1, V, T(t), 6.0, ms) for t in _frange(14.5, 22.0, 0.5)]
    R += [mk(34, 1, T_, T(t), 25, deg) for t in _frange(14.5, 22.0, 1.5)]
    EXPECT_MAIN[(34, 1, 0)] = (STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                               T(15.0) - E0, T(15.0) - E0, T(18.4) - E0,
                               T(21.4) - E0, T(21.4) - E0)

    # --- dev 35：同刻冲突（不算越线）+ 同刻一致重复（保留全部来源）---
    R += [mk(35, 1, P, T(0), 50, kPa), mk(35, 1, P, T(0.5), 60, kPa)]
    # 同刻两条压力读数值冲突（乱序插入：大值在前）→ 通道转未知
    R += [mk(35, 1, P, T(1.0), 125, kPa), mk(35, 1, P, T(1.0), 120, kPa)]
    R += [mk(35, 1, P, T(1.5), 120, kPa)]   # 未知→高：不算越线
    R += [mk(35, 1, P, T(2.0), 70, kPa), mk(35, 1, P, T(2.5), 120, kPa)]
    R += [mk(35, 1, P, T(t), 120, kPa) for t in _frange(3.0, 7.0, 0.5)]
    R += [mk(35, 1, V, T(t), 0.5, ms) for t in _frange(0.0, 2.5, 0.5)]
    # 同刻两条完全一致的振动读数（不同来源）→ 一次更新，保留全部来源
    R += [mk(35, 1, V, T(3.0), 6.0, ms), mk(35, 1, V, T(3.0), 6.0, ms)]
    R += [mk(35, 1, V, T(t), 6.0, ms) for t in _frange(3.5, 7.0, 0.5)]
    R += [mk(35, 1, T_, T(t), 25, deg) for t in _frange(0.0, 7.5, 1.5)]
    EXPECT_MAIN[(35, 1, 0)] = (STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                               T(2.5) - E0, T(3.0) - E0, T(3.0) - E0,
                               T(6.0) - E0, T(6.0) - E0)

    # --- dev 36：温度读数单位与配方不符 → 温度未知 → 完成点 indeterminate ---
    R += [mk(36, 1, P, T(0), 50, kPa), mk(36, 1, P, T(0.5), 60, kPa)]
    R += [mk(36, 1, P, T(t), 120, kPa) for t in _frange(1.0, 6.0, 0.5)]
    R += [mk(36, 1, V, T(t), 0.5, ms) for t in _frange(0.0, 1.0, 0.5)]
    R += [mk(36, 1, V, T(t), 6.0, ms) for t in _frange(1.5, 6.0, 0.5)]
    R += [mk(36, 1, T_, T(t), 25, deg) for t in (0.0, 1.5, 3.0)]
    R += [mk(36, 1, T_, T(3.5), 25, "Pa")]   # 单位不符 → 温度转未知
    R += [mk(36, 1, T_, T(t), 25, deg) for t in (4.0, 5.5, 7.0)]
    EXPECT_MAIN[(36, 1, 0)] = (STATUS_INDETERMINATE,
                               REASON_TEMP_UNKNOWN_BEFORE_COMPLETION,
                               T(1.0) - E0, T(1.5) - E0, T(1.5) - E0,
                               T(4.5) - E0, T(4.5) - E0)

    # --- dev 37：重叠候选共用后段读数，确认期限不同 → 不同结论 ---
    R += [mk(37, 1, P, T(0), 50, kPa), mk(37, 1, P, T(0.5), 60, kPa),
          mk(37, 1, P, T(1.0), 120, kPa), mk(37, 1, P, T(1.5), 70, kPa)]
    R += [mk(37, 1, P, T(t), 130, kPa) for t in _frange(2.0, 12.0, 0.5)]
    R += [mk(37, 1, V, T(t), 0.5, ms) for t in _frange(0.0, 3.0, 0.5)]
    R += [mk(37, 1, V, T(t), 6.0, ms) for t in _frange(3.5, 12.0, 0.5)]
    R += [mk(37, 1, T_, T(t), 25, deg) for t in _frange(0.0, 12.0, 1.5)]
    EXPECT_MAIN[(37, 1, 0)] = (STATUS_REJECTED, REASON_CONFIRM_TIMEOUT,
                               T(1.0) - E0, None, None, None, T(11.0) - E0)
    EXPECT_MAIN[(37, 1, 1)] = (STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                               T(2.0) - E0, T(3.5) - E0, T(3.5) - E0,
                               T(6.5) - E0, T(6.5) - E0)

    # --- dev 38 boot 1：短温度缺口 → 完成点 indeterminate ---
    R += [mk(38, 1, P, T(0), 50, kPa), mk(38, 1, P, T(0.5), 60, kPa)]
    R += [mk(38, 1, P, T(t), 120, kPa) for t in _frange(1.0, 6.0, 0.5)]
    R += [mk(38, 1, V, T(t), 0.5, ms) for t in _frange(0.0, 1.0, 0.5)]
    R += [mk(38, 1, V, T(t), 6.0, ms) for t in _frange(1.5, 6.0, 0.5)]
    R += [mk(38, 1, T_, T(t), 25, deg) for t in (0.0, 1.5, 4.0, 5.5, 7.0)]
    EXPECT_MAIN[(38, 1, 0)] = (STATUS_INDETERMINATE,
                               REASON_TEMP_UNKNOWN_BEFORE_COMPLETION,
                               T(1.0) - E0, T(1.5) - E0, T(1.5) - E0,
                               T(4.5) - E0, T(4.5) - E0)

    # --- dev 38 boot 2：真实超温 → 立即 rejected ---
    R += [mk(38, 2, P, T(0), 50, kPa), mk(38, 2, P, T(0.5), 60, kPa)]
    R += [mk(38, 2, P, T(t), 120, kPa) for t in _frange(1.0, 3.0, 0.5)]
    R += [mk(38, 2, V, T(t), 0.5, ms) for t in _frange(0.0, 1.0, 0.5)]
    R += [mk(38, 2, V, T(t), 6.0, ms) for t in _frange(1.5, 3.0, 0.5)]
    R += [mk(38, 2, T_, T(t), 25, deg) for t in (0.0, 1.5)]
    R += [mk(38, 2, T_, T(2.0), 85, deg)]    # 达到上限 → 立即拒绝
    R += [mk(38, 2, T_, T(t), 25, deg) for t in (2.5, 4.0)]
    EXPECT_MAIN[(38, 2, 0)] = (STATUS_REJECTED, REASON_TEMPERATURE_EXCEEDED,
                               T(1.0) - E0, T(1.5) - E0, None, None, T(2.0) - E0)

    # --- dev 40：跨 boot_seq 状态隔离 ---
    R += [mk(40, 1, P, T(0), 50, kPa), mk(40, 1, P, T(0.5), 60, kPa),
          mk(40, 1, V, T(0), 0.5, ms), mk(40, 1, T_, T(0), 25, deg)]
    # boot 2 首个压力读数即为高值：上一周期的低值不得泄漏 → 不算越线
    R += [mk(40, 2, P, T(10.0), 150, kPa), mk(40, 2, P, T(10.5), 70, kPa)]
    R += [mk(40, 2, P, T(t), 150, kPa) for t in _frange(11.0, 15.0, 0.5)]
    R += [mk(40, 2, V, T(t), 6.0, ms) for t in _frange(10.0, 15.0, 0.5)]
    R += [mk(40, 2, T_, T(t), 25, deg) for t in _frange(10.0, 14.5, 1.5)]
    EXPECT_MAIN[(40, 2, 0)] = (STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                               T(11.0) - E0, T(11.0) - E0, T(11.0) - E0,
                               T(14.0) - E0, T(14.0) - E0)

    # --- dev 41：布局更换（通道/单位仍匹配）不清空状态 ---
    R += [mk(41, 1, P, T(0.5), 60, kPa, layout=5)]
    R += [mk(41, 1, P, T(t), 120, kPa, layout=6) for t in _frange(1.0, 6.0, 0.5)]
    R += [mk(41, 1, V, T(t), 6.0, ms, layout=6) for t in _frange(0.5, 6.0, 0.5)]
    R += [mk(41, 1, T_, T(t), 25, deg) for t in _frange(0.0, 6.0, 1.5)]
    EXPECT_MAIN[(41, 1, 0)] = (STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                               T(1.0) - E0, T(1.0) - E0, T(1.0) - E0,
                               T(4.0) - E0, T(4.0) - E0)
    return R


def build_cut_rows() -> List[tuple]:
    """截断演示库（dev 39）：分析截止时一个已完成、一个未结束。"""
    mk = _row_factory()
    R: List[tuple] = []
    P, V, T_ = 1, 2, 0
    kPa, ms, deg = "kPa", "m/s", "°C"
    R += [mk(39, 1, P, T(0), 50, kPa), mk(39, 1, P, T(0.5), 60, kPa)]
    R += [mk(39, 1, P, T(t), 120, kPa) for t in _frange(1.0, 4.5, 0.5)]
    R += [mk(39, 1, P, T(t), 70, kPa) for t in _frange(5.0, 7.5, 0.5)]
    R += [mk(39, 1, P, T(t), 130, kPa) for t in _frange(8.0, 10.0, 0.5)]
    R += [mk(39, 1, V, T(t), 6.0, ms) for t in _frange(0.0, 10.0, 0.5)]
    R += [mk(39, 1, T_, T(t), 25, deg) for t in _frange(0.0, 9.0, 1.5)]
    EXPECT_CUT[(39, 1, 0)] = (STATUS_CONFIRMED, REASON_STABLE_COMPLETED,
                              T(1.0) - E0, T(1.0) - E0, T(1.0) - E0,
                              T(4.0) - E0, T(4.0) - E0)
    EXPECT_CUT[(39, 1, 1)] = (STATUS_INDETERMINATE, REASON_OBSERVATION_INCOMPLETE,
                              T(8.0) - E0, T(8.0) - E0, None, None,
                              ANALYSIS_END_CUT - E0)
    return R


def insert_rows(con, rows: List[tuple]) -> None:
    """把直接建库小例子的测量行写入 tele_samples。"""
    con.executemany("INSERT INTO tele_samples VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def check_expected(attempts, expected: Dict[Tuple[int, int, int], tuple],
                   label: str) -> List[str]:
    """逐条核对判定结果与手工期望；返回不一致说明列表（空=全部通过）。"""
    problems = []
    got = {}
    for at in attempts:
        got[(at.device_id, at.boot_seq, at.attempt_seq)] = (
            at.status, at.reason, at.s_ns - E0, at.confirm_ns,
            at.stable_start_ns, at.stable_end_ns, at.end_ns)
    for key, exp in sorted(expected.items()):
        status, reason, s, conf, ss, se, end = exp
        want = (status, reason, s,
                None if conf is None else conf,
                None if ss is None else ss,
                None if se is None else se, end)
        # 期望中的时刻是相对 E0 的偏移；confirm/stable/end 需加回 E0
        actual = got.get(key)
        if actual is None:
            problems.append(f"{label} {key}：缺少尝试，期望 {want}")
            continue
        a_status, a_reason, a_s, a_conf, a_ss, a_se, a_end = actual
        shifted = (a_status, a_reason, a_s,
                   None if a_conf is None else a_conf - E0,
                   None if a_ss is None else a_ss - E0,
                   None if a_se is None else a_se - E0,
                   a_end - E0)
        if shifted != want:
            problems.append(f"{label} {key}：判定 {shifted} != 期望 {want}")
    extra = set(got) - set(expected)
    for key in sorted(extra):
        problems.append(f"{label} {key}：多出的尝试 {got[key]}")
    return problems
