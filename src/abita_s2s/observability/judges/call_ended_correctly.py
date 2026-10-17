"""Surface agent hangups before the caller is finished."""

QUESTION = {
    "type": "noul",
    "instructions": "Did the agent wait until the caller was finished before calling end_call? Answer FALSE if the caller was still speaking, giving information, asking a question, requesting staff, or had an unhandled request when the agent ended the call. A goodbye, declining further help, or completion of the permitted no-response procedure supports ending. A caller disconnect is not an agent hangup. Answer TRUE if the agent did not call end_call.",
    "criteria": {
        "true": "The agent ended only after the caller was finished with no unhandled request, followed the permitted no-response procedure, or did not end the call.",
        "false": "The agent ended while the caller was still speaking or needed an unanswered question or request handled.",
    },
}
