"""Named judge definitions used by the call evaluator."""

from abita_s2s.observability.judges import (
    appointment_datetime_correct,
    office_rules_grounded,
    conversation_responsive,
    expressed_sentiment,
)

QUESTIONS = {
    "appointment_datetime_correct": appointment_datetime_correct.QUESTION,
    "office_rules_grounded": office_rules_grounded.QUESTION,
    "conversation_responsive": conversation_responsive.QUESTION,
    "expressed_sentiment": expressed_sentiment.QUESTION,
}
