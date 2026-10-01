"""从现有脱敏样例装载校园闭环演示数据。"""

from __future__ import annotations

from typing import Any

from oral_screening.enums import ConsentScope
from oral_screening.service import ScreeningService


def build_service_from_seed(data: dict[str, Any]) -> ScreeningService:
    """依据 seed.json 的 campus_flow 段注册活动、儿童与两位监护人。

    样例里父亲没有任何授权范围——用于演示“另一位监护人从未看到通知”。
    """
    from datetime import datetime, timedelta, timezone

    flow = data["campus_flow"]
    svc = ScreeningService(review_sla=timedelta(days=30))
    act = flow["activity"]
    svc.register_activity(
        act["id"], act["school_id"], act["name"],
        datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc),
    )

    child = flow["child"]
    # 年龄与牙列信息沿用 records 中的既有样例
    case_record = next(r for r in data["records"] if r["kind"] == "oral_case")
    svc.register_child(
        child["id"],
        act["school_id"],
        child["class_name"],
        age_years=case_record.get("age_years"),
    )

    for g in flow["guardians"]:
        svc.register_guardian(
            g["id"], child["id"], g["label"], g["contact_ref"]
        )
        if g["scopes"]:
            svc.grant_consent(
                act["id"],
                child["id"],
                g["id"],
                [ConsentScope(s) for s in g["scopes"]],
            )

    return svc
