"""Surface agent hangups before the caller is finished."""

QUESTION = {
    "type": "noul",
    "instructions": "Did the agent wait until the caller was finished before ending the call? Only an end_call that ended the call counts: an end_call whose result says blocked did not end it and is not a hangup, and a caller disconnect is not an agent hangup. Answer TRUE if no end_call ended the call. Judge the moment of the end_call that ended it. The caller was finished if, before it, they said goodbye or thanks, said they were done (including remarks such as being on their way), or declined further help, or the agent asked once whether they needed anything else and got no request, and nothing they asked for was left unhandled. Caller words transcribed after that end_call do not make it FALSE when the caller had already finished. Answer FALSE if, right before that end_call, the caller was mid-sentence, asked a new question, gave new information, requested staff, or had an unhandled request, or the agent ended right after asking its own question without waiting for the answer.",
    "criteria": {
        "true": "The agent ended only after the caller was finished with nothing left unhandled, or no end_call ended the call.",
        "false": "The agent ended the call while the caller was mid-sentence, had a new question or unhandled request, or had not yet answered the agent's own question.",
    },
}
