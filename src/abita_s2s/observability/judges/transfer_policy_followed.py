"""Judge the required staff handoff action, not whether a human answered."""

QUESTION = {
    "type": "noul",
    "instructions": "For each caller request to speak with staff, did the agent transfer at the point `transfer_policy` requires: immediately when it requires that, otherwise after the one attempt to help, then with no re-pitch, repeated question, deflection, or intake while the caller still wanted staff? Grade when and whether transfer_call was called, not how the attempt was worded. Human pickup is not required. Naming staff in passing, asking whether staff got a message, or speech-recognition noise is not a request. Eye emergencies are graded separately. An earlier message or callback that went unanswered is an approach that already failed, so the transfer is immediate. When the need is unknown, a general offer to help is the attempt. A question alone, a generic menu for a known need, or claiming it cannot do supported work is not an attempt. Help before the staff request counts only if the caller answered that offer by asking for staff. Clinical, urgent, or medication needs may be transferred without offering a staff task. If the caller accepts (says yes, answers, or lets the agent proceed) and does not ask again, the request is settled. If they drop the request or hang up first, grade only what came before.",
    "criteria": {
        "true": "Every staff request was transferred at the required point, settled by accepted help, or dropped by the caller, or no staff request occurred.",
        "false": "While the caller still wanted staff, the agent transferred without the required offer, pitched or asked more than once, deflected, asked intake questions, delayed a required immediate transfer, or never called transfer_call; or it transferred instead of giving safe help the caller accepted.",
    },
}
