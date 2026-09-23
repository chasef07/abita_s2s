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


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def repository(root, *, legacy=False):
    git(root, "init", "-q")
    git(root, "config", "user.name", "Release test")
    git(root, "config", "user.email", "release@example.test")
    for component, directory in package.COMPONENTS.items():
        folder = root / directory
        (folder / "nested").mkdir(parents=True)
        (folder / "nested/data.txt").write_text(component)
        (folder / "README.md").write_text("Instructions")
    for name in ("speaker.md", "thinker.md"):
        (root / package.COMPONENTS["prompts"] / name).write_text(name)
    (root / "evals/scenario.yaml").write_text("name: test")
    (root / "pyproject.toml").write_text('[project]\nversion="1.0.0"')
    (root / "uv.lock").write_text("locked")
    (root / ".gitignore").write_text(
        "__pycache__/\n*.pyc\nout*/\nsrc/abita_s2s/release.json\n"
    )
    if not legacy:
        (root / "src/abita_s2s/component_folders.json").write_text(
            json.dumps(package.COMPONENTS)
        )
    git(root, "add", ".")
    git(root, "commit", "-qm", "Initial folders")
    return git(root, "rev-parse", "HEAD")


class ComponentVersionTests(unittest.TestCase):
    def test_versions_follow_entire_folders_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository(root)
            for component in package.COMPONENTS:
                git(root, "tag", f"{component}-v0.9.0")
            with patch.object(builder, "ROOT", root):

                def versions():
                    commit = git(root, "rev-parse", "HEAD")
                    return {
                        name: builder.component_version(
                            name, "1.0.0", commit, builder.component_files(name, commit)
                        )
                        for name in package.COMPONENTS
                    }

                unchanged = dict.fromkeys(package.COMPONENTS, "0.9.0")
                self.assertEqual(versions(), unchanged)
                for component, directory in package.COMPONENTS.items():
                    for operation in ("edit", "add", "rename", "delete"):
                        with self.subTest(component=component, operation=operation):
                            folder = root / directory
                            if operation == "edit":
                                (folder / "README.md").write_text("New instructions")
                            elif operation == "add":
                                (folder / "nested/new.json").write_text("{}")
                            elif operation == "rename":
                                (folder / "nested/data.txt").rename(
                                    folder / "nested/moved.txt"
                                )
                            else:
                                (folder / "README.md").unlink()
                            git(root, "add", ".")
                            git(root, "commit", "-qm", operation)
                            self.assertEqual(
                                versions(), {**unchanged, component: "1.0.0"}
                            )
                            git(root, "revert", "--no-edit", "HEAD")
                            self.assertEqual(versions(), unchanged)
                (root / "agent-only.txt").write_text("agent change")
                git(root, "add", ".")
                git(root, "commit", "-qm", "Agent only")
                self.assertEqual(versions(), unchanged)
                # A rerun ignores tags for the current release itself.
                for component in package.COMPONENTS:
                    git(root, "tag", f"{component}-v1.0.0")
                self.assertEqual(versions(), unchanged)

    def test_old_partial_bundles_cannot_represent_whole_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            commit = repository(root, legacy=True)
            for component in ("prompts", "evals"):
                git(root, "tag", f"{component}-v0.9.0")
            with patch.object(builder, "ROOT", root):
                for component in package.COMPONENTS:
                    files = builder.component_files(component, commit)
                    self.assertEqual(
                        builder.component_version(component, "1.0.0", commit, files),
                        "1.0.0",
                    )

    def test_reused_release_must_be_published_with_matching_content(self):
        for component in package.COMPONENTS:
            with self.subTest(component=component):
                manifest = {
                    f"{component}_version": "0.3.2",
                    f"{component}_sha256": "a" * 64,
                }

                def command(*args):
                    if args[1:3] == ("release", "view"):
                        return "false"
                    self.assertEqual(args[1:3], ("release", "download"))
                    (Path(args[-1]) / "release.json").write_text(json.dumps(manifest))
                    return ""

                with patch.object(publish_release, "run", side_effect=command):
                    publish_release.verify_reused_component(
                        component, "0.3.2", manifest
                    )
                    with self.assertRaisesRegex(ValueError, "content mismatch"):
                        publish_release.verify_reused_component(
                            component,
                            "0.3.2",
                            {**manifest, f"{component}_sha256": "wrong"},
                        )
                with patch.object(publish_release, "run", return_value="true"):
                    with self.assertRaisesRegex(ValueError, "already be published"):
                        publish_release.verify_reused_component(
                            component, "0.3.2", manifest
                        )


class ReleaseTests(unittest.TestCase):
    def test_installed_manifest_checks_every_packaged_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            commit = repository(root)
            with patch.object(builder, "ROOT", root):
                manifest, _ = builder.prepare(commit, root / "out")
            installed = root / "src/abita_s2s"
            with (
                patch.object(package, "PACKAGE", installed),
                patch.object(package, "version", return_value="1.0.0"),
            ):
                self.assertEqual(package.identity(), manifest)
                for component, directory in package.COMPONENTS.items():
                    for key, value in (
                        (f"{component}_version", "bad"),
                        (f"{component}_files", {}),
                        (f"{component}_sha256", "bad"),
                    ):
                        with self.subTest(key=key):
                            (installed / "release.json").write_text(
                                json.dumps({**manifest, key: value})
                            )
                            with self.assertRaises(ValueError):
                                package.identity()
                    (installed / "release.json").write_text(json.dumps(manifest))
                    if component != "evals":
                        path = root / directory / "nested/data.txt"
                        original = path.read_text()
                        path.write_text("tampered")
                        with self.assertRaises(ValueError):
                            package.identity()
                        path.write_text(original)

    def test_archives_cover_entire_tracked_folders_and_are_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            commit = repository(root)
            # Ignored runtime files must not affect source identities or bundles.
            for directory in package.COMPONENTS.values():
                cache = root / directory / "__pycache__"
                cache.mkdir()
                (cache / "module.pyc").write_bytes(b"cache")
            with patch.object(builder, "ROOT", root):
                manifest, _ = builder.prepare(commit, root / "out")
                archives = {}
                for component, directory in package.COMPONENTS.items():
                    path = root / "out" / f"{component}-v1.0.0.tar.gz"
                    archives[component] = path.read_bytes()
                    with tarfile.open(path) as archive:
                        self.assertEqual(
                            set(archive.getnames()), set(manifest[f"{component}_files"])
                        )
                        self.assertIn("README.md", archive.getnames())
                        self.assertIn("nested/data.txt", archive.getnames())
                        for name in archive.getnames():
                            self.assertEqual(
                                archive.extractfile(name).read(),
                                (root / directory / name).read_bytes(),
                            )
                builder.prepare(commit, root / "out")
                for component, data in archives.items():
                    self.assertEqual(
                        data, (root / "out" / f"{component}-v1.0.0.tar.gz").read_bytes()
                    )
                with self.assertRaises(ValueError):
                    builder.prepare("b" * 40, root / "out")
                with patch.object(builder, "component_version", return_value="0.9.0"):
                    reused, _ = builder.prepare(commit, root / "out-reused")
                for component in package.COMPONENTS:
                    self.assertEqual(reused[f"{component}_version"], "0.9.0")
                self.assertEqual(list((root / "out-reused").glob("*.tar.gz")), [])
                (root / "evals/README.md").write_text("Changed guidance")
                git(root, "add", ".")
                git(root, "commit", "-qm", "Update eval docs")
                changed_commit = git(root, "rev-parse", "HEAD")
                with self.assertRaisesRegex(ValueError, "different release"):
                    builder.prepare(changed_commit, root / "out")
                changed, _ = builder.prepare(changed_commit, root / "out-changed")
                self.assertNotEqual(manifest["evals_sha256"], changed["evals_sha256"])
                for component in ("prompts", "tools", "observability"):
                    self.assertEqual(
                        manifest[f"{component}_sha256"], changed[f"{component}_sha256"]
                    )

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
                        return json.dumps(
                            [
                                [
                                    {
                                        "tag_name": "v1.0.0",
                                        "draft": draft,
                                        "assets": [],
                                    }
                                ]
                            ]
                        )
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
                return json.dumps(
                    [
                        [
                            {
                                "tag_name": "v1.0.0",
                                "draft": True,
                                "assets": [],
                            }
                        ]
                    ]
                )
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
            agent_version="1.0.0",
            prompts_version="1.0.0",
            evals_version="1.0.0",
            git_commit="a" * 40,
            prompts_sha256="b" * 64,
            evals_sha256="c" * 64,
            tools_version="0.9.0",
            tools_sha256="d" * 64,
            observability_version="0.8.0",
            observability_sha256="e" * 64,
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
