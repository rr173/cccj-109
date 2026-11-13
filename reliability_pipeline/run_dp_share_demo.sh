#!/usr/bin/env bash
# 差分隐私计数共享演示（可自行复跑，全部产物落在临时目录，不污染仓库）。
#
# 覆盖：
#   1. 从真实 DuckDB outputs/reliability.duckdb 的 run_devices 选定 gold_run1 开通；
#   2. 系统安全随机源的真实发布、改序/去重换 request_id 的复用不收费；
#   3. 同 request_id 改参数冲突、未知公共域项/空子集/非法预算被拒；
#   4. 恰好花完预算后再查询被拒、已记账请求仍可重放；
#   5. 只读查账；
#   6. 再次开通被拒（既有账本不能清零）；
#   7. 源队列变化后冻结新查询（复制一份材料库改写，不动原库）。
#
# 单元/并发/崩溃/敏感度等论证由 pytest 覆盖：
#   python3 -m pytest tests/test_dp_*.py -q
set -euo pipefail

cd "$(dirname "$0")"
DB=outputs/reliability.duckdb
WORK="$(mktemp -d)/dp_share_demo"
WS="$WORK/ws.sqlite"
mkdir -p "$WORK"
echo "== 演示工作区: $WORK =="

j() { python3 -m json.tool --no-ensure-ascii; }

echo
echo "== 1) 开通：run_id=gold_run1，公共机型字典 A,B,C,D（D 无设备也保留格子），总预算 2,000,000 micro = epsilon 2 =="
python3 -m dp_share.cli provision --db "$DB" --run-id gold_run1 \
  --machine-types A,B,C,D --epsilon-total-micro 2000000 --workspace "$WS" | j

echo
echo "== 2) 首次发布（系统安全随机源，epsilon 0.5 = 500000 micro）=="
python3 -m dp_share.cli publish --workspace "$WS" --request-id req-001 \
  --machine-types B,A --outcomes censored,failure --epsilon-micro 500000 | j

echo
echo "== 3) 改序+去重+换 request_id 的同一规范查询：复用原答案，不再收费 =="
python3 -m dp_share.cli publish --workspace "$WS" --request-id req-002 \
  --machine-types A,B,B --outcomes failure,censored --epsilon-micro 500000 | j

echo
echo "== 4) 同一 request_id 改参数：冲突（退出码 7），不扣款 =="
set +e
python3 -m dp_share.cli publish --workspace "$WS" --request-id req-001 \
  --machine-types A --outcomes failure --epsilon-micro 500000
echo "退出码=$?（7=request_conflict）"
set -e

echo
echo "== 5) 非法请求：未知机型 / 空结局 / 非正预算（退出码 5），均不扣款 =="
set +e
python3 -m dp_share.cli publish --workspace "$WS" --request-id bad1 \
  --machine-types Z --outcomes failure --epsilon-micro 10
python3 -m dp_share.cli publish --workspace "$WS" --request-id bad2 \
  --machine-types A --outcomes "" --epsilon-micro 10
python3 -m dp_share.cli publish --workspace "$WS" --request-id bad3 \
  --machine-types A --outcomes failure --epsilon-micro 0
set -e

echo
echo "== 6) 新发布花掉剩余 1,500,000 micro，再查被 budget_exceeded（退出码 6）=="
python3 -m dp_share.cli publish --workspace "$WS" --request-id req-003 \
  --machine-types A,B,C,D --outcomes failure,retirement,censored \
  --epsilon-micro 1500000 | j
set +e
python3 -m dp_share.cli publish --workspace "$WS" --request-id req-004 \
  --machine-types C --outcomes failure --epsilon-micro 1
echo "退出码=$?（6=budget_exceeded）"
set -e

echo
echo "== 7) 预算花完后，已记账请求 req-001 仍可重放，拿回同一答案且不收费 =="
python3 -m dp_share.cli publish --workspace "$WS" --request-id req-001 \
  --machine-types B,A --outcomes censored,failure --epsilon-micro 500000 | j

echo
echo "== 8) 只读查账：request_id ↔ 发布编号 ↔ 花费（无真实人数/逐设备资料/原始噪声）=="
python3 -m dp_share.cli ledger --workspace "$WS" | j

echo
echo "== 9) 再次开通同一工作区被拒（不能清零既有预算）=="
set +e
python3 -m dp_share.cli provision --db "$DB" --run-id gold_run1 \
  --machine-types A,B,C,D --epsilon-total-micro 9999999 --workspace "$WS"
echo "退出码=$?（4=provision_error）"
set -e

echo
echo "== 10) 源队列变化即冻结：在工作区绑定的同一文件路径上替换内容（模拟上游重跑）=="
ALT_DB="$WORK/altered.duckdb"
# 先复制一份与原库完全一致的材料，并据此开通工作区（绑定该路径与指纹）
cp "$DB" "$ALT_DB"
WS2="$WORK/ws2.sqlite"
python3 -m dp_share.cli provision --db "$ALT_DB" --run-id gold_run1 \
  --machine-types A,B,C,D --epsilon-total-micro 1000000 --workspace "$WS2" | j
# 路径不变、逻辑内容改变：改写一台设备的结局
python3 - "$ALT_DB" <<'PY'
import sys, duckdb
dst = sys.argv[1]
con = duckdb.connect(dst)
con.execute("UPDATE run_devices SET status='retirement' "
            "WHERE run_id='gold_run1' AND device_id='D01'")
con.close()
print("已在同一路径替换源内容:", dst)
PY
set +e
python3 -m dp_share.cli publish --workspace "$WS2" --request-id req-after-change \
  --machine-types A --outcomes failure --epsilon-micro 10
echo "退出码=$?（8=source_changed）"
set -e
echo "冻结后查账（frozen=true，旧发布不变、零发布零扣款）："
python3 -m dp_share.cli ledger --workspace "$WS2" | j | head -16

echo
echo "演示完成。临时目录保留备查: $WORK"
