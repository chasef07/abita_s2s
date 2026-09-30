"""Check in once when LiveKit marks the caller away; the model never speaks unprompted."""

from livekit.agents import AgentSession, UserStateChangedEvent

from abita_s2s.runtime.observability import ObservedGPTLiveModel

CHECK_IN = (
    "The caller has been silent. If they asked for time, briefly say you are "
    "still here. Otherwise ask whether they are still there or need anything else."
)


def check_in_on_silence(session: AgentSession, model: ObservedGPTLiveModel) -> None:
    def on_user_state(event: UserStateChangedEvent) -> None:
        if event.new_state != "away":
            return
        if model.backend_busy:
            session.reset_away_timer()
            return
        session.generate_reply(instructions=CHECK_IN)

    session.on("user_state_changed", on_user_state)
