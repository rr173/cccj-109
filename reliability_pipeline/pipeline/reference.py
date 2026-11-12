"""逐日枚举参考实现（独立核对用）。

与 ``estimate.py`` 刻意保持不同的实现路线：

- 不按结局时点跳跃，而是**逐日**从 1 枚举到最大退出日；
- 不使用 NumPy，全部用 ``fractions.Fraction`` 精确有理数运算；
- 风险集通过对每台设备逐一判定 ``L < t <= T`` 得到。

无事件的日子对递推是恒等操作，因此数学上与主估计器等价；
两者实现独立，用于随机输入下的交叉核对。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Dict, List

from .estimate import CENSOR_STATUSES, OUTCOME_STATUSES
from .model import GROUP_ALL, STATUS_FAILURE, STATUS_RETIREMENT, DeviceInterval


def reference_estimate(intervals: List[DeviceInterval]) -> Dict[str, List[dict]]:
    """对每个分组（机型 + ALL）逐日枚举，返回逐结局时点的精确估计。"""
    groups: Dict[str, List[DeviceInterval]] = {GROUP_ALL: list(intervals)}
    for iv in intervals:
        groups.setdefault(iv.machine_type, []).append(iv)

    out: Dict[str, List[dict]] = {}
    for group in sorted(groups):
        ivs = groups[group]
        max_t = max((iv.T for iv in ivs), default=0)
        surv = Fraction(1)      # S(t)
        cif1 = Fraction(0)      # 故障 CIF
        cif2 = Fraction(0)      # 退役 CIF
        km_surv = Fraction(1)   # 对照 KM 生存
        rows: List[dict] = []
        for t in range(1, max_t + 1):
            at_risk = [iv for iv in ivs if iv.L < t <= iv.T]
            n = len(at_risk)
            d1 = sum(1 for iv in at_risk if iv.T == t and iv.status == STATUS_FAILURE)
            d2 = sum(1 for iv in at_risk if iv.T == t and iv.status == STATUS_RETIREMENT)
            c = sum(1 for iv in at_risk if iv.T == t and iv.status in CENSOR_STATUSES)
            if d1 + d2 == 0:
                continue  # 非结局日：递推恒等
            surv_prev = surv
            surv *= 1 - Fraction(d1 + d2, n)
            cif1 += surv_prev * Fraction(d1, n)
            cif2 += surv_prev * Fraction(d2, n)
            km_surv *= 1 - Fraction(d1, n)
            rows.append({
                "group": group,
                "t": t,
                "n_risk": n,
                "d_failure": d1,
                "d_retirement": d2,
                "n_censored": c,
                "surv": surv,
                "cif_failure": cif1,
                "cif_retirement": cif2,
                "km_surv": km_surv,
                "km_failure": 1 - km_surv,
                "members": sorted(iv.device_id for iv in at_risk),
            })
        out[group] = rows
    return out
