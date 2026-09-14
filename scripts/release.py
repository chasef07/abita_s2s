"""Build deterministic paired releases from a clean exact commit. No cloud writes."""

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

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, cwd=ROOT, **kwargs).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(commit: str, output: Path):
    if (
        not re.fullmatch(r"[0-9a-f]{40}", commit)
        or run("git", "rev-parse", "HEAD") != commit
    ):
        raise ValueError("Release requires the exact checked-out 40-character commit")
    if run("git", "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Release requires a clean checkout")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Use a stable major.minor.patch release version")
    files = {
        name: digest(ROOT / "src/abita_s2s/prompts" / name)
        for name in ("speaker.md", "thinker.md")
    }
    manifest = {
        "agent_version": version,
        "prompts_version": version,
        "git_commit": commit,
        "prompt_files": files,
        "prompts_sha256": hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
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
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        entries = {
            name: (ROOT / "src/abita_s2s/prompts" / name).read_bytes() for name in files
        }
        entries["manifest.json"] = encoded
        for name, data in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), epoch, 0o644
            archive.addfile(info, io.BytesIO(data))
    (output / f"prompts-v{version}.tar.gz").write_bytes(
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
    (output / "uv.lock").write_bytes((ROOT / "uv.lock").read_bytes())
    sums = {
        p.name: digest(p) for p in sorted(output.iterdir()) if p.name != "SHA256SUMS"
    }
    (output / "SHA256SUMS").write_text(
        "".join(f"{sha}  {name}\n" for name, sha in sums.items())
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    build(args.commit, args.output.resolve())
