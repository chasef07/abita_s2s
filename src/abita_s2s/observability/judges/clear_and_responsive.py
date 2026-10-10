"""Clear and responsive."""

QUESTION = {
    "type": "boolean",
    "instructions": "Was the conversation clear and responsive from the caller's point of view? Answer false if the agent asked avoidable repeated questions, collected information it could not use for the caller's request, gave confusing or contradictory statements, or if the caller had to check whether the agent was still there or repeat themselves because the agent did not respond. These are not failures: a caller who asks for a person or does not want to talk to an AI and is transferred promptly (a brief offer to help first is fine); a transfer or staff request because a tool result says the agent cannot complete the request, such as insurance that cannot be verified; a short call; ordinary clarification of a hard-to-hear name or number, asked once or twice; caller-requested pauses.",
    "criteria": {
        "true": "The conversation was clear and responsive, including prompt transfers the caller asked for.",
        "false": "The conversation had avoidable repetition, wasted questions, confusing statements, or unresponsiveness.",
    },
}
