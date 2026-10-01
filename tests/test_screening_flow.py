"""闭环规则测试。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from oral_screening.enums import (
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
from oral_screening.errors import (
    AuthorizationRequiredError,
    ConsentRequiredError,
    ConsentScopeError,
    DuplicateScreeningError,
    ForbiddenFieldError,
    WorkflowStateError,
)
from oral_screening.models import Guardian
from oral_screening.service import ScreeningService

ACT = "act-1"
SCHOOL = "sch-1"
CHILD = "kid-1"
MOTHER = "mom-1"
FATHER = "dad-1"


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def _ready_service(
    *,
    mother_scopes=(ConsentScope.SCREENING, ConsentScope.NOTIFY),
    father_scopes=(),
    review_sla: timedelta | None = None,
) -> ScreeningService:
    svc = ScreeningService() if review_sla is None else ScreeningService(review_sla=review_sla)
    svc.register_activity(ACT, SCHOOL, "春季筛查", _utc(2026, 3, 1))
    svc.register_child(CHILD, SCHOOL, "一(1)班", age_years=6)
    svc.register_guardian(MOTHER, CHILD, "妈妈", "gw://mom")
    svc.register_guardian(FATHER, CHILD, "爸爸", "gw://dad")
    if mother_scopes:
        svc.grant_consent(ACT, CHILD, MOTHER, mother_scopes)
    if father_scopes:
        svc.grant_consent(ACT, CHILD, FATHER, father_scopes)
    return svc


def _screen(svc: ScreeningService) -> None:
    svc.record_screening(
        ACT,
        CHILD,
        {
            "dentition": DentitionStage.PRIMARY,
            "observations": [Observation.MILD_CROWDING],
            "habits": [CareHabit.SOFT_DIET, CareHabit.NEEDS_BRUSHING_HELP],
            "follow_up_reasons": [FollowUpReason.CROWDING_BASELINE],
        },
        screened_by="nurse-1",
    )


class ConsentGateTest(unittest.TestCase):
    def test_screening_requires_activity_bound_consent(self) -> None:
        svc = ScreeningService()
        svc.register_activity(ACT, SCHOOL, "x", _utc(2026, 3, 1))
        svc.register_child(CHILD, SCHOOL, "班")
        svc.register_guardian(MOTHER, CHILD, "妈", "gw://m")
        with self.assertRaises(ConsentRequiredError):
            _screen(svc)

    def test_consent_must_match_activity(self) -> None:
        svc = _ready_service()
        svc.register_activity("act-2", SCHOOL, "另一次", _utc(2026, 9, 1))
        with self.assertRaises(ConsentRequiredError):
            svc.record_screening(
                "act-2",
                CHILD,
                {"dentition": DentitionStage.PRIMARY},
            )

    def test_revoked_consent_blocks_notification(self) -> None:
        svc = _ready_service()
        grant = svc.grant_consent(
            ACT, CHILD, FATHER, [ConsentScope.NOTIFY]
        )
        svc.revoke_consent(grant.consent_id)
        _screen(svc)
        # 母亲仍可通知；父亲授权已撤回
        recipients = svc.send_notifications(ACT, CHILD)
        self.assertEqual(recipients, [MOTHER])

    def test_inactive_guardian_cannot_consent(self) -> None:
        svc = _ready_service()
        svc.guardians[MOTHER].active = False
        with self.assertRaises(ConsentScopeError):
            svc.grant_consent(ACT, CHILD, MOTHER, [ConsentScope.SCREENING])


class FieldBoundaryTest(unittest.TestCase):
    def test_diagnosis_and_treatment_are_rejected(self) -> None:
        svc = _ready_service()
        with self.assertRaises(ForbiddenFieldError):
            svc.record_screening(
                ACT,
                CHILD,
                {
                    "dentition": DentitionStage.PRIMARY,
                    "诊断": "错颌畸形",
                },
            )
        with self.assertRaises(ForbiddenFieldError):
            svc.record_screening(
                ACT,
                CHILD,
                {
                    "dentition": DentitionStage.PRIMARY,
                    "矫正方案": "立即矫治",
                },
            )

    def test_unknown_vocabulary_rejected(self) -> None:
        svc = _ready_service()
        with self.assertRaises(ForbiddenFieldError):
            svc.record_screening(
                ACT, CHILD, {"dentition": "重度错颌必须矫正"}
            )

    def test_extra_fields_rejected(self) -> None:
        svc = _ready_service()
        with self.assertRaises(ForbiddenFieldError):
            svc.record_screening(
                ACT, CHILD, {"dentition": DentitionStage.PRIMARY, "x": 1}
            )

    def test_result_only_four_outcomes(self) -> None:
        svc = _ready_service()
        _screen(svc)
        svc.send_notifications(ACT, CHILD)
        svc.mark_delivered(ACT, CHILD, MOTHER)
        svc.mark_viewed(ACT, CHILD, MOTHER)
        svc.record_intent(ACT, CHILD, MOTHER, GuardianIntent.WANT_REVIEW)
        rid = svc.authorize_result_return(ACT, CHILD, MOTHER)
        with self.assertRaises(ForbiddenFieldError):
            svc.submit_review_result(
                ACT,
                CHILD,
                rid,
                outcome="必须立即正畸",
                institution_id="h-1",
            )
        with self.assertRaises(ForbiddenFieldError):
            svc.submit_review_result(
                ACT,
                CHILD,
                rid,
                outcome=ReviewOutcome.NO_ACTION,
                institution_id="h-1",
                extra={"治疗方案": "拔牙矫正"},
            )


class NotificationTest(unittest.TestCase):
    def test_each_guardian_tracked_separately(self) -> None:
        svc = _ready_service(
            mother_scopes=(ConsentScope.SCREENING, ConsentScope.NOTIFY),
            father_scopes=(ConsentScope.SCREENING, ConsentScope.NOTIFY),
        )
        _screen(svc)
        recipients = svc.send_notifications(ACT, CHILD)
        self.assertEqual(set(recipients), {MOTHER, FATHER})

        svc.mark_delivered(ACT, CHILD, MOTHER)
        svc.mark_viewed(ACT, CHILD, MOTHER)
        svc.mark_failed(ACT, CHILD, FATHER, "停机")

        record = svc.records[(ACT, CHILD)]
        self.assertIs(
            record.notifications[MOTHER].state, NotificationState.VIEWED
        )
        self.assertIs(
            record.notifications[FATHER].state, NotificationState.FAILED
        )
        self.assertTrue(record.has_queue(QueueKind.NOTIFY_FAILED))

        svc.retry_failed_notification(ACT, CHILD, FATHER)
        svc.mark_delivered(ACT, CHILD, FATHER)
        svc.mark_viewed(ACT, CHILD, FATHER)
        self.assertFalse(record.has_queue(QueueKind.NOTIFY_FAILED))

    def test_consentless_guardian_never_notified(self) -> None:
        # 复现“另一位监护人从未看到通知”
        svc = _ready_service()
        _screen(svc)
        self.assertEqual(svc.send_notifications(ACT, CHILD), [MOTHER])

    def test_notification_is_non_diagnostic(self) -> None:
        svc = _ready_service()
        _screen(svc)
        text = svc.render_notification(ACT, CHILD)
        self.assertIn("不是医院诊断", text)
        self.assertIn("不代表孩子需要立即矫正", text)

    def test_intent_requires_view(self) -> None:
        svc = _ready_service()
        _screen(svc)
        svc.send_notifications(ACT, CHILD)
        with self.assertRaises(WorkflowStateError):
            svc.record_intent(ACT, CHILD, MOTHER, GuardianIntent.WANT_REVIEW)


class ReviewLoopTest(unittest.TestCase):
    def _to_review(self, svc: ScreeningService) -> str:
        _screen(svc)
        svc.send_notifications(ACT, CHILD)
        svc.mark_delivered(ACT, CHILD, MOTHER)
        svc.mark_viewed(ACT, CHILD, MOTHER)
        svc.record_intent(ACT, CHILD, MOTHER, GuardianIntent.WANT_REVIEW)
        return svc.authorize_result_return(ACT, CHILD, MOTHER)

    def test_result_requires_authorization(self) -> None:
        svc = _ready_service()
        _screen(svc)
        svc.send_notifications(ACT, CHILD)
        svc.mark_delivered(ACT, CHILD, MOTHER)
        svc.mark_viewed(ACT, CHILD, MOTHER)
        svc.record_intent(ACT, CHILD, MOTHER, GuardianIntent.WANT_REVIEW)
        with self.assertRaises(AuthorizationRequiredError):
            svc.submit_review_result(
                ACT,
                CHILD,
                "review-act-1-kid-1",
                outcome=ReviewOutcome.ORTHO_EVALUATION,
                institution_id="h-1",
            )

    def test_full_loop_four_outcomes_and_close(self) -> None:
        for outcome in ReviewOutcome:
            svc = _ready_service()
            rid = self._to_review(svc)
            result = svc.submit_review_result(
                ACT,
                CHILD,
                rid,
                outcome=outcome,
                institution_id="h-1",
            )
            self.assertIs(result.outcome, outcome)
            record = svc.records[(ACT, CHILD)]
            self.assertIs(record.status, ScreeningStatus.RESULT_RETURNED)
            svc.acknowledge_result(ACT, CHILD, MOTHER)
            self.assertIs(record.status, ScreeningStatus.CLOSED)

    def test_no_second_contradictory_result(self) -> None:
        svc = _ready_service()
        rid = self._to_review(svc)
        svc.submit_review_result(
            ACT, CHILD, rid,
            outcome=ReviewOutcome.NO_ACTION, institution_id="h-1",
        )
        with self.assertRaises(WorkflowStateError):
            svc.submit_review_result(
                ACT, CHILD, rid,
                outcome=ReviewOutcome.ORTHO_EVALUATION, institution_id="h-1",
            )

    def test_decline_closes_and_blocks_return(self) -> None:
        svc = _ready_service()
        _screen(svc)
        svc.send_notifications(ACT, CHILD)
        svc.mark_delivered(ACT, CHILD, MOTHER)
        svc.mark_viewed(ACT, CHILD, MOTHER)
        svc.record_intent(ACT, CHILD, MOTHER, GuardianIntent.DECLINE)
        record = svc.records[(ACT, CHILD)]
        self.assertIs(record.status, ScreeningStatus.CLOSED)

    def test_supplement_keeps_loop_open(self) -> None:
        svc = _ready_service()
        _screen(svc)
        svc.send_notifications(ACT, CHILD)
        svc.mark_delivered(ACT, CHILD, MOTHER)
        svc.mark_viewed(ACT, CHILD, MOTHER)
        svc.record_intent(
            ACT, CHILD, MOTHER,
            GuardianIntent.SUPPLEMENT_HISTORY,
            supplement_text="之前乳牙磕到过",
        )
        record = svc.records[(ACT, CHILD)]
        self.assertIs(record.intent, GuardianIntent.PENDING)
        self.assertEqual(len(record.supplements), 1)
        # 之后仍可选择复核
        svc.record_intent(ACT, CHILD, MOTHER, GuardianIntent.WANT_REVIEW)
        self.assertIs(record.status, ScreeningStatus.REFERRED)

    def _both_parents_viewed(self, svc: ScreeningService) -> None:
        _screen(svc)
        svc.send_notifications(ACT, CHILD)
        for gid in (MOTHER, FATHER):
            svc.mark_delivered(ACT, CHILD, gid)
            svc.mark_viewed(ACT, CHILD, gid)

    def test_second_guardian_supplement_keeps_review_path(self) -> None:
        svc = _ready_service(
            father_scopes=(ConsentScope.SCREENING, ConsentScope.NOTIFY),
        )
        self._both_parents_viewed(svc)
        # 妈妈选择复核在先
        svc.record_intent(ACT, CHILD, MOTHER, GuardianIntent.WANT_REVIEW)
        # 爸爸随后只补充既往情况，不应把流程冲回“待回应”
        svc.record_intent(
            ACT, CHILD, FATHER,
            GuardianIntent.SUPPLEMENT_HISTORY,
            supplement_text="家族有正畸史",
        )
        record = svc.records[(ACT, CHILD)]
        self.assertIs(record.intent, GuardianIntent.WANT_REVIEW)
        self.assertIs(record.status, ScreeningStatus.REFERRED)
        self.assertEqual(len(record.supplements), 1)

    def test_intent_locked_after_result_returned(self) -> None:
        svc = _ready_service(
            father_scopes=(ConsentScope.SCREENING, ConsentScope.NOTIFY),
        )
        self._both_parents_viewed(svc)
        svc.record_intent(ACT, CHILD, MOTHER, GuardianIntent.WANT_REVIEW)
        rid = svc.authorize_result_return(ACT, CHILD, MOTHER)
        svc.submit_review_result(
            ACT, CHILD, rid,
            outcome=ReviewOutcome.CARIES_TREATMENT, institution_id="h-1",
        )
        with self.assertRaises(WorkflowStateError):
            svc.record_intent(ACT, CHILD, FATHER, GuardianIntent.DECLINE)
        with self.assertRaises(WorkflowStateError):
            svc.record_intent(
                ACT, CHILD, FATHER,
                GuardianIntent.SUPPLEMENT_HISTORY, supplement_text="x",
            )


class QueueTest(unittest.TestCase):
    def test_duplicate_screening_dedupes_to_queue(self) -> None:
        svc = _ready_service()
        _screen(svc)
        with self.assertRaises(DuplicateScreeningError):
            _screen(svc)
        self.assertEqual(len(svc.records), 1)
        record = svc.records[(ACT, CHILD)]
        self.assertTrue(record.has_queue(QueueKind.DUPLICATE))
        self.assertIs(record.status, ScreeningStatus.IN_QUEUE)

    def test_transfer_queue(self) -> None:
        svc = _ready_service()
        _screen(svc)
        svc.mark_transfer(ACT, CHILD, "sch-2")
        record = svc.records[(ACT, CHILD)]
        self.assertTrue(record.has_queue(QueueKind.TRANSFER))
        self.assertEqual(record.transferred_to_school, "sch-2")

    def test_guardian_change_revokes_and_queues(self) -> None:
        svc = _ready_service()
        _screen(svc)
        new_dad = Guardian("dad-2", CHILD, "继父", "gw://dad2")
        svc.change_guardianship(CHILD, FATHER, new_dad)
        record = svc.records[(ACT, CHILD)]
        self.assertTrue(record.has_queue(QueueKind.GUARDIAN_CHANGE))
        # 旧父亲授权失效、通知不能再发给他
        with self.assertRaises(ConsentScopeError):
            svc.mark_delivered(ACT, CHILD, FATHER)
        # 新监护人未授权前队列不能解除
        with self.assertRaises(ConsentRequiredError):
            svc.resolve_guardian_change_after_reconsent(ACT, CHILD)
        svc.grant_consent(
            ACT, CHILD, "dad-2",
            [ConsentScope.NOTIFY],
        )
        svc.resolve_guardian_change_after_reconsent(ACT, CHILD)
        self.assertFalse(record.has_queue(QueueKind.GUARDIAN_CHANGE))

    def test_overdue_sweep(self) -> None:
        svc = _ready_service(review_sla=timedelta(days=30))
        _screen(svc)
        svc.send_notifications(ACT, CHILD)
        svc.mark_delivered(ACT, CHILD, MOTHER)
        svc.mark_viewed(ACT, CHILD, MOTHER)
        svc.record_intent(
            ACT, CHILD, MOTHER, GuardianIntent.WANT_REVIEW,
            at=_utc(2026, 3, 5),
        )
        self.assertEqual(svc.sweep_overdue(_utc(2026, 3, 20)), [])
        overdue = svc.sweep_overdue(_utc(2026, 5, 1))
        self.assertEqual(overdue, [f"{ACT}:{CHILD}"])
        record = svc.records[(ACT, CHILD)]
        self.assertTrue(record.has_queue(QueueKind.OVERDUE_REVIEW))


class ViewIsolationTest(unittest.TestCase):
    def test_school_sees_only_status(self) -> None:
        svc = _ready_service()
        _screen(svc)
        view = svc.school_view(SCHOOL)
        self.assertEqual(len(view), 1)
        self.assertEqual(set(view[0]), {"ref", "status"})
        flat = repr(view)
        self.assertNotIn("牙列不齐", flat)
        self.assertNotIn("偏软饮食", flat)

    def test_health_view_is_de_identified(self) -> None:
        svc = _ready_service(
            father_scopes=(ConsentScope.SCREENING, ConsentScope.NOTIFY),
        )
        _screen(svc)
        svc.send_notifications(ACT, CHILD)
        svc.mark_delivered(ACT, CHILD, MOTHER)
        svc.mark_viewed(ACT, CHILD, MOTHER)
        svc.mark_failed(ACT, CHILD, FATHER, "停机")

        coverage = svc.health_coverage_view()[ACT]
        self.assertEqual(coverage["screened"], 1)
        self.assertEqual(coverage["eligible_guardians"], 2)
        self.assertEqual(coverage["notifications_failed"], 1)
        self.assertEqual(coverage["notifications_viewed"], 1)
        # 妈妈查收了，但爸爸从未看到——覆盖差异必须暴露“部分查收”
        self.assertEqual(coverage["coverage_gap"]["partial_guardian_view"], 1)
        self.assertEqual(coverage["coverage_gap"]["screened_but_not_viewed"], 0)
        flat = repr(coverage)
        for identifier in (CHILD, MOTHER, FATHER, SCHOOL, "gw://", "一(1)班"):
            self.assertNotIn(identifier, flat)


class ExplanationTest(unittest.TestCase):
    def test_explanation_never_pushes_orthodontics(self) -> None:
        svc = _ready_service()
        _screen(svc)
        text = svc.guardian_explanation(ACT, CHILD)
        self.assertIn("不是诊断", text)
        self.assertIn("六周岁", text)
        self.assertNotIn("必须", text)

        svc.send_notifications(ACT, CHILD)
        svc.mark_delivered(ACT, CHILD, MOTHER)
        svc.mark_viewed(ACT, CHILD, MOTHER)
        svc.record_intent(ACT, CHILD, MOTHER, GuardianIntent.WANT_REVIEW)
        rid = svc.authorize_result_return(ACT, CHILD, MOTHER)
        svc.submit_review_result(
            ACT, CHILD, rid,
            outcome=ReviewOutcome.ORTHO_EVALUATION, institution_id="h-1",
        )
        text = svc.guardian_explanation(ACT, CHILD)
        self.assertIn("评估本身不等于开始矫正", text)


if __name__ == "__main__":
    unittest.main()
