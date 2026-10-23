"""Speak up when LiveKit marks the caller away or a backend response fails; the model never speaks unprompted."""

from collections.abc import Callable

from livekit.agents import AgentSession, UserStateChangedEvent
from livekit.plugins.openai.realtime import GPTLiveSession

from abita_s2s.call_control import CallControl
from abita_s2s.prompt import load_prompt

CHECK_IN = load_prompt("check_in")
STILL_WORKING = load_prompt("still_working")
COULD_NOT_FINISH = load_prompt("could_not_finish")
STILL_WORKING_LINES = 3
FAILED = frozenset({"response.failed", "response.incomplete"})
FINISHED = FAILED | {"response.completed", "response.cancelled"}


class BackendResponses:
    """Whether the backend is busy, for every reader in the call, and whether the caller spoke since the latest request.

    ``running`` holds the delegated responses the voice model still waits on, and decides what
    the caller hears. ``busy`` also counts tool calls that wait for their outputs and a pending
    continuation. The service rejects a new request that would split that batch, so the
    watchdog checks ``busy`` before it sends one.
    """

    def __init__(self) -> None:
        self.running: set[str | None] = set()
        self.caller_spoke_since_request = False
        self.on_failure = None
        self._live: GPTLiveSession | None = None

    def follow(self, live: GPTLiveSession) -> None:
        self._live = live
        live.on("openai_server_event_received", self.record)
        live.on("input_speech_started", self.caller_turn)

    @property
    def busy(self) -> bool:
        live = self._live
        return bool(
            self.running
            or (
                live is not None
                and (live._backend_open_calls or live._backend_response_pending)
            )
        )

    def record(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "session.started":
            self.running.clear()
        if kind == "session.delegation.created" and not self.running:
            self.request_started()
        if kind != "response.event":
            return
        inner = event.get("event", {}).get("type")
        delegation_id = event.get("delegation_id")
        if inner == "response.created":
            self.running.add(delegation_id)
        elif inner in FINISHED:
            self.running.discard(delegation_id)
            if inner in FAILED and self.on_failure:
                self.on_failure(delegation_id)

    def request_started(self) -> None:
        self.caller_spoke_since_request = False

    def caller_turn(self) -> None:
        self.caller_spoke_since_request = True


def check_in_on_silence(
    session: AgentSession,
    backend: BackendResponses,
    control: CallControl,
    hand_off_held: Callable[[], bool],
) -> None:
    """Say STILL_WORKING at most STILL_WORKING_LINES times per caller turn, then check in as an idle call does."""
    still_working = 0

    def on_user_state(event: UserStateChangedEvent) -> None:
        nonlocal still_working
        if event.new_state == "speaking":
            still_working = 0
        if event.new_state != "away":
            return
        if backend.running and still_working < STILL_WORKING_LINES:
            still_working += 1
            session.generate_reply(instructions=STILL_WORKING)
            session.reset_away_timer()
            return
        if hand_off_held():
            session.reset_away_timer()
            return
        session.generate_reply(instructions=CHECK_IN)

    def on_failure(delegation_id: str | None) -> None:
        if delegation_id is None or backend.running or control.leaving:
            return
        session.generate_reply(instructions=COULD_NOT_FINISH)

    backend.on_failure = on_failure
    session.on("user_state_changed", on_user_state)
