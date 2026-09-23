"""Public owner interface with real HTTP validation and deterministic middleware."""

import asyncio
import json
import unittest
from unittest.mock import Mock

import httpx
from test_patient_resolution import CONFIG, call_state, receipt, search

from insurance_fixtures import decision, check_response
from abita_s2s.eligibility_contract import EligibilityInput
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration, Registration, normalize
from abita_s2s.insurance_state import (
    accepted_insurance,
    insurance_ready,
    registration_insurance,
)
from abita_s2s.integrations.patient_middleware import PatientMiddleware, Receipt
from abita_s2s.integrations.registration_middleware import RegistrationMiddleware


def registration(**changes):
    return Registration.model_validate(
        {
            "firstName": "Jane",
            "lastName": "Doe",
            "dob": "01/02/1980",
            "phone": None,
            "inboundPhoneConfirmed": True,
            "email": None,
            "street": "1 Example Street",
            "aptSuite": None,
            "city": "Example",
            "state": "FL",
            "zip": "12345",
            "sex": "female",
            "subscriberName": "Jane Doe",
            "insuranceMemberId": "member-example",
            "readBack": True,
        }
        | changes
    )


def created(status="created", **changes):
    return {
        "status": status,
        "insuranceDecision": decision(),
        "patientId": "new-chart",
        "name": "Doe, Jane",
        "dob": "01/02/1980",
        "routing": "all",
        "allowedProviders": ["Dr. Example"],
    } | changes


def updated(**changes):
    return {
        "status": "updated",
        "effect": "completed",
        "insuranceDecision": decision(
            changes.get("newInsurance", "Aetna"),
            "routine_vision" if changes.get("newInsurance") == "VSP" else "medical",
        ),
        "patientId": "chart-jane",
        "newInsurance": "Aetna",
        "routing": "all",
        "allowedProviders": ["Dr. Example"],
    } | changes


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    def test_receipt_comparison_ignores_punctuation_but_preserves_product(self):
        self.assertEqual(normalize("HMO & PPO"), normalize("HMO and PPO"))
        self.assertEqual(
            normalize("Cigna: Open-Access"), normalize("CIGNA Open Access")
        )
        self.assertNotEqual(normalize("NHP HMO Only"), normalize("NHP HMO Access"))

    def owner(self, responses):
        self.requests = []

        async def handler(request):
            if request.url.path == "/api/insurance/decision":
                return check_response(request)
            self.requests.append((request.url.path, json.loads(request.content)))
            result = responses.pop(0)
            if callable(result):
                return await result(request)
            if isinstance(result, Exception):
                raise result
            return httpx.Response(200, json=result)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        state = call_state()
        state.reporter = Mock()
        resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
        self.addAsyncCleanup(resolver.aclose)
        owner = InsuranceRegistration(
            state, resolver, RegistrationMiddleware(client, CONFIG)
        )
        self.addAsyncCleanup(owner.aclose)
        return state, resolver, owner

    async def prepared(self, responses, plan="Aetna", coverage="medical"):
        state, resolver, owner = self.owner([search(), *responses])
        await resolver.resolve("Jane", "01/02/1980")
        self.assertEqual((await owner.check(plan, coverage))["outcome"], "accepted")
        return state, resolver, owner

    async def test_payer_name_correction_requires_corrected_confirmed_registration(
        self,
    ):
        correction = {
            "status": "active",
            "officeId": "spring_hill",
            "checkedAt": "2026-09-23T12:00:00Z",
            "identity": {
                "status": "matched_with_name_correction",
                "reviewRequired": False,
            },
            "matchedPatient": {
                "firstName": "Jane",
                "lastName": "Doe",
                "dateOfBirth": "19800102",
                "memberId": "member-example",
            },
        }
        state, _, owner = self.owner([correction, created()])
        await owner.check("Aetna", "medical")
        await owner.eligibility(
            EligibilityInput(
                firstName="Ane",
                lastName="Doe Jr.",
                dob="01/02/1980",
                plan="Aetna",
                memberId="member-example",
            )
        )
        old = registration(
            firstName="Ane", lastName="Doe Jr.", subscriberName="Ane Doe Jr."
        )
        self.assertEqual((await owner.add(old))["outcome"], "needs_name_confirmation")
        self.assertEqual(
            (await owner.add(registration(subscriberName="Ane Doe Jr.")))["outcome"],
            "needs_name_confirmation",
        )
        self.assertEqual(
            (await owner.add(registration(readBack=None)))["outcome"],
            "needs_read_back",
        )
        self.assertEqual(len(self.requests), 1)
        self.assertEqual((await owner.add(registration()))["outcome"], "created")
        self.assertEqual(self.requests[-1][1]["firstName"], "Jane")
        self.assertEqual(self.requests[-1][1]["lastName"], "Doe")
        self.assertEqual(self.requests[-1][1]["subscriberName"], "Jane Doe")

    async def test_full_creation_validates_identity_and_caches_duplicate(self):
        state, _, owner = await self.prepared([created()])
        result = await owner.add(registration())
        state.reporter.record.assert_called_once_with(
            "patient",
            {
                "outcome": "created",
                "externalPatientId": "new-chart",
                "superseded": False,
            },
            call_id=None,
        )
        self.assertEqual(result["outcome"], "created")
        self.assertEqual(
            result["answer"],
            "success: New patient chart created with insurance attached.",
        )
        self.assertEqual(state.patient.active.patientId, "new-chart")
        self.assertTrue(insurance_ready(state))
        self.assertEqual(await owner.add(registration()), result)
        self.assertEqual(len(self.requests), 2)
        payload = self.requests[-1][1]
        self.assertEqual(payload["office"], "+17275919997")
        self.assertEqual(payload["phone"], "5555550101")
        self.assertNotIn("ssn", payload)
        self.assertNotIn("new-chart", json.dumps(result))

    async def test_new_patient_creation_without_any_lookup(self):
        state, _, owner = self.owner([created()])
        self.assertIsNone(state.patient.absence)
        self.assertEqual((await owner.check("Aetna", "medical"))["outcome"], "accepted")
        self.assertEqual(self.requests, [])
        result = await owner.add(registration())
        self.assertEqual(result["outcome"], "created")
        self.assertEqual(
            result["answer"],
            "success: New patient chart created with insurance attached.",
        )
        self.assertEqual(state.patient.active.patientId, "new-chart")
        self.assertTrue(insurance_ready(state))
        self.assertEqual([path for path, _ in self.requests], ["/api/add-patient"])
        self.assertEqual(await owner.add(registration()), result)
        self.assertEqual(len(self.requests), 1)

    async def test_two_new_patients_recheck_coverage_without_lookup(self):
        state, resolver, owner = self.owner(
            [
                created(),
                created(patientId="second-chart", name="Doe, John", dob="02/03/1982"),
            ]
        )
        await owner.check("Aetna", "medical")
        first = await owner.add(registration())
        revision = state.patient.revision
        second = registration(firstName="John", dob="02/03/1982")

        self.assertEqual((await owner.add(second))["outcome"], "needs_insurance")
        self.assertIsNone(state.patient.active)
        self.assertIsNone(state.patient.absence)
        self.assertIsNone(accepted_insurance(state))
        self.assertGreater(state.patient.revision, revision)
        self.assertEqual(
            resolver.staff_task_patient(), {"name": "John", "dob": "02/03/1982"}
        )
        self.assertEqual(len(self.requests), 1)

        await owner.check("Aetna", "medical")
        result = await owner.add(second)
        self.assertEqual(result["outcome"], "created")
        self.assertEqual(state.patient.active.patientId, "second-chart")
        self.assertTrue(insurance_ready(state))
        self.assertEqual(await owner.add(second), result)
        self.assertEqual(await owner.add(registration()), first)
        self.assertEqual(state.patient.active.patientId, "second-chart")
        self.assertEqual(
            state.insurance.registrations,
            {"new-chart": "created", "second-chart": "created"},
        )
        self.assertEqual([path for path, _ in self.requests], ["/api/add-patient"] * 2)

    async def test_registration_switch_invalidates_inflight_patient_refresh(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            await finish.wait()
            return httpx.Response(200, json=receipt())

        state, resolver, owner = self.owner([delayed])
        state.patient.active = Receipt.model_validate(
            receipt(appointmentsStatus="error")
        )
        task = asyncio.create_task(resolver.resolve("Jane", "01/02/1980"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            result = await owner.add(registration(firstName="John", dob="02/03/1982"))
            self.assertEqual(result["outcome"], "needs_insurance")
        finally:
            finish.set()
        self.assertEqual((await task)["outcome"], "superseded")
        self.assertIsNone(state.patient.active)
        self.assertEqual(
            resolver.staff_task_patient(), {"name": "John", "dob": "02/03/1982"}
        )

    async def test_existing_same_patient_is_not_cleared_for_registration(self):
        state, resolver, owner = self.owner([receipt()])
        await resolver.resolve("Jane", "01/02/1980")
        active = state.patient.active
        self.assertIsNotNone(active)
        self.assertEqual((await owner.add(registration()))["outcome"], "already_active")
        self.assertIs(state.patient.active, active)
        self.assertEqual(len(self.requests), 1)

    async def test_registration_switch_preserves_state_when_writes_blocked(self):
        for blocked in ("write_pending", "write_uncertain", "closed"):
            with self.subTest(blocked=blocked):
                state, _, owner = self.owner([created()])
                await owner.check("Aetna", "medical")
                await owner.add(registration())
                active, revision = state.patient.active, state.patient.revision
                if blocked == "closed":
                    owner.close_admission()
                else:
                    setattr(state.insurance, blocked, True)
                result = await owner.add(
                    registration(firstName="John", dob="02/03/1982")
                )
                self.assertTrue(result["answer"].startswith("blocked:"))
                self.assertIs(state.patient.active, active)
                self.assertEqual(state.patient.revision, revision)
                self.assertIsNotNone(accepted_insurance(state))
                self.assertEqual(len(self.requests), 1)

    async def test_callback_and_readback(self):
        state, _, owner = await self.prepared([], plan="VSP", coverage="routine_vision")
        self.assertEqual(
            (await owner.add(registration(inboundPhoneConfirmed=None)))["outcome"],
            "needs_callback",
        )
        result = await owner.add(registration(readBack=None))
        self.assertEqual(result["outcome"], "needs_read_back")
        self.assertIn("member-example", result["answer"])
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(state.patient.active)

    async def test_self_pay_and_insured_vision_omit_ssn(self):
        for plan, member in [
            ("Self Pay", "self pay"),
            ("VSP", "member-example"),
        ]:
            state, _, owner = await self.prepared(
                [created(insuranceDecision=decision(plan, "routine_vision"))],
                plan=plan,
                coverage="routine_vision",
            )
            result = await owner.add(
                registration(phone="5555550999", inboundPhoneConfirmed=None)
            )
            self.assertEqual(
                result["answer"],
                "success: New patient chart created with self-pay recorded."
                if plan == "Self Pay"
                else "success: New patient chart created with insurance attached.",
            )
            body = self.requests[-1][1]
            self.assertEqual(body["subscriberNum"], member)
            self.assertNotIn("ssn", body)
            self.assertEqual(body["coverageType"], "routine_vision")
            self.assertEqual(state.call.caller_phone, "+15555550101")
            self.assertEqual(state.patient.active.phone, "5555550999")

    async def test_completed_creation_retains_accepted_insurance_without_recheck(self):
        state, _, owner = await self.prepared([created(insuranceDecision=None)])
        self.assertEqual((await owner.add(registration()))["outcome"], "created")
        self.assertIsNotNone(accepted_insurance(state))
        self.assertTrue(insurance_ready(state))

    async def test_creation_proceeds_directly_to_availability_without_recheck(self):
        from datetime import datetime, UTC
        from abita_s2s.scheduling import Scheduling
        from abita_s2s.integrations.scheduling_http import SchedulingHTTP
        from test_scheduling import inventory

        for repeated_decision in (None, decision("VSP", "routine_vision")):
            with self.subTest(repeated_decision=repeated_decision):
                state, _, owner = await self.prepared(
                    [created(insuranceDecision=repeated_decision)],
                    plan="VSP",
                    coverage="routine_vision",
                )
                await owner.add(registration())
                requests = []

                def handler(request):
                    requests.append((request.url.path, json.loads(request.content)))
                    return httpx.Response(200, json=inventory())

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ) as client:
                    scheduling = Scheduling(
                        state,
                        SchedulingHTTP(client, CONFIG),
                        now=lambda: datetime(2026, 9, 14, 17, tzinfo=UTC),
                    )
                    self.addAsyncCleanup(scheduling.aclose)
                    result = await scheduling.availability(
                        "routine_vision", "2026-09-15"
                    )
                self.assertEqual(result["outcome"], "found")
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0][1]["insurancePlan"], "VSP")
                self.assertEqual(requests[0][1]["patientId"], "new-chart")
                self.assertIsNone(registration_insurance(state, "medical"))

    async def test_completed_chart_visit_change_uses_backend_policy(self):
        from datetime import datetime, UTC
        from abita_s2s.scheduling import Scheduling
        from abita_s2s.integrations.scheduling_http import SchedulingHTTP

        state, _, owner = await self.prepared(
            [created(insuranceDecision=decision("VSP", "routine_vision"))],
            plan="VSP",
            coverage="routine_vision",
        )
        await owner.add(registration())
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "status": "error",
                    "outcome": "policy_blocked",
                    "slots": [],
                    "message": "This chart needs accepted medical coverage.",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduling = Scheduling(
                state,
                SchedulingHTTP(client, CONFIG),
                now=lambda: datetime(2026, 9, 14, 17, tzinfo=UTC),
            )
            self.addAsyncCleanup(scheduling.aclose)
            result = await scheduling.availability("medical", "2026-09-15")
        self.assertEqual(result["outcome"], "unsupported")
        self.assertIn("accepted medical coverage", result["answer"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["patientId"], "new-chart")
        self.assertNotIn("insurancePlan", requests[0])

    async def test_write_decision_must_match_requested_plan_office_and_coverage(self):
        for changed in (
            decision("Different Product"),
            decision(office="hollywood"),
            decision(coverage="routine_vision"),
        ):
            with self.subTest(decision=changed):
                state, _, owner = await self.prepared(
                    [created(insuranceDecision=changed)]
                )
                self.assertEqual(
                    (await owner.add(registration()))["outcome"], "uncertain"
                )
                self.assertTrue(state.insurance.write_uncertain)
                self.assertFalse(insurance_ready(state))
                await owner.add(registration())
                self.assertEqual(len(self.requests), 2)

    async def test_partial_receipt_keeps_chart_and_blocks_scheduling_and_duplicate(
        self,
    ):
        state, _, owner = await self.prepared([created("partial")])
        result = await owner.add(registration())
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(state.patient.active.patientId, "new-chart")
        self.assertFalse(insurance_ready(state))
        self.assertIsNone(state.patient.active.insuranceCarrier)
        await owner.add(registration())
        self.assertEqual(len(self.requests), 2)

    async def test_invalid_receipts_and_network_uncertainty_never_retry(self):
        for result in [
            created(name="Other, Person"),
            created(patientId=""),
            created(dob="02/03/1970"),
            {"status": "created"},
            httpx.ReadTimeout("offline timeout"),
            {"status": "error", "outcome": "indeterminate_write"},
        ]:
            state, _, owner = await self.prepared([result])
            output = await owner.add(registration())
            self.assertIn(output["outcome"], ["uncertain", "invalid_receipt"])
            await owner.add(registration())
            self.assertTrue(state.insurance.write_uncertain)
            self.assertIsNone(state.patient.active)
            self.assertEqual(len(self.requests), 2)

    async def test_corrected_registration_retries_after_proven_no_write_failure(self):
        state, _, owner = await self.prepared(
            [
                {
                    "status": "error",
                    "outcome": "validation_failed",
                    "message": "Correct the postal code.",
                },
                created(),
            ]
        )
        rejected = await owner.add(registration(zip="bad"))
        self.assertEqual(rejected["outcome"], "failed")
        self.assertIn("Correct the postal code", rejected["answer"])
        self.assertNotIn("Do not repeat", rejected["answer"])
        self.assertFalse(state.insurance.write_uncertain)
        result = await owner.add(registration(zip="12345"))
        self.assertEqual(result["outcome"], "created")
        self.assertEqual(self.requests[-1][1]["zip"], "12345")
        self.assertEqual(len(self.requests), 3)

    async def test_explicit_creation_rejection_preserves_failure(self):
        state, _, owner = await self.prepared(
            [{"status": "error", "outcome": "rejected"}, created()]
        )
        self.assertEqual((await owner.add(registration()))["outcome"], "failed")
        self.assertFalse(state.insurance.write_uncertain)
        self.assertIsNone(state.patient.active)
        self.assertEqual((await owner.add(registration()))["outcome"], "created")
        self.assertEqual(len(self.requests), 3)

    async def test_cancellation_duplicate_and_stale_creation(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            await finish.wait()
            return httpx.Response(200, json=created("partial"))

        state, resolver, owner = await self.prepared([delayed])
        task = asyncio.create_task(owner.add(registration()))
        await entered.wait()
        self.assertEqual((await owner.add(registration()))["outcome"], "write_pending")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(state.insurance.write_pending)
        await resolver.resolve("John", None)
        finish.set()
        await owner.aclose()
        self.assertIsNone(state.patient.active)
        self.assertIsNone(accepted_insurance(state))
        self.assertIn("new-chart", state.insurance.registrations)
        self.assertTrue(state.reporter.record.call_args.args[1]["superseded"])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual((await owner.add(registration()))["outcome"], "partial")

    async def test_acceptance_corrections_are_scoped(self):
        state, resolver, owner = await self.prepared([])
        (await owner.check("Unknown insurance", "medical"))
        self.assertIsNone(accepted_insurance(state))
        (await owner.check("VSP", "routine_vision"))
        self.assertIsNone(accepted_insurance(state, "medical"))
        await resolver.resolve("John", None)
        self.assertIsNone(accepted_insurance(state))
        self.assertEqual(
            (await owner.add(registration()))["outcome"], "needs_insurance"
        )
        self.assertEqual(len(self.requests), 1)

    async def test_update_sends_intent_without_provider_refs_and_caches_receipt(self):
        state, _, owner = self.owner([updated()])
        state.patient.active = Receipt.model_validate(receipt())
        (await owner.check("Aetna", "medical"))
        result = await owner.update("member-example")
        self.assertEqual(result["outcome"], "updated")
        self.assertEqual(
            set(self.requests[0][1]),
            {
                "patientId",
                "dob",
                "office",
                "insurance",
                "coverageType",
                "subscriberNum",
            },
        )
        self.assertEqual(state.patient.active.insuranceCarrier, "Aetna")
        self.assertTrue(insurance_ready(state))
        await owner.update("member-example")
        self.assertEqual(len(self.requests), 1)

    async def test_completed_update_without_decision_retains_confirmed_product(
        self,
    ):
        state, _, owner = self.owner([updated(insuranceDecision=None)])
        state.patient.active = Receipt.model_validate(receipt())
        await owner.check("Aetna", "medical")
        self.assertEqual((await owner.update("member-example"))["outcome"], "updated")
        self.assertEqual(accepted_insurance(state).decision.canonicalPlan, "Aetna")
        self.assertEqual(state.patient.active.insuranceDecision.canonicalPlan, "Aetna")
        self.assertEqual(accepted_insurance(state).decision.canonicalPlan, "Aetna")
        self.assertTrue(insurance_ready(state))

    async def test_no_effect_update_preserves_context_and_allows_corrected_retry(self):
        for status_code in (200, 400):
            with self.subTest(status_code=status_code):

                async def rejected(request):
                    return httpx.Response(
                        status_code,
                        json={
                            "status": "error",
                            "effect": "no_effect",
                            "outcome": "validation_failed",
                        },
                    )

                state, _, owner = self.owner([rejected, updated()])
                state.patient.active = Receipt.model_validate(receipt())
                await owner.check("Aetna", "medical")
                result = await owner.update("wrong-member")
                self.assertEqual(result["outcome"], "failed")
                self.assertIn("validation_failed", result["answer"])
                self.assertNotIn("Do not repeat", result["answer"])
                self.assertEqual(
                    state.patient.active.insuranceCarrier, "Test Insurance"
                )
                self.assertFalse(state.insurance.write_uncertain)
                self.assertEqual(
                    (await owner.update("correct-member"))["outcome"], "updated"
                )
                self.assertEqual(
                    self.requests[-1][1]["subscriberNum"], "correct-member"
                )
                self.assertEqual(len(self.requests), 2)

    async def test_partial_and_uncertain_updates_block_all_further_mutations(self):
        for effect in ("partial", "uncertain", "unknown", None):
            with self.subTest(effect=effect):
                state, _, owner = self.owner(
                    [
                        {
                            "status": "error",
                            "effect": effect,
                            "outcome": "reconciled_failure",
                        }
                    ]
                )
                state.patient.active = Receipt.model_validate(receipt())
                await owner.check("Aetna", "medical")
                result = await owner.update("member-example")
                self.assertEqual(
                    result["outcome"], "partial" if effect == "partial" else "uncertain"
                )
                if effect == "partial":
                    self.assertIn("replacement was not attached", result["answer"])
                self.assertFalse(insurance_ready(state))
                await owner.check("VSP", "routine_vision")
                self.assertEqual(
                    (await owner.update("correct-member"))["outcome"],
                    "partial" if effect == "partial" else "uncertain",
                )
                self.assertEqual(len(self.requests), 1)

    async def test_update_does_not_need_provider_references_from_patient_resolution(
        self,
    ):
        state, _, owner = self.owner([updated()])
        state.patient.active = Receipt.model_validate(
            receipt(insPlanId=None, respPartyId=None)
        )
        await owner.check("Aetna", "medical")
        self.assertEqual((await owner.update("member-example"))["outcome"], "updated")
        self.assertEqual(
            [path for path, _ in self.requests], ["/api/patient/update-insurance"]
        )

    async def test_failed_or_mismatched_update_blocks_further_writes(self):
        for response in [
            updated(patientId="other-chart"),
            updated(newInsurance="other-plan"),
            {"status": "error", "outcome": "reconciled_failure"},
            {"status": "updated"},
            {k: v for k, v in updated().items() if k != "effect"},
        ]:
            state, _, owner = self.owner([response])
            state.patient.active = Receipt.model_validate(receipt())
            (await owner.check("Aetna", "medical"))
            self.assertEqual(
                (await owner.update("member-example"))["outcome"], "uncertain"
            )
            self.assertFalse(insurance_ready(state))
            self.assertEqual(state.patient.active.insuranceCarrier, "Test Insurance")
            await owner.update("member-other")
            self.assertEqual(len(self.requests), 1)

    async def test_stale_update_does_not_modify_new_patient(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            await finish.wait()
            return httpx.Response(200, json=updated())

        state, resolver, owner = self.owner([delayed])
        state.patient.active = Receipt.model_validate(receipt())
        (await owner.check("Aetna", "medical"))
        task = asyncio.create_task(owner.update("member-example"))
        await entered.wait()
        await resolver.resolve("John", None)
        finish.set()
        result = await task
        self.assertEqual(result["outcome"], "updated")
        self.assertIn("context changed", result["answer"])
        self.assertIsNone(state.patient.active)
        self.assertIsNone(accepted_insurance(state))

    async def test_update_survives_cancellation_and_keeps_newer_plan_correction(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            await finish.wait()
            return httpx.Response(200, json=updated())

        state, _, owner = self.owner([delayed])
        state.patient.active = Receipt.model_validate(receipt())
        (await owner.check("Aetna", "medical"))
        task = asyncio.create_task(owner.update("member-example"))
        await entered.wait()
        self.assertFalse(insurance_ready(state))
        self.assertEqual(
            (await owner.update("member-example"))["outcome"], "write_pending"
        )
        (await owner.check("VSP", "routine_vision"))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        finish.set()
        await owner.aclose()
        self.assertEqual(state.patient.active.insuranceCarrier, "Aetna")
        self.assertIsNone(accepted_insurance(state))
        self.assertFalse(state.insurance.write_uncertain)
        self.assertEqual(len(self.requests), 1)

    async def test_new_plan_can_be_changed_back_without_replaying_an_old_receipt(self):
        state, _, owner = self.owner(
            [
                updated(),
                updated(newInsurance="VSP"),
                updated(),
            ]
        )
        state.patient.active = Receipt.model_validate(receipt())
        for plan, coverage in [
            ("Aetna", "medical"),
            ("VSP", "routine_vision"),
            ("Aetna", "medical"),
        ]:
            (await owner.check(plan, coverage))
            self.assertEqual(
                (await owner.update("member-example"))["outcome"], "updated"
            )
        self.assertEqual(len(self.requests), 3)
        writes = [
            body for path, body in self.requests if path.endswith("update-insurance")
        ]
        self.assertEqual(
            [body["insurance"] for body in writes],
            ["Aetna", "VSP", "Aetna"],
        )
        self.assertTrue(insurance_ready(state))

    async def test_queued_write_rechecks_context_before_dispatch(self):
        for operation in ("create", "update"):
            for correction in ("patient", "plan"):
                with self.subTest(operation=operation, correction=correction):
                    state, resolver, owner = self.owner(
                        [created() if operation == "create" else updated()]
                    )
                    if operation == "update":
                        state.patient.active = Receipt.model_validate(receipt())
                    await owner.check("Aetna", "medical")

                    async def correct_context():
                        self.assertTrue(state.insurance.write_pending)
                        if correction == "patient":
                            await resolver.resolve("John", None)
                        else:
                            await owner.check("VSP", "routine_vision")

                    # The write owner queues its shielded task after this correction.
                    writing = asyncio.create_task(
                        owner.add(registration())
                        if operation == "create"
                        else owner.update("member-example")
                    )
                    correcting = asyncio.create_task(correct_context())
                    result, _ = await asyncio.gather(writing, correcting)
                    self.assertEqual(self.requests, [])
                    self.assertEqual(result["outcome"], "stale")
                    self.assertIn(
                        "No registration or insurance change was sent", result["answer"]
                    )
                    self.assertFalse(state.insurance.write_pending)
                    self.assertFalse(state.insurance.write_uncertain)

                    if correction == "plan":
                        # An undispatched attempt must not consume the write receipt.
                        await owner.check("Aetna", "medical")
                        retried = (
                            await owner.add(registration())
                            if operation == "create"
                            else await owner.update("member-example")
                        )
                        self.assertEqual(
                            retried["outcome"],
                            "created" if operation == "create" else "updated",
                        )
                        self.assertEqual(len(self.requests), 1)

    async def test_http_failure_and_deadline_never_repeat_write(self):
        for delayed in [False, True]:

            async def failure(request, delayed=delayed):
                if delayed:
                    await asyncio.sleep(0.05)
                return httpx.Response(503)

            _, _, owner = await self.prepared([failure])
            owner._middleware._deadline = 0.01
            result = await owner.add(registration())
            self.assertEqual(result["outcome"], "uncertain")
            await owner.add(registration())
            self.assertEqual(len(self.requests), 2)

    async def test_changed_surname_does_not_authorize_duplicate_after_stale_creation(
        self,
    ):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            await finish.wait()
            return httpx.Response(200, json=created())

        state, resolver, owner = await self.prepared([delayed, search()])
        task = asyncio.create_task(owner.add(registration()))
        await entered.wait()
        await resolver.resolve("John", None)
        finish.set()
        await task
        await resolver.resolve("Jane", "01/02/1980")
        (await owner.check("Aetna", "medical"))
        result = await owner.add(registration(lastName="Smith"))
        self.assertEqual(result["outcome"], "created")
        self.assertIn("Doe, Jane", result["answer"])
        self.assertIsNone(state.patient.active)
        self.assertEqual(len(self.requests), 3)

    async def test_patient_reload_cannot_restore_insurance_before_completed_update(
        self,
    ):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def reload(request):
            entered.set()
            await finish.wait()
            return httpx.Response(200, json=receipt())

        state, resolver, owner = self.owner([reload, updated()])
        state.patient.active = Receipt.model_validate(
            receipt(appointmentsStatus="error")
        )
        (await owner.check("Aetna", "medical"))
        reading = asyncio.create_task(resolver.resolve("Jane", "01/02/1980"))
        await entered.wait()
        self.assertEqual((await owner.update("member-example"))["outcome"], "updated")
        finish.set()
        self.assertEqual((await reading)["outcome"], "superseded")
        self.assertEqual(state.patient.active.insuranceCarrier, "Aetna")
