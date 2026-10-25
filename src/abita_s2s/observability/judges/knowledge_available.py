"""Missing office knowledge is a gap to fill, not proof of an agent mistake."""

QUESTION = {
    "type": "noul",
    "instructions": "Did the office knowledge in the agent instructions contain the information needed to answer the caller's office questions? Compare the caller's questions with that office knowledge, not the agent's answer. Answer FALSE if the office knowledge did not supply, or only said it did not supply, the information a caller's office question needed. Answer TRUE if no office question was asked or office information could not be loaded; a load failure does not prove the knowledge was missing.",
    "criteria": {
        "true": "The office knowledge supplied the information needed to answer the caller's office questions, no office question was asked, or office information could not be loaded.",
        "false": "The office knowledge did not supply the information needed to answer a caller's office question.",
    },
}
