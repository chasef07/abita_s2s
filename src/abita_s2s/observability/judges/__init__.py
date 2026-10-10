"""Named judge definitions and the seated jury used by the call evaluator."""

from abita_s2s.observability.judges import (
    appointment_datetime_correct,
    booking_requested,
    clear_and_responsive,
    expressed_sentiment,
    need_understood,
    office_rules_grounded,
    person_request_honored,
    right_help,
    time_offered,
)

JURORS = ("typesafe-ai/jev", "microsoft/microsoft-decision-1")

QUESTIONS = {
    "booking_requested": booking_requested.QUESTION,
    "time_offered": time_offered.QUESTION,
    "need_understood": need_understood.QUESTION,
    "right_help": right_help.QUESTION,
    "clear_and_responsive": clear_and_responsive.QUESTION,
    "person_request_honored": person_request_honored.QUESTION,
    "office_rules_grounded": office_rules_grounded.QUESTION,
    "appointment_datetime_correct": appointment_datetime_correct.QUESTION,
    "expressed_sentiment": expressed_sentiment.QUESTION,
}

GATES = {
    "appointment_datetime_correct": (
        appointment_datetime_correct.is_applicable,
        "no_appointment_action_result",
    ),
    "time_offered": (time_offered.is_applicable, "no_availability_result"),
}
