"""实验分配（整簇随机化）子系统（``python3 -m allocation.cli``）。

在「共用维护班组的设备必须整体接受同一方案」的约束下，为分层随机对照试验
产生**精确**的实验名单：每个 cluster 整体进入 A 或 B，每个层的 A 台数恰好
等于设计文件要求的 ``target_A``。

本包只做随机化与审计，不更改观察日志、不推导故障结局、不计算试验效果，
也不触碰 ``pipeline`` / ``telemetry`` / ``startup`` / ``alignment`` 的任何
表或文件。算法与均匀性推导见 ``docs/allocation.md``。

模块
----
- ``inputs``   CSV 设备名单与 JSON 设计文件的严格读取（行号定位的拒绝）
- ``model``    规范化输入、层/组排序、锁定项合并与逐层计数
- ``count``    按 cluster 向量贡献的精确动态规划（任意精度整数）
- ``sampler``  前缀条件计数 → 无偏抽签；rank ↔ 唯一方案；逐步轨迹
- ``outputs``  全量校验后的原子写出（失败绝不覆盖既有文件）
- ``demo``     可复跑材料 + 独立枚举程序 + 全部核对
- ``cli``      命令行入口
"""

from __future__ import annotations

__version__ = "1.0.0"
