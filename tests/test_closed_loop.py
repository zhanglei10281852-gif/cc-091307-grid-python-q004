"""燃气隐患闭环服务测试：覆盖流程、分支、并发、补传、幂等、权限与重启续时。"""
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from src.errors import (
    AccessDeniedError,
    StateTransitionError,
    ValidationError,
    VersionConflictError,
)
from src.models import Authorization, DeviceInfo, InspectionItem
from src.service import GasHazardService

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


class Clock:
    """可推进的测试时钟。"""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kwargs):
        self.t += timedelta(**kwargs)


def make_auth():
    return Authorization(
        entry_allowed=True,
        device_replace_allowed=True,
        contact_allowed=True,
        granted_at=T0,
        note="已签署入户检测授权",
    )


def make_device(online=False):
    return DeviceInfo(
        device_id="ALM-001",
        kind="燃气报警器",
        online=online,
        fault="" if online else "长期离线",
        last_check_at=None,
    )


def make_items():
    return [
        InspectionItem("报警器供电"),
        InspectionItem("阀门密封性"),
        InspectionItem("软管老化"),
    ]


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(T0)
        self.svc = GasHazardService(":memory:", now_fn=self.clock)
        self.svc.register_worker("grid-1", "张网格", "grid_worker", ["1栋"])
        self.svc.register_worker("grid-2", "李网格", "grid_worker", ["2栋"])
        self.svc.register_worker("mgr-1", "王管理", "manager")

    def tearDown(self):
        self.svc.close()

    def register(self, building="1栋", days=7, **kwargs):
        params = dict(
            building=building,
            unit="2单元",
            room="301",
            household_name="陈先生",
            household_phone="138****0001",
            authorization=make_auth(),
            device=make_device(),
            inspection_items=make_items(),
            deadline=self.clock() + timedelta(days=days),
            discovered_by="grid-1",
            note="报警器长期离线",
        )
        params.update(kwargs)
        return self.svc.register_hazard(**params)

    def drive_to_pending_review(self, hazard_id):
        """（按需派发并）上门整改完成，返回当前视图。可重复调用（复核退回后再整改）。"""
        current = self.svc.get_hazard_for_manager("mgr-1", hazard_id)
        if current["status"] == "discovered":
            self.svc.dispatch(hazard_id=hazard_id, assignee="维修-甲", actor="grid-1")
            current = self.svc.get_hazard_for_manager("mgr-1", hazard_id)
        return self.svc.record_visit(
            hazard_id=hazard_id,
            actor="维修-甲",
            outcome="rectified",
            expected_version=current["version"],
            inspection_results=[
                {"name": "报警器供电", "result": "passed"},
                {"name": "阀门密封性", "result": "passed"},
                {"name": "软管老化", "result": "passed"},
            ],
        )


class TestMainFlow(ServiceTestBase):
    def test_full_closed_loop(self):
        hz = self.register()
        hid = hz["hazard_id"]
        self.assertEqual(hz["status"], "discovered")
        self.assertEqual(hz["version"], 1)

        d = self.svc.dispatch(hazard_id=hid, assignee="维修-甲", actor="grid-1")
        self.assertFalse(d["duplicated"])
        self.assertEqual(d["hazard"]["status"], "dispatched")
        self.assertEqual(d["hazard"]["assignee"], "维修-甲")

        v = self.svc.record_visit(
            hazard_id=hid, actor="维修-甲", outcome="in_progress", expected_version=2
        )
        self.assertEqual(v["status"], "on_site")

        v = self.svc.record_visit(
            hazard_id=hid, actor="维修-甲", outcome="rectified", expected_version=3
        )
        self.assertEqual(v["status"], "pending_review")

        v = self.svc.review(
            hazard_id=hid,
            reviewer="grid-1",
            passed=True,
            evidence=["photo://after.jpg", "可燃气体读数 0ppm"],
            expected_version=4,
        )
        self.assertEqual(v["status"], "closed")
        self.assertEqual(v["review_verdict"], "passed")
        self.assertIsNotNone(v["closed_at"])
        # 每个环节都有留痕
        types = [e["type"] for e in v["events"]]
        self.assertEqual(
            types,
            ["discovered", "dispatched", "visit_in_progress",
             "visit_rectified", "review_passed"],
        )

    def test_only_passed_review_can_close(self):
        hz = self.register()
        hid = hz["hazard_id"]
        # 未上门不能复核
        with self.assertRaises(StateTransitionError):
            self.svc.review(
                hazard_id=hid, reviewer="grid-1", passed=True,
                evidence=["p"], expected_version=1,
            )
        self.drive_to_pending_review(hid)
        # 复核必须留证据
        with self.assertRaises(ValidationError):
            self.svc.review(
                hazard_id=hid, reviewer="grid-1", passed=True,
                evidence=[], expected_version=3,
            )
        # 复核不通过 → 退回整改，不能销项
        v = self.svc.review(
            hazard_id=hid, reviewer="grid-1", passed=False,
            evidence=["photo://hose.jpg"], note="软管仍老化，需更换",
            expected_version=3,
        )
        self.assertEqual(v["status"], "dispatched")
        self.assertEqual(v["review_verdict"], "failed")
        # 复核不通过必须留说明
        v2 = self.drive_to_pending_review(hid)
        with self.assertRaises(ValidationError):
            self.svc.review(
                hazard_id=hid, reviewer="grid-1", passed=False,
                evidence=["p"], expected_version=v2["version"],
            )


class TestBranches(ServiceTestBase):
    def test_entry_refused_branch(self):
        hz = self.register()
        hid = hz["hazard_id"]
        self.svc.dispatch(hazard_id=hid, assignee="维修-甲", actor="grid-1")
        # 拒绝入户必须留说明
        with self.assertRaises(ValidationError):
            self.svc.record_visit(
                hazard_id=hid, actor="维修-甲", outcome="entry_refused",
                expected_version=2,
            )
        v = self.svc.record_visit(
            hazard_id=hid, actor="维修-甲", outcome="entry_refused",
            note="住户家中无人，拒绝开门", expected_version=2,
        )
        self.assertEqual(v["status"], "entry_refused")
        self.assertEqual(v["branch"], "entry_refused")
        self.assertEqual(v["branch_note"], "住户家中无人，拒绝开门")
        # 拒绝入户状态下不能直接上门，需重新派发
        with self.assertRaises(StateTransitionError):
            self.svc.record_visit(
                hazard_id=hid, actor="维修-甲", outcome="in_progress",
                expected_version=v["version"],
            )
        d = self.svc.dispatch(hazard_id=hid, assignee="维修-甲", actor="grid-1",
                              note="再次约访")
        self.assertFalse(d["duplicated"])
        self.assertEqual(d["hazard"]["status"], "dispatched")
        self.assertEqual(d["hazard"]["branch"], "none")  # 回到主流程，分支清除

    def test_device_replaced_branch(self):
        hz = self.register()
        hid = hz["hazard_id"]
        self.svc.dispatch(hazard_id=hid, assignee="维修-甲", actor="grid-1")
        # 缺说明、缺新设备信息均不允许
        with self.assertRaises(ValidationError):
            self.svc.record_visit(
                hazard_id=hid, actor="维修-甲", outcome="device_replaced",
                expected_version=2,
            )
        with self.assertRaises(ValidationError):
            self.svc.record_visit(
                hazard_id=hid, actor="维修-甲", outcome="device_replaced",
                note="旧报警器失效", expected_version=2,
            )
        new_device = DeviceInfo(device_id="ALM-002", online=True,
                                last_check_at=self.clock())
        v = self.svc.record_visit(
            hazard_id=hid, actor="维修-甲", outcome="device_replaced",
            note="旧报警器长期离线无法恢复，现场更换",
            new_device=new_device, expected_version=2,
        )
        self.assertEqual(v["status"], "device_replaced")
        self.assertEqual(v["branch"], "device_replaced")
        self.assertEqual(v["device"]["device_id"], "ALM-002")
        self.assertTrue(v["device"]["online"])
        # 更换后仍需复核通过才能销项
        v = self.svc.submit_for_review(
            hazard_id=hid, actor="维修-甲", expected_version=v["version"]
        )
        v = self.svc.review(
            hazard_id=hid, reviewer="grid-1", passed=True,
            evidence=["photo://new-device.jpg", "上线心跳正常"],
            expected_version=v["version"],
        )
        self.assertEqual(v["status"], "closed")

    def test_overdue_branch(self):
        hz = self.register(days=1)
        hid = hz["hazard_id"]
        self.svc.dispatch(hazard_id=hid, assignee="维修-甲", actor="grid-1")
        # 未超期不能走超期处理
        with self.assertRaises(ValidationError):
            self.svc.record_overdue_handling(
                hazard_id=hid, actor="mgr-1", action="escalate",
                note="催办", expected_version=2,
            )
        self.clock.advance(days=3)
        view = self.svc.get_hazard_for_manager("mgr-1", hid)
        self.assertEqual(view["overdue_days"], 2)
        # 超期处理必须留说明
        with self.assertRaises(ValidationError):
            self.svc.record_overdue_handling(
                hazard_id=hid, actor="mgr-1", action="escalate",
                note="", expected_version=2,
            )
        # 升级督办
        v = self.svc.record_overdue_handling(
            hazard_id=hid, actor="mgr-1", action="escalate",
            note="住户长期不在家，升级社区督办", expected_version=2,
        )
        self.assertEqual(v["branch"], "overdue")
        self.assertEqual(v["branch_note"], "住户长期不在家，升级社区督办")
        # 延期：新期限必须晚于原期限
        with self.assertRaises(ValidationError):
            self.svc.record_overdue_handling(
                hazard_id=hid, actor="mgr-1", action="extend",
                note="延期", new_deadline=T0,  # 早于原期限（T0+1天）
                expected_version=v["version"],
            )
        v = self.svc.record_overdue_handling(
            hazard_id=hid, actor="mgr-1", action="extend",
            note="已与住户约定三日后上门", new_deadline=self.clock() + timedelta(days=3),
            expected_version=v["version"],
        )
        self.assertEqual(v["overdue_days"], 0)
        self.assertEqual(v["branch"], "none")
        handled = [e for e in v["events"] if e["type"] == "overdue_handled"]
        self.assertEqual(len(handled), 2)
        self.assertTrue(all(e["note"] for e in handled))


class TestConcurrency(ServiceTestBase):
    def test_stale_version_rejected(self):
        hz = self.register()
        hid = hz["hazard_id"]
        self.svc.dispatch(hazard_id=hid, assignee="维修-甲", actor="grid-1")
        # 另一人基于旧版本（v1）更新 → 版本冲突
        with self.assertRaises(VersionConflictError):
            self.svc.record_visit(
                hazard_id=hid, actor="维修-乙", outcome="in_progress",
                expected_version=1,
            )

    def test_concurrent_updates_exactly_one_wins(self):
        hz = self.register()
        hid = hz["hazard_id"]
        self.svc.dispatch(hazard_id=hid, assignee="维修-甲", actor="grid-1")
        barrier = threading.Barrier(2)
        results, errors = [], []

        def visit(actor):
            try:
                barrier.wait(timeout=5)
                results.append(self.svc.record_visit(
                    hazard_id=hid, actor=actor, outcome="in_progress",
                    expected_version=2,
                ))
            except VersionConflictError:
                errors.append(actor)

        threads = [threading.Thread(target=visit, args=(a,))
                   for a in ("维修-甲", "维修-乙")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        final = self.svc.get_hazard_for_manager("mgr-1", hid)
        self.assertEqual(final["version"], 3)
        self.assertEqual(final["status"], "on_site")


class TestOfflineBackfill(ServiceTestBase):
    def test_backfill_cannot_overwrite_closed(self):
        hz = self.register()
        hid = hz["hazard_id"]
        v = self.drive_to_pending_review(hid)
        v = self.svc.review(
            hazard_id=hid, reviewer="grid-1", passed=True,
            evidence=["photo://ok.jpg"], expected_version=v["version"],
        )
        self.assertEqual(v["status"], "closed")
        # 离线补传“整改完成” → 不得覆盖已销项结论
        r = self.svc.apply_offline_event(
            event_id="off-1", hazard_id=hid, event_type="visit_rectified",
            occurred_at=T0 + timedelta(hours=1), actor="维修-甲",
        )
        self.assertFalse(r["applied"])
        self.assertEqual(r["hazard"]["status"], "closed")
        self.assertEqual(r["hazard"]["review_verdict"], "passed")
        # 未生效的补传也留痕
        ignored = [e for e in r["hazard"]["events"] if not e["applied"]]
        self.assertEqual(len(ignored), 1)
        self.assertEqual(ignored[0]["source"], "offline")
        # 离线补传“复核不通过” → 同样不得覆盖
        r = self.svc.apply_offline_event(
            event_id="off-2", hazard_id=hid, event_type="review_failed",
            occurred_at=T0 + timedelta(hours=2), actor="复核-丙",
            note="存疑", payload={"evidence": ["photo://x.jpg"]},
        )
        self.assertFalse(r["applied"])
        self.assertEqual(r["hazard"]["review_verdict"], "passed")

    def test_backfill_duplicate_and_stale(self):
        hz = self.register()
        hid = hz["hazard_id"]
        self.svc.dispatch(hazard_id=hid, assignee="维修-甲", actor="grid-1")
        # 正常补传：上门整改完成
        r = self.svc.apply_offline_event(
            event_id="off-10", hazard_id=hid, event_type="visit_rectified",
            occurred_at=T0 + timedelta(hours=1), actor="维修-甲",
        )
        self.assertTrue(r["applied"])
        self.assertEqual(r["hazard"]["status"], "pending_review")
        applied = [e for e in r["hazard"]["events"]
                   if e["type"] == "offline:visit_rectified"]
        self.assertEqual(applied[0]["occurred_at"],
                         (T0 + timedelta(hours=1)).isoformat())
        # 同一 event_id 重复补传 → 幂等忽略
        r = self.svc.apply_offline_event(
            event_id="off-10", hazard_id=hid, event_type="visit_rectified",
            occurred_at=T0 + timedelta(hours=1), actor="维修-甲",
        )
        self.assertFalse(r["applied"])
        self.assertEqual(r["reason"], "duplicate")
        # 落后于当前进度的补传 → 仅留痕不回退
        r = self.svc.apply_offline_event(
            event_id="off-11", hazard_id=hid, event_type="visit_in_progress",
            occurred_at=T0 + timedelta(minutes=30), actor="维修-甲",
        )
        self.assertFalse(r["applied"])
        self.assertEqual(r["hazard"]["status"], "pending_review")

    def test_backfill_review_not_overwrite_newer_review(self):
        hz = self.register()
        hid = hz["hazard_id"]
        v = self.drive_to_pending_review(hid)
        self.clock.advance(hours=2)
        v = self.svc.review(
            hazard_id=hid, reviewer="grid-1", passed=False,
            evidence=["photo://bad.jpg"], note="复核不通过",
            expected_version=v["version"],
        )
        # 离线终端补传一条更早发生的“复核通过” → 不得覆盖已有结论
        r = self.svc.apply_offline_event(
            event_id="off-20", hazard_id=hid, event_type="review_passed",
            occurred_at=T0 + timedelta(hours=1), actor="复核-丁",
            payload={"evidence": ["photo://cached.jpg"]},
        )
        self.assertFalse(r["applied"])
        self.assertEqual(r["hazard"]["review_verdict"], "failed")
        self.assertEqual(len(r["hazard"]["reviews"]), 1)


class TestIdempotentDispatch(ServiceTestBase):
    def test_duplicate_dispatch_returns_original(self):
        hz = self.register()
        hid = hz["hazard_id"]
        d1 = self.svc.dispatch(hazard_id=hid, assignee="维修-甲", actor="grid-1",
                               request_id="req-001")
        self.assertFalse(d1["duplicated"])
        # 同一请求号重复派发 → 原流程
        d2 = self.svc.dispatch(hazard_id=hid, assignee="维修-乙", actor="grid-1",
                               request_id="req-001")
        self.assertTrue(d2["duplicated"])
        self.assertEqual(d2["dispatch_id"], d1["dispatch_id"])
        # 无请求号但流程进行中 → 仍返回原流程，责任人不变
        d3 = self.svc.dispatch(hazard_id=hid, assignee="维修-乙", actor="grid-2")
        self.assertTrue(d3["duplicated"])
        self.assertEqual(d3["dispatch_id"], d1["dispatch_id"])
        self.assertEqual(d3["hazard"]["assignee"], "维修-甲")
        self.assertEqual(d3["hazard"]["version"], 2)  # 未产生多余变更

    def test_dispatch_closed_hazard_rejected(self):
        hz = self.register()
        hid = hz["hazard_id"]
        v = self.drive_to_pending_review(hid)
        self.svc.review(
            hazard_id=hid, reviewer="grid-1", passed=True,
            evidence=["photo://ok.jpg"], expected_version=v["version"],
        )
        with self.assertRaises(StateTransitionError):
            self.svc.dispatch(hazard_id=hid, assignee="维修-甲", actor="grid-1")


class TestAccessControl(ServiceTestBase):
    def test_grid_worker_scope_and_masking(self):
        hz1 = self.register(building="1栋")
        hz2 = self.register(building="2栋", room="502")
        # 网格员只能看到所辖楼栋
        mine = self.svc.list_hazards_for_worker("grid-1")
        self.assertEqual([h["hazard_id"] for h in mine], [hz1["hazard_id"]])
        # 仅必要字段：不含住户隐私与复核证据
        view = mine[0]
        for key in ("household_name", "household_phone", "authorization",
                    "reviews", "events"):
            self.assertNotIn(key, view)
        for key in ("hazard_id", "building", "room", "status", "deadline",
                    "overdue_days", "inspection_items", "device",
                    "entry_allowed", "assignee"):
            self.assertIn(key, view)
        # 越楼栋访问被拒绝
        with self.assertRaises(AccessDeniedError):
            self.svc.get_hazard_for_worker("grid-1", hz2["hazard_id"])
        # 未登记人员
        with self.assertRaises(Exception):
            self.svc.list_hazards_for_worker("ghost")

    def test_manager_tracks_assignee_overdue_and_evidence(self):
        hz = self.register(days=1)
        hid = hz["hazard_id"]
        self.svc.dispatch(hazard_id=hid, assignee="维修-甲", actor="grid-1")
        self.clock.advance(days=4)
        v = self.drive_to_pending_review(hid)
        v = self.svc.review(
            hazard_id=hid, reviewer="grid-1", passed=False,
            evidence=["photo://fail1.jpg"], note="软管仍老化",
            expected_version=v["version"],
        )
        v = self.drive_to_pending_review(hid)
        v = self.svc.review(
            hazard_id=hid, reviewer="grid-1", passed=True,
            evidence=["photo://ok2.jpg", "读数 0ppm"],
            expected_version=v["version"],
        )
        view = self.svc.get_hazard_for_manager("mgr-1", hid)
        self.assertEqual(view["assignee"], "维修-甲")          # 责任人
        self.assertGreaterEqual(view["overdue_days"], 0)       # 逾期天数
        self.assertEqual(len(view["reviews"]), 2)              # 每次复核
        self.assertEqual(view["reviews"][0]["evidence"], ["photo://fail1.jpg"])
        self.assertEqual(view["reviews"][1]["evidence"],
                         ["photo://ok2.jpg", "读数 0ppm"])
        self.assertFalse(view["reviews"][0]["passed"])
        self.assertTrue(view["reviews"][1]["passed"])
        # 管理者可见全量楼栋
        self.register(building="2栋", room="502")
        all_views = self.svc.list_hazards_for_worker("mgr-1")
        self.assertEqual(len(all_views), 2)
        # 非管理者不能使用管理者视图
        with self.assertRaises(AccessDeniedError):
            self.svc.get_hazard_for_manager("grid-1", hid)


class TestRestartKeepsTiming(unittest.TestCase):
    def test_unfinished_items_keep_counting_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "hazards.db")
            clock1 = Clock(T0)
            svc1 = GasHazardService(db, now_fn=clock1)
            svc1.register_worker("mgr-1", "王管理", "manager")
            hz = svc1.register_hazard(
                building="1栋", unit="2单元", room="301",
                household_name="陈先生", household_phone="138****0001",
                authorization=make_auth(), device=make_device(),
                inspection_items=make_items(),
                deadline=T0 + timedelta(days=2),
                discovered_by="grid-1",
            )
            hid = hz["hazard_id"]
            svc1.dispatch(hazard_id=hid, assignee="维修-甲", actor="grid-1")
            svc1.close()

            # 服务重启（5 天后）：未完事项继续计时
            clock2 = Clock(T0 + timedelta(days=5))
            svc2 = GasHazardService(db, now_fn=clock2)
            view = svc2.get_hazard_for_manager("mgr-1", hid)
            self.assertEqual(view["status"], "dispatched")
            self.assertEqual(view["overdue_days"], 3)
            self.assertEqual(view["assignee"], "维修-甲")
            # 流程可在重启后继续推进
            v = svc2.record_visit(
                hazard_id=hid, actor="维修-甲", outcome="rectified",
                expected_version=view["version"],
            )
            self.assertEqual(v["status"], "pending_review")
            svc2.close()


if __name__ == "__main__":
    unittest.main()
