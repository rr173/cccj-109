#!/usr/bin/env bash
# 一条命令跑通折线压缩归档演示：材料生成 → 接入 → 逐材料归档 →
# 独立核验（逐点误差 + 范围交叉核对）→ 窄查询块选择性 → 损坏/截断检测。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m telemetry.archcli demo --workdir outputs/archive_demo "$@"
