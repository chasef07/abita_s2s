"""Offline release/CLI contracts: no account credentials or cloud calls."""

import copy
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import tarfile
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


class ComponentVersionTests(unittest.TestCase):
    def test_versions_follow_content_independently_using_real_git_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
            git("init", "-q")
            git("config", "user.name", "Release test")
            git("config", "user.email", "release@example.test")
            prompts = root / "src/abita_s2s/prompts"
            prompts.mkdir(parents=True)
            for name in ("speaker.md", "thinker.md"):
                (prompts / name).write_text(name + "\n")
            evals = root / "evals"
            evals.mkdir()
            (evals / "scenario.yaml").write_text("name: original\n")
            git("add", ".")
            git("commit", "-qm", "Initial bundles")
            git("tag", "prompts-v0.3.2")
            git("tag", "evals-v0.3.2")
            with patch.object(builder, "ROOT", root):
                def versions():
                    commit = git("rev-parse", "HEAD")
                    return (
                        builder.component_version("prompts", "0.3.3", commit, package.checksums(prompts)),
                        builder.component_version("evals", "0.3.3", commit, package.eval_checksums(evals)),
                    )
                self.assertEqual(versions(), ("0.3.2", "0.3.2"))
                (prompts / "speaker.md").write_text("changed\n")
                self.assertEqual(versions(), ("0.3.3", "0.3.2"))
                (evals / "added.yml").write_text("name: added\n")
                self.assertEqual(versions(), ("0.3.3", "0.3.3"))
                (prompts / "speaker.md").write_text("speaker.md\n")
                self.assertEqual(versions(), ("0.3.2", "0.3.3"))
                (evals / "added.yml").unlink()
                (evals / "results.json").write_text("{}")
                self.assertEqual(versions(), ("0.3.2", "0.3.2"))
                git("add", ".")
                git("commit", "-qm", "Agent-only release")
                git("tag", "prompts-v0.3.3")
                git("tag", "evals-v0.3.3")
                self.assertEqual(versions(), ("0.3.2", "0.3.2"))
                (evals / "scenario.yaml").unlink()
                (evals / "replacement.yaml").write_text("name: original\n")
                self.assertEqual(versions(), ("0.3.2", "0.3.3"))

    def test_reused_release_must_be_published_with_matching_content(self):
        manifest = {"prompts_version": "0.3.2", "prompts_sha256": "a" * 64}
        def command(*args):
            if args[1:3] == ("release", "view"):
                return "false"
            self.assertEqual(args[1:3], ("release", "download"))
            (Path(args[-1]) / "release.json").write_text(json.dumps(manifest))
            return ""
        with patch.object(publish_release, "run", side_effect=command):
            publish_release.verify_reused_component("prompts", "0.3.2", manifest)
            with self.assertRaisesRegex(ValueError, "content mismatch"):
                publish_release.verify_reused_component("prompts", "0.3.2", {**manifest, "prompts_sha256": "wrong"})
        with patch.object(publish_release, "run", return_value="true"):
            with self.assertRaisesRegex(ValueError, "already be published"):
                publish_release.verify_reused_component("prompts", "0.3.2", manifest)


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
                prompts_version="0.8.0",
                evals_version="0.9.0",
                eval_files={"scenarios.yaml": "b" * 64},
                evals_sha256=package.content_digest({"scenarios.yaml": "b" * 64}),
                prompt_files=files,
                prompts_sha256=package.content_digest(files),
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
                    ("prompts_version", "invalid"),
                    ("evals_version", "invalid"),
                    ("evals_sha256", "wrong"),
                    ("eval_files", {}),
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

    def test_archives_are_reproducible_and_eval_changes_change_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompts = root / "src/abita_s2s/prompts"
            prompts.mkdir(parents=True)
            for name in ("speaker.md", "thinker.md"):
                (prompts / name).write_text(name)
            (root / "pyproject.toml").write_text('[project]\nversion="1.0.0"')
            (root / "uv.lock").write_text("locked")
            (root / "evals").mkdir()
            (root / "evals/scenarios.yaml").write_text("name: intake\nscenarios: []\n")
            (root / "evals/result.json").write_text("not a scenario")

            def git(*args):
                if args[1] == "tag":
                    return ""
                if args[1:] == ("rev-parse", "--is-shallow-repository"):
                    return "false"
                if args[1] == "rev-parse":
                    return "a" * 40
                if args[1] == "status":
                    return ""
                return "1000000000"

            with (
                patch.object(builder, "ROOT", root),
                patch.object(builder, "run", side_effect=git),
            ):
                manifest, _ = builder.prepare("a" * 40, root / "out")
                first = (root / "out/prompts-v1.0.0.tar.gz").read_bytes()
                eval_archive = root / "out/evals-v1.0.0.tar.gz"
                first_evals = eval_archive.read_bytes()
                with tarfile.open(eval_archive) as archive:
                    self.assertEqual(set(archive.getnames()), {"scenarios.yaml", "manifest.json"})
                    self.assertEqual(archive.extractfile("scenarios.yaml").read(), (root / "evals/scenarios.yaml").read_bytes())
                    self.assertEqual(json.load(archive.extractfile("manifest.json")), manifest)
                builder.prepare("a" * 40, root / "out")
                self.assertEqual(first_evals, eval_archive.read_bytes())
                self.assertEqual(
                    first, (root / "out/prompts-v1.0.0.tar.gz").read_bytes()
                )
                with self.assertRaises(ValueError):
                    builder.prepare("b" * 40, root / "out")
                (root / "evals/scenarios.yaml").write_text("name: changed\nscenarios: []\n")
                with self.assertRaisesRegex(ValueError, "different release"):
                    builder.prepare("a" * 40, root / "out")
                changed, _ = builder.prepare("a" * 40, root / "changed")
                self.assertNotEqual(manifest["evals_sha256"], changed["evals_sha256"])
                with patch.object(builder, "component_version", return_value="0.9.0"):
                    reused, _ = builder.prepare("a" * 40, root / "reused")
                self.assertEqual(reused["prompts_version"], "0.9.0")
                self.assertEqual(reused["evals_version"], "0.9.0")
                self.assertEqual(list((root / "reused").glob("*.tar.gz")), [])
                self.assertEqual(manifest["prompts_sha256"], changed["prompts_sha256"])
                (root / "evals/scenarios.yaml").unlink()
                with self.assertRaisesRegex(ValueError, "at least one eval"):
                    builder.prepare("a" * 40, root / "empty")

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

    def test_publisher_finishes_release_please_draft_with_existing_tag(self):
        calls = []

        def command(*args):
            calls.append(args)
            if args[:2] == ("git", "rev-parse"):
                return "a" * 40
            if args[:2] == ("gh", "api"):
                return json.dumps([[{
                    "tag_name": "v1.0.0", "draft": True, "assets": [],
                }]])
            return "existing"

        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "release.json"
            asset.write_text("verified release")
            with patch.object(publish_release, "run", side_effect=command):
                publish_release.publish("v1.0.0", "a" * 40, [asset])
        self.assertIn(("gh", "release", "upload", "v1.0.0", str(asset)), calls)
        self.assertIn(("gh", "release", "edit", "v1.0.0", "--draft=false"), calls)
        self.assertFalse(any(c[:3] == ("gh", "release", "create") for c in calls))
        self.assertFalse(any(c[:2] == ("git", "push") for c in calls))


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.manifest = dict(
            agent_version="1.0.0", prompts_version="1.0.0", evals_version="1.0.0",
            git_commit="a" * 40, prompts_sha256="b" * 64, evals_sha256="c" * 64,
        )
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

    def test_config_path_is_relative_for_livekit_cli(self):
        config = Path("livekit.toml").resolve()
        with (
            patch.object(deploy, "target", return_value="CA_python"),
            patch.object(deploy, "run", return_value="lk version 2.18.6") as run,
        ):
            client = deploy.LiveKit(config)
            client.command("deploy", ".")
        run.assert_called_with(
            "lk", "--config", "livekit.toml", "--yes", "agent", "deploy", "."
        )

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
        self.assertNotIn("--id", command)
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

    def test_automatic_deploy_uses_default_production_and_exact_attributes(self):
        with patch.object(self.client, "wait", return_value="version-exact") as wait:
            self.assertEqual(
                self.client.execute("deploy", self.manifest), "version-exact"
            )
        command = next(c for c in self.calls if c[0] == "deploy")
        self.assertNotIn("--deployment", command)
        self.assertNotIn("--id", command)
        self.assertIn("--no-default-attributes", command)
        for key in deploy.KEYS:
            self.assertIn(f"{key}={self.manifest[key]}", command)
        wait.assert_called_once_with("production", self.manifest)

    def test_automatic_deploy_rejects_typescript_target_before_mutation(self):
        self.status["agents"][0]["agentName"] = "abita-agent"
        with self.assertRaises(ValueError):
            self.client.execute("deploy", self.manifest)
        self.assertFalse(any(c[0] == "deploy" for c in self.calls))

    def test_automatic_deploy_reports_failed_health(self):
        with (
            patch.object(self.client, "wait", side_effect=ValueError("Unhealthy")),
            self.assertRaisesRegex(ValueError, "Unhealthy"),
        ):
            self.client.execute("deploy", self.manifest)

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
