"""Load version-controlled instructions shipped with the agent package."""

from pathlib import Path
from typing import Literal

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: Literal["speaker", "thinker"]) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"Prompt file is empty: {path}")
    return content
