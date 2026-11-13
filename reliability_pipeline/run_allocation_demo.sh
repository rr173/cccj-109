#!/usr/bin/env bash
# 一条命令跑通实验分配示例：无解（跨层）→ 多解+锁定逐 rank 核对 →
# Z>2^53 末尾 rank → 分布检查 → 边界 → 文件安全/行序/跨进程回放。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m allocation.cli demo --workdir outputs/allocation_demo "$@"
