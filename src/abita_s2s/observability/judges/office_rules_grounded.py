"""Office rules grounded."""

QUESTION = {
    "type": "boolean",
    "instructions": "Was every factual claim about office hours, whether the office is open, providers, locations, services, or policies supported by recorded office instructions or a successful knowledge result available BEFORE the claim? Check each claim, including claims in Spanish, against earlier evidence for the relevant office. A caller's suggestion, the agent's own statements, general knowledge, or an unrelated tool result is not supporting evidence. If even one claim lacks earlier support or contradicts it, answer false. For example, saying 'we are open until five today' without earlier supporting hours fails. A later lookup, correction, or otherwise grounded answer does not erase an earlier unsupported claim. Greetings, acknowledgments, and explicit statements that information is unknown are not factual office claims.",
    "criteria": {
        "true": "Every factual office claim has supporting evidence available before it was made, or no factual office claims were made.",
        "false": "At least one factual office claim lacks earlier supporting evidence or contradicts it, even if the rest of the call is grounded or the claim is later corrected.",
    },
}
