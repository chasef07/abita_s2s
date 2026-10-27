"""Judge the required staff handoff action, not whether a human answered."""

QUESTION = {
    "type": "noul",
    "instructions": "Grade each caller request to speak with staff by the actual transfer_call tool call; human pickup is not required. Naming staff in passing, asking whether staff got a message, or speech-recognition noise is not a request. Eye emergencies are graded separately. Transfer immediately, with no offer or intake, for a returned call from a named staff member or when an earlier message or callback went unanswered. Otherwise the agent may ask one brief clarifying question, then must make one offer before transferring: specific work when the need is known and safe, a general offer when it is unknown. A question alone, a generic menu for a known need, or claiming it cannot do supported work is not an offer. Help before the staff request does not count unless the caller answered that offer by asking for staff. Clinical, urgent, or medication needs may be transferred without a task offer. If the caller accepts (says yes, answers, or lets the agent proceed) and does not ask again, the request is settled; giving a reason is not accepting. If they still want staff, the next action must be transfer_call, with no re-pitch, repeated question, deflection, or intake. If they drop the request or hang up first, grade only what came before.",
    "criteria": {
        "true": "Every staff request was transferred at the required point, settled by accepted help, or dropped by the caller, or no staff request occurred.",
        "false": "While the caller still wanted staff, the agent transferred without the required offer, pitched or asked more than once, deflected, asked intake questions, delayed a required immediate transfer, or never called transfer_call; or it transferred instead of giving safe help the caller accepted.",
    },
}
