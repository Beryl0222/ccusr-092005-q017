"""端到端演示：一条“校园提示 -> 医院反馈”的完整链路。

数据全部来自 fixtures/seed.json 中已有的六岁儿童样例
（乳牙列期、轻度牙列不齐、偏软饮食、刷牙需协助）。

运行：python3 scripts/run_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oral_screening.enums import (  # noqa: E402
    CareHabit,
    ConsentScope,
    DentitionStage,
    FollowUpReason,
    GuardianIntent,
    Observation,
    QueueKind,
    ReviewOutcome,
)
from oral_screening.errors import (  # noqa: E402
    DuplicateScreeningError,
    ForbiddenFieldError,
)
from oral_screening.seed_loader import build_service_from_seed  # noqa: E402
from project_data import load_seed  # noqa: E402

ACTIVITY = "campus-oral-2026-spring"
CHILD = "child-case-31a"
MOTHER = "guardian-mother-31a"
FATHER = "guardian-father-31a"


def main() -> None:
    data = load_seed()
    svc = build_service_from_seed(data)

    case = next(r for r in data["records"] if r["kind"] == "oral_case")
    habit = next(r for r in data["records"] if r["kind"] == "habit")

    print("=" * 64)
    print("1) 筛查登记：只写牙列阶段、肉眼观察、护理习惯、复核原因")
    print("=" * 64)
    record = svc.record_screening(
        ACTIVITY,
        CHILD,
        {
            "dentition": DentitionStage(case["dentition"]),
            "observations": [Observation(o) for o in case["observations"]],
            "habits": [CareHabit(h) for h in habit["items"]],
            "follow_up_reasons": [FollowUpReason.CROWDING_BASELINE,
                                   FollowUpReason.HABIT_WATCH],
        },
        screened_by="screening-nurse-07",
    )
    print(f"已建档：{record.ref}，状态：{record.status.value}")

    print("\n-- 筛查端若夹带诊断/矫正方案，入口直接拒绝 --")
    try:
        svc.record_screening(
            ACTIVITY,
            CHILD,
            {
                "dentition": DentitionStage.PRIMARY,
                "诊断": "错颌畸形",
                "矫正方案": "立即佩戴矫治器",
            },
        )
    except ForbiddenFieldError as exc:
        print(f"已拒绝：{exc}")

    print("\n-- 同一活动重复提交：不建第二份档，进入重复筛查队列 --")
    try:
        svc.record_screening(
            ACTIVITY,
            CHILD,
            {"dentition": DentitionStage.PRIMARY},
        )
    except DuplicateScreeningError as exc:
        print(f"已去重：{exc}")
    record.resolve_queue(QueueKind.DUPLICATE)  # 运营核实后关闭该队列事项

    print("\n" + "=" * 64)
    print("2) 标准化提示：逐监护人发送，短信本身声明“不是诊断”")
    print("=" * 64)
    recipients = svc.send_notifications(ACTIVITY, CHILD)
    print(f"首批收件监护人：{recipients}  （父亲未授权通知，故不在列）")
    print(svc.render_notification(ACTIVITY, CHILD))

    print("\n-- 投诉核实后，父亲补授权；首条短信网关失败进入队列，重投成功 --")
    svc.grant_consent(
        ACTIVITY, CHILD, FATHER, [ConsentScope.SCREENING, ConsentScope.NOTIFY]
    )
    svc.send_notifications(ACTIVITY, CHILD)
    svc.mark_failed(ACTIVITY, CHILD, FATHER, "网关超时")
    print(f"当前开放队列：{[q.kind.value for q in record.queues if q.open]}")
    svc.retry_failed_notification(ACTIVITY, CHILD, FATHER)
    svc.mark_delivered(ACTIVITY, CHILD, FATHER)
    svc.mark_viewed(ACTIVITY, CHILD, FATHER)
    print(f"重投查收后开放队列：{[q.kind.value for q in record.queues if q.open]}")

    svc.mark_delivered(ACTIVITY, CHILD, MOTHER)
    svc.mark_viewed(ACTIVITY, CHILD, MOTHER)
    for gid, note in record.notifications.items():
        print(f"  {gid}: {note.state.value}（尝试 {note.attempts} 次）")

    print("\n" + "=" * 64)
    print("3) 家长查收：先看到易懂说明，再自行选择去向")
    print("=" * 64)
    print(svc.guardian_explanation(ACTIVITY, CHILD))

    print("\n-- 妈妈选择医院复核（系统不替她选正畸）--")
    svc.record_intent(ACTIVITY, CHILD, MOTHER, GuardianIntent.WANT_REVIEW)
    review_id = svc.authorize_result_return(ACTIVITY, CHILD, MOTHER)
    print(f"状态：{record.status.value}；本次复核授权号：{review_id}")

    print("\n" + "=" * 64)
    print("4) 医院复核：无监护授权不能回传；结论只能四选一")
    print("=" * 64)
    svc.submit_review_result(
        ACTIVITY,
        CHILD,
        review_id,
        outcome=ReviewOutcome.CONTINUE_WATCH,  # 与样例“随访观察”一致
        institution_id="stomatology-hospital-02",
        note="替牙期常见排列变化，6个月后复查，无需矫治",
    )
    print(f"回传后状态：{record.status.value}")
    svc.acknowledge_result(ACTIVITY, CHILD, MOTHER)
    print(f"家长查收反馈后状态：{record.status.value}")

    print("\n" + "=" * 64)
    print("5) 家长最终看到的说明")
    print("=" * 64)
    print(svc.guardian_explanation(ACTIVITY, CHILD))

    print("\n" + "=" * 64)
    print("6) 两类受限视图 + 单档校验")
    print("=" * 64)
    print("学校端（只见完成状态，无任何观察内容）：")
    for row in svc.school_view("school-001"):
        print(f"  {row}")
    print("\n卫生人员端（去标识化覆盖差异）：")
    coverage = svc.health_coverage_view()[ACTIVITY]
    for key, value in coverage.items():
        print(f"  {key}: {value}")

    assert len(svc.records) == 1, "同一(活动,儿童)只能有一份档案"
    print("\n单档校验通过：系统中仅 1 份档案，不存在相互矛盾的两份记录。")


if __name__ == "__main__":
    main()
