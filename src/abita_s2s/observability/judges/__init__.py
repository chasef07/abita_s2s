"""Each module in this package is one judge, keyed by its module name.

A judge module defines ``QUESTION`` and may define ``skip(history, closeout)``,
which returns a result instead of asking the jury.
"""

import hashlib
import importlib
import pkgutil
from pathlib import Path

JURORS = (
    "typesafe/jev-1.14",
    "perplexity/pplx-decider-v1.2",
    "baseten/mammoth-1:free",
)

JUDGES = {
    module.name: importlib.import_module(f"{__name__}.{module.name}")
    for module in pkgutil.iter_modules(__path__)
}

QUESTIONS = {name: judge.QUESTION for name, judge in JUDGES.items()}

GATES = {name: judge.skip for name, judge in JUDGES.items() if hasattr(judge, "skip")}

DEFINITIONS_HASH = hashlib.sha256(
    b"".join(
        name.encode()
        + b"\0"
        + Path(__file__).with_name(f"{name}.py").read_bytes().replace(b"\r\n", b"\n")
        + b"\0"
        for name in JUDGES
    )
).hexdigest()[:12]
