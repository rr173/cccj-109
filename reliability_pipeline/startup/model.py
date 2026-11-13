"""启机识别的数据类型：测量行、尝试结果、证据项。

结果三态：

- ``confirmed``      确认窗口内振动到位，随后压力/振动连续稳定 H，
                     且 [s, c] 全程温度已知且严格低于 T_max；
- ``rejected``       资料完整前提下的失败：存活期温度达到上限（立即）、
                     确认超时（窗口内振动始终已知但未到位）、
                     稳定超时（确认后直到期限也没有连续 H 的稳定段）；
- ``indeterminate``  证据不足，**不算设备启动失败**：完成点前安全温度有未知
                     区间、超时判定窗口内存在参与判断的未知状态、
                     或分析截止时观察尚未结束。

``indeterminate`` 与 ``rejected`` 严格区分：前者说明数据缺失或观察未完，
后者说明在资料完整的前提下过程确实未按配方完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ---- 状态与原因码 --------------------------------------------------------

STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUS_INDETERMINATE = "indeterminate"
STATUSES = (STATUS_CONFIRMED, STATUS_REJECTED, STATUS_INDETERMINATE)

# confirmed
REASON_STABLE_COMPLETED = "stable_completed"
# rejected
REASON_TEMPERATURE_EXCEEDED = "temperature_exceeded"
REASON_CONFIRM_TIMEOUT = "confirm_timeout"
REASON_STABLE_TIMEOUT = "stable_timeout"
# indeterminate
REASON_TEMP_UNKNOWN_BEFORE_COMPLETION = "temperature_unknown_before_completion"
REASON_CONFIRM_TIMEOUT_UNKNOWN = "confirm_timeout_unknown"
REASON_STABLE_TIMEOUT_UNKNOWN = "stable_timeout_unknown"
REASON_OBSERVATION_INCOMPLETE = "observation_incomplete"

REASONS = (
    REASON_STABLE_COMPLETED,
    REASON_TEMPERATURE_EXCEEDED,
    REASON_CONFIRM_TIMEOUT,
    REASON_STABLE_TIMEOUT,
    REASON_TEMP_UNKNOWN_BEFORE_COMPLETION,
    REASON_CONFIRM_TIMEOUT_UNKNOWN,
    REASON_STABLE_TIMEOUT_UNKNOWN,
    REASON_OBSERVATION_INCOMPLETE,
)

# ---- 证据种类 ------------------------------------------------------------

EV_CROSSING = "crossing"                    # 越线建候选（压力读数定位）
EV_CONFIRM = "confirm"                      # 确认（振动读数定位）
EV_CONFIRM_WINDOW_MISSED = "confirm_window_missed"   # 确认窗口错过（点）
EV_STABLE_BREAK = "stable_break"            # 连续稳定被打断（原因 + 定位）
EV_STABLE_COMPLETED = "stable_completed"    # 稳定段完成（区间 [c-H, c]）
EV_TEMP_UNKNOWN = "temperature_unknown"     # 温度未知区间（起因定位，可空）
EV_TEMP_EXCEEDED = "temperature_exceeded"   # 温度达到上限（读数定位）
EV_CONFLICT = "conflict"                    # 同刻冲突（双方定位）
EV_UNIT_MISMATCH = "unit_mismatch"          # 单位与配方不符（读数定位）
EV_UNKNOWN_PARTICIPATING = "unknown_participating"  # 参与超时判定的未知区间
EV_DEADLINE = "deadline"                    # 总期限到达（点）
EV_OBSERVATION_CUT = "observation_cut"      # 分析截止，观察未结束（点）


@dataclass(frozen=True)
class SampleRef:
    """一条源测量在库中的定位键（绝不伪造：只指向 tele_samples 真实行）。"""

    file_id: int
    frame_offset: int
    ordinal: int
    channel: int
    ts_ns: int
    value: float
    unit: str

    def key(self) -> Tuple[int, int, int]:
        return (self.file_id, self.frame_offset, self.ordinal)


@dataclass(frozen=True)
class SampleRow:
    """tele_samples 中一行（只取识别需要的列）。"""

    device_id: int
    boot_seq: int
    channel: int
    ts_ns: int
    value: float
    unit: str
    file_id: int
    frame_offset: int
    ordinal: int

    def ref(self) -> SampleRef:
        return SampleRef(self.file_id, self.frame_offset, self.ordinal,
                         self.channel, self.ts_ns, self.value, self.unit)


@dataclass
class EvidenceItem:
    """一条证据：时间片（点则 t_end=None）+ 说明 + 源测量定位键列表。"""

    kind: str
    t_start_ns: int
    t_end_ns: Optional[int] = None            # None = 时点；否则为半开区间 [start, end)
    role: Optional[str] = None                # pressure / vibration / temperature
    detail: str = ""
    refs: List[SampleRef] = field(default_factory=list)


@dataclass
class Attempt:
    """一次启机尝试（候选）的最终可复核结果。"""

    device_id: int
    boot_seq: int
    attempt_seq: int                          # 该 (设备, 周期) 内按 s 排序的序号
    s_ns: int                                 # 越线时刻
    confirm_ns: Optional[int] = None          # 确认时刻（未确认=None）
    stable_start_ns: Optional[int] = None     # 稳定段起点 c-H
    stable_end_ns: Optional[int] = None       # 稳定完成时刻 c
    end_ns: int = 0                           # 结果落定时刻
    status: str = ""
    reason: str = ""
    detail: str = ""
    evidence: List[EvidenceItem] = field(default_factory=list)

    def key(self) -> Tuple[int, int, int]:
        return (self.device_id, self.boot_seq, self.attempt_seq)

    def verdict_tuple(self) -> Tuple:
        """用于引擎/参考实现逐条比对的规范化元组。"""
        return (self.device_id, self.boot_seq, self.attempt_seq, self.s_ns,
                self.confirm_ns, self.stable_start_ns, self.stable_end_ns,
                self.end_ns, self.status, self.reason)
