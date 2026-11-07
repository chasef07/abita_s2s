"""Whole calls pinned to golden bytes: each tool answer, each middleware request, each Product payload.

The golden file was written from the code before the per-call record existed. A refactor
must leave it unchanged. Regenerate only for a deliberate change: WRITE_PINS=1 python -m unittest test_call_pins
"""

import asyncio
import json
import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from test_insurance_registration import created, updated
from test_patient_resolution import CONFIG, call_state, candidate, receipt, search
from test_scheduling import appointment, booking, inventory, rescheduled, soonest

from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.integrations.patient_middleware import PatientMiddleware
from abita_s2s.integrations.product import Product
from abita_s2s.integrations.registration_middleware import RegistrationMiddleware
from abita_s2s.integrations.scheduling_http import SchedulingHTTP
from abita_s2s.runtime.reporting import CallReporter
from abita_s2s.runtime.session_startup import _gather_raising
from abita_s2s.scheduling.appointments import Scheduling
from abita_s2s.staff_tasks import StaffTasks
from abita_s2s.tools.insurance import InsuranceTools
from abita_s2s.tools.patients import PatientTools
from abita_s2s.tools.scheduling import SchedulingTools
from abita_s2s.tools.staff_tasks import StaffTaskTools

PINS = Path(__file__).parent / "pins" / "call_flows.json"
NOW = datetime(2026, 9, 14, 17, tzinfo=UTC)
JANE = "01/02/1980"
JOHN = "02/03/1982"
PLAN = "test-insurance"
MEMBER = "member-example"
ACK = {"status": "created", "interactionId": "d3665980-68ce-4336-87af-e2ba40ad2e8e"}
STAFF_ACK = {
    "status": "created",
    "taskId": "1f0c2f4e-3a44-4c1b-9a50-6b4a4c6d7e8f",
    "category": "other",
    "urgency": "normal",
}
REPORT = {
    "job_id": "job-pin",
    "room": "room-pin",
    "chat_history": {"items": []},
    "events": [{"type": "close", "reason": "participant_disconnected", "error": None}],
}
INTAKE = dict(
    firstName="Jane",
    lastName="Doe",
    dob=JANE,
    callback="inbound",
    email=None,
    street="1 Example Street",
    aptSuite=None,
    city="Example",
    state="FL",
    zip="12345",
    sex="female",
    visitType="medical",
    insurancePlanId="aetna",
    subscriberName="Jane Doe",
    insuranceMemberId=MEMBER,
)
BOOK = dict(appointmentReason="Dry eyes", referringDoctor=None)


class FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class Gate:
    """A middleware reply held until the flow releases it, to change the call during a request."""

    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, _request):
        self.arrived.set()
        await self.release.wait()
        return httpx.Response(self.status, json=self.body)


def failure(status=500):
    return httpx.Response(status, json={"error": "unavailable"})


class Call:
    """One call's real owners and tools over scripted middleware, recording every byte that leaves."""

    def __init__(self, phone="+15555550101", **script):
        self.state = call_state(phone)
        self.state.call = replace(self.state.call, session_started_at=NOW)
        self.script = {
            "/api/patient/resolve": script.get("resolve", []),
            "/api/scheduler/slots": script.get("slots", []),
            "/api/appointment/book": script.get("book", []),
            "/api/appointment/cancel": script.get("cancel", []),
            "/api/appointment/reschedule": script.get("move", []),
            "/api/add-patient": script.get("create", []),
            "/api/patient/update-insurance": script.get("update", []),
        }
        self.at = NOW
        self.answers = []
        self.middleware = []
        self.product = []
        self.turn = 0
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._receive))
        config = replace(
            CONFIG,
            interaction_url="https://product.test/v1/ai/interactions",
            staff_tasks_url="https://product.test/v1/staff-tasks",
            product_secret="offline-secret",
        )
        self.resolver = PatientResolver(
            self.state, PatientMiddleware(self.client, config)
        )
        self.scheduling = Scheduling(
            self.state,
            self.resolver,
            SchedulingHTTP(self.client, config),
            now=lambda: self.at,
        )
        self.registration = RegistrationMiddleware(self.client, config)
        self.insurance = InsuranceRegistration(
            self.state, self.resolver, self.registration
        )
        self.staff_tasks = StaffTasks(
            self.state, self.resolver, Product(self.client, config)
        )
        self.state.reporter = CallReporter(
            self.state.call, self.client, config, self.drain, "", ""
        )
        self.state.reporter.started = True
        patients = PatientTools(self.resolver, self.insurance, self.scheduling)
        scheduling = SchedulingTools(self.scheduling)
        self.tools = {
            "resolve_patient": patients.resolve_patient,
            "add_patient": patients.add_patient,
            "update_insurance": InsuranceTools(self.insurance).update_insurance,
            "list_available_appointments": scheduling.list_available_appointments,
            "book_appointment": scheduling.book_appointment,
            "cancel_appointment": scheduling.cancel_appointment,
            "reschedule_appointment": scheduling.reschedule_appointment,
            "save_staff_task": StaffTaskTools(self.staff_tasks).save_staff_task,
        }

    async def _receive(self, request):
        if request.url.host == "product.test":
            self.product.append(request.content.decode())
            return httpx.Response(
                201, json=STAFF_ACK if "staff-tasks" in request.url.path else ACK
            )
        self.middleware.append(f"{request.url.path} {request.content.decode()}")
        reply = self.script[request.url.path].pop(0)
        if callable(reply):
            return await reply(request)
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    async def drain(self):
        writers = (self.resolver, self.scheduling, self.insurance, self.staff_tasks)
        for writer in writers:
            writer.close_admission()
        await _gather_raising(
            *(writer.aclose() for writer in writers),
            cancelled="An accepted mutation was cancelled during shutdown",
        )

    async def say(self, tool, **args):
        self.turn += 1
        context = SimpleNamespace(
            userdata=self.state,
            function_call=SimpleNamespace(call_id=f"tool-{self.turn}"),
            disallow_interruptions=lambda: None,
        )
        try:
            answer = await self.tools[tool](context, **args)
        except Exception as error:
            answer = f"raised {type(error).__name__}"
        self.answers.append([tool, answer])
        return answer

    async def jane(self, dob=JANE):
        return await self.say("resolve_patient", firstName="Jane", dob=dob)

    async def john(self):
        return await self.say("resolve_patient", firstName="John", dob=JOHN)

    async def search(self, **args):
        return await self.say(
            "list_available_appointments",
            **{"visitType": "medical", "insurancePlanId": PLAN, **args},
        )

    async def move_search(self, ref="A1", **args):
        return await self.search(
            **{"insurancePlanId": None, "rescheduleAppointmentRef": ref, **args}
        )

    async def book(self, ref, read_back=True):
        return await self.say(
            "book_appointment", appointmentSlotRef=ref, readBack=read_back, **BOOK
        )

    async def move(self, ref, read_back=True, old="A1"):
        return await self.say(
            "reschedule_appointment",
            oldAppointmentRef=old,
            appointmentSlotRef=ref,
            readBack=read_back,
        )

    async def cancel(self, ref="A1", read_back=True):
        return await self.say(
            "cancel_appointment", appointmentRef=ref, readBack=read_back
        )

    async def update(self, plan="aetna", member=MEMBER):
        return await self.say(
            "update_insurance",
            insurancePlanId=plan,
            visitType="medical",
            insuranceMemberId=member,
        )

    async def finish(self):
        await self.state.reporter.finish(lambda: REPORT)
        await self.client.aclose()
        return {
            "answers": self.answers,
            "middleware": self.middleware,
            "product": self.product,
        }


def chart(**extra):
    return receipt(appointmentsStatus="found", appointments=[appointment()], **extra)


def tokenless_chart():
    return receipt(
        appointmentsStatus="found", appointments=[appointment(cancellationToken=None)]
    )


def john_chart(**extra):
    return receipt("chart-john", "John", JOHN, **extra)


def later_inventory():
    found = inventory()
    found["slots"][0].update(datetime="2026-09-16T09:00", bookingToken="second-slot")
    return found


async def book_replay_and_repeat(call):
    call.resolver.start_phone_lookup()
    await call.say("resolve_patient", firstName="Jane", dob=None)
    await call.jane()
    found = await call.search()
    ref = soonest(found)[0]
    await call.book(ref, read_back=None)
    await call.book(ref)
    await call.book(ref)
    await call.jane()


async def book_cancel_then_rebook(call):
    await call.jane()
    ref = soonest(await call.search())[0]
    await call.book(ref)
    await call.cancel("A2")
    await call.book(ref)
    again = soonest(await call.search())[0]
    await call.book(again)
    await call.jane()


async def book_refused_then_retry(call):
    await call.jane()
    ref = soonest(await call.search())[0]
    await call.book(ref)
    await call.book(ref)
    ref = soonest(await call.search())[0]
    await call.book(ref)
    await call.book(ref)


async def book_refusal_labels(call):
    await call.jane()
    for _ in range(4):
        ref = soonest(await call.search())[0]
        await call.book(ref)


async def move_and_replay(call):
    await call.jane()
    found = await call.move_search()
    ref = soonest(found)[0]
    await call.book(ref)
    await call.move(ref, read_back=None)
    await call.move(ref)
    await call.move(ref)
    await call.book(ref)
    await call.jane()


async def move_with_a_plan_then_other_slot(call):
    await call.jane()
    ref = soonest(await call.move_search(insurancePlanId=PLAN))[0]
    await call.move(ref)
    await call.cancel("A1")
    await call.move("SEP16-0900-BACH")
    await call.move("SEP15-0900-BACH", old="A2")


async def partial_move_blocks_the_chart(call):
    await call.jane()
    ref = soonest(await call.move_search())[0]
    await call.move(ref)
    await call.move(ref)
    await call.cancel("A2")
    await call.cancel("A1")
    await call.book(ref)
    await call.search()
    await call.jane()


async def refused_move_reloads_on_the_next_search(call):
    await call.jane()
    ref = soonest(await call.move_search())[0]
    await call.move(ref)
    await call.jane()
    ref = soonest(await call.move_search())[0]
    await call.move(ref)
    await call.move(ref)


async def refused_move_labels(call):
    await call.jane()
    for _ in range(3):
        ref = soonest(await call.move_search())[0]
        await call.move(ref)


async def uncertain_move(call):
    await call.jane()
    ref = soonest(await call.move_search())[0]
    await call.move(ref)
    await call.move(ref)
    await call.cancel("A1")


async def cancel_and_replay(call):
    await call.jane()
    await call.cancel(read_back=None)
    await call.cancel()
    await call.cancel()
    await call.say("resolve_patient", firstName="Jane", dob=None)


async def cancel_survives_a_reload_that_still_lists_it(call):
    await call.jane()
    await call.cancel()
    await call.john()
    await call.jane()
    await call.cancel()


async def refused_cancel_revokes_until_reload(call):
    await call.jane()
    await call.cancel()
    await call.cancel()
    await call.jane()
    await call.cancel()


async def cancel_without_token_revokes(call):
    await call.jane()
    await call.cancel()
    await call.jane()
    await call.cancel()


async def uncertain_book_blocks_only_that_chart(call):
    await call.jane()
    ref = soonest(await call.search())[0]
    await call.book(ref)
    await call.book(ref)
    await call.cancel("A1")
    await call.john()
    ref = soonest(await call.search())[0]
    await call.book(ref)
    await call.jane()
    await call.cancel("A1")


async def uncertain_cancel_blocks_later_writes(call):
    await call.jane()
    await call.cancel()
    await call.cancel()
    ref = soonest(await call.search())[0]
    await call.book(ref)


async def patient_switch_during_book(call):
    gate = Gate(booking())
    call.script["/api/appointment/book"].append(gate)
    await call.jane()
    ref = soonest(await call.search())[0]
    pending = asyncio.create_task(call.book(ref))
    await gate.arrived.wait()
    await call.john()
    gate.release.set()
    await pending
    await call.search()
    await call.jane()
    await call.book(ref)


async def patient_switch_during_refused_move(call):
    gate = Gate({"status": "failed", "outcome": "slot_unavailable"})
    call.script["/api/appointment/reschedule"].append(gate)
    await call.jane()
    ref = soonest(await call.move_search())[0]
    pending = asyncio.create_task(call.move(ref))
    await gate.arrived.wait()
    await call.john()
    gate.release.set()
    await pending
    await call.say("resolve_patient", firstName="John", dob=None)
    await call.jane()


async def parallel_writes_send_once(call):
    gate = Gate(booking())
    call.script["/api/appointment/book"].append(gate)
    await call.jane()
    ref = soonest(await call.search())[0]
    first = asyncio.create_task(call.book(ref))
    await gate.arrived.wait()
    await call.book(ref)
    await call.cancel("A1")
    await call.search()
    gate.release.set()
    await first
    await call.book(ref)


async def insurance_update_replays_only_the_latest(call):
    await call.jane()
    found = await call.search()
    ref = soonest(found)[0]
    await call.update()
    await call.update()
    await call.book(ref)
    await call.move_search()
    await call.update("test-insurance")
    await call.update("aetna")
    await call.say("resolve_patient", firstName="Jane", dob=None)
    await call.search()


async def insurance_failure_changes_nothing(call):
    await call.jane()
    ref = soonest(await call.search())[0]
    await call.update()
    await call.book(ref)
    await call.update()
    await call.say("resolve_patient", firstName="Jane", dob=None)


async def insurance_partial_blocks_the_call(call):
    await call.jane()
    ref = soonest(await call.search())[0]
    await call.update()
    await call.update()
    await call.update("test-insurance")
    await call.book(ref)
    await call.search()
    await call.cancel()
    await call.john()
    await call.update()
    await call.say("add_patient", readBack=True, **{**INTAKE, "firstName": "Mary"})


async def insurance_uncertain_blocks_the_call(call):
    await call.jane()
    await call.update()
    await call.update()
    await call.search()
    await call.john()
    await call.update()


async def patient_switch_during_update(call):
    gate = Gate(updated())
    call.script["/api/patient/update-insurance"].append(gate)
    await call.jane()
    pending = asyncio.create_task(call.update())
    await gate.arrived.wait()
    await call.john()
    gate.release.set()
    await pending
    await call.jane()
    await call.update()


async def parallel_insurance_writes(call):
    gate = Gate(updated())
    call.script["/api/patient/update-insurance"].append(gate)
    await call.jane()
    ref = soonest(await call.search())[0]
    first = asyncio.create_task(call.update())
    await gate.arrived.wait()
    await call.update("test-insurance")
    await call.book(ref)
    await call.search()
    await call.cancel()
    gate.release.set()
    await first


async def registration_created(call):
    await call.say("add_patient", readBack=None, **INTAKE)
    await call.say("add_patient", readBack=True, **INTAKE)
    await call.say("add_patient", readBack=True, **INTAKE)
    await call.jane()
    ref = soonest(await call.search())[0]
    await call.book(ref)


async def registration_partial_then_update(call):
    await call.say("add_patient", readBack=True, **INTAKE)
    await call.jane()
    await call.search()
    await call.update()
    ref = soonest(await call.search())[0]
    await call.book(ref)
    await call.jane()


async def registration_uncertain_blocks_the_call(call):
    await call.say("add_patient", readBack=True, **INTAKE)
    await call.say("add_patient", readBack=True, **INTAKE)
    await call.say("add_patient", readBack=True, **{**INTAKE, "firstName": "Mary"})
    await call.john()
    await call.update()
    await call.search()


async def registration_failed_then_corrected(call):
    await call.say("add_patient", readBack=True, **INTAKE)
    await call.say("add_patient", readBack=True, **INTAKE)


async def patient_switch_during_registration(call):
    gate = Gate(created())
    call.script["/api/add-patient"].append(gate)
    pending = asyncio.create_task(call.say("add_patient", readBack=True, **INTAKE))
    await gate.arrived.wait()
    await call.john()
    gate.release.set()
    await pending
    await call.say("add_patient", readBack=True, **INTAKE)
    await call.jane()


async def book_raises_and_fails_the_drain(call):
    await call.jane()
    ref = soonest(await call.search())[0]
    with patch.object(
        call.scheduling.http, "book", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        await call.book(ref)
    await call.book(ref)
    await call.cancel("A1")


async def update_raises_and_fails_the_drain(call):
    await call.jane()
    with patch.object(
        call.registration, "update", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        await call.update()
    await call.update()
    await call.search()


async def time_alone_does_not_revoke(call):
    await call.jane()
    ref = soonest(await call.move_search())[0]
    call.at = NOW + timedelta(minutes=28)
    await call.move_search()
    await call.move(ref)
    await call.say("resolve_patient", firstName="Jane", dob=None)
    await call.cancel("A2")


async def rejected_token_reloads_once(call):
    await call.jane()
    await call.move_search()
    await call.move_search(startDate="2026-09-20")


async def ambiguous_repeat_keeps_the_chart_and_names_the_claim(call):
    call.resolver.start_phone_lookup()
    await call.jane()
    await call.say("resolve_patient", firstName="Jane", dob=None)
    await call.say(
        "save_staff_task",
        category="other",
        urgency="normal",
        summary="Call back",
        message="Please call back",
    )
    await call.cancel()
    await call.jane()
    await call.say(
        "save_staff_task",
        category="other",
        urgency="normal",
        summary="Second request",
        message="Another need",
    )


async def repeat_resolve_keeps_a_fuzzy_phone_name(call):
    call.resolver.start_phone_lookup()
    await call.say("resolve_patient", firstName="Jon", dob=None)
    await call.say("resolve_patient", firstName="Jon", dob=None)
    await call.say("resolve_patient", firstName="Bob", dob=None)
    await call.say("resolve_patient", firstName=None, dob="03/03/1990")
    await call.say(
        "save_staff_task",
        category="other",
        urgency="normal",
        summary="Unmatched",
        message="Caller not found",
    )
    await call.say("resolve_patient", firstName="John", dob=JOHN)


async def phone_receipt_is_used_once(call):
    call.resolver.start_phone_lookup()
    await call.say("resolve_patient", firstName="Jane", dob=None)
    await call.john()
    await call.say("resolve_patient", firstName="Jane", dob=None)


async def closed_call_refuses_everything(call):
    await call.jane()
    ref = soonest(await call.search())[0]
    await call.drain()
    await call.book(ref)
    await call.move(ref)
    await call.cancel()
    await call.search()
    await call.update()
    await call.say("add_patient", readBack=True, **INTAKE)
    await call.jane()
    await call.say(
        "save_staff_task",
        category="other",
        urgency="normal",
        summary="Too late",
        message="Too late",
    )


async def no_patient_yet(call):
    await call.update()
    await call.book("SEP15-0900-BACH")
    await call.cancel()
    await call.move("SEP15-0900-BACH")
    await call.search()
    await call.say("resolve_patient", firstName=None, dob=None)
    await call.say("resolve_patient", firstName="Jane", dob="not a date")


async def a_new_load_replaces_coverage_written_before_it(call):
    await call.jane()
    await call.update()
    await call.say("resolve_patient", firstName="Jane", dob=None)
    await call.cancel()
    await call.jane()


async def reload_keeps_offers_and_a_switch_voids_them(call):
    await call.jane()
    ref = soonest(await call.search())[0]
    await call.cancel()
    await call.jane()
    await call.book(ref, read_back=None)
    await call.john()
    await call.jane()
    await call.book(ref, read_back=None)


async def patient_switch_during_search(call):
    gate = Gate(inventory())
    call.script["/api/scheduler/slots"].append(gate)
    await call.jane()
    pending = asyncio.create_task(call.search())
    await gate.arrived.wait()
    await call.john()
    gate.release.set()
    await pending


async def update_during_search(call):
    gate = Gate(inventory())
    call.script["/api/scheduler/slots"].append(gate)
    await call.jane()
    pending = asyncio.create_task(call.search())
    await gate.arrived.wait()
    await call.update()
    gate.release.set()
    await pending


async def read_under_way_is_discarded_by_a_booking(call):
    gate = Gate(chart())
    await call.jane()
    ref = soonest(await call.search())[0]
    await call.cancel()
    call.script["/api/patient/resolve"][:0] = [gate]
    pending = asyncio.create_task(call.jane())
    await gate.arrived.wait()
    await call.book(ref)
    gate.release.set()
    await pending
    await call.say("resolve_patient", firstName="Jane", dob=None)


async def registration_leaves_the_chart_it_was_on(call):
    await call.jane()
    await call.say("add_patient", readBack=None, **INTAKE)
    await call.say("add_patient", readBack=None, **{**INTAKE, "firstName": "Mary"})
    await call.say(
        "save_staff_task",
        category="other",
        urgency="normal",
        summary="New patient",
        message="Registration in progress",
    )
    await call.cancel()
    await call.jane()


async def move_search_refusals(call):
    await call.jane()
    await call.move_search(ref="A9")
    await call.move_search(visitType="routine_vision")
    await call.search(insurancePlanId=None)
    await call.search(insurancePlanId="not-a-plan")
    await call.search(startDate="2026-09-14")
    await call.search(startDate="soon")
    await call.move_search()
    await call.jane()


async def switch_back_reads_a_fresh_chart(call):
    await call.jane()
    ref = soonest(await call.search())[0]
    await call.john()
    await call.book(ref)
    await call.jane()
    await call.book(ref)


async def read_under_way_is_discarded_by_a_settled_write(call):
    gate = Gate(chart())
    await call.jane()
    await call.cancel(ref="A9")
    call.script["/api/patient/resolve"][:0] = [gate]
    pending = asyncio.create_task(call.jane())
    await gate.arrived.wait()
    await call.update()
    gate.release.set()
    await pending
    await call.say("resolve_patient", firstName="Jane", dob=None)


async def read_under_way_survives_a_refused_booking(call):
    gate = Gate(chart())
    await call.say("resolve_patient", firstName="Jane", dob=JANE)
    call.script["/api/patient/resolve"][:0] = [gate]
    ref = soonest(await call.search())[0]
    pending = asyncio.create_task(
        call.say("resolve_patient", firstName="Jane", dob=JANE)
    )
    await gate.arrived.wait()
    await call.book(ref)
    gate.release.set()
    await pending


FLOWS = {
    "book_replay_and_repeat": (
        book_replay_and_repeat,
        dict(resolve=[chart()], slots=[inventory()], book=[booking()]),
    ),
    "book_cancel_then_rebook": (
        book_cancel_then_rebook,
        dict(
            resolve=[chart()],
            slots=[inventory(), inventory()],
            book=[booking(), booking(889)],
            cancel=[{"status": "cancelled", "appointmentId": 888}],
        ),
    ),
    "book_refused_then_retry": (
        book_refused_then_retry,
        dict(
            resolve=[chart()],
            slots=[inventory(), inventory()],
            book=[
                {"status": "error", "outcome": "slot_unavailable", "message": "Taken"},
                booking(),
            ],
        ),
    ),
    "book_refusal_labels": (
        book_refusal_labels,
        dict(
            resolve=[chart()],
            slots=[inventory(), inventory(), inventory(), inventory()],
            book=[
                {"status": "error", "outcome": "invalid_booking_token"},
                {"status": "error", "outcome": "appointment_type_unresolved"},
                {"status": "error", "outcome": "write_failed", "message": "No write"},
                {"status": "error", "outcome": "indeterminate_write"},
            ],
        ),
    ),
    "move_and_replay": (
        move_and_replay,
        dict(resolve=[chart()], slots=[inventory()], move=[rescheduled()]),
    ),
    "move_with_a_plan_then_other_slot": (
        move_with_a_plan_then_other_slot,
        dict(
            resolve=[chart()],
            slots=[inventory(allowedProviders=["Dr. Bach"])],
            move=[rescheduled()],
        ),
    ),
    "partial_move_blocks_the_chart": (
        partial_move_blocks_the_chart,
        dict(
            resolve=[chart()],
            slots=[inventory(), inventory()],
            move=[rescheduled("partial")],
        ),
    ),
    "refused_move_reloads_on_the_next_search": (
        refused_move_reloads_on_the_next_search,
        dict(
            resolve=[chart(), chart()],
            slots=[inventory(), inventory()],
            move=[{"status": "failed", "outcome": "slot_unavailable"}, rescheduled()],
        ),
    ),
    "refused_move_labels": (
        refused_move_labels,
        dict(
            resolve=[chart(), chart(), chart()],
            slots=[inventory(), inventory(), inventory()],
            move=[
                {"status": "failed", "outcome": "invalid_reschedule_token"},
                {
                    "status": "failed",
                    "outcome": "ownership_mismatch",
                    "message": "Changed",
                },
                {"status": "failed", "outcome": "write_failed"},
            ],
        ),
    ),
    "uncertain_move": (
        uncertain_move,
        dict(resolve=[chart()], slots=[inventory()], move=[failure()]),
    ),
    "cancel_and_replay": (
        cancel_and_replay,
        dict(
            resolve=[chart()],
            cancel=[{"status": "cancelled", "appointmentId": 77}],
        ),
    ),
    "cancel_survives_a_reload_that_still_lists_it": (
        cancel_survives_a_reload_that_still_lists_it,
        dict(
            resolve=[chart(), john_chart(), chart()],
            cancel=[{"status": "cancelled", "appointmentId": 77}],
        ),
    ),
    "refused_cancel_revokes_until_reload": (
        refused_cancel_revokes_until_reload,
        dict(
            resolve=[chart(), chart()],
            cancel=[
                {"status": "error", "outcome": "provider_rejected", "message": "No"},
                {"status": "cancelled", "appointmentId": 77},
            ],
        ),
    ),
    "cancel_without_token_revokes": (
        cancel_without_token_revokes,
        dict(
            resolve=[
                receipt(
                    appointmentsStatus="found",
                    appointments=[appointment(cancellationToken=None)],
                ),
                chart(),
            ],
            cancel=[{"status": "cancelled", "appointmentId": 77}],
        ),
    ),
    "uncertain_book_blocks_only_that_chart": (
        uncertain_book_blocks_only_that_chart,
        dict(
            resolve=[chart(), john_chart(), chart()],
            slots=[inventory(), inventory()],
            book=[failure(), booking(901)],
        ),
    ),
    "uncertain_cancel_blocks_later_writes": (
        uncertain_cancel_blocks_later_writes,
        dict(resolve=[chart()], slots=[inventory()], cancel=[failure()]),
    ),
    "patient_switch_during_book": (
        patient_switch_during_book,
        dict(
            resolve=[chart(), john_chart(), chart()],
            slots=[inventory(), inventory()],
        ),
    ),
    "patient_switch_during_refused_move": (
        patient_switch_during_refused_move,
        dict(
            resolve=[chart(), john_chart(), chart()],
            slots=[inventory()],
        ),
    ),
    "parallel_writes_send_once": (
        parallel_writes_send_once,
        dict(resolve=[chart()], slots=[inventory()]),
    ),
    "insurance_update_replays_only_the_latest": (
        insurance_update_replays_only_the_latest,
        dict(
            resolve=[chart()],
            slots=[inventory(), inventory()],
            update=[
                updated(),
                updated(newInsurance="Test Insurance"),
                updated(),
            ],
        ),
    ),
    "insurance_failure_changes_nothing": (
        insurance_failure_changes_nothing,
        dict(
            resolve=[chart()],
            slots=[inventory()],
            book=[booking()],
            update=[
                {"status": "error", "effect": "no_effect", "message": "Refused"},
                {"status": "error", "effect": "no_effect", "outcome": "rejected"},
            ],
        ),
    ),
    "insurance_partial_blocks_the_call": (
        insurance_partial_blocks_the_call,
        dict(
            resolve=[chart(), john_chart()],
            slots=[inventory()],
            cancel=[{"status": "cancelled", "appointmentId": 77}],
            update=[{"status": "error", "effect": "partial", "message": "Half"}],
        ),
    ),
    "insurance_uncertain_blocks_the_call": (
        insurance_uncertain_blocks_the_call,
        dict(resolve=[chart(), john_chart()], update=[failure()]),
    ),
    "patient_switch_during_update": (
        patient_switch_during_update,
        dict(resolve=[chart(), john_chart(), chart()]),
    ),
    "parallel_insurance_writes": (
        parallel_insurance_writes,
        dict(
            resolve=[chart()],
            slots=[inventory()],
            cancel=[{"status": "cancelled", "appointmentId": 77}],
        ),
    ),
    "registration_created": (
        registration_created,
        dict(
            create=[created(patientId="chart-jane")],
            slots=[inventory()],
            book=[booking()],
        ),
    ),
    "registration_partial_then_update": (
        registration_partial_then_update,
        dict(
            create=[created("partial", patientId="chart-jane")],
            slots=[inventory()],
            update=[updated()],
            book=[booking()],
        ),
    ),
    "registration_uncertain_blocks_the_call": (
        registration_uncertain_blocks_the_call,
        dict(resolve=[john_chart()], create=[failure()]),
    ),
    "registration_failed_then_corrected": (
        registration_failed_then_corrected,
        dict(
            create=[
                {"status": "error", "outcome": "rejected", "message": "Bad"},
                created(),
            ]
        ),
    ),
    "patient_switch_during_registration": (
        patient_switch_during_registration,
        dict(resolve=[john_chart(), receipt("new-chart")]),
    ),
    "book_raises_and_fails_the_drain": (
        book_raises_and_fails_the_drain,
        dict(resolve=[chart()], slots=[inventory()]),
    ),
    "update_raises_and_fails_the_drain": (
        update_raises_and_fails_the_drain,
        dict(resolve=[chart()]),
    ),
    "time_alone_does_not_revoke": (
        time_alone_does_not_revoke,
        dict(
            resolve=[chart()],
            slots=[inventory(), inventory()],
            move=[rescheduled()],
            cancel=[{"status": "cancelled", "appointmentId": 888}],
        ),
    ),
    "rejected_token_reloads_once": (
        rejected_token_reloads_once,
        dict(
            resolve=[chart(), chart(), chart()],
            slots=[
                inventory(
                    outcome="invalid_reschedule_token",
                    status="error",
                    slots=[],
                    message="Expired",
                ),
                inventory(),
                inventory(
                    outcome="invalid_reschedule_token",
                    status="error",
                    slots=[],
                    message="Expired",
                ),
                inventory(
                    outcome="invalid_reschedule_token",
                    status="error",
                    slots=[],
                    message="Expired",
                ),
            ],
        ),
    ),
    "ambiguous_repeat_keeps_the_chart_and_names_the_claim": (
        ambiguous_repeat_keeps_the_chart_and_names_the_claim,
        dict(
            resolve=[
                {
                    "status": "multiple_matches",
                    "matches": [
                        candidate(),
                        candidate("chart-jane-2", "Jane", "05/06/2010"),
                    ],
                },
                chart(),
            ],
            cancel=[{"status": "cancelled", "appointmentId": 77}],
        ),
    ),
    "repeat_resolve_keeps_a_fuzzy_phone_name": (
        repeat_resolve_keeps_a_fuzzy_phone_name,
        dict(
            resolve=[
                john_chart(),
                search(),
                john_chart(),
            ],
        ),
    ),
    "phone_receipt_is_used_once": (
        phone_receipt_is_used_once,
        dict(resolve=[chart(), john_chart(), chart()]),
    ),
    "closed_call_refuses_everything": (
        closed_call_refuses_everything,
        dict(resolve=[chart()], slots=[inventory()]),
    ),
    "no_patient_yet": (no_patient_yet, dict()),
    "a_new_load_replaces_coverage_written_before_it": (
        a_new_load_replaces_coverage_written_before_it,
        dict(
            resolve=[
                chart(insuranceCarrierId="car1", insuranceNote="Check the card"),
                chart(insuranceCarrierId="car1", insuranceNote="Check the card"),
            ],
            update=[updated()],
            cancel=[{"status": "error", "outcome": "provider_rejected"}],
        ),
    ),
    "reload_keeps_offers_and_a_switch_voids_them": (
        reload_keeps_offers_and_a_switch_voids_them,
        dict(
            resolve=[tokenless_chart(), chart(), john_chart(), chart()],
            slots=[inventory()],
        ),
    ),
    "patient_switch_during_search": (
        patient_switch_during_search,
        dict(resolve=[chart(), john_chart()]),
    ),
    "update_during_search": (
        update_during_search,
        dict(resolve=[chart()], update=[updated()]),
    ),
    "read_under_way_is_discarded_by_a_booking": (
        read_under_way_is_discarded_by_a_booking,
        dict(
            resolve=[tokenless_chart(), chart()],
            slots=[inventory()],
            book=[booking()],
        ),
    ),
    "registration_leaves_the_chart_it_was_on": (
        registration_leaves_the_chart_it_was_on,
        dict(resolve=[chart(), chart()]),
    ),
    "move_search_refusals": (
        move_search_refusals,
        dict(resolve=[chart()], slots=[inventory()]),
    ),
    "switch_back_reads_a_fresh_chart": (
        switch_back_reads_a_fresh_chart,
        dict(
            resolve=[chart(), john_chart(), chart()],
            slots=[inventory()],
        ),
    ),
    "read_under_way_is_discarded_by_a_settled_write": (
        read_under_way_is_discarded_by_a_settled_write,
        dict(
            resolve=[receipt(appointmentsStatus="error", appointments=[]), chart()],
            update=[updated()],
        ),
    ),
    "read_under_way_survives_a_refused_booking": (
        read_under_way_survives_a_refused_booking,
        dict(
            resolve=[receipt(appointmentsStatus="error", appointments=[])],
            slots=[inventory()],
            book=[{"status": "error", "outcome": "slot_unavailable"}],
        ),
    ),
}


class CallPinTests(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    async def run_flow(self, name):
        flow, script = FLOWS[name]
        drafts = (f"draft-{n}" for n in range(9))
        with (
            patch("abita_s2s.runtime.reporting.datetime", FrozenClock),
            patch("abita_s2s.runtime.reporting.AGENT_VERSION", "pinned"),
            patch(
                "abita_s2s.runtime.reporting.evaluate_call",
                AsyncMock(return_value={"status": "pinned"}),
            ),
            patch(
                "abita_s2s.staff_tasks.uuid4",
                lambda: SimpleNamespace(hex=next(drafts)),
            ),
        ):
            call = Call(**script)
            async with asyncio.timeout(5):
                await flow(call)
                return await call.finish()

    async def test_every_flow_matches_its_golden_bytes(self):
        results = {}
        for name in FLOWS:
            with self.subTest(flow=name):
                results[name] = await self.run_flow(name)
        if os.environ.get("WRITE_PINS"):
            PINS.write_text(json.dumps(results, indent=1) + "\n")
        golden = json.loads(PINS.read_text())
        self.assertEqual(set(results), set(golden))
        for name, result in results.items():
            with self.subTest(flow=name):
                self.assertEqual(result, golden[name])
