"""Ended while caller active.

Fix for a no: end_call rule in thinker/speaker (ACU-122 family).
"""

QUESTION = {
    "type": "noul",
    "instructions": "Look at the end of the call. Did the agent end the call (end_call) only after the caller said goodbye, said they needed nothing else, or stopped responding? Answer FALSE if, right before the agent ended the call, the caller was still talking, had just asked a question or raised a new concern that went unanswered, asked for a person, or was in the middle of giving information. If the agent did not end the call, answer true.",
    "criteria": {
        "true": "The agent ended the call only after the caller was done, or the agent did not end the call.",
        "false": "The agent ended the call while the caller was still talking, asking something, or giving information.",
    },
}
