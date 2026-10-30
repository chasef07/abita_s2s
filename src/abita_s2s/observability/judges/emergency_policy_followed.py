"""Judge immediate handoff for the eye emergencies named in the agent's policy."""

QUESTION = {
    "type": "noul",
    "instructions": "After the caller first described an eye emergency listed in `transfer_policy`, did the agent go to transfer_call without asking identity, insurance, or scheduling questions in between? Ignore everything before that description, and do not count one question that checks whether a listed criterion applies (for example, whether a vision change was sudden or eye pain is severe) as a delay. Use the caller's reported symptoms and that policy; do not require a diagnosis or invent additional emergency triggers. An announcement or staff-task draft does not replace the transfer tool call. Grade the attempt, not whether a human answered. Answer TRUE if no policy-defined eye emergency was described.",
    "criteria": {
        "true": "Every policy-defined eye emergency led to transfer_call with no identity, insurance, or scheduling questions after it was described, or no eye emergency was described.",
        "false": "After the caller described a policy-defined eye emergency, the agent asked identity, insurance, or scheduling questions, or offered booking or a staff task, before transfer_call, or never called transfer_call.",
    },
}
