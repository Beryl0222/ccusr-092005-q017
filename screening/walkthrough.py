"""用 fixtures/seed.json 的现有资料走通一条完整闭环：

校园筛查提示 → 通知全部监护人（含一次通知失败补达）→ 家长回执并授权
→ 医院复核反馈回传 → 监护人看到易懂说明、学校只看到完成状态、
卫生人员看到去标识化覆盖差异。

运行：python3 -m screening.walkthrough
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from project_data import load_seed

from .models import FeedbackCategory, Guardian, GuardianChoice, QueueKind
from .service import ScreeningLoopService
from .views import guardian_view, health_coverage_view, school_view

# 样例资料中的专业判断 → 医院反馈分类
_DECISION_TO_FEEDBACK = {
    "无需处理": FeedbackCategory.NO_ACTION,
    "随访观察": FeedbackCategory.CONTINUE_OBSERVATION,
    "龋病处置": FeedbackCategory.CARIES_TREATMENT,
    "正畸评估": FeedbackCategory.ORTHODONTIC_EVALUATION,
}


@dataclass
class WalkthroughResult:
    service: ScreeningLoopService
    campaign_id: str
    child_key: str
    school_rows: list[dict]
    guardian_report: dict
    health_coverage: dict


def run_seed_walkthrough(seed_path: str | Path = "fixtures/seed.json") -> WalkthroughResult:
    data = load_seed(seed_path)
    oral = next(r for r in data["records"] if r["kind"] == "oral_case")
    habit = next(r for r in data["records"] if r["kind"] == "habit" and r.get("case_id") == oral["id"])
    feedback_category = _DECISION_TO_FEEDBACK[oral["decision"]]

    campaign_id = "camp-2026-autumn"
    school_id = "school-01"
    child_key = oral["id"]  # 沿用样例编号作为校内匿名编号

    service = ScreeningLoopService(today=date(2026, 10, 1), review_deadline_days=30)
    service.create_campaign(campaign_id, school_id=school_id, title="2026年秋季校园口腔筛查")

    # 两位监护人：一位渠道可达，一位最初联系不上（通知失败 → 队列 → 补达）。
    service.register_child(
        child_key,
        school_id=school_id,
        age_years=oral["age_years"],
        guardians=[
            Guardian("guardian-mother", "母亲", contact_ok=True),
            Guardian("guardian-father", "父亲", contact_ok=False),
        ],
    )

    # 1. 学校先取得与本次活动对应的监护同意。
    service.grant_consent(campaign_id, child_key, guardian_id="guardian-mother")

    # 2. 筛查登记：牙列阶段、肉眼观察、护理习惯、建议复核原因，不写诊断。
    service.register_screening(
        campaign_id,
        child_key,
        dentition_stage=oral["dentition"],
        observations=oral["observations"],
        care_habits=habit["items"],
        review_reason="校园筛查肉眼观察提示牙列不齐，建议到医院口腔科复核",
        screener="school-nurse-01",
    )

    # 3. 另一位监护人通知失败已进入队列；修复渠道后重试补达。
    assert service.open_entries(QueueKind.NOTIFICATION_FAILURE), "应存在通知失败条目"
    service.update_guardian_contact(child_key, "guardian-father", contact_ok=True)
    assert service.retry_failed_notifications() == 1

    # 4. 家长回执：选择医院复核、授权结果回传，并补充既往情况。
    service.guardian_respond(
        campaign_id,
        child_key,
        "guardian-mother",
        choices=[GuardianChoice.HOSPITAL_REVIEW, GuardianChoice.SUPPLEMENT_HISTORY],
        authorize_feedback=True,
        history_note="既往无口腔就诊记录，无药物过敏史",
    )

    # 5. 医院复核反馈经授权回传（沿用样例资料中的专业判断）。
    service.submit_hospital_feedback(
        campaign_id,
        child_key,
        category=feedback_category,
        facility="市口腔医院",
        note=f"建议{habit['review_months']}个月后复查",
    )

    return WalkthroughResult(
        service=service,
        campaign_id=campaign_id,
        child_key=child_key,
        school_rows=school_view(service, school_id),
        guardian_report=guardian_view(service, campaign_id, child_key, "guardian-mother"),
        health_coverage=health_coverage_view(service, campaign_id, min_cell_size=1),
    )


def main() -> None:
    result = run_seed_walkthrough()
    summary = {
        "学校可见": result.school_rows,
        "监护人可见": result.guardian_report,
        "卫生人员可见": result.health_coverage,
        "未结队列条目": [e.entry_id for e in result.service.open_entries()],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
