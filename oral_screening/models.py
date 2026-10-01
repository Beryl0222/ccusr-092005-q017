"""实体模型（内存存储即可，重点在规则与字段边界）。

身份最小化：校园侧只保存班级与年龄等最小信息，用不透明令牌
（``child_id`` / ``guardian_id``）关联，不保存身份证号等强身份信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

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
    ScreeningStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Activity:
    """一次校园筛查活动。同意必须与具体活动绑定。"""

    activity_id: str
    school_id: str
    name: str
    occurs_at: datetime


@dataclass(frozen=True)
class Child:
    """在册儿童：仅保留最小身份信息。"""

    child_id: str
    school_id: str
    class_name: str
    age_years: int | None = None


@dataclass
class Guardian:
    """监护人。监护关系变化时 ``active`` 置 False，而不是删除历史。"""

    guardian_id: str
    child_id: str
    label: str  # 例如“母亲”，仅用于通知称谓
    contact_ref: str  # 联系方式的不透明引用（由通知网关持有真实号码）
    relationship: str = "监护人"
    active: bool = True


@dataclass
class ConsentGrant:
    """一次“与本次活动对应”的监护授权。

    - SCREENING/NOTIFY 在筛查前取得，绑定 ``activity_id``；
    - RESULT_RETURN 在家长选择医院复核时单独授予，绑定一次复核。
    """

    consent_id: str
    activity_id: str
    child_id: str
    guardian_id: str
    scopes: frozenset[ConsentScope]
    granted_at: datetime = field(default_factory=_now)
    review_id: str | None = None  # RESULT_RETURN 时绑定具体复核
    revoked_at: datetime | None = None

    @property
    def effective(self) -> bool:
        return self.revoked_at is None

    def covers(self, scope: ConsentScope) -> bool:
        return self.effective and scope in self.scopes


@dataclass
class NotificationRecord:
    """按“每位监护人”独立追踪的一条校园提示。"""

    guardian_id: str
    state: NotificationState = NotificationState.PENDING
    attempts: int = 0
    last_error: str | None = None
    updated_at: datetime = field(default_factory=_now)
    viewed_at: datetime | None = None


@dataclass
class HistorySupplement:
    """监护人补充的既往情况——独立保存，不改写筛查观察。"""

    guardian_id: str
    text: str
    at: datetime = field(default_factory=_now)


@dataclass
class ReviewResult:
    """医疗机构回传的复核结果：只有四分类结论 + 安排信息。

    回传方不能写入自由文本治疗方案；龋病处置/正畸评估都只是“去向”，
    具体方案由接诊医疗机构在院内系统作出。
    """

    review_id: str
    outcome: ReviewOutcome
    institution_id: str
    returned_at: datetime
    note: str | None = None  # 仅允许非指令性说明，服务层会拒绝治疗方案式文本


@dataclass
class QueueItem:
    kind: QueueKind
    ref: str  # (活动, 儿童) 档案引用
    note: str
    created_at: datetime = field(default_factory=_now)
    resolved_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.resolved_at is None


@dataclass
class ScreeningRecord:
    """一份筛查档案。(activity_id, child_id) 全局唯一，绝不重复建档。"""

    activity_id: str
    child_id: str
    school_id: str
    class_name: str

    # —— 筛查人员可写的全部字段（观察类，无诊断、无治疗方案）——
    dentition: DentitionStage | None = None
    observations: list[Observation] = field(default_factory=list)
    habits: list[CareHabit] = field(default_factory=list)
    follow_up_reasons: list[FollowUpReason] = field(default_factory=list)
    screened_by: str | None = None
    screened_at: datetime | None = None

    # —— 后续环节写入，筛查端不可触碰 ——
    notifications: dict[str, NotificationRecord] = field(default_factory=dict)
    intent: GuardianIntent = GuardianIntent.PENDING
    intent_guardian_id: str | None = None
    intent_at: datetime | None = None
    supplements: list[HistorySupplement] = field(default_factory=list)
    review: ReviewResult | None = None
    review_due_at: datetime | None = None  # 选择复核后的 SLA 截止时间
    closed_at: datetime | None = None

    # —— 转学与队列 ——
    transferred_to_school: str | None = None
    guardian_changed_at: datetime | None = None
    queues: list[QueueItem] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.activity_id}:{self.child_id}"

    @property
    def open_queue_kinds(self) -> set[QueueKind]:
        return {q.kind for q in self.queues if q.open}

    def has_queue(self, kind: QueueKind) -> bool:
        return any(q.kind is kind and q.open for q in self.queues)

    def enqueue(self, kind: QueueKind, note: str) -> QueueItem:
        # 同类未结队列不重复开立
        for item in self.queues:
            if item.kind is kind and item.open:
                return item
        item = QueueItem(kind=kind, ref=self.ref, note=note)
        self.queues.append(item)
        return item

    def resolve_queue(self, kind: QueueKind) -> None:
        for item in self.queues:
            if item.kind is kind and item.open:
                item.resolved_at = _now()

    @property
    def status(self) -> ScreeningStatus:
        if self.open_queue_kinds:
            return ScreeningStatus.IN_QUEUE
        if self.closed_at is not None:
            return ScreeningStatus.CLOSED
        if self.review is not None:
            return ScreeningStatus.RESULT_RETURNED
        if self.intent is GuardianIntent.WANT_REVIEW:
            return ScreeningStatus.REFERRED
        if self.intent is not GuardianIntent.PENDING:
            return ScreeningStatus.RESPONDED
        return ScreeningStatus.NOTIFIED
