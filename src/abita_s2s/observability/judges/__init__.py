"""Named judge definitions and the seated jury used by the call evaluator."""

from abita_s2s.observability.judges import (
    appointment_datetime_correct,
    call_ended_correctly,
    emergency_policy_followed,
    expressed_sentiment,
    knowledge_available,
    office_question_answered,
    transfer_policy_followed,
)

JURORS = (
    "typesafe/jev-1.14",
    "perplexity/pplx-decider-v1.2",
    "baseten/mammoth-1:free",
)

QUESTIONS = {
    "knowledge_available": knowledge_available.QUESTION,
    "office_question_answered": office_question_answered.QUESTION,
    "appointment_datetime_correct": appointment_datetime_correct.QUESTION,
    "call_ended_correctly": call_ended_correctly.QUESTION,
    "transfer_policy_followed": transfer_policy_followed.QUESTION,
    "emergency_policy_followed": emergency_policy_followed.QUESTION,
    "expressed_sentiment": expressed_sentiment.QUESTION,
}

GATES = {
    "knowledge_available": knowledge_available.skip,
    "appointment_datetime_correct": appointment_datetime_correct.skip,
}
