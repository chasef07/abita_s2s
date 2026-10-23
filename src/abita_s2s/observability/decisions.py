"""Read OpenRouter Decisions answers for the jury and the call labeler."""

import json
from typing import TypeGuard

URL = "https://openrouter.ai/api/alpha/decisions"
MAX_RESPONSE_BYTES = 1024 * 1024
ROUNDING = 0.005


def read_answers(content: bytes) -> dict:
    """Return the response's answers by question name, or raise ``ValueError``."""
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError("Response too large")
    body = json.loads(content, parse_constant=_reject_constant)
    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, dict):
        raise ValueError("Missing answers")
    return answers


def parse(question: dict, answer) -> float | str:
    """Return the answer's value for ``question``, or raise ``ValueError``.

    Decisions rounds each probability to two decimals, so a distribution may
    miss a total of one by half a rounding step per label. A score is the
    probability-weighted label position, so it may miss the weighted sum of
    the rounded probabilities by half a step per position plus its own rounding.
    """
    if not isinstance(answer, dict) or answer.get("type") != question["type"]:
        raise ValueError("Invalid answer type")
    if question["type"] == "noul":
        value = answer.get("noul")
        if not _within(value, 1):
            raise ValueError("Invalid probability")
        return value
    criteria = question["criteria"]
    positions = criteria if question["type"] == "choice" else range(len(criteria))
    labels = [str(label) for label in positions]
    probabilities = answer.get("probabilities")
    if (
        not isinstance(probabilities, dict)
        or not probabilities
        or not probabilities.keys() <= set(labels)
        or not all(_within(value, 1) for value in probabilities.values())
        or abs(sum(probabilities.values()) - 1) > len(labels) * ROUNDING + 1e-9
    ):
        raise ValueError("Invalid probabilities")
    if question["type"] == "choice":
        choice = answer.get("choice")
        if probabilities.keys() != set(labels) or choice not in labels:
            raise ValueError("Invalid choice")
        return choice
    score = answer.get("score")
    weighted = sum(int(label) * value for label, value in probabilities.items())
    if (
        not _within(score, len(labels) - 1)
        or abs(score - weighted) > ROUNDING * (sum(range(len(labels))) + 1) + 1e-9
    ):
        raise ValueError("Invalid score")
    return score


def _within(value, maximum: int) -> TypeGuard[float]:
    return type(value) in (int, float) and 0 <= value <= maximum


def _reject_constant(constant: str):
    raise ValueError(f"Non-finite {constant}")
