"""Insurance plan as stated.

Fix for a no: Thinker insurance step: pass the caller's complete plan words. (probation)
"""

from livekit.agents import ChatContext

QUESTION = {
    "type": "noul",
    "instructions": "Did every check_insurance call use the plan as the caller described it, including words like Medicaid, Medicare, HMO, PPO, or a network name, and was the plan checked again after the caller added to or corrected the plan? Normal spelling or capitalization changes are fine.",
    "criteria": {
        "true": "Every insurance check used the caller's complete plan description, including later additions or corrections.",
        "false": "An insurance check dropped or changed part of the plan the caller described, or was not repeated after the caller corrected it.",
    },
}


def is_applicable(history: ChatContext) -> bool:
    """Run only after an insurance check returns."""
    return any(
        item.type == "function_call_output" and item.name == "check_insurance"
        for item in history.items
    )
