"""领域模型：状态机、分支与登记数据结构。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def ensure_aware(dt: datetime) -> datetime:
    """统一为带时区时间；naive 时间按 UTC 处理。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def to_iso(dt: datetime) -> str:
    return ensure_aware(dt).isoformat()


def parse_iso(text: str) -> datetime:
    return ensure_aware(datetime.fromisoformat(text))


class HazardStatus(str, Enum):
    """隐患主流程状态：发现 → 派发 → 上门 → 复核 → 销项。"""

    DISCOVERED = "discovered"            # 已发现，待派发
    DISPATCHED = "dispatched"            # 已派发，待上门
    ON_SITE = "on_site"                  # 上门处理中
    ENTRY_REFUSED = "entry_refused"      # 分支：住户拒绝入户
    DEVICE_REPLACED = "device_replaced"  # 分支：设备已更换，待复核
    PENDING_REVIEW = "pending_review"    # 整改完成，待复核
    CLOSED = "closed"                    # 已销项（仅复核通过可到达）


class Branch(str, Enum):
    """当前所处的异常分支标记。"""

    NONE = "none"
    ENTRY_REFUSED = "entry_refused"      # 住户拒绝入户
    DEVICE_REPLACED = "device_replaced"  # 设备更换
    OVERDUE = "overdue"                  # 超过整改期限


class VisitOutcome(str, Enum):
    """上门结果。"""

    IN_PROGRESS = "in_progress"          # 已上门，处理中（如需等待配件）
    RECTIFIED = "rectified"              # 当场整改完成
    ENTRY_REFUSED = "entry_refused"      # 住户拒绝入户
    DEVICE_REPLACED = "device_replaced"  # 现场更换设备


class OverdueAction(str, Enum):
    """超期处理动作。"""

    EXTEND = "extend"        # 延期（须给出新期限与说明）
    ESCALATE = "escalate"    # 升级督办（须留说明）


# 主流程推进顺序，用于离线补传的“回退”判定
STATUS_ORDER: dict[HazardStatus, int] = {
    HazardStatus.DISCOVERED: 1,
    HazardStatus.DISPATCHED: 2,
    HazardStatus.ON_SITE: 3,
    HazardStatus.ENTRY_REFUSED: 3,
    HazardStatus.DEVICE_REPLACED: 4,
    HazardStatus.PENDING_REVIEW: 4,
    HazardStatus.CLOSED: 5,
}

# 允许的状态流转（状态机强约束）
ALLOWED_TRANSITIONS: frozenset[tuple[HazardStatus, HazardStatus]] = frozenset({
    (HazardStatus.DISCOVERED, HazardStatus.DISPATCHED),        # 派发
    (HazardStatus.ENTRY_REFUSED, HazardStatus.DISPATCHED),     # 拒绝入户后重新派发
    (HazardStatus.PENDING_REVIEW, HazardStatus.DISPATCHED),    # 复核不通过退回整改
    (HazardStatus.DISPATCHED, HazardStatus.ON_SITE),           # 上门
    (HazardStatus.DISPATCHED, HazardStatus.ENTRY_REFUSED),
    (HazardStatus.DISPATCHED, HazardStatus.DEVICE_REPLACED),
    (HazardStatus.DISPATCHED, HazardStatus.PENDING_REVIEW),
    (HazardStatus.ON_SITE, HazardStatus.ON_SITE),              # 多次上门
    (HazardStatus.ON_SITE, HazardStatus.ENTRY_REFUSED),
    (HazardStatus.ON_SITE, HazardStatus.DEVICE_REPLACED),
    (HazardStatus.ON_SITE, HazardStatus.PENDING_REVIEW),
    (HazardStatus.DEVICE_REPLACED, HazardStatus.PENDING_REVIEW),
    (HazardStatus.PENDING_REVIEW, HazardStatus.CLOSED),        # 复核通过 → 销项
})


@dataclass
class Authorization:
    """住户授权范围。"""

    entry_allowed: bool              # 允许入户检查/维修
    device_replace_allowed: bool     # 允许更换设备
    contact_allowed: bool            # 允许电话联系
    granted_at: datetime             # 授权时间
    note: str = ""                   # 授权补充说明

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_allowed": self.entry_allowed,
            "device_replace_allowed": self.device_replace_allowed,
            "contact_allowed": self.contact_allowed,
            "granted_at": to_iso(self.granted_at),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Authorization":
        return cls(
            entry_allowed=bool(d["entry_allowed"]),
            device_replace_allowed=bool(d["device_replace_allowed"]),
            contact_allowed=bool(d["contact_allowed"]),
            granted_at=parse_iso(d["granted_at"]),
            note=d.get("note", ""),
        )


@dataclass
class DeviceInfo:
    """燃气设备状态。"""

    device_id: str
    kind: str = "燃气报警器"
    online: bool = True
    fault: str = ""                              # 故障描述，如“长期离线”
    last_check_at: datetime | None = None        # 最近一次检测时间

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "kind": self.kind,
            "online": self.online,
            "fault": self.fault,
            "last_check_at": to_iso(self.last_check_at) if self.last_check_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeviceInfo":
        return cls(
            device_id=d["device_id"],
            kind=d.get("kind", "燃气报警器"),
            online=bool(d.get("online", True)),
            fault=d.get("fault", ""),
            last_check_at=parse_iso(d["last_check_at"]) if d.get("last_check_at") else None,
        )


@dataclass
class InspectionItem:
    """检查项目。"""

    name: str
    required: bool = True
    result: str = "pending"          # pending / passed / failed
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "result": self.result,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InspectionItem":
        return cls(
            name=d["name"],
            required=bool(d.get("required", True)),
            result=d.get("result", "pending"),
            note=d.get("note", ""),
        )
