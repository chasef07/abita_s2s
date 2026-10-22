"""Appointment datetime correct.

Fix for a no: Booking/reschedule/cancel flow and tool results.
"""

from livekit.agents import ChatContext

ACTIONS = {"book_appointment", "reschedule_appointment", "cancel_appointment"}
CONFIRMED = {"booked", "cancelled", "rescheduled", "partial_reschedule"}

QUESTION = {
    "type": "noul",
    "instructions": "For every confirmed booking, rescheduling, or cancellation action, did the tool result match the caller's final intended appointment date and time? Use the final agreed date/time, including explicitly accepted alternatives, in the office timezone. For rescheduling check both the original appointment and the new date/time; for cancellation check the targeted appointment. Compare actual tool results, not the assistant's claim. Grade confirmed actions only; a failed or missing result does not establish a wrong date/time. For a partial reschedule, grade the confirmed new booking even if the old cancellation is unconfirmed.",
    "criteria": {
        "true": "Every confirmed appointment action's tool result matches the caller's intended date and time.",
        "false": "A confirmed action targets or produces a different appointment or date/time than the caller finally agreed to.",
    },
}


def skip(history: ChatContext, closeout: dict) -> dict | None:
    """Only compare dates when the closeout records a confirmed appointment change."""
    if any(fact["outcome"] in CONFIRMED for fact in closeout.get("domainOutcomes", ())):
        return None
    if any(
        item.type in {"function_call", "function_call_output"} and item.name in ACTIONS
        for item in history.items
    ):
        return {"status": "unknown", "reason": "no_confirmed_appointment_action"}
    return {"status": "not_applicable", "reason": "no_appointment_action"}
