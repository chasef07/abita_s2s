"""Office question answered.

Fix for a no: Office knowledge in the Portal: staff fill the gap (ACU-125).
"""

QUESTION = {
    "type": "noul",
    "instructions": "Did the caller ask a question about the office - hours, locations, phone or fax numbers, providers, services, fees, policies, how to get records or prescriptions, referrals, parking, directions? If so, did the agent give a specific answer taken from an office knowledge or tool result? Answer FALSE if the caller asked an office question and the agent said it could not confirm or find the answer, the knowledge result said the information was not supplied, or the agent had to offer a staff message or transfer only because the answer was missing. Answer true if the caller asked no office questions or every office question was answered from a tool result. Questions about the caller's own appointments, insurance coverage, or medical advice do not count.",
    "criteria": {
        "true": "Every office question the caller asked was answered from office knowledge, or none was asked.",
        "false": "The caller asked an office question that office knowledge could not answer.",
    },
}
