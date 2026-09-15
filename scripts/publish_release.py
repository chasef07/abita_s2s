"""Publish immutable agent and component GitHub assets; reruns compare bytes, never clobber."""

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
    listing = json.loads(
        run("gh", "api", "repos/{owner}/{repo}/releases", "--paginate", "--slurp")
    )
    release = next((r for page in listing for r in page if r["tag_name"] == tag), None)
    if not refs:
        if release is not None:
            raise ValueError("Existing release has no tag; restore its original tag before publishing")
        run("git", "push", "origin", f"{commit}:refs/tags/{tag}")
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


def verify_reused_component(component, version, manifest):
    tag = f"{component}-v{version}"
    if run("gh", "release", "view", tag, "--json", "isDraft", "--jq", ".isDraft") != "false":
        raise ValueError("Reused component release must already be published")
    with tempfile.TemporaryDirectory() as temp:
        run("gh", "release", "download", tag, "--pattern", "release.json", "--dir", temp)
        prior = json.loads((Path(temp) / "release.json").read_text())
        if (prior[f"{component}_version"] != version or
                prior[f"{component}_sha256"] != manifest[f"{component}_sha256"]):
            raise ValueError("Reused component release content mismatch")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    manifest = json.loads((directory / "release.json").read_text())
    commit, version = manifest["git_commit"], manifest["agent_version"]
    if run("git", "rev-parse", "HEAD") != commit:
        raise ValueError("Publish must run at the built commit")
    for component in ("prompts", "evals"):
        bundle_version = manifest[f"{component}_version"]
        if bundle_version != version:
            verify_reused_component(component, bundle_version, manifest)
            continue
        asset = directory / f"{component}-v{version}.tar.gz"
        publish(f"{component}-v{version}", commit, [asset, directory / "release.json"])
    publish(f"v{version}", commit, sorted(directory.iterdir()))
