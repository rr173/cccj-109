"""差分隐私计数共享子系统（dp_share）。

面向合作研究方的**带预算账本的差分隐私计数发布**能力。本包是独立的新能力：
- 只读访问既有 DuckDB 的 ``run_devices``，不写 DuckDB、不改任何既有管线；
- 隐私账本使用独立的 SQLite 工作区文件；
- 不提供任意 SQL、不接受逐设备条件、不暴露真实人数与原始噪声。

公共模块：
- :class:`~dp_share.errors.DPShareError` 及各子类（带稳定错误码）；
- :func:`~dp_share.source.extract_queue` 从 DuckDB 抽取并校验共享队列；
- :class:`~dp_share.noise.SecureLaplaceSource` 系统安全随机源 Laplace 噪声；
- :func:`~dp_share.ledger.provision_workspace` /
  :func:`~dp_share.ledger.publish` / :func:`~dp_share.ledger.read_ledger`。
"""

from .errors import (
    BudgetExceededError,
    DPShareError,
    LedgerConflictError,
    MechanismError,
    ProvisionError,
    QueryValidationError,
    SourceChangedError,
    SourceUnavailableError,
)
from .ledger import provision_workspace, publish, read_ledger
from .noise import SecureLaplaceSource
from .source import PUBLIC_OUTCOMES, extract_queue

__version__ = "1.0.0"

__all__ = [
    "PUBLIC_OUTCOMES",
    "DPShareError",
    "BudgetExceededError",
    "LedgerConflictError",
    "MechanismError",
    "ProvisionError",
    "QueryValidationError",
    "SourceChangedError",
    "SourceUnavailableError",
    "SecureLaplaceSource",
    "extract_queue",
    "provision_workspace",
    "publish",
    "read_ledger",
]
