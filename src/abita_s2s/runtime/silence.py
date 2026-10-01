"""Check in once when LiveKit marks the caller away; the model never speaks unprompted."""

from livekit.agents import AgentSession, UserStateChangedEvent

CHECK_IN = (
    "The caller has been silent and no backend work is running. If the caller "
    "gave information or a request you have not delegated yet, delegate it now. "
    "Otherwise, if they asked for time, briefly say you are still here; if not, "
    "ask whether they are still there or need anything else."
)
FINISHED = frozenset(
    {
        "response.completed",
        "response.failed",
        "response.incomplete",
        "response.cancelled",
    }
)


class BackendResponses:
    """Delegated backend responses the voice model is still waiting on."""

    def __init__(self) -> None:
        self.running: set[str | None] = set()

    def record(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "session.started":
            self.running.clear()
        if kind != "response.event":
            return
        inner = event.get("event", {}).get("type")
        if inner == "response.created":
            self.running.add(event.get("delegation_id"))
        elif inner in FINISHED:
            self.running.discard(event.get("delegation_id"))


def check_in_on_silence(session: AgentSession, backend: BackendResponses) -> None:
    def on_user_state(event: UserStateChangedEvent) -> None:
        if event.new_state != "away":
            return
        if backend.running:
            session.reset_away_timer()
            return
        session.generate_reply(instructions=CHECK_IN)

    session.on("user_state_changed", on_user_state)
