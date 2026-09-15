"""Explicit release selection and fail-closed LiveKit deployment operations.

Only the serialized GitHub deployment workflow should invoke mutations. No create,
secret upload, image upload or implicit production selection is implemented here.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import tomllib

from abita_s2s.release import checksums, prompt_digest

KEYS = ("agent_version", "prompts_version", "git_commit", "prompts_sha256")


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def target(config: Path):
    data = tomllib.loads(config.read_text())
    agent = data["agent"]["id"]
    project = data["project"]["subdomain"]
    if not re.fullmatch(r"CA_[A-Za-z0-9]+", agent) or not re.fullmatch(
        r"[a-z0-9-]+", project
    ):
        raise ValueError("Provision an explicit Python agent ID and project subdomain")
    # Explicit allowlist requires an operator to provision this new Python target.
    if agent != os.environ.get("ABITA_S2S_AGENT_ID"):
        raise ValueError(
            "Target does not match the separately provisioned Python agent"
        )
    return agent


def validate_release(directory: Path, commit: str):
    run("git", "diff", "--exit-code", "HEAD")
    checked = set()
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        sha, name = line.split("  ")
        if name in checked or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("Invalid release checksum entry")
        checked.add(name)
        if Path(name).name != name:
            raise ValueError("Invalid release asset path")
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != sha:
            raise ValueError("Release checksum mismatch: " + name)
    manifest = json.loads((directory / "release.json").read_text())
    if manifest["git_commit"] != commit or run("git", "rev-parse", "HEAD") != commit:
        raise ValueError("Release commit does not match exact checkout")
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    expected_assets = {
        "release.json",
        "uv.lock",
        f"prompts-v{version}.tar.gz",
        f"abita_s2s-{version}.tar.gz",
        f"abita_s2s-{version}-py3-none-any.whl",
    }
    if checked != expected_assets:
        raise ValueError("Release checksum list is incomplete or unexpected")
    files = checksums(Path("src/abita_s2s/prompts"))
    sha = prompt_digest(files)
    if (
        manifest["agent_version"] != version
        or manifest["prompts_version"] != version
        or manifest["prompt_files"] != files
        or manifest["prompts_sha256"] != sha
        or manifest["uv_lock_sha256"]
        != hashlib.sha256(Path("uv.lock").read_bytes()).hexdigest()
    ):
        raise ValueError("Release version, lock or prompt mismatch")
    return manifest


class LiveKit:
    def __init__(self, config):
        self.agent = target(config)
        self.prefix = ["lk", "--config", str(config.resolve()), "--yes"]
        if run("lk", "--version") != "lk version 2.18.6":
            raise ValueError("LiveKit CLI 2.18.6 is required")

    def command(self, *args):
        return run(*self.prefix, "agent", *args)

    def versions(self):
        return json.loads(self.command("versions", "--id", self.agent, "--json"))[
            "versions"
        ]

    def observe(self, deployment, manifest, expected=None):
        status = json.loads(self.command("status", "--id", self.agent, "--json"))
        agents = [a for a in status.get("agents", []) if a.get("agentId") == self.agent]
        if len(agents) != 1 or agents[0].get("agentName") != "abita-s2s":
            raise ValueError("Target is not the Python abita-s2s agent")
        rows = [
            r
            for r in agents[0].get("agentDeployments", [])
            if (r.get("deployment") or "production") == deployment
        ]
        if not rows or any(
            r.get("status") != "Running" or r.get("replicas", 0) < 1 for r in rows
        ):
            raise ValueError("Expected deployment has no healthy running replicas")
        ids = {r.get("version") for r in rows}
        if len(ids) != 1 or None in ids or "" in ids:
            raise ValueError("Deployment has mixed or unknown versions")
        version = ids.pop()
        if expected and version != expected:
            raise ValueError("Deployment changed since validation")
        versions = [v for v in self.versions() if v["version"] == version]
        if len(versions) != 1 or any(
            versions[0].get("attributes", {}).get(k) != manifest[k] for k in KEYS
        ):
            raise ValueError("Observed release attributes mismatch")
        return version

    def wait(self, deployment, manifest, expected=None, seconds=300):
        deadline = time.monotonic() + seconds
        while True:
            try:
                return self.observe(deployment, manifest, expected)
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(5)

    def execute(self, action, manifest, expected=None):
        # Check dispatch identity even before the first mutation.
        status = json.loads(self.command("status", "--id", self.agent, "--json"))
        agents = status.get("agents", [])
        if (
            len(agents) != 1
            or agents[0].get("agentId") != self.agent
            or agents[0].get("agentName") != "abita-s2s"
        ):
            raise ValueError(
                "Provision the new Python abita-s2s agent before deploying"
            )
        if action == "stage":
            attrs = [
                arg for key in KEYS for arg in ("--attribute", f"{key}={manifest[key]}")
            ]
            self.command(
                "deploy",
                "--deployment",
                "staging",
                "--no-default-attributes",
                *attrs,
                ".",
            )
            return self.wait("staging", manifest)
        if not expected:
            raise ValueError(
                "Promotion/rollback requires an explicit known LiveKit version"
            )
        if action == "promote":
            self.observe("staging", manifest, expected)
            self.command("promote", "--id", self.agent, "--deployment", "staging")
        elif action == "rollback":
            versions = [v for v in self.versions() if v["version"] == expected]
            if len(versions) != 1 or any(
                versions[0].get("attributes", {}).get(k) != manifest[k] for k in KEYS
            ):
                raise ValueError("Rollback version does not match selected release")
            self.command("rollback", "--id", self.agent, "--version", expected)
        else:
            raise ValueError("Unsupported operation")
        return self.wait("production", manifest, expected)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stage", "promote", "rollback"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version")
    args = parser.parse_args()
    manifest = validate_release(args.release, args.commit)
    Path("src/abita_s2s/release.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    )
    client = LiveKit(args.config)
    observed = client.execute(args.action, manifest, args.version)
    record = {
        **{k: manifest[k] for k in KEYS},
        "livekit_version": observed,
        "agent_id": client.agent,
        "action": args.action,
    }
    Path("deployment-record.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))
