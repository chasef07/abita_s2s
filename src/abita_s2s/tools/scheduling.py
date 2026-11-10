"""Model-facing appointment tools and availability presentation."""

from typing import Literal

from livekit.agents import RunContext, function_tool

from abita_s2s.insurance_contract import CoverageType
from abita_s2s.offices import SchedulingOfficeChoice
from abita_s2s.scheduling.appointments import Scheduling
from abita_s2s.state import CallState


class SchedulingTools:
    def __init__(self, scheduling: Scheduling) -> None:
        self._scheduling = scheduling

    @function_tool
    async def list_available_appointments(
        self,
        context: RunContext[CallState],
        visitType: CoverageType,
        insurancePlanId: str | None,
        startDate: str | None = None,
        office: SchedulingOfficeChoice | None = None,
        rescheduleAppointmentRef: str | None = None,
    ) -> str:
        """Find eligible slots for the active patient in a 14-day Eastern-time window.

        Requires patient resolution or completed registration and no unfinished insurance write.
        With a plan, only doctors who take it are searched. With a null plan and a
        reference, only that appointment's doctor at its own office is searched.

        Args:
            visitType: medical or routine_vision; match the existing visit when rescheduling.
            insurancePlanId: The caller-confirmed plan's ID from the Insurance list for visitType.
                Null only when moving a loaded appointment and the caller says their
                insurance has not changed since it was booked.
            rescheduleAppointmentRef: The loaded appointment being moved. Every search for a
                move sends it; omit it for a new visit.
            startDate: YYYY-MM-DD, tomorrow or later; null defaults to tomorrow.
                To search the next window, use the day after the returned searched-through date.
            office: Required caller-selected office for Hollywood/Sweetwater calls;
                omit for other offices and to keep a moved appointment at its office.
                Hollywood routine_vision callers may also choose north-miami-beach-optical;
                the result says what to ask first.
        """
        return await self._scheduling.availability_text(
            visitType, insurancePlanId, startDate, office, rescheduleAppointmentRef
        )

    @function_tool
    async def book_appointment(
        self,
        context: RunContext[CallState],
        appointmentSlotRef: str,
        appointmentReason: str,
        referringDoctor: str | None,
        readBack: Literal[True] | None,
        referringDoctorPhone: str | None = None,
        referringDoctorAddress: str | None = None,
    ) -> str:
        """Book a new appointment using a returned slot after caller confirmation of date, time and provider.

        Claim success only from this result. Do not retry uncertain writes.
        Use reschedule_appointment to move an existing appointment.

        Args:
            appointmentSlotRef: The slot reference copied from the result line whose time you read back.
            appointmentReason: The visit reason the caller already gave, in their words. Never diagnose.
            readBack: True only after the caller confirms the read-back; otherwise null.
            referringDoctor: The referring doctor's name, or null if not referred or an
                existing patient.
            referringDoctorPhone: The referring doctor's phone, or null if unknown.
            referringDoctorAddress: The referring doctor's office address, or null if unknown.
        """
        return await self._scheduling.book(
            slot_ref=appointmentSlotRef,
            reason=appointmentReason,
            referrer=referringDoctor,
            referrer_phone=referringDoctorPhone,
            referrer_address=referringDoctorAddress,
            confirmed=readBack,
            call_id=context.function_call.call_id,
        )

    @function_tool
    async def cancel_appointment(
        self,
        context: RunContext[CallState],
        appointmentRef: str,
        readBack: Literal[True] | None,
    ) -> str:
        """Cancel only after verification and the caller confirms cancellation of the exact loaded appointment.

        Claim success only from the result; never retry uncertain cancellation.

        Args:
            appointmentRef: The private reference of the loaded appointment to cancel.
            readBack: True only after the caller confirms the exact date, time, provider
                and intent to cancel; otherwise null.
        """
        return await self._scheduling.cancel(
            confirmed=readBack,
            old_ref=appointmentRef,
            call_id=context.function_call.call_id,
        )

    @function_tool
    async def reschedule_appointment(
        self,
        context: RunContext[CallState],
        oldAppointmentRef: str,
        appointmentSlotRef: str,
        readBack: Literal[True] | None,
        appointmentReason: str | None = None,
    ) -> str:
        """Move a loaded appointment to a confirmed slot from a search that sent its reference.

        Books first, then cancels the old visit. Report partial success and never repeat an uncertain booking.

        Args:
            oldAppointmentRef: The private reference of the loaded appointment being moved,
                the same one its search sent.
            appointmentSlotRef: As book_appointment describes.
            readBack: True only after the caller confirms the old appointment and the new
                date, time and provider; otherwise null.
            appointmentReason: Only a new reason the caller volunteered; never ask for one.
        """
        return await self._scheduling.reschedule(
            old_ref=oldAppointmentRef,
            slot_ref=appointmentSlotRef,
            reason=appointmentReason,
            confirmed=readBack,
            call_id=context.function_call.call_id,
        )
