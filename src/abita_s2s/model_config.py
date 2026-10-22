"""Own GPT-Live voice and delegated reasoning configuration."""

from datetime import datetime

from abita_s2s.runtime.observability import ObservedGPTLiveModel as GPTLiveModel

from abita_s2s.config import Config
from abita_s2s.offices import EASTERN
from abita_s2s.prompt import thinker_instructions


SPEAKER_MODEL = "gpt-live-1"
THINKER_MODEL = "gpt-6-luna"
THINKER_REASONING_EFFORT = "high"


def create_model(config: Config) -> GPTLiveModel:
    now = datetime.now(EASTERN)
    return GPTLiveModel(
        api_key=config.openai_api_key,
        model=SPEAKER_MODEL,
        voice=config.voice,
        responses_options={
            "model": THINKER_MODEL,
            "reasoning": {"effort": THINKER_REASONING_EFFORT},
            "parallel_tool_calls": False,
            "instructions": thinker_instructions(now),
        },
    )
