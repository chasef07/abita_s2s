"""Hand-kept repository facts agree with their one source."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/publish.yml",
    ".github/workflows/deploy.yml",
    ".github/workflows/replay.yml",
)
VERSION = r"(\d+(?:\.\d+)+)"

PINS = {
    "uv": (
        ("Dockerfile", rf"astral-sh/uv:{VERSION}"),
        *(
            (path, rf"astral-sh/setup-uv@.*\n\s+with:\n\s+version: '{VERSION}'")
            for path in WORKFLOWS
        ),
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


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True)


class RepoConsistencyTests(unittest.TestCase):
    def test_toolchain_pins_agree_with_their_source(self):
        found = []
        for tool, pins in PINS.items():
            source_path, source_pattern = pins[0]
            source = re.search(source_pattern, (ROOT / source_path).read_text())
            if source is None:
                found.append(f"{source_path}: no {tool} pin")
                continue
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
        if found:
            self.fail("\n".join(["Pin every file to the same version:", *found]))

    def test_tracked_files_match_no_gitignore_rule(self):
        listing = git(
            "ls-files",
            "-z",
            "--cached",
            "--ignored",
            "--exclude-per-directory=.gitignore",
        )
        tracked = [path for path in listing.split("\0") if path]
        rules = (
            git("check-ignore", "-v", "--no-index", "--", *tracked).splitlines()
            if tracked
            else []
        )
        found = [
            f"{path} matches {rule}; run git rm --cached {path}"
            for rule, path in (line.split("\t") for line in rules)
        ]
        if found:
            self.fail("\n".join(["Scratch output stays untracked:", *found]))
