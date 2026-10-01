"""领域错误。"""

from __future__ import annotations


class ScreeningError(Exception):
    """所有闭环规则冲突的基类。"""


class ConsentRequiredError(ScreeningError):
    """未取得与本次活动对应的监护同意即试图登记/回传。"""


class ConsentScopeError(ScreeningError):
    """同意存在但范围不匹配（活动不符、已撤回、监护人无权）。"""


class ForbiddenFieldError(ScreeningError):
    """写入方触碰了不属于其角色的字段。

    筛查人员不得写入诊断或治疗方案；复核回传必须使用四分类结论，
    而不是自由文本治疗指令。
    """


class DuplicateScreeningError(ScreeningError):
    """同一活动内对同一儿童重复筛查（转入重复筛查队列而非建第二份档）。"""


class AuthorizationRequiredError(ScreeningError):
    """医疗机构结果缺少本次复核的监护授权。"""


class WorkflowStateError(ScreeningError):
    """在不允许的状态上执行操作（如未通知先回传）。"""
