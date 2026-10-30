import asyncio
import unittest
from datetime import UTC, datetime

import httpx

from abita_s2s.config import Config
from abita_s2s.insurance_list import (
    UNAVAILABLE,
    OfficePlans,
    listed_plan,
    read_office_plans,
    render_insurance_list,
)
from abita_s2s.integrations.registration_middleware import RegistrationMiddleware
from abita_s2s.prompt import thinker_instructions
from test_patient_resolution import call_state

CONFIG = Config(
    "offline", middleware_url="https://middleware.test", middleware_token="test-auth"
)
STARTED = datetime(2026, 10, 8, 12, tzinfo=UTC)


def plan(plan_id, family="Aetna", **fields):
    return {
        "planId": plan_id,
        "label": plan_id.replace("-", " ").title(),
        "family": family,
        "names": [],
        "carrierCode": "AET07",
        "carrierId": "car1",
        "carrierName": "AETNA",
        "outcome": "accepted",
        "allowedProviders": ["Dr. Bach"],
        "requirements": [],
        "callerNotice": "",
        "note": "internal routing note",
        "acceptedAt": [],
        "selfPay": False,
        **fields,
    }


def render(medical=(), routine_vision=()):
    return render_insurance_list(
        OfficePlans.model_validate(
            {"medical": list(medical), "routine_vision": list(routine_vision)}
        )
    )


def plans_reply(office_id, coverage, plans):
    return {
        "officeId": office_id,
        "coverage": coverage,
        "catalogRevisionId": f"rev-{coverage}",
        "plans": plans,
    }


class RenderTests(unittest.TestCase):
    def test_sections_group_plans_by_family_in_case_insensitive_order(self):
        text = render(
            medical=[
                plan("cigna-hmo", family="cigna"),
                plan("aetna-ppo", family="Aetna"),
                plan("avmed-select", family="AvMed"),
                plan("aetna-hmo", family="Aetna"),
            ],
            routine_vision=[plan("eyemed-vision", family="EyeMed")],
        )
        self.assertEqual(
            text,
            "### Medical visits\n\n"
            "#### Aetna: all accepted\n\n"
            "- Aetna Ppo `aetna-ppo`: accepted\n"
            "- Aetna Hmo `aetna-hmo`: accepted\n\n"
            "#### AvMed\n\n"
            "- Avmed Select `avmed-select`: accepted\n\n"
            "#### cigna\n\n"
            "- Cigna Hmo `cigna-hmo`: accepted\n\n"
            "### Routine vision exams\n\n"
            "#### EyeMed\n\n"
            "- Eyemed Vision `eyemed-vision`: accepted",
        )

    def test_empty_coverage_says_the_office_does_not_accept_insurance(self):
        self.assertEqual(
            render(),
            "### Medical visits\n\n"
            "This office does not accept insurance for medical visits.\n\n"
            "### Routine vision exams\n\n"
            "This office does not accept insurance for routine vision exams.",
        )

    def test_family_verdict(self):
        staff = {"outcome": "needs_staff_task", "requirements": ["staff_verify"]}
        cases = (
            ([{}, {}], ": all accepted"),
            (
                [{"carrierId": "car1"}, {"carrierId": "car2"}],
                ": all accepted; ask which plan is on the card before registering",
            ),
            ([{"outcome": "not_accepted"}] * 2, ": none accepted"),
            (
                [{}, {"outcome": "not_accepted"}],
                ": varies by plan; ask which plan is on the card",
            ),
            ([{}, staff], ": varies by plan; ask which plan is on the card"),
            ([staff, staff], ": varies by plan; ask which plan is on the card"),
            (
                [{"outcome": "accepted", "requirements": ["pcp_referral"]}, {}],
                ": varies by plan; ask which plan is on the card",
            ),
            ([{}], ""),
            ([{"outcome": "not_accepted"}], ""),
        )
        for fields, verdict in cases:
            with self.subTest(fields=fields):
                plans = [plan(f"aetna-{i}", **f) for i, f in enumerate(fields)]
                heading = render(medical=plans).split("\n\n")[1]
                self.assertEqual(heading, f"#### Aetna{verdict}")

    def test_plan_status(self):
        cases = (
            ({}, "accepted"),
            ({"outcome": "not_accepted"}, "not accepted"),
            (
                {"outcome": "not_accepted", "acceptedAt": ["Sweetwater", "Hollywood"]},
                "not accepted here (accepted at Sweetwater, Hollywood)",
            ),
            (
                {"outcome": "needs_staff_task", "requirements": ["pcp_referral"]},
                "accepted; office staff must verify a referral from the primary "
                "care doctor before scheduling",
            ),
            (
                {
                    "outcome": "needs_staff_task",
                    "requirements": ["prior_authorization"],
                },
                "accepted; office staff must verify prior authorization before scheduling",
            ),
            (
                {
                    "outcome": "needs_staff_task",
                    "requirements": ["pcp_referral", "staff_verify"],
                },
                "accepted; office staff must verify a referral from the primary "
                "care doctor and coverage before scheduling",
            ),
            (
                {"outcome": "needs_staff_task"},
                "not confirmed yet; office staff confirm before scheduling",
            ),
            (
                {
                    "outcome": "needs_staff_task",
                    "requirements": ["pcp_referral"],
                    "allowedProviders": [],
                },
                "not confirmed yet; office staff confirm before scheduling",
            ),
            (
                {
                    "outcome": "not_accepted",
                    "callerNotice": "You can be seen as self-pay.",
                },
                "not accepted. Note: You can be seen as self-pay.",
            ),
        )
        for fields, status in cases:
            with self.subTest(fields=fields):
                self.assertEqual(
                    render(medical=[plan("aetna-hmo", **fields)]).splitlines()[4],
                    f"- Aetna Hmo `aetna-hmo`: {status}",
                )

    def test_only_informative_aliases_are_listed(self):
        line = render(
            medical=[
                plan(
                    "avmed-select",
                    label="AvMed Select",
                    names=[
                        "AvMed Select Plan",
                        "the avmed  SELECT",
                        "Health",
                        "AvMed Broad Network",
                        "AvMed Broad Network Medical",
                        "Av Med Select",
                        "Broad Network",
                    ],
                ),
                plan(
                    "aetna-commercial",
                    label="Aetna Commercial HMO & PPO",
                    names=["Aetna Commercial HMO and PPO", "Aetna Employer Plan"],
                ),
            ]
        ).splitlines()
        self.assertIn(
            "- AvMed Select `avmed-select`: accepted "
            "(also said as: AvMed Broad Network; Av Med Select)",
            line,
        )
        self.assertIn(
            "- Aetna Commercial HMO & PPO `aetna-commercial`: accepted "
            "(also said as: Aetna Employer Plan)",
            line,
        )

    def test_internal_fields_are_never_rendered(self):
        text = render(
            medical=[plan("aetna-hmo"), plan("aetna-ppo", outcome="not_accepted")]
        )
        for internal in ("AET07", "car1", "AETNA", "Dr. Bach", "internal routing note"):
            self.assertNotIn(internal, text)

    def test_unknown_outcome_or_requirement_is_rejected(self):
        for fields in ({"outcome": "maybe"}, {"requirements": ["eligibility"]}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                render(medical=[plan("aetna-hmo", **fields)])


class ReadTests(unittest.IsolatedAsyncioTestCase):
    def middleware(self, handler, config=CONFIG):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return RegistrationMiddleware(client, config)

    async def test_reads_both_coverages_concurrently_and_renders_them(self):
        requests, both = [], asyncio.Event()
        replies = {
            "medical": [plan("aetna-hmo")],
            "routine_vision": [plan("eyemed-vision", family="EyeMed")],
        }

        async def handler(request):
            requests.append(request)
            if len(requests) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            coverage = request.url.params["coverage"]
            return httpx.Response(
                200, json=plans_reply("spring_hill", coverage, replies[coverage])
            )

        with self.assertLogs("abita_s2s.insurance_list", level="INFO") as logs:
            plans = await read_office_plans(self.middleware(handler), "spring-hill")
        self.assertEqual(plans, OfficePlans.model_validate(replies))
        self.assertEqual(
            sorted(record.getMessage() for record in logs.records),
            [
                "Insurance plans loaded office=spring-hill coverage=medical revision=rev-medical plans=1",
                "Insurance plans loaded office=spring-hill coverage=routine_vision revision=rev-routine_vision plans=1",
            ],
        )
        text = render_insurance_list(plans)
        self.assertEqual(
            sorted((r.method, r.url.path, r.url.query.decode()) for r in requests),
            [
                ("GET", "/api/insurance/plans", f"office=spring-hill&coverage={c}")
                for c in ("medical", "routine_vision")
            ],
        )
        self.assertTrue(
            all(r.headers["authorization"] == "test-auth" for r in requests)
        )
        self.assertIn(
            f"<insurance-list>\n{text}\n</insurance-list>",
            thinker_instructions(STARTED, "Office facts.", text),
        )

    async def test_any_failed_read_yields_no_plans_and_logs_its_cause(self):
        medical = plans_reply("spring_hill", "medical", [plan("aetna-hmo")])

        def offline(request):
            raise httpx.ConnectError("private transport detail")

        def vision(status, body):
            def handler(request):
                if request.url.params["coverage"] == "medical":
                    return httpx.Response(200, json=medical)
                return httpx.Response(status, json=body)

            return handler

        valid = plans_reply("spring_hill", "routine_vision", [])
        cases = (
            (
                vision(503, {"error": "private-error-body"}),
                CONFIG,
                "routine_vision:http_503",
            ),
            (
                vision(200, {"status": "error", "message": "Unknown office"}),
                CONFIG,
                "routine_vision:invalid_response",
            ),
            (
                vision(200, {**valid, "plans": [{**plan("x"), "family": None}]}),
                CONFIG,
                "routine_vision:invalid_response",
            ),
            (
                vision(
                    200,
                    {k: v for k, v in valid.items() if k != "catalogRevisionId"},
                ),
                CONFIG,
                "routine_vision:invalid_response",
            ),
            (
                vision(200, {**valid, "officeId": "sweetwater"}),
                CONFIG,
                "routine_vision:mismatched_response",
            ),
            (
                vision(200, {**valid, "coverage": "medical"}),
                CONFIG,
                "routine_vision:mismatched_response",
            ),
            (offline, CONFIG, "medical:network,routine_vision:network"),
            (
                vision(200, valid),
                Config("offline"),
                "medical:not_configured,routine_vision:not_configured",
            ),
        )
        for handler, config, cause in cases:
            with (
                self.subTest(cause=cause),
                self.assertLogs("abita_s2s.insurance_list", level="ERROR") as logs,
            ):
                plans = await read_office_plans(
                    self.middleware(handler, config), "spring-hill"
                )
                self.assertIsNone(plans)
                self.assertEqual(render_insurance_list(plans), UNAVAILABLE)
                self.assertEqual(len(logs.output), 1)
                self.assertIn(f"cause={cause}", logs.output[0])
                self.assertIn("office=spring-hill", logs.output[0])
                self.assertNotIn("private", str(logs.output))
                self.assertNotIn("test-auth", str(logs.output))


class ListedPlanTests(unittest.TestCase):
    def test_tools_take_only_a_plan_id_from_the_visit_types_list(self):
        state = call_state()
        self.assertEqual(listed_plan(state, " vsp ", "routine_vision").label, "VSP")
        self.assertTrue(listed_plan(state, "self-pay-medical", "medical").selfPay)
        for plan_id, coverage in (
            ("vsp", "medical"),
            ("Aetna", "medical"),
            ("aetna-hmo", "medical"),
        ):
            with self.subTest(plan_id=plan_id, coverage=coverage):
                result = listed_plan(state, plan_id, coverage)
                self.assertEqual(result["outcome"], "unknown_plan")
                self.assertTrue(result["answer"].startswith("needs_input: "))
                self.assertIn(
                    "Insurance list section for medical visits", result["answer"]
                )

    def test_unloaded_list_blocks_instead_of_trusting_plan_words(self):
        state = call_state()
        state.insurance.plans = None
        result = listed_plan(state, "aetna", "medical")
        self.assertEqual(result["outcome"], "insurance_list_unavailable")
        self.assertTrue(result["answer"].startswith("blocked: "))
        self.assertIn("staff request or transfer", result["answer"])
