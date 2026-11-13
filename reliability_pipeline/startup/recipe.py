"""启机配方：JSON 装载与校验。

配方是**一份固定的启机过程定义**，不是通用规则语言。示例：

.. code-block:: json

    {
      "name": "standard-start",
      "channels": {
        "pressure":    {"channel": 1, "unit": "kPa", "max_age_ns": 1000000000},
        "vibration":   {"channel": 2, "unit": "m/s", "max_age_ns": 1000000000},
        "temperature": {"channel": 0, "unit": "°C",  "max_age_ns": 2000000000}
      },
      "thresholds": {"P_start": 100.0, "P_run": 90.0, "V_run": 5.0, "T_max": 80.0},
      "durations":  {"C": 2000000000, "H": 3000000000, "W": 10000000000}
    }

校验规则（任何一条不满足都在**计算之前**报错，``RecipeError``）：

- 三个角色 pressure / vibration / temperature 必须齐全，通道号互不重复；
- 通道号为 0..255 的整数（协议中通道是 u8）；单位为非空字符串；
- 每个通道 ``max_age_ns`` 为正整数（零阶保持的有效期）；
- 阈值 P_start / P_run / V_run / T_max 必须为有限数（拒绝 NaN/Inf）；
- 时长 C / H / W 为正整数纳秒，且 C<=W、H<=W；
- 不认识的键一律拒绝（防止拼写错误被静默忽略）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

ROLES = ("pressure", "vibration", "temperature")
THRESHOLDS = ("P_start", "P_run", "V_run", "T_max")
DURATIONS = ("C", "H", "W")
_CHANNEL_KEYS = {"channel", "unit", "max_age_ns"}
_TOP_KEYS = {"name", "channels", "thresholds", "durations"}


class RecipeError(ValueError):
    """配方非法：在计算之前报错。"""


@dataclass(frozen=True)
class ChannelRecipe:
    """一个角色通道的配方项。"""

    role: str
    channel: int
    unit: str
    max_age_ns: int


@dataclass(frozen=True)
class Recipe:
    """校验通过的启机配方（不可变）。"""

    name: str
    channels: Dict[str, ChannelRecipe]          # role -> ChannelRecipe
    P_start: float
    P_run: float
    V_run: float
    T_max: float
    C: int          # 确认等待（ns）
    H: int          # 连续稳定时长（ns）
    W: int          # 尝试总期限（ns）

    def role_of_channel(self, channel: int) -> str | None:
        for role, ch in self.channels.items():
            if ch.channel == channel:
                return role
        return None


def _err(msg: str) -> RecipeError:
    return RecipeError(f"启机配方非法：{msg}")


def _check_int(value: Any, what: str, lo: int | None = None,
               hi: int | None = None) -> int:
    # bool 是 int 的子类，明确拒绝
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err(f"{what} 必须为整数，实际 {value!r}")
    if lo is not None and value < lo:
        raise _err(f"{what} 必须 >= {lo}，实际 {value}")
    if hi is not None and value > hi:
        raise _err(f"{what} 必须 <= {hi}，实际 {value}")
    return value


def _check_threshold(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _err(f"阈值 {what} 必须为数值，实际 {value!r}")
    v = float(value)
    if not math.isfinite(v):
        raise _err(f"阈值 {what} 必须为有限值，实际 {value!r}")
    return v


def load_recipe(path: str | Path) -> Recipe:
    """从 JSON 文件装载并校验配方；非法时抛 ``RecipeError``。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise _err(f"无法读取配方文件 {path}：{e}") from e
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise _err(f"配方不是合法 JSON：{e}") from e
    return parse_recipe(obj)


def parse_recipe(obj: Any) -> Recipe:
    """从已解析的 JSON 对象构造配方（校验全部约束）。"""
    if not isinstance(obj, dict):
        raise _err("顶层必须是对象")
    unknown = set(obj) - _TOP_KEYS
    if unknown:
        raise _err(f"不认识的顶层键：{sorted(unknown)}")
    for key in ("channels", "thresholds", "durations"):
        if key not in obj:
            raise _err(f"缺少顶层键 {key!r}")
    name = obj.get("name", "unnamed")
    if not isinstance(name, str):
        raise _err(f"name 必须为字符串，实际 {name!r}")

    # ---- 通道 ----------------------------------------------------------
    ch_obj = obj["channels"]
    if not isinstance(ch_obj, dict):
        raise _err("channels 必须是对象")
    unknown = set(ch_obj) - set(ROLES)
    if unknown:
        raise _err(f"channels 中不认识的角色：{sorted(unknown)}")
    channels: Dict[str, ChannelRecipe] = {}
    seen_channels: Dict[int, str] = {}
    for role in ROLES:
        if role not in ch_obj:
            raise _err(f"channels 缺少角色 {role!r}")
        item = ch_obj[role]
        if not isinstance(item, dict):
            raise _err(f"channels.{role} 必须是对象")
        unknown = set(item) - _CHANNEL_KEYS
        if unknown:
            raise _err(f"channels.{role} 有不认识的键：{sorted(unknown)}")
        for key in _CHANNEL_KEYS:
            if key not in item:
                raise _err(f"channels.{role} 缺少键 {key!r}")
        channel = _check_int(item["channel"], f"channels.{role}.channel", 0, 255)
        unit = item["unit"]
        if not isinstance(unit, str) or not unit:
            raise _err(f"channels.{role}.unit 必须为非空字符串，实际 {unit!r}")
        max_age = _check_int(item["max_age_ns"], f"channels.{role}.max_age_ns", 1)
        if channel in seen_channels:
            raise _err(f"通道号 {channel} 被 {seen_channels[channel]!r} 与 "
                       f"{role!r} 重复使用，三个角色通道必须互不相同")
        seen_channels[channel] = role
        channels[role] = ChannelRecipe(role, channel, unit, max_age)

    # ---- 阈值 ----------------------------------------------------------
    th_obj = obj["thresholds"]
    if not isinstance(th_obj, dict):
        raise _err("thresholds 必须是对象")
    unknown = set(th_obj) - set(THRESHOLDS)
    if unknown:
        raise _err(f"thresholds 有不认识的键：{sorted(unknown)}")
    thresholds = {}
    for key in THRESHOLDS:
        if key not in th_obj:
            raise _err(f"thresholds 缺少 {key!r}")
        thresholds[key] = _check_threshold(th_obj[key], key)

    # ---- 时长 ----------------------------------------------------------
    du_obj = obj["durations"]
    if not isinstance(du_obj, dict):
        raise _err("durations 必须是对象")
    unknown = set(du_obj) - set(DURATIONS)
    if unknown:
        raise _err(f"durations 有不认识的键：{sorted(unknown)}")
    durations = {}
    for key in DURATIONS:
        if key not in du_obj:
            raise _err(f"durations 缺少 {key!r}")
        durations[key] = _check_int(du_obj[key], f"durations.{key}", 1)
    C, H, W = durations["C"], durations["H"], durations["W"]
    if C > W:
        raise _err(f"确认等待 C={C} 不能超过总期限 W={W}（要求 C<=W）")
    if H > W:
        raise _err(f"连续稳定时长 H={H} 不能超过总期限 W={W}（要求 H<=W）")

    return Recipe(name=name, channels=channels,
                  P_start=thresholds["P_start"], P_run=thresholds["P_run"],
                  V_run=thresholds["V_run"], T_max=thresholds["T_max"],
                  C=C, H=H, W=W)
