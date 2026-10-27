"""Surface agent hangups before the caller is finished."""

QUESTION = {
    "type": "noul",
    "instructions": "Judge only the end_call that ended the call; one whose result says blocked did not end it, and a caller disconnect is not an agent hangup. At that moment the caller was finished if they had said goodbye or thanks, said they were done (such as being on their way), declined further help, or made no request after the agent asked once whether they needed anything else. Caller words transcribed after that end_call do not count if the caller had already finished.",
    "criteria": {
        "true": "The caller was finished with nothing left unhandled, or no end_call ended the call.",
        "false": "Right before the end_call, the caller was mid-sentence, asked a new question, gave new information, requested staff, or had an unhandled request, or the agent had just asked a question without waiting for the answer.",
    },
}
