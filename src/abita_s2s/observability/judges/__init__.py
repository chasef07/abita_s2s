"""Named judge definitions used by the call evaluator."""

from abita_s2s.observability.judges import (
    request_understood,
    appointment_datetime_correct,
    office_rules_grounded,
    results_reported_truthfully,
    conversation_responsive,
    expressed_sentiment,
)

QUESTIONS = {
    "request_understood": request_understood.QUESTION,
    "appointment_datetime_correct": appointment_datetime_correct.QUESTION,
    "office_rules_grounded": office_rules_grounded.QUESTION,
    "results_reported_truthfully": results_reported_truthfully.QUESTION,
    "conversation_responsive": conversation_responsive.QUESTION,
    "expressed_sentiment": expressed_sentiment.QUESTION,
}
