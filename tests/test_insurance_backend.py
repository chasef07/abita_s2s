"""Insurance decisions are backend-owned; s2s words them and sends plan IDs."""

import json
from typing import get_args
import unittest
from unittest.mock import AsyncMock

import httpx
from insurance_fixtures import decision
from test_patient_resolution import CONFIG, call_state, receipt
from test_insurance_registration import created, registration, updated

from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import (
    ANSWERS,
    REQUIREMENTS,
    InsuranceRegistration,
    decision_answer,
)
from abita_s2s.insurance_contract import InsuranceDecision, InsuranceRequirement
from abita_s2s.insurance_state import insurance_ready
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

    def test_every_reason_and_requirement_kind_has_wording(self):
        for wording, model, field in (
            (ANSWERS, InsuranceDecision, "reason"),
            (REQUIREMENTS, InsuranceRequirement, "kind"),
        ):
            self.assertEqual(
                set(wording), set(get_args(model.model_fields[field].annotation))
            )

    def test_answer_is_written_from_decision_fields_not_backend_text(self):
        notice = "At Spring Hill, patients with this plan can only see Dr. Bach."
        unknown = dict(participation="unknown", planId="", canonicalPlan="")
        for body, expected in (
            (
                decision("Sunshine Medicaid Vision", "routine_vision"),
                "Sunshine Medicaid Vision is accepted for routine vision exams at this office.",
            ),
            (
                decision("Humana Medicaid", callerNotice=notice),
                f"Humana Medicaid is accepted for medical visits at this office. Note: {notice}",
            ),
            (
                decision(
                    "Cigna",
                    outcome="not_accepted",
                    participation="not_accepted",
                    canSchedule=False,
                    reason="not_accepted",
                ),
                "Cigna is not accepted for medical visits at this office.",
            ),
            (
                decision(
                    outcome="not_accepted",
                    participation="not_accepted",
                    planId="",
                    canonicalPlan="",
                    canSchedule=False,
                    reason="office_no_coverage",
                ),
                "This office does not accept insurance for medical visits.",
            ),
            (
                decision(
                    coverage="routine_vision",
                    outcome="not_accepted",
                    participation="not_accepted",
                    planId="",
                    canonicalPlan="",
                    canSchedule=False,
                    reason="no_self_pay",
                ),
                "This office does not offer self-pay for routine vision exams.",
            ),
            (
                decision(
                    outcome="needs_clarification",
                    participation="unknown",
                    planId="",
                    canonicalPlan="",
                    canSchedule=False,
                    reason="ask_card",
                ),
                "Ask what insurance plan is on the card.",
            ),
            (
                decision(
                    outcome="needs_clarification",
                    participation="unknown",
                    planId="",
                    canonicalPlan="",
                    canSchedule=False,
                    reason="ask_full_name",
                ),
                "Ask for the full plan name on the card.",
            ),
            (
                decision(
                    outcome="needs_clarification",
                    participation="unknown",
                    planId="",
                    canonicalPlan="",
                    canSchedule=False,
                    reason="choose_plan",
                    options=[
                        dict(planId="aetna-hmo", label="Aetna HMO"),
                        dict(planId="aetna-ppo", label="Aetna PPO"),
                    ],
                ),
                "Ask which plan is on the card: Aetna HMO, Aetna PPO.",
            ),
            (
                decision(
                    "Aetna HMO",
                    outcome="needs_staff_task",
                    canSchedule=False,
                    reason="requirement",
                    requirements=[dict(kind="pcp_referral", verification="unverified")],
                ),
                "Office staff must verify a referral from the patient's primary care doctor before scheduling medical visits. Aetna HMO is accepted.",
            ),
            (
                decision(
                    "VSP",
                    "routine_vision",
                    outcome="needs_staff_task",
                    canSchedule=False,
                    reason="no_provider_for_age",
                    allowedProviders=[],
                ),
                "VSP is accepted, but no doctor here who takes it can see a patient of this age. Office staff must arrange this visit.",
            ),
            (
                decision(
                    "CarePlus Medicare",
                    outcome="needs_staff_task",
                    canSchedule=False,
                    participation="unknown",
                    reason="pending_confirmation",
                    callerNotice="Staff will call you back.",
                ),
                "Office staff must confirm CarePlus Medicare for medical visits before scheduling. Note: Staff will call you back.",
            ),
            (
                decision(
                    outcome="needs_staff_task",
                    canSchedule=False,
                    **unknown,
                    reason="chart_unverified",
                ),
                "Office staff must verify the insurance on the chart before scheduling.",
            ),
        ):
            body["answer"] = "success: backend wording"
            with self.subTest(reason=body["reason"]):
                self.assertEqual(
                    decision_answer(InsuranceDecision.model_validate(body)), expected
                )

    async def test_listed_plan_with_requirements_registers_and_updates_once(self):
        for operation in ("registration", "update"):
            with self.subTest(operation=operation):
                writes = []
                d = decision(
                    "Aetna",
                    outcome="needs_staff_task",
                    reason="requirement",
                    canSchedule=False,
                    requirements=[
                        dict(kind="prior_authorization", verification="unverified")
                    ],
                )

                def handler(request):
                    writes.append((request.url.path, json.loads(request.content)))
                    result = (
                        created(insuranceDecision=d)
                        if operation == "registration"
                        else updated(insuranceDecision=d)
                    )
                    return httpx.Response(200, json=result)

                state, owner = self.owner(handler)
                if operation == "update":
                    state.patient.active = Receipt.model_validate(receipt())
                result = (
                    await owner.add(registration())
                    if operation == "registration"
                    else await owner.update("aetna", "medical", "member-example")
                )
                self.assertEqual(
                    result["outcome"],
                    "created" if operation == "registration" else "updated",
                )
                self.assertTrue(insurance_ready(state))
                self.assertEqual(
                    [(path, body["insurancePlanId"]) for path, body in writes],
                    [
                        (
                            "/api/add-patient"
                            if operation == "registration"
                            else "/api/patient/update-insurance",
                            "aetna",
                        )
                    ],
                )
