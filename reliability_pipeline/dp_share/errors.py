"""稳定错误类型。所有对外错误都带机器可读的 ``code``。"""

from __future__ import annotations


class DPShareError(Exception):
    """本子系统所有错误的基类。"""

    code = "dp_share_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ProvisionError(DPShareError):
    """开通阶段错误：表结构、队列校验、公共域校验、工作区已存在等。"""

    code = "provision_error"


class QueryValidationError(DPShareError):
    """研究方请求不合法（空子集、未知项、非法预算、非法 request_id 等）。"""

    code = "query_validation_error"


class BudgetExceededError(DPShareError):
    """剩余 micro 预算不足以承担本次新发布。"""

    code = "budget_exceeded"


class LedgerConflictError(DPShareError):
    """同一 request_id 携带了与首次发布不同的规范参数。"""

    code = "request_conflict"


class SourceChangedError(DPShareError):
    """源队列逻辑内容与绑定时不一致，工作区已冻结新查询。"""

    code = "source_changed"


class SourceUnavailableError(DPShareError):
    """源 DuckDB 暂时无法读取（文件缺失/打不开），新请求失败关闭。"""

    code = "source_unavailable"


class MechanismError(DPShareError):
    """噪声机制内部错误（非法尺度等，正常配置下不应发生）。"""

    code = "mechanism_error"
