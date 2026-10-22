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
from abita_s2s.observability.judges import DEFINITIONS_HASH, GATES, JURORS, QUESTIONS
from abita_s2s.offices import EASTERN
from abita_s2s.prompt import load_prompt, thinker_instructions

logger = logging.getLogger(__name__)
EVALUATION_SECONDS = 20
EVALUATION_GRACE_SECONDS = 1
RETRY_SECONDS = 0.25
MIN_CALL_SECONDS = 30
PUBLISHED_VERSIONS = {"08dd0cd605e2": "typesafe-scorecard-v8"}
EVALUATOR_VERSION = PUBLISHED_VERSIONS.get(
    DEFINITIONS_HASH, f"typesafe-scorecard-{DEFINITIONS_HASH}"
)
YES_ABOVE = 0.40
SPEAKERS = {"user": "caller", "assistant": "agent"}


def judge_state(history: ChatContext) -> dict:
    """Give judges the agent's rules once and the call as one line per turn or tool result.

    A tool line sits where its result arrived, so judges see what the agent knew when.
    """
    started = datetime.fromtimestamp(history.items[0].created_at, EASTERN)
    calls = {
        item.call_id: item for item in history.items if item.type == "function_call"
    }
    answered = {
        item.call_id for item in history.items if item.type == "function_call_output"
    }
    lines = []
    for item in history.items:
        if item.type == "message" and item.role in SPEAKERS:
            if text := (item.text_content or "").strip():
                interrupted = " (interrupted)" if item.interrupted else ""
                lines.append(f"{SPEAKERS[item.role]}: {text}{interrupted}")
        elif item.type == "function_call" and item.call_id not in answered:
            lines.append(f"tool {item.name}({item.arguments}) -> no result")
        elif item.type == "function_call_output" and item.call_id in calls:
            call = calls[item.call_id]
            result = f"ERROR: {item.output}" if item.is_error else item.output
            lines.append(f"tool {call.name}({call.arguments}) -> {result}")
    return {
        "agent_instructions": thinker_instructions(started)
        + "\n\n"
        + load_prompt("speaker"),
        "transcript": lines,
    }


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
    seconds: float = EVALUATION_SECONDS,
    closeout: dict,
    transferred: bool,
    include_jury: bool = True,
) -> dict:
    """Ask each juror every question in one request within one deadline.

    A malformed answer fails only its own question. Each question takes the
    jury's majority for yes/no answers and mean for sentiment scores.
    """
    state = judge_state(history)
    results, errors = {}, {}
    if include_jury:
        for name, gate in GATES.items():
            if skipped := gate(history, closeout):
                results[name] = skipped
    asked = [name for name in QUESTIONS if name not in results] if include_jury else []
    jurors = JURORS if include_jury else ()
    deadline = asyncio.get_running_loop().time() + seconds
    async with httpx.AsyncClient(timeout=seconds) as client:

        async def ask(juror):
            attempts = 0
            http_status = None
            try:
                async with asyncio.timeout_at(deadline):
                    for attempt in range(2):
                        attempts += 1
                        http_status = None
                        delay = RETRY_SECONDS
                        try:
                            response = await client.post(
                                URL,
                                headers={"Authorization": f"Bearer {api_key}"},
                                json={
                                    "model": juror,
                                    "state": state,
                                    "questions": {
                                        name: QUESTIONS[name] for name in asked
                                    },
                                    "provider": {"zdr": True},
                                },
                            )
                            http_status = response.status_code
                            response.raise_for_status()
                        except httpx.HTTPStatusError:
                            if attempt or http_status not in (
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
                            if attempt:
                                raise
                        else:
                            return read_answers(response.content)
                        await asyncio.sleep(delay)
            except Exception as error:
                logger.error(
                    "Call evaluation failed juror=%s cause=%s http_status=%s attempts=%s",
                    juror,
                    type(error).__name__,
                    http_status,
                    attempts,
                )
                raise

        replies = await asyncio.gather(
            *(ask(juror) for juror in jurors),
            classify_labels(
                client,
                state,
                closeout,
                api_key=api_key,
                deadline=deadline,
                transferred=transferred,
            ),
            return_exceptions=True,
        )
    labels = replies.pop()
    if isinstance(labels, Exception):
        labels = classification("incomplete", "classification_failed")
    replies = dict(zip(jurors, replies, strict=True))
    for name in asked:
        votes, failures = {}, {}
        for juror, reply in replies.items():
            if isinstance(reply, Exception):
                failures[juror] = type(reply).__name__
                continue
            try:
                votes[juror] = parse(QUESTIONS[name], reply.get(name))
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
    return {"results": results, "errors": errors, "classification": labels}


async def evaluate_call(
    report: dict,
    api_key: str | None,
    seconds: float,
    *,
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
