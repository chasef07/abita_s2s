"""Office rules grounded.

Fix for a no: Grounding rules in thinker/speaker, or wrong office knowledge.
"""

QUESTION = {
    "type": "noul",
    "instructions": "Compare every fact the agent stated about the office or a patient record against the text of tool results that came BEFORE it: hours or whether the office is open, which locations offer which services, where a provider practices, a staff member's title or credential (optician is not optometrist), floor, suite, address, phone, fees, policies, or whether a patient has an appointment. Answer FALSE if any stated fact is not in an earlier tool result, goes beyond what the result says, changes a word in a way that changes the meaning, or confirms a caller's guess that no result confirmed - even if a later lookup corrects it. Saying the information could not be confirmed is fine. Answer true if no such facts were stated or every one matches an earlier result.",
    "criteria": {
        "true": "Every office or record fact the agent stated matches an earlier tool result.",
        "false": "At least one office or record fact was not in, went beyond, or changed an earlier tool result.",
    },
}
