"""One per-call scheduling owner: private appointment references and every appointment write."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NamedTuple

from abita_s2s.identity import PatientResolver
from abita_s2s.integrations.scheduling_http import (
    Booking,
    Cancellation,
    RescheduleReceipt,
    SchedulingFailure,
    SchedulingHTTP,
    SchedulingOutcome,
    Slot,
    WriteReceipt,
)
from abita_s2s.offices import get_office_profile
from abita_s2s.records import Appointment, Receipt
from abita_s2s.results import reply
from abita_s2s.scheduling import answers
from abita_s2s.scheduling.answers import (
    CHOOSE_LOADED_APPOINTMENT,
    COULD_NOT_RELOAD,
    PATIENT_CHANGED_DURING_WRITE,
    RELOAD_BEFORE_MOVE,
    RELOAD_TO_CHANGE,
    STALE_BEFORE_WRITE,
    UNAVAILABLE,
    BookOutcome,
    CancelOutcome,
    MoveOutcome,
)
from abita_s2s.scheduling.availability import (
    MoveSearch,
    OfferedSlot,
    Offers,
    PlanSearch,
    SchedulingContext,
    Selection,
    scheduling_context,
)
from abita_s2s.state import CallState
from abita_s2s.writes import WriteKey, WriteLedger, WriteOutcome, write_effect


class Refusal(NamedTuple):
    """What one middleware error outcome means for each write.

    Go errors after it touches the provider are always typed, so blank and validation
    mean the provider was never asked. An outcome a write's path cannot produce stays
    uncertain.
    """

    book: BookOutcome
    cancel: CancelOutcome
    move: MoveOutcome


REFUSALS: dict[SchedulingOutcome | None, Refusal] = {
    None: Refusal("refused", "refused", "refused"),
    "validation": Refusal("refused", "refused", "refused"),
    "invalid_booking_token": Refusal("expired", "uncertain", "expired"),
    "booking_token_required": Refusal("expired", "uncertain", "expired"),
    "invalid_reschedule_token": Refusal("expired", "uncertain", "expired"),
    "invalid_cancellation_token": Refusal("uncertain", "expired", "uncertain"),
    "appointment_type_unresolved": Refusal("needs_input", "uncertain", "refused"),
    "patient_context_mismatch": Refusal("refused", "uncertain", "refused"),
    "slot_unavailable": Refusal("refused", "uncertain", "refused"),
    "provider_conflict": Refusal("refused", "refused", "refused"),
    "provider_rejected": Refusal("refused", "refused", "refused"),
    "ownership_mismatch": Refusal("uncertain", "refused", "refused"),
    "write_failed": Refusal("failed", "failed", "failed"),
    "indeterminate_write": Refusal("uncertain", "uncertain", "uncertain"),
}


def said_by_middleware(
    result: WriteReceipt | RescheduleReceipt | SchedulingFailure,
) -> str:
    """The middleware's own reason, shown only for a refusal that changed nothing."""
    return "" if isinstance(result, SchedulingFailure) else result.message


def booked_appointment(
    receipt: WriteReceipt | SchedulingFailure | None, slot: Slot, office: str
) -> Appointment | None:
    """The appointment a receipt confirms as booked, or None when it confirms nothing."""
    if (
        not isinstance(receipt, WriteReceipt)
        or receipt.status != "booked"
        or not receipt.appointmentId
    ):
        return None
    return Appointment(
        id=receipt.appointmentId,
        date=slot.date,
        time=slot.time,
        provider=receipt.providerName or slot.provider,
        facility=receipt.locationName or get_office_profile(office).display_name,
        officeId=receipt.officeId,
        visitType=receipt.visitType,
        cancellationToken=receipt.cancellationToken,
        type=receipt.appointmentTypeName or "Appointment",
        rescheduleToken=receipt.rescheduleToken,
    )


def booking_outcome(
    result: WriteReceipt | SchedulingFailure, new: Appointment | None
) -> BookOutcome:
    """A named booking refusal counts whatever the receipt status; other non-error receipts are uncertain."""
    if new is not None:
        return "written"
    if isinstance(result, SchedulingFailure):
        return "uncertain" if result.uncertain else "unsent"
    outcome = REFUSALS[result.outcome].book
    if result.status == "error":
        return outcome
    named = result.outcome not in (None, "validation")
    return outcome if named and outcome in ("refused", "expired") else "uncertain"


def cancellation_outcome(
    result: WriteReceipt | SchedulingFailure, appointment_id: int
) -> CancelOutcome:
    """Only an error receipt can refuse a cancellation; other unmatched receipts are uncertain."""
    if isinstance(result, SchedulingFailure):
        return "uncertain" if result.uncertain else "unsent"
    if result.status == "cancelled" and result.appointmentId == appointment_id:
        return "written"
    if result.status != "error":
        return "uncertain"
    return REFUSALS[result.outcome].cancel


def move_outcome(
    result: RescheduleReceipt | SchedulingFailure,
    old_id: int,
    new: Appointment | None,
) -> MoveOutcome:
    if not isinstance(result, RescheduleReceipt):
        return "uncertain"
    if new is None:
        return (
            REFUSALS[result.outcome].move if result.status == "failed" else "uncertain"
        )
    cancellation = result.cancellation
    cancelled = (
        result.status == "completed"
        and cancellation is not None
        and cancellation.status == "cancelled"
        and cancellation.appointmentId == old_id
        and new.id != old_id
    )
    return "written" if cancelled else "partial"


@dataclass(frozen=True, repr=False)
class MutationReceipt:
    result: dict
    booked: Appointment | None = None
    cancelled_id: int | None = None
    selection: Selection | None = None


Operation = Callable[..., Awaitable[dict]]


class Scheduling:
    def __init__(
        self,
        state: CallState,
        resolver: PatientResolver,
        http: SchedulingHTTP,
        *,
        now=None,
    ):
        self.state = state
        self._resolver = resolver
        self.http = http
        self.now = now or (lambda: datetime.now(UTC))
        self._offers = Offers(state, http, lambda: self.now())
        self._references: dict[tuple[str, int], str] = {}
        self._next_ref = 0
        self._write_task: asyncio.Task[dict] | None = None
        self._closed = False
        self._ledger: WriteLedger[MutationReceipt] = WriteLedger()

    def close_admission(self) -> None:
        self._closed = True

    async def aclose(self):
        self._closed = True
        await self._offers.aclose()
        if self._write_task:
            try:
                await asyncio.shield(self._write_task)
            except asyncio.CancelledError:
                if not self._write_task.cancelled():
                    raise

    def _context(self) -> SchedulingContext:
        return scheduling_context(self.state)

    def _reference(self, patient_id: str, appointment: Appointment) -> str:
        key = (patient_id, appointment.id)
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
                "appointmentRef": self._reference(p.patientId, a),
                "date": a.date,
                "time": a.time,
                "provider": a.doctor,
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
            if patient and patient.appointmentsStatus == "error":
                return f"\n{RELOAD_TO_CHANGE}"
            return ""
        lines = ["Existing appointments:"]
        for appointment in appointments:
            lines.append(
                f"{appointment['appointmentRef']}: {appointment['date']} at {appointment['time']}"
                f" — {appointment['provider']}; location: {appointment['facility'] or 'not recorded'}"
                f"; visit type: {appointment['visitType'] or 'unknown (ask staff to reschedule)'}"
            )
        return "\n" + "\n".join(lines)

    def _writing(self) -> bool:
        return bool(self._write_task and not self._write_task.done())

    def _search_mode(
        self, visit, plan_id: str | None, old_ref: str | None
    ) -> PlanSearch | MoveSearch | dict:
        """Parse the search arguments into a mode, or the reply refusing them.

        Receipts apply first, so a reload that lags a write this call keeps its effects.
        """
        self._reconcile_receipts()
        p = self.state.patient.active
        if not (p and old_ref):
            if plan_id is None:
                return reply(
                    "needs_input",
                    "needs_input: Send the confirmed plan ID. Send null only to move a loaded appointment, with its reference, after the caller says their insurance has not changed.",
                )
            return PlanSearch(plan_id)
        moved = self._target(p, "reschedule", old_ref)[0]
        if moved is None:
            return reply("needs_input", CHOOSE_LOADED_APPOINTMENT)
        if moved.visitType != visit:
            return reply(
                "needs_input",
                "needs_input: Search the moved appointment's own visit type. If it is unknown, offer a staff request to reschedule.",
            )
        if not moved.rescheduleToken:
            return reply("needs_staff_review", COULD_NOT_RELOAD)
        if plan_id is not None:
            return PlanSearch(plan_id, moved)
        if p.patientId in self.state.insurance.coverage_changed:
            return reply(
                "needs_input",
                "needs_input: Insurance changed on this call, so a move needs the plan confirmed. Search with the confirmed plan ID and this appointment's reference.",
            )
        return MoveSearch(moved)

    async def _search(
        self, visit, plan_id: str | None, start, office, old_ref: str | None
    ) -> dict:
        mode = self._search_mode(visit, plan_id, old_ref)
        if isinstance(mode, dict):
            self._offers.invalidate()
            return mode
        return await self._offers.search(
            visit, mode, start, office, writing=self._writing()
        )

    def _revoke_authority(self) -> None:
        """Loaded tokens stop counting until a newer load, and a read under way predates this."""
        if self.state.patient.active:
            self.state.patient.mark_appointments_stale()
        self._resolver.fence_reads()

    def _move_needs_reload(self, old_ref: str) -> bool:
        p = self.state.patient.active
        if p is None:
            return False
        moved = self._target(p, "reschedule", old_ref)[0]
        return p.appointmentsStatus == "error" or (
            moved is not None and not moved.rescheduleToken
        )

    async def availability(
        self, visit, plan_id: str | None, start=None, office=None, old_ref=None
    ):
        """Search slots. A move reloads a revoked chart first, and retries once if Go refuses its token."""
        if self._closed:
            return reply("unavailable", "blocked: Scheduling has closed for this call.")
        if old_ref and not self._writing() and self._move_needs_reload(old_ref):
            if not await self._resolver.reload_chart():
                self._offers.invalidate()
                return reply("needs_staff_review", COULD_NOT_RELOAD)
        result = await self._search(visit, plan_id, start, office, old_ref)
        if result["outcome"] != "invalid_reschedule_token":
            return result
        self._revoke_authority()
        if await self._resolver.reload_chart():
            result = await self._search(visit, plan_id, start, office, old_ref)
            if result["outcome"] != "invalid_reschedule_token":
                return result
        self._offers.invalidate()
        return reply("needs_staff_review", COULD_NOT_RELOAD)

    async def book(
        self,
        *,
        slot_ref: str,
        reason: str,
        referrer: str | None,
        referrer_phone: str | None,
        referrer_address: str | None,
        confirmed: Literal[True] | None,
        call_id: str,
    ) -> str:
        return await self._execute(
            self._book,
            slot_ref=slot_ref,
            reason=reason,
            referrer=referrer,
            referrer_phone=referrer_phone,
            referrer_address=referrer_address,
            confirmed=confirmed,
            call_id=call_id,
        )

    async def cancel(
        self, *, confirmed: Literal[True] | None, old_ref: str, call_id: str
    ) -> str:
        return await self._execute(
            self._cancel, confirmed=confirmed, old_ref=old_ref, call_id=call_id
        )

    async def reschedule(
        self,
        *,
        slot_ref: str,
        reason: str | None,
        confirmed: Literal[True] | None,
        old_ref: str,
        call_id: str,
    ) -> str:
        return await self._execute(
            self._reschedule,
            slot_ref=slot_ref,
            reason=reason,
            confirmed=confirmed,
            old_ref=old_ref,
            call_id=call_id,
        )

    async def _execute(self, operation: Operation, **arguments) -> str:
        if self._closed:
            return UNAVAILABLE
        if self._write_task and not self._write_task.done():
            return "blocked: An appointment change is already in progress. Wait for its result; do not repeat it."
        patient = self.state.patient.active
        if not patient:
            return "needs_input: Verify the patient before changing appointments."
        if fence := self._ledger.blocking(patient.patientId):
            return fence.result["answer"] + self.appointments_text()
        captured = self._context()

        async def change() -> dict:
            if self._closed or self._context() != captured:
                return reply("stale", STALE_BEFORE_WRITE)
            result = await operation(patient, captured, **arguments)
            return self._finish_change(result, captured)

        self._write_task = asyncio.create_task(change())
        result = await asyncio.shield(self._write_task)
        return result["answer"] + self.appointments_text()

    def _target(
        self, patient: Receipt, action: Literal["cancel", "reschedule"], ref: str
    ) -> tuple[Appointment | None, WriteKey]:
        matches = [
            a
            for a in patient.appointments
            if self._reference(patient.patientId, a) == ref.strip()
        ]
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
                        patient_id,
                        appointment_id,
                    ), known_ref in self._references.items()
                    if patient_id == patient.patientId and known_ref == ref.strip()
                ),
                None,
            )
        )
        return old, WriteKey(patient.patientId, action, appointment_id)

    def _cancelled(self, patient_id: str, appointment_id: int) -> bool:
        return any(
            receipt.cancelled_id == appointment_id
            for receipt in self._ledger.receipts(patient_id)
        )

    def _replay(self, patient: Receipt, saved: MutationReceipt) -> dict:
        if saved.booked and self._cancelled(patient.patientId, saved.booked.id):
            return reply(
                "superseded",
                "blocked: That booking was cancelled or replaced. Select and confirm a current returned slot for a new booking.",
            )
        return saved.result

    def _settle(
        self,
        captured: SchedulingContext,
        key: WriteKey,
        outcome: WriteOutcome,
        receipt: MutationReceipt,
        *,
        target: Appointment | None,
    ) -> None:
        """Spend the target's authority when the outcome says so, then keep or forget the receipt.

        A write that took effect outdates a read under way for its patient. Another
        patient's lookup is untouched, because this write changed nothing it reads.
        """
        effect = write_effect(outcome)
        if self._context().patient_id == captured.patient_id:
            if target is not None and effect.spends_target:
                self._revoke_authority()
            if effect.replays:
                self._resolver.fence_reads()
        self._ledger.record(key, outcome, receipt)

    async def _cancel(
        self,
        p: Receipt,
        captured: SchedulingContext,
        *,
        old_ref: str,
        confirmed: bool | None,
        call_id: str,
    ) -> dict:
        old, key = self._target(p, "cancel", old_ref)
        if saved := self._ledger.get(key):
            return self._replay(p, saved)
        if old is None:
            return reply("needs_input", CHOOSE_LOADED_APPOINTMENT)
        if not old.cancellationToken:
            self._revoke_authority()
            return reply(
                "needs_input",
                "needs_input: Reload appointments to obtain cancellation authorization, then reconfirm the exact appointment.",
            )
        if confirmed is not True:
            return reply(
                "needs_confirmation",
                f"needs_input: Confirm cancellation of {old.read_back} before cancelling.",
            )
        self._offers.invalidate()
        self._ledger.record(
            key, "uncertain", MutationReceipt(answers.uncertain("cancellation"))
        )
        result = await self.http.cancel(
            Cancellation(patientId=p.patientId, cancellationToken=old.cancellationToken)
        )
        outcome = cancellation_outcome(result, old.id)
        answer = answers.cancellation(outcome, said_by_middleware(result))
        self._report(
            p,
            answer["outcome"],
            call_id=call_id,
            cancellation_outcome=answer["outcome"],
            old=old,
        )
        cancelled_id = old.id if outcome == "written" else None
        self._settle(
            captured,
            key,
            outcome,
            MutationReceipt(answer, cancelled_id=cancelled_id),
            target=old,
        )
        return answer

    async def _book(
        self,
        p: Receipt,
        captured: SchedulingContext,
        *,
        slot_ref: str,
        reason: str | None,
        confirmed: bool | None,
        call_id: str,
        referrer: str | None = None,
        referrer_phone: str | None = None,
        referrer_address: str | None = None,
        old: Appointment | None = None,
    ) -> dict:
        slot_ref = slot_ref.strip().upper()
        issued = self._offers.issued(slot_ref)
        offered = self._offers.get(slot_ref)
        key = (
            WriteKey(p.patientId, "reschedule", old.id)
            if old
            else WriteKey(p.patientId, "book", issued.selection if issued else slot_ref)
        )
        if not old and (saved := self._ledger.get(key)):
            rebooking = (
                offered
                and saved.booked
                and self._cancelled(p.patientId, saved.booked.id)
            )
            if not rebooking:
                return self._replay(p, saved)
        if not offered:
            return reply(
                "needs_input",
                "needs_input: That slot reference was not returned. Choose a slot exactly as listed in the latest results.",
            )
        if offered.context != captured or offered.expires <= self.now():
            self._offers.invalidate()
            return reply(
                "needs_input",
                "needs_input: Search availability again and choose a current returned slot.",
            )
        if not old and offered.moved_id is not None:
            return reply(
                "needs_input",
                "needs_input: That slot was found to move an existing appointment, so it cannot book a new one. Reschedule with it, or search again without the appointment's reference for a new visit.",
            )
        if old and offered.moved_id != old.id:
            return reply(
                "needs_input",
                "needs_input: That slot was not found for this appointment. Search again with this appointment's reference and reconfirm the move.",
            )
        if old and not old.rescheduleToken:
            self._revoke_authority()
            return reply(
                "needs_input",
                "needs_input: Reload appointments to obtain reschedule authorization, then reconfirm the move.",
            )
        if old and not (reason and reason.strip()):
            reason = f"Reschedule: {old.type or old.visitType}"
        if not reason or not reason.strip():
            return reply(
                "needs_input",
                "needs_input: Use the known visit reason or ask for it. For a vague concern, ask one focused follow-up; if still vague, preserve the caller's words and record that they could not add detail.",
            )
        slot = offered.slot
        description = (
            f"{slot.date} at {slot.time} Eastern with {slot.provider} for {p.name}"
        )
        if confirmed is not True:
            return reply(
                "needs_confirmation",
                f"needs_input: Confirm moving the appointment on {old.read_back} to {description} before rescheduling."
                if old
                else f"needs_input: Confirm {description} before booking.",
            )
        booking = Booking(
            patientId=p.patientId,
            patientName=p.name,
            dob=p.dob,
            bookingToken=slot.bookingToken,
            visitCategory=offered.visit,
            appointmentReason=reason.strip(),
            visitReason=reason.strip(),
            referringDoctor=(referrer or "").strip() or None,
            referringDoctorPhone=(referrer_phone or "").strip() or None,
            referringDoctorAddress=(referrer_address or "").strip() or None,
            rescheduleToken=old.rescheduleToken if old else None,
        )
        self._offers.invalidate()
        self._ledger.record(
            key, "uncertain", MutationReceipt(answers.uncertain("booking"))
        )
        if old:
            moved = await self.http.reschedule(booking)
            return self._moved(
                p, captured, key, moved, offered, old, description, call_id
            )
        result = await self.http.book(booking)
        new = booked_appointment(result, slot, offered.office)
        outcome = booking_outcome(result, new)
        answer = answers.booking(outcome, description, said_by_middleware(result))
        self._report(
            p,
            answer["outcome"],
            call_id=call_id,
            booking=result,
            booking_outcome=answer["outcome"],
            slot=slot,
        )
        self._settle(
            captured,
            key,
            outcome,
            MutationReceipt(answer, booked=new),
            target=None,
        )
        return answer

    def _moved(
        self,
        p: Receipt,
        captured: SchedulingContext,
        key: WriteKey,
        result: RescheduleReceipt | SchedulingFailure,
        offered: OfferedSlot,
        old: Appointment,
        description: str,
        call_id: str,
    ) -> dict:
        slot = offered.slot
        booked = result.booking if isinstance(result, RescheduleReceipt) else None
        new = booked_appointment(booked, slot, offered.office)
        outcome = move_outcome(result, old.id, new)
        answer = answers.move(
            outcome,
            description,
            old,
            reason=result.outcome if isinstance(result, RescheduleReceipt) else None,
            message=said_by_middleware(result),
        )
        if new is None:
            lost = isinstance(result, SchedulingFailure) and result.uncertain
            self._report(
                p,
                answer["outcome"],
                call_id=call_id,
                booking=result,
                booking_outcome=answer["outcome"],
                cancellation_outcome="uncertain" if lost else "not_attempted",
                old=old,
                slot=slot,
            )
            self._settle(captured, key, outcome, MutationReceipt(answer), target=old)
            return answer
        cancelled = outcome == "written"
        self._report(
            p,
            answer["outcome"],
            call_id=call_id,
            booking=booked,
            booking_outcome="booked",
            cancellation_outcome="cancelled" if cancelled else "uncertain",
            old=old,
            slot=slot,
        )
        receipt = MutationReceipt(
            answer,
            booked=new,
            cancelled_id=old.id if cancelled else None,
            selection=offered.selection,
        )
        self._settle(captured, key, outcome, receipt, target=old)
        self._ledger.record(
            WriteKey(p.patientId, "book", offered.selection), outcome, receipt
        )
        return answer

    async def _reschedule(
        self,
        p: Receipt,
        captured: SchedulingContext,
        *,
        old_ref: str,
        slot_ref: str,
        reason: str | None,
        confirmed: bool | None,
        call_id: str,
    ) -> dict:
        old, key = self._target(p, "reschedule", old_ref)
        if saved := self._ledger.get(key):
            issued = self._offers.issued(slot_ref.strip().upper())
            if (
                saved.cancelled_id is not None
                and issued is not None
                and issued.selection != saved.selection
            ):
                return reply(
                    "needs_input",
                    "needs_input: That appointment has already moved. Search again with its current reference for a different move.",
                )
            return self._replay(p, saved)
        if p.appointmentsStatus == "error":
            return reply("needs_input", RELOAD_BEFORE_MOVE)
        if old is None:
            return reply("needs_input", CHOOSE_LOADED_APPOINTMENT)
        return await self._book(
            p,
            captured,
            slot_ref=slot_ref,
            reason=reason,
            confirmed=confirmed,
            call_id=call_id,
            old=old,
        )

    def _report(
        self,
        patient: Receipt,
        outcome: str,
        *,
        call_id: str,
        booking: WriteReceipt | RescheduleReceipt | SchedulingFailure | None = None,
        booking_outcome: str | None = None,
        cancellation_outcome: str = "not_attempted",
        old: Appointment | None = None,
        slot: Slot | None = None,
    ) -> None:
        reporter = self.state.reporter
        if not reporter:
            return
        evidence: dict[str, object] = {"externalPatientId": str(patient.patientId)}
        if booking is not None:
            booking_result: dict[str, object] = {"status": booking_outcome}
            evidence["bookingResult"] = booking_result
            if (
                booking_outcome == "booked"
                and isinstance(booking, WriteReceipt)
                and slot is not None
            ):
                evidence["newAppointmentId"] = str(booking.appointmentId)
                booking_result.update(
                    appointmentId=booking.appointmentId,
                    appointmentDate=slot.date,
                    appointmentTime=slot.time,
                    providerName=booking.providerName or slot.provider,
                    appointmentTypeName=booking.appointmentTypeName,
                    locationName=booking.locationName,
                    patientName=patient.name,
                    providerProfileId=booking.profileId,
                    officeId=booking.officeId,
                    visitType=booking.visitType,
                )
        if old:
            evidence["oldAppointmentId"] = str(old.id)
            evidence["cancellationResult"] = {
                "status": cancellation_outcome,
                "appointmentId": old.id,
                "appointmentDate": old.date,
                "appointmentTime": old.time,
                "providerName": old.provider,
                "appointmentTypeName": old.type,
                "locationName": old.facility,
                "patientName": patient.name,
            }
        evidence["action"] = (
            "RESCHEDULED"
            if booking is not None and old
            else ("BOOKED" if booking is not None else "CANCELLED")
        )
        reporter.appointment(evidence, outcome, call_id=call_id)

    def _reconcile_receipts(self):
        """Apply confirmed receipt effects after writes and patient reloads."""
        patient = self.state.patient.active
        if patient is None:
            return
        receipts = self._ledger.receipts(patient.patientId)
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
        self.state.patient.update_appointments(list(appointments.values()))

    def _finish_change(self, result: dict, captured: SchedulingContext) -> dict:
        if self._context().patient_id != captured.patient_id:
            return {**result, "answer": result["answer"] + PATIENT_CHANGED_DURING_WRITE}
        self._reconcile_receipts()
        return result
