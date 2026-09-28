"""Conversation responsive."""

QUESTION = {
    "type": "noul",
    "instructions": "Did the conversation remain responsive, without evidence that the agent went silent or stalled while the caller was waiting for it to continue? Look for caller attempts to regain the agent's attention, such as repeated 'hello', 'are you there', repeating an unanswered question or answer, or saying the line went quiet or seems disconnected. Interpret these in context: an opening greeting, an ordinary clarification, a correction, background speech, or a caller-requested pause is not a stall. A brief agent acknowledgment without useful continuation can still be a stall. Later recovery or successful task completion does not erase an earlier stall. Judge observable conversational evidence, not the technical cause. Do not infer silence or its duration from missing transcript content or timestamps alone.",
    "criteria": {
        "true": "The conversation shows no evidence that the caller had to regain the agent's attention or repeat themselves because it stopped responding or progressing.",
        "false": "The caller's words and surrounding exchange indicate the agent stopped responding or progressing while the caller waited, even if it eventually recovered.",
    },
}
