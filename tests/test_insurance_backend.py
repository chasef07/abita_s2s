"""Exercise ownership boundaries with synthetic backend decisions and identities."""

import asyncio
import json
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
from abita_s2s.middleware import Receipt
from abita_s2s.registration_middleware import RegistrationMiddleware


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
                self.assertEqual(
                    insurance_ready(state, "medical"), operation == "update"
                )
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

    def test_requirement_is_not_active_coverage_or_authorization_proof(self):
        d = InsuranceDecision.model_validate(
            decision(
                canSchedule=False,
                requirements=[
                    dict(kind="vob_authorization", verification="unverified")
                ],
            )
        )
        self.assertEqual(d.eligibility, "not_checked")
        self.assertFalse(d.canSchedule)
