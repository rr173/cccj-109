"""手算金标准小队列。

设计覆盖（观察截止 2024-01-31；时间单位：自投产日起整日数）：

=========  ==========  ==================================================
设备       机型        覆盖情形
=========  ==========  ==================================================
D01        A           常规故障（t=9）
D02        A           延迟入组（L=4，左截断）+ 与 D01 同日退役（t=9）
D03        A           失访删失（t=7）
D04        A           纯删失（观察截止）
D05        B           故障（t=19），其撤销在截点 1 后才收到 → 两次运行结果不同
D06        B           入组前已终止 → 设备拒收
D07        B           观察期末恰好故障（t=30 = obs_end）
D08        B           故障记录日期非法（2024-13-40）→ 记录拒收，设备转纯删失
D09        B           故障记录早于投产日 → 记录拒收，设备转纯删失
D10        B           循环撤销（E10A↔E10B）→ 两条记录拒收，设备转纯删失
D11        B           入组日=终止日=观察期末（L=T=30）→ 无法贡献观察时长
D12        A           同一设备同日故障+退役 → 固定优先级取故障（t=14）
D13        C           纯删失组（C 型无任何结局时点）
=========  ==========  ==================================================

期望值全部由手工推导（见 README「金标准手算」一节），以 ``Fraction``
精确表示；``verify-gold`` 逐点比对管线输出。
"""

from __future__ import annotations

import csv
from fractions import Fraction
from pathlib import Path

F = Fraction

GOLD_OBSERVATION_END = "2024-01-31"
GOLD_CUTOFF_RUN1 = "2024-02-15"   # V05（撤销）尚未收到
GOLD_CUTOFF_RUN2 = "2024-02-28"    # V05 已收到，D05 故障被撤销

DEVICES_CSV = """device_id,machine_type,commission_date,enroll_date
D01,A,2024-01-01,2024-01-01
D02,A,2024-01-01,2024-01-05
D03,A,2024-01-01,2024-01-01
D04,A,2024-01-01,2024-01-01
D05,B,2024-01-01,2024-01-01
D06,B,2024-01-01,2024-01-10
D07,B,2024-01-01,2024-01-01
D08,B,2024-01-01,2024-01-01
D09,B,2024-01-01,2024-01-01
D10,B,2024-01-01,2024-01-01
D11,B,2024-01-01,2024-01-31
D12,A,2024-01-01,2024-01-01
D13,C,2024-01-01,2024-01-01
"""

EVENTS_CSV = """event_id,device_id,event_type,event_date,received_at,voids_event_id,note
E01,D01,failure,2024-01-10,2024-01-12,,常规故障
E02,D02,retirement,2024-01-10,2024-01-11,,与E01同日退役
E03,D03,loss_to_followup,2024-01-08,2024-01-09,,失访
E05,D05,failure,2024-01-20,2024-01-22,,将被迟到的撤销作废
V05,D05,void,2024-02-20,2024-02-20,E05,截点1后才收到的撤销
E06,D06,failure,2024-01-05,2024-01-06,,入组前已终止
E07,D07,failure,2024-01-31,2024-02-01,,观察期末恰好故障
E08,D08,failure,2024-13-40,2024-01-15,,非法日期
E09,D09,failure,2023-12-15,2024-01-05,,早于投产日
E10A,D10,void,2024-01-15,2024-01-16,E10B,循环撤销A
E10B,D10,void,2024-01-15,2024-01-16,E10A,循环撤销B
E11,D11,failure,2024-01-31,2024-02-01,,入组日即终止日
E12A,D12,failure,2024-01-15,2024-01-17,,同日多事件-故障
E12B,D12,retirement,2024-01-15,2024-01-17,,同日多事件-退役
"""

# ---- 设备时间段期望：(cohort_status, L, T, status, terminal_event_id) --------
_RUN1_DEVICES = {
    "D01": ("included", 0, 9, "failure", "E01"),
    "D02": ("included", 4, 9, "retirement", "E02"),
    "D03": ("included", 0, 7, "censored_loss", "E03"),
    "D04": ("included", 0, 30, "censored_end", None),
    "D05": ("included", 0, 19, "failure", "E05"),
    "D06": ("rejected", None, None, None, None),
    "D07": ("included", 0, 30, "failure", "E07"),
    "D08": ("included", 0, 30, "censored_end", None),
    "D09": ("included", 0, 30, "censored_end", None),
    "D10": ("included", 0, 30, "censored_end", None),
    "D11": ("excluded_no_time", 30, 30, "failure", "E11"),
    "D12": ("included", 0, 14, "failure", "E12A"),
    "D13": ("included", 0, 30, "censored_end", None),
}
_RUN2_DEVICES = dict(_RUN1_DEVICES, D05=("included", 0, 30, "censored_end", None))

# ---- 拒收明细期望：(level, entity_id, reason) --------------------------------
_RUN1_REJECTIONS = {
    ("device", "D06", "terminated_before_enroll"),
    ("event", "E08", "invalid_event_date"),
    ("event", "E09", "event_before_commission"),
    ("event", "E10A", "void_cycle"),
    ("event", "E10B", "void_cycle"),
}
_RUN2_REJECTIONS = set(_RUN1_REJECTIONS)

# ---- 分组曲线期望：(t, n_risk, d_fail, d_retire, n_cens, S, CIF1, CIF2, S_KM) -
_RUN1_GROUPS = {
    "A": [
        (9, 4, 1, 1, 0, F(1, 2), F(1, 4), F(1, 4), F(3, 4)),
        (14, 2, 1, 0, 0, F(1, 4), F(1, 2), F(1, 4), F(3, 8)),
    ],
    "B": [
        (19, 5, 1, 0, 0, F(4, 5), F(1, 5), F(0), F(4, 5)),
        (30, 4, 1, 0, 3, F(3, 5), F(2, 5), F(0), F(3, 5)),
    ],
    "C": [],
    "ALL": [
        (9, 10, 1, 1, 0, F(4, 5), F(1, 10), F(1, 10), F(9, 10)),
        (14, 8, 1, 0, 0, F(7, 10), F(1, 5), F(1, 10), F(63, 80)),
        (19, 7, 1, 0, 0, F(3, 5), F(3, 10), F(1, 10), F(27, 40)),
        (30, 6, 1, 0, 5, F(1, 2), F(2, 5), F(1, 10), F(9, 16)),
    ],
}
_RUN2_GROUPS = {
    "A": _RUN1_GROUPS["A"],
    "B": [
        (30, 5, 1, 0, 4, F(4, 5), F(1, 5), F(0), F(4, 5)),
    ],
    "C": [],
    "ALL": [
        (9, 10, 1, 1, 0, F(4, 5), F(1, 10), F(1, 10), F(9, 10)),
        (14, 8, 1, 0, 0, F(7, 10), F(1, 5), F(1, 10), F(63, 80)),
        (30, 7, 1, 0, 6, F(3, 5), F(3, 10), F(1, 10), F(27, 40)),
    ],
}

# ---- 风险集成员期望 -----------------------------------------------------------
_RUN1_MEMBERS = {
    ("A", 9): {"D01", "D02", "D04", "D12"},
    ("A", 14): {"D04", "D12"},
    ("B", 19): {"D05", "D07", "D08", "D09", "D10"},
    ("B", 30): {"D07", "D08", "D09", "D10"},
    ("ALL", 9): {"D01", "D02", "D04", "D05", "D07", "D08", "D09", "D10", "D12", "D13"},
    ("ALL", 14): {"D04", "D05", "D07", "D08", "D09", "D10", "D12", "D13"},
    ("ALL", 19): {"D04", "D05", "D07", "D08", "D09", "D10", "D13"},
    ("ALL", 30): {"D04", "D07", "D08", "D09", "D10", "D13"},
}
_RUN2_MEMBERS = {
    ("A", 9): {"D01", "D02", "D04", "D12"},
    ("A", 14): {"D04", "D12"},
    ("B", 30): {"D05", "D07", "D08", "D09", "D10"},
    ("ALL", 9): {"D01", "D02", "D04", "D05", "D07", "D08", "D09", "D10", "D12", "D13"},
    ("ALL", 14): {"D04", "D05", "D07", "D08", "D09", "D10", "D12", "D13"},
    ("ALL", 30): {"D04", "D05", "D07", "D08", "D09", "D10", "D13"},
}

EXPECTED = {
    "gold_run1": {
        "knowledge_cutoff": GOLD_CUTOFF_RUN1,
        "observation_end": GOLD_OBSERVATION_END,
        "devices": _RUN1_DEVICES,
        "rejections": _RUN1_REJECTIONS,
        "groups": _RUN1_GROUPS,
        "members": _RUN1_MEMBERS,
    },
    "gold_run2": {
        "knowledge_cutoff": GOLD_CUTOFF_RUN2,
        "observation_end": GOLD_OBSERVATION_END,
        "devices": _RUN2_DEVICES,
        "rejections": _RUN2_REJECTIONS,
        "groups": _RUN2_GROUPS,
        "members": _RUN2_MEMBERS,
    },
}


def write_gold_csvs(out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "devices.csv").write_text(DEVICES_CSV, encoding="utf-8")
    (out / "events.csv").write_text(EVENTS_CSV, encoding="utf-8")
    return out
