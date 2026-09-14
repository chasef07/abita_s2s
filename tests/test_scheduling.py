"""Offline scheduling behavior against the actual HTTP envelopes and registered tools."""

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from livekit.agents import AgentSession, llm
from livekit.agents.llm.utils import build_strict_openai_schema
from test_patient_resolution import CONFIG, call_state, receipt

from abita_s2s.agent import AbitaAgent
from abita_s2s.insurance_state import AcceptedInsurance
from abita_s2s.middleware import Receipt
from abita_s2s.offices import SPRING_HILL, get_office_profile
from abita_s2s.scheduling import Scheduling
from abita_s2s.scheduling_http import SchedulingHTTP

NOW = datetime(2026, 9, 14, 17, tzinfo=UTC)


def inventory(**extra):
    return {
        "status": "success",
        "outcome": "availability_found",
        "slots": [
            {
                "provider": "Dr. Austin Bach",
                "date": "2026-09-15",
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
        "type": "Established Adult Medical",
        "cancellationToken": "private-cancel",
        "rescheduleToken": "private-reschedule",
        **extra,
    }


def verified(state, patient_id="chart-jane", visit="medical", **extra):
    state.patient.active = Receipt.model_validate(
        receipt(patient_id, routing="bach_only", preauthRequired=False, **extra)
    )
    state.insurance.accepted = AcceptedInsurance(
        state.call.called_office_key,
        state.patient.revision,
        patient_id,
        None,
        "Test Insurance",
        visit,
    )


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    def owner(self, responses, *, state=None):
        state = state or call_state(None)
        if state.patient.active is None:
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
        owner = Scheduling(state, SchedulingHTTP(client, CONFIG), now=lambda: NOW)
        self.addAsyncCleanup(owner.aclose)
        return owner, requests

    async def tool(self, owner, name, **args):
        return json.loads(
            await getattr(owner, name)(SimpleNamespace(userdata=owner.state), **args)
        )

    async def slots(self, owner, **args):
        result = await self.tool(
            owner, "list_available_appointments", visitType="medical", **args
        )
        self.assertEqual(result["outcome"], "found", result)
        return result["slots"][0]["appointmentSlotRef"]

    async def book(self, owner, ref, **extra):
        return await self.tool(
            owner,
            "book_appointment",
            appointmentSlotRef=ref,
            appointmentReason="Blurry vision in left eye since yesterday",
            referringDoctor="none",
            readBack=True,
            **extra,
        )

    async def test_availability_contract_cache_private_tokens_and_office(self):
        state = call_state(None, get_office_profile("sweetwater"))
        owner, requests = self.owner([inventory()], state=state)
        self.assertEqual(
            (
                await self.tool(
                    owner, "list_available_appointments", visitType="medical"
                )
            )["outcome"],
            "needs_input",
        )
        ref = await self.slots(owner, office="hollywood")
        self.assertEqual(await self.slots(owner, office="hollywood"), ref)
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0][1],
            {
                "office": "+19542872010",
                "startDate": "2026-09-15",
                "rangeDays": 14,
                "dob": "01/02/1980",
                "routing": "bach_only",
            },
        )
        self.assertEqual(requests[0][2]["authorization"], "test-auth")
        self.assertNotIn("private-signed-slot", json.dumps(owner._cache[2]))

    async def test_missing_prerequisites_and_office_care(self):
        owner, requests = self.owner([])
        for change in ("partial", "uncertain", "preauth", "acceptance", "patient"):
            verified(owner.state)
            owner.state.insurance.registrations.clear()
            owner.state.insurance.write_uncertain = False
            if change == "partial":
                owner.state.insurance.registrations["chart-jane"] = "partial"
            elif change == "uncertain":
                owner.state.insurance.write_uncertain = True
            elif change == "preauth":
                owner.state.patient.active = owner.state.patient.active.model_copy(
                    update={"preauthRequired": True}
                )
            elif change == "acceptance":
                owner.state.insurance.accepted = None
                owner.state.patient.active = owner.state.patient.active.model_copy(
                    update={"insuranceCarrier": None}
                )
            else:
                owner.state.patient.active = None
            self.assertEqual(
                (await owner.availability("medical"))["outcome"], "needs_input"
            )
        self.assertEqual(requests, [])
        for key, visit in (
            ("crystal-river", "routine_vision"),
            ("north-miami-beach-optical", "medical"),
        ):
            state = call_state(None, get_office_profile(key))
            owner, requests = self.owner([], state=state)
            self.assertEqual(
                (await owner.availability(visit))["outcome"], "unsupported"
            )

    async def test_eastern_date_and_correction_invalidates_slots(self):
        owner, requests = self.owner(
            [inventory(bookingTokenExpiresAt="2026-09-15T02:00:00Z")]
        )
        owner.now = lambda: datetime(
            2026, 9, 15, 1, tzinfo=UTC
        )  # Still Sep 14 Eastern.
        ref = await self.slots(owner)
        for start in ("2026-09-14", "20260916", "bad"):
            self.assertEqual(
                (await owner.availability("medical", start))["outcome"], "needs_input"
            )
        self.assertEqual((await self.book(owner, ref))["outcome"], "needs_input")
        self.assertEqual(len(requests), 1)

    async def test_shared_read_cancelled_waiter_and_patient_switch(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=inventory())

        owner, requests = self.owner([delayed])
        first = asyncio.create_task(owner.availability("medical"))
        await entered.wait()
        second = asyncio.create_task(owner.availability("medical"))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        owner.state.patient.revision += 1
        verified(owner.state, "chart-john")
        release.set()
        self.assertEqual((await second)["outcome"], "stale")
        self.assertEqual(owner._slots, {})
        self.assertEqual(len(requests), 1)

    async def test_last_cancelled_waiter_stops_read_and_allows_fresh_search(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        owner, requests = self.owner([delayed, inventory()])
        task = asyncio.create_task(owner.availability("medical"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await cancelled.wait()
        self.assertEqual(owner._slots, {})
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
        first = asyncio.create_task(owner.availability("medical"))
        await entered.wait()
        result = await owner.availability("medical", "2026-09-16")
        self.assertTrue(result["retry_same_search"])
        release.set()
        self.assertEqual((await first)["outcome"], "stale")
        self.assertFalse(
            (await owner.availability("medical", "2026-09-16"))["retry_same_search"]
        )
        self.assertEqual(
            (await owner.availability("medical", "2026-09-16"))["outcome"],
            "availability_failed",
        )
        self.assertFalse(owner._slots)
        self.assertEqual(len(requests), 3)

    async def test_booking_confirmation_receipt_duplicate_and_new_cancellation(self):
        owner, requests = self.owner(
            [
                inventory(),
                {"status": "partial", "appointmentId": 888, "appointmentTypeId": 1007},
                {"status": "cancelled"},
            ]
        )
        ref = await self.slots(owner)
        for reason, referrer, confirm in (
            ("eye exam", "none", True),
            ("Routine annual exam", "", True),
            ("Routine annual exam", "none", None),
        ):
            result = await self.tool(
                owner,
                "book_appointment",
                appointmentSlotRef=ref,
                appointmentReason=reason,
                referringDoctor=referrer,
                readBack=confirm,
            )
            self.assertIn(result["outcome"], ("needs_input", "needs_confirmation"))
        result = await self.book(owner, ref)
        self.assertEqual(result["outcome"], "partial_booking")
        self.assertIn("note did not save", result["answer"])
        self.assertEqual((await self.book(owner, ref))["outcome"], "partial_booking")
        body = requests[1][1]
        self.assertEqual(body["bookingToken"], "private-signed-slot")
        self.assertEqual(body["patientId"], "chart-jane")
        self.assertNotIn("office", body)
        self.assertEqual(body["patientStatus"], "established")
        cancel_ref = result["appointments"][0]["appointmentRef"]
        self.assertEqual(
            (await self.tool(owner, "cancel_appointment", appointmentRef=cancel_ref))[
                "outcome"
            ],
            "cancelled",
        )
        self.assertEqual(
            (await self.tool(owner, "cancel_appointment", appointmentRef=cancel_ref))[
                "outcome"
            ],
            "cancelled",
        )
        self.assertEqual(
            requests[-1][1],
            {"appointmentId": 888, "patientId": "chart-jane", "office": "+17275919997"},
        )
        self.assertEqual(owner.state.patient.active.appointments, [])
        self.assertEqual(len(requests), 3)

    async def test_expired_token_and_patient_context_round_trip(self):
        for mode in ("expiry", "switch"):
            owner, requests = self.owner([inventory()])
            ref = await self.slots(owner)
            if mode == "expiry":
                owner.now = lambda: NOW + timedelta(hours=2)
            else:
                owner.state.patient.revision += 2
                verified(owner.state)
            self.assertEqual((await self.book(owner, ref))["outcome"], "needs_input")
            self.assertEqual(len(requests), 1)

    async def test_uncertain_book_never_repeats_even_after_reload(self):
        for response in (
            {"status": "booked"},
            {"status": "error", "outcome": "write_ambiguous"},
            {"unexpected": True},
        ):
            owner, requests = self.owner([inventory(), response, inventory()])
            ref = await self.slots(owner)
            self.assertEqual((await self.book(owner, ref))["outcome"], "uncertain")
            ref = await self.slots(owner)
            self.assertEqual((await self.book(owner, ref))["outcome"], "uncertain")
            self.assertEqual(len(requests), 3)
            self.assertEqual(owner.state.patient.active.appointments, [])

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
        self.assertEqual((await self.book(owner, ref))["outcome"], "in_progress")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        owner.state.patient.revision += 1
        verified(owner.state, "chart-john")
        release.set()
        await asyncio.shield(owner._write_task)
        self.assertFalse(owner.state.patient.active.appointments)
        owner.state.patient.revision += 1
        verified(owner.state)
        self.assertEqual((await self.book(owner, ref))["outcome"], "booked")
        self.assertEqual(len(requests), 2)

    async def test_cancellation_exact_target_token_and_expired_details(self):
        owner, requests = self.owner(
            [{"status": "error", "outcome": "invalid_cancellation_token"}]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = owner.appointments()[0]["appointmentRef"]
        self.assertEqual(
            (await self.tool(owner, "cancel_appointment", appointmentRef="77"))[
                "outcome"
            ],
            "needs_input",
        )
        self.assertEqual(
            (await self.tool(owner, "cancel_appointment", appointmentRef=ref))[
                "outcome"
            ],
            "rejected",
        )
        self.assertEqual(requests[0][1], {"cancellationToken": "private-cancel"})
        self.assertEqual(owner.state.patient.active.appointmentsStatus, "error")

    async def test_reschedule_partial_and_success_never_rebooks(self):
        for status in ("cancelled", "error"):
            owner, requests = self.owner(
                [
                    inventory(),
                    {
                        "status": "booked",
                        "appointmentId": 888,
                        "appointmentTypeId": 1007,
                    },
                    {"status": status},
                ]
            )
            verified(
                owner.state, appointmentsStatus="found", appointments=[appointment()]
            )
            old_ref = owner.appointments()[0]["appointmentRef"]
            slot_ref = await self.slots(owner)
            args = {
                "oldAppointmentRef": old_ref,
                "appointmentSlotRef": slot_ref,
                "appointmentReason": "Annual medical follow up",
                "referringDoctor": "Dr. Test",
                "readBack": True,
            }
            result = await self.tool(owner, "reschedule_appointment", **args)
            self.assertEqual(
                result["outcome"],
                "rescheduled" if status == "cancelled" else "partial_reschedule",
            )
            self.assertEqual(
                (await self.tool(owner, "reschedule_appointment", **args))["outcome"],
                result["outcome"],
            )
            self.assertEqual(requests[1][1]["rescheduleToken"], "private-reschedule")
            self.assertEqual(requests[1][1]["appointmentTypeId"], 1007)
            self.assertEqual(
                [r[0] for r in requests],
                [
                    "/api/scheduler/slots",
                    "/api/appointment/book",
                    "/api/appointment/cancel",
                ],
            )
            self.assertEqual(
                [a.id for a in owner.state.patient.active.appointments],
                [888] if status == "cancelled" else [77, 888],
            )

    async def test_unknown_appointment_type_does_not_guess_reschedule_visit(self):
        for type_id in (None, 99999):
            owner, requests = self.owner([inventory(), {"status": "cancelled"}])
            verified(owner.state, appointmentsStatus="found", appointments=[appointment(appointmentTypeId=type_id)])
            old = owner.appointments()[0]
            self.assertIsNone(old["visitType"])
            ref = await self.slots(owner)
            result = await self.tool(
                owner, "reschedule_appointment", oldAppointmentRef=old["appointmentRef"],
                appointmentSlotRef=ref, appointmentReason="Annual follow up",
                referringDoctor="none", readBack=True,
            )
            self.assertEqual(result["outcome"], "needs_staff_review")
            self.assertEqual(len(requests), 1)
            self.assertEqual([a.id for a in owner.state.patient.active.appointments], [77])
            # Cancelling the verified exact appointment does not require guessing its visit type.
            result = await self.tool(owner, "cancel_appointment", appointmentRef=old["appointmentRef"])
            self.assertEqual(result["outcome"], "cancelled")

    async def test_failed_reschedule_does_not_cancel_old(self):
        owner, requests = self.owner(
            [inventory(), {"status": "error", "outcome": "slot_unavailable"}]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        old_ref = owner.appointments()[0]["appointmentRef"]
        ref = await self.slots(owner)
        result = await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=old_ref,
            appointmentSlotRef=ref,
            appointmentReason="Annual medical follow up",
            referringDoctor="none",
            readBack=True,
        )
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(len(requests), 2)
        self.assertEqual([a.id for a in owner.state.patient.active.appointments], [77])

    async def test_closed_owner_cannot_start_new_reads_or_writes(self):
        owner, requests = self.owner([])
        await owner.aclose()
        self.assertEqual(
            (await owner.availability("medical"))["outcome"], "unavailable"
        )
        self.assertEqual((await self.book(owner, "S1"))["outcome"], "unavailable")
        self.assertEqual(requests, [])

    async def test_expired_arrival_refreshes_once_and_empty_reads_cache(self):
        expired = inventory(bookingTokenExpiresAt="2026-09-14T16:00:00Z")
        owner, requests = self.owner([expired, inventory()])
        await self.slots(owner)
        self.assertEqual(len(requests), 2)
        owner, requests = self.owner([expired, expired])
        self.assertEqual(
            (await owner.availability("medical"))["outcome"], "availability_failed"
        )
        self.assertEqual(
            (await owner.availability("medical"))["outcome"], "availability_failed"
        )
        self.assertEqual(len(requests), 2)
        owner, requests = self.owner([inventory(outcome="no_availability", slots=[])])
        self.assertEqual((await owner.availability("medical"))["outcome"], "none")
        self.assertEqual((await owner.availability("medical"))["outcome"], "none")
        self.assertEqual(len(requests), 1)

    async def test_transport_write_uncertainty_and_explicit_failure(self):
        async def timeout(request):
            raise httpx.ReadTimeout("offline")

        owner, requests = self.owner([inventory(), timeout])
        ref = await self.slots(owner)
        self.assertEqual((await self.book(owner, ref))["outcome"], "uncertain")
        self.assertEqual((await self.book(owner, ref))["outcome"], "uncertain")
        self.assertEqual(len(requests), 2)
        owner, requests = self.owner(
            [
                inventory(),
                {"status": "error", "outcome": "write_failed"},
                inventory(),
                {"status": "booked", "appointmentId": 888},
            ]
        )
        ref = await self.slots(owner)
        self.assertEqual((await self.book(owner, ref))["outcome"], "failed")
        ref = await self.slots(owner)
        self.assertEqual((await self.book(owner, ref))["outcome"], "booked")
        self.assertEqual(len(requests), 4)

    async def test_reschedule_patient_switch_preserves_booking_without_cancelling(self):
        async def switched(request):
            owner.state.patient.revision += 1
            verified(owner.state, "chart-john")
            return httpx.Response(200, json={"status": "booked", "appointmentId": 888})

        owner, requests = self.owner([inventory(), switched])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        old_ref = owner.appointments()[0]["appointmentRef"]
        ref = await self.slots(owner)
        result = await self.tool(
            owner,
            "reschedule_appointment",
            oldAppointmentRef=old_ref,
            appointmentSlotRef=ref,
            appointmentReason="Annual medical follow up",
            referringDoctor="none",
            readBack=True,
        )
        self.assertEqual(result["outcome"], "partial_reschedule")
        self.assertEqual(owner.state.patient.active.appointments, [])
        self.assertEqual(len(requests), 2)
        owner.state.patient.revision += 1
        verified(owner.state)
        self.assertEqual((await self.book(owner, ref))["outcome"], "partial_reschedule")
        self.assertEqual(len(requests), 2)

    async def test_completed_cancellation_survives_same_patient_reload(self):
        owner, requests = self.owner([{"status": "cancelled"}])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = owner.appointments()[0]["appointmentRef"]
        await self.tool(owner, "cancel_appointment", appointmentRef=ref)
        owner.state.patient.revision += 1
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        new_ref = owner._reference(owner.state.patient.active.appointments[0])
        self.assertNotEqual(ref, new_ref)
        self.assertEqual(
            (await self.tool(owner, "cancel_appointment", appointmentRef=new_ref))[
                "outcome"
            ],
            "cancelled",
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(owner.appointments(), [])
        self.assertEqual(owner.state.patient.active.appointments, [])

    def test_schema_has_no_patient_ids_or_tokens(self):
        owner, _ = self.owner([])
        for tool in owner.tools:
            schema = build_strict_openai_schema(tool)
            serialized = json.dumps(schema)
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
        return SchedulingStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


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
            delta = llm.ChoiceDelta(
                role="assistant", content=json.loads(current[-1].output)["answer"]
            )
        else:
            user = items[last_user].text_content
            previous = json.loads(outputs[-1].output) if outputs else {}
            if user == "search":
                name, args = (
                    "list_available_appointments",
                    {"visitType": "medical", "startDate": None, "office": None},
                )
            elif user == "cancel":
                name, args = (
                    "cancel_appointment",
                    {"appointmentRef": previous["appointments"][-1]["appointmentRef"]},
                )
            else:
                name = (
                    "reschedule_appointment" if user == "move" else "book_appointment"
                )
                args = {
                    "appointmentSlotRef": previous["slots"][0]["appointmentSlotRef"],
                    "appointmentReason": "Annual medical follow up",
                    "referringDoctor": "none",
                    "readBack": True,
                }
                if user == "move":
                    args["oldAppointmentRef"] = previous["appointments"][0][
                        "appointmentRef"
                    ]
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
        agent = AbitaAgent(SPRING_HILL, None, scheduling=owner)
        async with AgentSession(llm=model, userdata=owner.state) as session:
            with patch.object(AbitaAgent, "on_enter", new=AsyncMock()):
                await session.start(agent=agent)
            await asyncio.wait_for(session.run(user_input="search"), 5)
            owner.state.patient.revision += 1
            verified(owner.state, "chart-john")
            await asyncio.wait_for(session.run(user_input="book"), 5)
            outputs = [
                item
                for item in model.requests[-1].items
                if item.type == "function_call_output"
            ]
            self.assertEqual(json.loads(outputs[-1].output)["outcome"], "needs_input")
        self.assertEqual(len(requests), 1)

    async def test_all_four_registered_tools_through_agent_session(self):
        owner, requests = self.owner(
            [
                inventory(),
                {"status": "booked", "appointmentId": 888, "appointmentTypeId": 1007},
                {"status": "cancelled"},
                inventory(),
                {"status": "booked", "appointmentId": 999, "appointmentTypeId": 1007},
                {"status": "cancelled"},
            ]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        model = SchedulingModel()
        agent = AbitaAgent(SPRING_HILL, None, scheduling=owner)
        async with AgentSession(llm=model, userdata=owner.state) as session:
            with patch.object(AbitaAgent, "on_enter", new=AsyncMock()):
                await session.start(agent=agent)
            for user, expected in (
                ("search", "found"),
                ("book", "booked"),
                ("cancel", "cancelled"),
                ("search", "found"),
                ("move", "rescheduled"),
            ):
                await asyncio.wait_for(session.run(user_input=user), 5)
                outputs = [
                    item
                    for item in model.requests[-1].items
                    if item.type == "function_call_output"
                ]
                self.assertEqual(json.loads(outputs[-1].output)["outcome"], expected)
                for output in outputs:
                    for secret in (
                        "private-signed-slot",
                        "private-cancel",
                        "private-reschedule",
                        "chart-jane",
                    ):
                        self.assertNotIn(secret, output.output)
        self.assertEqual(len(requests), 6)
        self.assertEqual([a.id for a in owner.state.patient.active.appointments], [999])
