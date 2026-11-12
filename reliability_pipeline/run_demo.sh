#!/usr/bin/env bash
# 一条命令跑通：日志生成 → 两次不同截点的分析 → 金标准核对 →
# 竞争风险偏差展示 → 随机交叉核对。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m pipeline.cli demo --workdir . "$@"
