"""New booking requested.

Not a failure: fact for booking conversion.
"""

QUESTION = {
    "type": "noul",
    "instructions": "Did the caller ask to book a NEW appointment, for themselves or someone else (for example a child)? Rescheduling, moving, or cancelling an existing appointment does not count. Asking about an existing appointment, insurance, prescriptions, glasses, records, or billing does not count by itself.",
    "criteria": {
        "true": "The caller asked to book a new appointment.",
        "false": "The caller did not ask to book a new appointment (they may have asked to cancel or reschedule, or something else).",
    },
}
