"""Own the GPT-Live model: voice, delegated reasoning, and the call behavior each session feeds."""

from datetime import datetime

from livekit.plugins.openai.realtime import GPTLiveModel, GPTLiveSession

from abita_s2s.config import Config
from abita_s2s.offices import EASTERN
from abita_s2s.prompt import thinker_instructions
from abita_s2s.runtime.observability import ObservedGPTLiveSession
from abita_s2s.runtime.silence import BackendResponses

SPEAKER_MODEL = "gpt-live-1.1"
SPEAKER_VOICE = "bossa"
THINKER_MODEL = "gpt-6-luna"
THINKER_REASONING_EFFORT = "high"


class CallModel(GPTLiveModel):
    """GPT-Live that traces each session and feeds it to the call's backend tracker."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.backend = BackendResponses()

    def session(self) -> GPTLiveSession:
        live = ObservedGPTLiveSession(self)
        self.backend.follow(live)
        return live


def create_model(
    config: Config, office_knowledge: str, insurance_list: str
) -> CallModel:
    now = datetime.now(EASTERN)
    return CallModel(
        api_key=config.openai_api_key,
        model=SPEAKER_MODEL,
        voice=SPEAKER_VOICE,
        delegation_recovery=True,
        responses_options={
            "model": THINKER_MODEL,
            "reasoning": {"effort": THINKER_REASONING_EFFORT},
            "parallel_tool_calls": False,
            "instructions": thinker_instructions(now, office_knowledge, insurance_list),
        },
    )
