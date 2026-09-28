"""Appointment datetime correct."""

from livekit.agents import ChatContext

QUESTION = {
    "type": "noul",
    "instructions": "For every booking, rescheduling, or cancellation action, did the tool result match the caller's final intended appointment date and time? Use the final agreed date/time, including explicitly accepted alternatives, in the office timezone. For rescheduling check both the original appointment and the new date/time; for cancellation check the targeted appointment. Compare actual tool results, not the assistant's claim. Missing results cannot establish a match.",
    "criteria": {
        "true": "Every appointment action's tool result confirms the caller's intended date and time.",
        "false": "Any action targets or produces the wrong date/time, or there is insufficient evidence of a matching appointment action.",
    },
}


def is_applicable(history: ChatContext) -> bool:
    """Run only after a booking, reschedule, or cancellation tool returns."""
    return any(
        item.type == "function_call_output"
        and item.name
        in {"book_appointment", "reschedule_appointment", "cancel_appointment"}
        for item in history.items
    )
