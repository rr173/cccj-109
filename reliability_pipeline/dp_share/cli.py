"""差分隐私计数共享命令行入口（``python3 -m dp_share.cli``）。

子命令
======
- ``provision``  从 DuckDB 的 run_devices 选定 run_id 开通共享工作区；
- ``publish``    合作研究方提交一次格子向量计数发布；
- ``ledger``     只读查账（request_id ↔ 发布编号 ↔ 花费）。

所有正常输出为单行 JSON（stdout）；错误输出为 JSON 到 stderr 且退出码非 0。
输出内容不含真实人数、逐设备资料、原始噪声或随机源状态。

生产 ``publish`` 固定使用系统安全随机源，命令行没有 seed/确定性开关；
测试需要的确定性噪声与崩溃点只在引擎内部装配，不暴露到本命令。
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import DPShareError
from .ledger import provision_workspace, publish, read_ledger


def _emit(obj: dict) -> int:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


def _emit_error(exc: DPShareError) -> int:
    payload = {"ok": False, "error_code": exc.code, "error": exc.message}
    if exc.details:
        payload["error_details"] = exc.details
    sys.stderr.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    # 不同错误类别给稳定退出码，便于脚本判断；均为非零。
    codes = {
        "provision_error": 4,
        "query_validation_error": 5,
        "budget_exceeded": 6,
        "request_conflict": 7,
        "source_changed": 8,
        "source_unavailable": 9,
        "mechanism_error": 10,
    }
    return codes.get(exc.code, 1)


def _csv_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip() != ""]


def cmd_provision(args) -> int:
    result = provision_workspace(
        db_path=args.db,
        run_id=args.run_id,
        machine_types=_csv_list(args.machine_types),
        epsilon_total_micro=args.epsilon_total_micro,
        workspace_path=args.workspace,
    )
    result["ok"] = True
    return _emit(result)


def cmd_publish(args) -> int:
    result = publish(
        workspace_path=args.workspace,
        request_id=args.request_id,
        machine_types=_csv_list(args.machine_types),
        outcomes=_csv_list(args.outcomes),
        epsilon_micro=args.epsilon_micro,
    )
    result["ok"] = True
    result["request_id"] = args.request_id
    return _emit(result)


def cmd_ledger(args) -> int:
    result = read_ledger(args.workspace)
    result["ok"] = True
    return _emit(result)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="dp-share",
        description="差分隐私计数共享：带隐私预算账本的 Laplace 格子计数发布",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("provision", help="从 DuckDB 选定队列并开通共享工作区")
    sp.add_argument("--db", required=True, help="既有可靠性 DuckDB（只读打开）")
    sp.add_argument("--run-id", required=True, help="run_devices 中的 run_id")
    sp.add_argument(
        "--machine-types", required=True,
        help="公共机型字典，逗号分隔；显式提供，不从私有数据自动挑选",
    )
    sp.add_argument(
        "--epsilon-total-micro", required=True, type=int,
        help="总预算，正整数，单位百万分之一 epsilon",
    )
    sp.add_argument("--workspace", required=True, help="新建 SQLite 账本路径（不可已存在）")
    sp.set_defaults(fn=cmd_provision)

    sp = sub.add_parser("publish", help="提交一次差分隐私计数发布")
    sp.add_argument("--workspace", required=True)
    sp.add_argument("--request-id", required=True, help="非空字符串，研究方提供")
    sp.add_argument("--machine-types", required=True, help="机型子集，逗号分隔")
    sp.add_argument("--outcomes", required=True, help="结局子集，逗号分隔")
    sp.add_argument(
        "--epsilon-micro", required=True, type=int,
        help="本次发布费用，正整数，单位百万分之一 epsilon",
    )
    sp.set_defaults(fn=cmd_publish)

    sp = sub.add_parser("ledger", help="只读查账")
    sp.add_argument("--workspace", required=True)
    sp.set_defaults(fn=cmd_ledger)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except DPShareError as exc:
        return _emit_error(exc)
    except ValueError as exc:
        # argparse 之外的参数问题（理论上 type=int 已拦住大部分）
        payload = {"ok": False, "error_code": "query_validation_error",
                   "error": str(exc)}
        sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return 5


if __name__ == "__main__":
    sys.exit(main())
