"""TypeSafe's outcome, clarity, and reaction questions over a saved conversation.

Reference: https://evals.typesafe.ai/agent_trace_observability
This helper returns judgments; it does not execute the demo's triage actions.
"""

import asyncio
import logging
import os
from datetime import UTC, datetime

import httpx
from livekit.agents import ChatContext


logger = logging.getLogger(__name__)
EVALUATION_SECONDS = 20
EVALUATOR_VERSION = "typesafe-trace-v1"


QUESTIONS = {
    "outcome": {
        "request_fulfilled": {
            "type": "boolean",
            "instructions": "According to the tool results and the conversation record -- not the assistant's own words -- did the user get what they asked for?",
        },
        "handed_off": {
            "type": "boolean",
            "instructions": "Did the assistant route the request to a person or queue -- an escalation, case, ticket or transfer -- that a tool result confirms was created, and tell the user so?",
        },
        "handoff_required": {
            "type": "boolean",
            "instructions": "Do the agent's instructions -- its policy rules and entitlement lines -- require this request to be escalated, or deny the agent the operation needed to complete it?",
        },
        "left_undone": {
            "type": "boolean",
            "instructions": "Was any part of what the user asked for left undone, unrouted, and unaddressed in the final message?",
        },
        "claims_supported": {
            "type": "boolean",
            "instructions": "Is every factual claim in the assistant's final message supported by a tool result or by something the user said in the conversation?",
        },
    },
    "clarity": {
        "request_specificity": {
            "type": "score",
            "instructions": "How clearly did the user state what they wanted?",
            "criteria": [
                "contradictory or shifting: the user asks for incompatible things or changes the ask repeatedly",
                "vague: a goal is stated but details needed to act on it are missing",
                "mostly clear: the goal and most details are stated, with minor gaps",
                "fully specified: the goal and the details needed to act are all stated",
            ],
        },
        "in_scope": {
            "type": "boolean",
            "instructions": "Is the request within what this agent is for, according to its stated purpose?",
        },
    },
    "reaction": {
        "expressed_satisfaction": {
            "type": "score",
            "instructions": "How satisfied is the user with the outcome, judging only by what they expressed after the assistant's final message?",
            "criteria": [
                "angry or escalating: complains, says the problem is unresolved, demands a person, or reopens",
                "dissatisfied: says something was wrong, missing, or unhelpful",
                "neutral or mixed: no clear signal either way, or thanks alongside an unresolved point",
                "satisfied: indicates the issue was handled",
                "very satisfied: explicitly praises the help or confirms full resolution",
            ],
        },
        "reports_unresolved": {
            "type": "boolean",
            "instructions": "Does the user say their original issue is still unresolved, or that the assistant answered something other than what they asked?",
        },
    },
}


async def evaluate_with_jev(
    history: ChatContext,
    *,
    agent_purpose: str,
    feedback: dict | None = None,
) -> dict:
    """Evaluate all recorded turns; feedback must be from after the final response.

    Keep agent config updates in history, including the instructions active in the
    call. Supply the agent's actual purpose, not the evaluator's description of it.
    No raw audio or images are sent. API errors and missing answers raise rather
    than becoming passing judgments. Returns raw responses, including usage.
    """
    if not agent_purpose.strip():
        raise ValueError("agent_purpose is required")
    items = history.to_dict(exclude_timestamp=False, exclude_metrics=True)["items"]
    messages = [item for item in items if item["type"] == "message"]
    final_message = next(
        (item for item in reversed(messages) if item["role"] == "assistant"), None
    )
    states = {
        "outcome": {
            "agent_purpose": agent_purpose,
            "conversation": items,
            "final_message": final_message,
            "note": "Judge what was achieved from the tool results and the record, not the assistant's assertions. Agent config updates contain the recorded instructions.",
        },
        "clarity": {
            "agent_purpose": agent_purpose,
            "user_messages": [item for item in messages if item["role"] == "user"],
            "note": "Only the user's messages during the run are shown. Judge the request itself.",
        },
    }
    results = {}
    if feedback:
        states["reaction"] = {
            "assistant_final_message": final_message,
            "feedback": feedback,
            "note": "Judge only what the user expressed after the assistant's final message.",
        }
    else:
        results["reaction"] = {
            "status": "unavailable",
            "reason": "No post-call feedback supplied.",
        }

    async with httpx.AsyncClient(timeout=EVALUATION_SECONDS) as client:
        for group, state in states.items():
            response = await client.post(
                "https://ai-gateway.vercel.sh/v1/evaluate",
                headers={"Authorization": f"Bearer {os.environ['AI_GATEWAY_API_KEY']}"},
                json={
                    "model": "typesafe-ai/jev",
                    "state": state,
                    "questions": QUESTIONS[group],
                },
            )
            response.raise_for_status()
            result = response.json()
            answers = result.get("answers", {})
            if not isinstance(answers, dict) or set(answers) != set(QUESTIONS[group]):
                raise ValueError(f"Incomplete Jev answers for {group}")
            for name, question in QUESTIONS[group].items():
                answer = answers[name]
                if (
                    not isinstance(answer, dict)
                    or answer.get("type") != question["type"]
                ):
                    raise ValueError(f"Invalid Jev answer for {group}.{name}")
                if question["type"] == "boolean":
                    value = answer.get("probability")
                    maximum = 1
                else:
                    probabilities = answer.get("probabilities")
                    if not isinstance(probabilities, dict) or not probabilities:
                        raise ValueError(
                            f"Missing Jev probabilities for {group}.{name}"
                        )
                    if any(
                        type(value) not in (int, float) or not 0 <= value <= 1
                        for value in probabilities.values()
                    ):
                        raise ValueError(
                            f"Invalid Jev probabilities for {group}.{name}"
                        )
                    value = answer.get("score")
                    maximum = len(question["criteria"]) - 1
                if type(value) not in (int, float) or not 0 <= value <= maximum:
                    raise ValueError(f"Invalid Jev value for {group}.{name}")
            results[group] = result
    return results


async def evaluate_call(report: dict) -> dict:
    """Return a persistable result without letting a judge failure break closeout."""
    evaluation = {
        "evaluator": "jev",
        "evaluatorVersion": EVALUATOR_VERSION,
        "model": "typesafe-ai/jev",
        "evaluatedAt": datetime.now(UTC).isoformat(),
        "status": "incomplete",
    }
    try:
        history = ChatContext.from_dict(report["chat_history"])
        if not any(
            item.type == "message" and item.role == "user" for item in history.items
        ):
            evaluation.update(status="skipped", reason="no_user_messages")
        elif not os.environ.get("AI_GATEWAY_API_KEY", "").strip():
            evaluation.update(status="skipped", reason="gateway_key_not_configured")
        else:
            instructions = next(
                (
                    item.instructions
                    for item in reversed(history.items)
                    if item.type == "agent_config_update" and item.instructions
                ),
                None,
            )
            if not instructions:
                evaluation["reason"] = "agent_instructions_missing"
            else:
                async with asyncio.timeout(EVALUATION_SECONDS):
                    evaluation["results"] = await evaluate_with_jev(
                        history, agent_purpose=str(instructions)
                    )
                evaluation["status"] = "complete"
    except Exception as error:
        # Exception messages and API bodies may contain call content or credentials.
        evaluation["reason"] = type(error).__name__
        logger.error("Call evaluation failed cause=%s", type(error).__name__)
    return evaluation
