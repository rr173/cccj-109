#!/usr/bin/env bash
# 一条命令跑通离线时钟对齐示例：真实二进制接入 → 锚点校验 → 投影 →
# 参考钟窗口 → 手算有理数核对 → 负例（矛盾/超外推/非法行）→ 回归材料。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m alignment.cli demo --workdir outputs/alignment_demo "$@"
