import unittest

from screening.models import CaseStatus, FeedbackCategory
from screening.walkthrough import run_seed_walkthrough


def all_text(payload) -> str:
    if isinstance(payload, dict):
        return " ".join(all_text(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return " ".join(all_text(v) for v in payload)
    return str(payload)


class SeedWalkthroughTest(unittest.TestCase):
    """用现有六岁儿童和护理习惯资料走通校园提示到医院反馈的链路。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = run_seed_walkthrough()

    def test_chain_completes_with_single_consistent_file(self) -> None:
        service = self.result.service
        self.assertEqual(len(service.cases), 1, "同一儿童同一活动只能有一份档案")
        case = service.case_file(self.result.campaign_id, self.result.child_key)
        self.assertIs(case.status, CaseStatus.COMPLETED)
        self.assertIs(case.feedback.category, FeedbackCategory.CONTINUE_OBSERVATION)  # 样例“随访观察”
        self.assertEqual(service.open_entries(), [], "链路走完后不应有未处理的队列条目")

    def test_both_guardians_eventually_reached(self) -> None:
        service = self.result.service
        reached = {n.guardian_id for n in service.notifications if n.delivered}
        self.assertEqual(reached, {"guardian-mother", "guardian-father"})

    def test_guardian_sees_plain_explanation_without_orthodontic_steering(self) -> None:
        report = self.result.guardian_report
        text = all_text(report)
        self.assertIn("不是医院诊断", text)
        self.assertIn("轻度牙列不齐", text)
        self.assertIn("继续观察", report["hospital_feedback"]["summary"])
        self.assertEqual(report["history_notes_received"], ["既往无口腔就诊记录，无药物过敏史"])
        for steering in ("矫正", "正畸", "矫治"):
            self.assertNotIn(steering, text, "系统不得自动把孩子导向矫正")

    def test_school_only_sees_completion(self) -> None:
        rows = self.result.school_rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"child_key", "campaign_id", "status"})
        self.assertEqual(rows[0]["status"], "已完成")

    def test_health_view_is_deidentified_and_covers_funnel(self) -> None:
        coverage = self.result.health_coverage
        self.assertEqual(coverage["totals"]["screened"], 1)
        self.assertEqual(coverage["totals"]["completed"], 1)
        self.assertEqual(coverage["coverage_gaps"]["screened_not_responded"], 0)
        text = all_text(coverage)
        for identifier in ("child-case-31a", "guardian", "母亲", "父亲"):
            self.assertNotIn(identifier, text)


if __name__ == "__main__":
    unittest.main()
