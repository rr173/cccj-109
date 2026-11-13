"""离线时钟对齐工具（``python3 -m alignment.cli``）。

把同一设备、同一启动周期内的本地时间坐标（``tele_samples.ts_ns``）通过
一份同步锚点表映射到共同参考钟；不修改接入器、不回写原始时间，也不推导
启机/故障结论。算法与误差语义见 ``docs/clock_alignment.md``。
"""

from __future__ import annotations

__version__ = "1.0.0"
