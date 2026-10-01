"""校园口腔筛查闭环的领域模型。

核心约束：
- 筛查环节只记录标准化提示（肉眼观察），不记录诊断或治疗方案；
- 诊断类结论只能来自医院复核反馈，且需监护授权后回传；
- 每个儿童在每场活动中只有一份档案，避免相互矛盾的多份记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class DentitionStage(str, Enum):
    """牙列阶段。"""

    PRIMARY = "乳牙列期"
    MIXED = "混合牙列期"
    PERMANENT = "恒牙列期"


class GuardianChoice(str, Enum):
    """家长查收筛查提示后可选的回执。"""

    HOSPITAL_REVIEW = "hospital_review"  # 选择医院复核
    REFUSE_CONTACT = "refuse_contact"  # 拒绝后续联系
    SUPPLEMENT_HISTORY = "supplement_history"  # 补充既往情况


class FeedbackCategory(str, Enum):
    """医院复核反馈的四类结论，只能由医疗机构给出。"""

    NO_ACTION = "no_action"  # 无需处理
    CONTINUE_OBSERVATION = "continue_observation"  # 继续观察
    CARIES_TREATMENT = "caries_treatment"  # 龋病处置
    ORTHODONTIC_EVALUATION = "orthodontic_evaluation"  # 专业正畸评估


class QueueKind(str, Enum):
    """需要人工跟进的五类队列。"""

    DUPLICATE_SCREENING = "duplicate_screening"  # 重复筛查
    TRANSFER = "transfer"  # 转学
    NOTIFICATION_FAILURE = "notification_failure"  # 通知失败
    GUARDIANSHIP_CHANGE = "guardianship_change"  # 监护关系变化
    OVERDUE_REVIEW = "overdue_review"  # 逾期未复核


class CaseStatus(str, Enum):
    """档案状态机。学校侧只能看到由它映射出的完成状态。"""

    SCREENED = "screened"  # 已登记筛查，等待家长回执
    AWAITING_HOSPITAL = "awaiting_hospital"  # 家长已选择医院复核，等待反馈
    COMPLETED = "completed"  # 医院反馈已回传，闭环完成
    CLOSED_REFUSED = "closed_refused"  # 家长拒绝后续联系


# 筛查环节只允许使用的标准化提示用语，避免筛查人员自行书写诊断。
STANDARD_OBSERVATIONS: frozenset[str] = frozenset(
    {
        "轻度牙列不齐",
        "牙列拥挤",
        "牙列稀疏",
        "深覆盖",
        "反合",
        "疑似龋坏",
        "牙面色素沉着",
        "牙龈红肿",
        "口腔卫生欠佳",
        "未见明显异常",
    }
)

# 诊断或治疗方案用语。筛查记录的任何文本字段都不得出现，
# 是否属于这些情况只能由医院复核判断。
FORBIDDEN_SCREENING_TERMS: tuple[str, ...] = (
    "诊断",
    "确诊",
    "治疗",
    "矫正",
    "正畸",
    "矫治",
    "拔牙",
    "处方",
    "手术",
)


class ScreeningError(Exception):
    """闭环流程的通用错误。"""


class ConsentRequiredError(ScreeningError):
    """未取得与本次活动对应的监护同意。"""


class ForbiddenContentError(ScreeningError):
    """筛查记录中出现诊断或治疗方案用语。"""


class UnknownObservationError(ScreeningError):
    """肉眼观察使用了非标准化提示用语。"""


class AuthorizationRequiredError(ScreeningError):
    """医疗机构结果未经监护授权，不能回传。"""


class InvalidStateError(ScreeningError):
    """当前档案状态不允许该操作。"""


class DuplicateFeedbackError(ScreeningError):
    """同一档案已存在医院反馈，不能生成第二份结论。"""


@dataclass
class Campaign:
    """一次校园筛查活动。监护同意与活动一一对应。"""

    campaign_id: str
    school_id: str
    title: str
    created_on: date


@dataclass
class Guardian:
    """监护人联系方式。contact_ok 模拟通知渠道是否可达。"""

    guardian_id: str
    relation: str  # 与儿童的关系，如“母亲”
    contact_ok: bool = True


@dataclass
class ChildRef:
    """最少身份信息：校内匿名编号 + 年龄，不保存姓名、证件号等。"""

    child_key: str
    school_id: str
    age_years: int


@dataclass
class Consent:
    """监护同意，与具体活动绑定，不能跨活动复用。"""

    campaign_id: str
    child_key: str
    guardian_id: str
    granted: bool
    decided_on: date


@dataclass
class ScreeningRecord:
    """筛查登记：只有牙列阶段、肉眼观察、护理习惯、建议复核原因。

    刻意不提供诊断、治疗方案字段，筛查人员无从写入。
    """

    dentition_stage: DentitionStage
    observations: tuple[str, ...]
    care_habits: tuple[str, ...]
    review_reason: str
    screener: str
    recorded_on: date


@dataclass
class GuardianResponse:
    """家长回执。"""

    guardian_id: str
    choices: frozenset[GuardianChoice]
    authorize_feedback: bool  # 是否授权医疗机构回传复核结果
    history_note: str | None  # 补充的既往情况
    responded_on: date


@dataclass
class HospitalFeedback:
    """医院复核反馈。"""

    category: FeedbackCategory
    facility: str
    note: str
    received_on: date


@dataclass
class Notification:
    """一条发给某位监护人的通知。每位监护人单独跟踪送达状态。"""

    campaign_id: str
    child_key: str
    guardian_id: str
    kind: str  # screening_result / feedback_ready
    delivered: bool
    sent_on: date


@dataclass
class CaseFile:
    """每个儿童在每场活动中的唯一档案。"""

    campaign_id: str
    child_key: str
    screening: ScreeningRecord
    status: CaseStatus = CaseStatus.SCREENED
    responses: list[GuardianResponse] = field(default_factory=list)
    history_notes: list[str] = field(default_factory=list)
    feedback_authorized: bool = False
    feedback: HospitalFeedback | None = None
    review_due_on: date | None = None
    guardianship_hold: bool = False  # 监护关系变化未处理完时暂停通知
    completed_on: date | None = None


@dataclass
class QueueEntry:
    """队列条目，由人工跟进后标记解决。"""

    entry_id: str
    kind: QueueKind
    child_key: str
    campaign_id: str | None
    reason: str
    created_on: date
    guardian_id: str | None = None
    resolved: bool = False
    resolution_note: str | None = None
