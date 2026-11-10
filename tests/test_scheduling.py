"""Offline scheduling behavior against the actual HTTP envelopes and registered tools."""

import asyncio
import json
import re
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock, Mock, patch

import httpx
from insurance_fixtures import decision
from livekit.agents import AgentSession, llm
from livekit.agents.llm.tool_context import ToolContext
from livekit.agents.llm.utils import (
    build_legacy_openai_schema,
    build_strict_openai_schema,
)
from owner_fixtures import (
    call_owners,
    coverage_written,
    insurance_write_open,
    insurance_write_uncertain,
    leave,
    registered,
    revoke,
    select,
)
from test_patient_resolution import CONFIG, call_state, candidate, receipt

from abita_s2s.agent import AbitaAgent
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.integrations.patient_middleware import PatientMiddleware
from abita_s2s.integrations.registration_middleware import RegistrationMiddleware
from abita_s2s.integrations.scheduling_http import (
    RescheduleReceipt,
    SchedulingFailure,
    SchedulingHTTP,
    SchedulingOutcome,
    Slot,
    WriteReceipt,
)
from abita_s2s.offices import (
    HOLLYWOOD_ROUTINE_OFFICE_CHOICE,
    HOLLYWOOD_ROUTINE_OFFICE_FOLLOW_UP,
    SPRING_HILL,
    get_office_profile,
)
from abita_s2s.records import Candidate, Receipt
from abita_s2s.runtime.reporting import CallReporter
from abita_s2s.scheduling.answers import (
    BookOutcome,
    CancelOutcome,
    MoveOutcome,
)
from abita_s2s.scheduling.appointments import REFUSALS, Scheduling
from abita_s2s.scheduling.availability import SHARED_OFFICE_CHOICE
from abita_s2s.tools.scheduling import SchedulingTools

NOW = datetime(2026, 9, 14, 17, tzinfo=UTC)
REF = r"[A-Z]{3}[0-9]{2}-[0-9]{4}-[A-Z]+(?:-[0-9]+)?"
REF_0900 = "SEP15-0900-BACH"
PLAN = "test-insurance"
GO_ORIGINAL_CHANGED = "The original appointment changed or no longer exists. Load appointments again before another change."
GO_COULD_NOT_READ_ORIGINAL = "Unable to verify appointment before cancellation. Please load appointments again and choose the appointment to cancel."
GO_BOOK_FAILED = "AdvancedMD did not create the appointment. Please check availability again before retrying. The original appointment was not cancelled."
GO_CANCEL_REFUSALS = (
    (
        "write_failed",
        "Failed to cancel appointment in AdvancedMD. Please try again or contact the office.",
    ),
    (
        "write_failed",
        "AdvancedMD did not cancel the appointment. Please load appointments again before retrying.",
    ),
    (
        "provider_conflict",
        "AdvancedMD could not cancel the appointment because its state changed. Please load appointments again.",
    ),
    (
        "provider_rejected",
        "AdvancedMD rejected the cancellation. Please load appointments again or contact the office.",
    ),
)


def soonest(output):
    return re.search(r"^Soonest options: (.*)$", output, re.MULTILINE)[1].split(", ")


def slot(provider="Dr. Austin Bach", start="2026-09-15T09:00", booked=0, **extra):
    hour, minute = map(int, start[11:].split(":"))
    return {
        "provider": provider,
        "time": f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}",
        "datetime": start,
        "bookingToken": f"token-{provider}-{start}-{extra.get('columnId', 12)}",
        "columnId": 12,
        "profileId": 13,
        "duration": 15,
        **({"sameStartBooked": booked} if booked else {}),
        **extra,
    }


def inventory(**extra):
    return {
        "status": "success",
        "outcome": "availability_found",
        "slots": [
            {
                "provider": "Dr. Austin Bach",
                "time": "9:00 AM",
                "datetime": "2026-09-15T09:00",
                "bookingToken": "private-signed-slot",
                "columnId": 12,
                "profileId": 13,
                "duration": 15,
            }
        ],
        "bookingTokenExpiresAt": "2026-09-14T18:00:00Z",
        **extra,
    }


def appointment(**extra):
    return {
        "id": 77,
        "date": "2026-09-20",
        "time": "10:00 AM",
        "provider": "Dr. Bach",
        "appointmentTypeId": 1007,
        "visitType": "medical",
        "officeId": "spring_hill",
        "office": "Spring Hill",
        "type": "Established Adult Medical",
        "cancellationToken": "private-cancel",
        "rescheduleToken": "private-reschedule",
        **extra,
    }


def booking(appointment_id=888, status="booked"):
    return {
        "status": status,
        "appointmentId": appointment_id,
        "appointmentTypeId": 1007,
        "visitType": "medical",
        "officeId": "spring_hill",
        "office": "Spring Hill",
        "cancellationToken": "new-cancel",
        "rescheduleToken": "new-reschedule",
    }


def rescheduled(status="cancelled", appointment_id=888, old_id=77):
    return {
        "status": "completed" if status == "cancelled" else "partial",
        "booking": booking(appointment_id),
        "cancellation": {"status": "cancelled", "appointmentId": old_id}
        if status == "cancelled"
        else None,
    }


def verified(state, patient_id="chart-jane", **extra):
    select(state, receipt(patient_id, **extra))


def speech_context(state):
    speech = SimpleNamespace(
        wait_for_playout=AsyncMock(), interrupted=False, exception=lambda: None
    )
    return SimpleNamespace(
        userdata=state,
        function_call=SimpleNamespace(call_id="native-call-id"),
        wait_for_playout=AsyncMock(),
        session=SimpleNamespace(generate_reply=Mock(return_value=speech)),
    )


class SlotRecordTests(unittest.TestCase):
    def test_slot_validates_provider_and_start_at_the_boundary(self):
        fields = dict(time="9:00 PM", bookingToken="token")
        offset = Slot(
            provider=" Dr. Austin Bach (Overflow) ",
            datetime="2026-09-16T01:00:00+00:00",
            **fields,
        )
        naive = Slot(provider="Dr. D. Noel", datetime="2026-09-15T21:00", **fields)
        self.assertEqual(offset.provider, "Dr. Bach")
        self.assertEqual(naive.provider, "Dr. Noel")
        self.assertEqual(offset.start, naive.start)
        self.assertEqual(naive.start.tzname(), "EDT")
        self.assertEqual(offset.date, "2026-09-16")
        self.assertEqual(
            offset,
            Slot(
                provider="Dr. Bach",
                datetime="2026-09-16T01:00:00+00:00",
                **fields,
            ),
        )

    def test_receipt_provider_is_normalized_and_may_be_blank(self):
        self.assertEqual(
            WriteReceipt(status="booked", providerName="Dr. J. Licht").providerName,
            "Dr. Licht",
        )
        self.assertEqual(
            WriteReceipt(status="booked", providerName="").providerName, ""
        )
        self.assertIsNone(WriteReceipt(status="booked").providerName)


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    def owner(self, responses, *, state=None):
        state = state or call_state(None)
        state.reporter = Mock()
        if state.chart() is None:
            verified(state)
        requests = []

        async def handler(request):
            requests.append(
                (request.url.path, json.loads(request.content), dict(request.headers))
            )
            value = responses.pop(0)
            if callable(value):
                return await value(request)
            return httpx.Response(200, json=value)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
        self.addAsyncCleanup(resolver.aclose)
        owner = Scheduling(
            state, resolver, SchedulingHTTP(client, CONFIG), now=lambda: NOW
        )
        self.addAsyncCleanup(owner.aclose)
        return owner, requests

    async def tool(self, owner, name, **args):
        if name == "list_available_appointments":
            args.setdefault("insurancePlanId", PLAN)
        output = await getattr(SchedulingTools(owner), name)(
            speech_context(owner.state),
            **args,
        )
        return output

    async def slots(self, owner, **args):
        result = await self.tool(
            owner, "list_available_appointments", visitType="medical", **args
        )
        self.assertTrue(result.startswith("success: "), result)
        return soonest(result)[0]

    async def move_slot(self, owner, **args):
        """A slot from a plan-gated search for the first loaded appointment."""
        return await self.slots(
            owner,
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
            **args,
        )

    async def book(self, owner, ref, referringDoctor=None, **extra):
        return await self.tool(
            owner,
            "book_appointment",
            appointmentSlotRef=ref,
            appointmentReason="Blurry vision in left eye since yesterday",
            referringDoctor=referringDoctor,
            readBack=True,
            **extra,
        )

    async def test_scheduling_requests_omit_retired_fields(self):
        for move in (False, True):
            with self.subTest(move=move):
                owner, requests = self.owner(
                    [inventory(), rescheduled() if move else booking()]
                )
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                registered(owner.state, "chart-jane", "created")
                old_ref = owner.appointments()[0]["appointmentRef"]
                ref = await self.slots(
                    owner, **({"rescheduleAppointmentRef": old_ref} if move else {})
                )
                if move:
                    result = await self.tool(
                        owner,
                        "reschedule_appointment",
                        oldAppointmentRef=old_ref,
                        appointmentSlotRef=ref,
                        appointmentReason="Follow up",
                        readBack=True,
                    )
                else:
                    result = await self.book(owner, ref)
                self.assertTrue(result.startswith("success:"), result)
                self.assertEqual(len(requests), 2)
                for _, body, _ in requests:
                    self.assertTrue(
                        {"insurancePlan", "coverageType", "rangeDays"}.isdisjoint(body),
                        body,
                    )

    async def test_booking_sends_referring_doctor_contact_details(self):
        for extra, expected in (
            (
                dict(
                    referringDoctor="Dr. Lee",
                    referringDoctorPhone=" 352-555-0100 ",
                    referringDoctorAddress="12 Main St",
                ),
                {
                    "referringDoctor": "Dr. Lee",
                    "referringDoctorPhone": "352-555-0100",
                    "referringDoctorAddress": "12 Main St",
                },
            ),
            (
                dict(referringDoctor="Dr. Kim", referringDoctorPhone=""),
                {"referringDoctor": "Dr. Kim"},
            ),
            (dict(referringDoctor=None), {}),
        ):
            with self.subTest(expected=expected):
                owner, requests = self.owner([inventory(), booking()])
                ref = await self.slots(owner)
                result = await self.book(owner, ref, **extra)
                self.assertTrue(result.startswith("success:"), result)
                body = requests[1][1]
                self.assertEqual(
                    {key: body[key] for key in body if key.startswith("referring")},
                    expected,
                )

    def test_booking_requires_the_referring_doctor_and_null_means_none(self):
        owner, _ = self.owner([])
        tool = next(
            tool
            for tool in llm.find_function_tools(SchedulingTools(owner))
            if tool.id == "book_appointment"
        )
        parameters = build_legacy_openai_schema(tool, internally_tagged=True)[
            "parameters"
        ]
        self.assertIn("referringDoctor", parameters["required"])
        doctor = parameters["properties"]["referringDoctor"]
        self.assertIn({"type": "null"}, doctor["anyOf"])
        self.assertNotIn('"none"', doctor["description"])
        self.assertIn("null", doctor["description"])

    async def test_a_move_sends_no_referring_doctor(self):
        owner, requests = self.owner([inventory(), rescheduled()])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        moved = await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
            appointmentSlotRef=await self.move_slot(owner),
            readBack=True,
        )
        self.assertTrue(moved.startswith("success:"), moved)
        self.assertFalse(
            [key for key in requests[-1][1] if key.startswith("referring")]
        )

    async def test_retired_booking_statuses_remain_uncertain(self):
        for status in ("partial", "success"):
            with self.subTest(status=status):
                owner, requests = self.owner([inventory(), booking(status=status)])
                ref = await self.slots(owner)
                result = await self.book(owner, ref)
                self.assertIn("could not be confirmed", result)
                self.assertEqual(await self.book(owner, ref), result)
                self.assertEqual(owner.state.chart().appointments, [])
                self.assertEqual(len(requests), 2)

    async def test_search_sends_the_listed_plan_id_with_the_visit_type(self):
        owner, requests = self.owner([inventory()])
        await self.slots(owner, insurancePlanId=" aetna ")
        self.assertEqual(
            (requests[0][1]["visitType"], requests[0][1]["insurancePlanId"]),
            ("medical", "aetna"),
        )

    async def test_plan_outside_the_visit_types_list_is_rejected_before_search(self):
        for plan, unloaded in (("vsp", False), ("Aetna", False), ("aetna", True)):
            with self.subTest(plan=plan, unloaded=unloaded):
                owner, requests = self.owner([inventory()])
                if unloaded:
                    owner.state.plans = None
                result = await self.tool(
                    owner,
                    "list_available_appointments",
                    visitType="medical",
                    insurancePlanId=plan,
                )
                self.assertTrue(
                    result.startswith("blocked: " if unloaded else "needs_input: "),
                    result,
                )
                self.assertIn(
                    "Insurance list" if not unloaded else "insurance list", result
                )
                self.assertEqual(requests, [])

    async def test_existing_patient_routine_vision_books_from_the_planned_search(self):
        state = call_state(None, get_office_profile("north-miami-beach-optical"))
        verified(state)
        owner, requests = self.owner([inventory(), booking()], state=state)
        result = await self.tool(
            owner, "list_available_appointments", visitType="routine_vision"
        )
        self.assertTrue(result.startswith("success:"), result)
        ref = soonest(result)[0]
        self.assertTrue((await self.book(owner, ref)).startswith("success:"))
        self.assertEqual(
            [path for path, _, _ in requests],
            [
                "/api/scheduler/slots",
                "/api/appointment/book",
            ],
        )
        for _, payload, _ in requests:
            self.assertEqual(payload["patientId"], "chart-jane")
            self.assertNotIn("insurancePlan", payload)
            self.assertNotIn("routing", payload)
        self.assertEqual(requests[0][1]["visitType"], "routine_vision")
        self.assertEqual(requests[1][1]["visitCategory"], "routine_vision")

    async def test_scheduling_results_name_the_patient_they_act_for(self):
        owner, _ = self.owner([inventory(), booking()])
        search = await self.tool(
            owner, "list_available_appointments", visitType="medical"
        )
        self.assertIn("through 2026-09-28 for Doe, Jane.", search)
        ref = soonest(search)[0]
        confirm = await self.tool(
            owner,
            "book_appointment",
            appointmentSlotRef=ref,
            appointmentReason="Follow up",
            referringDoctor=None,
            readBack=None,
        )
        self.assertIn(
            "Confirm 2026-09-15 at 9:00 AM Eastern with Dr. Bach for Doe, Jane before booking.",
            confirm,
        )
        booked = await self.book(owner, ref)
        self.assertTrue(
            booked.startswith(
                "success: Booked 2026-09-15 at 9:00 AM Eastern with Dr. Bach for Doe, Jane."
            ),
            booked,
        )

    async def test_insurance_write_guards_invalidate_offered_slots(self):
        for guard in ("pending", "uncertain", "partial"):
            with self.subTest(guard=guard):
                owner, requests = self.owner([inventory()])
                ref = await self.slots(owner)
                if guard == "partial":
                    registered(owner.state, "chart-jane", "partial")
                elif guard == "pending":
                    insurance_write_open(owner.state)
                else:
                    insurance_write_uncertain(owner.state)
                self.assertFalse((await self.book(owner, ref)).startswith("success:"))
                self.assertEqual(len(requests), 1)

    async def test_availability_contract_cache_private_tokens_and_office(self):
        state = call_state(None, get_office_profile("sweetwater"))
        owner, requests = self.owner([inventory()], state=state)
        self.assertEqual(
            (
                await self.tool(
                    owner, "list_available_appointments", visitType="medical"
                )
            ),
            "needs_input: Ask for Hollywood or Sweetwater on those office calls; omit office for other calls.",
        )
        ref = await self.slots(owner, office="hollywood")
        self.assertEqual(await self.slots(owner, office="hollywood"), ref)
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0][1],
            {
                "office": "hollywood",
                "patientId": "chart-jane",
                "startDate": "2026-09-15",
                "visitType": "medical",
                "insurancePlanId": PLAN,
                "dob": "01/02/1980",
            },
        )
        self.assertEqual(requests[0][2]["authorization"], "test-auth")
        cached = await owner.availability("medical", PLAN, office="hollywood")
        self.assertEqual(len(requests), 1)
        self.assertNotIn(
            "private-signed-slot", json.dumps(cached, default=datetime.isoformat)
        )

    async def test_hollywood_routine_vision_announces_north_miami_beach_once(self):
        state = call_state(None, get_office_profile("hollywood"))
        verified(state)
        owner, requests = self.owner([inventory(), inventory()], state=state)

        async def routine(**args):
            return await self.tool(
                owner,
                "list_available_appointments",
                visitType="routine_vision",
                **args,
            )

        self.assertEqual(
            await routine(office="hollywood"), HOLLYWOOD_ROUTINE_OFFICE_CHOICE
        )
        self.assertEqual(await routine(), HOLLYWOOD_ROUTINE_OFFICE_FOLLOW_UP)
        self.assertTrue((await routine(office="hollywood")).startswith("success: "))
        self.assertTrue(
            (await routine(office="north-miami-beach-optical")).startswith("success: ")
        )
        self.assertEqual(
            [request[1]["office"] for request in requests],
            ["hollywood", "north-miami-beach-optical"],
        )

    async def test_hollywood_caller_choosing_sweetwater_skips_announcement(self):
        state = call_state(None, get_office_profile("hollywood"))
        verified(state)
        owner, requests = self.owner([inventory()], state=state)
        result = await self.tool(
            owner,
            "list_available_appointments",
            visitType="routine_vision",
            office="sweetwater",
        )
        self.assertTrue(result.startswith("success: "), result)
        self.assertEqual(requests[0][1]["office"], "sweetwater")

    async def test_north_miami_beach_is_only_offered_for_hollywood_routine_vision(self):
        for called, visit in (
            ("hollywood", "medical"),
            ("sweetwater", "routine_vision"),
        ):
            with self.subTest(called=called, visit=visit):
                state = call_state(None, get_office_profile(called))
                owner, requests = self.owner([], state=state)
                result = await self.tool(
                    owner,
                    "list_available_appointments",
                    visitType=visit,
                    office="north-miami-beach-optical",
                )
                self.assertEqual(result, SHARED_OFFICE_CHOICE)
                self.assertEqual(requests, [])

    async def test_plain_text_preserves_empty_search_and_retry_boundaries(self):
        owner, requests = self.owner(
            [
                inventory(outcome="no_availability", slots=[]),
                inventory(
                    outcome="availability_search_incomplete",
                    slots=[],
                    shouldRetrySameSearch=True,
                ),
                inventory(
                    outcome="availability_search_incomplete",
                    slots=[],
                    shouldRetrySameSearch=True,
                ),
            ]
        )
        empty = await self.tool(
            owner, "list_available_appointments", visitType="medical"
        )
        self.assertTrue(empty.startswith("no_results: "), empty)
        self.assertIn("Searched 2026-09-15 through 2026-09-28", empty)
        for expected in (
            "Retry this search once.",
            "Do not retry this search; ask staff for help.",
        ):
            failed = await self.tool(
                owner,
                "list_available_appointments",
                visitType="medical",
                startDate="2026-09-16",
            )
            self.assertTrue(failed.startswith("blocked: "), failed)
            self.assertIn("this does not mean no openings", failed)
            self.assertIn(expected, failed)
            self.assertNotIn("Available appointments", failed)
        stopped = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            startDate="2026-09-16",
        )
        self.assertIn("do not", stopped.lower())
        self.assertEqual(len(requests), 3)

    async def test_malformed_availability_is_blocked_and_retry_is_bounded(self):
        base = inventory()["slots"][0]
        malformed = [
            inventory(bookingTokenExpiresAt="invalid"),
            inventory(bookingTokenExpiresAt="2026-09-14T18:00:00"),
            inventory(bookingTokenExpiresAt=None),
            inventory(slots=[]),
            inventory(outcome="no_availability"),
            inventory(status="error"),
            inventory(searchedFrom="2026-09-15bogus"),
            inventory(searchedThrough="2026-09-29"),
            inventory(slots=[{**base, "bookingToken": " "}]),
        ] + [
            inventory(slots=[{**base, "datetime": value}])
            for value in (
                "2026-09-16bogus",
                "2026-09-16",
                "2026-09-16T99:00",
                "2026-09-31T09:00",
                "2026-09-29T09:00",
            )
        ]
        for response in malformed:
            with self.subTest(response=response):
                owner, requests = self.owner([response, response])
                for expected in (
                    "Retry this search once.",
                    "Do not retry this search;",
                ):
                    output = await self.tool(
                        owner, "list_available_appointments", visitType="medical"
                    )
                    self.assertTrue(output.startswith("blocked:"), output)
                    self.assertIn(expected, output)
                    self.assertNotRegex(output, REF)
                stopped = await self.tool(
                    owner, "list_available_appointments", visitType="medical"
                )
                self.assertTrue(stopped.startswith("blocked:"), stopped)
                self.assertEqual(len(requests), 2)
                self.assertEqual(
                    (await self.book(owner, "S1")).split(":", 1)[0], "needs_input"
                )
                self.assertEqual(len(requests), 2)

    async def test_readable_reference_collision_preserves_both_slots(self):
        owner, requests = self.owner(
            [
                inventory(
                    slots=[slot(), slot("Dr. Miriam Bach", columnId=40, profileId=41)]
                ),
                booking(),
            ]
        )
        output = await self.tool(
            owner, "list_available_appointments", visitType="medical"
        )
        self.assertIn(
            "  Dr. Bach morning: open 9:00 AM SEP15-0900-BACH\n"
            "  Dr. Miriam Bach morning: open 9:00 AM SEP15-0900-BACH-2",
            output,
        )
        result = await self.book(owner, "sep15-0900-bach-2")
        self.assertTrue(result.startswith("success:"), result)
        self.assertEqual(
            requests[-1][1]["bookingToken"], "token-Dr. Miriam Bach-2026-09-15T09:00-40"
        )

    async def test_relative_dates_use_eastern_calendar_and_slot_timezone(self):
        for now, slot_datetime, label in (
            (
                datetime(2026, 9, 15, 1, tzinfo=UTC),
                "2026-09-15T09:00",
                "Tuesday, September 15, 2026 (tomorrow), EDT",
            ),
            (
                datetime(2026, 10, 31, 17, tzinfo=UTC),
                "2026-11-01T09:00",
                "Sunday, November 1, 2026 (tomorrow), EST",
            ),
            (
                datetime(2026, 9, 14, 17, tzinfo=UTC),
                "2026-09-27T09:00",
                "Sunday, September 27, 2026 (in 13 days), EDT",
            ),
        ):
            with self.subTest(slot_datetime=slot_datetime):
                base = inventory()["slots"][0]
                owner, _ = self.owner(
                    [
                        inventory(
                            slots=[{**base, "datetime": slot_datetime}],
                            bookingTokenExpiresAt=(
                                now + timedelta(hours=1)
                            ).isoformat(),
                        )
                    ]
                )
                owner.now = lambda: now
                output = await self.tool(
                    owner, "list_available_appointments", visitType="medical"
                )
                self.assertIn(label, output)

    async def test_same_time_adjacent_dates_book_the_selected_complete_line(self):
        base = inventory()["slots"][0]
        owner, requests = self.owner(
            [
                inventory(
                    slots=[
                        {
                            **base,
                            "provider": "Dr. Melissa Otero",
                            "datetime": "2026-10-13T11:00",
                            "time": "11:00 AM",
                            "bookingToken": "oct13-private",
                        },
                        {
                            **base,
                            "provider": "Dr. Melissa Otero",
                            "datetime": "2026-10-14T11:00",
                            "time": "11:00 AM",
                            "bookingToken": "oct14-private",
                        },
                    ]
                ),
                booking(),
            ]
        )
        output = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            startDate="2026-10-06",
        )
        self.assertIn(
            "Tuesday, October 13, 2026 (in 29 days), EDT\n"
            "  Dr. Melissa Otero morning: open 11:00 AM OCT13-1100-OTERO\n\n"
            "Wednesday, October 14, 2026 (in 30 days), EDT\n"
            "  Dr. Melissa Otero morning: open 11:00 AM OCT14-1100-OTERO",
            output,
        )
        result = await self.book(owner, "oct13-1100-otero")
        self.assertTrue(result.startswith("success:"), result)
        self.assertEqual(requests[-1][1]["bookingToken"], "oct13-private")
        self.assertIn("2026-10-13", result)

    async def test_availability_has_complete_slot_lines_without_existing_appointments(
        self,
    ):
        base = inventory()["slots"][0]
        owner, _ = self.owner(
            [
                inventory(
                    slots=[
                        {**base, "datetime": "2026-09-15T11:30", "time": "11:30 AM"},
                        {**base, "datetime": "2026-09-15T11:45", "time": "11:45 AM"},
                        {**base, "datetime": "2026-09-16T09:00", "time": "9:00 AM"},
                    ]
                )
            ]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        output = await self.tool(
            owner, "list_available_appointments", visitType="medical"
        )
        self.assertEqual(
            output,
            "success: Found eligible openings.\n"
            "Searched 2026-09-15 through 2026-09-28 for Doe, Jane.\n"
            "Soonest options: SEP15-1130-BACH, SEP15-1145-BACH\n\n"
            "Tuesday, September 15, 2026 (tomorrow), EDT\n"
            "  Dr. Bach morning: open 11:30 AM SEP15-1130-BACH, 11:45 AM SEP15-1145-BACH\n\n"
            "Wednesday, September 16, 2026 (in 2 days), EDT\n"
            "  Dr. Bach morning: open 9:00 AM SEP16-0900-BACH",
        )
        for private in (
            "private-signed-slot",
            "private-cancel",
            "private-reschedule",
            "chart-jane",
        ):
            self.assertNotIn(private, output)
        for ref, time in (
            ("SEP15-1130-BACH", "11:30 AM"),
            ("SEP15-1145-BACH", "11:45 AM"),
        ):
            unconfirmed = await self.tool(
                owner,
                "book_appointment",
                appointmentSlotRef=ref,
                appointmentReason="Follow up",
                referringDoctor=None,
                readBack=None,
            )
            self.assertTrue(
                unconfirmed.startswith(
                    f"needs_input: Confirm 2026-09-15 at {time} Eastern"
                ),
                unconfirmed,
            )
        self.assertTrue(
            owner.appointments_text().startswith("\nExisting appointments:")
        )

    async def test_resolution_describes_reconciled_appointments(self):
        owner, _ = self.owner(
            [inventory(), booking(77), {"status": "cancelled", "appointmentId": 77}]
        )
        resolver = PatientResolver(owner.state, AsyncMock())
        self.addAsyncCleanup(resolver.aclose)
        agent = AbitaAgent(
            call_owners(SPRING_HILL, resolver=resolver, scheduling=owner)
        )
        resolve_patient = ToolContext(agent.tools).function_tools["resolve_patient"]
        context = SimpleNamespace(
            userdata=owner.state, function_call=SimpleNamespace(call_id="resolution")
        )
        await self.book(owner, await self.slots(owner))
        verified(owner.state, appointmentsStatus="none", appointments=[])
        output = await resolve_patient(context, "Jane", None)
        self.assertIn("Existing appointments", output)
        self.assertIn("2026-09-15 at 9:00 AM", output)
        self.assertNotIn("No upcoming appointments", output)

        cancelled = await self.tool(
            owner,
            "cancel_appointment",
            appointmentRef=owner.appointments()[0]["appointmentRef"],
            readBack=True,
        )
        self.assertTrue(cancelled.startswith("success:"), cancelled)
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        output = await resolve_patient(context, "Jane", None)
        self.assertIn("No upcoming appointments.", output)
        self.assertNotIn("Existing appointments", output)

    async def test_insurance_update_keeps_appointment_refs(self):
        owner, requests = self.owner([{"status": "cancelled", "appointmentId": 77}])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = owner.appointments()[0]["appointmentRef"]
        coverage_written(owner.state, "Aetna", "car1")
        self.assertEqual(owner.appointments()[0]["appointmentRef"], ref)
        cancelled = await self.tool(
            owner, "cancel_appointment", appointmentRef=ref, readBack=True
        )
        self.assertTrue(cancelled.startswith("success:"), cancelled)
        self.assertEqual(len(requests), 1)

    async def test_missing_prerequisites_and_office_care(self):
        for change in ("partial", "uncertain", "pending", "patient"):
            owner, requests = self.owner([])
            if change == "partial":
                registered(owner.state, "chart-jane", "partial")
            elif change == "uncertain":
                insurance_write_uncertain(owner.state)
            elif change == "pending":
                insurance_write_open(owner.state)
            else:
                leave(owner.state)
            self.assertEqual(
                (await owner.availability("medical", PLAN))["outcome"], "needs_input"
            )
        self.assertEqual(requests, [])
        for key, visit in (
            ("crystal-river", "routine_vision"),
            ("north-miami-beach-optical", "medical"),
        ):
            state = call_state(None, get_office_profile(key))
            verified(state)
            owner, requests = self.owner(
                [inventory(outcome="no_eligible_providers", slots=[])], state=state
            )
            result = await self.tool(
                owner, "list_available_appointments", visitType=visit
            )
            self.assertTrue(
                result.startswith("blocked: No providers are eligible"), result
            )
            self.assertIn(
                "Confirm the office and visit type or ask staff for help", result
            )
            self.assertNotIn("Searched", result)
            self.assertNotIn("other dates", result)
            self.assertEqual(
                (await owner.availability(visit, PLAN))["outcome"], "unsupported"
            )
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][1]["visitType"], visit)

    async def test_a_null_plan_move_whose_doctor_left_never_asks_about_the_office(self):
        owner, requests = self.owner(
            [inventory(outcome="no_eligible_providers", slots=[])]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        result = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
        )
        self.assertEqual(
            result,
            "blocked: Dr. Bach is no longer eligible for this visit. To see another"
            " doctor, confirm the plan first, or offer a staff request; changing dates"
            " will not resolve this restriction.",
        )
        self.assertEqual(len(requests), 1)

    async def test_a_plan_gated_move_with_no_eligible_doctor_still_names_the_office(
        self,
    ):
        owner, _ = self.owner([inventory(outcome="no_eligible_providers", slots=[])])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        result = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
        )
        self.assertIn("Confirm the office and visit type or ask staff for help", result)

    async def test_calendar_marks_shared_starts_and_offers_open_slots_first(self):
        owner, requests = self.owner(
            [
                inventory(
                    slots=[
                        slot(booked=1),
                        slot(columnId=14),
                        slot(start="2026-09-15T09:15", booked=1),
                        slot("Dr. D. Noel", "2026-09-15T13:00", columnId=20),
                        slot("Dr. D. Noel", "2026-09-15T13:30", booked=1, columnId=20),
                        slot(start="2026-09-16T09:00"),
                    ]
                ),
                booking(),
            ]
        )
        output = await self.tool(
            owner, "list_available_appointments", visitType="medical"
        )
        self.assertEqual(
            output,
            "success: Found eligible openings.\n"
            "Searched 2026-09-15 through 2026-09-28 for Doe, Jane.\n"
            "Soonest options: SEP15-0900-BACH, SEP15-1300-NOEL\n\n"
            "Tuesday, September 15, 2026 (tomorrow), EDT\n"
            "  Dr. Bach morning: open 9:00 AM SEP15-0900-BACH; shared 9:15 AM SEP15-0915-BACH\n"
            "  Dr. Noel afternoon: open 1:00 PM SEP15-1300-NOEL; shared 1:30 PM SEP15-1330-NOEL\n\n"
            "Wednesday, September 16, 2026 (in 2 days), EDT\n"
            "  Dr. Bach morning: open 9:00 AM SEP16-0900-BACH",
        )
        self.assertTrue(
            (await self.book(owner, "SEP15-0900-BACH")).startswith("success:")
        )
        self.assertEqual(
            requests[-1][1]["bookingToken"], "token-Dr. Austin Bach-2026-09-15T09:00-14"
        )

    async def test_soonest_options_prefer_open_slots_on_the_earliest_day(self):
        for name, slots, expected in (
            (
                "open afternoon beats shared morning",
                [
                    slot(booked=1),
                    slot(start="2026-09-15T09:15", booked=1),
                    slot(start="2026-09-15T14:00"),
                    slot(start="2026-09-15T14:15"),
                ],
                ["SEP15-1400-BACH", "SEP15-1415-BACH"],
            ),
            (
                "one open slot on the earliest day",
                [
                    slot(),
                    slot(start="2026-09-15T09:15", booked=1),
                    slot(start="2026-09-16T09:00"),
                ],
                ["SEP15-0900-BACH"],
            ),
            (
                "earliest day only shared",
                [
                    slot(booked=1),
                    slot(start="2026-09-15T13:00", booked=1),
                    slot(start="2026-09-16T10:00", booked=1),
                    slot(start="2026-09-17T10:15"),
                ],
                ["SEP15-0900-BACH", "SEP17-1015-BACH"],
            ),
            (
                "nothing open",
                [slot(booked=1), slot(start="2026-09-15T09:15", booked=1)],
                ["SEP15-0900-BACH"],
            ),
            (
                "days follow Eastern time",
                [
                    slot(start="2026-09-15T14:00", booked=1),
                    {**slot(), "datetime": "2026-09-16T01:00:00+00:00"},
                ],
                ["SEP15-2100-BACH"],
            ),
        ):
            with self.subTest(name):
                owner, _ = self.owner([inventory(slots=slots)])
                output = await self.tool(
                    owner, "list_available_appointments", visitType="medical"
                )
                self.assertEqual(soonest(output), expected)

    async def test_rebooking_a_ref_on_another_column_replays_the_booking(self):
        owner, requests = self.owner(
            [
                inventory(slots=[slot(), slot(booked=1, columnId=14)]),
                booking(),
                inventory(slots=[slot(booked=1), slot(columnId=14)]),
            ]
        )
        original = await self.book(owner, await self.slots(owner))
        self.assertTrue(original.startswith("success:"), original)
        listing = await self.tool(
            owner, "list_available_appointments", visitType="medical"
        )
        ref = soonest(listing)[0]
        self.assertIn(f"open 9:00 AM {ref}", listing)
        self.assertEqual(await self.book(owner, ref), original)
        self.assertEqual(
            [path for path, _, _ in requests].count("/api/appointment/book"), 1
        )

    async def test_eastern_date_and_correction_invalidates_slots(self):
        owner, requests = self.owner(
            [inventory(bookingTokenExpiresAt="2026-09-15T02:00:00Z")]
        )
        owner.now = lambda: datetime(2026, 9, 15, 1, tzinfo=UTC)
        ref = await self.slots(owner)
        for start in ("2026-09-14", "20260916", "bad"):
            self.assertEqual(
                (await owner.availability("medical", PLAN, start))["outcome"],
                "needs_input",
            )
        self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], "needs_input")
        self.assertEqual(len(requests), 1)

    async def test_shared_read_cancelled_waiter_and_patient_switch(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=inventory())

        owner, requests = self.owner([delayed])
        first = asyncio.create_task(owner.availability("medical", PLAN))
        await entered.wait()
        second = asyncio.create_task(owner.availability("medical", PLAN))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        verified(owner.state, "chart-john")
        release.set()
        self.assertEqual((await second)["outcome"], "stale")
        self.assertIn(
            "slot reference was not returned", await self.book(owner, REF_0900)
        )
        self.assertEqual(len(requests), 1)

    async def test_corrected_search_does_not_reuse_invalidated_pending_read(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=inventory())

        owner, requests = self.owner([delayed, inventory()])
        first = asyncio.create_task(owner.availability("medical", PLAN))
        await entered.wait()
        self.assertEqual(
            (await owner.availability("medical", PLAN, "bad"))["outcome"], "needs_input"
        )
        corrected = asyncio.create_task(owner.availability("medical", PLAN))
        await asyncio.sleep(0)
        release.set()
        self.assertEqual((await first)["outcome"], "stale")
        found = await corrected
        self.assertEqual(found["outcome"], "found")
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(found["slots"]), 1)

    async def test_last_cancelled_waiter_stops_read_and_allows_fresh_search(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        owner, requests = self.owner([delayed, inventory()])
        task = asyncio.create_task(owner.availability("medical", PLAN))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await cancelled.wait()
        self.assertIn(
            "slot reference was not returned", await self.book(owner, REF_0900)
        )
        await self.slots(owner)
        self.assertEqual(len(requests), 2)

    async def test_later_search_wins_and_partial_inventory_not_offered(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=inventory())

        incomplete = inventory(
            outcome="availability_search_incomplete",
            status="error",
            shouldRetrySameSearch=True,
        )
        owner, requests = self.owner([delayed, incomplete, incomplete])
        first = asyncio.create_task(owner.availability("medical", PLAN))
        await entered.wait()
        result = await owner.availability("medical", PLAN, "2026-09-16")
        self.assertTrue(result["retry_same_search"])
        release.set()
        self.assertEqual((await first)["outcome"], "stale")
        self.assertFalse(
            (await owner.availability("medical", PLAN, "2026-09-16"))[
                "retry_same_search"
            ]
        )
        self.assertEqual(
            (await owner.availability("medical", PLAN, "2026-09-16"))["outcome"],
            "availability_failed",
        )
        self.assertIn(
            "slot reference was not returned", await self.book(owner, REF_0900)
        )
        self.assertEqual(len(requests), 3)

    async def test_booking_confirmation_receipt_duplicate_and_new_cancellation(self):
        owner, requests = self.owner(
            [
                inventory(),
                booking(888),
                {"status": "cancelled", "appointmentId": 888},
            ]
        )
        ref = await self.slots(owner)
        for reason, referrer, confirm in (
            ("", None, True),
            ("Routine annual exam", None, None),
        ):
            result = await self.tool(
                owner,
                "book_appointment",
                appointmentSlotRef=ref,
                appointmentReason=reason,
                referringDoctor=referrer,
                readBack=confirm,
            )
            self.assertEqual(result.split(":", 1)[0], "needs_input")
        result = await self.book(owner, ref)
        self.assertEqual(result.split(":", 1)[0], "success")
        self.assertEqual(await self.book(owner, ref), result)
        owner.state.reporter.appointment.assert_called_once()
        evidence = owner.state.reporter.appointment.call_args.args[0]
        self.assertEqual(evidence["bookingResult"]["status"], "booked")
        self.assertEqual(evidence["newAppointmentId"], "888")
        self.assertEqual(evidence["bookingResult"]["appointmentDate"], "2026-09-15")
        self.assertEqual(
            evidence["bookingResult"]["appointmentTime"],
            inventory()["slots"][0]["time"],
        )
        self.assertEqual(evidence["bookingResult"]["providerName"], "Dr. Bach")
        self.assertNotIn("private-signed-slot", json.dumps(evidence))
        body = requests[1][1]
        self.assertEqual(body["bookingToken"], "private-signed-slot")
        self.assertEqual(body["patientId"], "chart-jane")
        self.assertNotIn("office", body)
        self.assertNotIn("patientStatus", body)
        cancel_ref = owner.appointments()[0]["appointmentRef"]
        self.assertEqual(
            (
                await self.tool(
                    owner,
                    "cancel_appointment",
                    appointmentRef=cancel_ref,
                    readBack=True,
                )
            ).split(":", 1)[0],
            "success",
        )
        self.assertEqual(
            (
                await self.tool(
                    owner,
                    "cancel_appointment",
                    appointmentRef=cancel_ref,
                    readBack=True,
                )
            ).split(":", 1)[0],
            "success",
        )
        self.assertEqual(
            requests[-1][1],
            {"patientId": "chart-jane", "cancellationToken": "new-cancel"},
        )
        self.assertEqual(owner.state.chart().appointments, [])
        self.assertEqual(len(requests), 3)

    async def test_cancelled_booking_cannot_satisfy_a_new_booking(self):
        owner, requests = self.owner(
            [
                inventory(),
                booking(),
                {"status": "cancelled", "appointmentId": 888},
                inventory(),
                {"status": "booked", "appointmentId": 999},
            ]
        )
        first = await self.slots(owner)
        await self.book(owner, first)
        await self.tool(
            owner,
            "cancel_appointment",
            appointmentRef=owner.appointments()[0]["appointmentRef"],
            readBack=True,
        )
        replay = await self.book(owner, first)
        self.assertTrue(replay.startswith("blocked:"), replay)
        self.assertIn("cancelled", replay)
        second = await self.slots(owner)
        self.assertEqual(first, second)
        result = await self.book(owner, second)
        self.assertTrue(result.startswith("success:"), result)
        self.assertEqual([a.id for a in owner.state.chart().appointments], [999])
        self.assertEqual(
            [path for path, _, _ in requests],
            [
                "/api/scheduler/slots",
                "/api/appointment/book",
                "/api/appointment/cancel",
                "/api/scheduler/slots",
                "/api/appointment/book",
            ],
        )

    async def test_booking_replay_is_scoped_to_the_selected_slot(self):
        later = inventory()
        later["slots"][0].update(
            datetime="2026-09-16T09:00", bookingToken="second-slot"
        )
        owner, requests = self.owner(
            [
                inventory(),
                {"status": "booked", "appointmentId": 888},
                inventory(),
                later,
                {"status": "booked", "appointmentId": 999},
            ]
        )
        first = await self.slots(owner)
        original = await self.book(owner, first)
        self.assertEqual(await self.book(owner, first), original)
        same_slot = await self.slots(owner)
        self.assertEqual(await self.book(owner, same_slot), original)
        second = await self.slots(owner, startDate="2026-09-16")
        result = await self.book(owner, second)
        self.assertIn("Booked 2026-09-16", result)
        self.assertEqual([a.id for a in owner.state.chart().appointments], [888, 999])
        self.assertEqual(
            [
                body["bookingToken"]
                for path, body, _ in requests
                if path == "/api/appointment/book"
            ],
            ["private-signed-slot", "second-slot"],
        )

    async def test_booking_rechecks_admission_when_write_task_starts(self):
        for change in ("patient", "shutdown"):
            with self.subTest(change=change):
                owner, requests = self.owner([inventory()])
                ref = await self.slots(owner)
                callback = (
                    (lambda: verified(owner.state, "chart-john"))
                    if change == "patient"
                    else owner.close_admission
                )
                asyncio.get_running_loop().call_soon(callback)
                result = await self.book(owner, ref)
                self.assertTrue(result.startswith("blocked:"), result)
                self.assertEqual(len(requests), 1)

    async def test_existing_patient_backend_rejection_is_not_bypassed(self):
        async def rejected(_):
            return httpx.Response(
                400, json={"error": "Staff must verify chart insurance"}
            )

        for phase in ("search", "book"):
            with self.subTest(phase=phase):
                owner, requests = self.owner(
                    [rejected] if phase == "search" else [inventory(), rejected]
                )
                if phase == "search":
                    result = await self.tool(
                        owner, "list_available_appointments", visitType="medical"
                    )
                else:
                    ref = await self.slots(owner)
                    result = await self.book(owner, ref)
                self.assertTrue(result.startswith("blocked:"), result)
                self.assertEqual(len(requests), 1 if phase == "search" else 2)
                self.assertNotIn("insurancePlan", requests[-1][1])

    async def test_chained_moves_preserve_receipts_and_reject_obsolete_booking_replay(
        self,
    ):
        later = inventory()
        later["slots"][0].update(
            datetime="2026-09-16T09:00", bookingToken="second-slot"
        )
        last = inventory()
        last["slots"][0].update(datetime="2026-09-17T09:00", bookingToken="third-slot")
        owner, requests = self.owner(
            [
                inventory(),
                booking(888, "booked"),
                later,
                rescheduled(appointment_id=999, old_id=888),
                last,
                rescheduled(appointment_id=1000, old_id=999),
                {"status": "cancelled", "appointmentId": 1000},
                inventory(),
                {"status": "booked", "appointmentId": 1001},
            ]
        )
        initial = await self.slots(owner)
        await self.book(owner, initial)
        moves = []
        for day, expected_id in (("2026-09-16", 999), ("2026-09-17", 1000)):
            old_ref = owner.appointments()[0]["appointmentRef"]
            slot_ref = await self.slots(
                owner, startDate=day, rescheduleAppointmentRef=old_ref
            )
            args = dict(
                oldAppointmentRef=old_ref,
                appointmentSlotRef=slot_ref,
                appointmentReason="Annual follow up",
                readBack=True,
            )
            result = await self.tool(owner, "reschedule_appointment", **args)
            self.assertTrue(result.startswith("success:"), result)
            self.assertEqual(
                [a.id for a in owner.state.chart().appointments], [expected_id]
            )
            self.assertEqual(
                await self.tool(owner, "reschedule_appointment", **args), result
            )
            moves.append(args)
        self.assertTrue((await self.book(owner, initial)).startswith("blocked:"))
        self.assertTrue(
            (await self.tool(owner, "reschedule_appointment", **moves[0])).startswith(
                "blocked:"
            )
        )
        await self.tool(
            owner,
            "cancel_appointment",
            appointmentRef=owner.appointments()[0]["appointmentRef"],
            readBack=True,
        )
        await self.book(owner, await self.slots(owner))
        verified(
            owner.state, appointmentsStatus="found", appointments=[appointment(id=888)]
        )
        owner.appointments()
        self.assertEqual([a.id for a in owner.state.chart().appointments], [1001])
        self.assertEqual(len(requests), 9)

    async def test_write_continues_after_caller_cancel_and_records_old_patient(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"status": "booked", "appointmentId": 888})

        owner, requests = self.owner([inventory(), delayed])
        ref = await self.slots(owner)
        task = asyncio.create_task(self.book(owner, ref))
        await entered.wait()
        self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], "blocked")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        verified(owner.state, "chart-john")
        release.set()
        await asyncio.shield(owner._writes._last_sent)
        self.assertFalse(owner.state.chart().appointments)
        verified(owner.state)
        self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], "success")
        self.assertEqual(len(requests), 2)

    async def test_abandoned_write_updates_appointments_before_close_returns(self):
        for action in ("book", "cancel"):
            with self.subTest(action=action):
                entered, release = asyncio.Event(), asyncio.Event()

                async def delayed(request):
                    entered.set()
                    await release.wait()
                    result = (
                        {"status": "booked", "appointmentId": 888}
                        if action == "book"
                        else {"status": "cancelled", "appointmentId": 77}
                    )
                    return httpx.Response(200, json=result)

                owner, requests = self.owner(
                    [inventory(), delayed] if action == "book" else [delayed]
                )
                if action == "book":
                    ref = await self.slots(owner)
                    task = asyncio.create_task(self.book(owner, ref))
                else:
                    verified(
                        owner.state,
                        appointmentsStatus="found",
                        appointments=[appointment()],
                    )
                    ref = owner.appointments()[0]["appointmentRef"]
                    task = asyncio.create_task(
                        self.tool(
                            owner,
                            "cancel_appointment",
                            appointmentRef=ref,
                            readBack=True,
                        )
                    )
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release.set()
                await owner.aclose()
                patient = owner.state.chart()
                self.assertEqual(
                    [a.id for a in patient.appointments],
                    [888] if action == "book" else [],
                )
                self.assertEqual(
                    patient.appointmentsStatus, "found" if action == "book" else "none"
                )
                self.assertEqual(len(requests), 2 if action == "book" else 1)

    async def test_reschedule_reconciles_during_write_and_after_reload(
        self,
    ):
        for cancellation_status in ("cancelled", "error"):
            with self.subTest(cancellation_status=cancellation_status):
                owner, requests = self.owner(
                    [
                        inventory(),
                        rescheduled(cancellation_status),
                    ]
                )
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                old_ref = owner.appointments()[0]["appointmentRef"]
                slot_ref = await self.slots(owner, rescheduleAppointmentRef=old_ref)
                args = dict(
                    oldAppointmentRef=old_ref,
                    appointmentSlotRef=slot_ref,
                    appointmentReason="Annual medical follow up",
                    readBack=True,
                )
                result = await self.tool(owner, "reschedule_appointment", **args)
                expected_prefix = (
                    "success:" if cancellation_status == "cancelled" else "blocked:"
                )
                self.assertTrue(result.startswith(expected_prefix), result)
                expected_ids = (
                    [888] if cancellation_status == "cancelled" else [77, 888]
                )
                self.assertEqual(
                    [a.id for a in owner.state.chart().appointments],
                    expected_ids,
                )
                checkpoints = [
                    c.args[0] for c in owner.state.reporter.appointment.call_args_list
                ]
                self.assertEqual(
                    [c["bookingResult"]["status"] for c in checkpoints], ["booked"]
                )
                self.assertEqual(
                    [c["cancellationResult"]["status"] for c in checkpoints],
                    [
                        "cancelled"
                        if cancellation_status == "cancelled"
                        else "uncertain"
                    ],
                )
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                for _ in range(2):
                    owner.appointments()
                    self.assertEqual(
                        [a.id for a in owner.state.chart().appointments],
                        expected_ids,
                    )
                replay = await self.tool(owner, "reschedule_appointment", **args)
                self.assertEqual(replay, result)
                self.assertEqual(len(requests), 2)

    async def test_availability_input_errors_do_not_retry_same_search(self):
        for status_code in (200, 400):
            with self.subTest(status_code=status_code):

                async def rejected(request):
                    return httpx.Response(
                        status_code,
                        json={
                            "status": "error",
                            "outcome": "invalid_input",
                            "slots": [],
                            "message": "Confirm the requested coverage type.",
                            "shouldRetrySameSearch": False,
                        },
                    )

                owner, requests = self.owner([rejected, rejected, inventory()])
                result = await owner.availability("medical", PLAN)
                self.assertEqual(
                    result["outcome"],
                    "needs_input",
                )
                self.assertIn("Confirm the requested coverage type", result["answer"])
                self.assertFalse(result["retry_same_search"])
                self.assertEqual(await owner.availability("medical", PLAN), result)
                owner.now = lambda: NOW + timedelta(seconds=61)
                self.assertEqual(await owner.availability("medical", PLAN), result)
                self.assertEqual(len(requests), 2)
                verified(owner.state)
                self.assertEqual(
                    (await owner.availability("routine_vision", PLAN))["outcome"],
                    "found",
                )
                self.assertEqual(len(requests), 3)

    async def test_input_correction_can_recover_after_cache_expires(
        self,
    ):
        owner, requests = self.owner(
            [
                inventory(
                    status="error",
                    outcome="invalid_input",
                    slots=[],
                    message="Staff must verify chart insurance.",
                ),
                inventory(),
            ]
        )
        self.assertEqual(
            (await owner.availability("medical", PLAN))["outcome"], "needs_input"
        )
        owner.now = lambda: NOW + timedelta(seconds=61)
        self.assertEqual(
            (await owner.availability("medical", PLAN))["outcome"], "found"
        )
        self.assertEqual(len(requests), 2)

    async def test_known_cancellation_rejections_are_recoverable(self):
        for outcome in (
            "provider_conflict",
            "provider_rejected",
            "ownership_mismatch",
            "write_failed",
        ):
            with self.subTest(outcome=outcome):
                owner, requests = self.owner(
                    [
                        {"status": "error", "outcome": outcome},
                        receipt(
                            appointmentsStatus="found",
                            appointments=[
                                appointment(cancellationToken="fresh-cancel")
                            ],
                        ),
                        {"status": "cancelled", "appointmentId": 77},
                    ]
                )
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                ref = owner.appointments()[0]["appointmentRef"]
                result = await self.tool(
                    owner, "cancel_appointment", appointmentRef=ref, readBack=True
                )
                self.assertNotIn("outcome could not be confirmed", result)
                self.assertEqual([a.id for a in owner.state.chart().appointments], [77])
                self.assertEqual(owner.state.chart().appointmentsStatus, "error")
                resolver = PatientResolver(
                    owner.state, PatientMiddleware(owner.http._client, CONFIG)
                )
                resolver._candidates = (Candidate.model_validate(candidate()),)
                self.addAsyncCleanup(resolver.aclose)
                result = await resolver.resolve("Jane", "01/02/1980")
                self.assertEqual(result["outcome"], "verified")
                self.assertEqual(requests[1][0], "/api/patient/resolve")
                ref = owner.appointments()[0]["appointmentRef"]
                result = await self.tool(
                    owner, "cancel_appointment", appointmentRef=ref, readBack=True
                )
                self.assertTrue(result.startswith("success:"), result)
                self.assertEqual(len(requests), 3)
                self.assertEqual(requests[-1][1]["cancellationToken"], "fresh-cancel")

    async def test_definite_booking_rejections_are_recoverable(self):
        for rejection, expected in (
            ({"status": "error", "outcome": "provider_rejected"}, "rejected"),
            ({"status": "error", "outcome": "provider_conflict"}, "rejected"),
            ({"status": "error", "outcome": "patient_context_mismatch"}, "rejected"),
            ({"status": "error"}, "rejected"),
            ({"status": "error", "outcome": ""}, "rejected"),
            ({"status": "error", "outcome": "validation"}, "rejected"),
        ):
            with self.subTest(rejection=rejection):
                owner, requests = self.owner(
                    [
                        inventory(),
                        rejection,
                        inventory(slots=[slot(start="2026-09-15T10:00")]),
                        booking(888, "booked"),
                    ]
                )
                result = await self.book(owner, await self.slots(owner))
                self.assertNotIn("outcome could not be confirmed", result)
                reports = owner.state.reporter.appointment.call_args_list
                self.assertEqual(
                    reports[-1].args[0]["bookingResult"]["status"], expected
                )
                result = await self.book(owner, await self.slots(owner))
                self.assertTrue(result.startswith("success: Booked"), result)
                self.assertEqual(len(requests), 4)
                self.assertEqual(requests[-1][0], "/api/appointment/book")

    async def test_ambiguous_booking_results_block_repeat_writes(self):
        async def server_error(request):
            return httpx.Response(500)

        for ambiguous in (
            {"status": "error", "outcome": "indeterminate_write"},
            server_error,
        ):
            with self.subTest(ambiguous=ambiguous):
                owner, requests = self.owner([inventory(), ambiguous, inventory()])
                result = await self.book(owner, await self.slots(owner))
                self.assertIn("outcome could not be confirmed", result)
                result = await self.book(owner, await self.slots(owner))
                self.assertIn("outcome could not be confirmed", result)
                self.assertEqual(
                    [path for path, _, _ in requests].count("/api/appointment/book"),
                    1,
                )

    async def test_only_definite_cancellation_rejections_mark_appointments_error(self):
        cases = [
            (WriteReceipt(status="error", outcome=outcome), "error")
            for outcome in (
                "invalid_cancellation_token",
                "provider_conflict",
                "provider_rejected",
                "ownership_mismatch",
                "write_failed",
            )
        ] + [
            (WriteReceipt(status="cancelled", appointmentId=999), "found"),
            (SchedulingFailure(reason="invalid_response", uncertain=True), "found"),
            (SchedulingFailure(reason="transport_error", uncertain=True), "found"),
            (SchedulingFailure(reason="not_configured"), "found"),
        ]
        for response, expected in cases:
            with self.subTest(response=response.model_dump()):
                owner, _ = self.owner([])
                owner.http.cancel = AsyncMock(return_value=response)
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                ref = owner.appointments()[0]["appointmentRef"]
                await self.tool(
                    owner, "cancel_appointment", appointmentRef=ref, readBack=True
                )
                owner.http.cancel.assert_awaited_once()
                self.assertEqual(owner.state.chart().appointmentsStatus, expected)

    async def test_cancellation_cancellation_receipt_must_identify_selected_appointment(
        self,
    ):
        for cancellation_receipt in (
            {"status": "cancelled"},
            {"status": "cancelled", "appointmentId": 999},
            {
                "status": "cancelled",
                "appointmentId": 999,
                "outcome": "invalid_cancellation_token",
            },
        ):
            with self.subTest(cancellation_receipt=cancellation_receipt):
                owner, requests = self.owner([cancellation_receipt])
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                ref = owner.appointments()[0]["appointmentRef"]
                result = await self.tool(
                    owner, "cancel_appointment", appointmentRef=ref, readBack=True
                )
                self.assertIn("outcome could not be confirmed", result)
                self.assertEqual([a.id for a in owner.state.chart().appointments], [77])
                self.assertNotIn("No upcoming appointments", owner.appointments_text())
                repeated = await self.tool(
                    owner, "cancel_appointment", appointmentRef=ref, readBack=True
                )
                self.assertEqual(repeated, result)
                self.assertEqual(len(requests), 1)

    async def test_cancellation_requires_confirmation_without_writing(self):
        owner, requests = self.owner([{"status": "cancelled", "appointmentId": 77}])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = owner.appointments()[0]["appointmentRef"]
        for confirmation in (None, False):
            result = await self.tool(
                owner,
                "cancel_appointment",
                appointmentRef=ref,
                readBack=confirmation,
            )
            self.assertTrue(result.startswith("needs_input: Confirm cancellation"))
            self.assertEqual(requests, [])
            self.assertEqual([a.id for a in owner.state.chart().appointments], [77])
        result = await self.tool(
            owner, "cancel_appointment", appointmentRef=ref, readBack=True
        )
        self.assertIn("success: The selected appointment was cancelled.", result)
        self.assertEqual(len(requests), 1)
        self.assertEqual(owner.state.chart().appointments, [])

    async def test_loaded_appointment_says_the_short_name_and_reports_the_charts(self):
        owner, _ = self.owner([{"status": "cancelled", "appointmentId": 77}])
        chart = appointment(provider="Dr. Austin Bach (Overflow)")
        verified(owner.state, appointmentsStatus="found", appointments=[chart])
        listed = owner.appointments()[0]
        self.assertEqual(listed["provider"], "Dr. Bach")
        ref = listed["appointmentRef"]
        confirm = await self.tool(
            owner, "cancel_appointment", appointmentRef=ref, readBack=None
        )
        self.assertIn("with Dr. Bach before cancelling", confirm)
        await self.tool(owner, "cancel_appointment", appointmentRef=ref, readBack=True)
        evidence = owner.state.reporter.appointment.call_args.args[0]
        self.assertEqual(
            evidence["cancellationResult"]["providerName"],
            "Dr. Austin Bach (Overflow)",
        )

    async def test_vague_reason_is_preserved_and_booking_requires_confirmation(self):
        owner, requests = self.owner(
            [
                inventory(),
                booking(888, "booked"),
            ]
        )
        ref = await self.slots(owner)
        args = dict(
            appointmentSlotRef=ref,
            appointmentReason="eye problems",
            referringDoctor=None,
            readBack=None,
        )
        result = await self.tool(owner, "book_appointment", **args)
        self.assertTrue(result.startswith("needs_input: Confirm"))
        self.assertEqual(len(requests), 1)
        args["readBack"] = True
        result = await self.tool(owner, "book_appointment", **args)
        self.assertTrue(result.startswith("success: Booked"))
        self.assertEqual(requests[-1][1]["appointmentReason"], "eye problems")
        self.assertIn("Existing appointments", result)

    async def test_unresolved_appointment_type_gives_the_reason_not_its_fields(self):
        reason = "Post-op appointments must use a medical scheduling lane."
        owner, requests = self.owner(
            [
                inventory(),
                {
                    "status": "error",
                    "outcome": "appointment_type_unresolved",
                    "missing": ["routing"],
                    "message": reason,
                },
                inventory(),
                booking(),
            ]
        )
        ref = await self.slots(owner)
        result = await self.book(owner, ref)
        self.assertTrue(result.startswith("needs_input:"), result)
        self.assertIn(reason, result)
        self.assertIn("staff request", result)
        self.assertNotIn("routing", result)
        self.assertEqual(owner.state.chart().appointments, [])
        retried = await self.book(owner, await self.slots(owner))
        self.assertTrue(retried.startswith("success:"), retried)
        self.assertEqual(len(requests), 4)

    async def test_no_effect_refusals_show_the_middlewares_reason(self):
        reason = "The destination does not support the existing appointment type."
        move = dict(appointmentReason="Follow up", readBack=True)
        cases = {
            "book provider_rejected": (
                "book",
                {"status": "error", "outcome": "provider_rejected", "message": reason},
            ),
            "book validation": (
                "book",
                {"status": "error", "outcome": "validation", "message": reason},
            ),
            "book blank": ("book", {"status": "error", "message": reason}),
            "book write_failed": (
                "book",
                {"status": "error", "outcome": "write_failed", "message": reason},
            ),
            "cancel provider_rejected": (
                "cancel",
                {"status": "error", "outcome": "provider_rejected", "message": reason},
            ),
            "cancel validation": (
                "cancel",
                {"status": "error", "outcome": "validation", "message": reason},
            ),
            "cancel blank": ("cancel", {"status": "error", "message": reason}),
            "cancel expired": (
                "cancel",
                {
                    "status": "error",
                    "outcome": "invalid_cancellation_token",
                    "message": reason,
                },
            ),
            "move validation": (
                "move",
                {"status": "failed", "outcome": "validation", "message": reason},
            ),
            "move blank": ("move", {"status": "failed", "message": reason}),
            "move provider_rejected": (
                "move",
                {"status": "failed", "outcome": "provider_rejected", "message": reason},
            ),
            "move expired": (
                "move",
                {
                    "status": "failed",
                    "outcome": "invalid_reschedule_token",
                    "message": reason,
                },
            ),
            "move write_failed": (
                "move",
                {"status": "failed", "outcome": "write_failed", "message": reason},
            ),
        }
        for case, (write, response) in cases.items():
            with self.subTest(case=case):
                cancelled = {"status": "cancelled", "appointmentId": 77}
                owner, requests = self.owner(
                    [response, cancelled]
                    if write == "cancel"
                    else [inventory(), response, cancelled]
                )
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                old_ref = owner.appointments()[0]["appointmentRef"]
                if write == "book":
                    result = await self.book(owner, await self.slots(owner))
                elif write == "cancel":
                    result = await self.tool(
                        owner,
                        "cancel_appointment",
                        appointmentRef=old_ref,
                        readBack=True,
                    )
                else:
                    result = await self.tool(
                        owner,
                        "reschedule_appointment",
                        oldAppointmentRef=old_ref,
                        appointmentSlotRef=await self.slots(
                            owner, rescheduleAppointmentRef=old_ref
                        ),
                        **move,
                    )
                self.assertIn(reason, result)
                self.assertNotIn("could not be confirmed", result)
                self.assertEqual([a.id for a in owner.state.chart().appointments], [77])
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                sent = len(requests)
                later = await self.tool(
                    owner, "cancel_appointment", appointmentRef=old_ref, readBack=True
                )
                self.assertEqual(len(requests), sent + 1, later)

    async def test_uncertain_writes_do_not_pass_the_middlewares_message_on(self):
        said = "AdvancedMD may have booked the appointment."
        for write, response in (
            ("book", {"status": "error", "outcome": "indeterminate_write"}),
            ("move", {"status": "uncertain", "outcome": "indeterminate_write"}),
        ):
            with self.subTest(write=write):
                owner, _ = self.owner([inventory(), response | {"message": said}])
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                old_ref = owner.appointments()[0]["appointmentRef"]
                if write == "book":
                    result = await self.book(owner, await self.slots(owner))
                else:
                    result = await self.tool(
                        owner,
                        "reschedule_appointment",
                        oldAppointmentRef=old_ref,
                        appointmentSlotRef=await self.slots(
                            owner, rescheduleAppointmentRef=old_ref
                        ),
                        readBack=True,
                    )
                self.assertIn("could not be confirmed", result)
                self.assertNotIn(said, result)

    async def test_an_outcome_outside_the_vocabulary_fails_closed(self):
        self.assertNotIn("unclassified", get_args(SchedulingOutcome))
        unclassified = {"outcome": "unclassified", "message": "Unexpected failure."}
        for write, response in (
            ("book", {"status": "error"} | unclassified),
            ("cancel", {"status": "error"} | unclassified),
            ("move", {"status": "failed"} | unclassified),
        ):
            with self.subTest(write=write):
                owner, requests = self.owner(
                    [response] if write == "cancel" else [inventory(), response]
                )
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                old_ref = owner.appointments()[0]["appointmentRef"]
                if write == "book":
                    result = await self.book(owner, await self.slots(owner))
                elif write == "cancel":
                    result = await self.tool(
                        owner,
                        "cancel_appointment",
                        appointmentRef=old_ref,
                        readBack=True,
                    )
                else:
                    result = await self.tool(
                        owner,
                        "reschedule_appointment",
                        oldAppointmentRef=old_ref,
                        appointmentSlotRef=await self.slots(
                            owner, rescheduleAppointmentRef=old_ref
                        ),
                        readBack=True,
                    )
                self.assertIn("could not be confirmed", result)
                self.assertNotIn("Unexpected failure", result)
                sent = len(requests)
                repeat = await self.tool(
                    owner, "cancel_appointment", appointmentRef=old_ref, readBack=True
                )
                self.assertIn("could not be confirmed", repeat)
                self.assertEqual(len(requests), sent)

    async def test_lost_move_race_says_the_slot_was_taken_and_the_search_recovers(self):
        owner, requests = self.owner(
            [
                inventory(),
                {
                    "status": "failed",
                    "outcome": "slot_unavailable",
                    "message": "This time slot is no longer available.",
                },
                receipt(
                    appointmentsStatus="found",
                    appointments=[appointment(rescheduleToken="fresh-reschedule")],
                ),
                inventory(),
                rescheduled(),
            ]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        old_ref = owner.appointments()[0]["appointmentRef"]

        lost = await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=old_ref,
            appointmentSlotRef=await self.slots(
                owner, rescheduleAppointmentRef=old_ref
            ),
            readBack=True,
        )

        self.assertTrue(lost.startswith("needs_input:"), lost)
        self.assertIn("taken", lost)
        self.assertIn("2026-09-20 at 10:00 AM was not changed", lost)
        self.assertIn("Search again", lost)
        self.assertNotIn("staff", lost)
        self.assertEqual(owner.state.chart().appointmentsStatus, "error")

        retried = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            rescheduleAppointmentRef=old_ref,
        )

        self.assertTrue(retried.startswith("success:"), retried)
        self.assertEqual(owner.appointments()[0]["appointmentRef"], old_ref)
        self.assertEqual(
            [path for path, _, _ in requests][2:],
            ["/api/patient/resolve", "/api/scheduler/slots"],
        )
        self.assertEqual(requests[3][1]["rescheduleToken"], "fresh-reschedule")

        moved = await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=old_ref,
            appointmentSlotRef=soonest(retried)[0],
            readBack=True,
        )
        self.assertTrue(moved.startswith("success:"), moved)
        self.assertEqual(requests[-1][0], "/api/appointment/reschedule")

    async def test_a_move_go_could_not_prove_unchanged_never_says_it_was(self):
        for outcome, said in (
            ("ownership_mismatch", GO_ORIGINAL_CHANGED),
            ("write_failed", GO_COULD_NOT_READ_ORIGINAL),
        ):
            with self.subTest(outcome=outcome):
                owner, _ = self.owner(
                    [
                        inventory(),
                        {"status": "failed", "outcome": outcome, "message": said},
                    ]
                )
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                result = await self.tool(
                    owner,
                    "reschedule_appointment",
                    oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
                    appointmentSlotRef=await self.move_slot(owner),
                    readBack=True,
                )
                self.assertIn(said, result)
                self.assertNotIn("was not changed", result)
                self.assertNotIn("Search again", result)
                self.assertIn("Resolve the patient again", result)
                self.assertEqual(owner.state.chart().appointmentsStatus, "error")

    async def test_a_failed_move_keeps_gos_word_that_the_original_was_not_cancelled(
        self,
    ):
        owner, _ = self.owner(
            [
                inventory(),
                {
                    "status": "failed",
                    "outcome": "write_failed",
                    "message": GO_BOOK_FAILED,
                },
            ]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        result = await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
            appointmentSlotRef=await self.move_slot(owner),
            readBack=True,
        )
        self.assertIn(GO_BOOK_FAILED, result)
        self.assertNotIn("was not changed", result)

    async def test_a_refused_cancel_never_says_the_appointment_was_unchanged(self):
        for outcome, said in GO_CANCEL_REFUSALS:
            with self.subTest(said=said):
                owner, _ = self.owner(
                    [{"status": "error", "outcome": outcome, "message": said}]
                )
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                result = await self.tool(
                    owner,
                    "cancel_appointment",
                    appointmentRef=owner.appointments()[0]["appointmentRef"],
                    readBack=True,
                )
                self.assertIn(said, result)
                self.assertNotIn("was not changed", result)
                self.assertIn("Resolve the patient again", result)

    async def test_a_successful_write_on_a_revoked_chart_says_to_reload(self):
        owner, _ = self.owner([inventory(), booking()])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = await self.slots(owner)
        revoke(owner.state)

        booked = await self.book(owner, ref)

        self.assertTrue(booked.startswith("success: Booked"), booked)
        self.assertTrue(
            booked.endswith(
                "\nResolve the patient again to reload appointments before moving or cancelling."
            ),
            booked,
        )

    async def test_a_lagging_reload_keeps_a_visit_booked_this_call_for_a_move(self):
        lagging = receipt(appointmentsStatus="none", appointments=[])
        owner, requests = self.owner([inventory(), booking(), lagging, inventory()])
        await self.book(owner, await self.slots(owner))
        booked_ref = owner.appointments()[0]["appointmentRef"]
        revoke(owner.state)

        found = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            rescheduleAppointmentRef=booked_ref,
        )

        self.assertTrue(found.startswith("success:"), found)
        self.assertEqual(requests[-1][0], "/api/scheduler/slots")
        self.assertEqual(requests[-1][1]["rescheduleToken"], "new-reschedule")

    async def test_a_lagging_reload_cannot_target_a_visit_cancelled_this_call(self):
        both = [appointment(), appointment(id=78)]
        lagging = receipt(appointmentsStatus="found", appointments=both)
        owner, requests = self.owner(
            [{"status": "cancelled", "appointmentId": 77}, lagging]
        )
        verified(owner.state, appointmentsStatus="found", appointments=both)
        cancelled_ref = owner.appointments()[0]["appointmentRef"]
        await self.tool(
            owner, "cancel_appointment", appointmentRef=cancelled_ref, readBack=True
        )
        revoke(owner.state)

        refused = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            rescheduleAppointmentRef=cancelled_ref,
        )

        self.assertTrue(refused.startswith("needs_input: Choose"), refused)
        self.assertEqual(
            [path for path, _, _ in requests],
            ["/api/appointment/cancel", "/api/patient/resolve"],
        )

    async def test_insurance_moving_during_a_write_is_not_called_a_patient_change(self):
        async def insurance_moved(request):
            coverage_written(owner.state, "Aetna", "car1")
            return httpx.Response(200, json=booking())

        owner, _ = self.owner([inventory(), insurance_moved])
        result = await self.book(owner, await self.slots(owner))
        self.assertTrue(result.startswith("success: Booked"), result)
        self.assertNotIn("patient", result.replace("for Doe, Jane", "").lower())
        self.assertEqual([a.id for a in owner.state.chart().appointments], [888])

    async def test_a_real_patient_change_during_a_write_is_still_reported(self):
        async def switched(request):
            verified(owner.state, "chart-john")
            return httpx.Response(200, json=booking())

        owner, _ = self.owner([inventory(), switched])
        result = await self.book(owner, await self.slots(owner))
        self.assertTrue(result.startswith("success: Booked"), result)
        self.assertIn("the active patient has since changed", result)

    async def test_cancellation_exact_target_token_and_expired_details(self):
        owner, requests = self.owner(
            [{"status": "error", "outcome": "invalid_cancellation_token"}]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = owner.appointments()[0]["appointmentRef"]
        self.assertEqual(
            (
                await self.tool(
                    owner, "cancel_appointment", appointmentRef="77", readBack=True
                )
            ).split(":", 1)[0],
            "needs_input",
        )
        self.assertEqual(
            (
                await self.tool(
                    owner, "cancel_appointment", appointmentRef=ref, readBack=True
                )
            ).split(":", 1)[0],
            "needs_input",
        )
        self.assertEqual(
            requests[0][1],
            {"patientId": "chart-jane", "cancellationToken": "private-cancel"},
        )
        self.assertEqual(owner.state.chart().appointmentsStatus, "error")

    async def test_reschedule_without_a_reason_keeps_the_existing_visit_type(self):
        cases = (
            (
                None,
                "Established Adult Medical",
                "Reschedule: Established Adult Medical",
            ),
            ("   ", "", "Reschedule: medical"),
        )
        for reason, kind, expected in cases:
            with self.subTest(reason=reason, kind=kind):
                owner, requests = self.owner([inventory(), rescheduled()])
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment(type=kind)],
                )
                result = await self.tool(
                    owner,
                    "reschedule_appointment",
                    oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
                    appointmentSlotRef=await self.move_slot(owner),
                    appointmentReason=reason,
                    readBack=True,
                )
                self.assertTrue(result.startswith("success:"), result)
                payload = requests[-1][1]
                self.assertEqual(payload["appointmentReason"], expected)
                self.assertEqual(payload["visitReason"], expected)

    async def test_null_plan_move_sends_its_token_and_lets_go_filter_the_doctor(self):
        owner, requests = self.owner(
            [
                inventory(
                    slots=[
                        slot("Dr. Austin Bach", "2026-09-15T08:00"),
                        slot("Dr. Austin Bach", "2026-09-15T09:00"),
                    ]
                ),
                rescheduled(),
            ]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        result = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
        )
        self.assertTrue(result.startswith("success:"), result)
        body = requests[0][1]
        self.assertNotIn("insurancePlanId", body)
        self.assertEqual(body["rescheduleToken"], "private-reschedule")
        self.assertEqual(body["office"], "spring-hill")
        self.assertNotIn("private-reschedule", result)
        moved = await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
            appointmentSlotRef=soonest(result)[0],
            readBack=True,
        )
        self.assertTrue(moved.startswith("success:"), moved)
        self.assertEqual(requests[1][1]["rescheduleToken"], "private-reschedule")

    async def test_null_plan_move_with_no_openings_names_the_doctor(self):
        owner, _ = self.owner([inventory(outcome="no_availability", slots=[])])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        result = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
        )
        self.assertTrue(result.startswith("no_results:"), result)
        self.assertIn("Dr. Bach", result)

    async def test_a_slot_only_serves_the_search_that_found_it(self):
        owner, requests = self.owner([inventory()])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        moving = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
        )
        refused = await self.book(owner, soonest(moving)[0])
        self.assertTrue(refused.startswith("needs_input:"), refused)
        self.assertIn("cannot book a new one", refused)

        owner, requests = self.owner([inventory()])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        new_visit = await self.slots(owner)
        for read_back in (None, True):
            with self.subTest(readBack=read_back):
                refused = await self.tool(
                    owner,
                    "reschedule_appointment",
                    oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
                    appointmentSlotRef=new_visit,
                    readBack=read_back,
                )
                self.assertTrue(refused.startswith("needs_input:"), refused)
                self.assertIn("not found for this appointment", refused)
        self.assertEqual(len(requests), 1)

    async def test_move_search_needs_a_matching_loaded_appointment(self):
        cases = (
            ("medical", None, None),
            ("routine_vision", "appointment", None),
            ("medical", "A99", None),
            ("routine_vision", "appointment", PLAN),
            ("medical", "A99", PLAN),
        )
        for visit, ref, plan in cases:
            with self.subTest(visit=visit, ref=ref, plan=plan):
                owner, requests = self.owner([])
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                if ref == "appointment":
                    ref = owner.appointments()[0]["appointmentRef"]
                result = await self.tool(
                    owner,
                    "list_available_appointments",
                    visitType=visit,
                    insurancePlanId=plan,
                    rescheduleAppointmentRef=ref,
                )
                self.assertTrue(result.startswith("needs_input:"), result)
                self.assertEqual(requests, [])

    async def test_search_modes_on_the_wire(self):
        refused = "needs_input: Send the confirmed plan ID."
        cases = {
            "plan, no reference": (PLAN, False, {"insurancePlanId": PLAN}),
            "plan and reference": (
                PLAN,
                True,
                {"insurancePlanId": PLAN, "rescheduleToken": "private-reschedule"},
            ),
            "null plan and reference": (
                None,
                True,
                {"rescheduleToken": "private-reschedule"},
            ),
        }
        for case, (plan, moving, expected) in cases.items():
            with self.subTest(case=case):
                owner, requests = self.owner([inventory()])
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                result = await self.tool(
                    owner,
                    "list_available_appointments",
                    visitType="medical",
                    insurancePlanId=plan,
                    rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"]
                    if moving
                    else None,
                )
                self.assertTrue(result.startswith("success:"), result)
                body = requests[0][1]
                self.assertEqual(
                    {
                        key: body[key]
                        for key in ("insurancePlanId", "rescheduleToken")
                        if key in body
                    },
                    expected,
                )
        owner, requests = self.owner([])
        result = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
        )
        self.assertTrue(result.startswith(refused), result)
        self.assertEqual(requests, [])

    async def test_null_plan_move_knows_its_office_before_any_question(self):
        cases = {
            "Hollywood call moving a Sweetwater visit": (
                "hollywood",
                dict(officeId="sweetwater"),
                "medical",
                "sweetwater",
            ),
            "Hollywood call moving a routine visit gets no promotion": (
                "hollywood",
                dict(officeId="hollywood", visitType="routine_vision"),
                "routine_vision",
                "hollywood",
            ),
            "Sweetwater call moving a Hollywood visit": (
                "sweetwater",
                dict(officeId="hollywood"),
                "medical",
                "hollywood",
            ),
            "Spring Hill call moving a Crystal River visit": (
                "spring-hill",
                dict(officeId="crystal_river"),
                "medical",
                "crystal-river",
            ),
        }
        for case, (called, loaded, visit, office) in cases.items():
            with self.subTest(case=case):
                state = call_state(None, get_office_profile(called))
                owner, requests = self.owner([inventory()], state=state)
                verified(
                    state,
                    appointmentsStatus="found",
                    appointments=[appointment(**loaded)],
                )
                result = await self.tool(
                    owner,
                    "list_available_appointments",
                    visitType=visit,
                    insurancePlanId=None,
                    rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
                )
                self.assertTrue(result.startswith("success:"), result)
                self.assertEqual(requests[0][1]["office"], office)

    async def test_a_move_search_leaves_the_promotion_for_a_later_new_visit(self):
        state = call_state(None, get_office_profile("hollywood"))
        owner, _ = self.owner([inventory()], state=state)
        verified(
            state,
            appointmentsStatus="found",
            appointments=[
                appointment(officeId="hollywood", visitType="routine_vision")
            ],
        )
        moved = await self.tool(
            owner,
            "list_available_appointments",
            visitType="routine_vision",
            insurancePlanId=None,
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
        )
        self.assertTrue(moved.startswith("success:"), moved)

        new_visit = await self.tool(
            owner, "list_available_appointments", visitType="routine_vision"
        )

        self.assertEqual(new_visit, HOLLYWOOD_ROUTINE_OFFICE_CHOICE)

    async def test_null_plan_move_refuses_another_office_or_an_unknown_one(self):
        state = call_state(None, get_office_profile("hollywood"))
        owner, requests = self.owner([], state=state)
        verified(
            state,
            appointmentsStatus="found",
            appointments=[appointment(officeId="sweetwater")],
        )
        elsewhere = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            office="hollywood",
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
        )
        self.assertTrue(elsewhere.startswith("needs_input:"), elsewhere)
        self.assertIn("confirm the plan", elsewhere)
        verified(
            state,
            appointmentsStatus="found",
            appointments=[appointment(officeId="a_new_office")],
        )
        unknown = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
        )
        self.assertTrue(unknown.startswith("blocked:"), unknown)
        self.assertIn("staff request", unknown)
        self.assertEqual(requests, [])

    async def test_plan_gated_move_stays_at_its_office_unless_another_is_named(self):
        for office, expected in ((None, "sweetwater"), ("hollywood", "hollywood")):
            with self.subTest(office=office):
                state = call_state(None, get_office_profile("hollywood"))
                owner, requests = self.owner([inventory()], state=state)
                verified(
                    state,
                    appointmentsStatus="found",
                    appointments=[appointment(officeId="sweetwater")],
                )
                result = await self.tool(
                    owner,
                    "list_available_appointments",
                    visitType="medical",
                    office=office,
                    rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
                )
                self.assertTrue(result.startswith("success:"), result)
                self.assertEqual(requests[0][1]["office"], expected)
                self.assertEqual(requests[0][1]["insurancePlanId"], PLAN)
                self.assertEqual(
                    requests[0][1]["rescheduleToken"], "private-reschedule"
                )

    async def test_null_plan_move_is_refused_after_coverage_changed_this_call(self):
        async def updated(request):
            return httpx.Response(
                200,
                json={
                    "status": "updated",
                    "effect": "completed",
                    "insuranceDecision": decision("Aetna"),
                    "patientId": "chart-jane",
                    "newInsurance": "Aetna",
                },
            )

        owner, requests = self.owner([updated, inventory(), inventory()])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        insurance = InsuranceRegistration(
            owner.state,
            owner._resolver,
            RegistrationMiddleware(owner.http._client, CONFIG),
        )
        self.addAsyncCleanup(insurance.aclose)
        changed = await insurance.update("aetna", "medical", "member-1")
        self.assertEqual(changed["outcome"], "updated")
        old_ref = owner.appointments()[0]["appointmentRef"]

        refused = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            rescheduleAppointmentRef=old_ref,
        )

        self.assertTrue(refused.startswith("needs_input:"), refused)
        self.assertIn("Insurance changed", refused)
        self.assertEqual(len(requests), 1)
        planned = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId="aetna",
            rescheduleAppointmentRef=old_ref,
        )
        self.assertTrue(planned.startswith("success:"), planned)
        self.assertEqual(requests[-1][1]["insurancePlanId"], "aetna")
        verified(
            owner.state,
            "chart-john",
            appointmentsStatus="found",
            appointments=[appointment(id=99)],
        )
        other_patient = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
        )
        self.assertTrue(other_patient.startswith("success:"), other_patient)

    async def test_refused_move_token_reloads_the_chart_once_and_retries(self):
        refusal = {
            "status": "error",
            "outcome": "invalid_reschedule_token",
            "message": "rescheduleToken is invalid or expired.",
            "slots": [],
        }
        fresh = receipt(
            appointmentsStatus="found",
            appointments=[appointment(rescheduleToken="fresh-reschedule")],
        )
        owner, requests = self.owner([refusal, fresh, inventory(), rescheduled()])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        old_ref = owner.appointments()[0]["appointmentRef"]
        since = owner.state.identity.since

        result = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            rescheduleAppointmentRef=old_ref,
        )

        self.assertTrue(result.startswith("success:"), result)
        self.assertEqual(
            [path for path, _, _ in requests],
            ["/api/scheduler/slots", "/api/patient/resolve", "/api/scheduler/slots"],
        )
        self.assertEqual(requests[2][1]["rescheduleToken"], "fresh-reschedule")
        self.assertEqual(owner.state.identity.since, since)
        self.assertEqual(owner.appointments()[0]["appointmentRef"], old_ref)
        moved = await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=old_ref,
            appointmentSlotRef=soonest(result)[0],
            readBack=True,
        )
        self.assertTrue(moved.startswith("success:"), moved)
        self.assertEqual(requests[3][1]["rescheduleToken"], "fresh-reschedule")

    async def test_move_search_stops_after_one_reload(self):
        refusal = {
            "status": "error",
            "outcome": "invalid_reschedule_token",
            "message": "rescheduleToken is invalid or expired.",
            "slots": [],
        }
        fresh = receipt(appointmentsStatus="found", appointments=[appointment()])
        cases = {
            "Go refuses the fresh token too": (
                [refusal, fresh, refusal],
                [
                    "/api/scheduler/slots",
                    "/api/patient/resolve",
                    "/api/scheduler/slots",
                ],
            ),
            "the reload fails": (
                [refusal, {"status": "error"}, {"status": "error"}],
                [
                    "/api/scheduler/slots",
                    "/api/patient/resolve",
                    "/api/patient/resolve",
                ],
            ),
            "the reload loads no appointments": (
                [refusal, receipt(appointmentsStatus="error")],
                ["/api/scheduler/slots", "/api/patient/resolve"],
            ),
        }
        for case, (responses, paths) in cases.items():
            with self.subTest(case=case):
                owner, requests = self.owner(list(responses))
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                result = await self.tool(
                    owner,
                    "list_available_appointments",
                    visitType="medical",
                    insurancePlanId=None,
                    rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
                )
                self.assertTrue(result.startswith("blocked:"), result)
                self.assertIn("staff request", result)
                self.assertEqual([path for path, _, _ in requests], paths)

    async def test_tokenless_move_search_reloads_then_searches_or_goes_to_staff(self):
        tokenless = appointment(rescheduleToken=None)
        fresh = appointment(rescheduleToken="fresh-reschedule")
        owner, requests = self.owner(
            [receipt(appointmentsStatus="found", appointments=[fresh]), inventory()]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[tokenless])
        result = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
        )
        self.assertTrue(result.startswith("success:"), result)
        self.assertEqual(requests[1][1]["rescheduleToken"], "fresh-reschedule")

        owner, requests = self.owner(
            [receipt(appointmentsStatus="found", appointments=[tokenless])]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[tokenless])
        result = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
        )
        self.assertTrue(result.startswith("blocked:"), result)
        self.assertEqual(len(requests), 1)

    async def test_an_expired_cancel_token_recovers_through_a_reload(self):
        owner, requests = self.owner(
            [
                {
                    "status": "error",
                    "outcome": "invalid_cancellation_token",
                    "message": "The cancellation token is invalid or expired.",
                },
                receipt(
                    appointmentsStatus="found",
                    appointments=[appointment(cancellationToken="fresh-cancel")],
                ),
                {"status": "cancelled", "appointmentId": 77},
            ]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = owner.appointments()[0]["appointmentRef"]

        expired = await self.tool(
            owner, "cancel_appointment", appointmentRef=ref, readBack=True
        )
        self.assertTrue(
            expired.startswith("needs_input: The appointment details expired."), expired
        )
        self.assertEqual(owner.state.chart().appointmentsStatus, "error")
        await owner._resolver.resolve("Jane", None)
        cancelled = await self.tool(
            owner, "cancel_appointment", appointmentRef=ref, readBack=True
        )

        self.assertTrue(cancelled.startswith("success:"), cancelled)
        self.assertEqual(
            [(path, body.get("cancellationToken")) for path, body, _ in requests],
            [
                ("/api/appointment/cancel", "private-cancel"),
                ("/api/patient/resolve", None),
                ("/api/appointment/cancel", "fresh-cancel"),
            ],
        )

    async def test_a_write_settling_during_a_reload_discards_the_reload(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_chart(request):
            started.set()
            await release.wait()
            return httpx.Response(
                200,
                json=receipt(appointmentsStatus="found", appointments=[appointment()]),
            )

        owner, requests = self.owner([inventory(), slow_chart, booking()])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        old_ref = owner.appointments()[0]["appointmentRef"]
        new_visit = await self.slots(owner)
        revoke(owner.state)

        searching = asyncio.create_task(
            owner.availability("medical", None, old_ref=old_ref)
        )
        await started.wait()
        booked = await self.book(owner, new_visit)
        self.assertTrue(booked.startswith("success:"), booked)
        release.set()
        result = await searching

        self.assertEqual(result["outcome"], "needs_staff_review")
        self.assertEqual(owner.state.chart().appointmentsStatus, "error")
        self.assertIn(888, [a.id for a in owner.state.chart().appointments])
        self.assertEqual(len(requests), 3)

    async def lookup_during_a_settling_write(self, settled):
        write_started, release_write = asyncio.Event(), asyncio.Event()
        lookup_started, release_lookup = asyncio.Event(), asyncio.Event()

        async def slow_write(request):
            write_started.set()
            await release_write.wait()
            return httpx.Response(200, json=settled)

        async def slow_lookup(request):
            lookup_started.set()
            await release_lookup.wait()
            return httpx.Response(200, json=receipt("chart-john", "John", "02/03/1982"))

        owner, _ = self.owner([inventory(), slow_write, slow_lookup])
        ref = await self.slots(owner)
        writing = asyncio.create_task(self.book(owner, ref))
        await write_started.wait()
        looking = asyncio.create_task(owner._resolver.resolve("John", "02/03/1982"))
        await lookup_started.wait()
        release_write.set()
        await writing
        release_lookup.set()
        return owner, await looking

    async def test_a_write_settling_during_another_patients_lookup_keeps_that_lookup(
        self,
    ):
        for settled in (
            booking(),
            {"status": "error", "outcome": "indeterminate_write"},
        ):
            with self.subTest(settled=settled["status"]):
                owner, result = await self.lookup_during_a_settling_write(settled)
                self.assertEqual(result["outcome"], "switched")
                self.assertEqual(owner.state.chart().patientId, "chart-john")

    async def test_a_revocation_during_a_reload_discards_the_reload(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_chart(request):
            started.set()
            await release.wait()
            return httpx.Response(
                200,
                json=receipt(appointmentsStatus="found", appointments=[appointment()]),
            )

        owner, _ = self.owner(
            [slow_chart, {"status": "error", "outcome": "provider_rejected"}]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        reloading = asyncio.create_task(owner._resolver.reload_chart())
        await started.wait()
        refused = await self.tool(
            owner,
            "cancel_appointment",
            appointmentRef=owner.appointments()[0]["appointmentRef"],
            readBack=True,
        )
        self.assertTrue(refused.startswith("needs_input:"), refused)
        release.set()

        self.assertFalse(await reloading)
        self.assertEqual(owner.state.chart().appointmentsStatus, "error")

    async def test_move_goes_to_staff_when_go_cannot_scope_it(self):
        said = "This appointment's doctor or visit type could not be confirmed."
        owner, _ = self.owner(
            [inventory(outcome="move_needs_staff", slots=[], message=said)]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        result = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId=None,
            rescheduleAppointmentRef=owner.appointments()[0]["appointmentRef"],
        )
        self.assertEqual(
            result, f"blocked: {said} Do not retry this search; offer a staff request."
        )

    async def test_move_replays_on_the_old_appointment_with_fresh_tokens(self):
        owner, requests = self.owner(
            [
                inventory(),
                receipt(
                    appointmentsStatus="found",
                    appointments=[appointment(rescheduleToken="fresh-reschedule")],
                ),
                rescheduled(),
            ]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = await self.move_slot(owner)
        revoke(owner.state)
        await owner._resolver.resolve("Jane", None)
        args = dict(
            oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
            appointmentSlotRef=ref,
            readBack=True,
        )

        moved = await self.tool(owner, "reschedule_appointment", **args)

        self.assertTrue(moved.startswith("success:"), moved)
        self.assertEqual(requests[-1][1]["rescheduleToken"], "fresh-reschedule")
        self.assertEqual(
            await self.tool(owner, "reschedule_appointment", **args), moved
        )
        self.assertEqual(
            [path for path, _, _ in requests].count("/api/appointment/reschedule"), 1
        )

    async def test_booking_replays_by_selection_until_it_is_cancelled(self):
        owner, requests = self.owner(
            [
                inventory(),
                booking(),
                inventory(),
                {"status": "cancelled", "appointmentId": 888},
                inventory(),
                booking(889),
            ]
        )
        first = await self.book(owner, await self.slots(owner))
        self.assertTrue(first.startswith("success: Booked"), first)
        repeated = await self.book(
            owner, await self.slots(owner, startDate="2026-09-15")
        )
        self.assertEqual(repeated.split("\n")[0], first.split("\n")[0])
        self.assertEqual(
            [path for path, _, _ in requests].count("/api/appointment/book"), 1
        )
        await self.tool(
            owner,
            "cancel_appointment",
            appointmentRef=owner.appointments()[0]["appointmentRef"],
            readBack=True,
        )
        again = await self.book(owner, await self.slots(owner))
        self.assertTrue(again.startswith("success: Booked"), again)
        self.assertEqual([a.id for a in owner.state.chart().appointments], [889])

    def two_visits(self, responses):
        owner, requests = self.owner(responses)
        verified(
            owner.state,
            appointmentsStatus="found",
            appointments=[
                appointment(),
                appointment(
                    id=78,
                    date="2026-10-05",
                    time="2:30 PM",
                    provider="Dr. J. Licht",
                    rescheduleToken="private-reschedule-78",
                ),
            ],
        )
        first, second = (a["appointmentRef"] for a in owner.appointments())
        return owner, requests, first, second

    async def test_a_slot_found_for_one_visit_never_moves_another(self):
        owner, requests, first, second = self.two_visits(
            [inventory(), inventory(), rescheduled(old_id=78)]
        )
        stale = await self.slots(owner, rescheduleAppointmentRef=first)
        ref = await self.slots(owner, rescheduleAppointmentRef=second)
        self.assertEqual((stale, ref), (REF_0900, REF_0900))

        for old_ref, answer in (
            (first, "not found for this appointment"),
            ("A9", "Choose and confirm"),
        ):
            for read_back in (None, True):
                with self.subTest(oldAppointmentRef=old_ref, readBack=read_back):
                    refused = await self.tool(
                        owner,
                        "reschedule_appointment",
                        oldAppointmentRef=old_ref,
                        appointmentSlotRef=ref,
                        readBack=read_back,
                    )
                    self.assertTrue(refused.startswith("needs_input:"), refused)
                    self.assertIn(answer, refused)
        self.assertEqual(len(requests), 2)
        self.assertEqual([a.id for a in owner.state.chart().appointments], [77, 78])

        args = dict(oldAppointmentRef=second, appointmentSlotRef=ref)
        read_back = await self.tool(
            owner, "reschedule_appointment", readBack=None, **args
        )
        self.assertEqual(
            read_back.split("\n")[0],
            "needs_input: Confirm moving the appointment on 2026-10-05 at 2:30 PM Eastern"
            " with Dr. Licht to 2026-09-15 at 9:00 AM Eastern with Dr. Bach for Doe, Jane"
            " before rescheduling.",
        )
        self.assertEqual(len(requests), 2)
        moved = await self.tool(owner, "reschedule_appointment", readBack=True, **args)
        self.assertTrue(moved.startswith("success:"), moved)
        self.assertEqual(requests[-1][1]["rescheduleToken"], "private-reschedule-78")
        self.assertEqual([a.id for a in owner.state.chart().appointments], [77, 888])
        self.assertEqual(
            await self.tool(owner, "reschedule_appointment", readBack=True, **args),
            moved,
        )
        self.assertEqual(len(requests), 3)

    async def test_a_move_slot_ref_answers_a_new_visit_call_with_the_move_it_made(self):
        owner, requests = self.owner([inventory(), rescheduled(), inventory()])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = await self.move_slot(owner)
        moved = await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
            appointmentSlotRef=ref,
            readBack=True,
        )
        self.assertTrue(moved.startswith("success:"), moved)
        for search_again in (False, True):
            with self.subTest(search_again=search_again):
                if search_again:
                    self.assertEqual(await self.slots(owner), ref)
                self.assertEqual(
                    (await self.book(owner, ref)).split("\n")[0],
                    moved.split("\n")[0],
                )
        self.assertEqual(len(requests), 3)

    async def test_a_new_search_replaces_earlier_slot_refs(self):
        later = inventory(slots=[slot(start="2026-09-16T09:00")])
        owner, requests = self.owner([inventory(), later])
        earlier = await self.slots(owner)
        await self.slots(owner, startDate="2026-09-16")
        refused = await self.book(owner, earlier)
        self.assertTrue(refused.startswith("needs_input:"), refused)
        self.assertIn("not returned", refused)
        self.assertEqual(len(requests), 2)

    def test_reschedule_names_the_appointment_and_the_slot(self):
        owner, _ = self.owner([])
        tools = {
            tool.id: build_strict_openai_schema(tool)["function"]["parameters"]
            for tool in llm.find_function_tools(SchedulingTools(owner))
        }
        self.assertEqual(
            set(tools["reschedule_appointment"]["properties"]),
            {
                "oldAppointmentRef",
                "appointmentSlotRef",
                "readBack",
                "appointmentReason",
            },
        )
        self.assertIn(
            "rescheduleAppointmentRef",
            tools["list_available_appointments"]["properties"],
        )

    async def test_reschedule_partial_and_success_never_rebooks(self):
        for status in ("cancelled", "error"):
            owner, requests = self.owner([inventory(), rescheduled(status)])
            verified(
                owner.state, appointmentsStatus="found", appointments=[appointment()]
            )
            old_ref = owner.appointments()[0]["appointmentRef"]
            slot_ref = await self.slots(owner, rescheduleAppointmentRef=old_ref)
            args = {
                "oldAppointmentRef": old_ref,
                "appointmentSlotRef": slot_ref,
                "appointmentReason": "Annual medical follow up",
                "readBack": True,
            }
            unconfirmed = await self.tool(
                owner, "reschedule_appointment", **{**args, "readBack": None}
            )
            self.assertTrue(unconfirmed.startswith("needs_input: Confirm"))
            self.assertEqual(len(requests), 1)
            self.assertEqual([a.id for a in owner.state.chart().appointments], [77])
            result = await self.tool(owner, "reschedule_appointment", **args)
            if status == "cancelled":
                self.assertIn("Your new appointment is booked for", result)
                self.assertIn(
                    f"Your old appointment on {appointment()['date']} at {appointment()['time']} is cancelled.",
                    result,
                )
                self.assertIn("Tell the caller both outcomes.", result)
            else:
                self.assertIn("new appointment is booked", result)
                self.assertIn(
                    "requires staff reconciliation. Do not book again.", result
                )
            self.assertEqual(
                result.split(":", 1)[0],
                "success" if status == "cancelled" else "blocked",
            )
            self.assertEqual(
                (await self.tool(owner, "reschedule_appointment", **args)).split(
                    ":", 1
                )[0],
                result.split(":", 1)[0],
            )
            checkpoints = [
                c.args[0] for c in owner.state.reporter.appointment.call_args_list
            ]
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(
                checkpoints[0]["cancellationResult"]["status"],
                "cancelled" if status == "cancelled" else "uncertain",
            )
            self.assertTrue(
                all(c["externalPatientId"] == "chart-jane" for c in checkpoints)
            )
            self.assertTrue(all(c["action"] == "RESCHEDULED" for c in checkpoints))
            self.assertNotIn("private-reschedule", json.dumps(checkpoints))
            self.assertEqual(requests[1][1]["rescheduleToken"], "private-reschedule")
            self.assertNotIn("appointmentTypeId", requests[1][1])
            self.assertEqual(
                [r[0] for r in requests],
                [
                    "/api/scheduler/slots",
                    "/api/appointment/reschedule",
                ],
            )
            self.assertEqual(
                [a.id for a in owner.state.chart().appointments],
                [888] if status == "cancelled" else [77, 888],
            )

    async def test_second_move_with_stale_slot_ref_does_not_replay_first_move(self):
        owner, requests = self.owner(
            [inventory(slots=[slot(), slot(start="2026-09-15T14:00")]), rescheduled()]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        old_ref = owner.appointments()[0]["appointmentRef"]
        listing = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            rescheduleAppointmentRef=old_ref,
        )
        first_slot, second_slot = soonest(listing)[:2]
        args = {
            "oldAppointmentRef": old_ref,
            "appointmentSlotRef": first_slot,
            "appointmentReason": "Annual follow up",
            "readBack": True,
        }
        moved = await self.tool(owner, "reschedule_appointment", **args)
        self.assertTrue(moved.startswith("success:"), moved)

        stale = await self.tool(
            owner,
            "reschedule_appointment",
            **{**args, "appointmentSlotRef": second_slot},
        )
        self.assertTrue(stale.startswith("needs_input:"), stale)
        self.assertIn("has already moved", stale)
        self.assertEqual(
            await self.tool(owner, "reschedule_appointment", **args), moved
        )
        self.assertEqual(len(requests), 2)

    async def test_metadata_authority_and_missing_action_tokens(self):
        owner, requests = self.owner([])
        verified(
            owner.state,
            appointmentsStatus="found",
            appointments=[
                appointment(
                    appointmentTypeId=99999,
                    visitType="routine_vision",
                    facility="Spring Hill",
                    officeId="crystal_river",
                    cancellationToken=None,
                )
            ],
        )
        entry = owner.appointments()[0]
        self.assertEqual(entry["visitType"], "routine_vision")
        result = await self.tool(
            owner,
            "cancel_appointment",
            appointmentRef=entry["appointmentRef"],
            readBack=True,
        )
        self.assertIn("Reload appointments", result)
        self.assertEqual(requests, [])

    async def test_missing_action_tokens_force_real_patient_reload(self):
        for field in ("cancellationToken",):
            with self.subTest(field=field):
                owner, requests = self.owner([inventory(), booking(88)])
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment(**{field: None})],
                )
                old_ref = owner.appointments()[0]["appointmentRef"]
                result = await self.tool(
                    owner,
                    "cancel_appointment",
                    appointmentRef=old_ref,
                    readBack=True,
                )
                self.assertIn("Reload appointments", result)
                self.assertEqual(owner.state.chart().appointmentsStatus, "error")
                self.assertFalse(
                    any(path.startswith("/api/appointment/") for path, _, _ in requests)
                )

                booked = await self.book(owner, await self.slots(owner))
                self.assertTrue(booked.startswith("success:"), booked)
                self.assertEqual(owner.state.chart().appointmentsStatus, "error")
                self.assertIn(88, [a.id for a in owner.state.chart().appointments])
                middleware = AsyncMock()
                middleware.resolve.return_value = Receipt.model_validate(
                    receipt(
                        appointmentsStatus="found",
                        appointments=[appointment()],
                    )
                )
                resolver = PatientResolver(owner.state, middleware)
                self.addAsyncCleanup(resolver.aclose)
                resolved = await resolver.resolve("Jane", None)
                self.assertEqual(resolved["outcome"], "verified")
                middleware.resolve.assert_awaited_once()
                self.assertTrue(getattr(owner.state.chart().appointments[0], field))
                self.assertEqual(owner.state.chart().appointmentsStatus, "found")
                owner.appointments()
                self.assertEqual(
                    {a.id for a in owner.state.chart().appointments}, {77, 88}
                )

    async def test_invalid_reschedule_receipt_is_uncertain_and_never_retried(self):
        for response in (
            {"status": "completed", "booking": booking()},
            {
                "status": "completed",
                "booking": booking(),
                "cancellation": {"status": "cancelled", "appointmentId": 888},
            },
            {"status": "partial"},
            {"status": "uncertain"},
            {"status": "uncertain", "booking": booking(999)},
        ):
            owner, requests = self.owner([inventory(), response])
            verified(
                owner.state, appointmentsStatus="found", appointments=[appointment()]
            )
            old_ref = owner.appointments()[0]["appointmentRef"]
            ref = await self.slots(owner, rescheduleAppointmentRef=old_ref)
            args = dict(
                oldAppointmentRef=old_ref,
                appointmentSlotRef=ref,
                appointmentReason="Follow up",
                readBack=True,
            )
            result = await self.tool(owner, "reschedule_appointment", **args)
            self.assertIn("could not be confirmed", result)
            self.assertEqual(owner.state.chart().appointmentsStatus, "found")
            self.assertEqual(
                await self.tool(
                    owner, "cancel_appointment", appointmentRef=old_ref, readBack=True
                ),
                result,
            )
            self.assertEqual(
                await self.tool(owner, "reschedule_appointment", **args), result
            )
            self.assertEqual([a.id for a in owner.state.chart().appointments], [77])
            self.assertEqual(len(requests), 2)

    async def test_unknown_appointment_type_does_not_guess_reschedule_visit(self):
        for type_id in (None, 99999):
            owner, requests = self.owner([{"status": "cancelled", "appointmentId": 77}])
            verified(
                owner.state,
                appointmentsStatus="found",
                appointments=[appointment(appointmentTypeId=type_id, visitType=None)],
            )
            old = owner.appointments()[0]
            self.assertIsNone(old["visitType"])
            result = await self.tool(
                owner,
                "list_available_appointments",
                visitType="medical",
                rescheduleAppointmentRef=old["appointmentRef"],
            )
            self.assertTrue(result.startswith("needs_input:"), result)
            self.assertIn("staff request", result)
            self.assertEqual(requests, [])
            result = await self.tool(
                owner,
                "cancel_appointment",
                appointmentRef=old["appointmentRef"],
                readBack=True,
            )
            self.assertEqual(result.split(":", 1)[0], "success")

    async def test_rejected_reschedule_reloads_appointments_and_tokens(self):
        for rejection, unchanged in (
            ("invalid_reschedule_token", True),
            ("ownership_mismatch", False),
            ("provider_rejected", True),
            ("write_failed", False),
        ):
            with self.subTest(rejection=rejection):
                owner, requests = self.owner(
                    [
                        inventory(),
                        {"status": "failed", "outcome": rejection},
                        receipt(
                            appointmentsStatus="found",
                            appointments=[
                                appointment(
                                    rescheduleToken="refreshed-reschedule",
                                    cancellationToken="refreshed-cancel",
                                )
                            ],
                        ),
                    ]
                )
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                resolver = PatientResolver(
                    owner.state, PatientMiddleware(owner.http._client, CONFIG)
                )
                self.addAsyncCleanup(resolver.aclose)
                agent = AbitaAgent(
                    call_owners(SPRING_HILL, resolver=resolver, scheduling=owner)
                )
                resolve_patient = ToolContext(agent.tools).function_tools[
                    "resolve_patient"
                ]
                result = await self.tool(
                    owner,
                    "reschedule_appointment",
                    oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
                    appointmentSlotRef=await self.move_slot(owner),
                    appointmentReason="Follow up",
                    readBack=True,
                )
                self.assertEqual("was not changed" in result, unchanged, result)
                self.assertEqual(owner.state.chart().appointmentsStatus, "error")
                await resolve_patient(speech_context(owner.state), "Jane", None)
                self.assertEqual(len(requests), 3)
                self.assertEqual(requests[-1][1]["patientId"], "chart-jane")
                patient = owner.state.chart()
                self.assertEqual(patient.appointmentsStatus, "found")
                self.assertEqual(
                    patient.appointments[0].rescheduleToken, "refreshed-reschedule"
                )
                self.assertEqual(
                    patient.appointments[0].cancellationToken, "refreshed-cancel"
                )
                unconfirmed = await self.tool(
                    owner,
                    "cancel_appointment",
                    appointmentRef=owner.appointments()[0]["appointmentRef"],
                    readBack=None,
                )
                self.assertTrue(
                    unconfirmed.startswith("needs_input: Confirm cancellation"),
                    unconfirmed,
                )
                self.assertEqual(len(requests), 3)

    async def test_a_refused_write_revokes_the_chart_when_only_insurance_moved(self):
        async def insurance_moved(request):
            coverage_written(owner.state, "Aetna", "car1")
            return httpx.Response(
                200,
                json={
                    "status": "error",
                    "outcome": "provider_rejected",
                    "message": "AdvancedMD rejected the cancellation. Please load appointments again or contact the office.",
                },
            )

        owner, requests = self.owner([insurance_moved])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = owner.appointments()[0]["appointmentRef"]

        await self.tool(owner, "cancel_appointment", appointmentRef=ref, readBack=True)

        self.assertEqual(owner.state.chart().appointmentsStatus, "error")
        repeat = await self.tool(
            owner, "cancel_appointment", appointmentRef=ref, readBack=True
        )
        self.assertTrue(repeat.startswith("needs_input: Choose"), repeat)
        self.assertEqual(len(requests), 1)

    async def test_rejected_reschedule_does_not_stale_a_new_patient_context(self):
        async def switched(request):
            verified(
                owner.state,
                "chart-john",
                appointmentsStatus="found",
                appointments=[appointment(id=99)],
            )
            return httpx.Response(
                200, json={"status": "failed", "outcome": "invalid_reschedule_token"}
            )

        owner, _ = self.owner([inventory(), switched])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
            appointmentSlotRef=await self.move_slot(owner),
            appointmentReason="Follow up",
            readBack=True,
        )
        self.assertEqual(owner.state.chart().patientId, "chart-john")
        self.assertEqual(owner.state.chart().appointmentsStatus, "found")
        self.assertEqual([a.id for a in owner.state.chart().appointments], [99])

    async def test_reschedule_retries_only_after_definite_failure_and_fresh_confirmation(
        self,
    ):
        owner, requests = self.owner(
            [
                inventory(),
                {"status": "failed", "outcome": "write_failed"},
                inventory(),
                rescheduled(),
            ]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        old_ref = owner.appointments()[0]["appointmentRef"]
        slot_ref = await self.slots(owner, rescheduleAppointmentRef=old_ref)
        args = dict(
            oldAppointmentRef=old_ref,
            appointmentSlotRef=slot_ref,
            appointmentReason="Follow up",
            readBack=True,
        )
        self.assertIn(
            "reschedule failed",
            await self.tool(owner, "reschedule_appointment", **args),
        )
        self.assertTrue(
            (await self.tool(owner, "reschedule_appointment", **args)).startswith(
                "needs_input:"
            )
        )
        self.assertEqual(len(requests), 2)
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        args["appointmentSlotRef"] = await self.slots(
            owner, rescheduleAppointmentRef=old_ref
        )
        self.assertTrue(
            (
                await self.tool(
                    owner, "reschedule_appointment", **{**args, "readBack": None}
                )
            ).startswith("needs_input: Confirm")
        )
        self.assertEqual(len(requests), 3)
        self.assertTrue(
            (await self.tool(owner, "reschedule_appointment", **args)).startswith(
                "success:"
            )
        )
        self.assertEqual([a.id for a in owner.state.chart().appointments], [888])
        self.assertEqual(len(requests), 4)

    async def test_reschedule_transport_failure_fences_further_writes(self):
        async def timed_out(request):
            raise httpx.ReadTimeout("response lost", request=request)

        owner, requests = self.owner([inventory(), timed_out, inventory()])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        old_ref = owner.appointments()[0]["appointmentRef"]
        slot_ref = await self.slots(owner, rescheduleAppointmentRef=old_ref)
        args = dict(
            oldAppointmentRef=old_ref,
            appointmentSlotRef=slot_ref,
            appointmentReason="Follow up",
            readBack=True,
        )
        result = await self.tool(owner, "reschedule_appointment", **args)
        self.assertIn("could not be confirmed", result)
        self.assertEqual(
            await self.tool(owner, "reschedule_appointment", **args), result
        )
        self.assertEqual(len(requests), 2)
        fresh_slot = await self.slots(owner)
        self.assertIn("could not be confirmed", await self.book(owner, fresh_slot))
        self.assertEqual(
            await self.tool(
                owner, "cancel_appointment", appointmentRef=old_ref, readBack=True
            ),
            result,
        )
        self.assertEqual(len(requests), 3)
        self.assertEqual([a.id for a in owner.state.chart().appointments], [77])

    async def test_lost_reschedule_response_reports_cancellation_uncertain(self):
        async def unavailable(request):
            return httpx.Response(503)

        async def timed_out(request):
            raise httpx.ReadTimeout("response lost", request=request)

        unconfirmed = "blocked: The reschedule outcome could not be confirmed."
        cases = {
            "503": (unavailable, unconfirmed, "uncertain"),
            "timeout": (timed_out, unconfirmed, "uncertain"),
            "uncertain receipt": (
                {"status": "uncertain"},
                unconfirmed,
                "not_attempted",
            ),
            "failed receipt": (
                {"status": "failed", "outcome": "write_failed"},
                "blocked: The reschedule failed.",
                "not_attempted",
            ),
        }
        for case, (response, answer, cancellation) in cases.items():
            with self.subTest(case=case):
                owner, requests = self.owner([inventory(), response])
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                result = await self.tool(
                    owner,
                    "reschedule_appointment",
                    oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
                    appointmentSlotRef=await self.move_slot(owner),
                    appointmentReason="Follow up",
                    readBack=True,
                )
                self.assertEqual(requests[-1][0], "/api/appointment/reschedule")
                self.assertTrue(result.startswith(answer), result)
                evidence = owner.state.reporter.appointment.call_args.args[0]
                self.assertEqual(evidence["cancellationResult"]["status"], cancellation)

    async def test_closed_owner_cannot_start_new_reads_or_writes(self):
        owner, requests = self.owner([])
        await owner.aclose()
        self.assertEqual(
            (await owner.availability("medical", PLAN))["outcome"], "unavailable"
        )
        self.assertEqual((await self.book(owner, "S1")).split(":", 1)[0], "blocked")
        self.assertEqual(requests, [])

    async def test_expired_arrival_refreshes_once_and_empty_reads_cache(self):
        expired = inventory(bookingTokenExpiresAt="2026-09-14T16:00:00Z")
        owner, requests = self.owner([expired, inventory()])
        await self.slots(owner)
        self.assertEqual(len(requests), 2)
        owner, requests = self.owner([expired, expired])
        self.assertEqual(
            (await owner.availability("medical", PLAN))["outcome"],
            "availability_failed",
        )
        self.assertEqual(
            (await owner.availability("medical", PLAN))["outcome"],
            "availability_failed",
        )
        self.assertEqual(len(requests), 2)
        owner, requests = self.owner([inventory(outcome="no_availability", slots=[])])
        self.assertEqual((await owner.availability("medical", PLAN))["outcome"], "none")
        self.assertEqual((await owner.availability("medical", PLAN))["outcome"], "none")
        self.assertEqual(len(requests), 1)

    async def test_reschedule_patient_switch_preserves_middleware_result_for_original_patient(
        self,
    ):
        async def switched(request):
            verified(owner.state, "chart-john")
            return httpx.Response(200, json=rescheduled())

        owner, requests = self.owner([inventory(), switched])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = await self.move_slot(owner)
        result = await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
            appointmentSlotRef=ref,
            appointmentReason="Annual medical follow up",
            readBack=True,
        )
        self.assertEqual(result.split(":", 1)[0], "success")
        self.assertEqual(owner.state.chart().appointments, [])
        evidence = owner.state.reporter.appointment.call_args.args[0]
        self.assertEqual(evidence["externalPatientId"], "chart-jane")
        self.assertEqual(evidence["cancellationResult"]["status"], "cancelled")
        self.assertEqual(
            owner.state.reporter.appointment.call_args.kwargs["call_id"],
            "native-call-id",
        )
        self.assertEqual(len(requests), 2)
        verified(owner.state)
        owner.appointments()
        self.assertEqual([a.id for a in owner.state.chart().appointments], [888])
        self.assertIn("Your new appointment is booked", await self.book(owner, ref))
        self.assertEqual(len(requests), 2)

    async def test_confirmed_changes_write_while_the_agent_is_still_speaking(self):
        for move in (False, True):
            with self.subTest(reschedule=move):
                owner, requests = self.owner(
                    [inventory(), rescheduled() if move else booking()]
                )
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                ref = await (self.move_slot(owner) if move else self.slots(owner))
                context = speech_context(owner.state)

                async def speaking_forever():
                    await asyncio.Event().wait()

                context.wait_for_playout.side_effect = speaking_forever
                args = dict(
                    appointmentSlotRef=ref,
                    appointmentReason="Eye exam",
                    readBack=True,
                )
                if move:
                    args["oldAppointmentRef"] = owner.appointments()[0][
                        "appointmentRef"
                    ]
                else:
                    args["referringDoctor"] = None
                action = "reschedule" if move else "book"
                result = await asyncio.wait_for(
                    getattr(SchedulingTools(owner), action + "_appointment")(
                        context, **args
                    ),
                    1,
                )
                self.assertTrue(result.startswith("success:"), result)
                self.assertEqual(len(requests), 2)
                context.wait_for_playout.assert_not_awaited()
                context.session.generate_reply.assert_not_called()

    async def test_unknown_slot_ref_keeps_offered_slots_and_cache(self):
        owner, requests = self.owner([inventory(), booking()])
        ref = await self.slots(owner)
        self.assertTrue((await self.book(owner, "ST_TYPO")).startswith("needs_input:"))
        await self.slots(owner)
        self.assertEqual(len(requests), 1)
        self.assertTrue((await self.book(owner, ref)).startswith("success:"))

    async def test_changing_plan_retires_offered_slots_before_booking(self):
        owner, requests = self.owner([inventory(), inventory()])
        ref = await self.slots(owner)
        self.assertEqual(await self.slots(owner), ref)
        await self.slots(owner, insurancePlanId="aetna")
        self.assertEqual(
            [body["insurancePlanId"] for _, body, _ in requests], [PLAN, "aetna"]
        )
        rejected = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            insurancePlanId="vsp",
        )
        self.assertTrue(rejected.startswith("needs_input: "), rejected)
        stale = await self.book(owner, ref)
        self.assertTrue(stale.startswith("needs_input:"), stale)
        self.assertEqual(len(requests), 2)

    async def test_uncleared_plan_blocks_with_the_decision_and_offers_nothing(self):
        uncleared = decision(
            "Test Insurance",
            outcome="needs_staff_task",
            canSchedule=False,
            reason="requirement",
            requirements=[dict(kind="pcp_referral", verification="unverified")],
            planId=PLAN,
        )
        owner, requests = self.owner(
            [
                inventory(),
                {
                    "status": "success",
                    "outcome": "insurance_not_cleared",
                    "availabilityFound": False,
                    "slots": [],
                    "message": "Backend wording.",
                    "insuranceDecision": uncleared,
                },
            ]
        )
        ref = await self.slots(owner)
        result = await self.tool(
            owner,
            "list_available_appointments",
            visitType="medical",
            startDate="2026-09-16",
        )
        self.assertEqual(
            result,
            "blocked: Test Insurance is accepted for medical visits. Tell the caller"
            " they need a referral from their primary care doctor before scheduling."
            " Offer a staff request or transfer; do not schedule.",
        )
        self.assertFalse((await self.book(owner, ref)).startswith("success:"))
        self.assertEqual(len(requests), 2)

    async def test_uncleared_result_must_carry_a_matching_decision(self):
        uncleared = dict(
            outcome="needs_staff_task", canSchedule=False, reason="pending_confirmation"
        )
        for body in (
            {"insuranceDecision": None},
            {"insuranceDecision": decision("Aetna", **uncleared)},
            {"insuranceDecision": decision("VSP", "routine_vision", **uncleared)},
            {"insuranceDecision": decision("Test Insurance")},
        ):
            with self.subTest(body=body):
                owner, _ = self.owner(
                    [
                        {
                            "status": "success",
                            "outcome": "insurance_not_cleared",
                            "slots": [],
                        }
                        | body
                    ]
                )
                result = await self.tool(
                    owner, "list_available_appointments", visitType="medical"
                )
                self.assertTrue(
                    result.startswith("blocked: Availability could not be verified"),
                    result,
                )

    async def write_effects(self, owner, write, sent):
        first = await write()
        replays = await write() == first and sent.await_count == 1
        later = await self.book(owner, "ST_TYPO")
        blocks = not later.startswith(
            "needs_input: That slot reference was not returned"
        )
        stale = owner.state.chart().appointmentsStatus == "error"
        return first, (replays, blocks, stale)

    async def test_every_booking_outcome_has_one_answer_and_effect(self):
        rejected = "needs_input: That slot could not be booked."
        failed = "blocked: The booking failed."
        cases = {
            "written": (
                WriteReceipt(status="booked", appointmentId=888),
                "booked",
                "success: Booked 2026-09-15 at 9:00 AM",
                (True, False, False),
            ),
            "refused": (
                WriteReceipt(status="error", outcome="slot_unavailable"),
                "rejected",
                rejected,
                (False, False, False),
            ),
            "expired": (
                WriteReceipt(status="error", outcome="invalid_booking_token"),
                "rejected",
                rejected,
                (False, False, False),
            ),
            "failed": (
                WriteReceipt(status="error", outcome="write_failed"),
                "failed",
                failed,
                (False, False, False),
            ),
            "unsent": (
                SchedulingFailure(reason="not_configured"),
                "failed",
                failed,
                (False, False, False),
            ),
            "needs_input": (
                WriteReceipt(
                    status="error",
                    outcome="appointment_type_unresolved",
                    message="Post-op appointments must use a medical scheduling lane.",
                ),
                "needs_input",
                "needs_input: That slot could not be booked. Post-op appointments",
                (False, False, False),
            ),
            "uncertain": (
                WriteReceipt(status="error", outcome="indeterminate_write"),
                "uncertain",
                "blocked: The booking outcome could not be confirmed.",
                (True, True, False),
            ),
        }
        self.assertEqual(set(cases), set(get_args(BookOutcome)))
        for outcome, (response, reported, answer, effects) in cases.items():
            with self.subTest(outcome=outcome):
                owner, _ = self.owner([inventory()])
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                owner.http.book = AsyncMock(return_value=response)
                ref = await self.slots(owner)
                first, observed = await self.write_effects(
                    owner, lambda: self.book(owner, ref), owner.http.book
                )
                self.assertTrue(first.startswith(answer), first)
                evidence = owner.state.reporter.appointment.call_args.args[0]
                self.assertEqual(evidence["bookingResult"]["status"], reported)
                self.assertEqual(observed, effects)

    async def test_every_cancellation_outcome_has_one_answer_and_effect(self):
        failed = "blocked: The cancellation failed."
        cases = {
            "written": (
                WriteReceipt(status="cancelled", appointmentId=77),
                "cancelled",
                "success: The selected appointment was cancelled.",
                (True, False, False),
            ),
            "expired": (
                WriteReceipt(status="error", outcome="invalid_cancellation_token"),
                "rejected",
                "needs_input: The appointment details expired.",
                (False, False, True),
            ),
            "refused": (
                WriteReceipt(status="error", outcome="provider_rejected"),
                "rejected",
                "needs_input: Cancellation was not completed.",
                (False, False, True),
            ),
            "failed": (
                WriteReceipt(status="error", outcome="write_failed"),
                "failed",
                failed,
                (False, False, True),
            ),
            "unsent": (
                SchedulingFailure(reason="not_configured"),
                "failed",
                failed,
                (False, False, False),
            ),
            "uncertain": (
                WriteReceipt(status="error", outcome="indeterminate_write"),
                "uncertain",
                "blocked: The cancellation outcome could not be confirmed.",
                (True, True, False),
            ),
        }
        self.assertEqual(set(cases), set(get_args(CancelOutcome)))
        for outcome, (response, reported, answer, effects) in cases.items():
            with self.subTest(outcome=outcome):
                owner, _ = self.owner([])
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                owner.http.cancel = AsyncMock(return_value=response)
                ref = owner.appointments()[0]["appointmentRef"]
                first, observed = await self.write_effects(
                    owner,
                    lambda: self.tool(
                        owner, "cancel_appointment", appointmentRef=ref, readBack=True
                    ),
                    owner.http.cancel,
                )
                self.assertTrue(first.startswith(answer), first)
                evidence = owner.state.reporter.appointment.call_args.args[0]
                self.assertEqual(evidence["cancellationResult"]["status"], reported)
                self.assertEqual(observed, effects)

    async def test_every_reschedule_outcome_has_one_answer_and_effect(self):
        cases = {
            "written": (
                RescheduleReceipt.model_validate(rescheduled()),
                ("booked", "cancelled"),
                "success: Your new appointment is booked for 2026-09-15",
                (True, False, False),
            ),
            "partial": (
                RescheduleReceipt.model_validate(rescheduled("error")),
                ("booked", "uncertain"),
                "blocked: The new appointment is booked for 2026-09-15",
                (True, True, False),
            ),
            "refused": (
                RescheduleReceipt(status="failed", outcome="provider_rejected"),
                ("failed", "not_attempted"),
                "needs_input: The reschedule was refused. Your old appointment on 2026-09-20 at 10:00 AM was not changed.",
                (False, False, True),
            ),
            "expired": (
                RescheduleReceipt(status="failed", outcome="invalid_reschedule_token"),
                ("failed", "not_attempted"),
                "needs_input: The appointment details expired. Your old appointment on 2026-09-20 at 10:00 AM was not changed.",
                (False, False, True),
            ),
            "failed": (
                RescheduleReceipt(status="failed", outcome="write_failed"),
                ("failed", "not_attempted"),
                "blocked: The reschedule failed. Resolve the patient again",
                (False, False, True),
            ),
            "uncertain": (
                RescheduleReceipt(status="uncertain"),
                ("uncertain", "not_attempted"),
                "blocked: The reschedule outcome could not be confirmed.",
                (True, True, False),
            ),
        }
        self.assertEqual(set(cases), set(get_args(MoveOutcome)))
        for outcome, (response, reported, answer, effects) in cases.items():
            with self.subTest(outcome=outcome):
                owner, _ = self.owner([inventory()])
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                owner.http.reschedule = AsyncMock(return_value=response)
                args = dict(
                    oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
                    appointmentSlotRef=await self.move_slot(owner),
                    appointmentReason="Follow up",
                    readBack=True,
                )
                first, observed = await self.write_effects(
                    owner,
                    lambda: self.tool(owner, "reschedule_appointment", **args),
                    owner.http.reschedule,
                )
                self.assertTrue(first.startswith(answer), first)
                if reported[0] == "booked":
                    self.assertIn("for Doe, Jane", first)
                evidence = owner.state.reporter.appointment.call_args.args[0]
                self.assertEqual(
                    (
                        evidence["bookingResult"]["status"],
                        evidence["cancellationResult"]["status"],
                    ),
                    reported,
                )
                self.assertEqual(observed, effects)

    async def test_recorded_appointment_outcome_is_the_outcome_told_to_the_agent(self):
        class Recorder(CallReporter):
            def __init__(self):
                self._base = {}
                self.outcomes = []

            def record(self, kind, evidence, *, call_id=None):
                self.outcomes.append(evidence["outcome"])

            def _queue(self, payload):
                pass

        cases = {
            "booking": (
                "book",
                WriteReceipt(status="booked", appointmentId=888),
                "booked",
            ),
            "cancellation": (
                "cancel",
                WriteReceipt(status="cancelled", appointmentId=77),
                "cancelled",
            ),
            "completed reschedule": (
                "reschedule",
                RescheduleReceipt.model_validate(rescheduled()),
                "rescheduled",
            ),
            "partial reschedule": (
                "reschedule",
                RescheduleReceipt.model_validate(rescheduled("error")),
                "partial_reschedule",
            ),
            "failed reschedule": (
                "reschedule",
                RescheduleReceipt(status="failed", outcome="write_failed"),
                "failed",
            ),
            "uncertain reschedule": (
                "reschedule",
                RescheduleReceipt(status="uncertain"),
                "uncertain",
            ),
            "lost reschedule response": (
                "reschedule",
                SchedulingFailure(reason="transport_error", uncertain=True),
                "uncertain",
            ),
        }
        for case, (write, response, outcome) in cases.items():
            with self.subTest(case=case):
                owner, _ = self.owner([inventory()])
                owner.state.reporter = reporter = Recorder()
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                setattr(owner.http, write, AsyncMock(return_value=response))
                old_ref = owner.appointments()[0]["appointmentRef"]
                if write == "cancel":
                    await self.tool(
                        owner,
                        "cancel_appointment",
                        appointmentRef=old_ref,
                        readBack=True,
                    )
                elif write == "book":
                    await self.book(owner, await self.slots(owner))
                else:
                    await self.tool(
                        owner,
                        "reschedule_appointment",
                        oldAppointmentRef=old_ref,
                        appointmentSlotRef=await self.slots(
                            owner, rescheduleAppointmentRef=old_ref
                        ),
                        appointmentReason="Follow up",
                        readBack=True,
                    )
                self.assertEqual(owner._writes._last_sent.result()["outcome"], outcome)
                self.assertEqual(reporter.outcomes, [outcome])

    async def test_every_middleware_outcome_is_classified_for_each_write(self):
        errors = {
            "validation": ("rejected", "rejected", "failed"),
            "invalid_booking_token": ("rejected", "uncertain", "failed"),
            "invalid_cancellation_token": ("uncertain", "rejected", "uncertain"),
            "invalid_reschedule_token": ("rejected", "uncertain", "failed"),
            "booking_token_required": ("rejected", "uncertain", "failed"),
            "appointment_type_unresolved": ("needs_input", "uncertain", "failed"),
            "patient_context_mismatch": ("rejected", "uncertain", "failed"),
            "slot_unavailable": ("rejected", "uncertain", "failed"),
            "provider_conflict": ("rejected", "rejected", "failed"),
            "provider_rejected": ("rejected", "rejected", "failed"),
            "ownership_mismatch": ("uncertain", "rejected", "failed"),
            "write_failed": ("failed", "failed", "failed"),
            "indeterminate_write": ("uncertain", "uncertain", "uncertain"),
            None: ("rejected", "rejected", "failed"),
        }
        self.assertEqual(set(errors), {*get_args(SchedulingOutcome), None})
        self.assertEqual(set(errors), set(REFUSALS))
        cases = [
            (WriteReceipt(status="error", outcome=outcome), expected[:2])
            for outcome, expected in errors.items()
        ] + [
            (
                WriteReceipt(status="booked", outcome="slot_unavailable"),
                ("rejected", "uncertain"),
            ),
            (WriteReceipt(status="booked"), ("uncertain", "uncertain")),
            (
                WriteReceipt(status="booked", outcome="validation"),
                ("uncertain", "uncertain"),
            ),
            (
                WriteReceipt(status="cancelled", outcome="write_failed"),
                ("uncertain", "uncertain"),
            ),
            (
                WriteReceipt(
                    status="cancelled", appointmentId=999, outcome="provider_rejected"
                ),
                ("rejected", "uncertain"),
            ),
        ]
        for response, expected in cases:
            with self.subTest(response=response.model_dump(exclude_none=True)):
                booking_owner, _ = self.owner([inventory()])
                booking_owner.http.book = AsyncMock(return_value=response)
                await self.book(booking_owner, await self.slots(booking_owner))
                cancel_owner, _ = self.owner([])
                verified(
                    cancel_owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                cancel_owner.http.cancel = AsyncMock(return_value=response)
                await self.tool(
                    cancel_owner,
                    "cancel_appointment",
                    appointmentRef=cancel_owner.appointments()[0]["appointmentRef"],
                    readBack=True,
                )
                self.assertEqual(
                    (
                        booking_owner.state.reporter.appointment.call_args.args[0][
                            "bookingResult"
                        ]["status"],
                        cancel_owner.state.reporter.appointment.call_args.args[0][
                            "cancellationResult"
                        ]["status"],
                    ),
                    expected,
                )
        for outcome, (_, _, moved) in errors.items():
            with self.subTest(move=outcome):
                owner, _ = self.owner([inventory()])
                verified(
                    owner.state,
                    appointmentsStatus="found",
                    appointments=[appointment()],
                )
                owner.http.reschedule = AsyncMock(
                    return_value=RescheduleReceipt(status="failed", outcome=outcome)
                )
                await self.tool(
                    owner,
                    "reschedule_appointment",
                    oldAppointmentRef=owner.appointments()[0]["appointmentRef"],
                    appointmentSlotRef=await self.move_slot(owner),
                    readBack=True,
                )
                self.assertEqual(
                    owner.state.reporter.appointment.call_args.args[0]["bookingResult"][
                        "status"
                    ],
                    moved,
                )

    def test_schema_has_no_patient_ids_or_tokens(self):
        owner, _ = self.owner([])
        for tool in llm.find_function_tools(SchedulingTools(owner)):
            schema = build_strict_openai_schema(tool)
            serialized = json.dumps(schema)
            self.assertNotIn("hospitalName", serialized)
            self.assertNotIn("hospitalDate", serialized)
            for private in (
                "patientId",
                "bookingToken",
                "cancellationToken",
                "columnId",
            ):
                self.assertNotIn(private, serialized)


class SchedulingModel(llm.LLM):
    def __init__(self):
        super().__init__()
        self.requests = []

    def chat(self, *, chat_ctx, tools=None, conn_options, **kwargs):
        self.requests.append(chat_ctx.copy())
        stream = SchedulingStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )
        return stream


class SchedulingStream(llm.LLMStream):
    async def _run(self):
        items = self._chat_ctx.items
        last_user = max(
            i
            for i, item in enumerate(items)
            if item.type == "message" and item.role == "user"
        )
        outputs = [item for item in items if item.type == "function_call_output"]
        current = [
            item for item in items[last_user:] if item.type == "function_call_output"
        ]
        if current:
            delta = llm.ChoiceDelta(role="assistant", content=current[-1].output)
        else:
            user = items[last_user].text_content
            previous = outputs[-1].output if outputs else ""
            if user in ("search", "search to move"):
                existing = next(
                    (
                        item.output
                        for item in reversed(outputs)
                        if "Existing appointments:" in item.output
                    ),
                    "",
                )
                loaded = re.search(r"^(A[0-9]+):", existing, re.MULTILINE)
                name, args = (
                    "list_available_appointments",
                    {
                        "visitType": "medical",
                        "insurancePlanId": PLAN,
                        "startDate": None,
                        "office": None,
                        "rescheduleAppointmentRef": loaded[1]
                        if user == "search to move" and loaded
                        else None,
                    },
                )
            elif user == "cancel":
                name, args = (
                    "cancel_appointment",
                    {
                        "appointmentRef": re.findall(
                            r"^(A[0-9]+):", previous, re.MULTILINE
                        )[-1],
                        "readBack": True,
                    },
                )
            else:
                name = (
                    "reschedule_appointment" if user == "move" else "book_appointment"
                )
                args = {
                    "appointmentSlotRef": soonest(previous)[0],
                    "appointmentReason": "Annual medical follow up",
                    "readBack": True,
                }
                if user == "move":
                    existing = next(
                        item.output
                        for item in reversed(outputs)
                        if "Existing appointments:" in item.output
                    )
                    args["oldAppointmentRef"] = re.search(
                        r"^(A[0-9]+):", existing, re.MULTILINE
                    )[1]
                else:
                    args["referringDoctor"] = None
            delta = llm.ChoiceDelta(
                role="assistant",
                tool_calls=[
                    llm.FunctionToolCall(
                        name=name,
                        arguments=json.dumps(args),
                        call_id=f"schedule-{last_user}",
                    )
                ],
            )
        self._event_ch.send_nowait(llm.ChatChunk(id="offline-scheduling", delta=delta))


class SchedulingSessionTests(unittest.IsolatedAsyncioTestCase):
    owner = SchedulingTests.owner

    async def test_patient_correction_rejects_previous_slot_through_session(self):
        owner, requests = self.owner([inventory()])
        model = SchedulingModel()
        agent = AbitaAgent(call_owners(SPRING_HILL, scheduling=owner))
        async with AgentSession(llm=model, userdata=owner.state) as session:
            with patch.object(AbitaAgent, "on_enter", new=AsyncMock()):
                await session.start(agent=agent)
            await asyncio.wait_for(session.run(user_input="search"), 5)
            verified(owner.state, "chart-john")
            await asyncio.wait_for(session.run(user_input="book"), 5)
            outputs = [
                item
                for item in model.requests[-1].items
                if item.type == "function_call_output"
            ]
            self.assertEqual(outputs[-1].output.split(":", 1)[0], "needs_input")
        self.assertEqual(len(requests), 1)

    async def test_all_four_registered_tools_through_agent_session(self):
        owner, requests = self.owner(
            [
                inventory(),
                booking(888, "booked"),
                {"status": "cancelled", "appointmentId": 888},
                inventory(),
                rescheduled(appointment_id=999),
            ]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        model = SchedulingModel()
        agent = AbitaAgent(call_owners(SPRING_HILL, scheduling=owner))
        async with AgentSession(llm=model, userdata=owner.state) as session:
            with patch.object(AbitaAgent, "on_enter", new=AsyncMock()):
                await session.start(agent=agent)
            for user, expected in (
                ("search", "success"),
                ("book", "success"),
                ("cancel", "success"),
                ("search to move", "success"),
                ("move", "success"),
            ):
                await asyncio.wait_for(session.run(user_input=user), 5)
                outputs = [
                    item
                    for item in model.requests[-1].items
                    if item.type == "function_call_output"
                ]
                if user.startswith("search"):
                    self.assertTrue(
                        outputs[-1].output.startswith("success: "), outputs[-1].output
                    )
                else:
                    self.assertEqual(outputs[-1].output.split(":", 1)[0], expected)
                for output in outputs:
                    for secret in (
                        "private-signed-slot",
                        "private-cancel",
                        "private-reschedule",
                        "chart-jane",
                    ):
                        self.assertNotIn(secret, output.output)
        self.assertEqual(len(requests), 5)
        self.assertEqual([a.id for a in owner.state.chart().appointments], [999])
