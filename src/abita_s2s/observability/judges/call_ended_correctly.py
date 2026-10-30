"""Surface agent hangups before the caller is finished."""

from livekit.agents import ChatContext

QUESTION = {
    "type": "noul",
    "instructions": "Judge only the end_call that ended the call; one whose result says blocked did not end it, and a caller disconnect is not an agent hangup. At that moment the caller was finished if they had said goodbye or thanks, said they were done (such as being on their way), declined further help, or made no request after the agent asked once whether they needed anything else. Caller words transcribed after that end_call do not count if the caller had already finished.",
    "criteria": {
        "true": "The caller was finished with nothing left unhandled, or no end_call ended the call.",
        "false": "Right before the end_call, the caller was mid-sentence, asked a new question, gave new information, requested staff, or had an unhandled request, or the agent had just asked a question without waiting for the answer.",
    },
}


def skip(history: ChatContext, closeout: dict) -> dict | None:
    """Only an end_call that was not blocked hung up; transfers and caller hangups are not graded here."""
    ended = {
        item.call_id
        for item in history.items
        if item.type == "function_call" and item.name == "end_call"
    }
    blocked = {
        item.call_id
        for item in history.items
        if item.type == "function_call_output"
        and item.call_id in ended
        and item.output.startswith("blocked")
    }
    if ended - blocked:
        return None
    return {"status": "not_applicable", "reason": "no_agent_hangup"}
