"""测试专用崩溃点装配脚本（不是发布命令的一部分）。

生产 ``python -m dp_share.cli publish`` 没有任何确定性噪声/崩溃开关；
本脚本只被 tests/test_dp_crash.py 以子进程方式调用，通过环境变量在引擎
内部装配：
- ``DP_FAULT=pre_commit``  ：事务提交前 os._exit；
- ``DP_FAULT=post_commit`` ：事务提交后、输出前 os._exit；
- ``DP_SCRIPTED_NOISE``    ：逗号分隔的确定性 Laplace 数值（测试替身）。
"""

from __future__ import annotations

import os
import sys

# 让脚本在 `python tests/dp_fault_driver.py ...` 下也能导入仓库包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dp_share.ledger import publish  # noqa: E402


class _EnvNoise:
    def __init__(self, values):
        self._values = list(values)

    def laplace(self, scale: float) -> float:
        return self._values.pop(0)


def main() -> int:
    import json

    workspace, request_id, machines, outcomes, eps = sys.argv[1:6]
    noise = None
    scripted = os.environ.get("DP_SCRIPTED_NOISE", "")
    if scripted:
        noise = _EnvNoise([float(x) for x in scripted.split(",")])
    resp = publish(
        workspace,
        request_id,
        machines.split(","),
        outcomes.split(","),
        int(eps),
        noise_source=noise,
        fault_point=os.environ.get("DP_FAULT"),
    )
    sys.stdout.write(json.dumps(resp, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
