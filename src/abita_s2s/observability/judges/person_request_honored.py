"""Person request honored."""

QUESTION = {
    "type": "noul",
    "instructions": "If the caller asked to speak with a person or to be transferred, did the agent transfer them promptly? Answer false if the agent said it was connecting the caller and then kept pitching or asking questions instead, said it could not transfer or that staff were unavailable without a tool result saying so, or repeated the request back without transferring. One brief offer to help first is fine. If the caller never asked for a person or a transfer, answer true.",
    "criteria": {
        "true": "The caller never asked for a person, or the agent transferred them promptly when asked.",
        "false": "The agent announced, ruled out, or delayed a transfer the caller asked for.",
    },
}
