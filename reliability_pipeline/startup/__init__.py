"""离线启机过程识别器（startup attempt detector）。

直接读取 DuckDB 中的标准遥测测量（``tele_samples``，由 ``telemetry`` 子系统
接入），按一份固定的 JSON 启机配方识别每一次启机尝试，并给出可复核的
三态判断（confirmed / rejected / indeterminate）与证据链。

本包只回答「这次启机过程是否按配方完成」：

- 不推断故障结局、不计算可靠性/生存曲线（那是 ``pipeline`` 包的职责）；
- 不经过压缩归档（``telemetry.arch*``），始终读取原始测量表；
- 不同设备、不同 boot_seq 的状态彼此隔离；布局号不参与状态键。

模块
----
- ``recipe``     JSON 配方装载与校验（通道/单位/max_age/阈值/时长）
- ``model``      结果与证据的数据类型（Attempt / EvidenceItem / 状态码）
- ``engine``     生产状态机：按事件时刻推进（更新 + 过期/确认/完成/期限定时器）
- ``reference``  独立的小规模参考判定器（枚举事件断点，不调用生产状态机）
- ``store``      DuckDB 结果表（``startup_runs/startup_attempts/startup_evidence*``）
- ``demo``       演示材料（真实二进制接入 + 直接建库的边界用例）
- ``cli``        命令行入口（``python3 -m startup.cli``）
"""

__version__ = "1.0.0"
