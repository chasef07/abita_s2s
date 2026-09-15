"""Exercise migration boundaries with real composition and offline HTTP."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import test_scheduling
from test_patient_resolution import CONFIG, call_state, candidate, receipt, search
from test_scheduling import NOW, inventory

from abita_s2s.call_control import CallControl
from abita_s2s.config import load_config
from abita_s2s.handoff import AdmissionRejected
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.insurance_state import AcceptedInsurance, insurance_ready
from abita_s2s.middleware import PatientMiddleware
from abita_s2s.scheduling import Scheduling
from abita_s2s.scheduling_http import SchedulingHTTP


class MigrationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_loopback_handoff_reaches_http(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                201,
                json={
                    "id": "00000000-0000-4000-8000-000000000002",
                    "expiresAt": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                    "sipDestination": "sip:acuity-handoff@product.example",
                },
            )

        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "offline",
                "ACUITY_PRODUCT_HANDOFF_URL": " http://127.0.0.1:8000/v1/handoffs/ ",
                "ABITA_EYE_GROUP_PRODUCT_PRACTICE_ID": "00000000-0000-4000-8000-000000000001",
                "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "offline",
            },
            clear=True,
        ):
            config = load_config()
            self.assertEqual(config.staff_tasks_url, "http://127.0.0.1:8000/v1/tasks")
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                control = CallControl(call_state(), client, handoff=config.handoff)
                # Admission must retain startup's normalized settings even if env changes.
                with patch.dict(
                    "os.environ",
                    {"ACUITY_PRODUCT_HANDOFF_URL": "https://wrong.example/v1/handoffs"},
                ):
                    target = await control.admission.resolve()
        self.assertTrue(target.admitted)
        self.assertEqual(str(requests[0].url), "http://127.0.0.1:8000/v1/handoffs")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer offline")

    tool = test_scheduling.SchedulingTests.tool
    book = test_scheduling.SchedulingTests.book

    async def resolved_owner(self, responses, **fields):
        state = call_state(None)
        responses = [
            search(candidate()),
            receipt(routing="bach_only", preauthRequired=False, **fields),
            *responses,
        ]
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=responses.pop(0))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
        scheduling = Scheduling(state, SchedulingHTTP(client, CONFIG), now=lambda: NOW)
        self.addAsyncCleanup(resolver.aclose)
        self.addAsyncCleanup(scheduling.aclose)
        self.assertEqual(
            (await resolver.resolve("Jane", "01/02/1980"))["outcome"], "verified"
        )
        self.assertIsNone(state.insurance.accepted)
        return scheduling, resolver, requests

    async def test_resolve_returning_patient_then_availability_and_booking(self):
        owner, _, requests = await self.resolved_owner(
            [
                inventory(),
                {"status": "booked", "appointmentId": 888},
            ]
        )
        available = await owner.availability("medical")
        self.assertEqual(available["outcome"], "found", available)
        result = await self.book(owner, available["slots"][0]["appointmentSlotRef"])
        self.assertEqual(result["outcome"], "booked")
        self.assertEqual(len(requests), 4)

    async def test_resolve_returning_patient_then_reschedule(self):
        owner, _, requests = await self.resolved_owner(
            [
                inventory(),
                {"status": "booked", "appointmentId": 888},
                {"status": "cancelled"},
            ],
            appointmentsStatus="found",
            appointments=[test_scheduling.appointment()],
        )
        old_ref = owner.appointments()[0]["appointmentRef"]
        available = await owner.availability("medical")
        self.assertEqual(available["outcome"], "found")
        result = await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=old_ref,
            appointmentSlotRef=available["slots"][0]["appointmentSlotRef"],
            appointmentReason="Annual medical follow up",
            referringDoctor="none",
            readBack=True,
        )
        self.assertEqual(result["outcome"], "rescheduled")
        self.assertEqual(
            [r.url.path for r in requests[-2:]],
            ["/api/appointment/book", "/api/appointment/cancel"],
        )

    async def test_previous_patients_check_does_not_block_returning_patient(self):
        for plan in ("Self Pay", "Unknown corrected plan"):
            with self.subTest(plan=plan):
                owner, resolver, requests = await self.resolved_owner(
                    [
                        search(candidate("chart-john", "John", "03/04/1981")),
                        receipt(
                            "chart-john",
                            "John",
                            "03/04/1981",
                            routing="bach_only",
                            preauthRequired=False,
                        ),
                        inventory(),
                        search(candidate()),
                        receipt(routing="bach_only", preauthRequired=False),
                    ]
                )
                insurance = InsuranceRegistration(owner.state, resolver, None)
                insurance.check(plan, "medical")
                await resolver.resolve("John", "03/04/1981")
                self.assertEqual(owner.state.patient.active.patientId, "chart-john")
                self.assertEqual(
                    (await owner.availability("medical"))["outcome"], "found"
                )
                self.assertEqual(len(requests), 5)
                # Switching away and back cannot revive Jane's rejected/stale check.
                await resolver.resolve("Jane", "01/02/1980")
                self.assertEqual(owner.state.patient.active.patientId, "chart-jane")
                self.assertFalse(insurance_ready(owner.state, "medical"))

    async def test_absent_patients_check_does_not_block_returning_patient(self):
        state = call_state(None)
        responses = [
            search(), search(candidate()),
            receipt(routing="bach_only", preauthRequired=False), inventory(),
        ]
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=responses.pop(0))
        )) as client:
            resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
            owner = Scheduling(state, SchedulingHTTP(client, CONFIG), now=lambda: NOW)
            self.addAsyncCleanup(resolver.aclose)
            self.addAsyncCleanup(owner.aclose)
            self.assertEqual((await resolver.resolve("John", "03/04/1981"))["outcome"], "not_found")
            insurance = InsuranceRegistration(state, resolver, None)
            insurance.check("Self Pay", "medical")
            self.assertIsNone(state.insurance.accepted.patient_id)
            self.assertEqual((await resolver.resolve("Jane", "01/02/1980"))["outcome"], "verified")
            self.assertEqual((await owner.availability("medical"))["outcome"], "found")
            self.assertEqual(responses, [])

    async def test_corrected_plan_blocks_cached_slots_but_not_exact_cancellation(self):
        owner, resolver, requests = await self.resolved_owner(
            [
                inventory(),
                {"status": "cancelled"},
            ],
            appointmentsStatus="found",
            appointments=[test_scheduling.appointment()],
        )
        available = await owner.availability("medical")
        slot = available["slots"][0]["appointmentSlotRef"]
        old_ref = owner.appointments()[0]["appointmentRef"]
        insurance = InsuranceRegistration(owner.state, resolver, None)
        insurance.check("Unknown corrected plan", "medical")
        self.assertIsNone(owner.state.insurance.accepted)
        self.assertFalse(insurance_ready(owner.state, "medical"))
        self.assertEqual((await self.book(owner, slot))["outcome"], "needs_input")
        result = await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=old_ref,
            appointmentSlotRef=slot,
            appointmentReason="Annual medical follow up",
            referringDoctor="none",
            readBack=True,
        )
        self.assertEqual(result["outcome"], "needs_input")
        self.assertEqual(
            (await owner.availability("medical"))["outcome"], "needs_input"
        )
        # Re-resolving the chart cannot silently restore a rejected on-file plan.
        await resolver.resolve("Jane", "01/02/1980")
        self.assertFalse(insurance_ready(owner.state, "medical"))
        self.assertEqual(len(requests), 3)
        result = await self.tool(owner, "cancel_appointment", appointmentRef=old_ref)
        self.assertEqual(result["outcome"], "cancelled")
        self.assertEqual(len(requests), 4)

    async def test_on_file_guard_preserves_scheduling_fences(self):
        owner, resolver, requests = await self.resolved_owner([])
        state = owner.state
        active = state.patient.active
        self.assertTrue(insurance_ready(state, "medical"))
        self.assertTrue(insurance_ready(state, "routine_vision"))
        for field, value in (
            ("insuranceCarrier", None),
            ("insuranceCarrier", "  "),
            ("routing", None),
            ("routingAmbiguous", True),
            ("preauthRequired", True),
        ):
            with self.subTest(field=field, value=value):
                state.patient.active = active.model_copy(update={field: value})
                self.assertFalse(insurance_ready(state, "medical"))
        state.patient.active = active
        for field in ("write_pending", "write_uncertain"):
            setattr(state.insurance, field, True)
            self.assertFalse(insurance_ready(state, "medical"))
            setattr(state.insurance, field, False)
        for status in ("created", "partial"):
            state.insurance.registrations[active.patientId] = status
            self.assertFalse(insurance_ready(state, "medical"))
        state.insurance.registrations.clear()
        checked = AcceptedInsurance(
            state.call.called_office_key,
            state.patient.revision,
            active.patientId,
            None,
            "Self Pay",
            "medical",
        )
        for change in (
            {"office_key": "crystal-river"},
            {"patient_revision": state.patient.revision - 1},
            {"coverage_type": "routine_vision"},
        ):
            state.insurance.accepted = replace(checked, **change)
            self.assertFalse(insurance_ready(state, "medical"))
        state.insurance.accepted = None
        insurance = InsuranceRegistration(state, resolver, None)
        insurance.check("Unknown corrected plan", "medical")
        self.assertFalse(insurance_ready(state, "medical"))
        insurance.check("Self Pay", "medical")
        self.assertTrue(insurance_ready(state, "medical"))
        self.assertFalse(insurance_ready(state, "routine_vision"))
        self.assertEqual(len(requests), 2)

    async def test_optional_practice_does_not_block_staff_tasks_or_direct_office(self):
        for practice in ("", "invalid"):
            with patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "offline",
                    "ACUITY_PRODUCT_HANDOFF_URL": "https://product.example/v1/handoffs/",
                    "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "offline",
                    "ABITA_EYE_GROUP_PRODUCT_PRACTICE_ID": practice,
                    "ACUITY_HANDOFF_URL": "https://legacy.example/admit",
                    "ACUITY_HANDOFF_SECRET": "legacy",
                },
                clear=True,
            ):
                config = load_config()
            self.assertEqual(config.staff_tasks_url, "https://product.example/v1/tasks")
            self.assertIsNone(
                config.handoff
            )  # Do not fall back across admission contracts.
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: self.fail("No admission request expected")
                )
            ) as client:
                state = call_state()
                control = CallControl(state, client, handoff=config.handoff)
                with self.assertRaises(AdmissionRejected):
                    await control.admission.resolve()
                state.call = replace(state.call, called_office_key="crystal-river")
                target = await control.admission.resolve()
                self.assertEqual(target.destination, "tel:+13527941244")
                self.assertFalse(target.admitted)

    def test_product_handoff_url_validation(self):
        env = {
            "OPENAI_API_KEY": "offline",
            "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "offline",
            "ABITA_EYE_GROUP_PRODUCT_PRACTICE_ID": "00000000-0000-4000-8000-000000000001",
        }
        for base in (
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://[::1]:8000",
            "https://product.example",
        ):
            with patch.dict(
                "os.environ",
                env | {"ACUITY_PRODUCT_HANDOFF_URL": base + "/v1/handoffs/"},
                clear=True,
            ):
                config = load_config()
            self.assertEqual(config.handoff.url, base + "/v1/handoffs")
        for url in (
            "http://external.example/v1/handoffs",
            "https://user:password@product.example/v1/handoffs",
            "https://product.example/v1/handoffs?x=1",
            "https://product.example/v1/handoffs#fragment",
        ):
            with (
                patch.dict(
                    "os.environ", env | {"ACUITY_PRODUCT_HANDOFF_URL": url}, clear=True
                ),
                self.assertRaises(ValueError),
            ):
                load_config()
