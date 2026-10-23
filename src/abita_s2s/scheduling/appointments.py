"""One per-call scheduling owner: private appointment references and every appointment write."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, assert_never

from abita_s2s.insurance_state import insurance_ready
from abita_s2s.integrations.patient_middleware import Appointment, Receipt
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
from abita_s2s.results import reply
from abita_s2s.scheduling import answers
from abita_s2s.scheduling.answers import (
    CHOOSE_LOADED_APPOINTMENT,
    STALE_BEFORE_WRITE,
    UNAVAILABLE,
    BookOutcome,
    CancelOutcome,
    MoveOutcome,
)
from abita_s2s.scheduling.availability import (
    Offers,
    OfferedSlot,
    SchedulingContext,
    provider_name,
    scheduling_context,
)
from abita_s2s.state import CallState
from abita_s2s.writes import WriteKey, WriteLedger, WriteOutcome, write_effect

Refusal = Literal["refused", "expired", "failed", "unsent", "uncertain"]


def refusal(
    action: Literal["book", "cancel"], outcome: SchedulingOutcome | None
) -> Refusal:
    """What a middleware error outcome means for a booking or for a cancellation."""
    booking = action == "book"
    match outcome:
        case "provider_conflict" | "provider_rejected":
            return "refused"
        case "slot_unavailable" | "patient_context_mismatch":
            return "refused" if booking else "uncertain"
        case (
            "invalid_booking_token"
            | "booking_token_required"
            | "invalid_reschedule_token"
        ):
            return "expired" if booking else "uncertain"
        case "invalid_cancellation_token":
            return "uncertain" if booking else "expired"
        case "ownership_mismatch":
            return "uncertain" if booking else "refused"
        case "write_failed":
            return "failed"
        case None:
            return "unsent" if booking else "uncertain"
        case "validation" | "indeterminate_write" | "appointment_type_unresolved":
            return "uncertain"
        case _:
            assert_never(outcome)


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
        provider=provider_name(receipt.providerName or slot.provider),
        facility=receipt.locationName or get_office_profile(office).display_name,
        office=receipt.office,
        officeId=receipt.officeId,
        visitType=receipt.visitType,
        cancellationToken=receipt.cancellationToken,
        type=receipt.appointmentTypeName or "Appointment",
        appointmentTypeId=receipt.appointmentTypeId,
        rescheduleToken=receipt.rescheduleToken,
        confirmed=True,
    )


def booking_outcome(
    result: WriteReceipt | SchedulingFailure, new: Appointment | None
) -> BookOutcome:
    """A booking refusal counts whatever the receipt status; other non-error receipts are uncertain."""
    if new is not None:
        return "written"
    if isinstance(result, SchedulingFailure):
        return "uncertain" if result.uncertain else "unsent"
    if result.outcome == "appointment_type_unresolved" and result.missing:
        return "needs_input"
    outcome = refusal("book", result.outcome)
    if result.status != "error" and outcome not in ("refused", "expired"):
        return "uncertain"
    return outcome


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
    return refusal("cancel", result.outcome)


def move_outcome(
    result: RescheduleReceipt | SchedulingFailure,
    old_id: int,
    new: Appointment | None,
) -> MoveOutcome:
    if not isinstance(result, RescheduleReceipt):
        return "uncertain"
    if new is None:
        return "failed" if result.status == "failed" else "uncertain"
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
    offered: OfferedSlot | None = None


Operation = Callable[..., Awaitable[dict]]


class Scheduling:
    def __init__(self, state: CallState, http: SchedulingHTTP, *, now=None):
        self.state = state
        self.http = http
        self.now = now or (lambda: datetime.now(UTC))
        self._offers = Offers(state, http, lambda: self.now())
        self._references: dict[tuple[int, str, int], str] = {}
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
        key = (self.state.patient.revision, patient_id, appointment.id)
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

    async def availability(self, visit, start=None, office=None):
        if self._closed:
            return reply("unavailable", "blocked: Scheduling has closed for this call.")
        return await self._offers.search(
            visit,
            start,
            office,
            writing=bool(self._write_task and not self._write_task.done()),
        )

    @property
    def closed(self) -> bool:
        return self._closed

    async def book(
        self,
        *,
        slot_ref: str,
        reason: str,
        referrer: str,
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
        reason: str,
        referrer: str,
        referrer_phone: str | None,
        referrer_address: str | None,
        confirmed: Literal[True] | None,
        old_ref: str,
        call_id: str,
    ) -> str:
        return await self._execute(
            self._reschedule,
            slot_ref=slot_ref,
            reason=reason,
            referrer=referrer,
            referrer_phone=referrer_phone,
            referrer_address=referrer_address,
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
                        _,
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
        patient: Receipt,
        captured: SchedulingContext,
        key: WriteKey,
        outcome: WriteOutcome,
        receipt: MutationReceipt,
        *,
        target: Appointment | None,
    ) -> None:
        """Spend the target's authority when the outcome says so, then keep or forget the receipt."""
        if (
            target is not None
            and write_effect(outcome).spends_target
            and self._context() == captured
        ):
            self._appointments_stale(patient)
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
            self._appointments_stale(p)
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
        self._offers.invalidate()
        self._ledger.record(
            key, "uncertain", MutationReceipt(answers.uncertain("cancellation"))
        )
        result = await self.http.cancel(
            Cancellation(patientId=p.patientId, cancellationToken=old.cancellationToken)
        )
        outcome = cancellation_outcome(result, old.id)
        answer = answers.cancellation(outcome)
        self._report(
            p, call_id=call_id, cancellation_outcome=answer["outcome"], old=old
        )
        cancelled_id = old.id if outcome == "written" else None
        self._settle(
            p,
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
        reason: str,
        referrer: str,
        referrer_phone: str | None,
        referrer_address: str | None,
        confirmed: bool | None,
        call_id: str,
        old: Appointment | None = None,
    ) -> dict:
        slot_ref = slot_ref.strip().upper()
        key = (
            WriteKey(p.patientId, "reschedule", old.id)
            if old
            else WriteKey(
                p.patientId, "book", (slot_ref, self._offers.issued(slot_ref))
            )
        )
        if not old and (saved := self._ledger.get(key)):
            return self._replay(p, saved)
        offered = self._offers.get(slot_ref)
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
        if not old:
            for saved in self._ledger.receipts(p.patientId):
                if (
                    saved.offered
                    and saved.booked
                    and saved.offered.selection == offered.selection
                    and not self._cancelled(p.patientId, saved.booked.id)
                ):
                    return self._replay(p, saved)
        if not insurance_ready(self.state):
            self._offers.invalidate()
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
        if old and not old.rescheduleToken:
            self._appointments_stale(p)
            return reply(
                "needs_input",
                "needs_input: Reload appointments to obtain reschedule authorization, then reconfirm the move.",
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
        booking = Booking(
            patientId=p.patientId,
            patientName=p.name,
            dob=p.dob,
            bookingToken=slot.bookingToken,
            visitCategory=offered.visit,
            patientStatus=status,
            appointmentReason=reason.strip(),
            visitReason=reason.strip(),
            referringDoctor=referrer.strip(),
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
        missing = result.missing if isinstance(result, WriteReceipt) else []
        answer = answers.booking(outcome, description, missing)
        self._report(
            p,
            call_id=call_id,
            booking=result,
            booking_outcome=answer["outcome"],
            slot=slot,
        )
        self._settle(
            p,
            captured,
            key,
            outcome,
            MutationReceipt(answer, booked=new, offered=offered if new else None),
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
        answer = answers.move(outcome, description, old)
        if new is None:
            self._report(
                p,
                call_id=call_id,
                booking=result,
                booking_outcome=answer["outcome"],
                old=old,
                slot=slot,
            )
            self._settle(p, captured, key, outcome, MutationReceipt(answer), target=old)
            return answer
        cancelled = outcome == "written"
        self._report(
            p,
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
            offered=offered,
        )
        self._settle(p, captured, key, outcome, receipt, target=old)
        if cancelled:
            self._ledger.record(
                WriteKey(p.patientId, "reschedule", new.id), outcome, receipt
            )
        return answer

    async def _reschedule(
        self,
        p: Receipt,
        captured: SchedulingContext,
        *,
        old_ref: str,
        slot_ref: str,
        reason: str,
        referrer: str,
        referrer_phone: str | None,
        referrer_address: str | None,
        confirmed: bool | None,
        call_id: str,
    ) -> dict:
        old, key = self._target(p, "reschedule", old_ref)
        saved = self._ledger.get(key)
        offered = self._offers.get(slot_ref.strip().upper())
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
            if not (replacement_move and (different or offered is None)):
                if different and saved.cancelled_id is not None:
                    return reply(
                        "needs_input",
                        "needs_input: Choose the current appointment reference for a different move.",
                    )
                return self._replay(p, saved)
        if old is None:
            return reply("needs_input", CHOOSE_LOADED_APPOINTMENT)
        return await self._book(
            p,
            captured,
            slot_ref=slot_ref,
            reason=reason,
            referrer=referrer,
            referrer_phone=referrer_phone,
            referrer_address=referrer_address,
            confirmed=confirmed,
            call_id=call_id,
            old=old,
        )

    def _report(
        self,
        patient: Receipt,
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
                    providerName=provider_name(booking.providerName or slot.provider),
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
        reporter.appointment(evidence, call_id=call_id)

    def _appointments_stale(self, patient: Receipt) -> None:
        """Revoke loaded appointment authority until the patient is reloaded."""
        self.state.patient.active = patient.model_copy(
            update={"appointmentsStatus": "error"}
        )

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
        if list(appointments.values()) != patient.appointments:
            self.state.patient.active = patient.model_copy(
                update={
                    "appointments": list(appointments.values()),
                    "appointmentsStatus": patient.appointmentsStatus
                    if patient.appointmentsStatus == "error"
                    else ("found" if appointments else "none"),
                }
            )

    def _finish_change(self, result: dict, captured: SchedulingContext) -> dict:
        if self._context() != captured:
            return {
                **result,
                "answer": result["answer"]
                + " This result belongs to the earlier patient context; the patient changed during the operation.",
            }
        self._reconcile_receipts()
        return result
