"""端到端演示：覆盖主流程与各异常分支。

运行：python -m src.demo
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime

from .models import (
    AuthorizationScope,
    CheckResult,
    DeviceStatus,
    Role,
)
from .service import ClosureService
from .storage import JsonStore


def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="gas-closure-")
    store = JsonStore(os.path.join(tmpdir, "data.json"))

    # 用可控时钟演示“逾期”与“重启继续计时”
    current = {"t": datetime(2026, 9, 1, 9, 0, 0)}
    svc = ClosureService(store, clock=lambda: current["t"])

    # —— 人员 ——
    officer = svc.register_user("李安全员", Role.SAFETY_OFFICER)
    grid3 = svc.register_user("王网格员", Role.GRID_WORKER, buildings=["3栋", "5栋"])
    grid7 = svc.register_user("赵网格员", Role.GRID_WORKER, buildings=["7栋"])
    manager = svc.register_user("陈主任", Role.MANAGER)
    repairer = svc.register_user("周师傅", Role.REPAIRER)

    # —— 住户与设备（授权范围登记）——
    r1 = svc.register_resident("张住户", "3栋", "301",
                               AuthorizationScope.INSPECTION_AND_REPAIR,
                               contact="138****0001")
    r2 = svc.register_resident("刘住户", "3栋", "302",
                               AuthorizationScope.NONE, contact="138****0002")
    d1 = svc.register_device(r1.id, DeviceStatus.OFFLINE)
    d2 = svc.register_device(r2.id, DeviceStatus.OFFLINE)

    print("=" * 70)
    print("1) 发现：3栋301 燃气报警器长期离线")
    h1 = svc.discover(
        officer.id, d1.id, "燃气报警器长期离线",
        rectify_deadline=date(2026, 9, 10),
        description="巡楼发现设备离线超过 15 天",
    )
    h1id, v1 = h1["id"], h1["version"]
    print(json.dumps(h1, ensure_ascii=False, indent=2))

    print("=" * 70)
    print("2) 派发给周师傅；重复派发应返回原流程")
    h1 = svc.dispatch(grid3.id, h1id, repairer.id, expected_version=v1)
    v = h1["version"]
    again = svc.dispatch_or_return(grid3.id, h1id, repairer.id,
                                   expected_version=v)
    print(f"   重复派发返回原流程：{again['returned_original_flow']}，"
          f"原责任人={again['owner']}，版本未变={again['version'] == v}")

    print("=" * 70)
    print("3) 上门：逐项检查并写入设备检测记录")
    results = {
        "报警器通电与联网状态": CheckResult.PASS,
        "报警器声光报警功能": CheckResult.PASS,
        "电磁阀切断联动": CheckResult.PASS,
        "燃气管路与接口检漏": CheckResult.PASS,
    }
    h1 = svc.arrive_onsite(repairer.id, h1id, expected_version=v,
                           check_results=results)
    v = h1["version"]

    print("=" * 70)
    print("4) 复核必须带证据；证据不足被拦截")
    try:
        svc.review(grid3.id, h1id, expected_version=v, passed=True,
                   evidence_refs=[])
    except Exception as exc:
        print(f"   拦截：{exc}")
    h1 = svc.review(grid3.id, h1id, expected_version=v, passed=True,
                    evidence_refs=["EV-photo-01", "EV-signed-02"],
                    note="联网恢复，联动测试正常")
    v = h1["version"]

    print("=" * 70)
    print("5) 复核通过后销项；他人并发更新触发版本冲突")
    conflict = svc.get_hazard(manager.id, h1id)
    svc.close(grid3.id, h1id, expected_version=v, note="闭环完成")
    try:
        svc.close(manager.id, h1id, expected_version=conflict["version"])
    except Exception as exc:
        print(f"   并发提交被拦截：{exc}")

    print("=" * 70)
    print("6) 异常分支：3栋302 拒绝入户，必须留说明")
    h2 = svc.discover(officer.id, d2.id, "报警器离线且住户拒绝沟通",
                      rectify_deadline=date(2026, 9, 5))
    h2id, v2 = h2["id"], h2["version"]
    h2 = svc.dispatch(grid3.id, h2id, repairer.id, expected_version=v2)
    v2 = h2["version"]
    try:
        svc.report_access_denied(repairer.id, h2id, expected_version=v2,
                                 reason="   ")
    except Exception as exc:
        print(f"   空说明被拦截：{exc}")
    h2 = svc.report_access_denied(
        repairer.id, h2id, expected_version=v2,
        reason="住户称家中有老人不愿陌生人入户，已张贴整改告知书",
    )
    v2 = h2["version"]
    print(f"   当前状态：{h2['state']}，分支说明：{h2['branch_note']}")

    print("=" * 70)
    print("7) 逾期分支：时钟推进到 9 月 12 日，系统标记逾期并继续计时")
    current["t"] = datetime(2026, 9, 12, 10, 0, 0)
    svc.refresh_overdue_flags()
    h2 = svc.get_hazard(grid3.id, h2id)
    v2 = h2["version"]  # 逾期标记也是一次版本推进，重新读取后再提交
    print(f"   状态={h2['state']}（主状态保留），逾期天数={h2['days_overdue']}，"
          f"已标记={h2['overdue_flagged']}")

    print("=" * 70)
    print("8) 协调成功后变更授权、重新派发，回到原流程并最终闭环")
    svc.update_authorization(
        grid3.id, r2.id, AuthorizationScope.INSPECTION_AND_REPAIR,
        note="经居委会上门沟通，住户同意工作日白天入户检查",
    )
    h2 = svc.dispatch(grid3.id, h2id, repairer.id, expected_version=v2,
                      note="住户已同意入户")
    v2 = h2["version"]
    h2 = svc.arrive_onsite(repairer.id, h2id, expected_version=v2,
                           check_results={k: CheckResult.PASS for k in results})
    v2 = h2["version"]
    h2 = svc.review(grid3.id, h2id, expected_version=v2, passed=True,
                    evidence_refs=["EV-302-01"], note="补检合格")
    v2 = h2["version"]
    h2 = svc.close(grid3.id, h2id, expected_version=v2)
    print(f"   最终状态：{h2['state']}，逾期天数（销项后）={h2['days_overdue']}")

    print("=" * 70)
    print("9) 离线终端补传：已完成结论不可覆盖")
    r3 = svc.register_resident("陈住户", "5栋", "501",
                               AuthorizationScope.INSPECTION_AND_REPAIR)
    d3 = svc.register_device(r3.id, DeviceStatus.OFFLINE)
    h3 = svc.discover(officer.id, d3.id, "5栋501 报警器离线",
                      rectify_deadline=date(2026, 9, 20))
    h3id, v3 = h3["id"], h3["version"]
    h3 = svc.dispatch(grid3.id, h3id, repairer.id, expected_version=v3)
    v3 = h3["version"]
    # 终端在 v3 基线下离线采集，之后服务端已走完复核
    h3 = svc.arrive_onsite(repairer.id, h3id, expected_version=v3,
                           check_results={k: CheckResult.PASS for k in results})
    v3 = h3["version"]
    h3 = svc.review(grid3.id, h3id, expected_version=v3, passed=True,
                    evidence_refs=["EV-501-01"])
    try:
        svc.offline_sync_onsite(
            "TERM-09", repairer.id, h3id, base_version=v3,
            check_results={"报警器通电与联网状态": CheckResult.FAIL},
            recorded_at=datetime(2026, 9, 11, 15, 0, 0),
        )
    except Exception as exc:
        print(f"   补传被拒绝：{exc}")

    print("=" * 70)
    print("10) RBAC：网格员只能看所辖楼栋；越权访问被拒绝")
    print(f"   王网格员可见单数：{len(svc.list_hazards(grid3.id))}")
    try:
        svc.get_hazard(grid7.id, h1id)
    except Exception as exc:
        print(f"   越权被拦截：{exc}")
    view = svc.get_hazard(grid3.id, h1id)
    print(f"   网格员视图字段：{sorted(view.keys())}")

    print("=" * 70)
    print("11) 管理者追踪：责任人 / 逾期天数 / 每次复核证据")
    tracking = svc.manager_tracking(manager.id)
    for row in tracking:
        print(f"   {row['id']} {row['building']} {row['state']} "
              f"责任人={row['owner'] or '-'} 逾期={row['days_overdue']}天 "
              f"复核次数={len(row['reviews'])}")

    print("=" * 70)
    print("12) 服务重启：数据恢复，未完事项逾期天数继续累计")
    del svc
    svc2 = ClosureService(JsonStore(os.path.join(tmpdir, "data.json")),
                          clock=lambda: current["t"])
    current["t"] = datetime(2026, 9, 25, 9, 0, 0)
    rows = svc2.manager_tracking(manager.id)
    assert len(rows) == 3
    reopened = next(r for r in rows if r["id"] == h3id)
    print(f"   {h3id} 重启后仍为 {reopened['state']}，"
          f"逾期天数={reopened['days_overdue']}")
    print("\n演示完成，数据文件：", os.path.join(tmpdir, "data.json"))


if __name__ == "__main__":
    main()
