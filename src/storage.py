"""JSON 文件存储：原子写入，服务重启后状态完整恢复。

逾期是按“整改期限日期”实时计算的，不依赖任何内存计时器，
因此重启后未完事项的逾期天数自然继续累计。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime
from typing import Any, Optional

from .models import (
    AuthorizationScope,
    CheckItem,
    CheckRecord,
    CheckResult,
    Device,
    DeviceStatus,
    Event,
    EventType,
    Hazard,
    HazardState,
    Resident,
    ReviewEvidence,
    Role,
    User,
)


class JsonStore:
    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, dict[str, Any]] = {
            "users": {},
            "residents": {},
            "devices": {},
            "hazards": {},
            "counters": {},
        }
        self.load()

    def load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data = json.load(fh)
        for key in ("users", "residents", "devices", "hazards", "counters"):
            self.data.setdefault(key, {})

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2,
                          default=_json_default)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def next_id(self, kind: str, prefix: str) -> str:
        n = int(self.data["counters"].get(kind, 0)) + 1
        self.data["counters"][kind] = n
        return f"{prefix}{n:04d}"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"不可序列化的类型: {type(obj)!r}")


# ---------------------------------------------------------------------------
# 反序列化
# ---------------------------------------------------------------------------
def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _parse_date(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def user_from_dict(d: dict[str, Any]) -> User:
    return User(
        id=d["id"],
        name=d["name"],
        role=Role(d["role"]),
        buildings=list(d.get("buildings", [])),
    )


def resident_from_dict(d: dict[str, Any]) -> Resident:
    return Resident(
        id=d["id"],
        name=d["name"],
        building=d["building"],
        room=d["room"],
        authorization=AuthorizationScope(d["authorization"]),
        contact=d.get("contact", ""),
    )


def check_record_from_dict(d: dict[str, Any]) -> CheckRecord:
    return CheckRecord(
        at=_parse_dt(d["at"]),
        item=d["item"],
        result=CheckResult(d["result"]),
        checker=d["checker"],
        record_ref=d.get("record_ref", ""),
        note=d.get("note", ""),
    )


def device_from_dict(d: dict[str, Any]) -> Device:
    return Device(
        id=d["id"],
        resident_id=d["resident_id"],
        building=d["building"],
        status=DeviceStatus(d["status"]),
        check_history=[check_record_from_dict(r)
                       for r in d.get("check_history", [])],
    )


def event_from_dict(d: dict[str, Any]) -> Event:
    return Event(
        seq=d["seq"],
        type=EventType(d["type"]),
        actor=d["actor"],
        at=_parse_dt(d["at"]),
        detail=d.get("detail", {}),
    )


def check_item_from_dict(d: dict[str, Any]) -> CheckItem:
    return CheckItem(
        name=d["name"],
        result=CheckResult(d["result"]),
        note=d.get("note", ""),
        checked_by=d.get("checked_by", ""),
        checked_at=_parse_dt(d.get("checked_at")),
    )


def review_from_dict(d: dict[str, Any]) -> ReviewEvidence:
    return ReviewEvidence(
        reviewer=d["reviewer"],
        passed=d["passed"],
        at=_parse_dt(d["at"]),
        items=[check_item_from_dict(i) for i in d.get("items", [])],
        evidence_refs=list(d.get("evidence_refs", [])),
        note=d.get("note", ""),
    )


def hazard_from_dict(d: dict[str, Any]) -> Hazard:
    return Hazard(
        id=d["id"],
        resident_id=d["resident_id"],
        building=d["building"],
        device_id=d["device_id"],
        title=d["title"],
        description=d.get("description", ""),
        discovered_by=d["discovered_by"],
        owner=d.get("owner", ""),
        state=HazardState(d["state"]),
        found_at=_parse_dt(d["found_at"]),
        rectify_deadline=_parse_date(d.get("rectify_deadline")),
        first_dispatched_at=_parse_dt(d.get("first_dispatched_at")),
        check_items=[check_item_from_dict(i) for i in d.get("check_items", [])],
        reviews=[review_from_dict(r) for r in d.get("reviews", [])],
        events=[event_from_dict(e) for e in d.get("events", [])],
        branch_note=d.get("branch_note", ""),
        overdue_flagged=d.get("overdue_flagged", False),
        closed_at=_parse_dt(d.get("closed_at")),
        version=int(d.get("version", 0)),
        base_version=int(d.get("base_version", 0)),
        resolved_version=d.get("resolved_version"),
    )
