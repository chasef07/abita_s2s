"""Public owner interface with real HTTP validation and deterministic middleware."""

import asyncio
import json
import unittest
from unittest.mock import Mock

import httpx
from test_patient_resolution import CONFIG, call_state, receipt, search

from insurance_fixtures import decision, check_response
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration, Registration, normalize
from abita_s2s.insurance_state import accepted_insurance, insurance_ready
from abita_s2s.middleware import PatientMiddleware, Receipt
from abita_s2s.registration_middleware import RegistrationMiddleware


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
        "insuranceDecision": decision(changes.get("newInsurance", "Aetna"), "routine_vision" if changes.get("newInsurance") == "VSP" else "medical"),
        "patientId": "chart-jane",
        "newInsurance": "Aetna",
        "routing": "all",
        "allowedProviders": ["Dr. Example"],
    } | changes


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    def test_receipt_comparison_ignores_punctuation_but_preserves_product(self):
        self.assertEqual(normalize("HMO & PPO"), normalize("HMO and PPO"))
        self.assertEqual(normalize("Cigna: Open-Access"), normalize("CIGNA Open Access"))
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

    async def test_full_creation_validates_identity_and_caches_duplicate(self):
        state, _, owner = await self.prepared([created()])
        result = await owner.add(registration())
        state.reporter.record.assert_called_once_with(
            "patient", {"outcome": "created", "externalPatientId": "new-chart", "superseded": False}, call_id=None
        )
        self.assertEqual(result["outcome"], "created")
        self.assertEqual(state.patient.active.patientId, "new-chart")
        self.assertIsNone(state.patient.active.insPlanId)
        self.assertTrue(insurance_ready(state, "medical"))
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
        self.assertEqual(state.patient.active.patientId, "new-chart")
        self.assertTrue(insurance_ready(state, "medical"))
        self.assertEqual([path for path, _ in self.requests], ["/api/add-patient"])
        self.assertEqual(await owner.add(registration()), result)
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
                [created(insuranceDecision=decision(plan, "routine_vision"))], plan=plan, coverage="routine_vision"
            )
            await owner.add(
                registration(
                    phone="5555550999", inboundPhoneConfirmed=None
                )
            )
            body = self.requests[-1][1]
            self.assertEqual(body["subscriberNum"], member)
            self.assertNotIn("ssn", body)
            self.assertEqual(body["coverageType"], "routine_vision")
            self.assertEqual(state.call.caller_phone, "+15555550101")
            self.assertEqual(state.patient.active.phone, "5555550999")

    async def test_creation_without_backend_decision_cannot_reuse_previous_check(self):
        state, _, owner = await self.prepared([created(insuranceDecision=None)])
        self.assertEqual((await owner.add(registration()))["outcome"], "created")
        self.assertIsNone(accepted_insurance(state))
        self.assertFalse(insurance_ready(state, "medical"))

    async def test_write_decision_must_match_requested_plan_office_and_coverage(self):
        for changed in (
            decision("Different Product"), decision(office="hollywood"),
            decision(coverage="routine_vision"),
        ):
            with self.subTest(decision=changed):
                state, _, owner = await self.prepared([created(insuranceDecision=changed)])
                self.assertEqual((await owner.add(registration()))["outcome"], "uncertain")
                self.assertTrue(state.insurance.write_uncertain)
                self.assertFalse(insurance_ready(state, "medical"))
                await owner.add(registration())
                self.assertEqual(len(self.requests), 2)

    async def test_partial_receipt_keeps_chart_and_blocks_scheduling_and_duplicate(
        self,
    ):
        state, _, owner = await self.prepared([created("partial")])
        result = await owner.add(registration())
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(state.patient.active.patientId, "new-chart")
        self.assertFalse(insurance_ready(state, "medical"))
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

    async def test_explicit_creation_rejection_preserves_failure(self):
        state, _, owner = await self.prepared(
            [{"status": "error", "outcome": "rejected"}]
        )
        self.assertEqual((await owner.add(registration()))["outcome"], "failed")
        self.assertFalse(state.insurance.write_uncertain)
        self.assertIsNone(state.patient.active)
        await owner.add(registration())
        self.assertEqual(len(self.requests), 2)

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

    async def test_update_uses_verified_backend_refs_and_receipt_and_no_duplicate(self):
        state, _, owner = self.owner([updated()])
        state.patient.active = Receipt.model_validate(receipt())
        (await owner.check("Aetna", "medical"))
        result = await owner.update("member-example")
        self.assertEqual(result["outcome"], "updated")
        self.assertEqual(self.requests[0][1]["insPlanId"], "private-plan")
        self.assertEqual(self.requests[0][1]["respPartyId"], "private-party")
        self.assertEqual(state.patient.active.insuranceCarrier, "Aetna")
        self.assertIsNone(state.patient.active.insPlanId)
        self.assertTrue(insurance_ready(state, "medical"))
        await owner.update("member-example")
        self.assertEqual(len(self.requests), 1)

    async def test_update_without_decision_does_not_reuse_on_file_or_checked_acceptance(self):
        state, _, owner = self.owner([updated(insuranceDecision=None)])
        state.patient.active = Receipt.model_validate(receipt())
        await owner.check("Aetna", "medical")
        self.assertEqual((await owner.update("member-example"))["outcome"], "updated")
        self.assertIsNone(accepted_insurance(state))
        self.assertIsNone(state.patient.active.insuranceDecision)
        self.assertTrue(insurance_ready(state, "medical"))

    async def test_failed_or_mismatched_update_blocks_further_writes(self):
        for response in [
            updated(patientId="other-chart"),
            updated(newInsurance="other-plan"),
            {"status": "error", "outcome": "reconciled_failure"},
            {"status": "updated"},
        ]:
            state, _, owner = self.owner([response])
            state.patient.active = Receipt.model_validate(
                receipt()
            )
            (await owner.check("Aetna", "medical"))
            self.assertEqual(
                (await owner.update("member-example"))["outcome"], "uncertain"
            )
            self.assertFalse(insurance_ready(state, "medical"))
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
        self.assertFalse(insurance_ready(state, "medical"))
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
                receipt(insuranceCarrier="Aetna", insPlanId="aetna-plan"),
                updated(newInsurance="VSP"),
                receipt(insuranceCarrier="VSP", insPlanId="vsp-plan"),
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
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(self.requests[-1][1]["oldInsurance"], "VSP")
        writes = [
            body for path, body in self.requests if path.endswith("update-insurance")
        ]
        self.assertEqual(
            [body["insPlanId"] for body in writes],
            ["private-plan", "aetna-plan", "vsp-plan"],
        )
        self.assertTrue(insurance_ready(state, "medical"))

    async def test_unverified_references_never_dispatch_an_insurance_write(self):
        for fresh in [
            receipt(insuranceCarrier="Aetna", insPlanId=None),
            receipt(insuranceCarrier="Aetna", respPartyId=None),
            receipt(insuranceCarrier="Unexpected Plan"),
            receipt(patient_id="other-chart", insuranceCarrier="Aetna"),
            receipt(name="Other", insuranceCarrier="Aetna"),
            receipt(dob="02/03/1981", insuranceCarrier="Aetna"),
            {"status": "not_found"},
        ]:
            state, _, owner = self.owner([fresh])
            state.patient.active = Receipt.model_validate(
                receipt(insuranceCarrier="Aetna", insPlanId=None)
            )
            (await owner.check("VSP", "routine_vision"))
            self.assertEqual(
                (await owner.update("member-example"))["outcome"],
                "needs_staff_review",
            )
            self.assertEqual(
                [path for path, _ in self.requests], ["/api/patient/resolve"]
            )
            self.assertFalse(state.insurance.write_uncertain)

    async def test_changed_context_during_reference_refresh_never_dispatches_write(
        self,
    ):
        for correction in ("patient", "plan"):
            entered, finish = asyncio.Event(), asyncio.Event()

            async def reload(request, entered=entered, finish=finish):
                entered.set()
                await finish.wait()
                return httpx.Response(200, json=receipt(insuranceCarrier="Aetna"))

            state, resolver, owner = self.owner([reload])
            state.patient.active = Receipt.model_validate(
                receipt(insuranceCarrier="Aetna", insPlanId=None)
            )
            (await owner.check("VSP", "routine_vision"))
            task = asyncio.create_task(owner.update("member-example"))
            await entered.wait()
            if correction == "patient":
                await resolver.resolve("John", None)
            else:
                (await owner.check("Aetna", "medical"))
            finish.set()
            self.assertEqual((await task)["outcome"], "needs_staff_review")
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
        self.assertIsNone(state.patient.active.insPlanId)
