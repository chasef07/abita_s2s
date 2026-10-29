"""Each module in this package is one judge, keyed by its module name.

A judge module defines ``QUESTION`` and may define ``skip(history, closeout)``,
which returns a result instead of asking the jury. The definitions hash also
covers what judges are shown, so a state change gets a new scorecard version.
"""

import hashlib
import importlib
import pkgutil
from pathlib import Path

from abita_s2s.prompt import PROMPTS_DIR

JURORS = (
    "typesafe/jev-1.14",
    "perplexity/pplx-decider-v1.2",
    "baseten/mammoth-1",
)

JUDGES = {
    module.name: importlib.import_module(f"{__name__}.{module.name}")
    for module in pkgutil.iter_modules(__path__)
}

QUESTIONS = {name: judge.QUESTION for name, judge in JUDGES.items()}

GATES = {name: judge.skip for name, judge in JUDGES.items() if hasattr(judge, "skip")}

STATE_SOURCES = (
    Path(__file__).parents[1] / "judge_state.py",
    PROMPTS_DIR / "transfer_policy.md",
)

DEFINITIONS_HASH = hashlib.sha256(
    b"".join(
        path.stem.encode() + b"\0" + path.read_bytes().replace(b"\r\n", b"\n") + b"\0"
        for path in (
            *(Path(__file__).with_name(f"{name}.py") for name in JUDGES),
            *STATE_SOURCES,
        )
    )
).hexdigest()[:12]
