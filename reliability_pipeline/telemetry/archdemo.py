"""折线压缩归档演示材料：手拼帧流 → 接入 → 归档 → 核验。

材料覆盖（均经真实 ``telemetry`` 接入链路入库，再从库中归档）：

- ``arch_ramp``   不等间隔采样的精确直线（长直线应压成一段）
- ``arch_spike``  平直信号中央一个单点尖峰（尖峰不得被抹掉）
- ``arch_saw``    锯齿（振幅大于容差，逐点核对）
- ``arch_hugens`` 巨大 Unix 纳秒基数（1.7e18）下 1 纳秒等间隔直线
- ``arch_dup1/2`` 跨文件冲突重复时刻（tick→ns 向下取整撞车）+ 相容重复
- ``arch_multi``  同一设备跨启动周期、跨布局、单位变化（必须分组）

本模块供演示使用；测试另行手工拼帧，不经过这里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .generator import StreamBuilder

EPOCH = 1_700_000_000_000_000_000          # 2023-11-14T22:13:20Z（> 2^53）

# 归档选择范围（覆盖全部材料的时刻）
T_START = EPOCH - 1_000_000_000
T_END = EPOCH + 2_000_000_000_000


@dataclass(frozen=True)
class MaterialSpec:
    name: str            # 归档文件基名
    files: tuple         # 需要接入的源文件
    device: int
    channel: int
    epsilon: float
    block_points: int
    note: str


SPECS: List[MaterialSpec] = [
    MaterialSpec("ramp", ("arch_ramp.bin",), 40, 0, 0.05, 32,
                 "不等间隔精确直线：应压成 1 段，参数区远小于原值"),
    MaterialSpec("spike", ("arch_spike.bin",), 42, 0, 0.05, 128,
                 "单点尖峰：尖峰点解码误差同样 ≤ eps，不被抹掉"),
    MaterialSpec("saw", ("arch_saw.bin",), 43, 0, 0.01, 64,
                 "锯齿：振幅大于容差，逐点核对容差"),
    MaterialSpec("hugens", ("arch_hugens.bin",), 44, 0, 0.05, 128,
                 "1.7e18 纳秒基数下 1ns 间隔：时刻精确、仍压成 1 段"),
    MaterialSpec("dup", ("arch_dup1.bin", "arch_dup2.bin"), 45, 0, 0.005, 64,
                 "冲突重复时刻必须切段；相容重复可同段"),
    MaterialSpec("multi", ("arch_multi.bin",), 46, 0, 0.01, 64,
                 "跨启动周期/布局/单位变化：拆成 3 组，互不共段"),
]


def build_materials() -> Dict[str, bytes]:
    mats: Dict[str, bytes] = {}

    # 1. 不等间隔精确直线：raw = 100 + t，t 间隔不规则（k + k²/50）
    b = StreamBuilder()
    b.boot(40, 1, EPOCH, 1000)
    b.layout(40, 1, [(0, 1, 0.5, 1.0, 1)])
    for k in range(300):
        t = k + (k * k) // 50
        b.meas(40, 1, t, [100 + t])
    mats["arch_ramp.bin"] = b.bytes()

    # 2. 平直信号 + 中央单点尖峰
    b = StreamBuilder()
    b.boot(42, 1, EPOCH, 1000)
    b.layout(42, 1, [(0, 1, 0.5, 1.0, 1)])
    for k in range(1000):
        b.meas(42, 1, k, [4000 if k == 500 else 200])
    mats["arch_spike.bin"] = b.bytes()

    # 3. 锯齿：振幅 0.1（物理单位），大于 eps=0.01
    b = StreamBuilder()
    b.boot(43, 1, EPOCH, 1000)
    b.layout(43, 1, [(0, 1, 0.5, 1.0, 1)])
    for k in range(600):
        phase = k % 40
        amp = phase if phase <= 20 else 40 - phase
        b.meas(43, 1, k, [200 + amp * 10])
    mats["arch_saw.bin"] = b.bytes()

    # 4. 巨大纳秒基数 + 1ns 间隔：hz=1e9，tick=k → ts=EPOCH+k
    b = StreamBuilder()
    b.boot(44, 1, EPOCH, 1_000_000_000)
    b.layout(44, 1, [(0, 1, 0.5, 1.0, 1)])
    for k in range(1000):
        b.meas(44, 1, k, [1000 + k])
    mats["arch_hugens.bin"] = b.bytes()

    # 5. 重复时刻：hz=3e9 → tick=1,2 都落在 EPOCH+0；tick=3,4 落在 EPOCH+1
    #    文件 2 只有测量包（设备状态在库中跨文件延续），制造跨来源重复
    b = StreamBuilder()
    b.boot(45, 1, EPOCH, 3_000_000_000)
    b.layout(45, 1, [(0, 1, 0.5, 1.0, 1)])
    b.meas(45, 1, 1, [100])                    # ts=EPOCH+0, v=0.051
    mats["arch_dup1.bin"] = b.bytes()
    b = StreamBuilder()
    b.layout(45, 1, [(0, 1, 0.5, 1.0, 1)])     # 重声明：接入时按重复忽略
    b.meas(45, 1, 2, [200])                    # ts=EPOCH+0, v=0.101（冲突）
    b.meas(45, 1, 3, [101])                    # ts=EPOCH+1, v=0.0515
    b.meas(45, 1, 4, [102])                    # ts=EPOCH+1, v=0.052（相容重复）
    mats["arch_dup2.bin"] = b.bytes()

    # 6. 跨启动周期 / 跨布局 / 单位变化（同一设备同一通道号）
    b = StreamBuilder()
    b.boot(46, 1, EPOCH, 1000)
    b.layout(46, 1, [(0, 1, 1.0, 0.0, 1)])     # ch0 int16 → °C
    b.layout(46, 2, [(0, 2, 1.0, 0.0, 2)])     # ch0 int32 → kPa
    b.meas(46, 1, 1, [10])
    b.meas(46, 2, 2, [20])
    b.meas(46, 1, 3, [11])
    b.meas(46, 2, 4, [21])
    b.boot(46, 2, EPOCH + 1_000_000_000_000, 1000)   # 新启动周期
    b.layout(46, 1, [(0, 1, 1.0, 0.0, 1)])           # 新周期重新声明
    b.meas(46, 1, 0, [50])
    b.meas(46, 1, 1, [51])
    mats["arch_multi.bin"] = b.bytes()

    return mats
