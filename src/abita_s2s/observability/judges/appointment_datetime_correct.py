"""Appointment datetime correct.

Fix for a no: Booking/reschedule/cancel flow and tool results.
"""

from livekit.agents import ChatContext

QUESTION = {
    "type": "noul",
    "instructions": "For every confirmed booking, rescheduling, or cancellation action, did the tool result match the caller's final intended appointment date and time? Use the final agreed date/time, including explicitly accepted alternatives, in the office timezone. For rescheduling check both the original appointment and the new date/time; for cancellation check the targeted appointment. Compare actual tool results, not the assistant's claim. Grade confirmed actions only; a failed or missing result does not establish a wrong date/time. For a partial reschedule, grade the confirmed new booking even if the old cancellation is unconfirmed.",
    "criteria": {
        "true": "Every confirmed appointment action's tool result matches the caller's intended date and time.",
        "false": "A confirmed action targets or produces a different appointment or date/time than the caller finally agreed to.",
    },
}


def skip(history: ChatContext) -> dict | None:
    """Only compare dates when a write confirmed an appointment change."""
    actions = [
        item
        for item in history.items
        if item.type in {"function_call", "function_call_output"}
        and item.name
        in {"book_appointment", "reschedule_appointment", "cancel_appointment"}
    ]
    if not actions:
        return {"status": "not_applicable", "reason": "no_appointment_action"}
    if not any(
        item.type == "function_call_output"
        and not item.is_error
        and str(item.output)
        .strip()
        .startswith(("success:", "blocked: The new appointment is booked for "))
        for item in actions
    ):
        return {"status": "unknown", "reason": "no_confirmed_appointment_action"}
    return None
