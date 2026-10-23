"""Load version-controlled instructions shipped with the agent package."""

from collections.abc import Collection
from datetime import datetime
from pathlib import Path
from typing import Literal

PROMPTS_DIR = Path(__file__).parent / "prompts"
BACKEND_TOOLS = "Backend tools:\n"
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


def speaker_instructions(capabilities: Collection[str]) -> str:
    """The speaker's rules, with its backend tools list cut to the capabilities this call offers."""
    head, tail = load_prompt("speaker").split(BACKEND_TOOLS, 1)
    listed, rest = tail.split("\n\n", 1)
    offered = [
        line
        for line in listed.splitlines()
        if line.removeprefix("- ").split(":", 1)[0] in capabilities
    ]
    return head + BACKEND_TOOLS + "\n".join(offered) + "\n\n" + rest


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
