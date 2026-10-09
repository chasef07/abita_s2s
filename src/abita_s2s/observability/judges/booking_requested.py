"""Booking requested."""

QUESTION = {
    "type": "boolean",
    "instructions": "Did the caller ask to book a new appointment, or to reschedule or cancel an existing appointment, at any point in the call? Count a request for an appointment for someone else (for example a child). Asking about an existing appointment's time or location, confirming it, or asking about prescriptions, orders, insurance, or billing is not a booking request by itself.",
    "criteria": {
        "true": "The caller asked to book, reschedule, or cancel an appointment.",
        "false": "The caller never asked to book, reschedule, or cancel an appointment.",
    },
}
