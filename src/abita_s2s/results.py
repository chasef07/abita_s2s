"""Owner result shape: an outcome code and the model-facing answer, plus facts."""


def reply(outcome: str, answer: str, **facts) -> dict:
    return {"outcome": outcome, "answer": answer, **facts}
