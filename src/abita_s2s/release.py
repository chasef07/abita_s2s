"""Verify the release identity shipped inside the installed package, offline."""

import hashlib
import json
import re
from importlib.metadata import version
from pathlib import Path

PACKAGE = Path(__file__).parent


def checksums(directory: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("speaker.md", "thinker.md")
    }


def content_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def eval_checksums(directory: Path) -> dict[str, str]:
    files = {
        path.relative_to(directory).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.suffix in (".yaml", ".yml")
    }
    if not files:
        raise ValueError("Release requires at least one eval scenario file")
    return files


def identity() -> dict:
    path = PACKAGE / "release.json"
    if not path.exists():
        return {"agent_version": version("abita-s2s"), "git_commit": "development"}
    data = json.loads(path.read_text())
    files = checksums(PACKAGE / "prompts")
    if (
        data["agent_version"] != version("abita-s2s")
        or not re.fullmatch(r"\d+\.\d+\.\d+", data["prompts_version"])
        or not re.fullmatch(r"\d+\.\d+\.\d+", data["evals_version"])
        or not data["eval_files"]
        or content_digest(data["eval_files"]) != data["evals_sha256"]
        or files != data["prompt_files"]
        or content_digest(files) != data["prompts_sha256"]
    ):
        raise ValueError("Installed release version, prompt or eval checksum mismatch")
    return data


def smoke() -> None:
    from abita_s2s.main import server
    from abita_s2s.prompt import load_prompt

    assert server is not None
    for name in ("speaker", "thinker"):
        assert load_prompt(name)
    print(json.dumps(identity(), sort_keys=True))


if __name__ == "__main__":
    smoke()
