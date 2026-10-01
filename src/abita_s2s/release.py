"""Verify the release identity shipped inside the installed package, offline."""

import hashlib
import json
import re
from functools import cache
from importlib.metadata import version
from pathlib import Path

from abita_s2s.observability.evaluation import EVALUATOR_VERSION

PACKAGE = Path(__file__).parent


COMPONENTS = json.loads((PACKAGE / "component_folders.json").read_text())


def checksums(directory: Path, names) -> dict[str, str]:
    return {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in names
    }


def content_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def identity() -> dict:
    path = PACKAGE / "release.json"
    if not path.exists():
        return {"agent_version": version("abita-s2s"), "git_commit": "development"}
    data = json.loads(path.read_text())
    if data["agent_version"] != version("abita-s2s"):
        raise ValueError("Installed release version mismatch")
    for component, directory in COMPONENTS.items():
        files = data[f"{component}_files"]
        if (
            not re.fullmatch(r"\d+\.\d+\.\d+", data[f"{component}_version"])
            or not files
            or any(
                Path(name).is_absolute() or ".." in Path(name).parts for name in files
            )
            or content_digest(files) != data[f"{component}_sha256"]
        ):
            raise ValueError("Installed component version or checksum mismatch")
        if directory.startswith("src/abita_s2s/"):
            installed = PACKAGE / directory.removeprefix("src/abita_s2s/")
            if checksums(installed, files) != files:
                raise ValueError("Installed component content mismatch")
    return data


@cache
def call_versions() -> dict[str, str]:
    """Versions that shape every call, read once from the verified installed release."""
    release = identity()
    return {
        "agent": release["agent_version"],
        "gitCommit": release["git_commit"],
        "prompts": release.get("prompts_version", "development"),
        "tools": release.get("tools_version", "development"),
        "judges": release.get("observability_version", "development"),
        "evaluator": EVALUATOR_VERSION,
    }


def smoke() -> None:
    from abita_s2s.main import server
    from abita_s2s.prompt import load_prompt

    assert server is not None
    for name in ("speaker", "thinker"):
        assert load_prompt(name)
    print(json.dumps(identity(), sort_keys=True))


if __name__ == "__main__":
    smoke()
