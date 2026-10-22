"""Hand-kept repository facts agree with their one source."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/publish.yml",
    ".github/workflows/deploy.yml",
)
VERSION = r"(\d+(?:\.\d+)+)"

PINS = {
    "uv": (
        ("Dockerfile", rf"astral-sh/uv:{VERSION}"),
        *((path, rf"(?m)^\s+version: '{VERSION}'") for path in WORKFLOWS),
        ("pyproject.toml", rf"uv_build=={VERSION}"),
    ),
    "Python": (
        (".python-version", rf"^{VERSION}\s*$"),
        ("Dockerfile", rf"python:{VERSION}-"),
        ("Dockerfile", rf"python{VERSION}/site-packages"),
        *((path, rf"python-version: '{VERSION}'") for path in WORKFLOWS),
    ),
    "LiveKit CLI": (
        ("scripts/deploy.py", rf'LK_VERSION = "{VERSION}"'),
        (".github/workflows/deploy.yml", rf"livekit-cli/releases/download/v{VERSION}/"),
        (".github/workflows/deploy.yml", rf"lk_{VERSION}_linux"),
    ),
}


class RepoConsistencyTests(unittest.TestCase):
    def test_toolchain_pins_agree_with_their_source(self):
        found = []
        for tool, pins in PINS.items():
            source_path, source_pattern = pins[0]
            source = re.search(source_pattern, (ROOT / source_path).read_text())
            if source is None:
                self.fail(f"No {tool} pin in {source_path}")
            expected = source.group(1).split(".")
            for path, pattern in pins:
                pinned = re.findall(pattern, (ROOT / path).read_text())
                if not pinned:
                    found.append(f"{path}: no {tool} pin matches {pattern!r}")
                for version in pinned:
                    parts = version.split(".")
                    if parts != expected[: len(parts)]:
                        found.append(
                            f"{path} pins {tool} {version}, "
                            f"but {source_path} pins {'.'.join(expected)}"
                        )
        self.assertEqual(found, [], "Pin every file to the same version")
