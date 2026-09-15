"""Verify the release identity shipped inside the installed package, offline."""

import hashlib
import json
from importlib.metadata import version
from pathlib import Path

PACKAGE = Path(__file__).parent


def checksums(directory: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("speaker.md", "thinker.md")
    }


def prompt_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def identity() -> dict:
    path = PACKAGE / "release.json"
    if not path.exists():
        return {"agent_version": version("abita-s2s"), "git_commit": "development"}
    data = json.loads(path.read_text())
    files = checksums(PACKAGE / "prompts")
    if (
        data["agent_version"] != version("abita-s2s")
        or data["prompts_version"] != data["agent_version"]
        or files != data["prompt_files"]
        or prompt_digest(files) != data["prompts_sha256"]
    ):
        raise ValueError("Installed release version or prompt checksum mismatch")
    return data


def smoke() -> None:
    from abita_s2s.main import server
    from abita_s2s.prompt import load_prompt

    assert server is not None
    for name in ("speaker", "thinker"):
        assert load_prompt(name)
    files = list((PACKAGE / "insurance_data").glob("*.json"))
    assert len(files) == 4
    for path in files:
        assert json.loads(path.read_text())
    print(json.dumps(identity(), sort_keys=True))


if __name__ == "__main__":
    smoke()
