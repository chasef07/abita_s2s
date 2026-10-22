"""Hand the backend any caller turn the voice model did not delegate.

GPT-Live sometimes acknowledges a request ("Sure", "Listo, confirmada") and never
delegates it, or never answers at all. Prompts reduce this but cannot prevent it.
Once the voice model has answered and the call goes quiet, the caller's undelegated
words go to the backend, which acts on them or not. A turn the voice model has not
answered is held, since the caller may still be talking, until the silence check-in
finds the call quiet and hands it off.
"""

import asyncio
import logging
import re

from livekit.agents import utils

logger = logging.getLogger(__name__)
HANDOFF_SECONDS = 5.0
BACKCHANNELS = frozenset({"mm-hmm", "mhm", "uh-huh", "hmm", "um", "uh"})


class DelegationWatchdog:
    """Collects caller words the voice model has not delegated and hands them off after a quiet spell."""

    def __init__(self, live, backend) -> None:
        self._live = live
        self._backend = backend
        self._caller: list[str] = []
        self._voice: list[str] = []
        self._timer: asyncio.TimerHandle | None = None

    def record(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "session.input_transcript.delta" and not self._live.backend_busy:
            self._caller.append(event.get("delta") or "")
            self._arm()
        elif kind == "session.output_transcript.delta" and self._caller:
            self._voice.append(event.get("delta") or "")
            self._arm()
        elif kind in (
            "session.delegation.created",
            "session.started",
            "session.closed",
        ):
            self._reset()

    def _arm(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = asyncio.get_running_loop().call_later(
            HANDOFF_SECONDS, self._hand_off
        )

    def _reset(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = None
        self._caller.clear()
        self._voice.clear()

    def _hand_off(self) -> None:
        if "".join(self._voice).strip():
            self._send()

    def hand_off_unanswered(self) -> bool:
        """On silence, hand the backend caller words the voice model neither answered nor delegated."""
        if "".join(self._voice).strip():
            return False
        return self._send()

    def _send(self) -> bool:
        caller = "".join(self._caller).strip()
        voice = "".join(self._voice).strip()
        self._reset()
        words = re.sub(r"[^\w\s-]", "", caller.lower()).strip()
        if len(words) < 2 or words in BACKCHANNELS or self._live.backend_busy:
            return False
        logger.warning(
            "delegation_watchdog_handoff caller_chars=%d answered=%s",
            len(caller),
            bool(voice),
        )
        self._backend.request_started()
        answered = (
            f"The voice assistant answered without handing it to you: {voice}\n"
            if voice
            else "The voice assistant has not answered or handed it to you.\n"
        )
        message = (
            f"The caller said: {caller}\n"
            f"{answered}"
            "If the caller asked for or approved something, do it now and correct any claim "
            "that it is already done. If no backend work is needed, return an empty response."
        )
        self._live.send_event(
            {
                "type": "response.item.create",
                "event_id": utils.shortuuid("watchdog_"),
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": message}],
                },
            }
        )
        self._live.send_event(
            {"type": "response.create", "event_id": utils.shortuuid("watchdog_create_")}
        )
        return True
