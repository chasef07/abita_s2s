"""Load version-controlled instructions shipped with the agent package."""

from datetime import datetime
from pathlib import Path
from typing import Literal

PROMPTS_DIR = Path(__file__).parent / "prompts"
Prompt = Literal[
    "speaker",
    "thinker",
    "call_started",
    "greeting",
    "check_in",
    "still_working",
    "could_not_finish",
    "goodbye",
]


def load_prompt(name: Prompt) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"Prompt file is empty: {path}")
    return content


def thinker_instructions(started: datetime) -> str:
    """The thinker's rules plus the clinic-local call start, as the thinker sees them."""
    return (
        load_prompt("thinker")
        + "\n\n"
        + load_prompt("call_started").format(started=started)
    )


def greeting(practice_name: str, now: datetime) -> str:
    """The opening instructions, seeded into session.input and then asked for once, as GPT-Live advises."""
    return load_prompt("greeting").format(practice_name=practice_name, now=now)
