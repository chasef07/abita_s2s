"""Model-facing appointment tools and availability presentation."""

from typing import Literal

from livekit.agents import RunContext, function_tool

from abita_s2s.insurance_contract import CoverageType
from abita_s2s.offices import EASTERN, SchedulingOfficeChoice
from abita_s2s.scheduling.answers import UNAVAILABLE, slot_lines
from abita_s2s.scheduling.appointments import Scheduling
from abita_s2s.state import CallState
from abita_s2s.tools.context import bound


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
        With a plan, only doctors who take it are searched. Without one, only the moved
        appointment's doctor is searched.

        Args:
            visitType: medical or routine_vision; match the existing visit when rescheduling.
            insurancePlanId: The caller-confirmed plan's ID from the Insurance list for visitType.
                Null only when moving a loaded appointment and the caller says their
                insurance has not changed since it was booked.
            rescheduleAppointmentRef: The loaded appointment being moved; required when
                insurancePlanId is null.
            startDate: YYYY-MM-DD, tomorrow or later; null defaults to tomorrow.
                To search the next window, use the day after the returned searched-through date.
            office: Required caller-selected office for Hollywood/Sweetwater calls;
                omit for other offices. Hollywood routine_vision callers may also
                choose north-miami-beach-optical; the result says what to ask first.
        """
        if not bound(self._scheduling, context):
            return UNAVAILABLE
        result = await self._scheduling.availability(
            visitType, insurancePlanId, startDate, office, rescheduleAppointmentRef
        )
        lines = [result["answer"]]
        if "searchedFrom" in result:
            patient = self._scheduling.state.patient.active
            lines.append(
                f"Searched {result['searchedFrom']} through {result['searchedThrough']}"
                + (f" for {patient.name}." if patient else ".")
            )
        if "retry_same_search" in result and result["outcome"] == "availability_failed":
            lines.append(
                "Retry this search once."
                if result["retry_same_search"]
                else "Do not retry this search; ask staff for help."
            )
        if slots := result.get("slots"):
            today = self._scheduling.now().astimezone(EASTERN).date()
            lines += slot_lines(slots, result["soonest"], today)
        return "\n".join(lines)

    @function_tool
    async def book_appointment(
        self,
        context: RunContext[CallState],
        appointmentSlotRef: str,
        appointmentReason: str,
        referringDoctor: str,
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
            referringDoctor: The referring doctor's name, or "none" if not referred or an
                existing patient. Never narrate "none".
            referringDoctorPhone: The referring doctor's phone, or null if unknown.
            referringDoctorAddress: The referring doctor's office address, or null if unknown.
        """
        if not bound(self._scheduling, context):
            return UNAVAILABLE
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
        if not bound(self._scheduling, context):
            return UNAVAILABLE
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
        referringDoctor: str,
        readBack: Literal[True] | None,
        appointmentReason: str | None = None,
        referringDoctorPhone: str | None = None,
        referringDoctorAddress: str | None = None,
    ) -> str:
        """Move the caller-confirmed loaded appointment to a confirmed returned slot.

        Books first, then cancels the old visit. Report partial success and never repeat an uncertain booking.

        Args:
            oldAppointmentRef: The private reference of the loaded appointment being moved.
            appointmentSlotRef: As book_appointment describes.
            referringDoctor: As book_appointment describes.
            readBack: True only after the caller confirms the old appointment and the new
                date, time and provider; otherwise null.
            appointmentReason: Only a new reason the caller volunteered; never ask for one.
            referringDoctorPhone: As book_appointment describes.
            referringDoctorAddress: As book_appointment describes.
        """
        if not bound(self._scheduling, context):
            return UNAVAILABLE
        return await self._scheduling.reschedule(
            slot_ref=appointmentSlotRef,
            reason=appointmentReason,
            referrer=referringDoctor,
            referrer_phone=referringDoctorPhone,
            referrer_address=referringDoctorAddress,
            confirmed=readBack,
            old_ref=oldAppointmentRef,
            call_id=context.function_call.call_id,
        )
