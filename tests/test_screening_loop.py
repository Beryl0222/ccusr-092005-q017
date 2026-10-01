import unittest
from datetime import date

from screening.models import (
    AuthorizationRequiredError,
    CaseStatus,
    ConsentRequiredError,
    DuplicateFeedbackError,
    FeedbackCategory,
    ForbiddenContentError,
    Guardian,
    GuardianChoice,
    InvalidStateError,
    QueueKind,
    ScreeningError,
    UnknownObservationError,
)
from screening.service import ScreeningLoopService
from screening.views import guardian_view, health_coverage_view, school_view

TODAY = date(2026, 10, 1)


def make_service(*, father_reachable: bool = True) -> ScreeningLoopService:
    """搭好活动、儿童（两位监护人）和母亲同意的初始状态。"""
    service = ScreeningLoopService(today=TODAY, review_deadline_days=30)
    service.create_campaign("camp-1", school_id="school-1", title="秋季校园口腔筛查")
    service.register_child(
        "child-1",
        school_id="school-1",
        age_years=6,
        guardians=[
            Guardian("mother", "母亲", contact_ok=True),
            Guardian("father", "父亲", contact_ok=father_reachable),
        ],
    )
    service.grant_consent("camp-1", "child-1", guardian_id="mother")
    return service


def screen_child(service: ScreeningLoopService, **overrides):
    kwargs = {
        "dentition_stage": "乳牙列期",
        "observations": ["轻度牙列不齐"],
        "care_habits": ["偏软饮食", "刷牙需协助"],
        "review_reason": "校园筛查肉眼观察提示牙列不齐，建议到医院复核",
        "screener": "school-nurse-01",
    }
    kwargs.update(overrides)
    return service.register_screening("camp-1", "child-1", **kwargs)


def all_text(payload) -> str:
    """把视图输出展开成纯文本，便于检查是否泄露了不该出现的内容。"""
    if isinstance(payload, dict):
        return " ".join(all_text(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return " ".join(all_text(v) for v in payload)
    return str(payload)


class ConsentGateTest(unittest.TestCase):
    def test_screening_requires_consent_for_this_campaign(self) -> None:
        service = ScreeningLoopService(today=TODAY)
        service.create_campaign("camp-1", school_id="school-1", title="活动一")
        service.register_child("child-1", school_id="school-1", age_years=6, guardians=[Guardian("mother", "母亲")])
        with self.assertRaises(ConsentRequiredError):
            screen_child(service)

    def test_consent_does_not_carry_over_to_another_campaign(self) -> None:
        service = make_service()
        service.create_campaign("camp-2", school_id="school-1", title="活动二")
        with self.assertRaises(ConsentRequiredError):
            service.register_screening(
                "camp-2",
                "child-1",
                dentition_stage="乳牙列期",
                observations=["轻度牙列不齐"],
                care_habits=[],
                review_reason="建议复核",
                screener="nurse",
            )

    def test_declined_consent_blocks_screening(self) -> None:
        service = make_service()
        service.decline_consent("camp-1", "child-1", guardian_id="mother")
        with self.assertRaises(ConsentRequiredError):
            screen_child(service)


class ScreenerCannotDiagnoseTest(unittest.TestCase):
    def test_diagnosis_or_treatment_terms_are_rejected(self) -> None:
        service = make_service()
        with self.assertRaises(ForbiddenContentError):
            screen_child(service, review_reason="诊断为错颌畸形，建议矫正治疗")
        with self.assertRaises(ForbiddenContentError):
            screen_child(service, care_habits=["建议佩戴矫治器"])

    def test_observation_must_be_standardized_prompt(self) -> None:
        service = make_service()
        with self.assertRaises(UnknownObservationError):
            screen_child(service, observations=["错颌畸形"])

    def test_screening_record_has_no_diagnosis_field(self) -> None:
        service = make_service()
        with self.assertRaises(TypeError):
            service.register_screening(
                "camp-1",
                "child-1",
                dentition_stage="乳牙列期",
                observations=["轻度牙列不齐"],
                care_habits=[],
                review_reason="建议复核",
                screener="nurse",
                diagnosis="错颌畸形",  # type: ignore[call-arg]
            )


class NotificationTest(unittest.TestCase):
    def test_all_guardians_notified_and_failure_queued_then_retried(self) -> None:
        service = make_service(father_reachable=False)
        screen_child(service)

        delivered = {n.guardian_id for n in service.notifications if n.delivered}
        failed = {n.guardian_id for n in service.notifications if not n.delivered}
        self.assertEqual(delivered, {"mother"})
        self.assertEqual(failed, {"father"})

        failures = service.open_entries(QueueKind.NOTIFICATION_FAILURE)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].guardian_id, "father")

        # 未查收的监护人不能回执。
        with self.assertRaises(InvalidStateError):
            service.guardian_respond("camp-1", "child-1", "father", choices=[GuardianChoice.HOSPITAL_REVIEW])

        # 修复渠道后重试补达，队列条目随之关闭。
        service.update_guardian_contact("child-1", "father", contact_ok=True)
        self.assertEqual(service.retry_failed_notifications(), 1)
        self.assertEqual(service.open_entries(QueueKind.NOTIFICATION_FAILURE), [])
        self.assertTrue(service._has_delivered_notification(service.case_file("camp-1", "child-1"), "father"))


class GuardianResponseTest(unittest.TestCase):
    def test_refuse_contact_closes_case_and_blocks_feedback(self) -> None:
        service = make_service()
        case = screen_child(service)
        service.guardian_respond("camp-1", "child-1", "mother", choices=[GuardianChoice.REFUSE_CONTACT])
        self.assertIs(case.status, CaseStatus.CLOSED_REFUSED)
        with self.assertRaises(InvalidStateError):
            service.submit_hospital_feedback("camp-1", "child-1", category=FeedbackCategory.NO_ACTION, facility="市口腔医院")

    def test_contradictory_choices_are_rejected(self) -> None:
        service = make_service()
        screen_child(service)
        with self.assertRaises(ScreeningError):
            service.guardian_respond(
                "camp-1",
                "child-1",
                "mother",
                choices=[GuardianChoice.HOSPITAL_REVIEW, GuardianChoice.REFUSE_CONTACT],
            )

    def test_supplement_history_requires_note_and_is_stored(self) -> None:
        service = make_service()
        case = screen_child(service)
        with self.assertRaises(ScreeningError):
            service.guardian_respond("camp-1", "child-1", "mother", choices=[GuardianChoice.SUPPLEMENT_HISTORY])
        service.guardian_respond(
            "camp-1",
            "child-1",
            "mother",
            choices=[GuardianChoice.SUPPLEMENT_HISTORY],
            history_note="三岁时磕掉过一颗乳牙",
        )
        self.assertEqual(case.history_notes, ["三岁时磕掉过一颗乳牙"])


class HospitalFeedbackTest(unittest.TestCase):
    def test_feedback_requires_guardian_authorization(self) -> None:
        service = make_service()
        screen_child(service)
        service.guardian_respond(
            "camp-1", "child-1", "mother", choices=[GuardianChoice.HOSPITAL_REVIEW], authorize_feedback=False
        )
        with self.assertRaises(AuthorizationRequiredError):
            service.submit_hospital_feedback("camp-1", "child-1", category=FeedbackCategory.NO_ACTION, facility="市口腔医院")

    def test_feedback_requires_review_choice_first(self) -> None:
        service = make_service()
        screen_child(service)
        with self.assertRaises(InvalidStateError):
            service.submit_hospital_feedback("camp-1", "child-1", category=FeedbackCategory.NO_ACTION, facility="市口腔医院")

    def test_second_feedback_is_rejected_to_avoid_contradictory_files(self) -> None:
        service = make_service()
        case = screen_child(service)
        service.guardian_respond(
            "camp-1", "child-1", "mother", choices=[GuardianChoice.HOSPITAL_REVIEW], authorize_feedback=True
        )
        service.submit_hospital_feedback("camp-1", "child-1", category=FeedbackCategory.CONTINUE_OBSERVATION, facility="市口腔医院")
        with self.assertRaises(DuplicateFeedbackError):
            service.submit_hospital_feedback(
                "camp-1", "child-1", category=FeedbackCategory.ORTHODONTIC_EVALUATION, facility="另一家医院"
            )
        self.assertIs(case.feedback.category, FeedbackCategory.CONTINUE_OBSERVATION)


class DuplicateScreeningTest(unittest.TestCase):
    def test_duplicate_screening_goes_to_queue_without_second_file(self) -> None:
        service = make_service()
        first = screen_child(service)
        again = screen_child(service, observations=["牙列拥挤"], review_reason="重复登记")
        self.assertIs(first, again)
        self.assertEqual(len(service.cases), 1)
        self.assertEqual(again.screening.observations, ("轻度牙列不齐",))
        duplicates = service.open_entries(QueueKind.DUPLICATE_SCREENING)
        self.assertEqual(len(duplicates), 1)


class TransferTest(unittest.TestCase):
    def test_transfer_moves_child_and_old_school_loses_visibility(self) -> None:
        service = make_service()
        screen_child(service)
        entry = service.mark_transfer("child-1", to_school_id="school-2")
        self.assertIs(entry.kind, QueueKind.TRANSFER)
        self.assertEqual(service.children["child-1"].school_id, "school-2")
        self.assertEqual(school_view(service, "school-1"), [])
        # 转出后不能再登记到原学校活动。
        with self.assertRaises(ScreeningError):
            screen_child(service)


class GuardianshipChangeTest(unittest.TestCase):
    def test_change_queues_and_pauses_notifications_until_resolved(self) -> None:
        service = make_service()
        case = screen_child(service)
        entry = service.report_guardianship_change("child-1", reason="监护权变更，待街道核实")
        self.assertIs(entry.kind, QueueKind.GUARDIANSHIP_CHANGE)
        self.assertTrue(case.guardianship_hold)

        service.guardian_respond(
            "camp-1", "child-1", "mother", choices=[GuardianChoice.HOSPITAL_REVIEW], authorize_feedback=True
        )
        service.submit_hospital_feedback("camp-1", "child-1", category=FeedbackCategory.NO_ACTION, facility="市口腔医院")
        # 冻结期间通知挂起，不会送达。
        self.assertFalse(any(n.kind == "feedback_ready" and n.delivered for n in service.notifications))

        service.resolve_entry(entry.entry_id, note="已核实新监护关系")
        self.assertFalse(case.guardianship_hold)
        # 解冻后重试，两位监护人挂起的通知补达。
        self.assertEqual(service.retry_failed_notifications(), 2)
        self.assertTrue(any(n.kind == "feedback_ready" and n.delivered for n in service.notifications))


class OverdueReviewTest(unittest.TestCase):
    def test_overdue_cases_enter_queue_once(self) -> None:
        service = make_service()
        screen_child(service)
        service.guardian_respond(
            "camp-1", "child-1", "mother", choices=[GuardianChoice.HOSPITAL_REVIEW], authorize_feedback=True
        )
        service.advance_days(31)
        created = service.sweep_overdue()
        self.assertEqual(len(created), 1)
        self.assertIs(created[0].kind, QueueKind.OVERDUE_REVIEW)
        self.assertEqual(service.sweep_overdue(), [])  # 幂等

    def test_completed_case_never_becomes_overdue(self) -> None:
        service = make_service()
        screen_child(service)
        service.guardian_respond(
            "camp-1", "child-1", "mother", choices=[GuardianChoice.HOSPITAL_REVIEW], authorize_feedback=True
        )
        service.submit_hospital_feedback("camp-1", "child-1", category=FeedbackCategory.NO_ACTION, facility="市口腔医院")
        service.advance_days(60)
        self.assertEqual(service.sweep_overdue(), [])


class SchoolViewTest(unittest.TestCase):
    def test_school_only_sees_completion_status(self) -> None:
        service = make_service()
        screen_child(service)
        rows = school_view(service, "school-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"child_key", "campaign_id", "status"})
        self.assertEqual(rows[0]["status"], "复核流程进行中")
        text = all_text(rows)
        for clinical in ("轻度牙列不齐", "偏软饮食", "复核原因", "龋", "正畸"):
            self.assertNotIn(clinical, text)


class HealthViewTest(unittest.TestCase):
    def test_coverage_view_is_deidentified(self) -> None:
        service = make_service()
        screen_child(service)
        view = health_coverage_view(service, "camp-1", min_cell_size=1)
        self.assertEqual(view["totals"]["screened"], 1)
        self.assertEqual(view["screened_by_dentition_stage"], {"乳牙列期": 1})
        text = all_text(view)
        for identifier in ("child-1", "mother", "father", "母亲", "父亲"):
            self.assertNotIn(identifier, text)

    def test_small_cells_are_suppressed_by_default(self) -> None:
        service = make_service()
        screen_child(service)
        view = health_coverage_view(service, "camp-1")  # 默认 min_cell_size=5
        self.assertIsNone(view["totals"]["screened"])
        self.assertEqual(view["totals"]["refused"], 0)  # 0 不属于小样本泄露


class GuardianViewTest(unittest.TestCase):
    def test_no_auto_orthodontic_direction_before_feedback(self) -> None:
        service = make_service()
        screen_child(service)
        view = guardian_view(service, "camp-1", "child-1", "mother")
        text = all_text(view)
        self.assertIn("不是医院诊断", text)
        self.assertIn("轻度牙列不齐", text)
        for steering in ("矫正", "正畸", "矫治"):
            self.assertNotIn(steering, text)
        self.assertEqual(view["options"], ["选择医院复核", "补充既往情况", "拒绝后续联系"])

    def test_observation_feedback_keeps_guardian_text_free_of_orthodontics(self) -> None:
        service = make_service()
        screen_child(service)
        service.guardian_respond(
            "camp-1", "child-1", "mother", choices=[GuardianChoice.HOSPITAL_REVIEW], authorize_feedback=True
        )
        service.submit_hospital_feedback("camp-1", "child-1", category=FeedbackCategory.CONTINUE_OBSERVATION, facility="市口腔医院")
        view = guardian_view(service, "camp-1", "child-1", "mother")
        self.assertIn("继续观察", view["hospital_feedback"]["summary"])
        for steering in ("矫正", "正畸", "矫治"):
            self.assertNotIn(steering, all_text(view))

    def test_orthodontic_wording_only_comes_from_hospital_feedback(self) -> None:
        service = make_service()
        screen_child(service)
        service.guardian_respond(
            "camp-1", "child-1", "mother", choices=[GuardianChoice.HOSPITAL_REVIEW], authorize_feedback=True
        )
        service.submit_hospital_feedback(
            "camp-1", "child-1", category=FeedbackCategory.ORTHODONTIC_EVALUATION, facility="市口腔医院"
        )
        view = guardian_view(service, "camp-1", "child-1", "mother")
        self.assertIn("正畸", view["hospital_feedback"]["summary"])

    def test_other_guardians_cannot_view(self) -> None:
        service = make_service()
        screen_child(service)
        with self.assertRaises(ScreeningError):
            guardian_view(service, "camp-1", "child-1", "stranger")


if __name__ == "__main__":
    unittest.main()
