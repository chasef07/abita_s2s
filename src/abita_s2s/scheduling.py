"""One per-call scheduling owner: private references, inventory and write receipts."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from livekit.agents import RunContext, function_tool

from abita_s2s.insurance_state import (
    AcceptedInsurance,
    insurance_ready,
    scheduling_insurance,
)
from abita_s2s.middleware import Appointment, Receipt
from abita_s2s.offices import get_office_profile
from abita_s2s.scheduling_http import (
    RescheduleReceipt,
    SchedulingFailure,
    SchedulingHTTP,
    Slot,
    WriteReceipt,
)
from abita_s2s.state import CallState

VisitType = Literal["medical", "routine_vision"]
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


@dataclass(frozen=True, repr=False)
class SchedulingContext:
    patient_revision: int
    patient_id: str | None
    dob: str | None
    accepted_insurance: AcceptedInsurance | None
    acceptance_id: int
    insurance_write_pending: bool
    insurance_write_uncertain: bool
    registration: str | None


@dataclass(frozen=True, repr=False)
class AvailabilitySearch:
    context: SchedulingContext
    office: str
    visit: VisitType
    start: date
    today: date


@dataclass(frozen=True, repr=False)
class OfferedSlot:
    slot: Slot
    context: SchedulingContext
    office: str
    visit: VisitType
    expires: datetime

    @property
    def selection(self):
        return self.office, self.visit, self.slot.key


@dataclass(repr=False)
class MutationReceipt:
    result: dict
    booked: Appointment | None = None
    cancelled_id: int | None = None
    offered: OfferedSlot | None = None


@dataclass(repr=False)
class PendingRead:
    key: AvailabilitySearch
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

    def _context(self) -> SchedulingContext:
        p = self.state.patient.active
        insurance = self.state.insurance
        registration = insurance.registrations.get(p.patientId) if p else None
        accepted = insurance.accepted
        return SchedulingContext(
            patient_revision=self.state.patient.revision,
            patient_id=p.patientId if p else None,
            dob=p.dob if p else None,
            accepted_insurance=accepted,
            acceptance_id=id(accepted),
            insurance_write_pending=insurance.write_pending,
            insurance_write_uncertain=insurance.write_uncertain,
            registration=registration,
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
                "visitType": a.visitType,
            }
            for a in p.appointments
        ]

    def appointments_text(self) -> str:
        appointments = self.appointments()
        patient = self.state.patient.active
        if not appointments:
            if patient and patient.appointmentsStatus == "none":
                return "\nNo upcoming appointments."
            return ""
        lines = ["Existing appointments:"]
        for appointment in appointments:
            lines.append(
                f"{appointment['appointmentRef']}: {appointment['date']} at {appointment['time']}"
                f" — {appointment['provider']}; location: {appointment['facility'] or 'not recorded'}"
                f"; visit type: {appointment['visitType'] or 'unknown'}"
            )
        return "\n" + "\n".join(lines)

    def _invalidate(self):
        self._generation += 1
        self._slots.clear()
        self._cache = None
        self._search_key = None
        self._search = None

    def _office(self, requested):
        called = self.state.call.called_office_key
        if called in ("hollywood", "sweetwater"):
            if requested not in ("hollywood", "sweetwater"):
                return None
            return requested
        return called if requested is None else None

    @function_tool
    async def list_available_appointments(
        self,
        context: RunContext[CallState],
        visitType: VisitType,
        startDate: str | None = None,
        office: Literal["hollywood", "sweetwater"] | None = None,
    ) -> str:
        """Find eligible slots for the active patient in a 14-day Eastern-time window.

        Requires patient resolution or completed registration and no unfinished insurance write.

        Args:
            visitType: medical or routine_vision; match the existing visit when rescheduling.
            startDate: YYYY-MM-DD, tomorrow or later; null defaults to tomorrow.
                To search the next window, use the day after the returned searched-through date.
            office: Required caller-selected office for Hollywood/Sweetwater calls;
                omit for other offices.
        """
        if context.userdata is not self.state or self._closed:
            return "blocked: Scheduling is unavailable."
        result = await self.availability(visitType, startDate, office)
        lines = [result["answer"]]
        if "searchedFrom" in result:
            lines.append(
                f"Searched {result['searchedFrom']} through {result['searchedThrough']}."
            )
        if "retry_same_search" in result and result["outcome"] == "availability_failed":
            lines.append(
                "Retry this search once."
                if result["retry_same_search"]
                else "Do not retry this search; ask staff for help."
            )
        if slots := result.get("slots"):
            groups = {}
            for slot in slots:
                groups.setdefault((slot["date"], slot["provider"]), []).append(slot)
            for (day, provider), openings in groups.items():
                calendar_day = date.fromisoformat(day)
                lines.extend(
                    [
                        "",
                        f"{calendar_day:%A, %B} {calendar_day.day}, {calendar_day.year}",
                        provider,
                    ]
                )
                lines.extend(
                    f"{slot['time']} — {slot['appointmentSlotRef']}"
                    for slot in openings
                )
        return "\n".join(lines)

    async def availability(self, visit, start=None, office=None):
        if self._closed:
            return reply("unavailable", "blocked: Scheduling has closed for this call.")
        context = self._context()
        selected = self._office(office)
        today = self.now().astimezone(EASTERN).date()
        try:
            first = date.fromisoformat(start) if start else today + timedelta(days=1)
            if start and first.isoformat() != start:
                raise ValueError()
        except ValueError:
            self._invalidate()
            return reply(
                "needs_input", "needs_input: Provide the requested date as YYYY-MM-DD."
            )
        if first <= today:
            self._invalidate()
            return reply(
                "needs_input",
                "needs_input: Same-day and past dates cannot be scheduled here. Ask whether tomorrow or later works; do not silently change the date.",
            )
        if selected is None:
            self._invalidate()
            return reply(
                "needs_input",
                "needs_input: Ask for Hollywood or Sweetwater on those office calls; omit office for other calls.",
            )
        if visit not in ("medical", "routine_vision") or not insurance_ready(
            self.state
        ):
            self._invalidate()
            return reply(
                "needs_input",
                "needs_input: Verify the patient, finish new-patient registration, or resolve the pending or uncertain insurance update before scheduling.",
            )
        if self._write_task and not self._write_task.done():
            return reply(
                "in_progress",
                "blocked: An appointment change is in progress. Wait for its result.",
            )
        p = self.state.patient.active
        decision = scheduling_insurance(self.state, visit)
        body = {
            "office": get_office_profile(selected).trunk_numbers[0],
            "startDate": first.isoformat(),
            "rangeDays": 14,
            "patientId": p.patientId,
            "coverageType": visit,
            "visitType": visit,
            "dob": p.dob,
        }
        if decision:
            body["insurancePlan"] = decision.canonicalPlan
        key = AvailabilitySearch(context, selected, visit, first, today)
        if self._search_key != key:
            self._invalidate()
            self._search_key = key
        if self._cache and self._cache[0] == key and self._cache[1] > self.now():
            return self._cache[2]
        failed = self._failures.get(key)
        if failed and (failed[0] >= 2 or not failed[1]):
            return reply(
                "availability_failed",
                "blocked: Availability could not be verified. Do not describe this as no openings or retry this search; ask staff for help.",
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
                    self._invalidate()

    async def _load(self, body, key, generation):
        result = await self.http.availability(body)
        if generation != self._generation or key.context != self._context():
            return reply(
                "stale",
                "blocked: The patient or appointment details changed. Check again with current details.",
            )
        previous = {item.slot.key: (ref, item) for ref, item in self._slots.items()}
        self._slots.clear()
        first = key.start.isoformat()
        through = (key.start + timedelta(days=13)).isoformat()
        expired = False
        if not isinstance(result, SchedulingFailure):
            if (
                (result.searchedFrom is not None and result.searchedFrom != first)
                or (
                    result.searchedThrough is not None
                    and result.searchedThrough != through
                )
                or any(not first <= slot.date <= through for slot in result.slots)
            ):
                result = SchedulingFailure(reason="invalid_search_window")
            elif result.outcome == "availability_found":
                expired = result.bookingTokenExpiresAt <= self.now()
                if expired:
                    result = SchedulingFailure(reason="expired_inventory")
        if not isinstance(result, SchedulingFailure) and result.outcome in (
            "invalid_input",
            "policy_blocked",
        ):
            answer = reply(
                "needs_input" if result.outcome == "invalid_input" else "unsupported",
                f"{'needs_input' if result.outcome == 'invalid_input' else 'blocked'}: {result.message}",
                retry_same_search=False,
            )
            # Policy errors are not exhausted transport retries. Recheck after
            # the normal TTL so chart corrections can restore scheduling.
            self._failures.pop(key, None)
            self._cache = (key, self.now() + timedelta(seconds=60), answer)
            return answer
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
            if expired and count < 2:
                return await self._load(body, key, generation)
            return reply(
                "availability_failed",
                "blocked: Availability could not be verified; this does not mean no openings.",
                retry_same_search=retry and count < 2,
            )
        self._failures.pop(key, None)
        if result.outcome == "no_eligible_providers":
            answer = reply(
                "unsupported",
                "blocked: No providers are eligible for the selected office, visit type, and patient requirements. Confirm the office and visit type or ask staff for help; changing dates will not resolve this restriction.",
            )
            self._cache = (key, self.now() + timedelta(seconds=60), answer)
            return answer
        if result.outcome == "no_availability":
            answer = reply(
                "none",
                "no_results: No eligible openings in the searched window. Ask what other dates work.",
                searchedFrom=first,
                searchedThrough=through,
            )
            self._cache = (key, self.now() + timedelta(seconds=60), answer)
            return answer
        expiry = result.bookingTokenExpiresAt
        unique = {slot.key: slot for slot in result.slots}
        for slot in unique.values():
            prior = previous.get(slot.key)
            if (
                prior
                and prior[1].context == key.context
                and prior[1].office == key.office
                and prior[1].visit == key.visit
            ):
                ref = prior[0]
            else:
                self._next_ref += 1
                ref = f"S{self._next_ref}"
            self._slots[ref] = OfferedSlot(
                slot, key.context, key.office, key.visit, expiry
            )
        answer = reply(
            "found",
            "success: Found eligible openings.",
            searchedFrom=first,
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
        readBack: Literal[True] | None,
    ) -> str:
        """Book a new appointment using a returned slot after caller confirmation of date, time and provider.

        Reuse the known visit reason. For a vague concern, ask one focused follow-up;
        if still unclear, preserve the caller's words and note the limitation. Never diagnose.
        readBack is true only after confirmation. Claim success only from this result. Do not retry uncertain writes.
        Use reschedule_appointment to move an existing appointment.

        Args:
            referringDoctor: Caller-provided referring doctor. Reuse an answer already
                supplied; otherwise ask "Did a doctor refer you?" If yes, ask for the
                name. Use internal value "none" only when the caller says they have
                no referring doctor. Do not ask whether to put or mark none, or
                narrate the internal value.
        """
        return await self._execute(
            context,
            self._book,
            slot_ref=appointmentSlotRef,
            reason=appointmentReason,
            referrer=referringDoctor,
            confirmed=readBack,
        )

    @function_tool
    async def cancel_appointment(
        self,
        context: RunContext[CallState],
        appointmentRef: str,
        readBack: Literal[True] | None,
    ) -> str:
        """Cancel only after verification and the caller confirms cancellation of the exact loaded appointment.

        Use its private appointmentRef. readBack is true only after the caller confirms
        the exact date, time, provider and intent to cancel.
        Claim success only from the result; never retry uncertain cancellation.
        """
        return await self._execute(
            context, self._cancel, confirmed=readBack, old_ref=appointmentRef
        )

    @function_tool
    async def reschedule_appointment(
        self,
        context: RunContext[CallState],
        oldAppointmentRef: str,
        appointmentSlotRef: str,
        appointmentReason: str,
        referringDoctor: str,
        readBack: Literal[True] | None,
    ) -> str:
        """Move the caller-confirmed loaded appointment to a confirmed returned slot.

        Confirm the old appointment and read back the new date, time and provider before readBack=true.
        Reuse known visit and referral details; ask only for missing information.
        Books first, then cancels the old visit. Report partial success and never repeat an uncertain booking.

        Args:
            referringDoctor: Caller-provided referring doctor. Reuse an answer already
                supplied; otherwise ask "Did a doctor refer you?" If yes, ask for the
                name. Use internal value "none" only when the caller says they have
                no referring doctor. Do not ask whether to put or mark none, or
                narrate the internal value.
        """
        return await self._execute(
            context,
            self._reschedule,
            slot_ref=appointmentSlotRef,
            reason=appointmentReason,
            referrer=referringDoctor,
            confirmed=readBack,
            old_ref=oldAppointmentRef,
        )

    async def _execute(
        self, context: RunContext[CallState], operation, **arguments
    ) -> str:
        if context.userdata is not self.state or self._closed:
            return "blocked: Scheduling is unavailable."
        if self._write_task and not self._write_task.done():
            return "blocked: An appointment change is already in progress. Wait for its result; do not repeat it."
        patient = self.state.patient.active
        if not patient:
            return "needs_input: Verify the patient before changing appointments."
        for (patient_id, _, _), receipt in self._receipts.items():
            if patient_id == patient.patientId and receipt.result["outcome"] in (
                "uncertain",
                "partial_reschedule",
            ):
                return receipt.result["answer"] + self.appointments_text()
        captured = self._context()

        async def change():
            if self._closed or self._context() != captured:
                return reply(
                    "stale",
                    "blocked: The call or patient changed before the appointment operation started.",
                )
            result = await operation(
                patient, captured, call_id=context.function_call.call_id, **arguments
            )
            return self._finish_change(result, captured)

        # Retain both the write and reconciliation even if the tool caller leaves.
        self._write_task = asyncio.create_task(change())
        result = await asyncio.shield(self._write_task)
        return result["answer"] + self.appointments_text()

    def _target(self, patient: Receipt, action: str, ref: str):
        matches = [a for a in patient.appointments if self._reference(a) == ref.strip()]
        old = (
            matches[0]
            if patient.appointmentsStatus == "found" and len(matches) == 1
            else None
        )
        appointment_id = (
            old.id
            if old
            else next(
                (
                    appointment_id
                    for (
                        _,
                        patient_id,
                        appointment_id,
                    ), known_ref in self._references.items()
                    if patient_id == patient.patientId and known_ref == ref.strip()
                ),
                None,
            )
        )
        return old, (patient.patientId, action, appointment_id)

    def _cancelled(self, patient_id: str, appointment_id: int) -> bool:
        return any(
            receipt.cancelled_id == appointment_id
            for (owner_id, _, _), receipt in self._receipts.items()
            if owner_id == patient_id
        )

    def _replay(self, patient: Receipt, saved: MutationReceipt) -> dict:
        if saved.booked and self._cancelled(patient.patientId, saved.booked.id):
            return reply(
                "superseded",
                "blocked: That booking was cancelled or replaced. Select and confirm a current returned slot for a new booking.",
            )
        return saved.result

    async def _cancel(
        self,
        p: Receipt,
        captured: SchedulingContext,
        *,
        old_ref: str,
        confirmed: bool | None,
        call_id: str,
    ) -> dict:
        old, receipt_key = self._target(p, "cancel", old_ref)
        if saved := self._receipts.get(receipt_key):
            return saved.result
        if old is None:
            return reply(
                "needs_input",
                "needs_input: Choose and confirm the exact currently loaded appointment. Reload patient appointments if needed.",
            )
        if not old.cancellationToken:
            self.state.patient.active = p.model_copy(
                update={"appointmentsStatus": "error"}
            )
            return reply(
                "needs_input",
                "needs_input: Reload appointments to obtain cancellation authorization, then reconfirm the exact appointment.",
            )
        if confirmed is not True:
            return reply(
                "needs_confirmation",
                f"needs_input: Confirm cancellation of {old.date} at {old.time} Eastern"
                f" with {provider_name(old.provider)} before cancelling.",
            )
        self._invalidate()
        self._receipts[receipt_key] = MutationReceipt(
            self._write_failure(
                SchedulingFailure(reason="pending", uncertain=True), "cancellation"
            )
        )
        result = await self.http.cancel(self._cancel_body(p, old))
        outcome = self._cancel_result(result, old.id)
        self._report(
            p, cancellation_outcome=outcome["outcome"], old=old, call_id=call_id
        )
        if (
            outcome["outcome"] != "cancelled"
            and isinstance(result, WriteReceipt)
            and result.status == "error"
            and result.outcome
            in (
                "invalid_cancellation_token",
                "provider_conflict",
                "provider_rejected",
                "ownership_mismatch",
                "write_failed",
            )
            and self._context() == captured
        ):
            self.state.patient.active = p.model_copy(
                update={"appointmentsStatus": "error"}
            )
        if outcome["outcome"] in ("cancelled", "uncertain"):
            self._receipts[receipt_key] = MutationReceipt(
                outcome,
                cancelled_id=old.id if outcome["outcome"] == "cancelled" else None,
            )
        else:
            self._receipts.pop(receipt_key, None)
        return outcome

    async def _book(
        self,
        p: Receipt,
        captured: SchedulingContext,
        *,
        slot_ref: str,
        reason: str,
        referrer: str,
        confirmed: bool | None,
        call_id: str,
        old: Appointment | None = None,
    ) -> dict:
        slot_ref = slot_ref.strip().upper()
        receipt_key = (
            (p.patientId, "reschedule", old.id)
            if old
            else (p.patientId, "book", slot_ref)
        )
        if not old and (saved := self._receipts.get(receipt_key)):
            return self._replay(p, saved)
        offered = self._slots.get(slot_ref)
        if not offered or offered.context != captured or offered.expires <= self.now():
            self._invalidate()
            return reply(
                "needs_input",
                "needs_input: Search availability again and choose a current returned slot.",
            )
        if not old:
            for (patient_id, _, _), saved in self._receipts.items():
                if (
                    patient_id == p.patientId
                    and saved.offered
                    and saved.booked
                    and saved.offered.selection == offered.selection
                    and not self._cancelled(p.patientId, saved.booked.id)
                ):
                    return saved.result
        if not insurance_ready(self.state):
            self._invalidate()
            return reply(
                "needs_input",
                "needs_input: Finish new-patient registration or resolve the pending or uncertain insurance update before booking.",
            )
        if old and old.visitType is None:
            return reply(
                "needs_staff_review",
                "blocked: The existing appointment's visit type could not be verified. Ask staff to reschedule it; no appointment was changed.",
            )
        if old and old.visitType != offered.visit:
            return reply(
                "needs_input",
                f"needs_input: Load {old.visitType} availability to match the existing appointment.",
            )
        if not reason or not reason.strip():
            return reply(
                "needs_input",
                "needs_input: Use the known visit reason or ask for it. For a vague concern, ask one focused follow-up; if still vague, preserve the caller's words and record that they could not add detail.",
            )
        if not referrer or not referrer.strip():
            return reply(
                "needs_input",
                "needs_input: Ask whether a doctor referred the caller and get their name; use 'none' only if the caller says no.",
            )
        slot = offered.slot
        description = (
            f"{slot.date} at {slot.time} Eastern with {provider_name(slot.provider)}"
        )
        if confirmed is not True:
            return reply(
                "needs_confirmation",
                f"needs_input: Confirm {description} with the caller before booking.",
            )
        status = (
            "new"
            if p.patientId in self.state.insurance.registrations
            else "established"
        )
        decision = scheduling_insurance(self.state, offered.visit)
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
        if decision:
            body["insurancePlan"] = decision.canonicalPlan
        if old:
            if not old.rescheduleToken:
                self.state.patient.active = p.model_copy(
                    update={"appointmentsStatus": "error"}
                )
                return reply(
                    "needs_input",
                    "needs_input: Reload appointments to obtain reschedule authorization, then reconfirm the move.",
                )
            body["rescheduleToken"] = old.rescheduleToken
        self._invalidate()
        self._receipts[receipt_key] = MutationReceipt(
            self._write_failure(
                SchedulingFailure(reason="pending", uncertain=True), "booking"
            )
        )
        reschedule = None
        if old:
            reschedule = await self.http.reschedule(body)
            if (
                not isinstance(reschedule, RescheduleReceipt)
                or reschedule.booking is None
            ):
                outcome = (
                    reply(
                        "failed",
                        "blocked: The reschedule failed. Reload appointments or ask staff to reconcile before another change.",
                    )
                    if isinstance(reschedule, RescheduleReceipt)
                    and reschedule.status == "failed"
                    else self._write_failure(
                        SchedulingFailure(reason="reschedule", uncertain=True),
                        "reschedule",
                    )
                )
                if outcome["outcome"] == "uncertain":
                    self._receipts[receipt_key] = MutationReceipt(outcome)
                else:
                    self._receipts.pop(receipt_key, None)
                self._report(
                    p,
                    booking=reschedule,
                    booking_outcome=outcome["outcome"],
                    old=old,
                    slot=slot,
                    call_id=call_id,
                )
                return outcome
            result = reschedule.booking
        else:
            result = await self.http.book(body)
        outcome = self._book_result(result, description)
        if not old:
            self._report(
                p,
                booking=result,
                booking_outcome=outcome["outcome"],
                slot=slot,
                call_id=call_id,
            )
        if outcome["outcome"] not in ("booked", "partial_booking"):
            if old:
                outcome = {
                    **outcome,
                    "answer": outcome["answer"]
                    + " The existing appointment was not cancelled.",
                }
            if outcome["outcome"] == "uncertain":
                self._receipts[receipt_key] = MutationReceipt(outcome)
            else:
                self._receipts.pop(receipt_key, None)
            return outcome
        appointment = Appointment(
            id=result.appointmentId,
            date=slot.date,
            time=slot.time,
            provider=provider_name(result.providerName or slot.provider),
            facility=result.locationName
            or get_office_profile(offered.office).display_name,
            office=result.office,
            officeId=result.officeId,
            visitType=result.visitType,
            cancellationToken=result.cancellationToken,
            type=result.appointmentTypeName or "Appointment",
            appointmentTypeId=result.appointmentTypeId,
            rescheduleToken=result.rescheduleToken,
            confirmed=True,
        )
        cancelled_id = None
        if old:
            cancelled = (
                reschedule.status == "completed"
                and reschedule.cancellation is not None
                and reschedule.cancellation.status == "cancelled"
                and reschedule.cancellation.appointmentId == old.id
                and appointment.id != old.id
            )
            cancelled_id = old.id if cancelled else None
            note = (
                " The patient note did not save; ask staff to complete it."
                if outcome["outcome"] == "partial_booking"
                else ""
            )
            outcome = reply(
                "rescheduled" if cancelled else "partial_reschedule",
                (
                    f"{'blocked' if note else 'success'}: Your new appointment is booked for {description}. Your old appointment on {old.date} at {old.time} is cancelled. Tell the caller both outcomes."
                    if cancelled
                    else f"blocked: The new appointment is booked for {description}, but cancellation of the old appointment requires staff reconciliation. Do not book again."
                )
                + note,
            )
            self._report(
                p,
                booking=result,
                booking_outcome="partial_booking" if note else "booked",
                cancellation_outcome="cancelled" if cancelled else "uncertain",
                old=old,
                slot=slot,
                call_id=call_id,
            )
        saved = MutationReceipt(
            outcome,
            booked=appointment,
            cancelled_id=cancelled_id,
            offered=offered,
        )
        self._receipts[receipt_key] = saved
        if old and cancelled_id:
            self._receipts[p.patientId, "reschedule", appointment.id] = saved
        return outcome

    async def _reschedule(
        self,
        p: Receipt,
        captured: SchedulingContext,
        *,
        old_ref: str,
        slot_ref: str,
        reason: str,
        referrer: str,
        confirmed: bool | None,
        call_id: str,
    ) -> dict:
        old, receipt_key = self._target(p, "reschedule", old_ref)
        saved = self._receipts.get(receipt_key)
        offered = self._slots.get(slot_ref.strip().upper())
        if saved:
            different = (
                offered
                and saved.offered
                and offered.selection != saved.offered.selection
            )
            replacement_move = (
                old
                and saved.booked
                and old.id == saved.booked.id
                and saved.cancelled_id is not None
            )
            if not (different and replacement_move):
                if different and saved.cancelled_id is not None:
                    return reply(
                        "needs_input",
                        "needs_input: Choose the current appointment reference for a different move.",
                    )
                return self._replay(p, saved)
        if old is None:
            return reply(
                "needs_input",
                "needs_input: Choose and confirm the exact currently loaded appointment. Reload patient appointments if needed.",
            )
        return await self._book(
            p,
            captured,
            slot_ref=slot_ref,
            reason=reason,
            referrer=referrer,
            confirmed=confirmed,
            call_id=call_id,
            old=old,
        )

    def _report(
        self,
        patient,
        *,
        call_id,
        booking=None,
        booking_outcome=None,
        cancellation_outcome="not_attempted",
        old=None,
        slot=None,
    ):
        reporter = self.state.reporter
        if not reporter:
            return
        evidence = {"externalPatientId": str(patient.patientId)}
        if booking is not None:
            status = (
                "partial" if booking_outcome == "partial_booking" else booking_outcome
            )
            evidence["bookingResult"] = {"status": status}
            if booking_outcome in ("booked", "partial_booking"):
                evidence["newAppointmentId"] = str(booking.appointmentId)
                evidence["bookingResult"].update(
                    appointmentId=booking.appointmentId,
                    appointmentDate=slot.date,
                    appointmentTime=slot.time,
                    providerName=provider_name(booking.providerName or slot.provider),
                    appointmentTypeName=booking.appointmentTypeName,
                    locationName=booking.locationName,
                    patientName=patient.name,
                )
        if old:
            evidence["oldAppointmentId"] = str(old.id)
            evidence["cancellationResult"] = {"status": cancellation_outcome}
            evidence["cancellationResult"].update(
                appointmentId=old.id,
                appointmentDate=old.date,
                appointmentTime=old.time,
                providerName=old.provider,
                appointmentTypeName=old.type,
                locationName=old.facility,
                patientName=patient.name,
            )
        evidence["action"] = (
            "RESCHEDULED"
            if booking is not None and old
            else ("BOOKED" if booking is not None else "CANCELLED")
        )
        reporter.appointment(evidence, call_id=call_id)

    def _reconcile_receipts(self):
        """Apply confirmed receipt effects after writes and patient reloads."""
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

    def _finish_change(self, result, captured):
        # Complete state updates even when the shielded tool caller has left.
        if self._context() != captured:
            return {
                **result,
                "answer": result["answer"]
                + " This result belongs to the earlier patient context; the patient changed during the operation.",
            }
        self._reconcile_receipts()
        return result

    def _cancel_body(self, patient, appointment):
        return {
            "patientId": patient.patientId,
            "cancellationToken": appointment.cancellationToken,
        }

    @staticmethod
    def _cancel_result(result, appointment_id):
        if (
            isinstance(result, WriteReceipt)
            and result.status == "cancelled"
            and result.appointmentId == appointment_id
        ):
            return reply(
                "cancelled", "success: The selected appointment was cancelled."
            )
        if (
            isinstance(result, WriteReceipt)
            and result.status == "error"
            and result.outcome == "invalid_cancellation_token"
        ):
            return reply(
                "rejected",
                "needs_input: The appointment details expired. Reload appointments and reconfirm the exact cancellation.",
            )
        if (
            isinstance(result, WriteReceipt)
            and result.status == "error"
            and result.outcome
            in ("provider_conflict", "provider_rejected", "ownership_mismatch")
        ):
            return reply(
                "rejected",
                "needs_input: Cancellation was not completed. Reload appointments and reconfirm the exact appointment, or ask office staff for help.",
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
                    f"{'blocked' if result.status == 'partial' else 'success'}: Booked {description}."
                    + (
                        " The appointment was booked, but the patient note did not save. Do not book again; ask staff to complete the note."
                        if result.status == "partial"
                        else ""
                    ),
                )
            if result.outcome == "appointment_type_unresolved" and result.missing:
                return reply(
                    "needs_input",
                    f"needs_input: Booking needs additional facts: {', '.join(result.missing)}. Clarify these before trying again.",
                )
            if result.outcome in (
                "slot_unavailable",
                "invalid_booking_token",
                "booking_token_required",
                "invalid_reschedule_token",
            ):
                return reply(
                    "rejected",
                    "needs_input: That slot could not be booked. Reload availability or the existing appointment as needed before confirming another choice.",
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
                f"blocked: The {action} failed. Ask staff for help or explicitly retry after resolving the failure.",
            )
        return reply(
            "uncertain",
            f"blocked: The {action} outcome could not be confirmed. Do not repeat the write or claim success; staff must reconcile the appointment record.",
        )
