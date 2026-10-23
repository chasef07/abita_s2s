"""What each scheduling write outcome tells the model."""

from collections.abc import Sequence
from typing import Literal, assert_never

from abita_s2s.integrations.patient_middleware import Appointment
from abita_s2s.results import reply

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
                "blocked: The reschedule failed. Reload appointments or ask staff to reconcile before another change.",
            )
        case "uncertain":
            return uncertain("reschedule")
        case _:
            assert_never(outcome)
