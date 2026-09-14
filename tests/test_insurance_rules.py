import unittest

from abita_s2s.insurance_rules import match_plan


class InsuranceRuleTests(unittest.TestCase):
    def test_office_and_visit_acceptance_regressions(self):
        cases = [
            ("spring-hill", "Humana Gold Plus", "medical", "not_accepted", None),
            (
                "spring-hill",
                "Humana Medicare",
                "medical",
                "accepted",
                "Humana Medicare",
            ),
            (
                "spring-hill",
                "Humana Medicaid",
                "medical",
                "accepted",
                "Humana Medicaid",
            ),
            ("crystal-river", "Humana PPO", "medical", "not_accepted", None),
            (
                "hollywood",
                "Humana Gold Plus",
                "medical",
                "accepted",
                "Humana Medicare HMO",
            ),
            ("sweetwater", "Humana Gold Plus", "routine_vision", "accepted", "iCare"),
            ("hollywood", "Florida Blue", "medical", "needs_clarification", None),
            ("hollywood", "Florida Blue HMO", "medical", "needs_staff_task", None),
            ("hollywood", "Care Plus", "medical", "needs_staff_task", None),
            ("hollywood", "Aetna EPO", "medical", "accepted", "Aetna EPO"),
            ("spring-hill", "Aetna EPO", "medical", "not_accepted", None),
            (
                "spring-hill",
                "I have Aetna Medicare PPO",
                "routine_vision",
                "accepted",
                "iCare",
            ),
            ("spring-hill", "I have Florida Blue HMO", "medical", "not_accepted", None),
            (
                "spring-hill",
                "Michigan Blue Cross Blue Shield PPO",
                "medical",
                "accepted",
                "Florida Blue",
            ),
            ("north-miami-beach-optical", "Aetna", "medical", "not_accepted", None),
            ("north-miami-beach-optical", "VSP", "routine_vision", "accepted", "VSP"),
            ("crystal-river", "Self Pay", "routine_vision", "not_accepted", None),
            (
                "spring-hill",
                "Unlisted insurance",
                "medical",
                "needs_clarification",
                None,
            ),
        ]
        for office, plan, coverage, outcome, canonical in cases:
            with self.subTest(office=office, plan=plan, coverage=coverage):
                result = match_plan(office, plan, coverage)
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["canonical_plan"], canonical)

    def test_provider_notice_and_prior_authorization_remain_visible(self):
        result = match_plan("spring-hill", "Humana Medicaid", "medical")
        self.assertIn("only see Dr. Bach", result["answer"])
        result = match_plan("hollywood", "Florida Blue HMO", "medical")
        self.assertIn("diplopia", result["answer"])
        self.assertIn("prior authorization", result["answer"])
        self.assertIn("permission", result["answer"])
