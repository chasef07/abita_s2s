"""Offline scheduling behavior against the actual HTTP envelopes and registered tools."""

import asyncio
import json
import re
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from livekit.agents import AgentSession, llm
from livekit.agents.llm.utils import build_strict_openai_schema
from test_patient_resolution import CONFIG, call_state, receipt

from abita_s2s.agent import AbitaAgent
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance_state import AcceptedInsurance
from abita_s2s.middleware import Appointment, Receipt
from abita_s2s.offices import SPRING_HILL, get_office_profile
from abita_s2s.scheduling import MutationReceipt, Scheduling
from abita_s2s.scheduling_http import SchedulingHTTP

NOW = datetime(2026, 9, 14, 17, tzinfo=UTC)


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
    return {"status": status, "appointmentId": appointment_id, "appointmentTypeId": 1007,
            "visitType": "medical", "officeId": "spring_hill", "office": "Spring Hill",
            "cancellationToken": "new-cancel", "rescheduleToken": "new-reschedule"}


def rescheduled(status="cancelled", appointment_id=888, old_id=77, note=False):
    return {"status": "completed" if status == "cancelled" else "partial",
            "booking": booking(appointment_id, "partial" if note else "booked"),
            "cancellation": {"status": "cancelled", "appointmentId": old_id} if status == "cancelled" else None}


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
        state.reporter = Mock()
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
        output = await getattr(owner, name)(SimpleNamespace(userdata=owner.state, function_call=SimpleNamespace(call_id="native-call-id")), **args)
        return output

    async def slots(self, owner, **args):
        result = await self.tool(
            owner, "list_available_appointments", visitType="medical", **args
        )
        self.assertTrue(result.startswith("success: "), result)
        return re.search(r"^(S[0-9]+):", result, re.MULTILINE)[1]

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
            ),
            "needs_input: Ask for Hollywood or Sweetwater on those office calls; omit office for other calls.\nNo upcoming appointments.",
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
                "visitType": "medical",
                "dob": "01/02/1980",
                "routing": "bach_only",
            },
        )
        self.assertEqual(requests[0][2]["authorization"], "test-auth")
        self.assertNotIn("private-signed-slot", json.dumps(owner._cache[2]))

    async def test_plain_text_preserves_empty_search_and_retry_boundaries(self):
        owner, requests = self.owner([
            inventory(outcome="no_availability", slots=[]),
            inventory(outcome="availability_search_incomplete", slots=[], shouldRetrySameSearch=True),
            inventory(outcome="availability_search_incomplete", slots=[], shouldRetrySameSearch=True),
        ])
        empty = await self.tool(owner, "list_available_appointments", visitType="medical")
        self.assertTrue(empty.startswith("no_results: "), empty)
        self.assertIn("Searched 2026-09-15 through 2026-09-28", empty)
        for expected in ("Retry this search once.", "Do not retry this search; ask staff for help."):
            failed = await self.tool(owner, "list_available_appointments", visitType="medical", startDate="2026-09-16")
            self.assertTrue(failed.startswith("blocked: "), failed)
            self.assertIn("this does not mean no openings", failed)
            self.assertIn(expected, failed)
            self.assertNotIn("Available appointments", failed)
        stopped = await self.tool(owner, "list_available_appointments", visitType="medical", startDate="2026-09-16")
        self.assertIn("do not", stopped.lower())
        self.assertEqual(len(requests), 3)

    async def test_plain_text_distinguishes_available_and_existing_appointments(self):
        owner, _ = self.owner([inventory()])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        output = await self.tool(owner, "list_available_appointments", visitType="medical")
        self.assertTrue(output.startswith("success: "), output)
        self.assertIn("Available appointments (Eastern time; references are private):", output)
        self.assertIn("Existing appointments (Eastern time; references are private):", output)
        self.assertRegex(output, r"S[0-9]+: 2026-09-15 at 9:00 AM")
        existing = owner.appointments()[0]
        self.assertIn(f"{existing['appointmentRef']}: {existing['date']} at {existing['time']}", output)
        self.assertIn(f"location: {existing['facility']}", output)
        for private in ("private-signed-slot", "private-cancel", "private-reschedule", "chart-jane"):
            self.assertNotIn(private, output)

    async def test_resolution_describes_reconciled_appointments(self):
        owner, _ = self.owner([])
        resolver = PatientResolver(owner.state, AsyncMock())
        self.addAsyncCleanup(resolver.aclose)
        agent = AbitaAgent(SPRING_HILL, None, resolver, scheduling=owner)
        context = SimpleNamespace(userdata=owner.state, function_call=SimpleNamespace(call_id="resolution"))
        booked = Appointment.model_validate(appointment())
        owner._receipts["chart-jane", "book", None] = MutationReceipt(
            {"outcome": "booked"}, booked=booked,
        )
        # A provider reload has not yet reflected the booking confirmed this call.
        verified(owner.state, appointmentsStatus="none", appointments=[])
        output = await agent.resolve_patient(context, "Jane", None)
        self.assertIn("Existing appointments", output)
        self.assertIn("2026-09-20 at 10:00 AM", output)
        self.assertNotIn("No upcoming appointments", output)

        # Likewise a reload can still include an appointment already cancelled.
        owner._receipts["chart-jane", "cancel", booked.id] = MutationReceipt(
            {"outcome": "cancelled"}, cancelled_id=booked.id,
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        output = await agent.resolve_patient(context, "Jane", None)
        self.assertIn("No upcoming appointments.", output)
        self.assertNotIn("Existing appointments", output)

    def test_unknown_appointments_never_claim_none(self):
        owner, _ = self.owner([])
        owner.state.patient.active = None
        self.assertEqual(owner.appointments_text(), "")
        verified(owner.state, appointmentsStatus="error")
        self.assertNotIn("No upcoming appointments", owner.appointments_text())

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
        for key, visit in (("crystal-river", "routine_vision"), ("north-miami-beach-optical", "medical")):
            state = call_state(None, get_office_profile(key))
            verified(state, visit=visit)
            owner, requests = self.owner([inventory(outcome="no_eligible_providers", slots=[])], state=state)
            result = await self.tool(owner, "list_available_appointments", visitType=visit)
            self.assertTrue(result.startswith("blocked: No providers are eligible"), result)
            self.assertIn("Confirm the office and visit type or ask staff for help", result)
            self.assertNotIn("Searched", result)
            self.assertNotIn("other dates", result)
            self.assertEqual(owner._cache[2]["outcome"], "unsupported")
            self.assertEqual(requests[0][1]["visitType"], visit)

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
        self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], "needs_input")
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
                booking(888, "partial"),
                {"status": "cancelled"},
            ]
        )
        ref = await self.slots(owner)
        for reason, referrer, confirm in (
            ("", "none", True),
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
            self.assertEqual(result.split(":", 1)[0], "needs_input")
        result = await self.book(owner, ref)
        self.assertEqual(result.split(":", 1)[0], 'blocked')
        self.assertIn("note did not save", result)
        self.assertIn("Do not book again", result)
        self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], 'blocked')
        owner.state.reporter.appointment.assert_called_once()
        evidence = owner.state.reporter.appointment.call_args.args[0]
        self.assertEqual(evidence["bookingResult"]["status"], "partial")
        self.assertEqual(evidence["newAppointmentId"], "888")
        self.assertEqual(evidence["bookingResult"]["appointmentDate"], "2026-09-15")
        self.assertEqual(evidence["bookingResult"]["appointmentTime"], inventory()["slots"][0]["time"])
        self.assertEqual(evidence["bookingResult"]["providerName"], "Dr. Bach")
        self.assertNotIn("private-signed-slot", json.dumps(evidence))
        body = requests[1][1]
        self.assertEqual(body["bookingToken"], "private-signed-slot")
        self.assertEqual(body["patientId"], "chart-jane")
        self.assertNotIn("office", body)
        self.assertEqual(body["patientStatus"], "established")
        cancel_ref = owner.appointments()[0]["appointmentRef"]
        self.assertEqual(
            (await self.tool(owner, "cancel_appointment", appointmentRef=cancel_ref, readBack=True)).split(":", 1)[0],
            'success',
        )
        self.assertEqual(
            (await self.tool(owner, "cancel_appointment", appointmentRef=cancel_ref, readBack=True)).split(":", 1)[0],
            'success',
        )
        self.assertEqual(
            requests[-1][1],
            {"patientId": "chart-jane", "cancellationToken": "new-cancel"},
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
            self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], "needs_input")
            self.assertEqual(len(requests), 1)

    async def test_cancelled_booking_cannot_satisfy_a_new_booking(self):
        owner, requests = self.owner([
            inventory(), booking(),
            {"status": "cancelled"}, inventory(),
            {"status": "booked", "appointmentId": 999},
        ])
        first = await self.slots(owner)
        await self.book(owner, first)
        await self.tool(owner, "cancel_appointment",
                        appointmentRef=owner.appointments()[0]["appointmentRef"], readBack=True)
        replay = await self.book(owner, first)
        self.assertTrue(replay.startswith("blocked:"), replay)
        self.assertIn("cancelled", replay)
        second = await self.slots(owner)
        self.assertNotEqual(first, second)
        result = await self.book(owner, second)
        self.assertTrue(result.startswith("success:"), result)
        self.assertEqual([a.id for a in owner.state.patient.active.appointments], [999])
        self.assertEqual([path for path, _, _ in requests], [
            "/api/scheduler/slots", "/api/appointment/book", "/api/appointment/cancel",
            "/api/scheduler/slots", "/api/appointment/book",
        ])

    async def test_booking_replay_is_scoped_to_the_selected_slot(self):
        later = inventory()
        later["slots"][0].update(datetime="2026-09-16T09:00", bookingToken="second-slot")
        owner, requests = self.owner([
            inventory(), {"status": "booked", "appointmentId": 888},
            inventory(), later, {"status": "booked", "appointmentId": 999},
        ])
        first = await self.slots(owner)
        original = await self.book(owner, first)
        self.assertEqual(await self.book(owner, first), original)
        # A fresh reference/token for the same live appointment is still a duplicate.
        same_slot = await self.slots(owner)
        self.assertEqual(await self.book(owner, same_slot), original)
        second = await self.slots(owner, startDate="2026-09-16")
        result = await self.book(owner, second)
        self.assertIn("Booked 2026-09-16", result)
        self.assertEqual([a.id for a in owner.state.patient.active.appointments], [888, 999])
        self.assertEqual([body["bookingToken"] for path, body, _ in requests
                          if path == "/api/appointment/book"], ["private-signed-slot", "second-slot"])

    async def test_uncertain_book_never_repeats_even_after_reload(self):
        for response in (
            {"status": "booked"},
            {"status": "error", "outcome": "write_ambiguous"},
            {"unexpected": True},
        ):
            owner, requests = self.owner([inventory(), response, inventory()])
            ref = await self.slots(owner)
            uncertain = await self.book(owner, ref)
            self.assertTrue(uncertain.startswith("blocked:"))
            self.assertIn("Do not repeat the write or claim success", uncertain)
            ref = await self.slots(owner)
            self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], 'blocked')
            self.assertEqual(len(requests), 3)
            self.assertEqual(owner.state.patient.active.appointments, [])

    async def test_booking_rechecks_admission_when_write_task_starts(self):
        for change in ("patient", "shutdown"):
            with self.subTest(change=change):
                owner, requests = self.owner([inventory()])
                ref = await self.slots(owner)
                callback = (lambda: verified(owner.state, "chart-john")) if change == "patient" else owner.close_admission
                asyncio.get_running_loop().call_soon(callback)
                result = await self.book(owner, ref)
                self.assertTrue(result.startswith("blocked:"), result)
                self.assertEqual(len(requests), 1)

    async def test_same_patient_coverage_correction_invalidates_offered_slots(self):
        owner, requests = self.owner([inventory()])
        ref = await self.slots(owner)
        # Even a new acceptance with identical values represents a fresh check.
        previous = owner.state.insurance.accepted
        verified(owner.state)
        self.assertEqual(previous, owner.state.insurance.accepted)
        self.assertIsNot(previous, owner.state.insurance.accepted)
        self.assertTrue((await self.book(owner, ref)).startswith("needs_input:"))
        self.assertEqual(len(requests), 1)

    async def test_chained_moves_preserve_receipts_and_reject_obsolete_booking_replay(self):
        later = inventory()
        later["slots"][0].update(datetime="2026-09-16T09:00", bookingToken="second-slot")
        last = inventory()
        last["slots"][0].update(datetime="2026-09-17T09:00", bookingToken="third-slot")
        owner, requests = self.owner([
            inventory(), booking(888, "booked"),
            later, rescheduled(appointment_id=999, old_id=888),
            last, rescheduled(appointment_id=1000, old_id=999),
            {"status": "cancelled"}, inventory(), {"status": "booked", "appointmentId": 1001},
        ])
        initial = await self.slots(owner)
        await self.book(owner, initial)
        moves = []
        for day, expected_id in (("2026-09-16", 999), ("2026-09-17", 1000)):
            old_ref = owner.appointments()[0]["appointmentRef"]
            slot_ref = await self.slots(owner, startDate=day)
            args = dict(oldAppointmentRef=old_ref, appointmentSlotRef=slot_ref,
                        appointmentReason="Annual follow up", referringDoctor="none", readBack=True)
            if moves:
                # A different selection cannot replay success for an obsolete old reference.
                stale = await self.tool(owner, "reschedule_appointment",
                                        **{**moves[0], "appointmentSlotRef": slot_ref})
                self.assertTrue(stale.startswith("needs_input:"), stale)
            result = await self.tool(owner, "reschedule_appointment", **args)
            self.assertTrue(result.startswith("success:"), result)
            self.assertEqual([a.id for a in owner.state.patient.active.appointments], [expected_id])
            self.assertEqual(await self.tool(owner, "reschedule_appointment", **args), result)
            moves.append(args)
        self.assertTrue((await self.book(owner, initial)).startswith("blocked:"))
        self.assertTrue((await self.tool(owner, "reschedule_appointment", **moves[0])).startswith("blocked:"))
        await self.tool(owner, "cancel_appointment", appointmentRef=owner.appointments()[0]["appointmentRef"], readBack=True)
        await self.book(owner, await self.slots(owner))
        # A stale backend reload must not resurrect any earlier booking.
        verified(owner.state, appointmentsStatus="found", appointments=[appointment(id=888)])
        owner.appointments()
        self.assertEqual([a.id for a in owner.state.patient.active.appointments], [1001])
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
        self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], 'blocked')
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
        self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], 'success')
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
                        if action == "book" else {"status": "cancelled"}
                    )
                    return httpx.Response(200, json=result)

                owner, requests = self.owner(
                    [inventory(), delayed] if action == "book" else [delayed]
                )
                if action == "book":
                    ref = await self.slots(owner)
                    task = asyncio.create_task(self.book(owner, ref))
                else:
                    verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
                    ref = owner.appointments()[0]["appointmentRef"]
                    task = asyncio.create_task(self.tool(
                        owner, "cancel_appointment", appointmentRef=ref, readBack=True,
                    ))
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release.set()
                await owner.aclose()
                # No presentation/read call should be needed to apply the receipt.
                patient = owner.state.patient.active
                self.assertEqual([a.id for a in patient.appointments], [888] if action == "book" else [])
                self.assertEqual(patient.appointmentsStatus, "found" if action == "book" else "none")
                self.assertEqual(len(requests), 2 if action == "book" else 1)

    async def test_partial_note_reschedule_reconciles_during_write_and_after_reload(self):
        for cancellation_status in ("cancelled", "error"):
            with self.subTest(cancellation_status=cancellation_status):
                owner, requests = self.owner([
                    inventory(), rescheduled(cancellation_status, note=True),
                ])
                verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
                old_ref = owner.appointments()[0]["appointmentRef"]
                slot_ref = await self.slots(owner)
                args = dict(
                    oldAppointmentRef=old_ref, appointmentSlotRef=slot_ref,
                    appointmentReason="Annual medical follow up", referringDoctor="none", readBack=True,
                )
                result = await self.tool(owner, "reschedule_appointment", **args)
                self.assertTrue(result.startswith("blocked:"))
                self.assertIn("note did not save", result)
                expected_ids = [888] if cancellation_status == "cancelled" else [77, 888]
                self.assertEqual([a.id for a in owner.state.patient.active.appointments], expected_ids)
                checkpoints = [c.args[0] for c in owner.state.reporter.appointment.call_args_list]
                self.assertEqual([c["bookingResult"]["status"] for c in checkpoints], ["partial"])
                self.assertEqual(
                    [c["cancellationResult"]["status"] for c in checkpoints],
                    ["cancelled" if cancellation_status == "cancelled" else "uncertain"],
                )
                # A stale reload must produce the same appointment list, repeatedly.
                verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
                for _ in range(2):
                    owner.appointments()
                    self.assertEqual([a.id for a in owner.state.patient.active.appointments], expected_ids)
                replay = await self.tool(owner, "reschedule_appointment", **args)
                self.assertEqual(replay, result)
                self.assertEqual(len(requests), 2)

    async def test_cancellation_requires_confirmation_without_writing(self):
        owner, requests = self.owner([{"status": "cancelled"}])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = owner.appointments()[0]["appointmentRef"]
        for confirmation in (None, False):
            result = await self.tool(
                owner, "cancel_appointment", appointmentRef=ref, readBack=confirmation,
            )
            self.assertTrue(result.startswith("needs_input: Confirm cancellation"))
            self.assertEqual(requests, [])
            self.assertEqual([a.id for a in owner.state.patient.active.appointments], [77])
        result = await self.tool(owner, "cancel_appointment", appointmentRef=ref, readBack=True)
        self.assertIn("success: The selected appointment was cancelled.", result)
        self.assertEqual(len(requests), 1)
        self.assertEqual(owner.state.patient.active.appointments, [])

    async def test_vague_reason_is_preserved_and_booking_requires_confirmation(self):
        owner, requests = self.owner([
            inventory(), booking(888, "booked"),
        ])
        ref = await self.slots(owner)
        args = dict(appointmentSlotRef=ref, appointmentReason="eye problems",
                    referringDoctor="none", readBack=None)
        result = await self.tool(owner, "book_appointment", **args)
        self.assertTrue(result.startswith("needs_input: Confirm"))
        self.assertEqual(len(requests), 1)
        args["readBack"] = True
        result = await self.tool(owner, "book_appointment", **args)
        self.assertTrue(result.startswith("success: Booked"))
        self.assertEqual(requests[-1][1]["appointmentReason"], "eye problems")
        self.assertIn("Existing appointments", result)

    async def test_missing_booking_facts_survive_plain_text(self):
        owner, requests = self.owner([
            inventory(), {"status": "error", "outcome": "appointment_type_unresolved", "missing": ["routing"]},
        ])
        ref = await self.slots(owner)
        result = await self.book(owner, ref)
        self.assertIn("needs_input: Booking needs additional facts: routing.", result)
        self.assertEqual(owner.state.patient.active.appointments, [])
        self.assertEqual(len(requests), 2)

    async def test_cancellation_exact_target_token_and_expired_details(self):
        owner, requests = self.owner(
            [{"status": "error", "outcome": "invalid_cancellation_token"}]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = owner.appointments()[0]["appointmentRef"]
        self.assertEqual(
            (await self.tool(owner, "cancel_appointment", appointmentRef="77", readBack=True)).split(":", 1)[0],
            "needs_input",
        )
        self.assertEqual(
            (await self.tool(owner, "cancel_appointment", appointmentRef=ref, readBack=True)).split(":", 1)[0],
            'needs_input',
        )
        self.assertEqual(requests[0][1], {"patientId": "chart-jane", "cancellationToken": "private-cancel"})
        self.assertEqual(owner.state.patient.active.appointmentsStatus, "error")

    async def test_reschedule_partial_and_success_never_rebooks(self):
        for status in ("cancelled", "error"):
            owner, requests = self.owner([inventory(), rescheduled(status)])
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
            unconfirmed = await self.tool(owner, "reschedule_appointment", **{**args, "readBack": None})
            self.assertTrue(unconfirmed.startswith("needs_input: Confirm"))
            self.assertEqual(len(requests), 1)
            self.assertEqual([a.id for a in owner.state.patient.active.appointments], [77])
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
                self.assertIn("requires staff reconciliation. Do not book again.", result)
            self.assertEqual(
                result.split(":", 1)[0],
                'success' if status == 'cancelled' else 'blocked',
            )
            self.assertEqual(
                (await self.tool(owner, "reschedule_appointment", **args)).split(":", 1)[0],
                result.split(":", 1)[0],
            )
            checkpoints = [c.args[0] for c in owner.state.reporter.appointment.call_args_list]
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(checkpoints[0]["cancellationResult"]["status"],
                             "cancelled" if status == "cancelled" else "uncertain")
            self.assertTrue(all(c["externalPatientId"] == "chart-jane" for c in checkpoints))
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
                [a.id for a in owner.state.patient.active.appointments],
                [888] if status == "cancelled" else [77, 888],
            )

    async def test_metadata_authority_and_missing_action_tokens(self):
        owner, requests = self.owner([])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment(
            appointmentTypeId=99999, visitType="routine_vision", facility="Spring Hill",
            officeId="crystal_river", cancellationToken=None,
        )])
        entry = owner.appointments()[0]
        self.assertEqual(entry["visitType"], "routine_vision")
        result = await self.tool(owner, "cancel_appointment", appointmentRef=entry["appointmentRef"], readBack=True)
        self.assertIn("Reload appointments", result)
        self.assertEqual(requests, [])

    async def test_missing_action_tokens_force_real_patient_reload(self):
        for action, field in (("cancel", "cancellationToken"), ("reschedule", "rescheduleToken")):
            with self.subTest(action=action):
                owner, requests = self.owner([inventory()] if action == "reschedule" else [])
                verified(owner.state, appointmentsStatus="found", appointments=[appointment(**{field: None})])
                old_ref = owner.appointments()[0]["appointmentRef"]
                if action == "cancel":
                    result = await self.tool(owner, "cancel_appointment", appointmentRef=old_ref, readBack=True)
                else:
                    slot_ref = await self.slots(owner)
                    result = await self.tool(
                        owner, "reschedule_appointment", oldAppointmentRef=old_ref,
                        appointmentSlotRef=slot_ref, appointmentReason="Follow up",
                        referringDoctor="none", readBack=True,
                    )
                self.assertIn("Reload appointments", result)
                self.assertEqual(owner.state.patient.active.appointmentsStatus, "error")
                self.assertFalse(any(path.startswith("/api/appointment/") for path, _, _ in requests))

                # Proven call-local receipts cannot turn the incomplete read into found.
                owner._receipts["chart-jane", "book", None] = MutationReceipt(
                    {"outcome": "booked"}, booked=Appointment.model_validate(appointment(id=88)),
                )
                owner.appointments()
                self.assertEqual(owner.state.patient.active.appointmentsStatus, "error")
                self.assertIn(88, [a.id for a in owner.state.patient.active.appointments])
                middleware = AsyncMock()
                middleware.resolve.return_value = Receipt.model_validate(receipt(
                    appointmentsStatus="found", appointments=[appointment()],
                ))
                resolver = PatientResolver(owner.state, middleware)
                self.addAsyncCleanup(resolver.aclose)
                resolved = await resolver.resolve("Jane", None)
                self.assertEqual(resolved["outcome"], "verified")
                middleware.resolve.assert_awaited_once()
                self.assertTrue(getattr(owner.state.patient.active.appointments[0], field))
                self.assertEqual(owner.state.patient.active.appointmentsStatus, "found")
                owner.appointments()
                self.assertEqual({a.id for a in owner.state.patient.active.appointments}, {77, 88})

    async def test_invalid_reschedule_receipt_is_uncertain_and_never_retried(self):
        for response in (
            {"status": "completed", "booking": booking()},
            {"status": "completed", "booking": booking(), "cancellation": {"status": "cancelled", "appointmentId": 888}},
            {"status": "partial"},
            {"status": "uncertain", "booking": booking(999)},
        ):
            owner, requests = self.owner([inventory(), response])
            verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
            old_ref = owner.appointments()[0]["appointmentRef"]
            ref = await self.slots(owner)
            args = dict(oldAppointmentRef=old_ref, appointmentSlotRef=ref,
                        appointmentReason="Follow up", referringDoctor="none", readBack=True)
            result = await self.tool(owner, "reschedule_appointment", **args)
            self.assertIn("could not be confirmed", result)
            self.assertEqual(await self.tool(owner, "reschedule_appointment", **args), result)
            self.assertEqual([a.id for a in owner.state.patient.active.appointments], [77])
            self.assertEqual(len(requests), 2)

    async def test_unknown_appointment_type_does_not_guess_reschedule_visit(self):
        for type_id in (None, 99999):
            owner, requests = self.owner([inventory(), {"status": "cancelled"}])
            verified(owner.state, appointmentsStatus="found", appointments=[appointment(appointmentTypeId=type_id, visitType=None)])
            old = owner.appointments()[0]
            self.assertIsNone(old["visitType"])
            ref = await self.slots(owner)
            result = await self.tool(
                owner, "reschedule_appointment", oldAppointmentRef=old["appointmentRef"],
                appointmentSlotRef=ref, appointmentReason="Annual follow up",
                referringDoctor="none", readBack=True,
            )
            self.assertEqual(result.split(":", 1)[0], 'blocked')
            self.assertEqual(len(requests), 1)
            self.assertEqual([a.id for a in owner.state.patient.active.appointments], [77])
            # Cancelling the verified exact appointment does not require guessing its visit type.
            result = await self.tool(owner, "cancel_appointment", appointmentRef=old["appointmentRef"], readBack=True)
            self.assertEqual(result.split(":", 1)[0], 'success')

    async def test_failed_reschedule_does_not_cancel_old(self):
        owner, requests = self.owner(
            [inventory(), {"status": "failed", "outcome": "slot_unavailable"}]
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
        self.assertEqual(result.split(":", 1)[0], 'blocked')
        self.assertEqual(len(requests), 2)
        self.assertEqual([a.id for a in owner.state.patient.active.appointments], [77])

    async def test_reschedule_retries_only_after_definite_failure_and_fresh_confirmation(self):
        owner, requests = self.owner([
            inventory(), {"status": "failed", "outcome": "write_failed"},
            inventory(), rescheduled(),
        ])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        old_ref = owner.appointments()[0]["appointmentRef"]
        slot_ref = await self.slots(owner)
        args = dict(oldAppointmentRef=old_ref, appointmentSlotRef=slot_ref,
                    appointmentReason="Follow up", referringDoctor="none", readBack=True)
        self.assertIn("reschedule failed", await self.tool(owner, "reschedule_appointment", **args))
        self.assertTrue((await self.tool(owner, "reschedule_appointment", **args)).startswith("needs_input:"))
        self.assertEqual(len(requests), 2)
        args["appointmentSlotRef"] = await self.slots(owner)
        self.assertTrue((await self.tool(owner, "reschedule_appointment", **{**args, "readBack": None})).startswith("needs_input: Confirm"))
        self.assertEqual(len(requests), 3)
        self.assertTrue((await self.tool(owner, "reschedule_appointment", **args)).startswith("success:"))
        self.assertEqual([a.id for a in owner.state.patient.active.appointments], [888])
        self.assertEqual(len(requests), 4)

    async def test_reschedule_transport_failure_fences_further_writes(self):
        async def timed_out(request):
            raise httpx.ReadTimeout("response lost", request=request)

        owner, requests = self.owner([inventory(), timed_out, inventory()])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        old_ref = owner.appointments()[0]["appointmentRef"]
        slot_ref = await self.slots(owner)
        args = dict(oldAppointmentRef=old_ref, appointmentSlotRef=slot_ref,
                    appointmentReason="Follow up", referringDoctor="none", readBack=True)
        result = await self.tool(owner, "reschedule_appointment", **args)
        self.assertIn("could not be confirmed", result)
        self.assertEqual(await self.tool(owner, "reschedule_appointment", **args), result)
        self.assertEqual(len(requests), 2)
        fresh_slot = await self.slots(owner)
        self.assertIn("could not be confirmed", await self.book(owner, fresh_slot))
        self.assertEqual(await self.tool(owner, "cancel_appointment", appointmentRef=old_ref, readBack=True), result)
        self.assertEqual(len(requests), 3)
        self.assertEqual([a.id for a in owner.state.patient.active.appointments], [77])

    async def test_closed_owner_cannot_start_new_reads_or_writes(self):
        owner, requests = self.owner([])
        await owner.aclose()
        self.assertEqual(
            (await owner.availability("medical"))["outcome"], "unavailable"
        )
        self.assertEqual((await self.book(owner, "S1")).split(":", 1)[0], 'blocked')
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
        self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], 'blocked')
        self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], 'blocked')
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
        self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], 'blocked')
        ref = await self.slots(owner)
        self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], 'success')
        self.assertEqual(len(requests), 4)

    async def test_reschedule_patient_switch_preserves_middleware_result_for_original_patient(self):
        async def switched(request):
            owner.state.patient.revision += 1
            verified(owner.state, "chart-john")
            return httpx.Response(200, json=rescheduled())

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
        self.assertEqual(result.split(":", 1)[0], 'success')
        self.assertEqual(owner.state.patient.active.appointments, [])
        evidence = owner.state.reporter.appointment.call_args.args[0]
        self.assertEqual(evidence["externalPatientId"], "chart-jane")
        self.assertEqual(evidence["cancellationResult"]["status"], "cancelled")
        self.assertEqual(owner.state.reporter.appointment.call_args.kwargs["call_id"], "native-call-id")
        self.assertEqual(len(requests), 2)
        owner.state.patient.revision += 1
        verified(owner.state)
        owner.appointments()
        self.assertEqual([a.id for a in owner.state.patient.active.appointments], [888])
        self.assertEqual((await self.book(owner, ref)).split(":", 1)[0], 'needs_input')
        self.assertEqual(len(requests), 2)

    async def test_completed_cancellation_survives_same_patient_reload(self):
        owner, requests = self.owner([{"status": "cancelled"}])
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        ref = owner.appointments()[0]["appointmentRef"]
        await self.tool(owner, "cancel_appointment", appointmentRef=ref, readBack=True)
        owner.state.patient.revision += 1
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        new_ref = owner._reference(owner.state.patient.active.appointments[0])
        self.assertNotEqual(ref, new_ref)
        self.assertEqual(
            (await self.tool(owner, "cancel_appointment", appointmentRef=new_ref, readBack=True)).split(":", 1)[0],
            'success',
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
                role="assistant", content=current[-1].output
            )
        else:
            user = items[last_user].text_content
            previous = outputs[-1].output if outputs else ""
            if user == "search":
                name, args = (
                    "list_available_appointments",
                    {"visitType": "medical", "startDate": None, "office": None},
                )
            elif user == "cancel":
                name, args = (
                    "cancel_appointment",
                    {"appointmentRef": re.findall(r"^(A[0-9]+):", previous, re.MULTILINE)[-1], "readBack": True},
                )
            else:
                name = (
                    "reschedule_appointment" if user == "move" else "book_appointment"
                )
                args = {
                    "appointmentSlotRef": re.search(r"^(S[0-9]+):", previous, re.MULTILINE)[1],
                    "appointmentReason": "Annual medical follow up",
                    "referringDoctor": "none",
                    "readBack": True,
                }
                if user == "move":
                    args["oldAppointmentRef"] = re.search(r"^(A[0-9]+):", previous, re.MULTILINE)[1]
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
            self.assertEqual(outputs[-1].output.split(":", 1)[0], "needs_input")
        self.assertEqual(len(requests), 1)

    async def test_all_four_registered_tools_through_agent_session(self):
        owner, requests = self.owner(
            [
                inventory(),
                booking(888, "booked"),
                {"status": "cancelled"},
                inventory(),
                rescheduled(appointment_id=999),
            ]
        )
        verified(owner.state, appointmentsStatus="found", appointments=[appointment()])
        model = SchedulingModel()
        agent = AbitaAgent(SPRING_HILL, None, scheduling=owner)
        async with AgentSession(llm=model, userdata=owner.state) as session:
            with patch.object(AbitaAgent, "on_enter", new=AsyncMock()):
                await session.start(agent=agent)
            for user, expected in (
                ("search", "success"),
                ("book", "success"),
                ("cancel", "success"),
                ("search", "success"),
                ("move", "success"),
            ):
                await asyncio.wait_for(session.run(user_input=user), 5)
                outputs = [
                    item
                    for item in model.requests[-1].items
                    if item.type == "function_call_output"
                ]
                if user == "search":
                    self.assertTrue(outputs[-1].output.startswith("success: "), outputs[-1].output)
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
        self.assertEqual([a.id for a in owner.state.patient.active.appointments], [999])
