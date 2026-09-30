"""Check in on a silent caller when LiveKit marks them away, then end the call."""

import logging

from livekit.agents import AgentSession, UserStateChangedEvent

from abita_s2s.call_control import CallControl

logger = logging.getLogger(__name__)
AWAY_SECONDS = 15.0
CHECK_INS = 2
CHECK_IN = (
    "The caller has been silent. If they asked for time, briefly say you are "
    "still here. Otherwise ask whether they are still there or need anything else."
)
GOODBYE = (
    "The caller has stayed silent after repeated check-ins. Say a brief goodbye "
    "and delegate call completion."
)


class SilenceCheckIn:
    """Speak into mutual silence; the realtime model never starts a turn on its own."""

    def __init__(self, session: AgentSession, control: CallControl):
        self.session = session
        self.control = control
        self.check_ins = 0
        session.on("user_state_changed", self.on_user_state)

    def on_user_state(self, event: UserStateChangedEvent) -> None:
        if event.new_state == "speaking":
            self.check_ins = 0
        if event.new_state != "away" or self.control.closing:
            return
        self.session.reset_away_timer()
        if self.session.llm.backend_busy:
            logger.info("silence_check_in skipped=backend_busy")
            return
        self.check_ins += 1
        final = self.check_ins > CHECK_INS
        logger.info("silence_check_in count=%s final=%s", self.check_ins, final)
        self.session.generate_reply(instructions=GOODBYE if final else CHECK_IN)
