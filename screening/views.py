"""三类角色的视图。

- 学校：只能看到完成状态，看不到任何临床内容；
- 卫生人员：去标识化的覆盖差异（仅聚合计数，支持小样本格抑制）；
- 监护人：易懂的风险说明。系统不会自动把孩子导向矫正——
  医院反馈之前，说明文字不出现任何正畸/矫正类表述；
  只有医疗机构反馈“专业正畸评估”后，才会向监护人转述该结论。
"""

from __future__ import annotations

from .models import (
    CaseStatus,
    DentitionStage,
    FeedbackCategory,
    QueueKind,
    ScreeningError,
)
from .service import ScreeningLoopService

# ----------------------------------------------------------------------
# 学校视图：只有完成状态
# ----------------------------------------------------------------------

_SCHOOL_STATUS_LABELS = {
    CaseStatus.SCREENED: "复核流程进行中",
    CaseStatus.AWAITING_HOSPITAL: "复核流程进行中",
    CaseStatus.COMPLETED: "已完成",
    CaseStatus.CLOSED_REFUSED: "家长谢绝",
}


def school_view(service: ScreeningLoopService, school_id: str) -> list[dict]:
    """学校只能看到每个孩子在每场活动中的完成状态。"""
    rows: list[dict] = []
    for child in service.children.values():
        if child.school_id != school_id:
            continue
        for campaign in service.campaigns.values():
            if campaign.school_id != school_id:
                continue
            key = (campaign.campaign_id, child.child_key)
            case = service.cases.get(key)
            consent = service.consents.get(key)
            if case is not None:
                status = _SCHOOL_STATUS_LABELS[case.status]
            elif consent is not None and consent.granted:
                status = "待筛查登记"
            else:
                status = "待监护同意"
            rows.append(
                {
                    "child_key": child.child_key,
                    "campaign_id": campaign.campaign_id,
                    "status": status,
                }
            )
    return rows


# ----------------------------------------------------------------------
# 卫生人员视图：去标识化覆盖差异
# ----------------------------------------------------------------------


def health_coverage_view(
    service: ScreeningLoopService,
    campaign_id: str,
    *,
    min_cell_size: int = 5,
) -> dict:
    """去标识化的覆盖差异：只有聚合计数，不含任何儿童或监护人标识。

    min_cell_size：小于该值的计数以 None 返回（小样本格抑制），0 除外。
    """
    campaign = service.campaigns.get(campaign_id)
    if campaign is None:
        raise ScreeningError(f"未知活动：{campaign_id}")

    def cell(n: int) -> int | None:
        return n if n == 0 or n >= min_cell_size else None

    children = [c for c in service.children.values() if c.school_id == campaign.school_id]
    cases = [
        service.cases[(campaign_id, c.child_key)]
        for c in children
        if (campaign_id, c.child_key) in service.cases
    ]
    consented = sum(
        1
        for c in children
        if (consent := service.consents.get((campaign_id, c.child_key))) is not None and consent.granted
    )
    responded = sum(1 for c in cases if c.responses)
    by_stage: dict[str, int | None] = {}
    for stage in DentitionStage:
        n = sum(1 for c in cases if c.screening.dentition_stage is stage)
        if n:
            by_stage[stage.value] = cell(n)

    return {
        "campaign_id": campaign_id,
        "min_cell_size": min_cell_size,
        "totals": {
            "registered": cell(len(children)),
            "consented": cell(consented),
            "screened": cell(len(cases)),
            "guardian_responded": cell(responded),
            "awaiting_hospital": cell(sum(1 for c in cases if c.status is CaseStatus.AWAITING_HOSPITAL)),
            "completed": cell(sum(1 for c in cases if c.status is CaseStatus.COMPLETED)),
            "refused": cell(sum(1 for c in cases if c.status is CaseStatus.CLOSED_REFUSED)),
        },
        "coverage_gaps": {
            "consented_not_screened": cell(consented - len(cases)),
            "screened_not_responded": cell(len(cases) - responded),
            "overdue_review": cell(sum(1 for e in service.open_entries(QueueKind.OVERDUE_REVIEW) if e.campaign_id == campaign_id)),
            "notification_failure_open": cell(
                sum(1 for e in service.open_entries(QueueKind.NOTIFICATION_FAILURE) if e.campaign_id == campaign_id)
            ),
        },
        "screened_by_dentition_stage": by_stage,
    }


# ----------------------------------------------------------------------
# 监护人视图：易懂的风险说明
# ----------------------------------------------------------------------

# 标准化提示的通俗解释。措辞刻意避开诊断与治疗表述，
# 是否进一步处理一律交给医院复核判断。
_OBSERVATION_EXPLANATIONS = {
    "轻度牙列不齐": "牙齿排列轻度不整齐。这个年龄段比较常见，不少情况会随生长发育变化，需要医院复核确认。",
    "牙列拥挤": "牙齿排列空间偏紧。是否需要处理，要由医院复核判断。",
    "牙列稀疏": "牙齿之间缝隙偏大。替牙阶段比较常见，需医院复核确认。",
    "深覆盖": "上前牙向前突出较明显，需医院复核评估。",
    "反合": "下牙咬在上牙外侧，建议尽早到医院复核。",
    "疑似龋坏": "牙面有可疑龋坏（蛀牙）迹象，需医院复核确认。",
    "牙面色素沉着": "牙面有色素沉着，多影响外观，需医院复核确认。",
    "牙龈红肿": "牙龈有红肿迹象，请注意日常清洁，需医院复核确认。",
    "口腔卫生欠佳": "口腔清洁情况欠佳，建议加强刷牙等日常护理。",
    "未见明显异常": "本次肉眼观察未见明显异常，保持日常护理即可。",
}

_FEEDBACK_PLAIN = {
    FeedbackCategory.NO_ACTION: "医院复核结论：目前无需处理，保持日常口腔护理即可。",
    FeedbackCategory.CONTINUE_OBSERVATION: "医院复核结论：继续观察，按建议时间复查。",
    FeedbackCategory.CARIES_TREATMENT: "医院复核结论：发现需要处理的龋坏（蛀牙），请按医院安排就诊处置。",
    FeedbackCategory.ORTHODONTIC_EVALUATION: "医院复核结论：建议由专业正畸医师做进一步评估。",
}

_GUARDIAN_STATUS_LABELS = {
    CaseStatus.SCREENED: "等待您的回执",
    CaseStatus.AWAITING_HOSPITAL: "等待医院复核反馈",
    CaseStatus.COMPLETED: "医院复核已反馈",
    CaseStatus.CLOSED_REFUSED: "已谢绝后续联系",
}


def guardian_view(service: ScreeningLoopService, campaign_id: str, child_key: str, guardian_id: str) -> dict:
    """监护人看到的易懂风险说明。

    医院反馈之前，说明只解释筛查提示的含义与可选回执，
    不出现任何正畸/矫正类导向；反馈之后如实转述医院结论。
    """
    case = service.case_file(campaign_id, child_key)
    guardian = service.guardians.get(child_key, {}).get(guardian_id)
    if guardian is None:
        raise ScreeningError(f"儿童 {child_key} 没有监护人 {guardian_id}")
    campaign = service.campaigns[campaign_id]

    screening = case.screening
    explanation = [
        "本次校园筛查只是初步观察，不是医院诊断。",
        f"筛查提示：{'、'.join(screening.observations)}。",
        *[_OBSERVATION_EXPLANATIONS[o] for o in screening.observations if o in _OBSERVATION_EXPLANATIONS],
        "是否需要进一步处理，要由医院口腔科复核后确定。",
    ]

    view: dict = {
        "campaign_title": campaign.title,
        "guardian_relation": guardian.relation,
        "status": _GUARDIAN_STATUS_LABELS[case.status],
        "dentition_stage": screening.dentition_stage.value,
        "explanation": explanation,
        "care_habits_noted": list(screening.care_habits),
        "review_reason": screening.review_reason,
        "history_notes_received": list(case.history_notes),
        "options": [],
        "hospital_feedback": None,
    }
    if case.status is CaseStatus.SCREENED:
        view["options"] = ["选择医院复核", "补充既往情况", "拒绝后续联系"]
    if case.feedback is not None:
        view["hospital_feedback"] = {
            "summary": _FEEDBACK_PLAIN[case.feedback.category],
            "facility": case.feedback.facility,
            "note": case.feedback.note,
        }
    return view
