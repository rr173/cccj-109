"""数据模型与全局政策常量。

时间约定
--------
- 所有日期为 ``datetime.date``；时长 = 自投产日起经过的**整日**数：
  ``t = (date - commission_date).days``。
- 设备进入日 ``L = (enroll_date - commission_date).days``，
  退出日 ``T = (terminal_date - commission_date).days``。
- 结局发生日 ``t`` 的风险集定义为 ``{i : L_i < t <= T_i}``（左截断：
  入组日当天尚不进入风险集，从 ``L+1`` 日起在险）。
- 因此只有 ``L < T`` 的设备才能贡献任何观察时长；``L >= T`` 的设备
  记入队列清单但标记 ``excluded_no_time``，不参与估计。

同日多事件的固定优先级（同一设备、同一日期、多个有效终止事件）
---------------------------------------------------------------
``failure``(0) > ``retirement``(1) > ``loss_to_followup``(2)，
再按 ``event_id`` 字典序决胜，保证完全确定性。
理由：故障是实际观察到的最严重结局；退役是已发生的行政事实；
失访只是信息缺失，证据强度最弱。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

# ---- 事件类型 -------------------------------------------------------------
EVENT_FAILURE = "failure"               # 故障（关注结局）
EVENT_RETIREMENT = "retirement"         # 预防性退役（竞争结局）
EVENT_LOSS = "loss_to_followup"         # 失访（删失）
EVENT_VOID = "void"                     # 作废（引用旧 event_id，禁止覆写）

TERMINAL_TYPES = (EVENT_FAILURE, EVENT_RETIREMENT, EVENT_LOSS)
ALL_EVENT_TYPES = TERMINAL_TYPES + (EVENT_VOID,)

# 同日优先级：数值小者优先
EVENT_PRIORITY = {EVENT_FAILURE: 0, EVENT_RETIREMENT: 1, EVENT_LOSS: 2}

# ---- 设备终止状态 -----------------------------------------------------------
STATUS_FAILURE = "failure"
STATUS_RETIREMENT = "retirement"
STATUS_CENSORED_LOSS = "censored_loss"      # 失访删失
STATUS_CENSORED_END = "censored_end"        # 观察截止删失

# ---- 队列状态 ---------------------------------------------------------------
COHORT_INCLUDED = "included"                # 参与估计
COHORT_EXCLUDED_NO_TIME = "excluded_no_time"  # L >= T，无法贡献观察时长
COHORT_REJECTED = "rejected"                # 拒收

# 分组标签：全部队列
GROUP_ALL = "ALL"


@dataclass(frozen=True)
class Device:
    """设备基础资料（投产日、入组日、机型）。"""

    device_id: str
    machine_type: str
    commission_date: Optional[date]   # None 表示原始日期非法
    enroll_date: Optional[date]
    commission_raw: str = ""
    enroll_raw: str = ""


@dataclass(frozen=True)
class RawEvent:
    """原始事件记录（追加式日志中的一行，禁止覆写）。"""

    event_id: str
    device_id: str
    event_type: str
    event_date: Optional[date]        # None 表示原始日期非法
    received_at: Optional[date]       # 收到日期（知识截点据此判定可见性）
    voids_event_id: Optional[str]     # 作废目标 event_id（仅 void 类型）
    event_date_raw: str = ""
    received_at_raw: str = ""
    note: str = ""
    line_no: int = 0                  # 原始文件行号，用于确定性决胜


@dataclass(frozen=True)
class DeviceInterval:
    """推导出的设备时间段（估计的输入）。"""

    device_id: str
    machine_type: str
    L: int                            # 进入日（入组日 - 投产日，整日）
    T: int                            # 退出日
    status: str                       # STATUS_*
    terminal_event_id: Optional[str]  # 决定结局的原始事件（可追溯）
