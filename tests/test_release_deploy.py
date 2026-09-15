"""Offline release/CLI contracts: no account credentials or cloud calls."""

import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from abita_s2s import release as package

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import deploy  # noqa: E402 - standalone operational scripts
import publish_release  # noqa: E402

spec = importlib.util.spec_from_file_location("build_release", SCRIPTS / "release.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseTests(unittest.TestCase):
    def test_installed_manifest_rejects_version_and_checksum_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prompts").mkdir()
            for name in ("speaker.md", "thinker.md"):
                (root / "prompts" / name).write_text(name)
            files = package.checksums(root / "prompts")
            manifest = dict(
                agent_version="1.0.0",
                prompts_version="1.0.0",
                prompt_files=files,
                prompts_sha256=package.prompt_digest(files),
                git_commit="a" * 40,
            )
            with (
                patch.object(package, "PACKAGE", root),
                patch.object(package, "version", return_value="1.0.0"),
            ):
                (root / "release.json").write_text(json.dumps(manifest))
                self.assertEqual(package.identity(), manifest)
                for key, value in (
                    ("agent_version", "2.0.0"),
                    ("prompts_version", "2.0.0"),
                    ("prompts_sha256", "wrong"),
                ):
                    (root / "release.json").write_text(
                        json.dumps({**manifest, key: value})
                    )
                    with self.assertRaises(ValueError):
                        package.identity()
                (root / "release.json").write_text(json.dumps(manifest))
                (root / "prompts/speaker.md").write_text("changed")
                with self.assertRaises(ValueError):
                    package.identity()

    def test_prompt_archive_rerun_is_identical_and_commit_is_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "src/abita_s2s/prompts"
            prompts.mkdir(parents=True)
            for name in ("speaker.md", "thinker.md"):
                (prompts / name).write_text(name)
            (root / "pyproject.toml").write_text('[project]\nversion="1.0.0"')
            (root / "uv.lock").write_text("locked")

            def git(*args):
                if args[1] == "rev-parse":
                    return "a" * 40
                if args[1] == "status":
                    return ""
                return "1000000000"

            with (
                patch.object(builder, "ROOT", root),
                patch.object(builder, "run", side_effect=git),
            ):
                builder.prepare("a" * 40, root / "out")
                first = (root / "out/prompts-v1.0.0.tar.gz").read_bytes()
                builder.prepare("a" * 40, root / "out")
                self.assertEqual(
                    first, (root / "out/prompts-v1.0.0.tar.gz").read_bytes()
                )
                with self.assertRaises(ValueError):
                    builder.prepare("b" * 40, root / "out")

    def test_publisher_refuses_reused_tag_before_upload(self):
        calls = []

        def command(*args):
            calls.append(args)
            return "b" * 40 if args[1] == "rev-parse" else "existing"

        with (
            patch.object(publish_release, "run", side_effect=command),
            self.assertRaises(ValueError),
        ):
            publish_release.publish("v1.0.0", "a" * 40, [])
        self.assertFalse(any(c[0] == "gh" for c in calls))

    def test_publisher_rerun_compares_assets_and_never_clobbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset"
            path.write_text("original")
            calls = []

            def command(*args):
                calls.append(args)
                if args[:2] == ("git", "rev-parse"):
                    return "a" * 40
                if args[:2] == ("gh", "api"):
                    return json.dumps(
                        [
                            [
                                {
                                    "tag_name": "v1.0.0",
                                    "draft": False,
                                    "assets": [{"name": "asset"}],
                                }
                            ]
                        ]
                    )
                if args[:3] == ("gh", "release", "download"):
                    (Path(args[-1]) / "asset").write_text("original")
                return "existing"

            with patch.object(publish_release, "run", side_effect=command):
                publish_release.publish("v1.0.0", "a" * 40, [path])
                path.write_text("changed")
                with self.assertRaises(ValueError):
                    publish_release.publish("v1.0.0", "a" * 40, [path])
            self.assertFalse(any("upload" in c for c in calls))

    def test_publisher_never_recreates_missing_tag_for_existing_release(self):
        for draft in (False, True):
            with self.subTest(draft=draft):
                calls = []

                def command(*args):
                    calls.append(args)
                    if args[:2] == ("gh", "api"):
                        return json.dumps([[{
                            "tag_name": "v1.0.0", "draft": draft, "assets": [],
                        }]])
                    return ""

                with patch.object(publish_release, "run", side_effect=command):
                    with self.assertRaises(ValueError):
                        publish_release.publish("v1.0.0", "b" * 40, [])
                self.assertFalse(any(c[:2] == ("git", "push") for c in calls))
                self.assertFalse(any(c[:2] == ("gh", "release") for c in calls))


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.manifest = dict(zip(deploy.KEYS, ("1.0.0", "1.0.0", "a" * 40, "b" * 64)))
        self.status = {
            "agents": [
                {
                    "agentId": "CA_python",
                    "agentName": "abita-s2s",
                    "agentDeployments": [
                        {
                            "deployment": "staging",
                            "version": "version-exact",
                            "status": "Running",
                            "replicas": 1,
                        }
                    ],
                }
            ]
        }
        self.versions = {
            "versions": [{"version": "version-exact", "attributes": self.manifest}]
        }
        self.calls = []
        self.client = object.__new__(deploy.LiveKit)
        self.client.agent = "CA_python"
        self.client.command = self.command

    def command(self, *args):
        self.calls.append(args)
        return json.dumps(self.status if args[0] == "status" else self.versions)

    def test_observed_version_and_attributes_must_all_match(self):
        self.assertEqual(self.client.observe("staging", self.manifest), "version-exact")
        for key in deploy.KEYS:
            broken = {**self.manifest, key: "wrong"}
            with self.assertRaises(ValueError):
                self.client.observe("staging", broken)
        with self.assertRaises(ValueError):
            self.client.observe("staging", self.manifest, "replaced-staging")
        self.status["agents"][0]["agentDeployments"][0]["status"] = "Sleeping"
        with self.assertRaises(ValueError):
            self.client.observe("staging", self.manifest)

    def test_mixed_regions_absent_or_wrong_agent_fail_closed(self):
        original = copy.deepcopy(self.status)
        for change in (
            lambda a: a.update(agentName="abita-agent"),
            lambda a: a.update(agentDeployments=[]),
            lambda a: a["agentDeployments"].append(
                {
                    "deployment": "staging",
                    "version": "other",
                    "status": "Running",
                    "replicas": 1,
                }
            ),
        ):
            self.status = copy.deepcopy(original)
            change(self.status["agents"][0])
            with self.assertRaises(ValueError):
                self.client.observe("staging", self.manifest)

    def test_promotion_rechecks_staging_and_never_builds(self):
        with patch.object(self.client, "wait", return_value="version-exact"):
            self.client.execute("promote", self.manifest, "version-exact")
        self.assertIn(
            ("promote", "--id", "CA_python", "--deployment", "staging"), self.calls
        )
        self.assertFalse(any(c[0] == "deploy" for c in self.calls))
        self.calls.clear()
        with self.assertRaises(ValueError):
            self.client.execute("promote", self.manifest, "changed")
        self.assertFalse(any(c[0] == "promote" for c in self.calls))

    def test_stage_attributes_and_explicit_rollback_selection(self):
        with patch.object(self.client, "wait", return_value="version-exact"):
            self.client.execute("stage", self.manifest)
            self.client.execute("rollback", self.manifest, "version-exact")
        command = next(c for c in self.calls if c[0] == "deploy")
        self.assertEqual(command[1:3], ("--deployment", "staging"))
        for key in deploy.KEYS:
            self.assertIn(f"{key}={self.manifest[key]}", command)
        self.assertIn(
            ("rollback", "--id", "CA_python", "--version", "version-exact"), self.calls
        )
        with self.assertRaises(ValueError):
            self.client.execute("rollback", self.manifest)
        with self.assertRaises(ValueError):
            self.client.execute("rollback", self.manifest, "unknown")

    def test_health_timeout_does_not_promote(self):
        self.status["agents"][0]["agentDeployments"][0]["status"] = "Failed"
        with self.assertRaises(ValueError):
            self.client.wait("staging", self.manifest, seconds=0)
        self.assertFalse(any(c[0] == "promote" for c in self.calls))

    def test_missing_target_is_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "livekit.toml"
            path.write_text(
                '[project]\nsubdomain="test-project"\n[agent]\nid="CA_python"'
            )
            with (
                patch.dict("os.environ", {}, clear=True),
                self.assertRaises(ValueError),
            ):
                deploy.target(path)
