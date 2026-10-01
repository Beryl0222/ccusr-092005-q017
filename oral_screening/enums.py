"""受控词表。

筛查端只能使用这些标准化取值；自由文本的诊断意见与治疗方案不属于
校园筛查的可写字段（见 :class:`oral_screening.errors.ForbiddenFieldError`）。
"""

from __future__ import annotations

import enum


class DentitionStage(enum.Enum):
    """牙列阶段。"""

    PRIMARY = "乳牙列期"
    MIXED = "混合牙列期"
    PERMANENT = "恒牙列期"
    UNKNOWN = "无法判断"


class Observation(enum.Enum):
    """肉眼观察项（非诊断）。

    名称对齐现有样例中的“轻度牙列不齐”。这是肉眼可见的排列描述，
    不是“错颌畸形”等诊断，也不暗示需要矫正。
    """

    MILD_CROWDING = "轻度牙列不齐"
    OPEN_BITE = "开合倾向"
    CROSS_BITE = "反合倾向"
    LARGE_OVERJET = "深覆盖倾向"
    SPACING = "牙间隙偏大"
    VISIBLE_CARIES_SIGN = "可疑龋坏迹象"
    GINGIVAL_SIGN = "牙龈红肿迹象"
    NONE = "未见明显异常"


#: 护理习惯受控项，与现有习惯资料对齐（偏软饮食、刷牙需协助）。
class CareHabit(enum.Enum):
    SOFT_DIET = "偏软饮食"
    NEEDS_BRUSHING_HELP = "刷牙需协助"
    IRREGULAR_BRUSHING = "刷牙不规律"
    FREQUENT_SNACKS = "频繁甜食/含糖饮料"
    NIGHT_MILK = "夜间含奶瓶/奶睡"
    FINGER_HABIT = "吮指/咬唇习惯"
    MOUTH_BREATHING = "口呼吸"
    NO_SPECIAL_HABIT = "无特殊习惯"


class FollowUpReason(enum.Enum):
    """建议复核原因——只是“建议再看一眼”的理由，不是诊断。"""

    CROWDING_BASELINE = "牙列排列需留存基线"
    OCCLUSAL_DEVELOPMENT = "咬合发育需继续观察"
    CARIES_SIGN = "可疑龋坏需医院确认"
    HABIT_WATCH = "护理习惯可能影响发育"
    ROUTINE = "常规建议复核"


class ConsentScope(enum.Enum):
    """监护授权的范围，按“本次活动/本次复核”分别授予。"""

    SCREENING = "参加本次筛查并登记最小信息"
    NOTIFY = "接收本次活动的校园提示"
    RESULT_RETURN = "授权本次医院复核结果回传"


class GuardianIntent(enum.Enum):
    """监护人查收后的处置意向。"""

    WANT_REVIEW = "选择医院复核"
    DECLINE = "拒绝后续联系"
    SUPPLEMENT_HISTORY = "补充既往情况"
    PENDING = "尚未回应"


class ReviewOutcome(enum.Enum):
    """医院复核结论四分类（闭环要求的全部取值）。

    系统本身不得解释为“必须立即矫正”——正畸评估只是建议去向之一。
    """

    NO_ACTION = "无需处理"
    CONTINUE_WATCH = "继续观察"
    CARIES_TREATMENT = "龋病处置"
    ORTHO_EVALUATION = "专业正畸评估"


class NotificationState(enum.Enum):
    PENDING = "待发送"
    DELIVERED = "已送达"
    VIEWED = "已查收"
    FAILED = "送达失败"


class ScreeningStatus(enum.Enum):
    """档案对外的完成状态——学校端只能看到这一层。"""

    NOTIFIED = "已通知待查收"
    RESPONDED = "监护人已回应"
    REFERRED = "已安排复核"
    RESULT_RETURNED = "复核结果已回传"
    CLOSED = "已结案"
    IN_QUEUE = "队列处理中"


class QueueKind(enum.Enum):
    """五类运营队列。"""

    DUPLICATE = "重复筛查"
    TRANSFER = "转学"
    NOTIFY_FAILED = "通知失败"
    GUARDIAN_CHANGE = "监护关系变化"
    OVERDUE_REVIEW = "逾期未复核"
