"""Exercise ownership boundaries with synthetic backend decisions and identities."""

import asyncio
import json
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock

import httpx
from insurance_fixtures import decision
from test_patient_resolution import CONFIG, call_state, receipt
from test_insurance_registration import created, registration, updated

from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.insurance_contract import InsuranceDecision
from abita_s2s.insurance_state import accepted_insurance, insurance_ready
from abita_s2s.integrations.patient_middleware import Receipt
from abita_s2s.integrations.registration_middleware import RegistrationMiddleware


class BackendInsuranceTests(unittest.IsolatedAsyncioTestCase):
    def owner(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        state = call_state()
        resolver = PatientResolver(state, AsyncMock())
        self.addAsyncCleanup(resolver.aclose)
        owner = InsuranceRegistration(
            state, resolver, RegistrationMiddleware(client, CONFIG)
        )
        self.addAsyncCleanup(owner.aclose)
        return state, owner

    async def test_opaque_plan_and_requirements_are_backend_owned(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json=decision(
                    "Backend Product",
                    carrierCode="SYNTHETIC",
                    outcome="needs_staff_task",
                    canSchedule=False,
                    requirements=[
                        dict(
                            kind="pcp_referral",
                            channel="uhc_portal",
                            verification="unverified",
                        )
                    ],
                    answer="blocked: Staff must verify the PCP referral in the insurer's portal.",
                ),
            )

        state, owner = self.owner(handler)
        state.patient.active = Receipt.model_validate(receipt())
        result = await owner.check("Caller's exact unfamiliar wording", "medical")
        checked = accepted_insurance(state)
        self.assertEqual(checked.decision.canonicalPlan, "Backend Product")
        self.assertEqual(checked.decision.carrierCode, "SYNTHETIC")
        self.assertEqual(checked.decision.requirements[0].verification, "unverified")
        self.assertFalse(checked.decision.canSchedule)
        self.assertIn("PCP referral", result["answer"])
        self.assertEqual(requests[0]["plan"], "Caller's exact unfamiliar wording")
        self.assertEqual(requests[0]["dob"], state.patient.active.dob)

    async def test_plan_question_is_relayed_without_acceptance(self):
        requests = []
        question = decision(
            outcome="needs_clarification",
            participation="unknown",
            planId="",
            canonicalPlan="",
            canSchedule=False,
            options=[
                dict(planId="synthetic-hmo", label="Synthetic HMO"),
                dict(planId="synthetic-ppo", label="Synthetic PPO"),
            ],
            answer="needs_input: Which of these is on your card: Synthetic HMO, or Synthetic PPO?",
        )

        def handler(request):
            plan = json.loads(request.content)["plan"]
            requests.append(plan)
            if plan == "Synthetic":
                return httpx.Response(200, json=question)
            return httpx.Response(
                200, json=decision("Synthetic PPO", planId="synthetic-ppo")
            )

        state, owner = self.owner(handler)
        result = await owner.check("Synthetic", "medical")
        self.assertEqual(result["outcome"], "needs_clarification")
        self.assertEqual(result["answer"], question["answer"])
        self.assertIsNone(accepted_insurance(state))
        self.assertEqual(
            (await owner.check("Synthetic PPO", "medical"))["outcome"], "accepted"
        )
        self.assertEqual(accepted_insurance(state).decision.planId, "synthetic-ppo")
        self.assertEqual(requests, ["Synthetic", "Synthetic PPO"])

    def offering(self, questions):
        bodies = []

        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            plan, coverage, office = body["plan"], body["coverageType"], body["office"]
            if plan not in questions:
                return httpx.Response(200, json=decision(plan, coverage, office))
            options = [
                dict(planId=label.casefold().replace(" ", "-"), label=label)
                for label in questions[plan]
            ]
            return httpx.Response(
                200,
                json=decision(
                    plan,
                    coverage,
                    office,
                    outcome="needs_clarification",
                    participation="unknown",
                    planId="",
                    canonicalPlan="",
                    canSchedule=False,
                    options=options,
                    answer="needs_input: Which of these is on your card?",
                ),
            )

        state, owner = self.owner(handler)
        return state, owner, bodies

    async def test_answer_to_offered_options_sends_their_plan_ids_once(self):
        state, owner, bodies = self.offering(
            {"Aetna": ("Aetna EPO", "Aetna Medicare"), "Unclear": ()}
        )
        for plan, offered, outcome in (
            ("Aetna", None, "needs_clarification"),
            ("the Medicare one", ["aetna-epo", "aetna-medicare"], "accepted"),
            ("Aetna", None, "needs_clarification"),
            ("Unclear", ["aetna-epo", "aetna-medicare"], "needs_clarification"),
            ("Aetna Medicare", None, "accepted"),
        ):
            with self.subTest(plan=plan, offered=offered):
                self.assertEqual(
                    (await owner.check(plan, "medical"))["outcome"], outcome
                )
                self.assertEqual(bodies[-1]["plan"], plan)
                self.assertEqual(bodies[-1].get("offeredPlanIds", None), offered)
        self.assertIsNone(state.insurance.offered)

    async def test_repeated_question_replaces_offered_plan_ids(self):
        state, owner, bodies = self.offering(
            {
                "Aetna": ("Aetna EPO", "Aetna Medicare", "Meritain Health"),
                "the Health one": ("Aetna Medicare", "Meritain Health"),
            }
        )
        await owner.check("Aetna", "medical")
        await owner.check("the Health one", "medical")
        self.assertEqual(
            state.insurance.offered.plan_ids, ("aetna-medicare", "meritain-health")
        )
        await owner.check("Meritain", "medical")
        self.assertEqual(
            [body.get("offeredPlanIds") for body in bodies],
            [
                None,
                ["aetna-epo", "aetna-medicare", "meritain-health"],
                ["aetna-medicare", "meritain-health"],
            ],
        )

    async def test_offered_plan_ids_apply_only_to_the_same_visit_office_and_patient(
        self,
    ):
        for change in ("visit", "office", "patient"):
            with self.subTest(change=change):
                state, owner, bodies = self.offering(
                    {"Aetna": ("Aetna HMO", "Aetna Medicare")}
                )
                await owner.check("Aetna", "medical")
                coverage = "medical"
                if change == "visit":
                    coverage = "routine_vision"
                elif change == "office":
                    state.call = replace(state.call, called_office_key="hollywood")
                else:
                    state.patient.revision += 1
                await owner.check("Medicare", coverage)
                self.assertEqual(bodies[-1]["plan"], "Medicare")
                self.assertNotIn("offeredPlanIds", bodies[-1])
                self.assertIsNone(state.insurance.offered)

    async def test_writes_send_checked_plan_id_and_name(self):
        legacy = {
            k: v for k, v in decision("Aetna HMO").items() if k != "planId"
        } | dict(routing="all_three", credentialedProviders=["Dr. Example"])
        self.assertEqual(InsuranceDecision.model_validate(legacy).planId, "")
        current = decision("Aetna HMO", carrierId="car-synthetic", options=[])
        for checked in (current, legacy):
            for operation in ("registration", "update"):
                with self.subTest(operation=operation, planId=checked.get("planId")):
                    writes = []

                    def handler(request):
                        if request.url.path == "/api/insurance/decision":
                            return httpx.Response(200, json=checked)
                        writes.append(json.loads(request.content))
                        return httpx.Response(
                            200,
                            json=created(insuranceDecision=checked)
                            if operation == "registration"
                            else updated(
                                newInsurance="Aetna HMO", insuranceDecision=checked
                            ),
                        )

                    state, owner = self.owner(handler)
                    if operation == "update":
                        state.patient.active = Receipt.model_validate(receipt())
                    await owner.check("aetna h m o", "medical")
                    if operation == "registration":
                        result = await owner.add(registration())
                    else:
                        result = await owner.update("member-example")
                    self.assertIn(result["outcome"], ("created", "updated"))
                    self.assertEqual(writes[0]["insurance"], "Aetna HMO")
                    if "planId" in checked:
                        self.assertEqual(writes[0]["insurancePlanId"], "aetna-hmo")
                    else:
                        self.assertNotIn("insurancePlanId", writes[0])

    async def test_no_local_fallback_on_invalid_unavailable_or_mismatched_decision(
        self,
    ):
        for response in (
            httpx.Response(503),
            httpx.Response(200, json={"outcome": "accepted"}),
            httpx.Response(200, json=decision(office="hollywood")),
            httpx.Response(200, json=decision(coverage="routine_vision")),
            httpx.Response(200, json=decision(canonicalPlan="", canSchedule=False)),
            httpx.Response(200, json=decision(participation="not_accepted")),
            httpx.Response(
                200,
                json=decision(
                    requirements=[
                        dict(kind="prior_authorization", verification="verified")
                    ]
                ),
            ),
        ):
            with self.subTest(response=response.status_code):
                state, owner = self.owner(lambda _: response)
                result = await owner.check("Aetna", "medical")
                self.assertEqual(result["outcome"], "unavailable")
                self.assertIsNone(accepted_insurance(state))

    async def test_accepted_plan_can_register_and_update_without_clearing_requirements(
        self,
    ):
        for operation in ("registration", "update"):
            with self.subTest(operation=operation):
                paths = []
                d = decision(
                    "Aetna HMO",
                    outcome="needs_staff_task",
                    canSchedule=False,
                    requirements=[
                        dict(kind="prior_authorization", verification="unverified")
                    ],
                    answer="blocked: This plan requires prior authorization before scheduling.",
                )

                def handler(request):
                    paths.append(request.url.path)
                    if request.url.path == "/api/insurance/decision":
                        return httpx.Response(200, json=d)
                    result = (
                        created(insuranceDecision=d)
                        if operation == "registration"
                        else updated(newInsurance="Aetna HMO", insuranceDecision=d)
                    )
                    return httpx.Response(200, json=result)

                state, owner = self.owner(handler)
                if operation == "update":
                    state.patient.active = Receipt.model_validate(receipt())
                await owner.check("Aetna HMO", "medical")
                self.assertIsNotNone(accepted_insurance(state))
                result = (
                    await owner.add(registration())
                    if operation == "registration"
                    else await owner.update("member-example")
                )
                self.assertEqual(
                    result["outcome"],
                    "created" if operation == "registration" else "updated",
                )
                self.assertFalse(state.patient.active.insuranceDecision.canSchedule)
                self.assertTrue(insurance_ready(state))
                self.assertEqual(
                    paths,
                    [
                        "/api/insurance/decision",
                        "/api/add-patient"
                        if operation == "registration"
                        else "/api/patient/update-insurance",
                    ],
                )

    async def test_patient_switch_and_out_of_order_checks_cannot_restore_old_acceptance(
        self,
    ):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if json.loads(request.content)["plan"] == "slow":
                entered.set()
                await finish.wait()
            return httpx.Response(200, json=decision())

        state, owner = self.owner(handler)
        slow = asyncio.create_task(owner.check("slow", "medical"))
        await entered.wait()
        await owner.check("newer", "medical")
        newest = accepted_insurance(state)
        finish.set()
        self.assertEqual((await slow)["outcome"], "stale")
        self.assertIs(accepted_insurance(state), newest)
        entered.clear()
        finish.clear()
        slow = asyncio.create_task(owner.check("slow", "medical"))
        await entered.wait()
        state.patient.revision += 1
        finish.set()
        self.assertEqual((await slow)["outcome"], "stale")
        self.assertIsNone(accepted_insurance(state))
