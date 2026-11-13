#!/usr/bin/env bash
# 一条命令跑通启机过程识别演示：真实二进制接入命令生成测量 → 识别 →
# 逐条核对结果与来源 → 边界用例（直接建库）→ 引擎 vs 独立参考交叉核对。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m startup.cli demo --workdir outputs/startup_demo "$@"
