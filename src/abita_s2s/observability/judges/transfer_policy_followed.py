"""Judge the required staff handoff action, not whether a human answered."""

QUESTION = {
    "type": "noul",
    "instructions": "Did the agent follow the staff-transfer policy for every request to speak with staff? A caller returning a call for a named staff member must be transferred immediately, without identity or routine intake. For an ordinary staff request, make one specific offer to complete supported work or save a safe non-urgent staff task. If the caller accepts, help; explaining their reason alone is not acceptance. If they decline or still want staff after the offer, transfer without another pitch or intake questions. Skip the offer when the same approach already failed or immediate transfer is required. One brief clarification is allowed if the need is unclear, but an answer is not required to transfer. Grade the actual tool call; an announcement alone is not a transfer, and human pickup is not required to pass. Answer TRUE if no staff request occurred. Eye emergencies are assessed separately.",
    "criteria": {
        "true": "Every staff request followed the policy, including providing accepted help or attempting transfer_call at the required point, or no staff request occurred.",
        "false": "The agent omitted or delayed a required staff transfer, skipped the required first offer to help, or transferred instead of providing the help the caller accepted.",
    },
}
