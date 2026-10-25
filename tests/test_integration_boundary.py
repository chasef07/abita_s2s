"""Typed requests keep the wire bytes, and adapters parse every outcome at the boundary."""

import asyncio
import hashlib
import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
from pydantic import ValidationError
from test_patient_resolution import CONFIG

from insurance_fixtures import decision
from abita_s2s.config import Config, HandoffConfig
from abita_s2s.integrations.patient_middleware import (
    PatientMiddleware,
    ResolveRequest,
)
from abita_s2s.integrations.product import (
    HandoffGrant,
    Product,
    Refusal,
    StaffTask,
    StaffTaskReceipt,
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

PRODUCT = Config(
    "offline",
    product_secret="test-secret",
    staff_tasks_url="https://product.test/v1/tasks",
    knowledge_url="https://product.test/v1/agent/knowledge/read",
    handoff=HandoffConfig(
        "https://product.test/v1/handoffs",
        "handoff-secret",
        "00000000-0000-4000-8000-000000000001",
    ),
)
TASK_ID = "00000000-0000-4000-8000-000000000009"


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


def staff_task(**changes):
    return StaffTask(
        callId="call",
        callerPhone="+15555550101",
        category="medication",
        message="Refill requested by caller.",
        officePhone=SPRING_HILL.trunk_numbers[0],
        source="agent",
        summary="Refill",
        urgency="normal",
        officeKey="spring-hill",
        **changes,
    )


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0) if self.responses else {}
        if isinstance(response, Exception):
            raise response
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

    async def test_product_requests_match_previous_dict_bodies(self):
        task = staff_task(
            patient={"id": "chart-jane", "name": "Doe, Jane", "dob": "01/02/1980"},
            inboundOfficePhone=SPRING_HILL.trunk_numbers[-1],
        )
        previous = {
            "callId": "call",
            "callerPhone": "+15555550101",
            "category": "medication",
            "message": "Refill requested by caller.",
            "officePhone": SPRING_HILL.trunk_numbers[0],
            "source": "agent",
            "summary": "Refill",
            "urgency": "normal",
            "patient": {"id": "chart-jane", "name": "Doe, Jane", "dob": "01/02/1980"},
            "inboundOfficePhone": SPRING_HILL.trunk_numbers[-1],
            "officeKey": "spring-hill",
        }
        previous["idempotencyKey"] = (
            "staff_task_"
            + hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()
        )
        receipt = {
            "status": "created",
            "taskId": TASK_ID,
            "category": "medication",
            "urgency": "normal",
        }
        self.assertEqual(
            await self.sent(lambda c: Product(c, PRODUCT).submit(task), receipt),
            wire(previous),
        )
        self.assertEqual(
            await self.sent(
                lambda c: Product(c, PRODUCT).read_knowledge("spring-hill")
            ),
            wire({}),
        )
        identity = {
            "practiceId": "00000000-0000-4000-8000-000000000001",
            "officeKey": "spring-hill",
            "sourceCallId": "call",
        }
        handoff = {
            **identity,
            "contact": {
                "phone": "+15555550101",
                "phoneSource": "livekit.sip.callerPhoneNumber",
            },
            "idempotencyKey": hashlib.sha256(
                json.dumps(identity, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        self.assertEqual(
            await self.sent(
                lambda c: Product(c, PRODUCT).admit(
                    "spring-hill", "call", "+15555550101"
                )
            ),
            wire(handoff),
        )

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

    async def test_staff_task_statuses_parse_and_unknown_status_is_ambiguous(self):
        for status in ("created", "duplicate"):
            with self.subTest(status=status):
                recorder = Recorder(
                    {
                        "status": status,
                        "taskId": TASK_ID,
                        "category": "medication",
                        "urgency": "normal",
                    }
                )
                result = await Product(self.client(recorder), PRODUCT).submit(
                    staff_task()
                )
                self.assertIsInstance(result, StaffTaskReceipt)
                self.assertEqual(result.status, status)
        recorder = Recorder(
            *[
                {
                    "status": "queued",
                    "taskId": TASK_ID,
                    "category": "medication",
                    "urgency": "normal",
                }
            ]
            * 2
        )
        result = await Product(self.client(recorder), PRODUCT).submit(staff_task())
        self.assertEqual(result, "ambiguous")
        self.assertEqual(len(recorder.requests), 2)
        self.assertEqual(recorder.requests[0].content, recorder.requests[1].content)

    async def test_handoff_admission_failures_are_typed(self):
        grant = {
            "id": TASK_ID,
            "expiresAt": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
            "sipDestination": "sip:acuity-handoff@product.test",
        }
        for response, expected in (
            (httpx.Response(201, json=grant), HandoffGrant),
            (httpx.Response(400), Refusal("rejected", "http_400")),
            (httpx.Response(409), Refusal("unconfirmed", "http_409")),
            (httpx.Response(503), Refusal("unconfirmed", "http_503")),
            (httpx.ReadTimeout("slow"), Refusal("unconfirmed", "timeout")),
            (httpx.ConnectError("down"), Refusal("unconfirmed", "network")),
            (
                httpx.Response(201, json=grant | {"id": "bad"}),
                Refusal("unconfirmed", "invalid_response"),
            ),
            (
                httpx.Response(201, json=grant | {"expiresAt": "2026-01-01T00:00:00"}),
                Refusal("unconfirmed", "invalid_response"),
            ),
        ):
            with self.subTest(response=response, expected=expected):
                result = await Product(self.client(Recorder(response)), PRODUCT).admit(
                    "spring-hill", "call", "+15555550101"
                )
                if expected is HandoffGrant:
                    self.assertIsInstance(result, HandoffGrant)
                else:
                    self.assertEqual(result, expected)
        self.assertEqual(
            await Product(self.client(Recorder()), Config("offline")).admit(
                "spring-hill", "call", ""
            ),
            Refusal("not_configured", "not_configured"),
        )

    async def test_handoff_admission_bounds_each_phase_not_the_whole_exchange(self):
        grant = json.dumps(
            {
                "id": TASK_ID,
                "expiresAt": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                "sipDestination": "sip:acuity-handoff@product.test",
            }
        ).encode()

        async def trickle(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            await asyncio.sleep(0.3)
            writer.write(
                b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(grant)}\r\n\r\n".encode()
                + grant[:10]
            )
            await writer.drain()
            await asyncio.sleep(0.3)
            writer.write(grant[10:])
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(trickle, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        config = Config(
            "offline",
            handoff=HandoffConfig(
                f"http://127.0.0.1:{port}/v1/handoffs",
                "handoff-secret",
                "00000000-0000-4000-8000-000000000001",
            ),
        )
        client = httpx.AsyncClient()
        self.addAsyncCleanup(client.aclose)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with patch("abita_s2s.integrations.product.HANDOFF_TIMEOUT", 0.5):
            result = await Product(client, config).admit(
                "spring-hill", "call", "+15555550101"
            )
        self.assertGreater(loop.time() - started, 0.5)
        self.assertIsInstance(result, HandoffGrant)
