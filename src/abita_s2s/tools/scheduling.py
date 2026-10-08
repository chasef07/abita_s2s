"""Model-facing appointment tools and availability presentation."""

from typing import Literal

from livekit.agents import RunContext, function_tool

from abita_s2s.insurance_contract import CoverageType
from abita_s2s.offices import EASTERN, SharedSchedulingOffice
from abita_s2s.scheduling import (
    UNAVAILABLE,
    Scheduling,
    afternoon,
    eastern_datetime,
)
from abita_s2s.state import CallState
from abita_s2s.tools.context import bound


class SchedulingTools:
    def __init__(self, scheduling: Scheduling) -> None:
        self._scheduling = scheduling

    @property
    def tools(self):
        return [
            self.list_available_appointments,
            self.book_appointment,
            self.cancel_appointment,
            self.reschedule_appointment,
        ]

    @function_tool
    async def list_available_appointments(
        self,
        context: RunContext[CallState],
        visitType: CoverageType,
        startDate: str | None = None,
        office: SharedSchedulingOffice | None = None,
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
        if not bound(self._scheduling, context) or self._scheduling.closed:
            return UNAVAILABLE
        result = await self._scheduling.availability(visitType, startDate, office)
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
            today = self._scheduling.now().astimezone(EASTERN).date()
            lines.append("Soonest options: " + ", ".join(result["soonest"]))
            days = {}
            for slot in slots:
                start = eastern_datetime(slot["datetime"])
                rows = days.setdefault(start.date(), {})
                rows.setdefault((slot["provider"], afternoon(start)), []).append(
                    (start, slot)
                )
            for day, rows in days.items():
                days_away = (day - today).days
                relative = (
                    "today"
                    if days_away == 0
                    else "tomorrow"
                    if days_away == 1
                    else f"in {days_away} days"
                )
                first = next(iter(rows.values()))[0][0]
                lines += [
                    "",
                    f"{first:%A, %B} {day.day}, {day.year} ({relative}), {first:%Z}",
                ]
                for (provider, is_afternoon), entries in rows.items():
                    groups = [
                        f"{label} "
                        + ", ".join(
                            f"{start.strftime('%I:%M %p').lstrip('0')} {slot['appointmentSlotRef']}"
                            for start, slot in entries
                            if slot["shared"] is shared
                        )
                        for label, shared in (("open", False), ("shared", True))
                        if any(slot["shared"] is shared for _, slot in entries)
                    ]
                    lines.append(
                        f"  {provider} {'afternoon' if is_afternoon else 'morning'}: "
                        + "; ".join(groups)
                    )
        return "\n".join(lines)

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
            referringDoctor: New patients: reuse a supplied answer or ask "Did a doctor
                refer you?" and get the name; "none" if not referred. Existing patients:
                "none" without asking. Never narrate "none".
        """
        if not bound(self._scheduling, context):
            return UNAVAILABLE
        return await self._scheduling.book(
            slot_ref=appointmentSlotRef,
            reason=appointmentReason,
            referrer=referringDoctor,
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

        Use its private appointmentRef. readBack is true only after the caller confirms
        the exact date, time, provider and intent to cancel.
        Claim success only from the result; never retry uncertain cancellation.
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
        appointmentReason: str,
        referringDoctor: str,
        readBack: Literal[True] | None,
    ) -> str:
        """Move the caller-confirmed loaded appointment to a confirmed returned slot.

        Confirm the old appointment and read back the new date, time and provider before readBack=true.
        Reuse known visit and referral details; ask only for missing information.
        Books first, then cancels the old visit. Report partial success and never repeat an uncertain booking.

        Args:
            referringDoctor: New patients: reuse a supplied answer or ask "Did a doctor
                refer you?" and get the name; "none" if not referred. Existing patients:
                "none" without asking. Never narrate "none".
        """
        if not bound(self._scheduling, context):
            return UNAVAILABLE
        return await self._scheduling.reschedule(
            slot_ref=appointmentSlotRef,
            reason=appointmentReason,
            referrer=referringDoctor,
            confirmed=readBack,
            old_ref=oldAppointmentRef,
            call_id=context.function_call.call_id,
        )
