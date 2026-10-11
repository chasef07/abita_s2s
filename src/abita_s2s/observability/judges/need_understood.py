"""Need understood."""

QUESTION = {
    "type": "noul",
    "instructions": "Did the agent correctly identify every request the caller made, including corrections and additional requests? First list each caller request, then check whether the agent's questions, tool calls, and statements address the request the caller actually made. Judge only whether the agent understood what was asked, not whether its answer was correct; factual accuracy is scored separately. Mishearing that the agent later corrected still counts as understood if the correction happened before any action was taken on the wrong understanding.",
    "criteria": {
        "true": "The agent correctly identified every caller request, even if an answer it gave was wrong.",
        "false": "The agent misunderstood, ignored, or acted on the wrong version of at least one caller request.",
    },
}
