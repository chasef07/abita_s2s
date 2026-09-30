"""Check in on a silent caller when LiveKit marks them away, then end the call."""

import logging

from livekit.agents import AgentSession, UserStateChangedEvent
from livekit.agents.voice import SpeechHandle

from abita_s2s.call_control import CallControl
from abita_s2s.runtime.observability import ObservedGPTLiveModel

logger = logging.getLogger(__name__)
AWAY_SECONDS = 15.0
CHECK_INS = (
    "The caller has been silent. If they asked for time, briefly say you are "
    "still here. Otherwise ask whether they are still there or need anything else.",
    "The caller is still silent after your check-in. In different words than "
    "before, briefly say you are still here and will end the call soon if you "
    "do not hear from them.",
)
GOODBYE = (
    "The caller has stayed silent after repeated check-ins. Say one brief "
    "goodbye and ask nothing; the call ends when you finish speaking."
)


class SilenceCheckIn:
    """Speak into mutual silence; the realtime model never starts a turn on its own."""

    def __init__(
        self,
        session: AgentSession,
        model: ObservedGPTLiveModel,
        control: CallControl,
    ):
        self.session = session
        self.model = model
        self.control = control
        self.check_ins = 0
        session.on("user_state_changed", self.on_user_state)

    def on_user_state(self, event: UserStateChangedEvent) -> None:
        if event.new_state == "speaking":
            self.check_ins = 0
        if (
            event.new_state != "away"
            or self.control.closing
            or self.check_ins > len(CHECK_INS)
        ):
            return
        self.session.reset_away_timer()
        if self.model.backend_busy:
            logger.info("silence_check_in skipped=backend_busy")
            return
        self.check_ins += 1
        final = self.check_ins > len(CHECK_INS)
        logger.info("silence_check_in count=%s final=%s", self.check_ins, final)
        if not final:
            self.session.generate_reply(instructions=CHECK_INS[self.check_ins - 1])
            return
        handle = self.session.generate_reply(instructions=GOODBYE)
        handle.add_done_callback(self.hang_up)

    def hang_up(self, handle: SpeechHandle) -> None:
        if handle.interrupted or self.check_ins <= len(CHECK_INS):
            return
        if blocked := self.control.begin_end(lambda: None):
            logger.info("silence_hang_up blocked=%s", blocked.split(":", 1)[0])
            return
        logger.info("silence_hang_up")
        self.session.shutdown()
