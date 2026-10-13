"""Promises backed.

Fix for a no: Staff-task tool result wording and speaker rules: say only what a tool result supports.
"""

QUESTION = {
    "type": "noul",
    "instructions": "Check every statement the agent made about what has happened or will happen next. Did a tool result support each one? Answer FALSE if the agent said any of these without a tool result that says so: that staff WILL call the caller back, follow up, take care of it, email or send something, or contact them by a certain time (a saved staff task result only says a draft was saved for staff - it does not promise a callback); that it is transferring to a specific person or department (transfer_call has no destination); that it is checking the caller's record, appointment, or staff availability when no such lookup tool ran; that something is already done, booked, cancelled, noted, or sent ('listo', 'anotado', 'ya lo dejo') before the tool result for it; or that it will note or do something and then no tool ever did it. Saying staff will review the saved request, or that the request will be sent when the call ends, is supported.",
    "criteria": {
        "true": "Every statement about what happened or will happen next was supported by a tool result.",
        "false": "The agent promised a callback, follow-up, specific transfer destination, lookup, or completed action that no tool result supported.",
    },
}
