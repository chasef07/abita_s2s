"""Own GPT-Live voice and delegated reasoning configuration."""

from datetime import datetime
from zoneinfo import ZoneInfo

from livekit.plugins.openai.realtime import GPTLiveModel

from abita_s2s.config import Config
from abita_s2s.prompt import load_prompt


SPEAKER_MODEL = "gpt-live-1"
THINKER_MODEL = "gpt-5.6-luna"


def create_model(config: Config) -> GPTLiveModel:
    now = datetime.now(ZoneInfo("America/New_York"))
    return GPTLiveModel(
        api_key=config.openai_api_key,
        model=SPEAKER_MODEL,
        voice=config.voice,
        responses_options={
            "model": THINKER_MODEL,
            "instructions": (
                load_prompt("thinker")
                + f"\n\nCall started: {now:%A, %Y-%m-%d %H:%M %Z} "
                "(America/New_York). Use this clinic-local date "
                "to interpret relative dates."
            ),
        },
    )
