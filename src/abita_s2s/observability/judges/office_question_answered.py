"""Office question answered.

Fix for a no: Answer the office question from relevant knowledge before ending or handing off.
"""

QUESTION = {
    "type": "noul",
    "instructions": "Did the agent correctly answer every office question using the office knowledge in the agent instructions? Office questions include hours, locations, providers, services, fees, office policies, and records or prescription procedures. Answer FALSE for an unanswered office question, an invented or incorrect answer, or an answer that goes beyond the office knowledge. A later correction does not support an earlier unsupported answer. Questions about patient-specific records, insurance acceptance, appointment availability, or medical advice do not count. Answer TRUE if no office question was asked.",
    "criteria": {
        "true": "Every office question was correctly answered from the office knowledge, or no office question was asked.",
        "false": "An office question went unanswered or received an incorrect or unsupported answer.",
    },
}
