"""Speak up when LiveKit marks the caller away or a backend response fails; the model never speaks unprompted."""

from livekit.agents import AgentSession, UserStateChangedEvent

from abita_s2s.call_control import CallControl

CHECK_IN = (
    "The caller has been silent and no backend work is running. If the caller "
    "gave information or a request you have not handed off yet, generate "
    "<|SpawnThinking|> now. Otherwise, if they asked for time, briefly say you "
    "are still here; if not, ask whether they are still there or need anything else."
)
STILL_WORKING = (
    "Tell the caller in one short sentence that you are still working on it. "
    "Do not guess the result."
)
COULD_NOT_FINISH = (
    "Tell the caller in one short sentence that you could not finish that just "
    "now, and ask whether they would like you to try again."
)
FAILED = frozenset({"response.failed", "response.incomplete"})
FINISHED = FAILED | {"response.completed", "response.cancelled"}


class BackendResponses:
    """Delegated backend responses the voice model is still waiting on, and whether the caller spoke since the latest request."""

    def __init__(self) -> None:
        self.running: set[str | None] = set()
        self.caller_spoke_since_request = False
        self.on_failure = None

    def record(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "session.started":
            self.running.clear()
        if kind == "session.delegation.created":
            self.caller_spoke_since_request = False
        if kind != "response.event":
            return
        inner = event.get("event", {}).get("type")
        delegation_id = event.get("delegation_id")
        if inner == "response.created":
            self.running.add(delegation_id)
            if delegation_id is None:
                self.caller_spoke_since_request = False
        elif inner in FINISHED:
            self.running.discard(delegation_id)
            if inner in FAILED and self.on_failure:
                self.on_failure(delegation_id)

    def caller_turn(self) -> None:
        self.caller_spoke_since_request = True


def check_in_on_silence(
    session: AgentSession, backend: BackendResponses, control: CallControl
) -> None:
    def on_user_state(event: UserStateChangedEvent) -> None:
        if event.new_state != "away":
            return
        if backend.running:
            session.generate_reply(instructions=STILL_WORKING)
            session.reset_away_timer()
            return
        session.generate_reply(instructions=CHECK_IN)

    def on_failure(delegation_id: str | None) -> None:
        if delegation_id is None or backend.running or control.leaving:
            return
        session.generate_reply(instructions=COULD_NOT_FINISH)

    backend.on_failure = on_failure
    session.on("user_state_changed", on_user_state)
