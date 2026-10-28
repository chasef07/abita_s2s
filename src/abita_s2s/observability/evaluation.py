"""Scorecard questions answered by a jury of decision models via OpenRouter."""

import asyncio
import logging
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from livekit.agents import ChatContext

from abita_s2s.observability.call_labels import (
    classification,
    classify_labels,
    transfer_called,
)
from abita_s2s.observability.decisions import URL, parse, read_answers
from abita_s2s.observability.judge_state import judge_state
from abita_s2s.observability.judges import DEFINITIONS_HASH, GATES, JURORS, QUESTIONS

logger = logging.getLogger(__name__)
EVALUATION_SECONDS = 20
EVALUATION_GRACE_SECONDS = 1
RETRY_SECONDS = 0.25
MIN_CALL_SECONDS = 30
PUBLISHED_VERSIONS = {"f60da2c6d2c8": "typesafe-scorecard-v9"}
EVALUATOR_VERSION = PUBLISHED_VERSIONS.get(
    DEFINITIONS_HASH, f"typesafe-scorecard-{DEFINITIONS_HASH}"
)
YES_ABOVE = 0.40


def majority(votes: dict) -> dict:
    """Yes when more jurors vote yes than no; a tie goes to the mean probability."""
    yes = sum(p > YES_ABOVE for p in votes.values())
    no = len(votes) - yes
    probability = sum(votes.values()) / len(votes)
    return {
        "verdict": yes > no or (yes == no and probability > YES_ABOVE),
        "probability": probability,
        "votes": votes,
    }


async def evaluate_judges(
    history: ChatContext,
    *,
    api_key: str,
    office_knowledge: str,
    seconds: float = EVALUATION_SECONDS,
    closeout: dict,
    transferred: bool,
    include_jury: bool = True,
) -> dict:
    """Ask each juror every question in one request within one deadline.

    A malformed answer fails only its own question. Each question takes the
    jury's majority for yes/no answers and mean for sentiment scores.
    """
    state = judge_state(history, office_knowledge)
    results, errors = {}, {}
    if include_jury:
        for name, gate in GATES.items():
            if skipped := gate(history, closeout):
                results[name] = skipped
    asked = [name for name in QUESTIONS if name not in results] if include_jury else []
    jurors = JURORS if include_jury else ()
    deadline = asyncio.get_running_loop().time() + seconds
    async with httpx.AsyncClient(timeout=seconds) as client:

        async def ask(
            model: str, state: dict, questions: dict
        ) -> tuple[dict, str | None]:
            attempts = 0
            http_status = None
            try:
                async with asyncio.timeout_at(deadline):
                    while True:
                        attempts += 1
                        http_status = None
                        delay = RETRY_SECONDS
                        try:
                            response = await client.post(
                                URL,
                                headers={"Authorization": f"Bearer {api_key}"},
                                json={
                                    "model": model,
                                    "state": state,
                                    "questions": questions,
                                    "provider": {"zdr": True},
                                },
                            )
                            http_status = response.status_code
                            response.raise_for_status()
                        except httpx.HTTPStatusError:
                            if attempts == 2 or http_status not in (
                                408,
                                429,
                                500,
                                502,
                                503,
                                504,
                                524,
                                529,
                            ):
                                raise
                            retry_after = response.headers.get("Retry-After")
                            if retry_after:
                                try:
                                    delay = float(retry_after)
                                except ValueError:
                                    try:
                                        delay = (
                                            parsedate_to_datetime(retry_after)
                                            - datetime.now(UTC)
                                        ).total_seconds()
                                    except (ValueError, TypeError, OverflowError):
                                        delay = RETRY_SECONDS
                                if not math.isfinite(delay):
                                    delay = RETRY_SECONDS
                                delay = max(RETRY_SECONDS, delay)
                        except httpx.TransportError:
                            if attempts == 2:
                                raise
                        else:
                            return read_answers(response.content)
                        await asyncio.sleep(delay)
            except Exception as error:
                logger.error(
                    "Call evaluation failed model=%s cause=%s http_status=%s attempts=%s",
                    model,
                    type(error).__name__,
                    http_status,
                    attempts,
                )
                raise

        juror_questions = {name: QUESTIONS[name] for name in asked}
        replies = await asyncio.gather(
            *(ask(juror, state, juror_questions) for juror in jurors),
            classify_labels(ask, state, closeout, transferred=transferred),
            return_exceptions=True,
        )
    labels = replies.pop()
    if isinstance(labels, Exception):
        labels = classification("incomplete", "classification_failed")
    replies = dict(zip(jurors, replies, strict=True))
    served = {
        juror: reply[1] for juror, reply in replies.items() if isinstance(reply, tuple)
    }
    for name in asked:
        votes, failures = {}, {}
        for juror, reply in replies.items():
            if not isinstance(reply, tuple):
                failures[juror] = type(reply).__name__
                continue
            try:
                votes[juror] = parse(QUESTIONS[name], reply[0].get(name))
            except ValueError:
                failures[juror] = "ValueError"
                logger.error(
                    "Call evaluation answer invalid judge=%s juror=%s", name, juror
                )
        if len(votes) < len(JURORS) // 2 + 1:
            errors[name] = {"cause": "no_quorum", "votes": votes, "errors": failures}
        elif QUESTIONS[name]["type"] == "noul":
            results[name] = {**majority(votes), "errors": failures}
        else:
            results[name] = {
                "score": sum(votes.values()) / len(votes),
                "votes": votes,
                "errors": failures,
            }
    return {
        "results": results,
        "errors": errors,
        "servedModels": served,
        "classification": labels,
    }


async def evaluate_call(
    report: dict,
    api_key: str | None,
    seconds: float,
    *,
    office_knowledge: str,
    budget: float = math.inf,
    closeout: dict | None = None,
) -> dict:
    """Return a persistable result without letting a judge failure break closeout.

    The whole evaluation fits in ``budget`` seconds so shutdown can still deliver it.
    """
    window = min(EVALUATION_SECONDS, budget - EVALUATION_GRACE_SECONDS)
    evaluation = {
        "evaluator": "jury",
        "evaluatorVersion": EVALUATOR_VERSION,
        "jurors": list(JURORS),
        "evaluatedAt": datetime.now(UTC).isoformat(),
        "status": "incomplete",
        "classification": classification("incomplete", "evaluation_failed"),
    }
    transferred = False
    try:
        if not report:
            evaluation.update(status="skipped", reason="native_report_unavailable")
            evaluation["classification"] = classification(
                "skipped", evaluation["reason"]
            )
            return evaluation
        history = ChatContext.from_dict(report["chat_history"])
        transferred = transfer_called(history)
        if not any(
            item.type == "message"
            and item.role == "user"
            and (item.text_content or "").strip()
            for item in history.items
        ):
            evaluation.update(status="skipped", reason="no_user_messages")
        elif not api_key:
            evaluation.update(status="skipped", reason="openrouter_key_not_configured")
        elif window <= 0:
            evaluation.update(status="skipped", reason="shutdown_budget_exhausted")
        else:
            async with asyncio.timeout(window + EVALUATION_GRACE_SECONDS):
                evaluation.update(
                    await evaluate_judges(
                        history,
                        api_key=api_key,
                        office_knowledge=office_knowledge,
                        seconds=window,
                        closeout=closeout or {},
                        transferred=transferred,
                        include_jury=seconds >= MIN_CALL_SECONDS,
                    )
                )
            if seconds < MIN_CALL_SECONDS:
                evaluation.update(status="skipped", reason="call_too_short")
            elif evaluation["errors"]:
                evaluation["reason"] = "judge_errors"
            else:
                evaluation["status"] = "complete"
        if (
            evaluation["status"] == "skipped"
            and evaluation["reason"] != "call_too_short"
        ):
            evaluation["classification"] = classification(
                "skipped", evaluation["reason"]
            )
    except Exception as error:
        evaluation["reason"] = type(error).__name__
        logger.error("Call evaluation failed cause=%s", type(error).__name__)
    if transferred:
        evaluation["classification"]["outcome"] = "transferred"
    return evaluation
