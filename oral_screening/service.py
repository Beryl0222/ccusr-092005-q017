"""闭环服务：规则、权限、队列与视图都集中在这一层。

调用顺序即业务闭环：

    grant_consent(SCREENING) -> record_screening
    grant_consent(NOTIFY)    -> send_notifications -> mark_delivered/mark_viewed
    record_intent(选择复核)  -> authorize_result_return
    submit_review_result     -> acknowledge_result / guardian_explanation

任何跳步都会抛出 :mod:`oral_screening.errors` 中的领域错误。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from .enums import (
    CareHabit,
    ConsentScope,
    DentitionStage,
    FollowUpReason,
    GuardianIntent,
    NotificationState,
    Observation,
    QueueKind,
    ReviewOutcome,
)
from .errors import (
    AuthorizationRequiredError,
    ConsentRequiredError,
    ConsentScopeError,
    DuplicateScreeningError,
    ForbiddenFieldError,
    WorkflowStateError,
)
from .models import (
    Activity,
    Child,
    ConsentGrant,
    Guardian,
    HistorySupplement,
    NotificationRecord,
    ReviewResult,
    ScreeningRecord,
)

#: 距选择复核后的默认复核期限，逾期进“逾期未复核”队列。
DEFAULT_REVIEW_SLA = timedelta(days=30)

#: 筛查表单允许提交的全部字段（白名单）。
SCREENING_ALLOWED_FIELDS = frozenset(
    {"dentition", "observations", "habits", "follow_up_reasons"}
)

#: 命中即说明筛查端在写入诊断/治疗——绝对禁止。
_DIAGNOSIS_KEYS = frozenset({"diagnosis", "诊断", "医疗诊断", "错颌诊断"})
_TREATMENT_KEYS = frozenset(
    {"treatment_plan", "治疗方案", "矫正方案", "正畸方案", "处置方案"}
)

#: 医院回传允许携带的非结论字段。
REVIEW_ALLOWED_EXTRA = frozenset({"note"})


# ---------------------------------------------------------------------------
# 标准化文案
# ---------------------------------------------------------------------------

NOTIFICATION_TEMPLATE = (
    "【校园口腔筛查】{guardian_label}您好：{class_name}{age_text}在本次校园"
    "口腔筛查中留有口腔健康提示。本短信是标准化健康提示，不是医院诊断，"
    "也不代表孩子需要立即矫正。请查收完整说明并选择：医院复核 / 暂不需要 / "
    "补充既往情况。如孩子有牙痛、牙龈肿胀等不适请及时就医。"
)

_PLAIN_OBSERVATION = {
    Observation.MILD_CROWDING: "肉眼可见牙齿排列有些不整齐",
    Observation.OPEN_BITE: "上下牙咬合时可能存在缝隙",
    Observation.CROSS_BITE: "个别牙齿咬合方向看起来相反",
    Observation.LARGE_OVERJET: "上牙看起来比较靠前",
    Observation.SPACING: "牙齿之间缝隙偏大",
    Observation.VISIBLE_CARIES_SIGN: "有疑似蛀牙的迹象，需要医院确认",
    Observation.GINGIVAL_SIGN: "牙龈看起来有红肿迹象",
    Observation.NONE: "肉眼未见明显异常",
}

_PLAIN_HABIT = {
    CareHabit.SOFT_DIET: "饮食偏软，颌骨和牙齿缺少适度咀嚼锻炼",
    CareHabit.NEEDS_BRUSHING_HELP: "刷牙还需要大人协助才能清洁到位",
    CareHabit.IRREGULAR_BRUSHING: "刷牙次数或时间不规律",
    CareHabit.FREQUENT_SNACKS: "甜食或含糖饮料较频繁",
    CareHabit.NIGHT_MILK: "有夜间含奶瓶或奶睡习惯",
    CareHabit.FINGER_HABIT: "有吮指、咬唇等口腔习惯",
    CareHabit.MOUTH_BREATHING: "习惯张口呼吸",
    CareHabit.NO_SPECIAL_HABIT: "没有发现需要特别调整的护理习惯",
}

_PLAIN_OUTCOME = {
    ReviewOutcome.NO_ACTION: "医院检查后认为目前无需处理，按日常护理继续即可。",
    ReviewOutcome.CONTINUE_WATCH: (
        "医院建议继续观察。孩子正处在换牙期，许多排列变化会自行调整，"
        "按约定时间复查即可。"
    ),
    ReviewOutcome.CARIES_TREATMENT: (
        "医院发现需要处理的龋齿，建议按医院安排完成补牙等龋病治疗；"
        "这是治疗蛀牙，与牙齿矫正无关。"
    ),
    ReviewOutcome.ORTHO_EVALUATION: (
        "医院建议到正畸专科做一次专业评估。评估只是请医生判断“将来是否需要、"
        "何时适合矫治”，评估本身不等于开始矫正，更不代表现在就要戴上矫治器。"
    ),
}

_FOOTER = (
    "说明：以上是校园筛查的健康提示与医院复核结论的通俗解释，"
    "不能替代医生面诊；系统不会替孩子预约或启动任何矫正治疗。"
)


def _coerce(value: Any, enum_type: type) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except ValueError:
        allowed = "、".join(m.value for m in enum_type)
        raise ForbiddenFieldError(f"取值 {value!r} 不在受控词表内：{allowed}")


def _coerce_list(values: Iterable[Any] | None, enum_type: type) -> list:
    if values is None:
        return []
    return [_coerce(v, enum_type) for v in values]


class ScreeningService:
    """内存版闭环后端。生产环境替换三个仓储字典即可，规则不变。"""

    def __init__(self, review_sla: timedelta = DEFAULT_REVIEW_SLA) -> None:
        self.activities: dict[str, Activity] = {}
        self.children: dict[str, Child] = {}
        self.guardians: dict[str, Guardian] = {}
        self.consents: list[ConsentGrant] = []
        self.records: dict[tuple[str, str], ScreeningRecord] = {}
        self.review_sla = review_sla

    # ------------------------------------------------------------------ 注册
    def register_activity(
        self, activity_id: str, school_id: str, name: str, occurs_at: datetime
    ) -> Activity:
        activity = Activity(activity_id, school_id, name, occurs_at)
        self.activities[activity_id] = activity
        return activity

    def register_child(
        self,
        child_id: str,
        school_id: str,
        class_name: str,
        age_years: int | None = None,
    ) -> Child:
        child = Child(child_id, school_id, class_name, age_years)
        self.children[child_id] = child
        return child

    def register_guardian(
        self,
        guardian_id: str,
        child_id: str,
        label: str,
        contact_ref: str,
        relationship: str = "监护人",
    ) -> Guardian:
        guardian = Guardian(guardian_id, child_id, label, contact_ref, relationship)
        self.guardians[guardian_id] = guardian
        return guardian

    # ------------------------------------------------------------------ 同意
    def grant_consent(
        self,
        activity_id: str,
        child_id: str,
        guardian_id: str,
        scopes: Iterable[ConsentScope | str],
    ) -> ConsentGrant:
        self._require_activity(activity_id)
        guardian = self._require_active_guardian(guardian_id, child_id)
        scopes = frozenset(_coerce(s, ConsentScope) for s in scopes)
        grant = ConsentGrant(
            consent_id=f"consent-{activity_id}-{guardian_id}",
            activity_id=activity_id,
            child_id=child_id,
            guardian_id=guardian.guardian_id,
            scopes=scopes,
        )
        # 同一监护人在同一活动上重复授权：合并范围；再次授权视为一次新的
        # 确认，时间戳前移（监护关系变化后的重确认依赖这个时间点判定）。
        for existing in self.consents:
            if (
                existing.activity_id == activity_id
                and existing.guardian_id == guardian_id
                and existing.review_id is None
            ):
                existing.scopes = existing.scopes | scopes
                existing.granted_at = datetime.now(timezone.utc)
                existing.revoked_at = None
                return existing
        self.consents.append(grant)
        return grant

    def revoke_consent(self, consent_id: str) -> None:
        for grant in self.consents:
            if grant.consent_id == consent_id and grant.effective:
                grant.revoked_at = datetime.now(timezone.utc)
                return
        raise ConsentScopeError("授权不存在或已撤回")

    def _consent_for(
        self,
        activity_id: str,
        child_id: str,
        scope: ConsentScope,
        guardian_id: str | None = None,
        review_id: str | None = None,
    ) -> ConsentGrant | None:
        for grant in self.consents:
            if grant.activity_id != activity_id or grant.child_id != child_id:
                continue
            if guardian_id is not None and grant.guardian_id != guardian_id:
                continue
            if review_id is not None and grant.review_id != review_id:
                continue
            if not grant.covers(scope):
                continue
            guardian = self.guardians.get(grant.guardian_id)
            if guardian is None or not guardian.active:
                continue
            return grant
        return None

    def _require_consent(self, *args: Any, **kwargs: Any) -> ConsentGrant:
        grant = self._consent_for(*args, **kwargs)
        if grant is None:
            scope = args[2] if len(args) >= 3 else kwargs.get("scope")
            if scope is ConsentScope.RESULT_RETURN:
                raise AuthorizationRequiredError("医院结果回传缺少监护授权")
            raise ConsentRequiredError(f"缺少与本次活动对应的监护授权：{scope}")
        return grant

    # -------------------------------------------------------------- 筛查登记
    def record_screening(
        self,
        activity_id: str,
        child_id: str,
        submission: Mapping[str, Any],
        screened_by: str | None = None,
    ) -> ScreeningRecord:
        """筛查人员提交观察。字段白名单之外一律拒绝（含诊断/治疗方案）。"""
        self._require_activity(activity_id)
        child = self.children.get(child_id)
        if child is None:
            raise ConsentRequiredError("儿童尚未用最小信息登记")

        self._reject_forbidden_fields(submission)

        # 闭环第一关：本次活动的筛查同意必须先行
        self._require_consent(activity_id, child_id, ConsentScope.SCREENING)

        key = (activity_id, child_id)
        if key in self.records:
            existing = self.records[key]
            existing.enqueue(
                QueueKind.DUPLICATE,
                "同一活动内再次提交筛查，已去重，不另建档案",
            )
            raise DuplicateScreeningError(
                "该儿童在本次活动中已有筛查档案，重复提交已进入重复筛查队列"
            )

        record = ScreeningRecord(
            activity_id=activity_id,
            child_id=child_id,
            school_id=child.school_id,
            class_name=child.class_name,
            dentition=_coerce(submission.get("dentition"), DentitionStage),
            observations=_coerce_list(submission.get("observations"), Observation),
            habits=_coerce_list(submission.get("habits"), CareHabit),
            follow_up_reasons=_coerce_list(
                submission.get("follow_up_reasons"), FollowUpReason
            ),
            screened_by=screened_by,
            screened_at=datetime.now(timezone.utc),
        )
        self.records[key] = record
        return record

    @staticmethod
    def _reject_forbidden_fields(submission: Mapping[str, Any]) -> None:
        extra = set(submission) - SCREENING_ALLOWED_FIELDS
        diagnostic = extra & _DIAGNOSIS_KEYS
        treatment = extra & _TREATMENT_KEYS
        if diagnostic:
            raise ForbiddenFieldError(
                f"筛查人员不得写入诊断：{'、'.join(sorted(diagnostic))}；"
                "诊断只能由医疗机构在复核环节作出"
            )
        if treatment:
            raise ForbiddenFieldError(
                f"筛查人员不得写入治疗或矫正方案：{'、'.join(sorted(treatment))}"
            )
        if extra:
            raise ForbiddenFieldError(
                f"筛查表单只允许 {sorted(SCREENING_ALLOWED_FIELDS)}，"
                f"收到越权字段：{sorted(extra)}"
            )

    # ---------------------------------------------------------------- 通知
    def send_notifications(self, activity_id: str, child_id: str) -> list[str]:
        """向所有获得 NOTIFY 授权的在册监护人分别发出提示，逐人追踪。"""
        record = self._require_record(activity_id, child_id)
        recipients = [
            g
            for g in self._active_guardians(child_id)
            if self._consent_for(
                activity_id, child_id, ConsentScope.NOTIFY, guardian_id=g.guardian_id
            )
        ]
        if not recipients:
            raise ConsentRequiredError("没有任何监护人授权接收本次活动通知")

        sent: list[str] = []
        for guardian in recipients:
            note = record.notifications.get(guardian.guardian_id)
            if note is None:
                note = NotificationRecord(guardian_id=guardian.guardian_id)
                record.notifications[guardian.guardian_id] = note
            note.attempts += 1
            note.updated_at = datetime.now(timezone.utc)
            sent.append(guardian.guardian_id)
        return sent

    def render_notification(self, activity_id: str, child_id: str) -> str:
        record = self._require_record(activity_id, child_id)
        child = self.children[child_id]
        age_text = f"（{child.age_years}岁）" if child.age_years is not None else ""
        return NOTIFICATION_TEMPLATE.format(
            guardian_label="各位家长",
            class_name=record.class_name,
            age_text=age_text,
        )

    def mark_delivered(self, activity_id: str, child_id: str, guardian_id: str) -> None:
        note = self._require_notification(activity_id, child_id, guardian_id)
        note.state = NotificationState.DELIVERED
        note.last_error = None
        note.updated_at = datetime.now(timezone.utc)

    def mark_viewed(self, activity_id: str, child_id: str, guardian_id: str) -> None:
        note = self._require_notification(activity_id, child_id, guardian_id)
        note.state = NotificationState.VIEWED
        note.viewed_at = datetime.now(timezone.utc)
        note.updated_at = note.viewed_at

    def mark_failed(
        self, activity_id: str, child_id: str, guardian_id: str, error: str
    ) -> None:
        note = self._require_notification(activity_id, child_id, guardian_id)
        note.state = NotificationState.FAILED
        note.last_error = error
        note.updated_at = datetime.now(timezone.utc)
        record = self._require_record(activity_id, child_id)
        record.enqueue(QueueKind.NOTIFY_FAILED, f"{guardian_id} 通知失败：{error}")

    def retry_failed_notification(
        self, activity_id: str, child_id: str, guardian_id: str
    ) -> None:
        """重投成功后关闭通知失败队列（全部失败项解决时）。"""
        record = self._require_record(activity_id, child_id)
        note = self._require_notification(activity_id, child_id, guardian_id)
        if note.state is not NotificationState.FAILED:
            raise WorkflowStateError("该通知不处于失败状态")
        note.state = NotificationState.PENDING
        note.attempts += 1
        note.updated_at = datetime.now(timezone.utc)
        if not any(
            n.state is NotificationState.FAILED
            for n in record.notifications.values()
        ):
            record.resolve_queue(QueueKind.NOTIFY_FAILED)

    # ----------------------------------------------------------- 查收与意向
    def record_intent(
        self,
        activity_id: str,
        child_id: str,
        guardian_id: str,
        intent: GuardianIntent | str,
        supplement_text: str | None = None,
        at: datetime | None = None,
    ) -> ScreeningRecord:
        record = self._require_record(activity_id, child_id)
        self._require_active_guardian(guardian_id, child_id)
        note = record.notifications.get(guardian_id)
        if note is None:
            raise WorkflowStateError("尚未向该监护人发出通知")
        if note.state is not NotificationState.VIEWED:
            raise WorkflowStateError("监护人尚未查收通知，不能登记处置意向")

        intent = _coerce(intent, GuardianIntent)
        moment = at or datetime.now(timezone.utc)

        if record.closed_at is not None:
            raise WorkflowStateError("档案已结案，处置意向不再可改")
        if record.review is not None:
            raise WorkflowStateError("复核结果已回传，请查收反馈而不是更改处置意向")

        if intent is GuardianIntent.SUPPLEMENT_HISTORY:
            if not supplement_text:
                raise WorkflowStateError("选择补充既往情况时必须填写内容")
            record.supplements.append(
                HistorySupplement(guardian_id=guardian_id, text=supplement_text)
            )
            # 既往情况只增不改：仅当尚未选择复核时意向回“待回应”；
            # 已在复核路径上时保留原意向，避免另一位监护人把流程冲掉。
            if record.intent is GuardianIntent.PENDING:
                record.intent_guardian_id = guardian_id
                record.intent_at = moment
            return record

        record.intent = intent
        record.intent_guardian_id = guardian_id
        record.intent_at = moment

        if intent is GuardianIntent.WANT_REVIEW:
            record.review_due_at = moment + self.review_sla
            record.resolve_queue(QueueKind.OVERDUE_REVIEW)
        elif intent is GuardianIntent.DECLINE:
            record.closed_at = moment
        return record

    # ----------------------------------------------------------- 授权与回传
    def authorize_result_return(
        self, activity_id: str, child_id: str, guardian_id: str
    ) -> str:
        """家长选择医院复核后，单独授予“本次复核结果回传”权。"""
        record = self._require_record(activity_id, child_id)
        if record.intent is not GuardianIntent.WANT_REVIEW:
            raise WorkflowStateError("只有选择医院复核后才能授权结果回传")
        self._require_active_guardian(guardian_id, child_id)
        review_id = f"review-{activity_id}-{child_id}"
        scopes = frozenset({ConsentScope.RESULT_RETURN})
        for grant in self.consents:
            if grant.review_id == review_id and grant.effective:
                return review_id
        self.consents.append(
            ConsentGrant(
                consent_id=f"consent-return-{review_id}",
                activity_id=activity_id,
                child_id=child_id,
                guardian_id=guardian_id,
                scopes=scopes,
                review_id=review_id,
            )
        )
        return review_id

    def submit_review_result(
        self,
        activity_id: str,
        child_id: str,
        review_id: str,
        *,
        outcome: ReviewOutcome | str,
        institution_id: str,
        note: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> ReviewResult:
        record = self._require_record(activity_id, child_id)
        if record.closed_at is not None:
            raise WorkflowStateError("档案已结案，不能再次回传结果（如需更正须走更正流程）")
        # 闭环硬门槛：没有本次复核的监护授权，医院结果不得回传
        self._require_consent(
            activity_id,
            child_id,
            ConsentScope.RESULT_RETURN,
            review_id=review_id,
        )
        if record.intent is not GuardianIntent.WANT_REVIEW:
            raise WorkflowStateError("该档案未进入医院复核流程")
        if record.review is not None:
            raise WorkflowStateError("复核结论已回传，不得用第二份结论覆盖")

        extra = extra or {}
        forbidden = set(extra) - REVIEW_ALLOWED_EXTRA
        if forbidden & _TREATMENT_KEYS or forbidden & _DIAGNOSIS_KEYS:
            raise ForbiddenFieldError(
                "回传内容只能是四分类结论，具体治疗方案不得写入本系统："
                f"{'、'.join(sorted(forbidden))}"
            )
        if forbidden:
            raise ForbiddenFieldError(f"回传不支持的字段：{sorted(forbidden)}")

        outcome = _coerce(outcome, ReviewOutcome)
        result = ReviewResult(
            review_id=review_id,
            outcome=outcome,
            institution_id=institution_id,
            returned_at=datetime.now(timezone.utc),
            note=note,
        )
        record.review = result
        record.resolve_queue(QueueKind.OVERDUE_REVIEW)
        return result

    def acknowledge_result(
        self, activity_id: str, child_id: str, guardian_id: str
    ) -> None:
        """监护人查收医院反馈后结案。"""
        record = self._require_record(activity_id, child_id)
        if record.review is None:
            raise WorkflowStateError("尚无回传结果可供查收")
        self._require_active_guardian(guardian_id, child_id)
        record.closed_at = datetime.now(timezone.utc)

    def guardian_explanation(self, activity_id: str, child_id: str) -> str:
        """给监护人的通俗风险说明，任何阶段都不导向“立即矫正”。"""
        record = self._require_record(activity_id, child_id)
        lines: list[str] = []

        if record.review is not None:
            lines.append(_PLAIN_OUTCOME[record.review.outcome])
            if record.review.note:
                lines.append(f"医院备注：{record.review.note}")
        else:
            obs = [_PLAIN_OBSERVATION[o] for o in record.observations] or [
                "本次肉眼观察没有需要特别说明的发现"
            ]
            lines.append("本次校园筛查看到的情况：" + "；".join(obs) + "。")
            if record.habits:
                habits = "；".join(_PLAIN_HABIT[h] for h in record.habits)
                lines.append(f"日常护理方面：{habits}。")
            lines.append(
                "六周岁前后开始换牙，牙列看起来不齐在这个阶段很常见，"
                "是否需要矫治要由医院检查后判断，校园提示本身不是诊断。"
            )
            if record.intent is GuardianIntent.PENDING:
                lines.append("您可以选择医院复核、暂不需要后续联系，或补充既往情况。")

        if record.supplements:
            lines.append("我们已记录您补充的既往情况，并会提供给复核医生参考。")
        lines.append(_FOOTER)
        return "\n".join(lines)

    # ------------------------------------------------------------------ 队列
    def mark_transfer(
        self, activity_id: str, child_id: str, new_school_id: str
    ) -> None:
        record = self._require_record(activity_id, child_id)
        record.transferred_to_school = new_school_id
        record.enqueue(
            QueueKind.TRANSFER, f"儿童转学至 {new_school_id}，档案随学籍移交"
        )

    def change_guardianship(
        self,
        child_id: str,
        removed_guardian_id: str,
        new_guardian: Guardian | None = None,
    ) -> None:
        """监护关系变化：旧监护授权撤回、通知按新人重新走，全程留队。"""
        guardian = self.guardians.get(removed_guardian_id)
        if guardian is None or guardian.child_id != child_id:
            raise WorkflowStateError("监护关系不存在")
        guardian.active = False
        for grant in self.consents:
            if grant.guardian_id == removed_guardian_id and grant.effective:
                grant.revoked_at = datetime.now(timezone.utc)

        if new_guardian is not None:
            if new_guardian.child_id != child_id:
                raise WorkflowStateError("新监护人必须关联同一儿童")
            self.guardians[new_guardian.guardian_id] = new_guardian

        # 该儿童所有未结档案进入监护关系变化队列
        changed_at = datetime.now(timezone.utc)
        for record in self._records_of_child(child_id):
            record.guardian_changed_at = changed_at
            record.enqueue(
                QueueKind.GUARDIAN_CHANGE,
                f"监护人 {removed_guardian_id} 监护关系终止，需重新确认通知与授权",
            )

    def resolve_guardian_change_after_reconsent(
        self, activity_id: str, child_id: str
    ) -> None:
        """新监护格局补齐授权后，监护关系变化队列才能解除。

        必须存在一份在变更时间点“之后”授予的有效通知授权——变更前的
        旧授权不算数，确保通知对象按新监护关系重新确认过。
        """
        record = self._require_record(activity_id, child_id)
        if record.guardian_changed_at is None:
            raise WorkflowStateError("该档案没有待处理的监护关系变化")
        reconfirmed = any(
            grant.covers(ConsentScope.NOTIFY)
            and grant.granted_at > record.guardian_changed_at
            and self.guardians.get(grant.guardian_id, None) is not None
            and self.guardians[grant.guardian_id].active
            for grant in self.consents
            if grant.activity_id == activity_id and grant.child_id == child_id
        )
        if not reconfirmed:
            raise ConsentRequiredError(
                "监护关系变化后尚无监护人重新确认通知授权，队列不能解除"
            )
        record.resolve_queue(QueueKind.GUARDIAN_CHANGE)

    def sweep_overdue(self, now: datetime | None = None) -> list[str]:
        """把已选择复核但超过期限仍无结果的档案送入逾期队列。"""
        now = now or datetime.now(timezone.utc)
        overdue: list[str] = []
        for record in self.records.values():
            if (
                record.intent is GuardianIntent.WANT_REVIEW
                and record.review is None
                and record.review_due_at is not None
                and record.review_due_at < now
                and not record.has_queue(QueueKind.OVERDUE_REVIEW)
                and not record.has_queue(QueueKind.TRANSFER)
            ):
                record.enqueue(
                    QueueKind.OVERDUE_REVIEW,
                    f"超过 {record.review_due_at:%Y-%m-%d} 仍未收到医院复核结果",
                )
                overdue.append(record.ref)
        return overdue

    def open_queue(self, kind: QueueKind) -> list[ScreeningRecord]:
        return [r for r in self.records.values() if r.has_queue(kind)]

    # ------------------------------------------------------------------ 视图
    def school_view(self, school_id: str) -> list[dict[str, str]]:
        """学校端：只看得到每份档案的完成状态，看不到任何观察/诊断内容。"""
        return [
            {"ref": r.ref, "status": r.status.value}
            for r in self.records.values()
            if r.school_id == school_id
        ]

    def health_coverage_view(self) -> dict[str, dict[str, Any]]:
        """卫生人员端：按活动的去标识化覆盖差异，无儿童与学校身份信息。"""
        buckets: dict[str, dict[str, Any]] = {}
        for activity_id in self.activities:
            records = [
                r for r in self.records.values() if r.activity_id == activity_id
            ]
            status_counter: Counter[str] = Counter(r.status.value for r in records)
            outcome_counter: Counter[str] = Counter(
                r.review.outcome.value for r in records if r.review is not None
            )
            queue_counter: Counter[str] = Counter(
                q.kind.value
                for r in records
                for q in r.queues
                if q.open
            )

            screened = len(records)
            sent = delivered = viewed = failed = 0
            eligible_guardians = 0
            records_any_view = 0
            records_partial_view = 0
            for r in records:
                notes = list(r.notifications.values())
                eligible_guardians += len(
                    [
                        g
                        for g in self._active_guardians(r.child_id)
                        if self._consent_for(
                            r.activity_id,
                            r.child_id,
                            ConsentScope.NOTIFY,
                            guardian_id=g.guardian_id,
                        )
                    ]
                )
                sent += len(notes)
                delivered += sum(
                    n.state
                    in (NotificationState.DELIVERED, NotificationState.VIEWED)
                    for n in notes
                )
                viewed_count = sum(
                    n.state is NotificationState.VIEWED for n in notes
                )
                viewed += viewed_count
                failed += sum(n.state is NotificationState.FAILED for n in notes)
                if viewed_count > 0:
                    records_any_view += 1
                # 已发出但并非每位监护人都查收（复现“另一位从未看到”）
                if notes and viewed_count < len(notes):
                    records_partial_view += 1

            # 覆盖差异：筛查了多少、通知触达多少、查收多少、结果回传多少
            returned = sum(r.review is not None for r in records)
            buckets[activity_id] = {
                "screened": screened,
                "eligible_guardians": eligible_guardians,
                "notifications_sent": sent,
                "notifications_delivered": delivered,
                "notifications_viewed": viewed,
                "notifications_failed": failed,
                "records_with_any_guardian_viewed": records_any_view,
                "results_returned": returned,
                "coverage_gap": {
                    "screened_but_not_viewed": screened - records_any_view,
                    "partial_guardian_view": records_partial_view,
                    "referred_but_no_result": sum(
                        r.intent is GuardianIntent.WANT_REVIEW and r.review is None
                        for r in records
                    ),
                },
                "status_breakdown": dict(status_counter),
                "outcome_breakdown": dict(outcome_counter),
                "open_queues": dict(queue_counter),
            }
        return buckets

    # ----------------------------------------------------------------- 内部
    def _require_activity(self, activity_id: str) -> Activity:
        activity = self.activities.get(activity_id)
        if activity is None:
            raise WorkflowStateError(f"活动不存在：{activity_id}")
        return activity

    def _require_record(self, activity_id: str, child_id: str) -> ScreeningRecord:
        record = self.records.get((activity_id, child_id))
        if record is None:
            raise WorkflowStateError("筛查档案不存在")
        return record

    def _active_guardians(self, child_id: str) -> list[Guardian]:
        return [
            g
            for g in self.guardians.values()
            if g.child_id == child_id and g.active
        ]

    def _records_of_child(self, child_id: str) -> list[ScreeningRecord]:
        return [
            r for r in self.records.values() if r.child_id == child_id
        ]

    def _require_active_guardian(
        self, guardian_id: str, child_id: str
    ) -> Guardian:
        guardian = self.guardians.get(guardian_id)
        if guardian is None or guardian.child_id != child_id:
            raise ConsentScopeError("监护关系不存在")
        if not guardian.active:
            raise ConsentScopeError("该监护关系已终止")
        return guardian

    def _require_notification(
        self, activity_id: str, child_id: str, guardian_id: str
    ) -> NotificationRecord:
        self._require_active_guardian(guardian_id, child_id)
        record = self._require_record(activity_id, child_id)
        note = record.notifications.get(guardian_id)
        if note is None:
            raise WorkflowStateError("尚未向该监护人发出通知")
        return note
