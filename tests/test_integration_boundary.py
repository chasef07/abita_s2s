"""Typed requests keep the wire bytes, and adapters parse every outcome at the boundary."""

import unittest

import httpx
from pydantic import ValidationError
from test_patient_resolution import CONFIG

from insurance_fixtures import decision
from abita_s2s.integrations.patient_middleware import (
    PatientMiddleware,
    ResolveRequest,
)
from abita_s2s.integrations.registration_middleware import (
    DecisionRequest,
    InsuranceChange,
    NewPatient,
    RegistrationMiddleware,
    WriteFailure,
)
from abita_s2s.integrations.scheduling_http import (
    Booking,
    Cancellation,
    RescheduleReceipt,
    SchedulingFailure,
    SchedulingHTTP,
    SlotSearch,
    WriteReceipt,
)
from abita_s2s.offices import SPRING_HILL


def wire(body: dict) -> bytes:
    return httpx.Request("POST", "https://wire.test", json=body).content


def new_patient(**changes):
    return NewPatient(
        firstName="Jane",
        lastName="Doe",
        dob="01/02/1980",
        street="1 Example Street",
        city="Example",
        state="FL",
        zip="12345",
        sex="female",
        phone="5555550101",
        aptSuite="",
        insurance="Aetna",
        subscriberName="Jane Doe",
        subscriberNum="member-example",
        office="spring-hill",
        **changes,
    )


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0) if self.responses else {}
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def client(self, recorder):
        client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
        self.addAsyncCleanup(client.aclose)
        return client

    async def sent(self, call, *responses) -> bytes:
        recorder = Recorder(*responses)
        await call(self.client(recorder))
        self.assertEqual(len(recorder.requests), 1)
        return recorder.requests[0].content

    async def test_middleware_requests_match_previous_dict_bodies(self):
        cases = [
            (
                lambda c: PatientMiddleware(c, CONFIG).resolve(
                    ResolveRequest(phone="+15555550101", office="spring-hill")
                ),
                {"phone": "+15555550101", "office": "spring-hill"},
            ),
            (
                lambda c: PatientMiddleware(c, CONFIG).resolve(
                    ResolveRequest(
                        firstName="Jane", dob="01/02/1980", office="hollywood"
                    )
                ),
                {"firstName": "Jane", "dob": "01/02/1980", "office": "hollywood"},
            ),
            (
                lambda c: PatientMiddleware(c, CONFIG).resolve(
                    ResolveRequest(patientId="chart-jane", office="spring-hill")
                ),
                {"patientId": "chart-jane", "office": "spring-hill"},
            ),
            (
                lambda c: RegistrationMiddleware(c, CONFIG).check(
                    DecisionRequest(
                        office="spring-hill",
                        plan="Aetna",
                        coverageType="medical",
                        dob="",
                    )
                ),
                {
                    "office": "spring-hill",
                    "plan": "Aetna",
                    "coverageType": "medical",
                    "dob": "",
                },
            ),
            (
                lambda c: RegistrationMiddleware(c, CONFIG).check(
                    DecisionRequest(
                        office="spring-hill",
                        plan="Aetna",
                        coverageType="routine_vision",
                        dob="01/02/1980",
                        offeredPlanIds=["aetna", "vsp"],
                    )
                ),
                {
                    "office": "spring-hill",
                    "plan": "Aetna",
                    "coverageType": "routine_vision",
                    "dob": "01/02/1980",
                    "offeredPlanIds": ["aetna", "vsp"],
                },
            ),
            (
                lambda c: RegistrationMiddleware(c, CONFIG).create(new_patient()),
                {
                    "firstName": "Jane",
                    "lastName": "Doe",
                    "dob": "01/02/1980",
                    "street": "1 Example Street",
                    "city": "Example",
                    "state": "FL",
                    "zip": "12345",
                    "sex": "female",
                    "phone": "5555550101",
                    "aptSuite": "",
                    "insurance": "Aetna",
                    "subscriberName": "Jane Doe",
                    "subscriberNum": "member-example",
                    "office": "spring-hill",
                },
            ),
            (
                lambda c: RegistrationMiddleware(c, CONFIG).create(
                    new_patient(
                        insurancePlanId="vsp",
                        email="jane@example.test",
                        coverageType="routine_vision",
                    )
                ),
                {
                    "firstName": "Jane",
                    "lastName": "Doe",
                    "dob": "01/02/1980",
                    "street": "1 Example Street",
                    "city": "Example",
                    "state": "FL",
                    "zip": "12345",
                    "sex": "female",
                    "phone": "5555550101",
                    "aptSuite": "",
                    "insurance": "Aetna",
                    "subscriberName": "Jane Doe",
                    "subscriberNum": "member-example",
                    "insurancePlanId": "vsp",
                    "email": "jane@example.test",
                    "coverageType": "routine_vision",
                    "office": "spring-hill",
                },
            ),
            (
                lambda c: RegistrationMiddleware(c, CONFIG).update(
                    InsuranceChange(
                        patientId="chart-jane",
                        dob="01/02/1980",
                        insurance="Aetna",
                        coverageType="medical",
                        subscriberNum="member-example",
                        insurancePlanId="aetna",
                        office="spring-hill",
                    )
                ),
                {
                    "patientId": "chart-jane",
                    "dob": "01/02/1980",
                    "insurance": "Aetna",
                    "coverageType": "medical",
                    "subscriberNum": "member-example",
                    "insurancePlanId": "aetna",
                    "office": "spring-hill",
                },
            ),
            (
                lambda c: SchedulingHTTP(c, CONFIG).availability(
                    SlotSearch(
                        office="sweetwater",
                        startDate="2026-06-03",
                        patientId="chart-jane",
                        visitType="medical",
                        dob="01/02/1980",
                    )
                ),
                {
                    "office": "sweetwater",
                    "startDate": "2026-06-03",
                    "patientId": "chart-jane",
                    "visitType": "medical",
                    "dob": "01/02/1980",
                },
            ),
            (
                lambda c: SchedulingHTTP(c, CONFIG).book(
                    Booking(
                        patientId="chart-jane",
                        patientName="Doe, Jane",
                        dob="01/02/1980",
                        bookingToken="token",
                        visitCategory="medical",
                        patientStatus="established",
                        appointmentReason="Blurry vision",
                        visitReason="Blurry vision",
                        referringDoctor="none",
                    )
                ),
                {
                    "patientId": "chart-jane",
                    "patientName": "Doe, Jane",
                    "dob": "01/02/1980",
                    "bookingToken": "token",
                    "visitCategory": "medical",
                    "patientStatus": "established",
                    "appointmentReason": "Blurry vision",
                    "visitReason": "Blurry vision",
                    "referringDoctor": "none",
                },
            ),
            (
                lambda c: SchedulingHTTP(c, CONFIG).reschedule(
                    Booking(
                        patientId="chart-jane",
                        patientName="Doe, Jane",
                        dob="01/02/1980",
                        bookingToken="token",
                        visitCategory="routine_vision",
                        patientStatus="new",
                        appointmentReason="Eye exam",
                        visitReason="Eye exam",
                        referringDoctor="Dr. Example",
                        referringDoctorPhone="555-555-0102",
                        referringDoctorAddress="2 Example Road",
                        rescheduleToken="move",
                    )
                ),
                {
                    "patientId": "chart-jane",
                    "patientName": "Doe, Jane",
                    "dob": "01/02/1980",
                    "bookingToken": "token",
                    "visitCategory": "routine_vision",
                    "patientStatus": "new",
                    "appointmentReason": "Eye exam",
                    "visitReason": "Eye exam",
                    "referringDoctor": "Dr. Example",
                    "referringDoctorPhone": "555-555-0102",
                    "referringDoctorAddress": "2 Example Road",
                    "rescheduleToken": "move",
                },
            ),
            (
                lambda c: SchedulingHTTP(c, CONFIG).cancel(
                    Cancellation(patientId="chart-jane", cancellationToken="cancel")
                ),
                {"patientId": "chart-jane", "cancellationToken": "cancel"},
            ),
        ]
        for call, previous in cases:
            with self.subTest(body=previous):
                self.assertEqual(await self.sent(call), wire(previous))

    def test_office_phone_and_unknown_fields_fail_before_send(self):
        for changes in (
            {"office": SPRING_HILL.trunk_numbers[0]},
            {"office_phone": SPRING_HILL.trunk_numbers[0]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                InsuranceChange(
                    **{
                        "patientId": "chart-jane",
                        "dob": "01/02/1980",
                        "insurance": "Aetna",
                        "coverageType": "medical",
                        "subscriberNum": "member-example",
                        "office": "spring-hill",
                    }
                    | changes
                )

    async def scheduling(self, method, *responses):
        request = (
            Cancellation(patientId="chart-jane", cancellationToken="cancel")
            if method == "cancel"
            else Booking(
                patientId="chart-jane",
                patientName="Doe, Jane",
                dob="01/02/1980",
                bookingToken="token",
                visitCategory="medical",
                patientStatus="established",
                appointmentReason="Blurry vision",
                visitReason="Blurry vision",
                referringDoctor="none",
            )
        )
        adapter = SchedulingHTTP(self.client(Recorder(*responses)), CONFIG)
        return await getattr(adapter, method)(request)

    async def test_scheduling_outcomes_parse_and_unknown_values_are_uncertain(self):
        for outcome in (
            "validation",
            "invalid_booking_token",
            "invalid_cancellation_token",
            "invalid_reschedule_token",
            "booking_token_required",
            "appointment_type_unresolved",
            "patient_context_mismatch",
            "slot_unavailable",
            "provider_conflict",
            "provider_rejected",
            "ownership_mismatch",
            "write_failed",
            "indeterminate_write",
            "",
        ):
            with self.subTest(outcome=outcome):
                result = await self.scheduling(
                    "book", {"status": "error", "outcome": outcome}
                )
                self.assertIsInstance(result, WriteReceipt)
                self.assertEqual(result.outcome, outcome or None)
        for status in ("booked", "cancelled"):
            with self.subTest(status=status):
                result = await self.scheduling(
                    "cancel", {"status": status, "appointmentId": 7}
                )
                self.assertEqual(result.status, status)
        for status in ("completed", "partial", "failed", "uncertain"):
            with self.subTest(status=status):
                booked = {"status": "booked", "appointmentId": 8}
                cancelled = {"status": "cancelled", "appointmentId": 7}
                body = {
                    "completed": {"booking": booked, "cancellation": cancelled},
                    "partial": {"booking": booked, "outcome": "write_failed"},
                }.get(status, {"outcome": "indeterminate_write"})
                result = await self.scheduling("reschedule", {"status": status, **body})
                self.assertIsInstance(result, RescheduleReceipt)
                self.assertEqual(result.status, status)
        for method, body in (
            ("book", {"status": "error", "outcome": "slot_taken"}),
            ("book", {"status": "pending", "appointmentId": 8}),
            ("cancel", {"status": "error", "outcome": "unexpected"}),
            ("reschedule", {"status": "failed", "outcome": "new_category"}),
        ):
            with self.subTest(method=method, body=body):
                self.assertEqual(
                    await self.scheduling(method, body),
                    SchedulingFailure(reason="invalid_response", uncertain=True),
                )

    async def registration(self, method, body, status_code=200):
        adapter = RegistrationMiddleware(
            self.client(Recorder(httpx.Response(status_code, json=body))), CONFIG
        )
        if method == "create":
            return await adapter.create(new_patient())
        return await adapter.update(
            InsuranceChange(
                patientId="chart-jane",
                dob="01/02/1980",
                insurance="Aetna",
                coverageType="medical",
                subscriberNum="member-example",
                office="spring-hill",
            )
        )

    async def test_registration_outcomes_parse_and_unknown_values_are_uncertain(self):
        for outcome, status in (
            ("validation_failed", "failed"),
            ("rejected", "failed"),
            ("reconciled_failure", "failed"),
            ("unavailable", "failed"),
            ("failed", "failed"),
            ("indeterminate_write", "uncertain"),
            ("reconciled_success", "uncertain"),
            (None, "uncertain"),
        ):
            with self.subTest(outcome=outcome):
                result = await self.registration(
                    "create", {"status": "error", "outcome": outcome}
                )
                self.assertEqual(
                    result,
                    WriteFailure(status=status, reason=outcome or "backend_failure"),
                )
        for effect, status in (
            ("no_effect", "failed"),
            ("partial", "partial"),
            ("uncertain", "uncertain"),
        ):
            with self.subTest(effect=effect):
                result = await self.registration(
                    "update",
                    {"status": "error", "effect": effect, "message": "Not changed."},
                    400,
                )
                self.assertEqual(
                    result, WriteFailure(status=status, reason="Not changed.")
                )
        accepted = decision(office="spring_hill")
        created = await self.registration(
            "create",
            {
                "status": "partial",
                "insuranceDecision": accepted,
                "patientId": "new-chart",
                "name": "Doe, Jane",
                "dob": "01/02/1980",
            },
        )
        self.assertEqual(created.status, "partial")
        for method, body in (
            ("create", {"status": "error", "outcome": "new_outcome"}),
            ("create", {"status": "queued"}),
            ("update", {"status": "error", "effect": "no_effect", "outcome": "x"}),
            ("update", {"status": "error", "effect": "completed"}),
        ):
            with self.subTest(method=method, body=body):
                self.assertEqual(
                    await self.registration(method, body),
                    WriteFailure(status="uncertain", reason="unverified_write"),
                )
