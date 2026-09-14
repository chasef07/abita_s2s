"""Publish immutable paired GitHub assets; reruns compare bytes, never clobber."""

import argparse
import json
from pathlib import Path
import tempfile

from release import digest, run


def publish(tag, commit, files):
    # Resolve an existing tag to a commit; never move it, including partial reruns.
    refs = run("git", "ls-remote", "origin", f"refs/tags/{tag}")
    if refs:
        run("git", "fetch", "origin", f"refs/tags/{tag}")
        if run("git", "rev-parse", "FETCH_HEAD^{commit}") != commit:
            raise ValueError("Immutable release tag belongs to a different commit")
    else:
        run("git", "push", "origin", f"{commit}:refs/tags/{tag}")
    listing = json.loads(
        run("gh", "api", "repos/{owner}/{repo}/releases", "--paginate", "--slurp")
    )
    release = next((r for page in listing for r in page if r["tag_name"] == tag), None)
    if release is None:
        run(
            "gh",
            "release",
            "create",
            tag,
            "--verify-tag",
            "--draft",
            "--title",
            tag,
            "--notes",
            f"Immutable source commit: {commit}. See release.json and SHA256SUMS.",
        )
        assets = set()
    else:
        assets = {a["name"] for a in release["assets"]}
    expected = {p.name for p in files}
    if assets - expected:
        raise ValueError("Unexpected assets on immutable release")
    for path in files:
        if path.name in assets:
            with tempfile.TemporaryDirectory() as temp:
                run(
                    "gh",
                    "release",
                    "download",
                    tag,
                    "--pattern",
                    path.name,
                    "--dir",
                    temp,
                )
                if digest(Path(temp) / path.name) != digest(path):
                    raise ValueError(
                        "Immutable release asset checksum mismatch: " + path.name
                    )
        else:
            if release and not release["draft"]:
                raise ValueError("Published release is incomplete; refuse mutation")
            run("gh", "release", "upload", tag, str(path))
    if release is None or release["draft"]:
        run("gh", "release", "edit", tag, "--draft=false")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    manifest = json.loads((directory / "release.json").read_text())
    commit, version = manifest["git_commit"], manifest["agent_version"]
    if run("git", "rev-parse", "HEAD") != commit:
        raise ValueError("Publish must run at the built commit")
    prompt = directory / f"prompts-v{version}.tar.gz"
    publish(f"prompts-v{version}", commit, [prompt, directory / "release.json"])
    publish(f"v{version}", commit, sorted(directory.iterdir()))
