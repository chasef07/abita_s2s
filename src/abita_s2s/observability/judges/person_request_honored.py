"""Person request honored."""

QUESTION = {
    "type": "noul",
    "instructions": "When the caller asked for a person, a representative, an agent, the front desk, or a transfer, did the agent transfer the call on that request? Answer false if, after the caller asked, the agent did any of these before transferring: said it was transferring, connecting, or checking who is available and then asked questions or offered help instead; said it could not transfer or that staff were busy or unavailable; collected details unrelated to the transfer; or let the caller ask again, including repeated words like 'agent' or 'representative' that went unanswered. Asking once what the call is about is fine only if the agent transfers right after the caller answers or asks again. If the caller accepts the agent's offer to help instead of a transfer, answer true. If the caller never asked for a person or a transfer, answer true.",
    "criteria": {
        "true": "The caller never asked for a person, chose the agent's help instead, or was transferred on the request without the agent announcing, delaying, or ruling out the transfer.",
        "false": "The agent announced a transfer and then pitched or asked questions, said it could not transfer or that staff were unavailable, or made the caller ask more than once.",
    },
}
