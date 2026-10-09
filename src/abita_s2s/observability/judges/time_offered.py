"""Time offered."""

from livekit.agents import ChatContext

QUESTION = {
    "type": "boolean",
    "instructions": "Did the agent offer the caller at least one specific appointment date and time taken from an availability tool result? A vague promise, a staff request to find a time, or a time the caller proposed that the agent did not confirm as available does not count.",
    "criteria": {
        "true": "The agent offered at least one specific date and time from returned availability.",
        "false": "The agent never offered a specific available date and time.",
    },
}


def is_applicable(history: ChatContext) -> bool:
    """Run only after an availability search returns."""
    return any(
        item.type == "function_call_output"
        and item.name == "list_available_appointments"
        for item in history.items
    )
