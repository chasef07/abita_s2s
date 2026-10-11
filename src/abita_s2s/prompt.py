"""Load version-controlled instructions shipped with the agent package."""

from datetime import datetime
from pathlib import Path
from typing import Literal

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: Literal["speaker", "thinker"]) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"Prompt file is empty: {path}")
    return content


def thinker_instructions(started: datetime) -> str:
    """The thinker's rules plus the clinic-local call start, as the thinker sees them."""
    return (
        load_prompt("thinker") + f"\n\nCall started: {started:%A, %Y-%m-%d %H:%M %Z} "
        "(America/New_York). Use this clinic-local date "
        "to interpret relative dates."
    )
