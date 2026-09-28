"""Expressed sentiment."""

QUESTION = {
    "type": "score",
    "instructions": "What overall sentiment does the user express across the entire call? Consider all user turns and changes over the conversation. Judge expressed emotion only, independently of task completion, satisfaction with the outcome, or whether a handoff was requested. A calmly stated unresolved issue or request for a person is not negative sentiment by itself. Do not let a polite closing erase earlier frustration or infer vocal tone from text. If no clear sentiment is expressed, use neutral or mixed.",
    "criteria": [
        "very negative: strong anger, hostility, or distress is expressed",
        "negative: frustration, annoyance, or disappointment is expressed",
        "neutral or mixed: no clear emotional signal, matter-of-fact language, or mixed positive and negative emotion",
        "positive: warmth, appreciation, or relief is expressed",
        "very positive: strong enthusiasm, delight, or gratitude is expressed",
    ],
}
