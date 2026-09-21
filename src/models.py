"""领域模型：状态机、住户授权、设备、隐患、检查项与流程事件。

闭环流程严格按 发现 → 派发 → 上门 → 复核 → 销项 推进，
拒绝入户、设备更换、逾期分别走独立分支并强制留说明。
逾期采用“标记叠加”而非独立状态，以免冲掉主流程状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 角色与枚举
# ---------------------------------------------------------------------------
class Role(str, Enum):
    GRID_WORKER = "grid_worker"  # 网格员：只能看所辖楼栋
    MANAGER = "manager"          # 管理者：可追踪责任、逾期与复核证据
    REPAIRER = "repairer"        # 维修人员：上门处置
    SAFETY_OFFICER = "safety_officer"  # 物业安全员：发现上报


class AuthorizationScope(str, Enum):
    """住户授权范围（入户作业的合法边界）。"""

    INSPECTION = "inspection"          # 仅允许检查
    INSPECTION_AND_REPAIR = "inspection_and_repair"  # 检查 + 维修
    DEVICE_REPLACEMENT = "device_replacement"        # 允许更换设备
    NONE = "none"                      # 明确拒绝任何入户


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    REPLACED = "replaced"          # 已更换
    REPLACEMENT_PENDING = "replacement_pending"  # 需更换，待安排


class CheckResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"


class HazardState(str, Enum):
    """隐患生命周期主状态（每一步都有事件留痕）。"""

    DISCOVERED = "discovered"        # 发现
    DISPATCHED = "dispatched"        # 已派发，等待上门
    ONSITE = "onsite"                # 已上门，等待复核
    RE_REVIEW_PENDING = "re_review_pending"  # 复核未过，整改后待再次复核
    RESOLVED = "resolved"            # 复核通过，等待销项确认
    CLOSED = "closed"                # 销项（终态）
    # —— 异常分支（均带说明，且不影响主流程回归）——
    ACCESS_DENIED = "access_denied"            # 住户拒绝入户
    REPLACEMENT_PENDING = "replacement_pending"  # 等待设备更换
    # 逾期不另设状态：以 overdue_flagged 标记 + OVERDUE_FLAG 事件作为独立分支，
    # 逾期天数按整改期限日期实时计算，服务重启后自然继续计时。


# 状态机允许的流转关系。
ALLOWED_TRANSITIONS: dict[HazardState, set[HazardState]] = {
    HazardState.DISCOVERED: {HazardState.DISPATCHED},
    HazardState.DISPATCHED: {
        HazardState.ONSITE,
        HazardState.ACCESS_DENIED,
        HazardState.REPLACEMENT_PENDING,
    },
    HazardState.ONSITE: {
        HazardState.RE_REVIEW_PENDING,
        HazardState.RESOLVED,
    },
    HazardState.RE_REVIEW_PENDING: {
        HazardState.RESOLVED,
    },
    HazardState.RESOLVED: {HazardState.CLOSED},
    HazardState.ACCESS_DENIED: {
        HazardState.DISPATCHED,  # 协调后重新派发，回到原流程
    },
    HazardState.REPLACEMENT_PENDING: {
        HazardState.DISPATCHED,  # 设备到位后重新派发
    },
    HazardState.CLOSED: set(),  # 终态
}


class EventType(str, Enum):
    DISCOVER = "discover"
    DISPATCH = "dispatch"
    RE_DISPATCH = "re_dispatch"      # 从异常分支/重复派发回到原流程
    ONSITE = "onsite"
    ACCESS_DENIED = "access_denied"
    DEVICE_REPLACEMENT = "device_replacement"
    OVERDUE_FLAG = "overdue_flag"
    REVIEW = "review"                # 复核（通过/不通过）
    CLOSE = "close"
    OFFLINE_SYNC = "offline_sync"    # 离线终端补传


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class User:
    id: str
    name: str
    role: Role
    buildings: list[str] = field(default_factory=list)  # 网格员所辖楼栋


@dataclass
class Resident:
    id: str
    name: str
    building: str          # 楼栋，如 "3栋"
    room: str              # 房号
    authorization: AuthorizationScope
    contact: str = ""


@dataclass
class CheckRecord:
    """设备检测记录（维修人员“找不到最近一次检测记录”的问题由此解决）。"""

    at: datetime
    item: str
    result: CheckResult
    checker: str
    record_ref: str = ""          # 单据/报告编号
    note: str = ""


@dataclass
class Device:
    id: str
    resident_id: str
    building: str
    status: DeviceStatus
    check_history: list[CheckRecord] = field(default_factory=list)

    @property
    def last_check_at(self) -> Optional[datetime]:
        return self.check_history[-1].at if self.check_history else None

    @property
    def last_check_record(self) -> Optional[CheckRecord]:
        return self.check_history[-1] if self.check_history else None


@dataclass
class CheckItem:
    """检查项目（上门逐项登记）。"""

    name: str
    result: CheckResult = CheckResult.PENDING
    note: str = ""
    checked_by: str = ""
    checked_at: Optional[datetime] = None


@dataclass
class Event:
    """流程事件：每一次流转、分支与复核都不可变地留痕。"""

    seq: int
    type: EventType
    actor: str
    at: datetime
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewEvidence:
    """复核证据（销项的硬性前提：只有复核通过才能销项）。"""

    reviewer: str
    passed: bool
    at: datetime
    items: list[CheckItem]
    evidence_refs: list[str]          # 照片/报告/签名单据等凭证引用
    note: str = ""


@dataclass
class Hazard:
    id: str
    resident_id: str
    building: str
    device_id: str
    title: str
    description: str

    discovered_by: str
    owner: str = ""                   # 当前责任人（维修人/承办网格员）
    state: HazardState = HazardState.DISCOVERED

    found_at: datetime = field(default_factory=datetime.now)
    rectify_deadline: Optional[date] = None  # 整改期限
    first_dispatched_at: Optional[datetime] = None

    check_items: list[CheckItem] = field(default_factory=list)
    reviews: list[ReviewEvidence] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    branch_note: str = ""             # 最近一次异常分支说明
    overdue_flagged: bool = False     # 逾期标记（叠加在主状态之上）
    closed_at: Optional[datetime] = None

    # 乐观锁版本：每次成功修改 +1；离线补传携带终端修改基线
    version: int = 0
    base_version: int = 0
    # 复核通过结论形成时的版本号；离线补传基线早于它即视为试图覆盖结论
    resolved_version: Optional[int] = None

    def days_overdue(self, today: Optional[date] = None) -> int:
        """逾期天数；复核通过/销项后冻结为 0，否则超过期限按天累计。"""
        if self.state in (HazardState.RESOLVED, HazardState.CLOSED):
            return 0
        if self.rectify_deadline is None:
            return 0
        today = today or date.today()
        return max(0, (today - self.rectify_deadline).days)

    def last_review_passed(self) -> Optional[ReviewEvidence]:
        for evidence in reversed(self.reviews):
            if evidence.passed:
                return evidence
        return None


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"不可序列化的类型: {type(obj)!r}")


def dataclass_to_dict(obj: Any) -> Any:
    """轻量 dataclass → JSON 友好字典（枚举/日期转字符串）。"""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: dataclass_to_dict(v) for k, v in obj.__dict__.items()}
    if isinstance(obj, list):
        return [dataclass_to_dict(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj
