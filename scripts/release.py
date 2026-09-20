"""Build agent, prompt and eval releases from a clean exact commit. No cloud writes."""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tomllib

from abita_s2s.model_config import SPEAKER_MODEL, THINKER_MODEL
from abita_s2s.release import checksums, eval_checksums, content_digest

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, cwd=ROOT, **kwargs).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def component_version(component: str, version: str, commit: str, files: dict) -> str:
    """Reuse the previous published bundle when its complete content matches."""
    tags = run(
        "git",
        "tag",
        "--merged",
        commit,
        "--list",
        f"{component}-v*",
        "--sort=-version:refname",
    ).splitlines()
    current = tuple(map(int, version.split(".")))
    for tag in tags:
        prior = tag.removeprefix(f"{component}-v")
        if not re.fullmatch(r"\d+\.\d+\.\d+", prior):
            continue
        # Ignore this release's tags so publishing cannot change a rebuild.
        if tuple(map(int, prior.split("."))) >= current:
            continue
        directory = "src/abita_s2s/prompts" if component == "prompts" else "evals"
        paths = run(
            "git", "ls-tree", "-r", "--name-only", tag, "--", directory
        ).splitlines()
        prior_files = {}
        for path in paths:
            name = path.removeprefix(directory + "/")
            if (component == "prompts" and name in ("speaker.md", "thinker.md")) or (
                component == "evals" and Path(name).suffix in (".yaml", ".yml")
            ):
                data = subprocess.check_output(
                    ["git", "show", f"{tag}:{path}"], cwd=ROOT
                )
                prior_files[name] = hashlib.sha256(data).hexdigest()
        return prior if prior_files == files else version
    return version


def prepare(commit: str, output: Path):
    if (
        not re.fullmatch(r"[0-9a-f]{40}", commit)
        or run("git", "rev-parse", "HEAD") != commit
    ):
        raise ValueError("Release requires the exact checked-out 40-character commit")
    if run("git", "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Release requires a clean checkout")
    if run("git", "rev-parse", "--is-shallow-repository") == "true":
        raise ValueError("Release requires full git history and component tags")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Use a stable major.minor.patch release version")
    files = checksums(ROOT / "src/abita_s2s/prompts")
    eval_files = eval_checksums(ROOT / "evals")
    manifest = {
        "agent_version": version,
        "prompts_version": component_version("prompts", version, commit, files),
        "evals_version": component_version("evals", version, commit, eval_files),
        "git_commit": commit,
        "prompt_files": files,
        "prompts_sha256": content_digest(files),
        "eval_files": eval_files,
        "evals_sha256": content_digest(eval_files),
        "uv_lock_sha256": digest(ROOT / "uv.lock"),
        "models": {"speaker": SPEAKER_MODEL, "thinker": THINKER_MODEL},
    }
    encoded = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    output.mkdir(parents=True, exist_ok=True)
    prior = output / "release.json"
    if prior.exists() and prior.read_bytes() != encoded:
        raise ValueError(
            "Output directory belongs to a different release; use a fresh directory"
        )
    (ROOT / "src/abita_s2s/release.json").write_bytes(encoded)
    (output / "release.json").write_bytes(encoded)
    epoch = int(run("git", "show", "-s", "--format=%ct", commit))
    # Fixed order, owner and timestamps make reruns byte-identical.
    for label, directory, names in (
        ("prompts", ROOT / "src/abita_s2s/prompts", files),
        ("evals", ROOT / "evals", eval_files),
    ):
        if manifest[f"{label}_version"] != version:
            continue
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as archive:
            entries = {name: (directory / name).read_bytes() for name in names}
            entries["manifest.json"] = encoded
            for name, data in sorted(entries.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.mode = len(data), epoch, 0o644
                archive.addfile(info, io.BytesIO(data))
        (output / f"{label}-v{version}.tar.gz").write_bytes(
            gzip.compress(buf.getvalue(), mtime=0)
        )
    return manifest, epoch


def build(commit: str, output: Path):
    manifest, epoch = prepare(commit, output)
    run(
        "uv",
        "build",
        "--no-sources",
        "--out-dir",
        str(output),
        env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch)},
    )
    (output / ".gitignore").unlink(
        missing_ok=True
    )  # uv creates this build-directory marker.
    (output / "uv.lock").write_bytes((ROOT / "uv.lock").read_bytes())
    sums = {
        p.name: digest(p) for p in sorted(output.iterdir()) if p.name != "SHA256SUMS"
    }
    (output / "SHA256SUMS").write_text(
        "".join(f"{sha}  {name}\n" for name, sha in sums.items())
    )
    from deploy import validate_release

    validate_release(output, commit)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    build(args.commit, args.output.resolve())
