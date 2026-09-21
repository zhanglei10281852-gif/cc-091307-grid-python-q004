"""小区燃气隐患闭环服务入口。

流程：发现 → 派发 → 上门 → 复核 → 销项。
分支：住户拒绝入户 / 设备更换 / 超过期限，均须留下说明。
约束：
- 只有复核通过才能销项；
- 写操作携带版本号做乐观并发控制，多人同时更新同一隐患时后写者收到版本冲突；
- 离线终端补传不得覆盖已完成结论（已销项、已有更新复核结论）；
- 重复派发返回原流程；
- 网格员仅可见所辖楼栋的必要信息，管理者可追踪责任人、逾期天数与每次复核证据；
- 数据落盘 SQLite，服务重启后未完事项按原期限继续计时。
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .errors import (
    AccessDeniedError,
    NotFoundError,
    StateTransitionError,
    ValidationError,
    VersionConflictError,
)
from .models import (
    ALLOWED_TRANSITIONS,
    STATUS_ORDER,
    Authorization,
    Branch,
    DeviceInfo,
    HazardStatus,
    InspectionItem,
    OverdueAction,
    VisitOutcome,
    ensure_aware,
    parse_iso,
    to_iso,
)
from .store import SQLiteStore

ROLE_GRID_WORKER = "grid_worker"
ROLE_MANAGER = "manager"

# 离线补传事件类型 → 目标状态
_OFFLINE_TARGETS: dict[str, HazardStatus] = {
    "visit_in_progress": HazardStatus.ON_SITE,
    "visit_rectified": HazardStatus.PENDING_REVIEW,
    "visit_entry_refused": HazardStatus.ENTRY_REFUSED,
    "visit_device_replaced": HazardStatus.DEVICE_REPLACED,
    "submit_review": HazardStatus.PENDING_REVIEW,
    "review_passed": HazardStatus.CLOSED,
    "review_failed": HazardStatus.DISPATCHED,
}

_REVIEW_EVENTS = {"review_passed", "review_failed"}


class GasHazardService:
    """燃气隐患闭环领域服务。"""

    def __init__(
        self,
        db_path: str = ":memory:",
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._store = SQLiteStore(db_path)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.ready = True

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "GasHazardService":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return ensure_aware(self._now_fn())

    def _get_or_404(self, hazard_id: str) -> dict:
        hz = self._store.get_hazard(hazard_id)
        if hz is None:
            raise NotFoundError(f"隐患不存在：{hazard_id}")
        return hz

    @staticmethod
    def _check_version(hz: dict, expected_version: int | None) -> None:
        if expected_version is not None and hz["version"] != expected_version:
            raise VersionConflictError(
                f"版本冲突：期望 v{expected_version}，当前 v{hz['version']}，"
                "该隐患已被他人更新，请刷新后重试"
            )

    def _append_event(
        self,
        *,
        hazard_id: str,
        type: str,
        actor: str,
        note: str = "",
        detail: dict | None = None,
        source: str = "online",
        occurred_at: datetime | None = None,
        applied: int = 1,
        event_id: str | None = None,
    ) -> None:
        now = self._now()
        self._store.append_event(
            {
                "event_id": event_id or uuid.uuid4().hex,
                "hazard_id": hazard_id,
                "type": type,
                "actor": actor,
                "note": note,
                "detail_json": json.dumps(detail or {}, ensure_ascii=False),
                "source": source,
                "occurred_at": to_iso(occurred_at or now),
                "recorded_at": to_iso(now),
                "applied": applied,
            }
        )

    def _transition(
        self,
        hz: dict,
        to: HazardStatus,
        *,
        actor: str,
        event_type: str,
        note: str = "",
        detail: dict | None = None,
        extra: dict | None = None,
        source: str = "online",
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> dict:
        """状态机流转 + 留痕；返回更新后的隐患行。"""
        frm = HazardStatus(hz["status"])
        if (frm, to) not in ALLOWED_TRANSITIONS:
            raise StateTransitionError(f"不允许从 {frm.value} 流转到 {to.value}")
        now = self._now()
        fields: dict[str, Any] = {"status": to.value, "updated_at": to_iso(now)}
        # 分支标记：进入分支时记录说明，回到主流程时清除
        if to == HazardStatus.ENTRY_REFUSED:
            fields["branch"] = Branch.ENTRY_REFUSED.value
            fields["branch_note"] = note
        elif to == HazardStatus.DEVICE_REPLACED:
            fields["branch"] = Branch.DEVICE_REPLACED.value
            fields["branch_note"] = note
        else:
            fields["branch"] = Branch.NONE.value
            fields["branch_note"] = ""
        if to == HazardStatus.CLOSED:
            fields["closed_at"] = to_iso(now)
        if extra:
            fields.update(extra)
        if not self._store.update_hazard(hz["id"], hz["version"], fields):
            raise VersionConflictError(f"隐患 {hz['id']} 更新时发生版本冲突")
        self._append_event(
            hazard_id=hz["id"],
            type=event_type,
            actor=actor,
            note=note,
            detail=detail,
            source=source,
            occurred_at=occurred_at,
            event_id=event_id,
        )
        return self._get_or_404(hz["id"])

    def _overdue_days(self, hz: dict) -> int:
        """逾期天数：未销项按当前时间计，已销项按销项时间计（重启后继续计时）。"""
        deadline = parse_iso(hz["deadline"])
        if hz["status"] == HazardStatus.CLOSED.value and hz["closed_at"]:
            end = parse_iso(hz["closed_at"])
        else:
            end = self._now()
        return max(0, (end - deadline).days)

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------
    def register_worker(
        self, worker_id: str, name: str, role: str, buildings: Iterable[str] = ()
    ) -> dict:
        """登记人员及其数据权限范围（网格员按楼栋授权）。"""
        if role not in (ROLE_GRID_WORKER, ROLE_MANAGER):
            raise ValidationError(f"未知角色：{role}")
        buildings = list(buildings)
        if role == ROLE_GRID_WORKER and not buildings:
            raise ValidationError("网格员必须指定所辖楼栋")
        with self._lock:
            self._store.upsert_worker(
                {
                    "id": worker_id,
                    "name": name,
                    "role": role,
                    "buildings_json": json.dumps(buildings, ensure_ascii=False),
                }
            )
        return {"worker_id": worker_id, "name": name, "role": role, "buildings": buildings}

    def register_hazard(
        self,
        *,
        building: str,
        unit: str,
        room: str,
        household_name: str,
        household_phone: str,
        authorization: Authorization,
        device: DeviceInfo,
        inspection_items: Iterable[InspectionItem | dict],
        deadline: datetime,
        discovered_by: str,
        note: str = "",
        hazard_id: str | None = None,
    ) -> dict:
        """发现隐患并登记：住户授权范围、设备状态、检查项目、整改期限。"""
        if not isinstance(authorization, Authorization):
            raise ValidationError("必须登记住户授权范围（Authorization）")
        if not isinstance(device, DeviceInfo):
            raise ValidationError("必须登记设备状态（DeviceInfo）")
        items = [
            it if isinstance(it, InspectionItem) else InspectionItem.from_dict(it)
            for it in inspection_items
        ]
        if not items:
            raise ValidationError("检查项目不能为空")
        if not isinstance(deadline, datetime):
            raise ValidationError("整改期限必须是 datetime")
        deadline = ensure_aware(deadline)
        hid = hazard_id or uuid.uuid4().hex
        now = self._now()
        row = {
            "id": hid,
            "building": building,
            "unit": unit,
            "room": room,
            "household_name": household_name,
            "household_phone": household_phone,
            "authorization_json": json.dumps(authorization.to_dict(), ensure_ascii=False),
            "device_json": json.dumps(device.to_dict(), ensure_ascii=False),
            "items_json": json.dumps([i.to_dict() for i in items], ensure_ascii=False),
            "status": HazardStatus.DISCOVERED.value,
            "branch": Branch.NONE.value,
            "branch_note": "",
            "assignee": None,
            "deadline": to_iso(deadline),
            "review_verdict": None,
            "discovered_by": discovered_by,
            "note": note,
            "created_at": to_iso(now),
            "updated_at": to_iso(now),
            "closed_at": None,
            "version": 1,
        }
        with self._lock:
            self._store.insert_hazard(row)
            self._append_event(
                hazard_id=hid,
                type="discovered",
                actor=discovered_by,
                note=note or "发现隐患并登记",
                detail={"device": device.to_dict(), "deadline": to_iso(deadline)},
                occurred_at=now,
            )
        return self._manager_view(self._get_or_404(hid))

    # ------------------------------------------------------------------
    # 派发（重复派发返回原流程）
    # ------------------------------------------------------------------
    def dispatch(
        self,
        *,
        hazard_id: str,
        assignee: str,
        actor: str,
        request_id: str | None = None,
        note: str = "",
        expected_version: int | None = None,
    ) -> dict:
        """派发隐患。同一 request_id 或已有进行中流程时返回原流程。"""
        with self._lock:
            if request_id:
                dup = self._store.get_dispatch_by_request(request_id)
                if dup:
                    return {
                        "dispatch_id": dup["id"],
                        "duplicated": True,
                        "hazard": self._manager_view(self._get_or_404(dup["hazard_id"])),
                    }
            hz = self._get_or_404(hazard_id)
            status = HazardStatus(hz["status"])
            if status == HazardStatus.CLOSED:
                raise StateTransitionError("隐患已销项，不能再次派发")
            if status in (
                HazardStatus.DISPATCHED,
                HazardStatus.ON_SITE,
                HazardStatus.DEVICE_REPLACED,
                HazardStatus.PENDING_REVIEW,
            ):
                # 已有进行中的处理流程：重复派发直接返回原流程
                existing = self._store.latest_dispatch(hazard_id)
                return {
                    "dispatch_id": existing["id"],
                    "duplicated": True,
                    "hazard": self._manager_view(hz),
                }
            # DISCOVERED / ENTRY_REFUSED（拒绝入户后重新派发）
            self._check_version(hz, expected_version)
            dispatch_id = uuid.uuid4().hex
            now = self._now()
            self._store.insert_dispatch(
                {
                    "id": dispatch_id,
                    "hazard_id": hazard_id,
                    "request_id": request_id,
                    "assignee": assignee,
                    "actor": actor,
                    "note": note,
                    "created_at": to_iso(now),
                }
            )
            hz = self._transition(
                hz,
                HazardStatus.DISPATCHED,
                actor=actor,
                event_type="dispatched",
                note=note or f"派发给 {assignee}",
                detail={
                    "dispatch_id": dispatch_id,
                    "assignee": assignee,
                    "request_id": request_id,
                },
                extra={"assignee": assignee},
            )
            return {
                "dispatch_id": dispatch_id,
                "duplicated": False,
                "hazard": self._manager_view(hz),
            }

    # ------------------------------------------------------------------
    # 上门（含拒绝入户、设备更换两个分支）
    # ------------------------------------------------------------------
    def record_visit(
        self,
        *,
        hazard_id: str,
        actor: str,
        outcome: str,
        expected_version: int,
        note: str = "",
        new_device: DeviceInfo | None = None,
        inspection_results: Iterable[dict] | None = None,
    ) -> dict:
        """登记上门结果。

        outcome:
        - in_progress：已上门，处理中；
        - rectified：当场整改完成 → 待复核；
        - entry_refused：住户拒绝入户（必须留说明）；
        - device_replaced：现场更换设备（必须留说明并提供新设备信息）。
        """
        try:
            outcome = VisitOutcome(outcome)
        except ValueError:
            raise ValidationError(f"未知上门结果：{outcome}") from None
        with self._lock:
            hz = self._get_or_404(hazard_id)
            self._check_version(hz, expected_version)
            status = HazardStatus(hz["status"])
            if status not in (HazardStatus.DISPATCHED, HazardStatus.ON_SITE):
                raise StateTransitionError(f"当前状态 {status.value} 不允许登记上门结果")
            extra: dict[str, Any] = {}
            detail: dict[str, Any] = {"outcome": outcome.value}
            if outcome == VisitOutcome.ENTRY_REFUSED:
                if not note:
                    raise ValidationError("住户拒绝入户必须填写说明")
                target = HazardStatus.ENTRY_REFUSED
            elif outcome == VisitOutcome.DEVICE_REPLACED:
                if not note:
                    raise ValidationError("设备更换必须填写说明")
                if not isinstance(new_device, DeviceInfo):
                    raise ValidationError("设备更换必须提供新设备信息")
                old = json.loads(hz["device_json"])
                extra["device_json"] = json.dumps(new_device.to_dict(), ensure_ascii=False)
                detail["old_device_id"] = old.get("device_id")
                detail["new_device"] = new_device.to_dict()
                target = HazardStatus.DEVICE_REPLACED
            elif outcome == VisitOutcome.RECTIFIED:
                target = HazardStatus.PENDING_REVIEW
            else:
                target = HazardStatus.ON_SITE
            if inspection_results is not None:
                items = self._merge_results(hz, inspection_results)
                extra["items_json"] = json.dumps(
                    [i.to_dict() for i in items], ensure_ascii=False
                )
            hz = self._transition(
                hz,
                target,
                actor=actor,
                event_type=f"visit_{outcome.value}",
                note=note,
                detail=detail,
                extra=extra,
            )
            return self._manager_view(hz)

    @staticmethod
    def _merge_results(hz: dict, results: Iterable[dict]) -> list[InspectionItem]:
        items = [InspectionItem.from_dict(d) for d in json.loads(hz["items_json"])]
        by_name = {i.name: i for i in items}
        for r in results:
            name = r.get("name")
            if name not in by_name:
                raise ValidationError(f"未知检查项目：{name}")
            result = r.get("result")
            if result not in ("pending", "passed", "failed"):
                raise ValidationError(f"非法检查结果：{result}")
            by_name[name].result = result
            if r.get("note"):
                by_name[name].note = r["note"]
        return items

    # ------------------------------------------------------------------
    # 复核与销项（只有复核通过才能销项）
    # ------------------------------------------------------------------
    def submit_for_review(self, *, hazard_id: str, actor: str, expected_version: int,
                          note: str = "") -> dict:
        """整改/更换完成，提交复核。"""
        with self._lock:
            hz = self._get_or_404(hazard_id)
            self._check_version(hz, expected_version)
            if HazardStatus(hz["status"]) not in (
                HazardStatus.ON_SITE,
                HazardStatus.DEVICE_REPLACED,
            ):
                raise StateTransitionError(f"当前状态 {hz['status']} 不能提交复核")
            hz = self._transition(
                hz,
                HazardStatus.PENDING_REVIEW,
                actor=actor,
                event_type="submitted_for_review",
                note=note or "整改完成，提交复核",
            )
            return self._manager_view(hz)

    def review(
        self,
        *,
        hazard_id: str,
        reviewer: str,
        passed: bool,
        evidence: Iterable[str],
        expected_version: int,
        note: str = "",
    ) -> dict:
        """复核。通过 → 销项；不通过 → 退回重新整改。必须留存证据。"""
        evidence = list(evidence)
        if not evidence:
            raise ValidationError("复核必须留存证据（照片、检测读数等）")
        if not passed and not note:
            raise ValidationError("复核不通过必须填写说明")
        with self._lock:
            hz = self._get_or_404(hazard_id)
            self._check_version(hz, expected_version)
            if HazardStatus(hz["status"]) != HazardStatus.PENDING_REVIEW:
                raise StateTransitionError("只有待复核状态才能复核")
            now = self._now()
            review_id = uuid.uuid4().hex
            self._store.insert_review(
                {
                    "id": review_id,
                    "hazard_id": hazard_id,
                    "reviewer": reviewer,
                    "passed": 1 if passed else 0,
                    "evidence_json": json.dumps(evidence, ensure_ascii=False),
                    "note": note,
                    "created_at": to_iso(now),
                }
            )
            if passed:
                hz = self._transition(
                    hz,
                    HazardStatus.CLOSED,
                    actor=reviewer,
                    event_type="review_passed",
                    note=note or "复核通过，予以销项",
                    detail={"review_id": review_id, "evidence": evidence},
                    extra={"review_verdict": "passed"},
                )
            else:
                hz = self._transition(
                    hz,
                    HazardStatus.DISPATCHED,
                    actor=reviewer,
                    event_type="review_failed",
                    note=note,
                    detail={"review_id": review_id, "evidence": evidence},
                    extra={"review_verdict": "failed"},
                )
            return self._manager_view(hz)

    # ------------------------------------------------------------------
    # 超期分支
    # ------------------------------------------------------------------
    def record_overdue_handling(
        self,
        *,
        hazard_id: str,
        actor: str,
        action: str,
        note: str,
        expected_version: int,
        new_deadline: datetime | None = None,
    ) -> dict:
        """超期处理：extend（延期，须新期限）或 escalate（升级督办），均须留说明。"""
        try:
            action = OverdueAction(action)
        except ValueError:
            raise ValidationError(f"未知超期处理动作：{action}") from None
        if not note:
            raise ValidationError("超期处理必须填写说明")
        with self._lock:
            hz = self._get_or_404(hazard_id)
            self._check_version(hz, expected_version)
            if HazardStatus(hz["status"]) == HazardStatus.CLOSED:
                raise StateTransitionError("隐患已销项，无需超期处理")
            if self._overdue_days(hz) <= 0:
                raise ValidationError("隐患尚未超过整改期限")
            extra: dict[str, Any] = {}
            detail: dict[str, Any] = {"action": action.value}
            if action == OverdueAction.EXTEND:
                if new_deadline is None:
                    raise ValidationError("延期必须提供新的整改期限")
                new_deadline = ensure_aware(new_deadline)
                if new_deadline <= parse_iso(hz["deadline"]):
                    raise ValidationError("新期限必须晚于原期限")
                extra["deadline"] = to_iso(new_deadline)
                extra["branch"] = Branch.NONE.value
                extra["branch_note"] = ""
                detail["new_deadline"] = to_iso(new_deadline)
            else:
                extra["branch"] = Branch.OVERDUE.value
                extra["branch_note"] = note
            now = self._now()
            extra["updated_at"] = to_iso(now)
            if not self._store.update_hazard(hazard_id, hz["version"], extra):
                raise VersionConflictError(f"隐患 {hazard_id} 更新时发生版本冲突")
            self._append_event(
                hazard_id=hazard_id,
                type="overdue_handled",
                actor=actor,
                note=note,
                detail=detail,
                occurred_at=now,
            )
            return self._manager_view(self._get_or_404(hazard_id))

    # ------------------------------------------------------------------
    # 离线补传（不得覆盖已完成结论）
    # ------------------------------------------------------------------
    def apply_offline_event(
        self,
        *,
        event_id: str,
        hazard_id: str,
        event_type: str,
        occurred_at: datetime,
        actor: str,
        note: str = "",
        payload: dict | None = None,
    ) -> dict:
        """离线终端补传事件。

        - 同一 event_id 重复补传：幂等忽略；
        - 隐患已销项：不覆盖已完成结论，仅留痕（applied=False）；
        - 事件进度落后于当前状态：仅留痕；
        - 复核类事件晚于已有复核结论：仅留痕。
        """
        payload = payload or {}
        occurred_at = ensure_aware(occurred_at)
        with self._lock:
            if self._store.get_event_by_id(event_id):
                hz = self._get_or_404(hazard_id)
                return {
                    "applied": False,
                    "reason": "duplicate",
                    "hazard": self._manager_view(hz),
                }
            hz = self._get_or_404(hazard_id)

            def ignore(reason: str) -> dict:
                self._append_event(
                    hazard_id=hazard_id,
                    type=f"offline:{event_type}",
                    actor=actor,
                    note=f"离线补传未生效：{reason}。{note}".rstrip("。"),
                    detail={"payload": payload},
                    source="offline",
                    occurred_at=occurred_at,
                    applied=0,
                    event_id=event_id,
                )
                return {
                    "applied": False,
                    "reason": reason,
                    "hazard": self._manager_view(self._get_or_404(hazard_id)),
                }

            status = HazardStatus(hz["status"])
            if status == HazardStatus.CLOSED:
                return ignore("隐患已销项，补传不得覆盖已完成结论")
            target = _OFFLINE_TARGETS.get(event_type)
            if target is None:
                return ignore(f"未知事件类型：{event_type}")
            if STATUS_ORDER[target] < STATUS_ORDER[status]:
                return ignore("补传事件早于当前处理进度")
            if event_type in _REVIEW_EVENTS:
                # 已有更新的复核结论时，补传不得覆盖
                for r in self._store.list_reviews(hazard_id):
                    if parse_iso(r["created_at"]) >= occurred_at:
                        return ignore("已存在更新的复核结论，补传不得覆盖")
                evidence = payload.get("evidence") or []
                if not evidence:
                    return ignore("复核补传缺少证据")
                if event_type == "review_failed" and not note:
                    return ignore("复核不通过必须填写说明")
                review_id = uuid.uuid4().hex
                self._store.insert_review(
                    {
                        "id": review_id,
                        "hazard_id": hazard_id,
                        "reviewer": actor,
                        "passed": 1 if event_type == "review_passed" else 0,
                        "evidence_json": json.dumps(evidence, ensure_ascii=False),
                        "note": note,
                        "created_at": to_iso(occurred_at),
                    }
                )
                extra = {
                    "review_verdict": "passed"
                    if event_type == "review_passed"
                    else "failed"
                }
                detail = {"review_id": review_id, "evidence": evidence}
            elif event_type == "visit_entry_refused":
                if not note:
                    return ignore("住户拒绝入户必须填写说明")
                extra, detail = {}, {"outcome": "entry_refused"}
            elif event_type == "visit_device_replaced":
                new_device = payload.get("new_device")
                if not note:
                    return ignore("设备更换必须填写说明")
                if not new_device:
                    return ignore("设备更换缺少新设备信息")
                device = DeviceInfo.from_dict(new_device)
                extra = {"device_json": json.dumps(device.to_dict(), ensure_ascii=False)}
                detail = {"outcome": "device_replaced", "new_device": device.to_dict()}
            else:
                extra, detail = {}, {"payload": payload}

            hz = self._transition(
                hz,
                target,
                actor=actor,
                event_type=f"offline:{event_type}",
                note=note,
                detail=detail,
                extra=extra,
                source="offline",
                occurred_at=occurred_at,
                event_id=event_id,
            )
            return {"applied": True, "reason": None, "hazard": self._manager_view(hz)}

    # ------------------------------------------------------------------
    # 查询：网格员按楼栋脱敏视图，管理者全量追踪视图
    # ------------------------------------------------------------------
    def list_hazards_for_worker(self, worker_id: str) -> list[dict]:
        """网格员：仅所辖楼栋的必要信息；管理者：全部隐患的完整视图。"""
        worker = self._worker_or_404(worker_id)
        with self._lock:
            rows = self._store.list_hazards()
            if worker["role"] == ROLE_MANAGER:
                return [self._manager_view(r) for r in rows]
            buildings = set(json.loads(worker["buildings_json"]))
            return [
                self._grid_view(r) for r in rows if r["building"] in buildings
            ]

    def get_hazard_for_worker(self, worker_id: str, hazard_id: str) -> dict:
        worker = self._worker_or_404(worker_id)
        with self._lock:
            hz = self._get_or_404(hazard_id)
            if worker["role"] == ROLE_MANAGER:
                return self._manager_view(hz)
            if hz["building"] not in json.loads(worker["buildings_json"]):
                raise AccessDeniedError("该隐患不在所辖楼栋范围内")
            return self._grid_view(hz)

    def get_hazard_for_manager(self, manager_id: str, hazard_id: str) -> dict:
        """管理者追踪视图：责任人、逾期天数、每次复核证据、完整留痕。"""
        worker = self._worker_or_404(manager_id)
        if worker["role"] != ROLE_MANAGER:
            raise AccessDeniedError("仅管理者可查看完整档案")
        with self._lock:
            return self._manager_view(self._get_or_404(hazard_id))

    def _worker_or_404(self, worker_id: str) -> dict:
        worker = self._store.get_worker(worker_id)
        if worker is None:
            raise NotFoundError(f"人员未登记：{worker_id}")
        return worker

    # ------------------------------------------------------------------
    # 视图组装
    # ------------------------------------------------------------------
    def _grid_view(self, hz: dict) -> dict:
        """网格员必要信息视图：不含住户姓名、电话、授权细节与复核证据。"""
        auth = json.loads(hz["authorization_json"])
        device = json.loads(hz["device_json"])
        items = json.loads(hz["items_json"])
        return {
            "hazard_id": hz["id"],
            "building": hz["building"],
            "unit": hz["unit"],
            "room": hz["room"],
            "status": hz["status"],
            "branch": hz["branch"],
            "branch_note": hz["branch_note"],
            "deadline": hz["deadline"],
            "overdue_days": self._overdue_days(hz),
            "device": {
                "device_id": device["device_id"],
                "kind": device["kind"],
                "online": device["online"],
                "fault": device["fault"],
            },
            "inspection_items": [
                {"name": i["name"], "required": i["required"], "result": i["result"]}
                for i in items
            ],
            "entry_allowed": auth["entry_allowed"],
            "device_replace_allowed": auth["device_replace_allowed"],
            "assignee": hz["assignee"],
            "version": hz["version"],
        }

    def _manager_view(self, hz: dict) -> dict:
        """管理者完整视图：含责任人、逾期天数、每次复核证据与全部留痕。"""
        view = self._grid_view(hz)
        view.update(
            {
                "household_name": hz["household_name"],
                "household_phone": hz["household_phone"],
                "authorization": json.loads(hz["authorization_json"]),
                "device": json.loads(hz["device_json"]),
                "inspection_items": json.loads(hz["items_json"]),
                "review_verdict": hz["review_verdict"],
                "discovered_by": hz["discovered_by"],
                "note": hz["note"],
                "created_at": hz["created_at"],
                "updated_at": hz["updated_at"],
                "closed_at": hz["closed_at"],
                "reviews": [
                    {
                        "review_id": r["id"],
                        "reviewer": r["reviewer"],
                        "passed": bool(r["passed"]),
                        "evidence": json.loads(r["evidence_json"]),
                        "note": r["note"],
                        "created_at": r["created_at"],
                    }
                    for r in self._store.list_reviews(hz["id"])
                ],
                "events": [
                    {
                        "type": e["type"],
                        "actor": e["actor"],
                        "note": e["note"],
                        "source": e["source"],
                        "applied": bool(e["applied"]),
                        "occurred_at": e["occurred_at"],
                        "recorded_at": e["recorded_at"],
                        "detail": json.loads(e["detail_json"]),
                    }
                    for e in self._store.list_events(hz["id"])
                ],
            }
        )
        return view


# 兼容原有占位入口
Service = GasHazardService
