"""Model-facing appointment tools and availability presentation."""

import asyncio
from datetime import datetime
from typing import Literal

from livekit.agents import RunContext, ToolError, function_tool

from abita_s2s.scheduling import EASTERN, Scheduling, VisitType
from abita_s2s.state import CallState


async def _announce(context: RunContext[CallState], action: str) -> None:
    async with asyncio.timeout(15):
        await context.wait_for_playout()
        speech = context.session.generate_reply(
            instructions=f"Say only: One moment while I {action} your appointment. Use the caller's language.",
            tool_choice="none",
        )
        await speech.wait_for_playout()
    if speech.interrupted or speech.exception() is not None:
        raise ToolError("Announcement did not complete. No appointment was changed.")


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
        if context.userdata is not self._scheduling.state or self._scheduling.closed:
            return "blocked: Scheduling is unavailable."
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
            lines.append("")
            for slot in slots:
                start = datetime.fromisoformat(slot["datetime"])
                start = (
                    start.replace(tzinfo=EASTERN)
                    if start.tzinfo is None
                    else start.astimezone(EASTERN)
                )
                days = (start.date() - today).days
                if days == 0:
                    relative = "today"
                elif days == 1:
                    relative = "tomorrow"
                else:
                    relative = f"in {days} days"
                clock = start.strftime("%I:%M %p %Z").lstrip("0")
                lines.append(
                    f"{slot['appointmentSlotRef']} – {start:%A, %B} {start.day}, "
                    f"{start.year} at {clock} ({relative}) — {slot['provider']}"
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
            referringDoctor: Caller-provided referring doctor. Reuse an answer already
                supplied; otherwise ask "Did a doctor refer you?" If yes, ask for the
                name. Use internal value "none" only when the caller says they have
                no referring doctor. Do not ask whether to put or mark none, or
                narrate the internal value.
        """
        if readBack is True:
            await _announce(context, "book")
        return await self._scheduling.book(
            context,
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
        return await self._scheduling.cancel(
            context, confirmed=readBack, old_ref=appointmentRef
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
        if readBack is True:
            await _announce(context, "reschedule")
        return await self._scheduling.reschedule(
            context,
            slot_ref=appointmentSlotRef,
            reason=appointmentReason,
            referrer=referringDoctor,
            confirmed=readBack,
            old_ref=oldAppointmentRef,
        )
