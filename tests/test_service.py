"""燃气隐患闭环服务测试。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from src.errors import (
    CompletedConclusionError,
    PermissionDenied,
    StateError,
    ValidationError,
    VersionConflict,
)
from src.models import (
    AuthorizationScope,
    CheckResult,
    DeviceStatus,
    HazardState,
    Role,
)
from src.service import ClosureService
from src.storage import JsonStore


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "data.json")
        self.current = datetime(2026, 9, 1, 9, 0, 0)
        self.svc = ClosureService(
            JsonStore(self.path), clock=lambda: self.current
        )
        self.officer = self.svc.register_user("安全员", Role.SAFETY_OFFICER)
        self.grid = self.svc.register_user(
            "网格员甲", Role.GRID_WORKER, buildings=["1栋", "2栋"]
        )
        self.grid_other = self.svc.register_user(
            "网格员乙", Role.GRID_WORKER, buildings=["9栋"]
        )
        self.manager = self.svc.register_user("主任", Role.MANAGER)
        self.rep = self.svc.register_user("维修甲", Role.REPAIRER)
        self.rep2 = self.svc.register_user("维修乙", Role.REPAIRER)

    def fresh_service(self) -> ClosureService:
        """模拟服务重启：从同一数据文件重建。"""
        return ClosureService(JsonStore(self.path),
                              clock=lambda: self.current)

    def make_hazard(self, scope=AuthorizationScope.INSPECTION_AND_REPAIR,
                    deadline: date | None = None,
                    building="1栋", room="101"):
        resident = self.svc.register_resident(
            "住户", building, room, scope, contact="13800000000"
        )
        device = self.svc.register_device(resident.id, DeviceStatus.OFFLINE)
        hazard = self.svc.discover(
            self.officer.id, device.id, "报警器离线",
            rectify_deadline=deadline or date(2026, 9, 10),
        )
        return resident, device, hazard

    def all_pass(self, hazard) -> dict[str, str]:
        # 安全员视图刻意最小化、不含检查项；需要检查项时以管理者视角读取
        if "check_items" not in hazard:
            hazard = self.svc.get_hazard(self.manager.id, hazard["id"])
        return {item["name"]: CheckResult.PASS
                for item in hazard["check_items"]}

    def run_to_onsite(self, hazard):
        h = self.svc.dispatch(self.grid.id, hazard["id"], self.rep.id,
                              expected_version=hazard["version"])
        h = self.svc.arrive_onsite(
            self.rep.id, h["id"], expected_version=h["version"],
            check_results=self.all_pass(h),
        )
        return h


class TestMainFlow(ServiceTestBase):
    def test_discover_dispatch_onsite_review_close(self):
        _, _, h = self.make_hazard()
        self.assertEqual(h["state"], HazardState.DISCOVERED.value)

        h = self.svc.dispatch(self.grid.id, h["id"], self.rep.id,
                              expected_version=h["version"])
        self.assertEqual(h["state"], HazardState.DISPATCHED.value)
        self.assertEqual(h["owner"], "维修甲")

        h = self.svc.arrive_onsite(
            self.rep.id, h["id"], expected_version=h["version"],
            check_results=self.all_pass(h),
        )
        self.assertEqual(h["state"], HazardState.ONSITE.value)

        h = self.svc.review(
            self.grid.id, h["id"], expected_version=h["version"],
            passed=True, evidence_refs=["EV-1"], note="合格",
        )
        self.assertEqual(h["state"], HazardState.RESOLVED.value)

        h = self.svc.close(self.grid.id, h["id"],
                           expected_version=h["version"])
        self.assertEqual(h["state"], HazardState.CLOSED.value)
        self.assertIsNotNone(h["closed_at"])

    def test_cannot_close_without_passing_review(self):
        h = self.run_to_onsite(self.make_hazard()[2])
        h = self.svc.review(
            self.grid.id, h["id"], expected_version=h["version"],
            passed=False, evidence_refs=["EV-2"], note="联动失败需整改",
        )
        self.assertEqual(h["state"], HazardState.RE_REVIEW_PENDING.value)
        with self.assertRaises(StateError):
            self.svc.close(self.grid.id, h["id"],
                           expected_version=h["version"])

        # 整改后再次复核通过才允许销项
        h = self.svc.review(
            self.grid.id, h["id"], expected_version=h["version"],
            passed=True, evidence_refs=["EV-3"], note="整改合格",
        )
        h = self.svc.close(self.grid.id, h["id"],
                           expected_version=h["version"])
        self.assertEqual(h["state"], HazardState.CLOSED.value)

    def test_review_requires_evidence(self):
        h = self.run_to_onsite(self.make_hazard()[2])
        with self.assertRaises(ValidationError):
            self.svc.review(self.grid.id, h["id"],
                            expected_version=h["version"],
                            passed=True, evidence_refs=[])

    def test_illegal_transition_rejected(self):
        _, _, h = self.make_hazard()
        h = self.svc.dispatch(self.grid.id, h["id"], self.rep.id,
                              expected_version=h["version"])
        # 已派发但尚未上门，不能直接复核
        with self.assertRaises(StateError):
            self.svc.review(
                self.grid.id, h["id"], expected_version=h["version"],
                passed=True, evidence_refs=["EV-x"],
            )


class TestBranches(ServiceTestBase):
    def test_access_denied_requires_note_and_returns_to_flow(self):
        _, _, h = self.make_hazard(scope=AuthorizationScope.NONE)
        h = self.svc.dispatch(self.grid.id, h["id"], self.rep.id,
                              expected_version=h["version"])
        with self.assertRaises(ValidationError):
            self.svc.report_access_denied(
                self.rep.id, h["id"], expected_version=h["version"], reason="",
            )
        h = self.svc.report_access_denied(
            self.rep.id, h["id"], expected_version=h["version"],
            reason="住户拒绝入户，已张贴告知书",
        )
        self.assertEqual(h["state"], HazardState.ACCESS_DENIED.value)
        self.assertTrue(h["branch_note"])

        # 未授权不能直接上门
        with self.assertRaises(StateError):
            self.svc.arrive_onsite(
                self.rep.id, h["id"], expected_version=h["version"],
                check_results=self.all_pass(h),
            )

    def test_replacement_branch_respects_authorization(self):
        resident, device, h = self.make_hazard(
            scope=AuthorizationScope.INSPECTION
        )
        h = self.svc.dispatch(self.grid.id, h["id"], self.rep.id,
                              expected_version=h["version"])
        h = self.svc.report_replacement_needed(
            self.rep.id, h["id"], expected_version=h["version"],
            reason="设备老化无法联网，需整机更换",
        )
        self.assertEqual(h["state"], HazardState.REPLACEMENT_PENDING.value)

        # 授权范围不含更换 → 拒绝
        with self.assertRaises(PermissionDenied):
            self.svc.complete_replacement(
                self.rep.id, h["id"], expected_version=h["version"],
            )

        self.svc.update_authorization(
            self.grid.id, resident.id, AuthorizationScope.DEVICE_REPLACEMENT,
            note="住户签字同意更换",
        )
        h = self.svc.complete_replacement(
            self.rep.id, h["id"], expected_version=h["version"],
        )
        self.assertEqual(h["state"], HazardState.DISPATCHED.value)
        self.assertNotEqual(h["device_id"], device.id)
        new_device = self.svc.get_device(self.manager.id, h["device_id"])
        self.assertEqual(new_device["status"], DeviceStatus.ONLINE.value)
        self.assertEqual(new_device["history_count"], 1)

    def test_overdue_flagged_and_keeps_counting_after_restart(self):
        _, _, h = self.make_hazard(deadline=date(2026, 9, 5))
        self.current = datetime(2026, 9, 10, 9, 0, 0)
        flagged = self.svc.refresh_overdue_flags()
        self.assertIn(h["id"], flagged)
        h = self.svc.get_hazard(self.grid.id, h["id"])
        self.assertEqual(h["state"], HazardState.DISCOVERED.value)  # 主状态保留
        self.assertTrue(h["overdue_flagged"])
        self.assertEqual(h["days_overdue"], 5)

        # 重启后继续计时，且不重复打标
        self.current = datetime(2026, 9, 12, 9, 0, 0)
        svc2 = self.fresh_service()
        self.assertEqual(svc2.refresh_overdue_flags(), [])
        h = svc2.get_hazard(self.grid.id, h["id"])
        self.assertEqual(h["days_overdue"], 7)

    def test_overdue_dispatch_requires_note(self):
        _, _, h = self.make_hazard(deadline=date(2026, 9, 5))
        self.current = datetime(2026, 9, 9, 9, 0, 0)
        self.svc.refresh_overdue_flags()
        h = self.svc.get_hazard(self.grid.id, h["id"])
        self.assertTrue(h["overdue_flagged"])
        # 逾期分支：不填写处置说明不能派发
        with self.assertRaises(ValidationError):
            self.svc.dispatch(self.grid.id, h["id"], self.rep.id,
                              expected_version=h["version"])
        h = self.svc.dispatch(
            self.grid.id, h["id"], self.rep.id,
            expected_version=h["version"], note="已约谈住户，限期整改",
        )
        self.assertEqual(h["state"], HazardState.DISPATCHED.value)

    def test_resolved_hazard_stops_overdue_clock(self):
        h = self.make_hazard(deadline=date(2026, 9, 5))[2]
        self.current = datetime(2026, 9, 8, 9, 0, 0)
        h = self.run_to_onsite(h)
        h = self.svc.review(
            self.grid.id, h["id"], expected_version=h["version"],
            passed=True, evidence_refs=["EV"],
        )
        self.current = datetime(2026, 9, 20, 9, 0, 0)
        self.svc.refresh_overdue_flags()
        h = self.svc.get_hazard(self.manager.id, h["id"])
        self.assertEqual(h["days_overdue"], 0)
        self.assertFalse(h["overdue_flagged"])


class TestConcurrencyAndOffline(ServiceTestBase):
    def test_optimistic_lock_version_conflict(self):
        # 用复核并发测试：两人基于同一版本读取，先提交者成功，后者冲突
        h = self.run_to_onsite(self.make_hazard()[2])
        stale = h["version"]
        self.svc.review(
            self.grid.id, h["id"], expected_version=stale,
            passed=True, evidence_refs=["EV-A"],
        )
        with self.assertRaises(VersionConflict):
            self.svc.review(
                self.manager.id, h["id"], expected_version=stale,
                passed=False, evidence_refs=["EV-B"], note="异议",
            )

    def test_duplicate_dispatch_beats_stale_version(self):
        # 已在流程中且版本过期：优先按“重复派发返回原流程”处理
        _, _, h = self.make_hazard()
        stale = h["version"]
        self.svc.dispatch(self.grid.id, h["id"], self.rep.id,
                          expected_version=stale)
        view = self.svc.dispatch_or_return(
            self.manager.id, h["id"], self.rep2.id,
            expected_version=stale,
        )
        self.assertTrue(view["returned_original_flow"])
        self.assertEqual(view["owner"], "维修甲")

    def test_duplicate_dispatch_returns_original_flow(self):
        _, _, h = self.make_hazard()
        first = self.svc.dispatch_or_return(
            self.grid.id, h["id"], self.rep.id, expected_version=h["version"]
        )
        self.assertFalse(first["returned_original_flow"])
        again = self.svc.dispatch_or_return(
            self.grid.id, h["id"], self.rep2.id,
            expected_version=first["version"],
        )
        self.assertTrue(again["returned_original_flow"])
        self.assertEqual(again["owner"], "维修甲")  # 责任人未被替换
        self.assertEqual(again["version"], first["version"])  # 未产生新版本

    def test_offline_sync_cannot_overwrite_completed_conclusion(self):
        _, _, h = self.make_hazard()
        base = h["version"]
        h = self.run_to_onsite(h)
        h = self.svc.review(
            self.grid.id, h["id"], expected_version=h["version"],
            passed=True, evidence_refs=["EV-9"],
        )
        # 终端拿着旧基线补传“失败”结果，不得推翻已通过结论
        with self.assertRaises(CompletedConclusionError):
            self.svc.offline_sync_onsite(
                "TERM-1", self.rep.id, h["id"], base_version=base,
                check_results={"报警器通电与联网状态": CheckResult.FAIL},
                recorded_at=datetime(2026, 9, 2, 10, 0, 0),
            )
        # 结论仍在
        self.assertEqual(
            self.svc.get_hazard(self.manager.id, h["id"])["state"],
            HazardState.RESOLVED.value,
        )

    def test_offline_sync_applies_when_still_dispatched(self):
        _, _, h = self.make_hazard()
        base = h["version"]
        h = self.svc.dispatch(self.grid.id, h["id"], self.rep.id,
                              expected_version=base)
        # 终端在离线状态下完成上门采集，回连后补传
        out = self.svc.offline_sync_onsite(
            "TERM-1", self.rep.id, h["id"], base_version=h["version"],
            check_results=self.all_pass(h),
            recorded_at=datetime(2026, 9, 2, 10, 0, 0),
        )
        self.assertTrue(out["offline_applied"])
        self.assertEqual(out["state"], HazardState.ONSITE.value)
        # 补传记录的时间采用终端采集时间
        self.assertEqual(
            out["check_items"][0]["checked_by"], "维修甲"
        )

    def test_offline_sync_after_onsite_does_not_roll_back(self):
        h = self.run_to_onsite(self.make_hazard()[2])
        before = h["version"]
        out = self.svc.offline_sync_onsite(
            "TERM-1", self.rep.id, h["id"], base_version=before,
            check_results=self.all_pass(h),
            recorded_at=datetime(2026, 9, 2, 10, 0, 0),
        )
        self.assertFalse(out["offline_applied"])
        self.assertEqual(out["state"], HazardState.ONSITE.value)

    def test_offline_sync_stale_base_does_not_advance_flow(self):
        # 隐患被重新派发过（版本前进），旧基线补传只能留痕
        _, _, h = self.make_hazard()
        stale = h["version"]
        h = self.svc.dispatch(self.grid.id, h["id"], self.rep.id,
                              expected_version=stale)
        self.current = datetime(2026, 9, 12, 9, 0, 0)
        self.svc.refresh_overdue_flags()
        h = self.svc.get_hazard(self.manager.id, h["id"])
        out = self.svc.offline_sync_onsite(
            "TERM-1", self.rep.id, h["id"], base_version=stale,
            check_results=self.all_pass(h),
            recorded_at=datetime(2026, 9, 2, 10, 0, 0),
        )
        self.assertFalse(out["offline_applied"])
        self.assertEqual(out["state"], HazardState.DISPATCHED.value)

    def test_offline_sync_requires_assigned_repairer(self):
        h = self.run_to_onsite(self.make_hazard()[2])
        with self.assertRaises(PermissionDenied):
            self.svc.offline_sync_onsite(
                "TERM-2", self.rep2.id, h["id"], base_version=h["version"],
                check_results=self.all_pass(h),
                recorded_at=datetime(2026, 9, 2, 10, 0, 0),
            )

    def test_concurrent_reviews_only_one_succeeds(self):
        """多名人员同时基于同一版本复核：恰好一人成功，其余版本冲突。"""
        h = self.run_to_onsite(self.make_hazard()[2])
        version = h["version"]

        def do_review(actor_id: str, passed: bool):
            try:
                self.svc.review(
                    actor_id, h["id"], expected_version=version,
                    passed=passed,
                    evidence_refs=[f"EV-{actor_id}"],
                    note="" if passed else "需整改",
                )
                return "ok"
            except Exception as exc:  # noqa: BLE001 - 按类型归类即可
                return type(exc).__name__

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(
                lambda i: do_review(
                    self.grid.id if i % 2 == 0 else self.manager.id,
                    i % 2 == 0,
                ),
                range(8),
            ))
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("VersionConflict"), 7)
        final = self.svc.get_hazard(self.manager.id, h["id"])
        self.assertEqual(final["version"], version + 1)


class TestRbacAndPersistence(ServiceTestBase):
    def test_grid_worker_sees_only_own_buildings(self):
        self.make_hazard(building="1栋", room="101")
        self.make_hazard(building="2栋", room="201")
        self.make_hazard(building="9栋", room="901")
        seen = self.svc.list_hazards(self.grid.id)
        self.assertEqual({row["building"] for row in seen}, {"1栋", "2栋"})
        self.assertEqual(len(self.svc.list_hazards(self.grid_other.id)), 1)
        self.assertEqual(
            len(self.svc.list_hazards(self.manager.id)), 3
        )

    def test_grid_worker_cross_building_denied(self):
        _, _, h = self.make_hazard(building="9栋")
        with self.assertRaises(PermissionDenied):
            self.svc.get_hazard(self.grid.id, h["id"])

    def test_grid_view_hides_contact_and_evidence_detail(self):
        h = self.run_to_onsite(self.make_hazard()[2])
        h = self.svc.review(
            self.grid.id, h["id"], expected_version=h["version"],
            passed=True, evidence_refs=["EV-secret"],
        )
        view = self.svc.get_hazard(self.grid.id, h["id"])
        self.assertNotIn("resident_contact", view)
        self.assertNotIn("timeline", view)
        self.assertNotIn("reviews", view)  # 只见末次结论，不见全部凭证
        self.assertEqual(view["last_review"]["passed"], True)

    def test_manager_tracking_has_owner_overdue_and_evidence(self):
        h = self.run_to_onsite(self.make_hazard()[2])
        self.svc.review(
            self.grid.id, h["id"], expected_version=h["version"],
            passed=True, evidence_refs=["EV-a", "EV-b"],
        )
        rows = self.svc.manager_tracking(self.manager.id)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["owner"], "维修甲")
        self.assertEqual(row["reviews"][0]["evidence_refs"], ["EV-a", "EV-b"])
        self.assertIn("timeline", row)
        self.assertGreaterEqual(len(row["timeline"]), 4)

    def test_repairer_only_sees_assigned(self):
        h1 = self.make_hazard(room="101")[2]
        self.make_hazard(room="102")
        self.svc.dispatch(self.grid.id, h1["id"], self.rep.id,
                          expected_version=h1["version"])
        self.assertEqual(len(self.svc.list_hazards(self.rep.id)), 1)
        self.assertEqual(self.svc.list_hazards(self.rep2.id), [])
        with self.assertRaises(PermissionDenied):
            self.svc.get_hazard(self.rep2.id, h1["id"])

    def test_device_last_check_record_available(self):
        """维修人员上门后，最近一次检测记录可查（题面痛点）。"""
        _, device, h = self.make_hazard()
        self.assertIsNone(
            self.svc.get_device(self.manager.id, device.id)["last_check_at"]
        )
        self.run_to_onsite(h)
        view = self.svc.get_device(self.manager.id, device.id)
        self.assertIsNotNone(view["last_check_at"])
        self.assertEqual(view["history_count"], 4)

    def test_restart_preserves_state_for_pending_work(self):
        _, _, h = self.make_hazard()
        h = self.run_to_onsite(h)
        hid = h["id"]
        version_before = h["version"]

        svc2 = self.fresh_service()
        h2 = svc2.get_hazard(self.manager.id, hid)
        self.assertEqual(h2["state"], HazardState.ONSITE.value)
        self.assertEqual(h2["version"], version_before)
        # 重启后流程可继续推进至闭环
        h2 = svc2.review(
            self.grid.id, hid, expected_version=h2["version"],
            passed=True, evidence_refs=["EV-restart"],
        )
        h2 = svc2.close(self.grid.id, hid,
                        expected_version=h2["version"])
        self.assertEqual(h2["state"], HazardState.CLOSED.value)


if __name__ == "__main__":
    unittest.main()
