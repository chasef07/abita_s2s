"""Named judge definitions and the seated jury used by the call evaluator."""

from abita_s2s.observability.judges import (
    appointment_datetime_correct,
    ended_while_caller_active,
    expressed_sentiment,
    insurance_plan_as_stated,
    new_booking_requested,
    office_question_answered,
    office_rules_grounded,
    promises_backed,
    requested_time_honored,
    time_offered,
)

JURORS = (
    "typesafe/jev-1.13",
    "perplexity/pplx-decider-v1.1-27b",
    "microsoft/microsoft-decision-1",
)

QUESTIONS = {
    "new_booking_requested": new_booking_requested.QUESTION,
    "time_offered": time_offered.QUESTION,
    "promises_backed": promises_backed.QUESTION,
    "ended_while_caller_active": ended_while_caller_active.QUESTION,
    "office_rules_grounded": office_rules_grounded.QUESTION,
    "office_question_answered": office_question_answered.QUESTION,
    "appointment_datetime_correct": appointment_datetime_correct.QUESTION,
    "insurance_plan_as_stated": insurance_plan_as_stated.QUESTION,
    "requested_time_honored": requested_time_honored.QUESTION,
    "expressed_sentiment": expressed_sentiment.QUESTION,
}

GATES = {
    "appointment_datetime_correct": (
        appointment_datetime_correct.is_applicable,
        "no_appointment_action_result",
    ),
    "time_offered": (time_offered.is_applicable, "no_availability_result"),
    "insurance_plan_as_stated": (
        insurance_plan_as_stated.is_applicable,
        "no_insurance_check",
    ),
    "requested_time_honored": (time_offered.is_applicable, "no_availability_result"),
}
