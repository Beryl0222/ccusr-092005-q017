"""校园口腔筛查闭环的核心服务。

闭环顺序：
1. 学校创建筛查活动，并取得与本次活动对应的监护同意；
2. 筛查人员用最少身份信息登记牙列阶段、肉眼观察、护理习惯和建议复核原因；
3. 系统通知全部监护人，家长回执选择医院复核 / 拒绝后续联系 / 补充既往情况；
4. 医疗机构结果经监护授权后回传，区分无需处理、继续观察、龋病处置、专业正畸评估。

重复筛查、转学、通知失败、监护关系变化、逾期未复核分别进入各自队列。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from .models import (
    FORBIDDEN_SCREENING_TERMS,
    STANDARD_OBSERVATIONS,
    AuthorizationRequiredError,
    Campaign,
    CaseFile,
    CaseStatus,
    ChildRef,
    Consent,
    ConsentRequiredError,
    DentitionStage,
    DuplicateFeedbackError,
    FeedbackCategory,
    ForbiddenContentError,
    Guardian,
    GuardianChoice,
    GuardianResponse,
    HospitalFeedback,
    InvalidStateError,
    Notification,
    QueueEntry,
    QueueKind,
    ScreeningError,
    ScreeningRecord,
    UnknownObservationError,
)


class ScreeningLoopService:
    """校园口腔筛查闭环的全部状态与操作。"""

    def __init__(self, *, today: date, review_deadline_days: int = 30) -> None:
        self._today = today
        self._review_deadline_days = review_deadline_days
        self.campaigns: dict[str, Campaign] = {}
        self.children: dict[str, ChildRef] = {}
        self.guardians: dict[str, dict[str, Guardian]] = {}
        self.consents: dict[tuple[str, str], Consent] = {}
        self.cases: dict[tuple[str, str], CaseFile] = {}
        self.notifications: list[Notification] = []
        self.queues: list[QueueEntry] = []
        self._entry_seq = 0

    # ------------------------------------------------------------------
    # 时钟（测试可推进，用于逾期未复核）
    # ------------------------------------------------------------------
    @property
    def today(self) -> date:
        return self._today

    def advance_days(self, days: int) -> None:
        self._today += timedelta(days=days)

    # ------------------------------------------------------------------
    # 活动与儿童登记
    # ------------------------------------------------------------------
    def create_campaign(self, campaign_id: str, *, school_id: str, title: str) -> Campaign:
        if campaign_id in self.campaigns:
            raise ScreeningError(f"活动已存在：{campaign_id}")
        campaign = Campaign(campaign_id, school_id, title, self._today)
        self.campaigns[campaign_id] = campaign
        return campaign

    def register_child(
        self,
        child_key: str,
        *,
        school_id: str,
        age_years: int,
        guardians: Iterable[Guardian],
    ) -> ChildRef:
        """以最少身份信息登记儿童，并登记全部监护人。"""
        if child_key in self.children:
            raise ScreeningError(f"儿童已登记：{child_key}")
        guardian_map = {g.guardian_id: g for g in guardians}
        if not guardian_map:
            raise ScreeningError("至少需要一位监护人，否则通知无法送达")
        child = ChildRef(child_key, school_id, age_years)
        self.children[child_key] = child
        self.guardians[child_key] = guardian_map
        return child

    def update_guardian_contact(self, child_key: str, guardian_id: str, *, contact_ok: bool) -> Guardian:
        guardian = self._guardian(child_key, guardian_id)
        guardian.contact_ok = contact_ok
        return guardian

    # ------------------------------------------------------------------
    # 监护同意（与活动一一对应）
    # ------------------------------------------------------------------
    def grant_consent(self, campaign_id: str, child_key: str, *, guardian_id: str) -> Consent:
        return self._record_consent(campaign_id, child_key, guardian_id=guardian_id, granted=True)

    def decline_consent(self, campaign_id: str, child_key: str, *, guardian_id: str) -> Consent:
        return self._record_consent(campaign_id, child_key, guardian_id=guardian_id, granted=False)

    def _record_consent(self, campaign_id: str, child_key: str, *, guardian_id: str, granted: bool) -> Consent:
        self._campaign(campaign_id)
        self._child(child_key)
        self._guardian(child_key, guardian_id)
        consent = Consent(campaign_id, child_key, guardian_id, granted, self._today)
        self.consents[(campaign_id, child_key)] = consent
        return consent

    # ------------------------------------------------------------------
    # 筛查登记
    # ------------------------------------------------------------------
    def register_screening(
        self,
        campaign_id: str,
        child_key: str,
        *,
        dentition_stage: str | DentitionStage,
        observations: Iterable[str],
        care_habits: Iterable[str],
        review_reason: str,
        screener: str,
    ) -> CaseFile:
        """登记筛查。重复登记进入重复筛查队列，并保留原档案不变。"""
        campaign = self._campaign(campaign_id)
        child = self._child(child_key)
        if child.school_id != campaign.school_id:
            raise ScreeningError("儿童已转出，不能登记到原学校的活动")
        consent = self.consents.get((campaign_id, child_key))
        if consent is None or not consent.granted:
            raise ConsentRequiredError("未取得与本次活动对应的监护同意，不能登记筛查")

        key = (campaign_id, child_key)
        existing = self.cases.get(key)
        if existing is not None:
            self._enqueue(
                QueueKind.DUPLICATE_SCREENING,
                child_key,
                campaign_id,
                "同一儿童在同一活动中的重复筛查登记，已保留原档案",
            )
            return existing

        stage = DentitionStage(dentition_stage)
        obs = tuple(observations)
        if not obs:
            raise ScreeningError("肉眼观察不能为空")
        unknown = [o for o in obs if o not in STANDARD_OBSERVATIONS]
        if unknown:
            raise UnknownObservationError(f"肉眼观察必须使用标准化提示用语，收到：{'、'.join(unknown)}")
        habits = tuple(care_habits)
        self._guard_screening_text("建议复核原因", review_reason)
        for habit in habits:
            self._guard_screening_text("护理习惯", habit)
        if not review_reason.strip():
            raise ScreeningError("建议复核原因不能为空")

        record = ScreeningRecord(stage, obs, habits, review_reason.strip(), screener, self._today)
        case = CaseFile(campaign_id=campaign_id, child_key=child_key, screening=record)
        self.cases[key] = case
        self._notify_guardians(case, kind="screening_result")
        return case

    @staticmethod
    def _guard_screening_text(field_name: str, text: str) -> None:
        for term in FORBIDDEN_SCREENING_TERMS:
            if term in text:
                raise ForbiddenContentError(f"{field_name}不得包含诊断或治疗方案用语：{term}")

    # ------------------------------------------------------------------
    # 家长回执
    # ------------------------------------------------------------------
    def guardian_respond(
        self,
        campaign_id: str,
        child_key: str,
        guardian_id: str,
        *,
        choices: Iterable[str | GuardianChoice],
        authorize_feedback: bool = False,
        history_note: str | None = None,
    ) -> CaseFile:
        """家长查收通知后的回执：医院复核 / 拒绝后续联系 / 补充既往情况。"""
        case = self._case(campaign_id, child_key)
        if case.status in (CaseStatus.COMPLETED, CaseStatus.CLOSED_REFUSED):
            raise InvalidStateError("档案已关闭，不能再回执")
        self._guardian(child_key, guardian_id)
        if not self._has_delivered_notification(case, guardian_id):
            raise InvalidStateError("该监护人尚未查收通知，不能回执")

        parsed = {GuardianChoice(c) for c in choices}
        if not parsed:
            raise ScreeningError("回执选择不能为空")
        if GuardianChoice.HOSPITAL_REVIEW in parsed and GuardianChoice.REFUSE_CONTACT in parsed:
            raise ScreeningError("回执选择相互矛盾：不能同时选择医院复核与拒绝后续联系")
        if GuardianChoice.SUPPLEMENT_HISTORY in parsed and not (history_note and history_note.strip()):
            raise ScreeningError("补充既往情况需填写具体内容")
        if authorize_feedback and GuardianChoice.HOSPITAL_REVIEW not in parsed:
            raise ScreeningError("只有选择医院复核时才需要授权医疗机构回传结果")

        response = GuardianResponse(
            guardian_id,
            frozenset(parsed),
            authorize_feedback,
            history_note.strip() if history_note else None,
            self._today,
        )
        case.responses.append(response)
        if response.history_note:
            case.history_notes.append(response.history_note)

        if GuardianChoice.HOSPITAL_REVIEW in parsed:
            case.status = CaseStatus.AWAITING_HOSPITAL
            case.review_due_on = self._today + timedelta(days=self._review_deadline_days)
            if authorize_feedback:
                case.feedback_authorized = True
        elif GuardianChoice.REFUSE_CONTACT in parsed:
            chose_review = any(GuardianChoice.HOSPITAL_REVIEW in r.choices for r in case.responses)
            if not chose_review:
                case.status = CaseStatus.CLOSED_REFUSED
        return case

    # ------------------------------------------------------------------
    # 医院反馈（需监护授权）
    # ------------------------------------------------------------------
    def submit_hospital_feedback(
        self,
        campaign_id: str,
        child_key: str,
        *,
        category: str | FeedbackCategory,
        facility: str,
        note: str = "",
    ) -> CaseFile:
        case = self._case(campaign_id, child_key)
        if case.status is CaseStatus.CLOSED_REFUSED:
            raise InvalidStateError("家长已拒绝后续联系，不能回传结果")
        if case.feedback is not None:
            raise DuplicateFeedbackError("同一档案已有医院反馈，不能生成第二份结论")
        if case.status is not CaseStatus.AWAITING_HOSPITAL:
            raise InvalidStateError("家长尚未选择医院复核，不能回传结果")
        if not case.feedback_authorized:
            raise AuthorizationRequiredError("医疗机构结果需经监护授权后才能回传")
        case.feedback = HospitalFeedback(FeedbackCategory(category), facility, note, self._today)
        case.status = CaseStatus.COMPLETED
        case.completed_on = self._today
        self._notify_guardians(case, kind="feedback_ready")
        return case

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------
    def _notify_guardians(self, case: CaseFile, *, kind: str) -> None:
        """通知全部监护人，逐人跟踪送达状态，失败进入通知失败队列。"""
        refused = {r.guardian_id for r in case.responses if GuardianChoice.REFUSE_CONTACT in r.choices}
        for guardian in self.guardians.get(case.child_key, {}).values():
            if guardian.guardian_id in refused:
                continue
            delivered = guardian.contact_ok and not case.guardianship_hold
            self.notifications.append(
                Notification(case.campaign_id, case.child_key, guardian.guardian_id, kind, delivered, self._today)
            )
            if not delivered and not case.guardianship_hold:
                self._enqueue(
                    QueueKind.NOTIFICATION_FAILURE,
                    case.child_key,
                    case.campaign_id,
                    f"监护人 {guardian.guardian_id} 的{kind}通知未送达",
                    guardian_id=guardian.guardian_id,
                )

    def _has_delivered_notification(self, case: CaseFile, guardian_id: str) -> bool:
        return any(
            n.campaign_id == case.campaign_id and n.child_key == case.child_key and n.guardian_id == guardian_id and n.delivered
            for n in self.notifications
        )

    def retry_failed_notifications(self) -> int:
        """重试未送达的通知（渠道恢复或监护关系冻结解除后）。返回新送达数量。"""
        newly_delivered = 0
        for notification in self.notifications:
            if notification.delivered:
                continue
            case = self.cases.get((notification.campaign_id, notification.child_key))
            if case is None or case.status is CaseStatus.CLOSED_REFUSED:
                continue
            if case.guardianship_hold:
                continue
            refused = {r.guardian_id for r in case.responses if GuardianChoice.REFUSE_CONTACT in r.choices}
            if notification.guardian_id in refused:
                continue
            guardian = self.guardians.get(notification.child_key, {}).get(notification.guardian_id)
            if guardian is None or not guardian.contact_ok:
                continue
            notification.delivered = True
            newly_delivered += 1
            for entry in self.open_entries(QueueKind.NOTIFICATION_FAILURE):
                if entry.child_key == notification.child_key and entry.guardian_id == notification.guardian_id:
                    self.resolve_entry(entry.entry_id, note="重试后送达")
        return newly_delivered

    # ------------------------------------------------------------------
    # 五类队列
    # ------------------------------------------------------------------
    def mark_transfer(self, child_key: str, *, to_school_id: str) -> QueueEntry:
        """转学：进入转学队列，儿童随档案转入新学校，原学校不再看到该儿童。"""
        child = self._child(child_key)
        entry = self._enqueue(
            QueueKind.TRANSFER,
            child_key,
            None,
            f"从 {child.school_id} 转入 {to_school_id}，档案随儿童迁移",
            dedupe=False,
        )
        child.school_id = to_school_id
        return entry

    def report_guardianship_change(
        self,
        child_key: str,
        *,
        reason: str,
        new_guardians: Iterable[Guardian] | None = None,
    ) -> QueueEntry:
        """监护关系变化：进入队列并暂停相关档案的通知，直到人工处理完毕。"""
        self._child(child_key)
        if new_guardians is not None:
            guardian_map = {g.guardian_id: g for g in new_guardians}
            if not guardian_map:
                raise ScreeningError("至少需要一位监护人，否则通知无法送达")
            self.guardians[child_key] = guardian_map
        entry = self._enqueue(QueueKind.GUARDIANSHIP_CHANGE, child_key, None, reason, dedupe=False)
        for case in self.cases.values():
            if case.child_key == child_key and case.status not in (CaseStatus.COMPLETED, CaseStatus.CLOSED_REFUSED):
                case.guardianship_hold = True
        return entry

    def sweep_overdue(self) -> list[QueueEntry]:
        """把逾期未复核的档案送入逾期队列（幂等，不重复入队）。"""
        created: list[QueueEntry] = []
        for case in self.cases.values():
            if case.status is not CaseStatus.AWAITING_HOSPITAL:
                continue
            if case.review_due_on is None or case.review_due_on >= self._today:
                continue
            already_queued = any(
                not e.resolved
                and e.kind is QueueKind.OVERDUE_REVIEW
                and e.child_key == case.child_key
                and e.campaign_id == case.campaign_id
                for e in self.queues
            )
            if already_queued:
                continue
            created.append(
                self._enqueue(
                    QueueKind.OVERDUE_REVIEW,
                    case.child_key,
                    case.campaign_id,
                    f"医院复核已逾期（应不晚于 {case.review_due_on.isoformat()}）",
                )
            )
        return created

    def open_entries(self, kind: QueueKind | None = None) -> list[QueueEntry]:
        return [e for e in self.queues if not e.resolved and (kind is None or e.kind is kind)]

    def resolve_entry(self, entry_id: str, *, note: str = "") -> QueueEntry:
        entry = next((e for e in self.queues if e.entry_id == entry_id), None)
        if entry is None:
            raise ScreeningError(f"未知队列条目：{entry_id}")
        if entry.resolved:
            return entry
        entry.resolved = True
        entry.resolution_note = note
        if entry.kind is QueueKind.GUARDIANSHIP_CHANGE:
            still_open = any(
                not e.resolved and e.kind is QueueKind.GUARDIANSHIP_CHANGE and e.child_key == entry.child_key
                for e in self.queues
            )
            if not still_open:
                for case in self.cases.values():
                    if case.child_key == entry.child_key:
                        case.guardianship_hold = False
        return entry

    def _enqueue(
        self,
        kind: QueueKind,
        child_key: str,
        campaign_id: str | None,
        reason: str,
        *,
        guardian_id: str | None = None,
        dedupe: bool = True,
    ) -> QueueEntry:
        if dedupe:
            for entry in self.queues:
                if (
                    not entry.resolved
                    and entry.kind is kind
                    and entry.child_key == child_key
                    and entry.campaign_id == campaign_id
                    and entry.guardian_id == guardian_id
                ):
                    return entry
        self._entry_seq += 1
        entry = QueueEntry(
            entry_id=f"qe-{self._entry_seq:04d}",
            kind=kind,
            child_key=child_key,
            campaign_id=campaign_id,
            reason=reason,
            created_on=self._today,
            guardian_id=guardian_id,
        )
        self.queues.append(entry)
        return entry

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    def case_file(self, campaign_id: str, child_key: str) -> CaseFile:
        return self._case(campaign_id, child_key)

    def _campaign(self, campaign_id: str) -> Campaign:
        campaign = self.campaigns.get(campaign_id)
        if campaign is None:
            raise ScreeningError(f"未知活动：{campaign_id}")
        return campaign

    def _child(self, child_key: str) -> ChildRef:
        child = self.children.get(child_key)
        if child is None:
            raise ScreeningError(f"未知儿童：{child_key}")
        return child

    def _guardian(self, child_key: str, guardian_id: str) -> Guardian:
        guardian = self.guardians.get(child_key, {}).get(guardian_id)
        if guardian is None:
            raise ScreeningError(f"儿童 {child_key} 没有监护人 {guardian_id}")
        return guardian

    def _case(self, campaign_id: str, child_key: str) -> CaseFile:
        case = self.cases.get((campaign_id, child_key))
        if case is None:
            raise ScreeningError(f"没有档案：{campaign_id}/{child_key}")
        return case
