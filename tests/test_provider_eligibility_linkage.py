"""Synthetic HTTP-to-closeout proof for provider-specific eligibility links."""

import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

import httpx
from insurance_fixtures import check_response, decision
from test_insurance_registration import created, registration
from test_patient_resolution import CONFIG, call_state
from test_reporting import ACK
from test_scheduling import NOW, booking, inventory

from abita_s2s.eligibility_contract import EligibilityInput
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.insurance_state import appointment_eligibility
from abita_s2s.integrations.patient_middleware import PatientMiddleware
from abita_s2s.integrations.registration_middleware import RegistrationMiddleware
from abita_s2s.integrations.scheduling_http import SchedulingHTTP
from abita_s2s.runtime.reporting import CallReporter
from abita_s2s.scheduling import Scheduling


def batch():
    identity = {"status": "matched_with_name_correction", "reviewRequired": False}
    person = {
        "firstName": "Jane",
        "lastName": "Doe",
        "dateOfBirth": "19800102",
        "memberId": "member-example",
    }
    common = {"officeId": "spring_hill", "checkedAt": "2026-09-23T12:00:00Z"}
    providers = []
    for profile, status in [("13", "active"), ("14", "active"), ("15", "active")]:
        providers.append(
            {
                **common,
                "status": status,
                "identity": identity,
                "matchedPatient": person,
                "provider": {
                    "profileId": profile,
                    "name": f"Dr. Example {profile}",
                    "firstName": "Example",
                    "lastName": profile,
                    "npi": f"10000000{profile}",
                },
                "providerResponse": {
                    "benefitsInformation": [
                        {"serviceTypeCodes": ["98"], "benefitAmount": profile}
                    ],
                    "future": {"original": profile},
                },
            }
        )
    return {
        **common,
        "status": "active",
        "identity": identity,
        "matchedPatient": person,
        "providerResults": providers,
    }


def intake(**changes):
    return EligibilityInput(
        **(
            {
                "firstName": "Ane",
                "lastName": "Doe",
                "dob": "01/02/1980",
                "memberId": "member-example",
                "plan": "Aetna",
            }
            | changes
        )
    )


class ProviderEligibilityLinkageTests(unittest.IsolatedAsyncioTestCase):
    def harness(self, *, receipt_changes=None, booking_wait=None):
        state = call_state()
        product = []
        requests = []
        response = batch()

        async def receive(request):
            body = json.loads(request.content)
            requests.append((request.url.path, body))
            path = request.url.path
            if path == "/v1/ai/interactions":
                product.append(body)
                return httpx.Response(201, json=ACK)
            if path == "/api/insurance/decision":
                return check_response(request)
            if path == "/api/eligibility/check":
                return httpx.Response(200, json=response)
            if path == "/api/add-patient":
                return httpx.Response(
                    200, json=created(name=f"{body['lastName']}, {body['firstName']}")
                )
            if path == "/api/scheduler/slots":
                value = inventory()
                value["slots"][0]["provider"] = "Dr. Example"
                if body["startDate"] == "2026-09-16":
                    value["slots"][0].update(
                        profileId=14,
                        datetime="2026-09-16T09:00",
                        bookingToken="replacement-slot",
                    )
                return httpx.Response(200, json=value)
            if path == "/api/appointment/book":
                if booking_wait:
                    booking_wait[0].set()
                    await booking_wait[1].wait()
                return httpx.Response(
                    200, json=booking() | {"profileId": "13"} | (receipt_changes or {})
                )
            if path == "/api/appointment/reschedule":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "booking": booking(999) | {"profileId": "14"},
                        "cancellation": {"status": "cancelled", "appointmentId": 888},
                    },
                )
            if path == "/api/patient/update-insurance":
                return httpx.Response(
                    200,
                    json={
                        "status": "updated",
                        "effect": "completed",
                        "patientId": "new-chart",
                        "newInsurance": body["insurance"],
                        "routing": "all",
                        "allowedProviders": ["Dr. Example"],
                        "insuranceDecision": decision(
                            body["insurance"], body["coverageType"]
                        ),
                    },
                )
            self.fail(f"Unexpected synthetic route {path}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
        self.addAsyncCleanup(client.aclose)
        resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
        self.addAsyncCleanup(resolver.aclose)
        insurance = InsuranceRegistration(
            state, resolver, RegistrationMiddleware(client, CONFIG)
        )
        self.addAsyncCleanup(insurance.aclose)
        scheduler = Scheduling(state, SchedulingHTTP(client, CONFIG), now=lambda: NOW)
        self.addAsyncCleanup(scheduler.aclose)

        async def drain():
            await insurance.aclose()
            await scheduler.aclose()

        reporter = CallReporter(
            state.call,
            client,
            replace(
                CONFIG,
                interaction_url="https://product.test/v1/ai/interactions",
                product_secret="offline-secret",
            ),
            drain,
            insurance=state.insurance,
        )
        reporter.started = True
        state.reporter = reporter
        return SimpleNamespace(
            state=state,
            insurance=insurance,
            scheduler=scheduler,
            reporter=reporter,
            product=product,
            response=response,
            requests=requests,
        )

    async def register(self, h, *, first="Jane"):
        await h.insurance.check("Aetna", "medical")
        self.assertIn("name_correction:", await h.insurance.eligibility(intake()))
        self.assertEqual(
            (
                await h.insurance.add(
                    registration(firstName=first, subscriberName=f"{first} Doe")
                )
            )["outcome"],
            "created",
        )
        return h.state.insurance.eligibility_checks[0]

    async def book(self, h):
        available = await h.scheduler.availability("medical")
        self.assertEqual(available["outcome"], "found", available)
        ref = available["slots"][0]["appointmentSlotRef"]
        context = SimpleNamespace(
            userdata=h.state, function_call=SimpleNamespace(call_id="synthetic-booking")
        )
        return await h.scheduler.book(
            context,
            slot_ref=ref,
            reason="Eye irritation",
            referrer="none",
            confirmed=True,
        )

    async def closeout(self, h):
        await h.reporter.finish()
        return next(p for p in reversed(h.product) if p["kind"] == "CLOSEOUT")

    async def test_correction_batch_registration_booking_closeout(self):
        h = self.harness()
        check = await self.register(h)
        self.assertEqual(check.patient_id, "new-chart")
        self.assertIn("success:", await self.book(h))
        closeout = await self.closeout(h)
        saved = closeout["closeoutPayload"]["eligibilityChecks"][0]
        self.assertEqual(saved["id"], check.id)
        self.assertEqual(saved["externalPatientId"], "new-chart")
        self.assertEqual(
            [r["status"] for r in saved["result"]["providerResults"]],
            ["active", "active", "active"],
        )
        self.assertEqual(
            [r["providerResponse"] for r in saved["result"]["providerResults"]],
            [r["providerResponse"] for r in h.response["providerResults"]],
        )
        receipt = closeout["appointmentOutcome"]["bookingResult"]
        self.assertEqual(receipt["eligibilityCheckId"], check.id)
        self.assertEqual(receipt["providerProfileId"], "13")
        domain = closeout["closeoutPayload"]["domainOutcomes"][-1]["evidence"]
        self.assertEqual(domain["bookingResult"], receipt)

    async def test_partial_batch_keeps_each_doctor_result_without_name_correction(self):
        h = self.harness()
        h.response.update(
            status="review",
            reviewReason="provider_results_disagree",
            identity={"status": "provider_results_disagree", "reviewRequired": True},
        )
        h.response.pop("matchedPatient")
        for child, status in zip(
            h.response["providerResults"], ["active", "inactive", "unknown"]
        ):
            child["status"] = status
        await h.insurance.check("Aetna", "medical")
        self.assertNotIn("name_correction:", await h.insurance.eligibility(intake()))
        self.assertEqual(
            (
                await h.insurance.add(
                    registration(firstName="Ane", subscriberName="Ane Doe")
                )
            )["outcome"],
            "created",
        )
        check = h.state.insurance.eligibility_checks[0]
        await self.book(h)
        closeout = await self.closeout(h)
        saved = closeout["closeoutPayload"]["eligibilityChecks"][0]["result"]
        self.assertEqual(saved["status"], "review")
        self.assertEqual(
            [child["status"] for child in saved["providerResults"]],
            ["active", "inactive", "unknown"],
        )
        self.assertEqual(
            [child["providerResponse"] for child in saved["providerResults"]],
            [child["providerResponse"] for child in h.response["providerResults"]],
        )
        self.assertEqual(
            closeout["appointmentOutcome"]["bookingResult"]["eligibilityCheckId"],
            check.id,
        )
        self.assertEqual(
            closeout["appointmentOutcome"]["bookingResult"]["providerProfileId"], "13"
        )

    async def test_same_member_and_dob_different_registered_name_does_not_bind(self):
        h = self.harness()
        check = await self.register(h, first="John")
        self.assertIsNone(check.patient_id)
        await self.book(h)
        self.assertNotIn(
            "eligibilityCheckId",
            (await self.closeout(h))["appointmentOutcome"]["bookingResult"],
        )

    async def test_insurance_update_and_return_to_same_plan_does_not_revive_batch(self):
        h = self.harness()
        check = await self.register(h)
        await h.insurance.check("VSP", "routine_vision")
        self.assertEqual((await h.insurance.update("new-member"))["outcome"], "updated")
        await h.insurance.check("Aetna", "medical")
        self.assertEqual(
            (await h.insurance.update("member-example"))["outcome"], "updated"
        )
        self.assertTrue(check.invalidated)
        self.assertIsNone(appointment_eligibility(h.state, "new-chart", "medical"))
        await self.book(h)
        self.assertNotIn(
            "eligibilityCheckId",
            (await self.closeout(h))["appointmentOutcome"]["bookingResult"],
        )

    async def test_missing_mismatched_provider_or_office_does_not_link(self):
        for changes in [
            {"profileId": None},
            {"appointmentId": None},
            {"profileId": "14"},
            {"officeId": "hollywood"},
            {"visitType": "routine_vision"},
        ]:
            with self.subTest(changes=changes):
                h = self.harness(receipt_changes=changes)
                await self.register(h)
                await self.book(h)
                self.assertNotIn(
                    "eligibilityCheckId",
                    (await self.closeout(h))["appointmentOutcome"]["bookingResult"],
                )

    async def test_booking_keeps_captured_batch_when_current_intake_changes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        h = self.harness(booking_wait=(entered, release))
        check = await self.register(h)
        booking_task = asyncio.create_task(self.book(h))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            await h.insurance.eligibility(
                intake(firstName="Other", memberId="other-member")
            )
            self.assertIsNot(h.state.insurance.current_eligibility[1], check)
        finally:
            release.set()
        await booking_task
        closeout = await self.closeout(h)
        self.assertEqual(
            closeout["appointmentOutcome"]["externalPatientId"], "new-chart"
        )
        self.assertEqual(
            closeout["appointmentOutcome"]["bookingResult"]["eligibilityCheckId"],
            check.id,
        )

    async def test_reschedule_preserves_each_provider_link_without_rechecking(self):
        h = self.harness()
        check = await self.register(h)
        self.assertIn("success:", await self.book(h))
        old_ref = h.scheduler.appointments()[0]["appointmentRef"]
        replacement = await h.scheduler.availability("medical", start="2026-09-16")
        self.assertEqual(replacement["outcome"], "found")
        context = SimpleNamespace(
            userdata=h.state,
            function_call=SimpleNamespace(call_id="synthetic-reschedule"),
        )
        result = await h.scheduler.reschedule(
            context,
            old_ref=old_ref,
            slot_ref=replacement["slots"][0]["appointmentSlotRef"],
            reason="Eye irritation",
            referrer="none",
            confirmed=True,
        )
        self.assertIn("success:", result)
        closeout = await self.closeout(h)
        events = [
            item["evidence"]["bookingResult"]
            for item in closeout["closeoutPayload"]["domainOutcomes"]
            if "bookingResult" in item["evidence"]
        ]
        self.assertEqual([item["appointmentId"] for item in events], [888, 999])
        self.assertEqual([item["providerProfileId"] for item in events], ["13", "14"])
        self.assertEqual(
            [item["eligibilityCheckId"] for item in events], [check.id, check.id]
        )
        self.assertEqual(
            sum(path == "/api/eligibility/check" for path, _ in h.requests), 1
        )
