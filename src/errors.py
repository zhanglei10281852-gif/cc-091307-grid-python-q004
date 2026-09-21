"""燃气隐患闭环服务的异常类型。"""


class ClosureError(Exception):
    """闭环服务领域异常基类。"""


class NotFoundError(ClosureError):
    """引用的住户、设备或隐患不存在。"""


class ValidationError(ClosureError):
    """登记或上报数据不完整（例如分支未留说明、复核未附证据）。"""


class StateError(ClosureError):
    """操作与隐患当前所处状态不符。"""


class VersionConflict(ClosureError):
    """并发更新冲突：提交依据的版本已过期。

    多人同时更新同一条隐患时，后提交者必须读取最新版本后重试，
    服务端不会以“后写覆盖先写”的方式落库。
    """

    def __init__(self, hazard_id: str, expected: int, current: int):
        self.hazard_id = hazard_id
        self.expected = expected
        self.current = current
        super().__init__(
            f"隐患 {hazard_id} 版本冲突：提交基于 v{expected}，当前已为 v{current}"
        )


class CompletedConclusionError(ClosureError):
    """离线终端补传试图覆盖已完成的结论（复核通过或已销项）。"""


class PermissionDenied(ClosureError):
    """网格员越权访问非所辖楼栋，或角色无权执行该操作。"""


class DuplicateDispatch(ClosureError):
    """重复派发：隐患已在处置流程中，返回原流程而非另起一条。"""

    def __init__(self, hazard_id: str, owner: str):
        self.hazard_id = hazard_id
        self.owner = owner
        super().__init__(
            f"隐患 {hazard_id} 已派发给 {owner}，重复派发将返回原流程"
        )
