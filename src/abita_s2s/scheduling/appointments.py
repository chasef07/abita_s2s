"""One per-call scheduling owner: private appointment references and write receipts."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from abita_s2s.insurance_state import insurance_ready
from abita_s2s.integrations.patient_middleware import Appointment, Receipt
from abita_s2s.integrations.scheduling_http import (
    Booking,
    Cancellation,
    RescheduleReceipt,
    SchedulingFailure,
    SchedulingHTTP,
    WriteReceipt,
)
from abita_s2s.offices import get_office_profile
from abita_s2s.results import reply
from abita_s2s.scheduling.answers import (
    CHOOSE_LOADED_APPOINTMENT,
    STALE_BEFORE_WRITE,
    UNAVAILABLE,
)
from abita_s2s.scheduling.availability import (
    Offers,
    OfferedSlot,
    SchedulingContext,
    provider_name,
    scheduling_context,
)
from abita_s2s.state import CallState


@dataclass(repr=False)
class MutationReceipt:
    result: dict
    booked: Appointment | None = None
    cancelled_id: int | None = None
    offered: OfferedSlot | None = None


class Scheduling:
    def __init__(self, state: CallState, http: SchedulingHTTP, *, now=None):
        self.state = state
        self.http = http
        self.now = now or (lambda: datetime.now(UTC))
        self._offers = Offers(state, http, lambda: self.now())
        self._references: dict[tuple, str] = {}
        self._next_ref = 0
        self._write_task = None
        self._closed = False
        self._receipts: dict[tuple, MutationReceipt] = {}

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

    async def _execute(self, operation, **arguments) -> str:
        if self._closed:
            return UNAVAILABLE
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
                return reply("stale", STALE_BEFORE_WRITE)
            result = await operation(patient, captured, **arguments)
            return self._finish_change(result, captured)

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
        self._receipts[receipt_key] = MutationReceipt(
            self._write_failure(
                SchedulingFailure(reason="pending", uncertain=True), "cancellation"
            )
        )
        result = await self.http.cancel(
            Cancellation(patientId=p.patientId, cancellationToken=old.cancellationToken)
        )
        outcome = self._cancel_result(result, old.id)
        self._report(
            p, cancellation_outcome=outcome["outcome"], old=old, call_id=call_id
        )
        if (
            outcome["outcome"] == "rejected"
            or (outcome["outcome"] == "failed" and isinstance(result, WriteReceipt))
        ) and self._context() == captured:
            self._appointments_stale(p)
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
        referrer_phone: str | None,
        referrer_address: str | None,
        confirmed: bool | None,
        call_id: str,
        old: Appointment | None = None,
    ) -> dict:
        slot_ref = slot_ref.strip().upper()
        receipt_key = (
            (p.patientId, "reschedule", old.id)
            if old
            else (p.patientId, "book", (slot_ref, self._offers.issued(slot_ref)))
        )
        if not old and (saved := self._receipts.get(receipt_key)):
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
        self._receipts[receipt_key] = MutationReceipt(
            self._write_failure(
                SchedulingFailure(reason="pending", uncertain=True), "booking"
            )
        )
        reschedule = None
        if old:
            reschedule = await self.http.reschedule(booking)
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
                if outcome["outcome"] == "failed" and self._context() == captured:
                    self._appointments_stale(p)
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
            result = await self.http.book(booking)
        outcome = self._book_result(result, description)
        if not old:
            self._report(
                p,
                booking=result,
                booking_outcome=outcome["outcome"],
                slot=slot,
                call_id=call_id,
            )
        if outcome["outcome"] != "booked":
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
            outcome = reply(
                "rescheduled" if cancelled else "partial_reschedule",
                (
                    f"success: Your new appointment is booked for {description}. Your old appointment on {old.date} at {old.time} is cancelled. Tell the caller both outcomes."
                    if cancelled
                    else f"blocked: The new appointment is booked for {description}, but cancellation of the old appointment requires staff reconciliation. Do not book again."
                ),
            )
            self._report(
                p,
                booking=result,
                booking_outcome="booked",
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
        referrer_phone: str | None,
        referrer_address: str | None,
        confirmed: bool | None,
        call_id: str,
    ) -> dict:
        old, receipt_key = self._target(p, "reschedule", old_ref)
        saved = self._receipts.get(receipt_key)
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
            evidence["bookingResult"] = {"status": booking_outcome}
            if booking_outcome == "booked":
                evidence["newAppointmentId"] = str(booking.appointmentId)
                evidence["bookingResult"].update(
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
        if self._context() != captured:
            return {
                **result,
                "answer": result["answer"]
                + " This result belongs to the earlier patient context; the patient changed during the operation.",
            }
        self._reconcile_receipts()
        return result

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
            if result.status == "booked" and result.appointmentId:
                return reply("booked", f"success: Booked {description}.")
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
                "provider_conflict",
                "provider_rejected",
                "patient_context_mismatch",
            ):
                return reply(
                    "rejected",
                    "needs_input: That slot could not be booked. Reload availability or the existing appointment as needed before confirming another choice.",
                )
            if result.status == "error" and not result.outcome:
                result = SchedulingFailure(reason="validation")
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
