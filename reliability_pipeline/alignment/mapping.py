"""分段仿射时钟映射：整数/有理数中心值与误差区间。

数学约定（全部在本地坐标 ``x`` 上定义，参考坐标 ``y``）：

- 锚点组折叠后为 ``(x_i, y_i, e_i)``，x、y 严格递增，``e_i >= 0``；
- 相邻锚点段 ``[x0, x1]`` 上，``a = (x - x0) / (x1 - x0)``（Fraction），
  中心 ``y = (1-a)·y0 + a·y1``，内插误差半径 ``u = (1-a)·e0 + a·e1``；
- 落在最左/最右锚点之外时只用最近一段，``d`` 为到最近端点的距离，
  要求 ``d <= max_extrapolation_ns``，且
  ``u = |1-a|·e0 + |a|·e1 + d·max_ppm/10^6``；
- 锚点本身取自身中心与误差（``u = e``），不做内插；
- 斜率检验：相邻段 ``(y1-y0)/(x1-x0)`` 与 1 的偏差若超过
  ``max_ppm/10^6``，该组整体无效（不跳过坏段）。

结果只承诺上述分段模型给出的范围，不宣称覆盖任意未建模的设备跳钟。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import List, Tuple

from .anchors import Anchor, Group

PPM_DENOM = 1_000_000

MODE_INTERP = "interp"
MODE_EXTRAP_LEFT = "extrapolate_left"
MODE_EXTRAP_RIGHT = "extrapolate_right"


class AlignmentConfigError(Exception):
    pass


@dataclass(frozen=True)
class MappingResult:
    """一个本地时刻的映射结果（未舍入的有理数）。"""

    center: Fraction
    radius: Fraction
    mode: str
    segment: Tuple[int, int]          # 段端点下标
    distance_beyond_ns: int           # 外推时到最近端点的距离；内插为 0

    @property
    def lower(self) -> Fraction:
        return self.center - self.radius

    @property
    def upper(self) -> Fraction:
        return self.center + self.radius

    def rounded(self) -> Tuple[int, int, int]:
        """(floor(center), floor(center-radius), ceil(center+radius))。"""
        return (math.floor(self.center), math.floor(self.lower),
                math.ceil(self.upper))


class Mapping:
    """一个有效锚点组的分段映射；构造前组长必须已通过校验。"""

    def __init__(self, group: Group, max_ppm: int):
        if not group.valid:
            raise ValueError(f"锚点组无效：{group.invalid_reason}")
        if max_ppm < 0:
            raise AlignmentConfigError("max_ppm 必须为非负整数")
        self.device_id = group.device_id
        self.boot_seq = group.boot_seq
        self.anchors: List[Anchor] = list(group.anchors)
        self.max_ppm = max_ppm
        self.x = [a.local_ns for a in self.anchors]
        self.y = [a.reference_ns for a in self.anchors]
        self.e = [a.error_ns for a in self.anchors]

    @property
    def segment_count(self) -> int:
        return len(self.anchors) - 1

    def _segment_for(self, x: int) -> Tuple[int, int]:
        """返回 (左端点下标, 右端点下标)；x 超界时用最近段。"""
        n = len(self.anchors)
        if x <= self.x[0]:
            return 0, 1
        if x >= self.x[-1]:
            return n - 2, n - 1
        i = bisect.bisect_right(self.x, x) - 1
        if i >= n - 1:
            i = n - 2
        return i, i + 1

    def evaluate(self, x: int, max_extrapolation_ns: int) -> MappingResult:
        """映射一个本地时刻；越界超出额度时抛 ``OutOfRange``。"""
        if max_extrapolation_ns < 0:
            raise AlignmentConfigError("max_extrapolation_ns 必须为非负整数")
        i0, i1 = self._segment_for(x)
        # 恰好落在锚点上（含两端）：采用锚点自身中心/误差，不做内插/外推；
        # 所用"锚点"只列该锚点自己的全部 anchor_id。
        hit = (i1 if x == self.x[i1] else
               (i0 if x == self.x[i0] else None))
        if hit is not None:
            return MappingResult(Fraction(self.y[hit]),
                                 Fraction(self.e[hit]),
                                 MODE_INTERP, (hit, hit), 0)
        x0, x1 = self.x[i0], self.x[i1]
        y0, y1 = self.y[i0], self.y[i1]
        e0, e1 = self.e[i0], self.e[i1]
        a = Fraction(x - x0, x1 - x0)
        if x < self.x[0]:
            d = self.x[0] - x
            mode = MODE_EXTRAP_LEFT
        elif x > self.x[-1]:
            d = x - self.x[-1]
            mode = MODE_EXTRAP_RIGHT
        else:
            d = 0
            mode = MODE_INTERP
        if d > max_extrapolation_ns:
            raise OutOfRange(
                f"本地时刻 {x} 距最近锚点端点 {d} ns，超过外推额度 "
                f"{max_extrapolation_ns} ns")
        center = (1 - a) * y0 + a * y1
        if d == 0:
            radius = (1 - a) * e0 + a * e1
        else:
            radius = (abs(1 - a) * e0 + abs(a) * e1
                      + Fraction(d * self.max_ppm, PPM_DENOM))
        return MappingResult(center, radius, mode, (i0, i1), d)

    def anchor_ids_for_segment(self, segment: Tuple[int, int]) -> List[str]:
        """该结果用到的锚点 id 列表；落在锚点上时只列该锚点（去重）。"""
        indices = sorted(set(segment))
        return [aid for idx in indices for aid in self.anchors[idx].anchor_ids]


class OutOfRange(Exception):
    """本地时刻超出允许的外推额度。"""


def finalize_groups(groups, max_ppm: int) -> None:
    """在所有结构校验通过后执行斜率检验；无效组写 ``invalid_reason``。

    ``max_ppm`` 以整数 ppm 表示：斜率的分子分母都是整数，逐段用精确
    ``Fraction`` 比较 ``|(y1-y0) - (x1-x0)| / (x1-x0)`` 与 ``max_ppm/10^6``。
    """
    if max_ppm < 0:
        raise AlignmentConfigError("max_ppm 必须为非负整数")
    for g in groups.values():
        if not g.valid:
            continue
        for a, b in zip(g.anchors, g.anchors[1:]):
            dx = b.local_ns - a.local_ns
            dy = b.reference_ns - a.reference_ns
            # 斜率偏离 1：|dy/dx - 1| = |dy - dx| / dx（dx > 0 已校验）
            deviation = Fraction(abs(dy - dx), dx)
            dev_ppm = deviation * PPM_DENOM
            if dev_ppm > max_ppm:
                g.invalid_reason = (
                    f"斜率偏离 1 超过 max_ppm：段 {a.primary_id}→{b.primary_id} "
                    f"偏差 {dev_ppm} ppm > 上限 {max_ppm} ppm（整组无效，"
                    f"不跳过坏段）")
                break
