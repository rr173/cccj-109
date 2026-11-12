#!/usr/bin/env bash
# 一条命令跑通遥测接入演示：样例包生成 → 各场景接入（含逐字节分片与
# 尾帧补齐）→ SIGKILL 崩溃恢复 → 偏移回溯与时间段/通道筛选查询。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m telemetry.cli demo --workdir outputs/telemetry_demo "$@"
