"""What the offered slots and each scheduling write outcome tell the model."""

from collections.abc import Sequence
from datetime import date
from typing import Literal, assert_never

from abita_s2s.integrations.patient_middleware import Appointment
from abita_s2s.results import reply
from abita_s2s.scheduling.availability import afternoon

UNAVAILABLE = "blocked: Scheduling is unavailable."
STALE_BEFORE_WRITE = (
    "blocked: The call or patient changed before the appointment operation started."
)
CHOOSE_LOADED_APPOINTMENT = "needs_input: Choose and confirm the exact currently loaded appointment. Reload patient appointments if needed."

BookOutcome = Literal[
    "written", "refused", "expired", "failed", "unsent", "needs_input", "uncertain"
]
CancelOutcome = Literal[
    "written", "refused", "expired", "failed", "unsent", "uncertain"
]
MoveOutcome = Literal["written", "partial", "failed", "uncertain"]


def slot_lines(slots: list[dict], soonest: list[str], today: date) -> list[str]:
    """The offered slots by day, then provider and half-day, open before shared."""
    lines = ["Soonest options: " + ", ".join(soonest)]
    days = {}
    for slot in slots:
        rows = days.setdefault(slot["start"].date(), {})
        rows.setdefault((slot["provider"], afternoon(slot["start"])), []).append(slot)
    for day, rows in days.items():
        days_away = (day - today).days
        relative = (
            "today"
            if days_away == 0
            else "tomorrow"
            if days_away == 1
            else f"in {days_away} days"
        )
        first = next(iter(rows.values()))[0]["start"]
        lines += [
            "",
            f"{first:%A, %B} {day.day}, {day.year} ({relative}), {first:%Z}",
        ]
        for (provider, is_afternoon), entries in rows.items():
            groups = [
                f"{label} "
                + ", ".join(
                    f"{slot['start'].strftime('%I:%M %p').lstrip('0')} {slot['appointmentSlotRef']}"
                    for slot in entries
                    if slot["shared"] is shared
                )
                for label, shared in (("open", False), ("shared", True))
                if any(slot["shared"] is shared for slot in entries)
            ]
            lines.append(
                f"  {provider} {'afternoon' if is_afternoon else 'morning'}: "
                + "; ".join(groups)
            )
    return lines


def failed(noun: str) -> dict:
    return reply(
        "failed",
        f"blocked: The {noun} failed. Ask staff for help or explicitly retry after resolving the failure.",
    )


def uncertain(noun: str) -> dict:
    return reply(
        "uncertain",
        f"blocked: The {noun} outcome could not be confirmed. Do not repeat the write or claim success; staff must reconcile the appointment record.",
    )


def booking(outcome: BookOutcome, description: str, missing: Sequence[str]) -> dict:
    match outcome:
        case "written":
            return reply("booked", f"success: Booked {description}.")
        case "refused" | "expired":
            return reply(
                "rejected",
                "needs_input: That slot could not be booked. Reload availability or the existing appointment as needed before confirming another choice.",
            )
        case "needs_input":
            return reply(
                "needs_input",
                f"needs_input: Booking needs additional facts: {', '.join(missing)}. Clarify these before trying again.",
            )
        case "failed" | "unsent":
            return failed("booking")
        case "uncertain":
            return uncertain("booking")
        case _:
            assert_never(outcome)


def cancellation(outcome: CancelOutcome) -> dict:
    match outcome:
        case "written":
            return reply(
                "cancelled", "success: The selected appointment was cancelled."
            )
        case "expired":
            return reply(
                "rejected",
                "needs_input: The appointment details expired. Reload appointments and reconfirm the exact cancellation.",
            )
        case "refused":
            return reply(
                "rejected",
                "needs_input: Cancellation was not completed. Reload appointments and reconfirm the exact appointment, or ask office staff for help.",
            )
        case "failed" | "unsent":
            return failed("cancellation")
        case "uncertain":
            return uncertain("cancellation")
        case _:
            assert_never(outcome)


def move(outcome: MoveOutcome, description: str, old: Appointment) -> dict:
    match outcome:
        case "written":
            return reply(
                "rescheduled",
                f"success: Your new appointment is booked for {description}. Your old appointment on {old.date} at {old.time} is cancelled. Tell the caller both outcomes.",
            )
        case "partial":
            return reply(
                "partial_reschedule",
                f"blocked: The new appointment is booked for {description}, but cancellation of the old appointment requires staff reconciliation. Do not book again.",
            )
        case "failed":
            return reply(
                "failed",
                f"blocked: The reschedule failed. Your old appointment on {old.date} at {old.time} was not changed. Reload appointments or ask staff to reconcile before another change.",
            )
        case "uncertain":
            return uncertain("reschedule")
        case _:
            assert_never(outcome)
