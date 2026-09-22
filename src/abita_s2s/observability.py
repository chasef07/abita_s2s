"""Provider session correlation and post-call quality judgments."""

import asyncio
import logging

from livekit.agents import JobContext
from livekit.plugins.openai.realtime import GPTLiveSession
from livekit.agents.evals import (
    JudgeGroup,
    accuracy_judge,
    conciseness_judge,
    task_completion_judge,
    tool_use_judge,
)

logger = logging.getLogger(__name__)
EVALUATION_SECONDS = 90


def log_openai_session(session: GPTLiveSession, call_id: str) -> None:
    """Record the current connection and each subsequent OpenAI reconnect."""

    def log_id(session_id: str) -> None:
        logger.info(
            "openai_session_started call_id=%s openai_session_id=%s",
            call_id,
            session_id,
            extra={"call_id": call_id, "openai_session_id": session_id},
        )

    def on_server_event(event: dict) -> None:
        if event.get("type") == "session.started":
            log_id(event["session"]["id"])

    session.on("openai_server_event_received", on_server_event)
    # AgentSession.start() can return before or after OpenAI's session.started.
    if session.session_id:
        log_id(session.session_id)


async def evaluate_call(ctx: JobContext) -> None:
    """Never let judging prevent closeout; missing verdicts are incomplete evaluations."""
    try:
        history = ctx.make_session_report().chat_history
        if not any(
            item.type == "message" and item.role == "user" for item in history.items
        ):
            ctx.tagger.add("abita.evaluation:skipped")
            return
        async with asyncio.timeout(EVALUATION_SECONDS):
            judges = JudgeGroup(
                llm="openai/gpt-4o-mini",
                judges=[
                    task_completion_judge(),
                    accuracy_judge(),
                    tool_use_judge(),
                    conciseness_judge(),
                ],
            )
            async with judges.llm:
                result = await judges.evaluate(history)
            missing = {judge.name for judge in judges.judges} - result.judgments.keys()
            if missing:
                ctx.tagger.add("abita.evaluation:incomplete")
                logger.error(
                    "Call evaluation missing judges=%s", ",".join(sorted(missing))
                )
            else:
                ctx.tagger.add("abita.evaluation:complete")
    except Exception as error:
        ctx.tagger.add("abita.evaluation:incomplete")
        logger.error("Call evaluation failed cause=%s", type(error).__name__)
