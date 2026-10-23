"""What scheduling writes tell the model."""

UNAVAILABLE = "blocked: Scheduling is unavailable."
STALE_BEFORE_WRITE = (
    "blocked: The call or patient changed before the appointment operation started."
)
CHOOSE_LOADED_APPOINTMENT = "needs_input: Choose and confirm the exact currently loaded appointment. Reload patient appointments if needed."
