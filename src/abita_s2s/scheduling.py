"""One per-call scheduling owner: private references, inventory and write receipts."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolFlag

from abita_s2s.insurance_state import insurance_ready
from abita_s2s.middleware import Appointment
from abita_s2s.offices import get_office_profile
from abita_s2s.scheduling_http import (
    SchedulingFailure,
    SchedulingHTTP,
    Slot,
    WriteReceipt,
)
from abita_s2s.state import CallState

VisitType = Literal["medical", "routine_vision"]
MEDICAL_TYPES = {1004, 1005, 1006, 1007, 1008, 6167, 6168, 6169}
ROUTINE_TYPES = {1010, 3364, 4244, 4245}
NEW_TYPES = {1004, 1006, 1010, 4244, 6167}
ESTABLISHED_TYPES = {1005, 1007, 3364, 4245, 6169}
EASTERN = ZoneInfo("America/New_York")


def reply(outcome, answer, **facts):
    return {"outcome": outcome, "answer": answer, **facts}


def provider_name(name):
    for old, new in (
        ("Dr. Austin Bach (Overflow)", "Dr. Bach"),
        ("Dr. Austin Bach", "Dr. Bach"),
        ("Dr. J. Licht", "Dr. Licht"),
        ("Dr. D. Noel", "Dr. Noel"),
    ):
        name = name.replace(old, new)
    return name


def visit_type(appointment):
    if appointment.appointmentTypeId in MEDICAL_TYPES:
        return "medical"
    if appointment.appointmentTypeId in ROUTINE_TYPES:
        return "routine_vision"
    return None


@dataclass(repr=False)
class OfferedSlot:
    slot: Slot
    context: tuple
    office: str
    visit: VisitType
    expires: datetime


@dataclass(repr=False)
class MutationReceipt:
    result: dict
    booked: Appointment | None = None
    cancelled_id: int | None = None


@dataclass(repr=False)
class PendingRead:
    key: tuple
    task: asyncio.Task
    waiters: int = 0


class Scheduling:
    def __init__(self, state: CallState, http: SchedulingHTTP, *, now=None):
        self.state = state
        self.http = http
        self.now = now or (lambda: datetime.now(UTC))
        self._slots: dict[str, OfferedSlot] = {}
        self._references: dict[tuple, str] = {}
        self._next_ref = 0
        self._generation = 0
        self._search_key = None
        self._search: PendingRead | None = None
        self._read_tasks = set()
        self._cache = None
        self._failures = {}
        self._write_task = None
        self._closed = False
        self._receipts: dict[tuple, MutationReceipt] = {}

    @property
    def tools(self):
        return [
            self.list_available_appointments,
            self.book_appointment,
            self.cancel_appointment,
            self.reschedule_appointment,
        ]

    def close_admission(self) -> None:
        self._closed = True

    async def aclose(self):
        self._closed = True
        for task in self._read_tasks:
            task.cancel()
        await asyncio.gather(*self._read_tasks, return_exceptions=True)
        # A sent mutation must reach reconciliation even if its caller disappears.
        if self._write_task:
            await asyncio.shield(self._write_task)

    def _context(self):
        p = self.state.patient.active
        return (
            self.state.patient.revision,
            p.patientId if p else None,
            p.dob if p else None,
            p.routing if p else None,
            p.preauthRequired if p else None,
            p.routingAmbiguous if p else None,
            p.insuranceCarrier if p else None,
            (self.state.call.called_office_key, p.patientId)
            in self.state.insurance.checked_patients
            if p else False,
            self.state.insurance.accepted,
            id(self.state.insurance.accepted),
            self.state.insurance.write_pending,
            self.state.insurance.write_uncertain,
            self.state.insurance.registrations.get(p.patientId) if p else None,
        )

    def _reference(self, appointment):
        p = self.state.patient
        key = (p.revision, p.active.patientId, appointment.id)
        if key not in self._references:
            self._next_ref += 1
            self._references[key] = f"A{self._next_ref}"
        return self._references[key]

    def appointments(self):
        self._reconcile_receipts()
        p = self.state.patient.active
        if not p or p.appointmentsStatus != "found":
            return []
        return [
            {
                "appointmentRef": self._reference(a),
                "date": a.date,
                "time": a.time,
                "provider": provider_name(a.provider),
                "facility": a.facility,
                "visitType": visit_type(a),
            }
            for a in p.appointments
        ]

    def _finish(self, result):
        return {**result, "appointments": self.appointments()}

    def _invalidate(self):
        self._generation += 1
        self._slots.clear()
        self._cache = None
        self._search_key = None

    def _select(self, ref):
        p = self.state.patient.active
        if not p or p.appointmentsStatus != "found":
            return None
        matches = [a for a in p.appointments if self._reference(a) == ref.strip()]
        return matches[0] if len(matches) == 1 else None

    def _office(self, requested):
        called = self.state.call.called_office_key
        if called in ("hollywood", "sweetwater"):
            if requested not in ("hollywood", "sweetwater"):
                return None
            return requested
        return called if requested is None else None

    @function_tool(flags=ToolFlag.CANCELLABLE)
    async def list_available_appointments(
        self,
        context: RunContext[CallState],
        visitType: VisitType,
        startDate: str | None = None,
        office: Literal["hollywood", "sweetwater"] | None = None,
    ) -> str:
        """Load eligible appointments after triage for a 14-calendar-day Eastern-time window.

        Offer only returned slots, at most two at a time. Reuse the loaded list for preferences
        within its window. For Hollywood/Sweetwater calls first ask which office they want.
        Args:
            visitType: Caller's medical or routine_vision visit, including reschedules.
            startDate: YYYY-MM-DD, tomorrow or later; null means tomorrow. Search later using the day after the window ends.
            office: Caller-selected Hollywood or Sweetwater; null for other offices.
        """
        if context.userdata is not self.state or self._closed:
            return json.dumps(reply("unavailable", "Scheduling is unavailable."))
        return json.dumps(
            self._finish(await self.availability(visitType, startDate, office))
        )

    async def availability(self, visit, start=None, office=None):
        if self._closed:
            return reply("unavailable", "Scheduling has closed for this call.")
        context = self._context()
        selected = self._office(office)
        today = self.now().astimezone(EASTERN).date()
        try:
            first = date.fromisoformat(start) if start else today + timedelta(days=1)
            if start and first.isoformat() != start:
                raise ValueError()
        except ValueError:
            self._invalidate()
            return reply("needs_input", "Provide the requested date as YYYY-MM-DD.")
        if first <= today:
            self._invalidate()
            return reply(
                "needs_input",
                "Same-day and past dates cannot be scheduled here. Ask whether tomorrow or later works; do not silently change the date.",
            )
        if selected is None:
            self._invalidate()
            return reply(
                "needs_input",
                "Ask for Hollywood or Sweetwater on those office calls; omit office for other calls.",
            )
        if (selected == "crystal-river" and visit == "routine_vision") or (
            selected == "north-miami-beach-optical" and visit == "medical"
        ):
            self._invalidate()
            return reply(
                "unsupported",
                "This office does not schedule that visit type. Use an appropriate office or ask staff for help.",
            )
        if visit not in ("medical", "routine_vision") or not insurance_ready(
            self.state, visit
        ):
            self._invalidate()
            return reply(
                "needs_input",
                "Verify or finish patient registration and resolve insurance acceptance, routing and authorization requirements before scheduling.",
            )
        if self._write_task and not self._write_task.done():
            return reply(
                "in_progress",
                "An appointment change is in progress. Wait for its result.",
            )
        p = self.state.patient.active
        routing = "optical_only" if visit == "routine_vision" else p.routing
        body = {
            "office": get_office_profile(selected).trunk_numbers[0],
            "startDate": first.isoformat(),
            "rangeDays": 14,
            "dob": p.dob,
        }
        if routing:
            body["routing"] = routing
        if p.preauthRequired:
            body["preauthRequired"] = True
        key = (context, selected, visit, first, today)
        if self._search_key != key:
            self._invalidate()
            self._search_key = key
        if self._cache and self._cache[0] == key and self._cache[1] > self.now():
            return self._cache[2]
        failed = self._failures.get(key)
        if failed and (failed[0] >= 2 or not failed[1]):
            return reply(
                "availability_failed",
                "Availability could not be verified. Do not describe this as no openings or retry this search; ask staff for help.",
            )
        if self._search and not self._search.task.done() and self._search.key == key:
            pending = self._search
        else:
            task = asyncio.create_task(self._load(body, key, self._generation))
            pending = self._search = PendingRead(key, task)
            self._read_tasks.add(task)
            task.add_done_callback(self._read_tasks.discard)
        pending.waiters += 1
        try:
            return await asyncio.shield(pending.task)
        finally:
            pending.waiters -= 1
            # A cancelled waiter cannot stop another waiter, but abandoned reads
            # must not publish later after the caller corrects their request.
            if pending.waiters == 0 and not pending.task.done():
                pending.task.cancel()
                if self._search is pending:
                    self._search = None
                    self._invalidate()

    async def _load(self, body, key, generation, *, refreshed=False):
        result = await self.http.availability(body)
        if generation != self._generation or key[0] != self._context():
            return reply(
                "stale",
                "The patient or appointment details changed. Check again with current details.",
            )
        previous = {item.slot.key: (ref, item) for ref, item in self._slots.items()}
        self._slots.clear()
        if (
            isinstance(result, SchedulingFailure)
            or result.outcome == "availability_search_incomplete"
            or result.status == "error"
        ):
            retry = (
                isinstance(result, SchedulingFailure) or result.shouldRetrySameSearch
            )
            count = self._failures.get(key, (0, False))[0] + 1
            self._failures[key] = (count, retry)
            return reply(
                "availability_failed",
                "Availability could not be verified; this does not mean no openings.",
                retry_same_search=retry and count < 2,
            )
        self._failures.pop(key, None)
        first = date.fromisoformat(body["startDate"])
        through = (first + timedelta(days=13)).isoformat()
        if (
            result.outcome in ("no_availability", "no_eligible_providers")
            and not result.slots
        ):
            answer = reply(
                "none",
                "No eligible openings in the searched window. Ask what other dates work.",
                searchedFrom=first.isoformat(),
                searchedThrough=through,
            )
            self._cache = (key, self.now() + timedelta(seconds=60), answer)
            return answer
        try:
            expiry = datetime.fromisoformat(result.bookingTokenExpiresAt or "")
            if expiry.tzinfo is None:
                raise ValueError()
            if expiry <= self.now():
                if not refreshed:
                    return await self._load(body, key, generation, refreshed=True)
                self._failures[key] = (2, False)
                return reply(
                    "availability_failed",
                    "Openings expired twice before they could be offered. Ask staff for help.",
                )
        except ValueError:
            return reply(
                "availability_failed",
                "Openings expired or could not be verified. Search again before offering or booking.",
            )
        if (
            not result.slots
            or result.outcome != "availability_found"
            or any(
                not slot.bookingToken or not slot.bookingToken.strip()
                for slot in result.slots
            )
        ):
            return reply(
                "availability_failed",
                "Availability returned an invalid result. Ask staff for help.",
            )
        unique = {}
        for slot in result.slots:
            if not first.isoformat() <= slot.date <= through:
                return reply(
                    "availability_failed",
                    "Availability returned dates outside the requested window. Ask staff for help.",
                )
            unique[slot.key] = slot
        for slot in unique.values():
            prior = previous.get(slot.key)
            if (
                prior
                and prior[1].context == key[0]
                and prior[1].office == key[1]
                and prior[1].visit == key[2]
            ):
                ref = prior[0]
            else:
                self._next_ref += 1
                ref = f"S{self._next_ref}"
            self._slots[ref] = OfferedSlot(slot, key[0], key[1], key[2], expiry)
        answer = reply(
            "found",
            "Offer at most two returned choices at a time. All times are Eastern. Use these references only after caller confirmation; references are private.",
            searchedFrom=first.isoformat(),
            searchedThrough=through,
            slots=[
                {
                    "appointmentSlotRef": ref,
                    "date": item.slot.date,
                    "time": item.slot.time,
                    "provider": provider_name(item.slot.provider),
                }
                for ref, item in self._slots.items()
            ],
        )
        self._cache = (key, min(expiry, self.now() + timedelta(seconds=60)), answer)
        return answer

    @function_tool
    async def book_appointment(
        self,
        context: RunContext[CallState],
        appointmentSlotRef: str,
        appointmentReason: str,
        referringDoctor: str,
        readBack: bool | None,
    ) -> str:
        """Book a new appointment using a returned slot after caller confirmation of date, time and provider.

        Ask who referred the caller; referringDoctor is 'none' only if they say no doctor referred them.
        Record a routine purpose or a symptom/concern plus one useful caller-provided detail; never diagnose.
        readBack is true only after confirmation. Claim success only from this result. Do not retry uncertain writes.
        Use reschedule_appointment to move an existing appointment.
        """
        return await self._execute(
            context,
            "book",
            appointmentSlotRef,
            appointmentReason,
            referringDoctor,
            readBack,
        )

    @function_tool
    async def cancel_appointment(
        self, context: RunContext[CallState], appointmentRef: str
    ) -> str:
        """Cancel only after verification and the caller confirms cancellation of the exact loaded appointment.

        Use its private appointmentRef. Claim success only from the result; never retry uncertain cancellation.
        """
        return await self._execute(context, "cancel", old_ref=appointmentRef)

    @function_tool
    async def reschedule_appointment(
        self,
        context: RunContext[CallState],
        oldAppointmentRef: str,
        appointmentSlotRef: str,
        appointmentReason: str,
        referringDoctor: str,
        readBack: bool | None,
    ) -> str:
        """Move the caller-confirmed loaded appointment to a confirmed returned slot.

        Confirm the old appointment and read back the new date, time and provider before readBack=true.
        Ask who referred the caller; use 'none' only when they say no doctor referred them.
        Books first, then cancels the old visit. Report partial success and never repeat an uncertain booking.
        """
        return await self._execute(
            context,
            "reschedule",
            appointmentSlotRef,
            appointmentReason,
            referringDoctor,
            readBack,
            oldAppointmentRef,
        )

    async def _execute(
        self,
        context,
        action,
        slot_ref=None,
        reason=None,
        referrer=None,
        confirmed=None,
        old_ref=None,
    ):
        if context.userdata is not self.state or self._closed:
            return json.dumps(reply("unavailable", "Scheduling is unavailable."))
        if self._write_task and not self._write_task.done():
            return json.dumps(
                reply(
                    "in_progress",
                    "An appointment change is already in progress. Wait for its result; do not repeat it.",
                )
            )
        # LiveKit tools are not cancellable. Shield also covers explicit session shutdown.
        self._write_task = asyncio.create_task(
            self._change(action, slot_ref, reason, referrer, confirmed, old_ref, context.function_call.call_id)
        )
        return json.dumps(self._finish(await asyncio.shield(self._write_task)))

    async def _change(self, action, slot_ref, reason, referrer, confirmed, old_ref, call_id):
        p = self.state.patient.active
        captured = self._context()
        if not p:
            return reply(
                "needs_input", "Verify the patient before changing appointments."
            )
        # Unknown writes and partial moves block further mutations for this patient.
        for (patient_id, _, _), receipt in self._receipts.items():
            if patient_id == p.patientId and receipt.result["outcome"] in (
                "uncertain",
                "partial_reschedule",
            ):
                return receipt.result
        if action == "book" and (p.patientId, "book", None) in self._receipts:
            return self._receipts[p.patientId, "book", None].result
        old = self._select(old_ref) if old_ref else None
        if action != "book" and old is None:
            # Exact completed reference replay is safe, including after removal from active appointments.
            for (_, patient_id, appointment_id), ref in self._references.items():
                if patient_id == p.patientId and ref == old_ref:
                    saved = self._receipts.get((patient_id, action, appointment_id))
                    if saved:
                        return saved.result
            return reply(
                "needs_input",
                "Choose and confirm the exact currently loaded appointment. Reload patient appointments if needed.",
            )
        receipt_key = (p.patientId, action, old.id if old else None)
        saved = self._receipts.get(receipt_key)
        selected = self._slots.get((slot_ref or "").strip().upper())
        # A caller may intentionally move the replacement to a different returned slot.
        different_move = (
            action == "reschedule"
            and saved
            and saved.booked
            and selected
            and selected.context == captured
            and (
                selected.slot.date,
                selected.slot.time,
                provider_name(selected.slot.provider),
            )
            != (saved.booked.date, saved.booked.time, saved.booked.provider)
        )
        if saved and not different_move:
            return saved.result
        if action == "cancel":
            self._invalidate()
            self._receipts[receipt_key] = MutationReceipt(
                self._write_failure(
                    SchedulingFailure(reason="pending", uncertain=True), "cancellation"
                )
            )
            result = await self.http.cancel(self._cancel_body(p, old))
            outcome = self._cancel_result(result)
            self._report(p, cancellation=result, old=old, call_id=call_id)
            if outcome["outcome"] == "cancelled":
                self._remove(p, old, captured)
            elif (
                isinstance(result, WriteReceipt)
                and result.outcome == "invalid_cancellation_token"
                and self._context() == captured
            ):
                self.state.patient.active = p.model_copy(
                    update={"appointments": [], "appointmentsStatus": "error"}
                )
            if outcome["outcome"] in ("cancelled", "uncertain"):
                self._receipts[receipt_key] = MutationReceipt(
                    outcome,
                    cancelled_id=old.id if outcome["outcome"] == "cancelled" else None,
                )
            else:
                self._receipts.pop(receipt_key, None)
            return self._patient_changed(outcome, captured)
        offered = self._slots.get((slot_ref or "").strip().upper())
        if not offered or offered.context != captured or offered.expires <= self.now():
            self._invalidate()
            return reply(
                "needs_input",
                "Search availability again and choose a current returned slot.",
            )
        if not insurance_ready(self.state, offered.visit):
            self._invalidate()
            return reply(
                "needs_input",
                "Resolve registration, insurance acceptance and authorization requirements before booking.",
            )
        if old and visit_type(old) is None:
            return reply(
                "needs_staff_review",
                "The existing appointment's visit type could not be verified. Ask staff to reschedule it; no appointment was changed.",
            )
        if old and visit_type(old) != offered.visit:
            return reply(
                "needs_input",
                f"Load {visit_type(old)} availability to match the existing appointment.",
            )
        if (
            not reason
            or not reason.strip()
            or re.fullmatch(
                r"appointment|appt|visit|office visit|booking|(?:my )?eyes?|(?:my )?eye (?:exam|issues?|problems?|concerns?)",
                reason.strip(),
                re.IGNORECASE,
            )
        ):
            return reply(
                "needs_input",
                "Ask for the routine purpose or symptom/concern plus one useful caller detail. If the caller cannot add details, record that limitation.",
            )
        if not referrer or not referrer.strip():
            return reply(
                "needs_input",
                "Ask whether a doctor referred the caller and get their name; use 'none' only if the caller says no.",
            )
        slot = offered.slot
        description = (
            f"{slot.date} at {slot.time} Eastern with {provider_name(slot.provider)}"
        )
        if confirmed is not True:
            return reply(
                "needs_confirmation",
                f"Confirm {description} with the caller before booking.",
            )
        status = (
            "new"
            if p.patientId in self.state.insurance.registrations
            else "established"
        )
        if old:
            if old.appointmentTypeId in NEW_TYPES:
                status = "new"
            elif old.appointmentTypeId in ESTABLISHED_TYPES:
                status = "established"
            elif re.search(r"\bnew\b", old.type, re.IGNORECASE):
                status = "new"
            elif re.search(
                r"\bestablished\b|\bfollow[\s_-]*up\b", old.type, re.IGNORECASE
            ):
                status = "established"
        body = {
            "patientId": p.patientId,
            "patientName": p.name,
            "dob": p.dob,
            "bookingToken": slot.bookingToken,
            "visitCategory": offered.visit,
            "patientStatus": status,
            "appointmentReason": reason.strip(),
            "visitReason": reason.strip(),
            "referringDoctor": referrer.strip(),
        }
        routing = "optical_only" if offered.visit == "routine_vision" else p.routing
        if routing:
            body["routing"] = routing
        if (
            old
            and old.appointmentTypeId
            and (
                old.rescheduleToken
                or old.appointmentTypeId
                in MEDICAL_TYPES | NEW_TYPES | ESTABLISHED_TYPES
            )
        ):
            body["appointmentTypeId"] = old.appointmentTypeId
        if old and old.rescheduleToken:
            body["rescheduleToken"] = old.rescheduleToken
        self._invalidate()
        self._receipts[receipt_key] = MutationReceipt(
            self._write_failure(
                SchedulingFailure(reason="pending", uncertain=True), "booking"
            )
        )
        result = await self.http.book(body)
        outcome = self._book_result(result, description)
        self._report(p, booking=result, old=old, slot=slot, call_id=call_id)
        if outcome["outcome"] not in ("booked", "partial_booking"):
            if outcome["outcome"] == "uncertain":
                self._receipts[receipt_key] = MutationReceipt(outcome)
            else:
                self._receipts.pop(receipt_key, None)
            if old:
                outcome = {
                    **outcome,
                    "answer": outcome["answer"]
                    + " The existing appointment was not cancelled.",
                }
                if outcome["outcome"] == "uncertain":
                    self._receipts[receipt_key] = MutationReceipt(outcome)
            return self._patient_changed(outcome, captured)
        appointment = Appointment(
            id=result.appointmentId,
            date=slot.date,
            time=slot.time,
            provider=provider_name(result.providerName or slot.provider),
            facility=result.locationName
            or get_office_profile(offered.office).display_name,
            office=offered.office,
            type=result.appointmentTypeName or "Appointment",
            appointmentTypeId=result.appointmentTypeId,
            rescheduleToken=result.rescheduleToken,
            confirmed=True,
        )
        if self._context() == captured:
            self.state.patient.active = p.model_copy(
                update={
                    "appointments": [
                        a for a in p.appointments if a.id != appointment.id
                    ]
                    + [appointment],
                    "appointmentsStatus": "found",
                }
            )
        if not old:
            self._receipts[receipt_key] = MutationReceipt(outcome, booked=appointment)
            return self._patient_changed(outcome, captured)
        # Persist a recovery receipt before the second write; a switch must never lose it.
        partial = reply(
            "partial_reschedule",
            f"The new appointment is booked for {description}, but cancellation of the old appointment requires staff reconciliation. Do not book again."
            + (" The patient note did not save." if result.status == "partial" else ""),
        )
        self._receipts[receipt_key] = MutationReceipt(partial, booked=appointment)
        if self._context() != captured:
            return self._patient_changed(partial, captured)
        cancellation = await self.http.cancel(self._cancel_body(p, old))
        self._report(p, booking=result, cancellation=cancellation, old=old, slot=slot, call_id=call_id)
        if self._cancel_result(cancellation)["outcome"] != "cancelled":
            return self._patient_changed(partial, captured)
        self._remove(p, old, captured)
        outcome = reply(
            "rescheduled",
            f"Rescheduled to {description}; the old appointment was cancelled."
            + (" The patient note did not save." if result.status == "partial" else ""),
        )
        self._receipts[receipt_key] = MutationReceipt(
            outcome, booked=appointment, cancelled_id=old.id
        )
        # Protect repeating the same move using the replacement reference.
        if self._context() == captured:
            self._receipts[p.patientId, action, appointment.id] = self._receipts[
                receipt_key
            ]
        return self._patient_changed(outcome, captured)

    def _report(self, patient, *, call_id, booking=None, cancellation=None, old=None, slot=None):
        reporter = self.state.reporter
        if not reporter:
            return
        evidence = {"externalPatientId": str(patient.patientId)}
        if booking is not None:
            outcome = self._book_result(booking, "the selected time")["outcome"]
            status = "partial" if outcome == "partial_booking" else outcome
            evidence["bookingResult"] = {"status": status}
            if outcome in ("booked", "partial_booking"):
                evidence["newAppointmentId"] = str(booking.appointmentId)
                evidence["bookingResult"].update(
                    appointmentId=booking.appointmentId,
                    appointmentDate=slot.date, appointmentTime=slot.time,
                    providerName=provider_name(booking.providerName or slot.provider),
                    appointmentTypeName=booking.appointmentTypeName,
                    locationName=booking.locationName,
                    patientName=patient.name,
                )
        if old:
            evidence["oldAppointmentId"] = str(old.id)
            evidence["cancellationResult"] = {
                "status": self._cancel_result(cancellation)["outcome"]
                if cancellation is not None else "not_attempted"
            }
            evidence["cancellationResult"].update(
                appointmentId=old.id, appointmentDate=old.date,
                appointmentTime=old.time, providerName=old.provider,
                appointmentTypeName=old.type, locationName=old.facility,
                patientName=patient.name,
            )
        evidence["action"] = "RESCHEDULED" if booking is not None and old else (
            "BOOKED" if booking is not None else "CANCELLED"
        )
        reporter.appointment(evidence, call_id=call_id)

    def _reconcile_receipts(self):
        """Apply observed writes to a reloaded patient, without a second calendar state."""
        patient = self.state.patient.active
        if patient is None:
            return
        receipts = [
            receipt
            for (patient_id, _, _), receipt in self._receipts.items()
            if patient_id == patient.patientId
        ]
        cancelled = {
            receipt.cancelled_id
            for receipt in receipts
            if receipt.cancelled_id is not None
        }
        appointments = {a.id: a for a in patient.appointments if a.id not in cancelled}
        for receipt in receipts:
            booked = receipt.booked
            if booked and booked.id not in cancelled and booked.id not in appointments:
                appointments[booked.id] = booked
        if list(appointments.values()) != patient.appointments:
            self.state.patient.active = patient.model_copy(
                update={
                    "appointments": list(appointments.values()),
                    "appointmentsStatus": patient.appointmentsStatus
                    if patient.appointmentsStatus == "error"
                    else ("found" if appointments else "none"),
                }
            )

    def _remove(self, patient, old, captured):
        if self._context() == captured:
            active = self.state.patient.active
            appointments = [a for a in active.appointments if a.id != old.id]
            self.state.patient.active = active.model_copy(
                update={
                    "appointments": appointments,
                    "appointmentsStatus": "found" if appointments else "none",
                }
            )

    def _patient_changed(self, result, captured):
        if self._context() != captured:
            return {
                **result,
                "answer": result["answer"]
                + " This result belongs to the earlier patient context; the patient changed during the operation.",
            }
        return result

    def _cancel_body(self, patient, appointment):
        if appointment.cancellationToken:
            return {"cancellationToken": appointment.cancellationToken}
        office = self.state.call.called_office_key
        facility = appointment.facility.lower().replace("-", " ")
        for aliases, key in (
            (("crystal river", "eye radiance"), "crystal-river"),
            (("spring hill",), "spring-hill"),
            (("hollywood",), "hollywood"),
            (("sweetwater",), "sweetwater"),
        ):
            if any(alias in facility for alias in aliases):
                office = key
                break
        return {
            "patientId": patient.patientId,
            "appointmentId": appointment.id,
            "office": get_office_profile(office).trunk_numbers[0],
        }

    @staticmethod
    def _cancel_result(result):
        if isinstance(result, WriteReceipt) and result.status == "cancelled":
            return reply("cancelled", "The selected appointment was cancelled.")
        if (
            isinstance(result, WriteReceipt)
            and result.outcome == "invalid_cancellation_token"
        ):
            return reply(
                "rejected",
                "The appointment details expired. Reload appointments and reconfirm the exact cancellation.",
            )
        return Scheduling._write_failure(result, "cancellation")

    @staticmethod
    def _book_result(result, description):
        if isinstance(result, WriteReceipt):
            if (
                result.status in ("booked", "success", "partial")
                and result.appointmentId
            ):
                return reply(
                    "partial_booking" if result.status == "partial" else "booked",
                    f"Booked {description}."
                    + (
                        " The appointment was booked, but the patient note did not save."
                        if result.status == "partial"
                        else ""
                    ),
                )
            if result.outcome == "appointment_type_unresolved" and result.missing:
                return reply(
                    "needs_input",
                    "Booking needs additional facts. Clarify the returned missing facts before trying again.",
                    missing=result.missing,
                )
            if result.outcome in (
                "slot_unavailable",
                "invalid_booking_token",
                "booking_token_required",
                "invalid_reschedule_token",
            ):
                return reply(
                    "rejected",
                    "That slot could not be booked. Reload availability or the existing appointment as needed before confirming another choice.",
                )
        return Scheduling._write_failure(result, "booking")

    @staticmethod
    def _write_failure(result, action):
        if (isinstance(result, SchedulingFailure) and not result.uncertain) or (
            isinstance(result, WriteReceipt)
            and result.status == "error"
            and result.outcome == "write_failed"
        ):
            return reply(
                "failed",
                f"The {action} failed. Ask staff for help or explicitly retry after resolving the failure.",
            )
        return reply(
            "uncertain",
            f"The {action} outcome could not be confirmed. Do not repeat the write or claim success; staff must reconcile the appointment record.",
        )
