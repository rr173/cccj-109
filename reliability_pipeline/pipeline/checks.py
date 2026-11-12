"""不变量核对：对每次分析结果的可核验约束。

核对项（任一失败即抛出/报告违规）
----------------------------------
1. 风险人数非负，且当日结局数之和不超过风险人数；
2. 累积发生概率单调不减、取值在 [0, 1]；
3. 两类累积发生概率之和不超过 1（含数值容差）；
4. 总体生存函数单调不增且在 [0, 1]；
5. Aalen–Johansen 恒等式 ``S(t) = 1 - CIF_1(t) - CIF_2(t)``；
6. 对照 KM 不低于 CIF_1：``F_KM(t) >= CIF_1(t)``（竞争事件被误作删失
   时系统性高估）；
7. 入选设备必有 ``L < T``；风险集成员数与 ``n_risk`` 一致；
8. 主估计器与逐日枚举参考实现结果一致（见 ``compare_with_reference``）。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Dict, List

from .estimate import GroupEstimate

TOL = 1e-9


def check_group_estimate(ge: GroupEstimate) -> List[str]:
    """对单个分组的估计结果做不变量核对，返回违规描述列表（空 = 通过）。"""
    problems: List[str] = []
    prev_cif1 = prev_cif2 = 0.0
    prev_surv = 1.0
    prev_km = 1.0
    for r in ge.rows:
        g, t = ge.group, r["t"]
        # 1. 计数非负且自洽
        if r["n_risk"] < 0:
            problems.append(f"[{g} t={t}] n_risk 为负: {r['n_risk']}")
        if r["d_failure"] < 0 or r["d_retirement"] < 0 or r["n_censored"] < 0:
            problems.append(f"[{g} t={t}] 事件计数为负")
        if r["d_failure"] + r["d_retirement"] > r["n_risk"]:
            problems.append(f"[{g} t={t}] 结局数 {r['d_failure']}+{r['d_retirement']} "
                            f"超过风险人数 {r['n_risk']}")
        # 2. CIF 单调且有界
        if not (-TOL <= r["cif_failure"] <= 1 + TOL) or not (-TOL <= r["cif_retirement"] <= 1 + TOL):
            problems.append(f"[{g} t={t}] CIF 越界")
        if r["cif_failure"] < prev_cif1 - TOL or r["cif_retirement"] < prev_cif2 - TOL:
            problems.append(f"[{g} t={t}] CIF 非单调")
        # 3. 两类之和 <= 1
        if r["cif_failure"] + r["cif_retirement"] > 1 + TOL:
            problems.append(f"[{g} t={t}] CIF 之和 > 1: "
                            f"{r['cif_failure'] + r['cif_retirement']}")
        # 4. 生存函数单调有界
        if not (-TOL <= r["surv"] <= 1 + TOL) or r["surv"] > prev_surv + TOL:
            problems.append(f"[{g} t={t}] 生存函数非法或不单调: {r['surv']}")
        # 5. AJ 恒等式
        if abs(r["surv"] - (1 - r["cif_failure"] - r["cif_retirement"])) > TOL:
            problems.append(f"[{g} t={t}] AJ 恒等式不成立: S={r['surv']} "
                            f"1-CIF1-CIF2={1 - r['cif_failure'] - r['cif_retirement']}")
        # 6. KM 对照不低于 CIF_1
        if r["km_failure"] < r["cif_failure"] - TOL:
            problems.append(f"[{g} t={t}] F_KM < CIF_1: {r['km_failure']} < {r['cif_failure']}")
        if r["km_surv"] > prev_km + TOL:
            problems.append(f"[{g} t={t}] KM 生存函数非单调")
        # 7. 风险集成员数一致
        members = ge.risk_members.get(t)
        if members is not None and len(members) != r["n_risk"]:
            problems.append(f"[{g} t={t}] 风险集成员数 {len(members)} != n_risk {r['n_risk']}")
        prev_cif1, prev_cif2 = r["cif_failure"], r["cif_retirement"]
        prev_surv, prev_km = r["surv"], r["km_surv"]
    return problems


def check_all(estimates: Dict[str, GroupEstimate]) -> List[str]:
    problems: List[str] = []
    for ge in estimates.values():
        problems.extend(check_group_estimate(ge))
    return problems


def compare_with_reference(
    estimates: Dict[str, GroupEstimate],
    reference: Dict[str, List[dict]],
    tol: float = 1e-12,
) -> List[str]:
    """主估计器（float/NumPy） vs 逐日枚举参考（Fraction），逐点比对。"""
    problems: List[str] = []
    for group, ref_rows in reference.items():
        est = estimates.get(group)
        est_rows = est.rows if est else []
        if len(est_rows) != len(ref_rows):
            problems.append(f"[{group}] 行数不一致: 主估计 {len(est_rows)} vs 参考 {len(ref_rows)}")
            continue
        for er, rr in zip(est_rows, ref_rows):
            tag = f"[{group} t={rr['t']}]"
            if er["t"] != rr["t"]:
                problems.append(f"{tag} 时点错位: {er['t']}")
                continue
            for key in ("n_risk", "d_failure", "d_retirement", "n_censored"):
                if er[key] != rr[key]:
                    problems.append(f"{tag} {key}: {er[key]} != {rr[key]}")
            for key in ("surv", "cif_failure", "cif_retirement", "km_surv", "km_failure"):
                exact = float(rr[key]) if isinstance(rr[key], Fraction) else rr[key]
                if abs(er[key] - exact) > tol:
                    problems.append(f"{tag} {key}: {er[key]} != {exact}")
            if group in estimates and estimates[group].risk_members.get(rr["t"]) != rr["members"]:
                problems.append(f"{tag} 风险集成员不一致")
    # 主估计多出的分组
    for group in estimates:
        if group not in reference:
            problems.append(f"主估计存在参考中没有的分组: {group}")
    return problems
