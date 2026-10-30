"""Office question answered.

Fix for a no: Answer the office question from relevant knowledge before ending or handing off.
"""

from abita_s2s.observability.judges.knowledge_available import OFFICE_QUESTION

QUESTION = {
    "type": "noul",
    "instructions": f"Did the agent correctly answer every office question using `office_knowledge`? {OFFICE_QUESTION} Answer FALSE for an office question left unanswered although `office_knowledge` has the answer, an invented or incorrect answer, or an answer that goes beyond the office knowledge. A later correction does not support an earlier unsupported answer. If `office_knowledge` lacks the answer, honestly saying so and offering staff follow-up is correct. If the call ended while the agent was answering, grade only what came before. Answer TRUE if no office question was asked.",
    "criteria": {
        "true": "Every office question was correctly answered from the office knowledge, or no office question was asked.",
        "false": "An office question that the office knowledge answers went unanswered, or an answer was incorrect or unsupported.",
    },
}
