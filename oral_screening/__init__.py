"""校园口腔筛查闭环后端。

围绕一条强制链路组织：

    活动监护同意 -> 最小信息登记（不含诊断/治疗）-> 标准化提示通知
    -> 监护人查收与处置意向 -> 监护授权下的医院复核回传
    -> 监护人易懂说明 + 单档结案/转队列

角色上，筛查人员只能写入观察类字段，医疗机构只能在授权范围内回传
四类复核结论之一，学校只见完成状态，卫生人员只见去标识化覆盖差异。
重复筛查、转学、通知失败、监护关系变化、逾期未复核各有独立队列。
"""

from __future__ import annotations

from .enums import (
    CareHabit,
    DentitionStage,
    FollowUpReason,
    Observation,
    ReviewOutcome,
    ScreeningStatus,
)
from .errors import ScreeningError
from .service import ScreeningService

__all__ = [
    "CareHabit",
    "DentitionStage",
    "FollowUpReason",
    "Observation",
    "ReviewOutcome",
    "ScreeningStatus",
    "ScreeningError",
    "ScreeningService",
]
