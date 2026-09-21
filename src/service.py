"""燃气隐患闭环领域服务。

主流程：发现 → 派发 → 上门 → 复核 → 销项。
异常分支：拒绝入户 / 等待设备更换 / 逾期，均强制留说明，可经重新派发回到原流程。
并发：所有写操作携带 expected_version 做乐观锁检测。
离线：补传只追加，不得覆盖已完成（复核通过/销项）的结论。
权限：网格员只见所辖楼栋的必要信息；管理者可见责任人、逾期天数与全部复核证据。
"""

from __future__ import annotations

import functools
import threading
from dataclasses import replace
from datetime import date, datetime
from typing import Any, Callable, Optional

from .errors import (
    CompletedConclusionError,
    DuplicateDispatch,
    NotFoundError,
    PermissionDenied,
    StateError,
    ValidationError,
    VersionConflict,
)
from .models import (
    ALLOWED_TRANSITIONS,
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
    dataclass_to_dict,
)
from .storage import (
    JsonStore,
    device_from_dict,
    hazard_from_dict,
    resident_from_dict,
    user_from_dict,
)

# 上门默认检查项目（发现时可增删）
DEFAULT_CHECK_ITEMS = (
    "报警器通电与联网状态",
    "报警器声光报警功能",
    "电磁阀切断联动",
    "燃气管路与接口检漏",
)


def _writes(fn):
    """写操作串行化：保证“校验版本 → 落库”区间内的乐观锁判定可靠。"""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._write_lock:
            return fn(self, *args, **kwargs)

    return wrapper


class ClosureService:
    def __init__(self, store: JsonStore, clock: Optional[Callable[[], datetime]] = None):
        self.store = store
        self.now = clock or datetime.now
        # 可重入：派发包装等方法会在锁内继续调用其它写方法
        self._write_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 装载与持久化
    # ------------------------------------------------------------------
    def _user(self, user_id: str) -> User:
        raw = self.store.data["users"].get(user_id)
        if raw is None:
            raise NotFoundError(f"人员不存在：{user_id}")
        return user_from_dict(raw)

    def _resident(self, resident_id: str) -> Resident:
        raw = self.store.data["residents"].get(resident_id)
        if raw is None:
            raise NotFoundError(f"住户不存在：{resident_id}")
        return resident_from_dict(raw)

    def _device(self, device_id: str) -> Device:
        raw = self.store.data["devices"].get(device_id)
        if raw is None:
            raise NotFoundError(f"设备不存在：{device_id}")
        return device_from_dict(raw)

    def _hazard(self, hazard_id: str) -> Hazard:
        raw = self.store.data["hazards"].get(hazard_id)
        if raw is None:
            raise NotFoundError(f"隐患不存在：{hazard_id}")
        return hazard_from_dict(raw)

    def _save_device(self, device: Device) -> None:
        self.store.data["devices"][device.id] = dataclass_to_dict(device)

    def _save_hazard(self, hazard: Hazard) -> None:
        """所有修改统一在此落库：版本号 +1 并原子写盘。"""
        hazard.version += 1
        self.store.data["hazards"][hazard.id] = dataclass_to_dict(hazard)
        self.store.save()

    def _event(self, hazard: Hazard, etype: EventType, actor: str,
               **detail: Any) -> None:
        hazard.events.append(Event(
            seq=len(hazard.events) + 1,
            type=etype,
            actor=actor,
            at=self.now(),
            detail=detail,
        ))

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------
    def _require_roles(self, user: User, roles: tuple[Role, ...]) -> None:
        if user.role not in roles:
            raise PermissionDenied(f"角色 {user.role.value} 无权执行该操作")

    def _require_building(self, user: User, building: str) -> None:
        if user.role == Role.MANAGER:
            return
        if user.role == Role.GRID_WORKER:
            if building not in user.buildings:
                raise PermissionDenied(
                    f"网格员 {user.name} 不管辖楼栋 {building}"
                )
            return
        # 维修/安全员的访问范围由调用方按责任人等条件约束
        raise PermissionDenied(f"角色 {user.role.value} 无权访问该楼栋数据")

    def _check_version(self, hazard: Hazard, expected_version: int) -> None:
        if expected_version != hazard.version:
            raise VersionConflict(hazard.id, expected_version, hazard.version)

    def _transition(self, hazard: Hazard, target: HazardState) -> None:
        if target not in ALLOWED_TRANSITIONS[hazard.state]:
            raise StateError(
                f"隐患 {hazard.id} 当前状态 {hazard.state.value}，"
                f"不能流转到 {target.value}"
            )
        hazard.state = target

    # ------------------------------------------------------------------
    # 基础登记：人员 / 住户授权 / 设备
    # ------------------------------------------------------------------
    @_writes
    def register_user(self, name: str, role: Role | str,
                      buildings: Optional[list[str]] = None,
                      user_id: Optional[str] = None) -> User:
        role = Role(role)
        uid = user_id or self.store.next_id("users", "U")
        if role == Role.GRID_WORKER and not buildings:
            raise ValidationError("网格员必须登记所辖楼栋")
        user = User(id=uid, name=name, role=role, buildings=list(buildings or []))
        self.store.data["users"][uid] = dataclass_to_dict(user)
        self.store.save()
        return user

    @_writes
    def register_resident(self, name: str, building: str, room: str,
                          authorization: AuthorizationScope | str,
                          contact: str = "",
                          resident_id: Optional[str] = None) -> Resident:
        rid = resident_id or self.store.next_id("residents", "R")
        resident = Resident(
            id=rid, name=name, building=building, room=room,
            authorization=AuthorizationScope(authorization), contact=contact,
        )
        self.store.data["residents"][rid] = dataclass_to_dict(resident)
        self.store.save()
        return resident

    @_writes
    def update_authorization(self, actor_id: str, resident_id: str,
                             scope: AuthorizationScope | str,
                             note: str = "") -> Resident:
        """住户协调后变更授权范围（如从拒绝入户改为允许检查），需留说明。"""
        actor = self._user(actor_id)
        self._require_roles(actor, (Role.GRID_WORKER, Role.MANAGER))
        resident = self._resident(resident_id)
        self._require_building(actor, resident.building)
        if not note.strip():
            raise ValidationError("授权范围变更必须填写情况说明")
        resident.authorization = AuthorizationScope(scope)
        self.store.data["residents"][resident.id] = dataclass_to_dict(resident)
        self.store.save()
        return resident

    @_writes
    def register_device(self, resident_id: str,
                        status: DeviceStatus | str = DeviceStatus.OFFLINE,
                        device_id: Optional[str] = None) -> Device:
        resident = self._resident(resident_id)
        did = device_id or self.store.next_id("devices", "D")
        device = Device(
            id=did, resident_id=resident_id, building=resident.building,
            status=DeviceStatus(status),
        )
        self._save_device(device)
        self.store.save()
        return device

    @_writes
    def add_device_check(self, actor_id: str, device_id: str, item: str,
                         result: CheckResult | str, record_ref: str = "",
                         note: str = "") -> CheckRecord:
        actor = self._user(actor_id)
        device = self._device(device_id)
        if actor.role in (Role.MANAGER, Role.GRID_WORKER):
            self._require_building(actor, device.building)
        elif actor.role not in (Role.REPAIRER, Role.SAFETY_OFFICER):
            raise PermissionDenied(f"角色 {actor.role.value} 无权登记检测")
        record = CheckRecord(
            at=self.now(), item=item, result=CheckResult(result),
            checker=actor.name, record_ref=record_ref, note=note,
        )
        device.check_history.append(record)
        self._save_device(device)
        self.store.save()
        return record

    # ------------------------------------------------------------------
    # 主流程第一步：发现
    # ------------------------------------------------------------------
    @_writes
    def discover(self, actor_id: str, device_id: str, title: str,
                 rectify_deadline: date | str,
                 description: str = "",
                 check_items: Optional[list[str]] = None) -> dict[str, Any]:
        actor = self._user(actor_id)
        self._require_roles(
            actor, (Role.SAFETY_OFFICER, Role.GRID_WORKER, Role.MANAGER)
        )
        device = self._device(device_id)
        resident = self._resident(device.resident_id)
        if actor.role != Role.SAFETY_OFFICER:
            self._require_building(actor, device.building)
        if not title.strip():
            raise ValidationError("隐患标题不能为空")
        try:
            deadline = (rectify_deadline if isinstance(rectify_deadline, date)
                        else date.fromisoformat(rectify_deadline))
        except (TypeError, ValueError) as exc:
            raise ValidationError("整改期限格式应为 YYYY-MM-DD") from exc

        hid = self.store.next_id("hazards", "H")
        names = list(check_items) if check_items else list(DEFAULT_CHECK_ITEMS)
        hazard = Hazard(
            id=hid, resident_id=resident.id, building=resident.building,
            device_id=device.id, title=title, description=description,
            discovered_by=actor.name, rectify_deadline=deadline,
            check_items=[CheckItem(name=n) for n in names],
        )
        # 发现离线设备时同步置为离线
        if device.status == DeviceStatus.ONLINE:
            device.status = DeviceStatus.OFFLINE
            self._save_device(device)
        self._event(hazard, EventType.DISCOVER, actor.name,
                    device_id=device.id, deadline=str(deadline))
        self._save_hazard(hazard)
        return self.get_hazard(actor_id, hid)

    # ------------------------------------------------------------------
    # 派发（含重复派发 → 返回原流程；异常分支 → 重新派发）
    # ------------------------------------------------------------------
    @_writes
    def dispatch(self, actor_id: str, hazard_id: str, repairer_id: str,
                 expected_version: int, note: str = "") -> dict[str, Any]:
        actor = self._user(actor_id)
        self._require_roles(actor, (Role.GRID_WORKER, Role.MANAGER))
        hazard = self._hazard(hazard_id)
        self._require_building(actor, hazard.building)
        repairer = self._user(repairer_id)
        if repairer.role != Role.REPAIRER:
            raise ValidationError(f"{repairer.name} 不是维修人员")

        # 已在处置流程中：重复派发不另起新单，直接返回原流程
        if hazard.state not in (
            HazardState.DISCOVERED,
            HazardState.ACCESS_DENIED,
            HazardState.REPLACEMENT_PENDING,
        ):
            raise DuplicateDispatch(hazard.id, hazard.owner)

        self._check_version(hazard, expected_version)
        if hazard.overdue_flagged and not note.strip():
            raise ValidationError("逾期隐患重新处置必须填写情况说明")
        from_state = hazard.state
        is_redispatch = from_state in (
            HazardState.ACCESS_DENIED, HazardState.REPLACEMENT_PENDING
        )
        self._transition(hazard, HazardState.DISPATCHED)
        hazard.owner = repairer.name
        if hazard.first_dispatched_at is None:
            hazard.first_dispatched_at = self.now()
        self._event(
            hazard,
            EventType.RE_DISPATCH if is_redispatch else EventType.DISPATCH,
            actor.name, repairer=repairer.name, note=note,
            previous_state=from_state.value if is_redispatch else None,
        )
        self._save_hazard(hazard)
        return self.get_hazard(actor_id, hazard_id)

    def dispatch_or_return(self, actor_id: str, hazard_id: str,
                           repairer_id: str, expected_version: int,
                           note: str = "") -> dict[str, Any]:
        """派发；若该隐患已在处置流程中，则不另起新单，直接返回原流程单据。

        返回视图中 ``returned_original_flow=True`` 表示命中重复派发、
        返回的是既有流程；False 表示本次确实完成了派发（含异常分支回归）。
        """
        try:
            view = self.dispatch(actor_id, hazard_id, repairer_id,
                                 expected_version, note)
            view["returned_original_flow"] = False
            return view
        except DuplicateDispatch:
            view = self.get_hazard(actor_id, hazard_id)
            view["returned_original_flow"] = True
            return view

    # ------------------------------------------------------------------
    # 异常分支一：住户拒绝入户（必须留说明）
    # ------------------------------------------------------------------
    @_writes
    def report_access_denied(self, actor_id: str, hazard_id: str,
                             expected_version: int, reason: str) -> dict[str, Any]:
        actor = self._user(actor_id)
        hazard = self._hazard(hazard_id)
        self._assert_field_actor(actor, hazard)
        if not reason or not reason.strip():
            raise ValidationError("住户拒绝入户必须填写情况说明")
        self._check_version(hazard, expected_version)
        resident = self._resident(hazard.resident_id)
        self._transition(hazard, HazardState.ACCESS_DENIED)
        hazard.branch_note = reason.strip()
        self._event(hazard, EventType.ACCESS_DENIED, actor.name,
                    reason=reason.strip(),
                    authorization=resident.authorization.value)
        self._save_hazard(hazard)
        return self.get_hazard(actor_id, hazard_id)

    # ------------------------------------------------------------------
    # 异常分支二：设备更换（必须留说明；完成更换需住户授权）
    # ------------------------------------------------------------------
    @_writes
    def report_replacement_needed(self, actor_id: str, hazard_id: str,
                                  expected_version: int,
                                  reason: str) -> dict[str, Any]:
        actor = self._user(actor_id)
        hazard = self._hazard(hazard_id)
        self._assert_field_actor(actor, hazard)
        if not reason or not reason.strip():
            raise ValidationError("设备更换必须填写情况说明")
        self._check_version(hazard, expected_version)
        device = self._device(hazard.device_id)
        self._transition(hazard, HazardState.REPLACEMENT_PENDING)
        hazard.branch_note = reason.strip()
        device.status = DeviceStatus.REPLACEMENT_PENDING
        self._save_device(device)
        self._event(hazard, EventType.DEVICE_REPLACEMENT, actor.name,
                    phase="needed", reason=reason.strip(),
                    device_id=device.id)
        self._save_hazard(hazard)
        return self.get_hazard(actor_id, hazard_id)

    @_writes
    def complete_replacement(self, actor_id: str, hazard_id: str,
                             expected_version: int,
                             note: str = "") -> dict[str, Any]:
        actor = self._user(actor_id)
        self._require_roles(actor, (Role.REPAIRER, Role.GRID_WORKER, Role.MANAGER))
        hazard = self._hazard(hazard_id)
        if actor.role == Role.GRID_WORKER:
            self._require_building(actor, hazard.building)
        elif actor.role == Role.REPAIRER and hazard.owner != actor.name:
            raise PermissionDenied(
                f"隐患 {hazard.id} 当前责任人是 {hazard.owner or '未派发'}"
            )
        resident = self._resident(hazard.resident_id)
        if resident.authorization != AuthorizationScope.DEVICE_REPLACEMENT:
            raise PermissionDenied(
                f"住户授权范围为 {resident.authorization.value}，不含设备更换"
            )
        self._check_version(hazard, expected_version)
        if hazard.state != HazardState.REPLACEMENT_PENDING:
            raise StateError(
                f"隐患 {hazard.id} 当前为 {hazard.state.value}，"
                "只有“等待设备更换”分支可以完成更换"
            )

        old_device = self._device(hazard.device_id)
        old_device.status = DeviceStatus.REPLACED
        self._save_device(old_device)
        new_device = self.register_device(resident.id, DeviceStatus.ONLINE)
        new_device.check_history.append(CheckRecord(
            at=self.now(), item="新装报警器开通检测", result=CheckResult.PASS,
            checker=actor.name, note="设备更换后首次检测",
        ))
        self._save_device(new_device)

        self._transition(hazard, HazardState.DISPATCHED)
        hazard.device_id = new_device.id
        self._event(hazard, EventType.RE_DISPATCH, actor.name,
                    old_device_id=old_device.id, new_device_id=new_device.id,
                    note=note, previous_state=HazardState.REPLACEMENT_PENDING.value)
        self._save_hazard(hazard)
        return self.get_hazard(actor_id, hazard_id)

    # ------------------------------------------------------------------
    # 上门：逐项登记检查结果，同时写入设备检测记录
    # ------------------------------------------------------------------
    @_writes
    def arrive_onsite(self, actor_id: str, hazard_id: str,
                      expected_version: int,
                      check_results: dict[str, str],
                      note: str = "") -> dict[str, Any]:
        actor = self._user(actor_id)
        hazard = self._hazard(hazard_id)
        self._assert_field_actor(actor, hazard)
        resident = self._resident(hazard.resident_id)
        if resident.authorization == AuthorizationScope.NONE:
            raise StateError("住户未授权入户，应走“拒绝入户”分支并留说明")
        self._check_version(hazard, expected_version)
        self._apply_onsite(hazard, actor, check_results, note, self.now())
        self._save_hazard(hazard)
        return self.get_hazard(actor_id, hazard_id)

    def _apply_onsite(self, hazard: Hazard, actor: User,
                      check_results: dict[str, str], note: str,
                      recorded_at: datetime) -> None:
        if not check_results:
            raise ValidationError("至少登记一项检查结果")
        unknown = set(check_results) - {i.name for i in hazard.check_items}
        if unknown:
            raise ValidationError(f"检查项目未登记：{sorted(unknown)}")
        device = self._device(hazard.device_id)
        for item in hazard.check_items:
            raw = check_results.get(item.name)
            if raw is None:
                raise ValidationError(f"检查项缺少结果：{item.name}")
            result = CheckResult(raw)
            item.result = result
            item.checked_by = actor.name
            item.checked_at = recorded_at
            device.check_history.append(CheckRecord(
                at=recorded_at, item=item.name, result=result,
                checker=actor.name, note=note,
            ))
        if device.status != DeviceStatus.REPLACED:
            device.status = DeviceStatus.ONLINE
        self._save_device(device)
        self._transition(hazard, HazardState.ONSITE)
        self._event(hazard, EventType.ONSITE, actor.name, note=note,
                    results={k: v for k, v in check_results.items()})

    # ------------------------------------------------------------------
    # 复核：通过/不通过都要留证据；不通过回到待整改复核
    # ------------------------------------------------------------------
    @_writes
    def review(self, actor_id: str, hazard_id: str, expected_version: int,
               passed: bool, evidence_refs: list[str],
               note: str = "") -> dict[str, Any]:
        actor = self._user(actor_id)
        self._require_roles(actor, (Role.GRID_WORKER, Role.MANAGER))
        hazard = self._hazard(hazard_id)
        self._require_building(actor, hazard.building)
        if not evidence_refs or any(not str(e).strip() for e in evidence_refs):
            raise ValidationError("复核必须上传证据（照片/报告/签名单据编号）")
        if not passed and not note.strip():
            raise ValidationError("复核不通过必须填写整改说明")
        self._check_version(hazard, expected_version)
        if hazard.state not in (HazardState.ONSITE, HazardState.RE_REVIEW_PENDING):
            raise StateError(
                f"隐患 {hazard.id} 当前为 {hazard.state.value}，还不能复核"
            )

        evidence = ReviewEvidence(
            reviewer=actor.name,
            passed=bool(passed),
            at=self.now(),
            items=[replace(i) for i in hazard.check_items],
            evidence_refs=list(evidence_refs),
            note=note.strip(),
        )
        hazard.reviews.append(evidence)
        if passed:
            self._transition(hazard, HazardState.RESOLVED)
            hazard.resolved_version = hazard.version + 1
        else:
            self._transition(hazard, HazardState.RE_REVIEW_PENDING)
        self._event(hazard, EventType.REVIEW, actor.name,
                    passed=bool(passed), evidence_refs=list(evidence_refs),
                    note=note.strip())
        self._save_hazard(hazard)
        return self.get_hazard(actor_id, hazard_id)

    # ------------------------------------------------------------------
    # 销项：只有复核通过才能销项
    # ------------------------------------------------------------------
    @_writes
    def close(self, actor_id: str, hazard_id: str,
              expected_version: int, note: str = "") -> dict[str, Any]:
        actor = self._user(actor_id)
        self._require_roles(actor, (Role.GRID_WORKER, Role.MANAGER))
        hazard = self._hazard(hazard_id)
        self._require_building(actor, hazard.building)
        self._check_version(hazard, expected_version)
        if hazard.state != HazardState.RESOLVED:
            raise StateError("只有复核通过的隐患才能销项")
        if hazard.last_review_passed() is None:
            raise StateError("缺少通过的复核记录，不得销项")
        self._transition(hazard, HazardState.CLOSED)
        hazard.closed_at = self.now()
        self._event(hazard, EventType.CLOSE, actor.name, note=note,
                    review_at=hazard.last_review_passed().at.isoformat())
        self._save_hazard(hazard)
        return self.get_hazard(actor_id, hazard_id)

    # ------------------------------------------------------------------
    # 离线终端补传：只追加，不覆盖已完成结论
    # ------------------------------------------------------------------
    @_writes
    def offline_sync_onsite(self, terminal_id: str, actor_id: str,
                            hazard_id: str, base_version: int,
                            check_results: dict[str, str],
                            recorded_at: datetime | str,
                            note: str = "") -> dict[str, Any]:
        actor = self._user(actor_id)
        hazard = self._hazard(hazard_id)
        # 补传同样受责任人/管辖范围约束
        if actor.role == Role.REPAIRER:
            if hazard.owner != actor.name:
                raise PermissionDenied(
                    f"隐患 {hazard.id} 当前责任人是 {hazard.owner or '未派发'}"
                )
        elif actor.role in (Role.GRID_WORKER, Role.MANAGER):
            self._require_building(actor, hazard.building)
        else:
            raise PermissionDenied(f"角色 {actor.role.value} 无权限补传")
        if isinstance(recorded_at, str):
            recorded_at = datetime.fromisoformat(recorded_at)

        # 已形成完成结论（复核通过/已销项）且终端基线早于结论：禁止覆盖
        if hazard.state in (HazardState.RESOLVED, HazardState.CLOSED) or (
            hazard.resolved_version is not None
            and base_version < hazard.resolved_version
        ):
            raise CompletedConclusionError(
                f"隐患 {hazard.id} 已完成（{hazard.state.value}），"
                f"离线补传 v{base_version} 不得覆盖既有结论"
            )
        if base_version > hazard.version:
            raise ValidationError(
                f"补传基线 v{base_version} 高于服务端版本 v{hazard.version}"
            )

        applied = False
        # 仅当终端基线与服务端一致、且主流程仍停在派发时，补传才直接驱动到
        # “已上门”；基线落后（离线期间已重新派发/打逾期标记）则只留痕，
        # 不凭过期基线推动状态。
        if hazard.state == HazardState.DISPATCHED \
                and base_version == hazard.version:
            resident = self._resident(hazard.resident_id)
            if resident.authorization == AuthorizationScope.NONE:
                raise StateError("住户未授权入户，应走“拒绝入户”分支")
            self._apply_onsite(hazard, actor, check_results, note, recorded_at)
            applied = True
        # 其余在办状态（基线落后/已上门/待复核/异常分支）：不回退流程，
        # 仅把终端采集内容作为补传事件留痕，保留服务端已有结论。
        self._event(hazard, EventType.OFFLINE_SYNC, actor.name,
                    terminal_id=terminal_id, base_version=base_version,
                    server_version=hazard.version, applied=applied,
                    recorded_at=recorded_at.isoformat(),
                    results=dict(check_results), note=note)
        self._save_hazard(hazard)
        result = self.get_hazard(actor_id, hazard_id)
        result["offline_applied"] = applied
        return result

    # ------------------------------------------------------------------
    # 逾期：按日期实时计算；标记为独立分支事件，重启后继续计时
    # ------------------------------------------------------------------
    @_writes
    def refresh_overdue_flags(self, today: Optional[date] = None) -> list[str]:
        today = today or self.now().date()
        flagged: list[str] = []
        for hid, raw in self.store.data["hazards"].items():
            hazard = hazard_from_dict(raw)
            if hazard.state in (HazardState.RESOLVED, HazardState.CLOSED):
                continue
            if hazard.overdue_flagged:
                continue
            if hazard.days_overdue(today) > 0:
                hazard.overdue_flagged = True
                self._event(hazard, EventType.OVERDUE_FLAG, "SYSTEM",
                            deadline=str(hazard.rectify_deadline),
                            days_overdue=hazard.days_overdue(today))
                self._save_hazard(hazard)
                flagged.append(hid)
        return flagged

    # ------------------------------------------------------------------
    # 查询与视图（RBAC：网格员只见所辖楼栋必要信息）
    # ------------------------------------------------------------------
    def get_device(self, actor_id: str, device_id: str) -> dict[str, Any]:
        actor = self._user(actor_id)
        device = self._device(device_id)
        if actor.role in (Role.GRID_WORKER,):
            self._require_building(actor, device.building)
        last = device.last_check_record
        return {
            "id": device.id,
            "building": device.building,
            "status": device.status.value,
            "last_check_at": device.last_check_at.isoformat()
            if device.last_check_at else None,
            "last_check_item": last.item if last else None,
            "last_check_record_ref": last.record_ref if last else None,
            "history_count": len(device.check_history),
        }

    def get_hazard(self, actor_id: str, hazard_id: str) -> dict[str, Any]:
        actor = self._user(actor_id)
        hazard = self._hazard(hazard_id)
        return self._project(hazard, actor)

    def list_hazards(self, actor_id: str,
                     state: Optional[HazardState | str] = None) -> list[dict[str, Any]]:
        actor = self._user(actor_id)
        wanted = HazardState(state) if state else None
        out: list[dict[str, Any]] = []
        for hid in sorted(self.store.data["hazards"]):
            hazard = self._hazard(hid)
            if wanted and hazard.state != wanted:
                continue
            if actor.role == Role.GRID_WORKER:
                if hazard.building not in actor.buildings:
                    continue
            elif actor.role == Role.REPAIRER:
                if hazard.owner != actor.name:
                    continue
            # 安全员可浏览本小区发现记录；管理者看全部
            out.append(self._project(hazard, actor))
        return out

    def manager_tracking(self, actor_id: str) -> list[dict[str, Any]]:
        """管理者视角：责任人、逾期天数、每次复核证据、完整流转痕迹。"""
        actor = self._user(actor_id)
        self._require_roles(actor, (Role.MANAGER,))
        rows: list[dict[str, Any]] = []
        for hid in sorted(self.store.data["hazards"]):
            hazard = self._hazard(hid)
            rows.append({
                "id": hazard.id,
                "building": hazard.building,
                "title": hazard.title,
                "state": hazard.state.value,
                "discovered_by": hazard.discovered_by,
                "owner": hazard.owner,
                "rectify_deadline": str(hazard.rectify_deadline),
                "days_overdue": hazard.days_overdue(self.now().date()),
                "overdue_flagged": hazard.overdue_flagged,
                "branch_note": hazard.branch_note,
                "reviews": [
                    {
                        "reviewer": r.reviewer,
                        "passed": r.passed,
                        "at": r.at.isoformat(),
                        "evidence_refs": r.evidence_refs,
                        "note": r.note,
                    }
                    for r in hazard.reviews
                ],
                "timeline": [
                    {"seq": e.seq, "type": e.type.value, "actor": e.actor,
                     "at": e.at.isoformat(), "detail": e.detail}
                    for e in hazard.events
                ],
                "closed_at": hazard.closed_at.isoformat()
                if hazard.closed_at else None,
                "version": hazard.version,
            })
        return rows

    # ------------------------------------------------------------------
    # 视图投影
    # ------------------------------------------------------------------
    def _assert_field_actor(self, actor: User, hazard: Hazard) -> None:
        """上门类操作的角色/范围校验。"""
        if actor.role == Role.REPAIRER:
            if hazard.owner != actor.name:
                raise PermissionDenied(
                    f"隐患 {hazard.id} 当前责任人是 {hazard.owner or '未派发'}"
                )
        elif actor.role in (Role.GRID_WORKER, Role.MANAGER):
            self._require_building(actor, hazard.building)
        else:
            raise PermissionDenied(f"角色 {actor.role.value} 无权现场处置")

    def _project(self, hazard: Hazard, actor: User) -> dict[str, Any]:
        resident = self._resident(hazard.resident_id)
        device = self._device(hazard.device_id)

        if actor.role == Role.GRID_WORKER:
            self._require_building(actor, hazard.building)
            return self._grid_view(hazard, resident, device)
        if actor.role == Role.REPAIRER:
            if hazard.owner != actor.name:
                raise PermissionDenied("只能查看派发给本人的隐患")
            return self._field_view(hazard, resident, device)
        if actor.role == Role.MANAGER:
            return self._manager_view(hazard, resident, device)
        # 安全员：最小必要信息
        return {
            "id": hazard.id,
            "building": hazard.building,
            "state": hazard.state.value,
            "title": hazard.title,
            "discovered_by": hazard.discovered_by,
            "version": hazard.version,
        }

    def _base_view(self, hazard: Hazard) -> dict[str, Any]:
        last_pass = hazard.last_review_passed()
        return {
            "id": hazard.id,
            "building": hazard.building,
            "room": None,  # 由调用方填充
            "device_id": hazard.device_id,
            "title": hazard.title,
            "state": hazard.state.value,
            "owner": hazard.owner,
            "found_at": hazard.found_at.isoformat(),
            "rectify_deadline": str(hazard.rectify_deadline),
            "days_overdue": hazard.days_overdue(self.now().date()),
            "overdue_flagged": hazard.overdue_flagged,
            "branch_note": hazard.branch_note,
            "check_items": [
                {"name": i.name, "result": i.result.value,
                 "checked_by": i.checked_by}
                for i in hazard.check_items
            ],
            "last_review_passed_at": last_pass.at.isoformat()
            if last_pass else None,
            "closed_at": hazard.closed_at.isoformat()
            if hazard.closed_at else None,
            "version": hazard.version,
        }

    def _grid_view(self, hazard: Hazard, resident: Resident,
                   device: Device) -> dict[str, Any]:
        """网格员：所辖楼栋 + 必要字段，隐去联系方式与复核凭证细节。"""
        view = self._base_view(hazard)
        view["room"] = resident.room
        view["resident_name"] = resident.name
        view["device_status"] = device.status.value
        review = hazard.reviews[-1] if hazard.reviews else None
        view["last_review"] = (
            {"passed": review.passed, "at": review.at.isoformat(),
             "note": review.note} if review else None
        )
        return view

    def _field_view(self, hazard: Hazard, resident: Resident,
                    device: Device) -> dict[str, Any]:
        """维修人员：本人单据，需要联系方式与授权范围以便作业。"""
        view = self._base_view(hazard)
        view["room"] = resident.room
        view["resident_name"] = resident.name
        view["resident_contact"] = resident.contact
        view["authorization"] = resident.authorization.value
        view["device_status"] = device.status.value
        return view

    def _manager_view(self, hazard: Hazard, resident: Resident,
                      device: Device) -> dict[str, Any]:
        view = self._base_view(hazard)
        view["room"] = resident.room
        view["resident_name"] = resident.name
        view["resident_contact"] = resident.contact
        view["authorization"] = resident.authorization.value
        view["description"] = hazard.description
        view["discovered_by"] = hazard.discovered_by
        view["device_status"] = device.status.value
        view["reviews"] = [
            {"reviewer": r.reviewer, "passed": r.passed,
             "at": r.at.isoformat(), "evidence_refs": r.evidence_refs,
             "note": r.note}
            for r in hazard.reviews
        ]
        view["timeline"] = [
            {"seq": e.seq, "type": e.type.value, "actor": e.actor,
             "at": e.at.isoformat(), "detail": e.detail}
            for e in hazard.events
        ]
        return view


# 向后兼容旧入口名
Service = ClosureService
