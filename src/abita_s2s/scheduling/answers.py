"""What the offered slots and each scheduling write outcome tell the model."""

from datetime import date
from typing import Literal, assert_never

from abita_s2s.integrations.scheduling_http import SchedulingOutcome
from abita_s2s.records import Appointment
from abita_s2s.results import reply
from abita_s2s.scheduling.availability import afternoon

UNAVAILABLE = "blocked: Scheduling is unavailable."
STALE_BEFORE_WRITE = "blocked: The call, patient, or insurance changed before the appointment operation started."
CHOOSE_LOADED_APPOINTMENT = "needs_input: Choose and confirm the exact currently loaded appointment. Reload patient appointments if needed."
RELOAD_BEFORE_MOVE = "needs_input: This appointment must be reloaded before moving it. Resolve the patient again to reload appointments, then repeat this search."
PATIENT_CHANGED_DURING_WRITE = " This result is for the patient who was active when it started; the active patient has since changed."

BookOutcome = Literal[
    "written", "refused", "expired", "failed", "unsent", "needs_input", "uncertain"
]
CancelOutcome = Literal[
    "written", "refused", "expired", "failed", "unsent", "uncertain"
]
MoveOutcome = Literal["written", "partial", "refused", "expired", "failed", "uncertain"]


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


def sentences(*parts: str) -> str:
    """One answer from its parts, leaving out a middleware reason that was not sent."""
    return " ".join(part.strip() for part in parts if part.strip())


def failed(noun: str, message: str = "") -> dict:
    return reply(
        "failed",
        sentences(
            f"blocked: The {noun} failed.",
            message,
            "Ask staff for help or explicitly retry after resolving the failure.",
        ),
    )


def uncertain(noun: str) -> dict:
    return reply(
        "uncertain",
        f"blocked: The {noun} outcome could not be confirmed. Do not repeat the write or claim success; staff must reconcile the appointment record.",
    )


def booking(outcome: BookOutcome, description: str, message: str) -> dict:
    match outcome:
        case "written":
            return reply("booked", f"success: Booked {description}.")
        case "refused" | "expired":
            return reply(
                "rejected",
                sentences(
                    "needs_input: That slot could not be booked.",
                    message,
                    "Reload availability or the existing appointment as needed before confirming another choice.",
                ),
            )
        case "needs_input":
            return reply(
                "needs_input",
                sentences(
                    "needs_input: That slot could not be booked.",
                    message,
                    "If a different search resolves this, search again; otherwise offer a staff request.",
                ),
            )
        case "failed" | "unsent":
            return failed("booking", message)
        case "uncertain":
            return uncertain("booking")
        case _:
            assert_never(outcome)


def cancellation(outcome: CancelOutcome, message: str) -> dict:
    match outcome:
        case "written":
            return reply(
                "cancelled", "success: The selected appointment was cancelled."
            )
        case "expired":
            return reply(
                "rejected",
                sentences(
                    "needs_input: The appointment details expired.",
                    message,
                    "Reload appointments and reconfirm the exact cancellation.",
                ),
            )
        case "refused":
            return reply(
                "rejected",
                sentences(
                    "needs_input: Cancellation was not completed.",
                    message,
                    "Reload appointments and reconfirm the exact appointment, or ask office staff for help.",
                ),
            )
        case "failed" | "unsent":
            return failed("cancellation", message)
        case "uncertain":
            return uncertain("cancellation")
        case _:
            assert_never(outcome)


def move(
    outcome: MoveOutcome,
    description: str,
    old: Appointment,
    *,
    reason: SchedulingOutcome | None,
    message: str,
) -> dict:
    """A move the middleware refused keeps today's failed label; only its answer is specific."""
    unchanged = f"Your old appointment on {old.date} at {old.time} was not changed."
    reload_first = "Resolve the patient again to reload appointments, then search again"
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
        case "refused" if reason == "slot_unavailable":
            return reply(
                "failed",
                sentences(
                    "needs_input: That time was just taken.",
                    unchanged,
                    f"{reload_first} and offer another time.",
                ),
            )
        case "refused":
            return reply(
                "failed",
                sentences(
                    "needs_input: The reschedule was refused.",
                    message,
                    unchanged,
                    f"{reload_first} and reconfirm the move, or offer a staff request.",
                ),
            )
        case "expired":
            return reply(
                "failed",
                sentences(
                    "needs_input: The appointment details expired.",
                    message,
                    unchanged,
                    f"{reload_first} and reconfirm the move.",
                ),
            )
        case "failed":
            return reply(
                "failed",
                sentences(
                    "blocked: The reschedule failed.",
                    unchanged,
                    message,
                    "Reload appointments or ask staff to reconcile before another change.",
                ),
            )
        case "uncertain":
            return uncertain("reschedule")
        case _:
            assert_never(outcome)
