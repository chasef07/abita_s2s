"""Correlate provider sessions with calls."""

import logging

from livekit.plugins.openai.realtime import GPTLiveSession

logger = logging.getLogger(__name__)


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
