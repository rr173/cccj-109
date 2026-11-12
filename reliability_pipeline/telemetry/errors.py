"""遥测接入口的错误类型。

``IngestError`` 表示续读前提被破坏：源文件被替换/改写、路径被另一个文件
对象占用、历史来源身份无法证明等。CLI 对应退出码 3，且整个事务回滚，
不会新增样本、拒收台账或推进进度。
"""

from __future__ import annotations


class IngestError(RuntimeError):
    """续读前提被破坏（前缀改写 / 文件替换 / 身份未知 / 截断到进度之前）。"""
