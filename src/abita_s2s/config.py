"""Read runtime credentials without exposing them in logs or repr."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    openai_api_key: str = field(repr=False)
    voice: str = "gleam"


def load_config() -> Config:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Missing required environment variable: OPENAI_API_KEY")
    # LiveKit CLI validates its URL, API key, and secret in room modes.
    voice = os.environ.get("GPT_LIVE_VOICE", "gleam").strip()
    if not voice:
        raise ValueError("GPT_LIVE_VOICE must not be empty")
    return Config(openai_api_key=api_key, voice=voice)
