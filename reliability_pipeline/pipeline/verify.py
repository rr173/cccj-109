"""金标准核对：管线输出 vs 手算期望值（精确分数）。"""

from __future__ import annotations

from fractions import Fraction
from typing import Dict, List

from .cohort import CohortResult
from .estimate import GroupEstimate
from .gold_data import EXPECTED

TOL = 1e-12


def verify_gold_run(
    run_id: str,
    cohort: CohortResult,
    estimates: Dict[str, GroupEstimate],
) -> List[str]:
    """返回违规列表（空 = 与手算金标准完全一致）。"""
    if run_id not in EXPECTED:
        raise KeyError(f"未知金标准运行: {run_id}")
    exp = EXPECTED[run_id]
    problems: List[str] = []

    # ---- 设备时间段 / 入选拒收清单
    actual = {r["device_id"]: r for r in cohort.device_rows}
    for dev_id, (cs, L, T, status, term) in exp["devices"].items():
        row = actual.get(dev_id)
        if row is None:
            problems.append(f"设备 {dev_id} 缺失")
            continue
        if row["cohort_status"] != cs:
            problems.append(f"{dev_id}: cohort_status {row['cohort_status']} != {cs}")
        if row["L"] != L or row["T"] != T:
            problems.append(f"{dev_id}: (L,T)=({row['L']},{row['T']}) != ({L},{T})")
        if row["status"] != status:
            problems.append(f"{dev_id}: status {row['status']} != {status}")
        if (row["terminal_event_id"] or None) != term:
            problems.append(f"{dev_id}: terminal_event_id {row['terminal_event_id']} != {term}")
    extra = set(actual) - set(exp["devices"])
    if extra:
        problems.append(f"多出设备: {sorted(extra)}")

    # ---- 拒收明细
    actual_rej = {(r["level"], r["entity_id"], r["reason"]) for r in cohort.rejections}
    if actual_rej != exp["rejections"]:
        problems.append(f"拒收明细不一致: 多 {sorted(actual_rej - exp['rejections'])} "
                        f"少 {sorted(exp['rejections'] - actual_rej)}")

    # ---- 分组曲线
    for group, exp_rows in exp["groups"].items():
        ge = estimates.get(group)
        actual_rows = ge.rows if ge else []
        if len(actual_rows) != len(exp_rows):
            problems.append(f"[{group}] 曲线行数 {len(actual_rows)} != {len(exp_rows)}")
            continue
        for ar, er in zip(actual_rows, exp_rows):
            t, n, d1, d2, c, s, c1, c2, skm = er
            tag = f"[{group} t={t}]"
            if ar["t"] != t:
                problems.append(f"{tag} 时点错位: {ar['t']}")
                continue
            for key, want in [("n_risk", n), ("d_failure", d1),
                              ("d_retirement", d2), ("n_censored", c)]:
                if ar[key] != want:
                    problems.append(f"{tag} {key}: {ar[key]} != {want}")
            for key, want in [("surv", s), ("cif_failure", c1),
                              ("cif_retirement", c2), ("km_surv", skm)]:
                if abs(ar[key] - float(want)) > TOL:
                    problems.append(f"{tag} {key}: {ar[key]} != {float(want)}"
                                    f"（手算 {want}）")
            want_km_fail = 1 - skm
            if abs(ar["km_failure"] - float(want_km_fail)) > TOL:
                problems.append(f"{tag} km_failure: {ar['km_failure']} != {float(want_km_fail)}")

    # ---- 风险集成员
    for (group, t), want_members in exp["members"].items():
        ge = estimates.get(group)
        got = set(ge.risk_members.get(t, [])) if ge else set()
        if got != want_members:
            problems.append(f"[{group} t={t}] 风险集成员 {sorted(got)} != {sorted(want_members)}")

    return problems
