"""Judge immediate handoff for the eye emergencies named in the agent's policy."""

QUESTION = {
    "type": "noul",
    "instructions": "When the caller described an eye emergency listed in `transfer_policy`, did the agent immediately call transfer_call without first collecting identity or doing routine intake? Use the caller's reported symptoms and that policy; do not require a diagnosis or invent additional emergency triggers. An announcement or staff-task draft does not replace the transfer tool call. Grade the attempt, not whether a human answered. Answer TRUE if no policy-defined eye emergency was described.",
    "criteria": {
        "true": "Every policy-defined eye emergency prompted an immediate transfer_call before identity or routine intake, or no eye emergency was described.",
        "false": "The caller described a policy-defined eye emergency and the agent omitted or delayed the required transfer_call for identity or routine intake.",
    },
}
