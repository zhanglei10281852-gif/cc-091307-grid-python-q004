"""领域异常定义。"""


class DomainError(Exception):
    """业务规则错误基类。"""


class NotFoundError(DomainError):
    """对象不存在。"""


class VersionConflictError(DomainError):
    """版本冲突：同一隐患已被他人修改。"""


class StateTransitionError(DomainError):
    """非法状态流转。"""


class ValidationError(DomainError):
    """参数或数据校验失败。"""


class AccessDeniedError(DomainError):
    """越权访问。"""
