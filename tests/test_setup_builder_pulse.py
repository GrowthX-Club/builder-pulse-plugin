from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("setup_builder_pulse", ROOT / "scripts" / "setup_builder_pulse.py")
assert SPEC is not None and SPEC.loader is not None
S = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)

COMMIT = "e" * 40
TOKEN = "a" * 64


def claimed_identity(installation: str = "11111111-2222-4333-8444-555555555555") -> dict:
    return {
        "installationId": installation,
        "installationToken": TOKEN,
        "builderId": "builder-1",
        "memberId": "member-1",
        "builderName": "Harness Member",
        "claimedEndpoint": "https://pulse.example",
        "promptCapture": "on",
    }


class FakeCommands:
    """Records every command the installer runs and answers from a script."""

    def __init__(self, case: "SetupCase") -> None:
        self.case = case
        self.calls: list[list[str]] = []
        self.codex_installed: dict | None = None
        self.codex_marketplace: dict | None = None
        self.claude_plugins: list[dict] = []
        self.claude_marketplaces: list[dict] = []
        self.fail_on: dict[str, str] = {}
        self.install_versions: list[str] = []

    def __call__(self, arguments, *, env=None, expect_json=False):
        argv = [str(a) for a in arguments]
        self.calls.append(argv)
        key = " ".join(argv)
        for needle, message in self.fail_on.items():
            if needle in key:
                raise S.SetupError(message)
        if argv[0] == "codex":
            return self.codex(argv, expect_json)
        if argv[0] == "claude":
            return self.claude(argv, expect_json)
        if str(self.case.cli_path) in argv:
            return self.plugin_cli(argv, env or {}, expect_json)
        return "" if not expect_json else {}

    def codex(self, argv, expect_json):
        if argv[1:3] == ["plugin", "list"]:
            return {"installed": [self.codex_installed] if self.codex_installed else [], "available": []}
        if argv[1:4] == ["plugin", "marketplace", "list"]:
            return {"marketplaces": [self.codex_marketplace] if self.codex_marketplace else []}
        if argv[1:4] == ["plugin", "marketplace", "add"]:
            ref = argv[argv.index("--ref") + 1]
            self.codex_marketplace = {"name": S.MARKETPLACE, "root": "/m", "marketplaceSource": {"sourceType": "git", "source": S.REPOSITORY, "ref": ref}}
            return ""
        if argv[1:4] == ["plugin", "marketplace", "remove"]:
            self.codex_marketplace = None
            return ""
        if argv[1:3] == ["plugin", "remove"]:
            self.codex_installed = None
            return ""
        if argv[1:3] == ["plugin", "add"]:
            ref = (self.codex_marketplace or {}).get("marketplaceSource", {}).get("ref", "v0.0.0")
            version = ref.removeprefix("v")
            root = self.case.codex_home / "plugins" / "cache" / S.MARKETPLACE / "builder-pulse" / version
            (root / "scripts").mkdir(parents=True, exist_ok=True)
            (root / "scripts" / "builder_pulse.py").write_text("# runtime\n")
            (root / ".codex-plugin").mkdir(exist_ok=True)
            (root / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "builder-pulse", "version": version}))
            self.codex_installed = {"pluginId": S.PLUGIN, "version": version}  # the real list omits installedPath
            self.install_versions.append(version)
            self.case.cli_path = root / "scripts" / "builder_pulse.py"
            return {"installedPath": str(root), "version": version}
        raise AssertionError(f"unexpected codex call {argv}")

    def claude(self, argv, expect_json):
        if argv[1:3] == ["plugin", "list"]:
            return list(self.claude_plugins)
        if argv[1:4] == ["plugin", "marketplace", "list"]:
            return list(self.claude_marketplaces)
        if argv[1:4] == ["plugin", "marketplace", "remove"]:
            self.claude_marketplaces = [m for m in self.claude_marketplaces if m["name"] != argv[4]]
            self.claude_plugins = [p for p in self.claude_plugins if not p["id"].endswith("@" + argv[4])]
            return ""
        if argv[1:4] == ["plugin", "marketplace", "add"]:
            self.claude_marketplaces.append({"name": S.CLAUDE_MARKETPLACE, "source": "github", "repo": S.REPOSITORY_SLUG, "ref": S.TARGET_RELEASE})
            return ""
        if argv[1:3] in (["plugin", "install"], ["plugin", "update"]):
            plugin_name = S.claude_plugin_id().split("@", 1)[0]  # posix or windows, as the installer expects on this platform
            root = self.case.home / ".claude" / "plugins" / "cache" / S.CLAUDE_MARKETPLACE / plugin_name / S.TARGET_VERSION
            (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
            (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": plugin_name, "version": S.TARGET_VERSION}))
            (root / ".in_use").mkdir(exist_ok=True)
            (root / ".in_use" / "4242").write_text("")
            self.claude_plugins = [p for p in self.claude_plugins if p["id"] != argv[3]]
            self.claude_plugins.append({"id": argv[3], "version": S.TARGET_VERSION, "scope": "user", "enabled": True, "installPath": str(root)})
            return ""
        if argv[1:3] == ["plugin", "uninstall"]:
            self.claude_plugins = [p for p in self.claude_plugins if p["id"] != argv[3]]
            return ""
        raise AssertionError(f"unexpected claude call {argv}")

    def plugin_cli(self, argv, env, expect_json):
        command = argv[argv.index(str(self.case.cli_path)) + 1:]
        data = self.case.data_dir
        if command[:3] == ["config", "set", "enabled"]:
            config = S.read_object(data / "config.json")
            config["enabled"] = command[3] == "true"
            S.write_object(data / "config.json", config)
            return ""
        if command[:1] == ["claim"]:
            self.case.claim_env = env.get("BUILDER_PULSE_INVITE_CODE")
            S.write_object(data / "identity.json", claimed_identity("fresh-0000-4000-8000-000000000000"))
            return ""
        if command[:2] == ["status", "--json"]:
            identity = S.read_object(data / "identity.json")
            return {"identity": {**{k: identity.get(k) for k in ("installationId", "builderId", "memberId")},
                                 "claimed": bool(identity.get("installationToken")), "tokenConfigured": bool(identity.get("installationToken"))}}
        if command[:2] == ["work", "enroll"]:
            contexts = S.read_object(data / "contexts.json")
            contexts[command[command.index("--project") + 1]] = "enrolled"
            S.write_object(data / "contexts.json", contexts)
            return ""
        if command[:2] == ["work", "list"]:
            return json.dumps({"projects": list(S.read_object(data / "contexts.json"))})
        if command[:1] == ["flush"]:
            return "{}"
        raise AssertionError(f"unexpected plugin cli call {command}")


class SetupCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.codex_home = self.home / ".codex"
        self.data_dir = self.home / ".builder-pulse"
        self.legacy_dir = self.codex_home / "plugins" / "data" / f"builder-pulse-{S.MARKETPLACE}"
        self.project = self.home / "Projects" / "app"
        self.project.mkdir(parents=True)
        self.cli_path = self.home / "runtime" / "builder_pulse.py"
        self.claim_env: str | None = None
        patches = [
            mock.patch.dict(os.environ, {"BUILDER_PULSE_DATA_DIR": str(self.data_dir), "CODEX_HOME": str(self.codex_home), "HOME": str(self.home)}),
            mock.patch.object(Path, "home", return_value=self.home),
            mock.patch.object(S, "LOG", S.SetupLog()),
            mock.patch.object(S, "temporary_roots", return_value=(self.home / "tmp",)),
            mock.patch.object(S, "DESKTOP_CODEX", self.home / "no-desktop-codex"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.commands = FakeCommands(self)
        self.server_calls: list[tuple[str, str]] = []
        for guard in (mock.patch.object(S, "run_command", self.commands),
                      mock.patch.object(S.subprocess, "run", side_effect=AssertionError("test reached a real subprocess")),
                      mock.patch.object(S.urlrequest, "urlopen", side_effect=AssertionError("test reached the network"))):
            guard.start()
            self.addCleanup(guard.stop)

    def fake_server_call(self, identity, route, version, confirm):
        self.server_calls.append((route, version))

    def run_setup(self, *, repair=False, invite="harness-invite-code-0001-abcdef", project=True, codex=True, claude=False,
                  activation=None, which=None):
        which = which or (lambda name: {"git": "/usr/bin/git", "codex": "/usr/bin/codex" if codex else None, "claude": "/usr/bin/claude" if claude else None}.get(name))
        activation = activation or (lambda cli_path, agent: {"activationReady": True, "serverVerified": True, "hooksVerified": True})
        with (mock.patch.object(S.shutil, "which", side_effect=which),
              mock.patch.object(S, "run_command", self.commands),
              mock.patch.object(S, "verify_release", return_value=COMMIT),
              mock.patch.object(S, "verify_installer_checkout", return_value=ROOT),
              mock.patch.object(S, "install_shared_runtime", return_value=self.cli_path),
              mock.patch.object(S, "server_call", side_effect=self.fake_server_call),
              mock.patch.object(S, "activate", side_effect=activation)):
            return S.setup("" if repair else invite, "https://pulse.example",
                           str(self.project) if project else "", "App" if project else "", repair=repair)

    def seed_codex(self, version="0.4.5"):
        root = self.codex_home / "plugins" / "cache" / S.MARKETPLACE / "builder-pulse" / version
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "builder_pulse.py").write_text("# old runtime\n")
        (root / ".codex-plugin").mkdir()
        (root / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "builder-pulse", "version": version}))
        self.commands.codex_installed = {"pluginId": S.PLUGIN, "version": version}
        self.commands.codex_marketplace = {"name": S.MARKETPLACE, "root": "/m", "marketplaceSource": {"sourceType": "git", "source": S.REPOSITORY, "ref": f"v{version}"}}

    def write_identity(self, directory: Path, identity: dict, name="identity.json"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(json.dumps(identity))


# ----------------------------------------------------------------- helpers


class HelperTests(SetupCase):
    def test_pristine_checkout_allows_only_tool_noise(self) -> None:
        self.assertTrue(S.checkout_is_pristine("?? .codex-marketplace-install.json\n!! scripts/__pycache__/x.pyc\n?? .DS_Store\n?? .in_use/99\n?? .orphaned_at\n?? .links_materialized\n"))
        for dirty in (" M scripts/builder_pulse.py\n", "?? urllib/\n", "!! json.py\n", "D  hooks/hooks.json\n",
                      "?? claude-plugins/posix/.in_use/99\n", "?? .in_use/notes.txt\n", "?? scripts/.orphaned_at\n"):
            self.assertFalse(S.checkout_is_pristine(dirty), dirty)

    def test_redaction_hides_secrets_home_and_project_paths(self) -> None:
        S.LOG.mask("harness-invite-code-0001-abcdef")
        text = S.LOG.redact(f"Bearer {TOKEN} inviteCode: harness-invite-code-0001-abcdef installationToken=\"{TOKEN}\" {self.home}/Projects/app")
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("harness-invite-code", text)
        self.assertNotIn(str(self.home), text)
        self.assertIn("~/Projects/app", text)
        self.assertEqual(S.display_arguments(["x", "--code", "secret-value", "--project-root", str(self.project), "--root=/a/b"]),
                         ["x", "--code", "[redacted]", "--project-root", "…/app", "--root=…/b"])

    def test_log_file_is_private_redacted_and_pruned(self) -> None:
        S.LOG.mask("harness-invite-code-0001-abcdef")
        S.LOG.write("before open", token=TOKEN)
        logs = self.data_dir / "logs"
        logs.mkdir(parents=True)
        for index in range(12):
            (logs / f"setup-2026010{index // 10}-00000{index % 10}.log").write_text("old\n")
        path = S.LOG.open(self.data_dir)
        assert path is not None
        S.LOG.write("after open", root=str(self.project), invite="harness-invite-code-0001-abcdef")
        content = path.read_text()
        self.assertNotIn(TOKEN, content)
        self.assertNotIn("harness-invite-code", content)
        self.assertNotIn(str(self.home), content)
        self.assertIn("before open", content)
        if os.name != "nt":  # chmod is a no-op on Windows
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertLessEqual(len(list(logs.glob("setup-*.log"))), 10)

    def test_approved_sources(self) -> None:
        self.assertEqual(S.approved_slug("https://github.com/udayanwalvekar/builder-pulse-plugin.git"), "udayanwalvekar/builder-pulse-plugin")
        self.assertEqual(S.approved_slug(S.REPOSITORY), S.REPOSITORY_SLUG)
        self.assertIsNone(S.approved_slug("https://github.com/evil/builder-pulse-plugin.git"))
        self.assertIsNone(S.approved_slug(None))

    def test_codex_desktop_fallback_only_when_path_has_no_codex(self) -> None:
        with mock.patch.object(S.shutil, "which", return_value="/usr/local/bin/codex"):
            self.assertEqual(S.codex_executable(), "/usr/local/bin/codex")
        desktop = self.home / "ChatGPT.app" / "Contents" / "Resources" / "codex"
        desktop.parent.mkdir(parents=True)
        desktop.write_text("#!/bin/sh\n")
        with (mock.patch.object(S.shutil, "which", return_value=None), mock.patch.object(S, "DESKTOP_CODEX", desktop),
              mock.patch.object(S.sys, "platform", "darwin"), mock.patch.object(S.subprocess, "run") as run):
            self.assertEqual(S.codex_executable(), str(desktop))
            self.assertIn(str(desktop.parent), S.tool_environment()["PATH"].split(os.pathsep)[0])
            run.side_effect = OSError("no")
            self.assertIsNone(S.codex_executable())
        with (mock.patch.object(S.shutil, "which", return_value=None), mock.patch.object(S, "DESKTOP_CODEX", desktop),
              mock.patch.object(S.sys, "platform", "linux")):
            self.assertIsNone(S.codex_executable())

    def test_windows_plugin_id_and_argument_display(self) -> None:
        cli = Path("C:/x/builder_pulse.py")  # built before os.name is patched: 3.11 refuses WindowsPath on POSIX
        with mock.patch.object(S.os, "name", "nt"):
            self.assertEqual(S.claude_plugin_id(), f"builder-pulse-claude-windows@{S.CLAUDE_MARKETPLACE}")
            self.assertIn("py -3", S.hook_review_message("codex", cli, None))
        self.assertEqual(S.display_arguments(["--root", "C:\\Users\\m\\proj"]), ["--root", "…/proj"])


class ReleaseTests(SetupCase):
    def release_response(self, body, status=200):
        raw = json.dumps(body).encode()
        response = mock.MagicMock()
        response.read.return_value = raw
        response.__enter__.return_value = response
        return response

    def test_release_requires_tag_and_published_immutable_release(self) -> None:
        listing = f"{'b' * 40}\trefs/tags/v0.6.0\n{COMMIT}\trefs/tags/v0.6.0^{{}}\n"
        with (mock.patch.object(S, "run_command", return_value=listing),
              mock.patch.object(S.urlrequest, "urlopen", return_value=self.release_response({"tag_name": "v0.6.0", "draft": False, "immutable": True}))):
            self.assertEqual(S.verify_release("v0.6.0"), COMMIT)
        for body in ({"tag_name": "v0.6.0", "draft": True, "immutable": True}, {"tag_name": "v0.6.0", "draft": False, "immutable": False}, {"tag_name": "v0.5.9", "draft": False, "immutable": True}):
            with (mock.patch.object(S, "run_command", return_value=listing),
                  mock.patch.object(S.urlrequest, "urlopen", return_value=self.release_response(body)),
                  self.assertRaisesRegex(S.SetupError, "published immutable")):
                S.verify_release("v0.6.0")
        with (mock.patch.object(S, "run_command", return_value="garbage\n"), self.assertRaisesRegex(S.SetupError, "invalid")):
            S.verify_release("v0.6.0")

    def test_rate_limit_is_explained(self) -> None:
        listing = f"{COMMIT}\trefs/tags/v0.6.0\n"
        error = S.urlerror.HTTPError("u", 403, "rate", {}, None)
        with (mock.patch.object(S, "run_command", return_value=listing), mock.patch.object(S.urlrequest, "urlopen", side_effect=error),
              self.assertRaisesRegex(S.SetupError, "rate limit")):
            S.verify_release("v0.6.0")

    def test_installer_checkout_must_match_the_release(self) -> None:
        def answers(status, head=COMMIT, origin=S.REPOSITORY):
            return lambda argv, **_: {"remote": origin, "rev-parse": head, "status": status}[[a for a in argv if a in ("remote", "rev-parse", "status")][0]]
        with mock.patch.object(S, "run_command", side_effect=answers("?? .DS_Store\n")):
            self.assertEqual(S.verify_installer_checkout(COMMIT), ROOT)
        for status, head, origin in ((" M scripts/setup_builder_pulse.py\n", COMMIT, S.REPOSITORY), ("", "f" * 40, S.REPOSITORY), ("", COMMIT, "https://github.com/evil/x.git")):
            with mock.patch.object(S, "run_command", side_effect=answers(status, head, origin)), self.assertRaisesRegex(S.SetupError, "does not match"):
                S.verify_installer_checkout(COMMIT)


# -------------------------------------------------------------- data and identity


class DataTests(SetupCase):
    def test_migration_copies_legacy_once_and_never_touches_a_claim(self) -> None:
        self.write_identity(self.legacy_dir, claimed_identity())
        (self.legacy_dir / "contexts.json").write_text('{"p": 1}')
        S.migrate_legacy_data(self.data_dir, self.legacy_dir)
        self.assertEqual(S.read_object(self.data_dir / "identity.json"), claimed_identity())
        self.assertTrue((self.data_dir / "contexts.json").is_file())
        self.assertTrue((self.legacy_dir / "identity.json").is_file(), "legacy copy is never deleted")
        # a claimed shared directory is left alone even when legacy differs
        other = claimed_identity("99999999-2222-4333-8444-555555555555")
        self.write_identity(self.legacy_dir, other)
        S.migrate_legacy_data(self.data_dir, self.legacy_dir)
        self.assertEqual(S.read_object(self.data_dir / "identity.json"), claimed_identity())

    def test_migration_merges_over_skeleton_runtime_and_logs(self) -> None:
        self.write_identity(self.legacy_dir, claimed_identity())
        (self.legacy_dir / "config.json").write_text('{"enabled": false, "endpoint": "https://pulse.example"}')
        self.write_identity(self.data_dir, {"installationId": "8c8006e7-0000-4000-8000-000000000001", "promptCapture": "off"})
        self.write_identity(self.data_dir, {"installationId": "8c8006e7-0000-4000-8000-000000000001"}, "setup-paused-identity.json")
        (self.data_dir / "config.json").write_text('{"enabled": false}')
        (self.data_dir / "runtime" / "0.5.0" / "scripts").mkdir(parents=True)
        (self.data_dir / "runtime" / "0.5.0" / "scripts" / "builder_pulse.py").write_text("# stale\n")
        (self.data_dir / "logs").mkdir()
        (self.data_dir / "logs" / "setup-old.log").write_text("x\n")
        (self.data_dir / ".lock").write_bytes(b"")
        S.migrate_legacy_data(self.data_dir, self.legacy_dir)
        self.assertEqual(S.read_object(self.data_dir / "identity.json"), claimed_identity())
        self.assertFalse((self.data_dir / "setup-paused-identity.json").exists())
        self.assertEqual(S.read_object(self.data_dir / "config.json")["endpoint"], "https://pulse.example")
        self.assertTrue((self.data_dir / "runtime" / "0.5.0" / "scripts" / "builder_pulse.py").is_file())
        self.assertTrue((self.data_dir / "logs" / "setup-old.log").is_file())
        self.assertFalse((self.data_dir.parent / f".{self.data_dir.name}-replaced").exists())

    def test_migration_failure_leaves_target_untouched_and_retry_completes(self) -> None:
        self.write_identity(self.legacy_dir, claimed_identity())
        (self.data_dir / "logs").mkdir(parents=True)
        original = shutil.copytree
        failed = {"once": False}

        def flaky(src, dst, *args, **kwargs):
            if Path(src) == self.legacy_dir and not failed["once"]:
                failed["once"] = True
                raise OSError(28, "No space left on device")
            return original(src, dst, *args, **kwargs)

        with mock.patch.object(S.shutil, "copytree", side_effect=flaky), self.assertRaisesRegex(S.SetupError, "migrated safely"):
            S.migrate_legacy_data(self.data_dir, self.legacy_dir)
        self.assertFalse((self.data_dir / "identity.json").exists())
        self.assertTrue((self.data_dir / "logs").is_dir())
        S.migrate_legacy_data(self.data_dir, self.legacy_dir)
        self.assertEqual(S.read_object(self.data_dir / "identity.json"), claimed_identity())

    def test_leftover_migration_directories_fail_closed_and_are_never_deleted(self) -> None:
        self.write_identity(self.legacy_dir, claimed_identity())
        aside = self.data_dir.parent / f".{self.data_dir.name}-replaced"
        aside.mkdir(parents=True)
        (aside / "identity.json").write_text("{}")
        with self.assertRaisesRegex(S.SetupError, "still exists"):
            S.migrate_legacy_data(self.data_dir, self.legacy_dir)
        self.assertTrue((aside / "identity.json").is_file())
        self.assertFalse(self.data_dir.exists())

    def test_unparseable_identity_fails_closed(self) -> None:
        self.data_dir.mkdir(parents=True)
        (self.data_dir / "identity.json").write_text("{not json")
        with self.assertRaisesRegex(S.SetupError, "data is invalid"):
            S.holds_claim(self.data_dir)

    def test_symlinks_in_data_are_refused(self) -> None:
        self.write_identity(self.legacy_dir, claimed_identity())
        (self.legacy_dir / "link").symlink_to(self.legacy_dir / "identity.json")
        with self.assertRaisesRegex(S.SetupError, "symbolic link"):
            S.migrate_legacy_data(self.data_dir, self.legacy_dir)

    def test_paused_identity_wins_and_must_match(self) -> None:
        identity = claimed_identity()
        self.write_identity(self.data_dir, {k: v for k, v in identity.items() if k != "installationToken"})
        self.write_identity(self.data_dir, identity, "setup-paused-identity.json")
        self.assertEqual(S.current_identity(self.data_dir), identity)
        self.write_identity(self.data_dir, claimed_identity("other-0000-4000-8000-000000000000"))
        with self.assertRaisesRegex(S.SetupError, "does not match"):
            S.current_identity(self.data_dir)

    def test_pause_and_restore_round_trip(self) -> None:
        identity = claimed_identity()
        self.write_identity(self.data_dir, identity)
        (self.data_dir / "outbox.jsonl").write_text("{}\n")
        S.pause_local(self.data_dir, identity)
        stripped = S.read_object(self.data_dir / "identity.json")
        self.assertNotIn("installationToken", stripped)
        self.assertEqual(stripped["promptCapture"], "off")
        self.assertFalse(S.read_object(self.data_dir / "config.json")["enabled"])
        self.assertFalse((self.data_dir / "outbox.jsonl").exists())
        self.assertEqual(S.current_identity(self.data_dir), identity)
        S.restore_identity(self.data_dir, identity)
        self.assertEqual(S.read_object(self.data_dir / "identity.json"), identity)
        self.assertFalse((self.data_dir / "setup-paused-identity.json").exists())
        S.pause_local(self.data_dir, identity)
        with self.assertRaisesRegex(S.SetupError, "changed during setup"):
            S.restore_identity(self.data_dir, claimed_identity("other-0000-4000-8000-000000000000"))

    def test_claim_requirements(self) -> None:
        self.assertEqual(S.require_claimed(claimed_identity())["memberId"], "member-1")
        for broken in ({}, {"installationId": "x", "promptCapture": "off"}, {**claimed_identity(), "installationToken": ""}, {**claimed_identity(), "claimedEndpoint": None}):
            with self.assertRaisesRegex(S.SetupError, "not fully claimed"):
                S.require_claimed(broken)
        self.write_identity(self.data_dir, {"installationId": "skeleton", "promptCapture": "off"})
        self.assertFalse(S.holds_claim(self.data_dir))
        self.write_identity(self.data_dir, {"installationId": "p", "pendingInstallationToken": TOKEN}, "setup-paused-identity.json")
        self.assertTrue(S.holds_claim(self.data_dir))

    def test_server_call_confirms_the_exact_installation(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"paused": True, "installationId": claimed_identity()["installationId"]}).encode()
        with mock.patch.object(S.urlrequest, "urlopen", return_value=response) as opened:
            S.server_call(claimed_identity(), "privacy-pause", "0.4.5", "paused")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://pulse.example/v1/privacy-pause")
        self.assertEqual(json.loads(request.data), {"installationId": claimed_identity()["installationId"], "pluginVersion": "0.4.5"})
        response.read.return_value = json.dumps({"paused": True, "installationId": "other"}).encode()
        with mock.patch.object(S.urlrequest, "urlopen", return_value=response), self.assertRaisesRegex(S.SetupError, "did not confirm"):
            S.server_call(claimed_identity(), "privacy-pause", "0.4.5", "paused")
        with mock.patch.object(S.urlrequest, "urlopen", side_effect=OSError("down")), self.assertRaisesRegex(S.SetupError, "could not resume"):
            S.server_call(claimed_identity(), "privacy-resume", "0.6.0", "resumed")
        with self.assertRaisesRegex(S.SetupError, "incomplete"):
            S.server_call({"installationId": "x"}, "privacy-pause", "0.6.0", "paused")


# ---------------------------------------------------------------- packages


class PackageTests(SetupCase):
    def test_codex_install_replaces_only_when_ref_or_version_differ(self) -> None:
        with mock.patch.object(S, "run_command", self.commands):
            self.seed_codex("0.4.5")
            cli_path = S.install_codex(S.TARGET_RELEASE, S.TARGET_VERSION)
        joined = [" ".join(c[1:4]) for c in self.commands.calls]
        self.assertIn("plugin remove " + S.PLUGIN, joined)
        self.assertIn("plugin marketplace remove", joined)
        self.assertIn("plugin marketplace add", joined)
        self.assertTrue(cli_path.is_file())
        self.commands.calls.clear()
        with mock.patch.object(S, "run_command", self.commands):
            S.install_codex(S.TARGET_RELEASE, S.TARGET_VERSION)
        joined = [" ".join(c[1:4]) for c in self.commands.calls]
        self.assertNotIn("plugin remove " + S.PLUGIN, joined)
        self.assertNotIn("plugin marketplace remove", joined)
        self.assertNotIn("plugin add " + S.PLUGIN, joined, "already at the target version: no churn")
        with mock.patch.object(S, "run_command", self.commands):
            self.assertEqual(S.install_codex(S.TARGET_RELEASE, S.TARGET_VERSION), cli_path)

    def test_codex_install_rejects_foreign_marketplace_source(self) -> None:
        self.commands.codex_marketplace = {"name": S.MARKETPLACE, "marketplaceSource": {"sourceType": "git", "source": "https://github.com/evil/x.git", "ref": "v1"}}
        with mock.patch.object(S, "run_command", self.commands), self.assertRaisesRegex(S.SetupError, "different source"):
            S.install_codex(S.TARGET_RELEASE, S.TARGET_VERSION)

    def test_codex_install_rejects_wrong_version(self) -> None:
        with mock.patch.object(S, "run_command", self.commands), self.assertRaisesRegex(S.SetupError, "unexpected Builder Pulse version"):
            S.install_codex("v0.4.5", S.TARGET_VERSION)

    def test_claude_install_update_and_verification(self) -> None:
        with mock.patch.object(S, "run_command", self.commands):
            S.install_claude([])
        verbs = [c[2] for c in self.commands.calls if c[0] == "claude" and c[1] == "plugin"]
        self.assertEqual(verbs[:3], ["marketplace", "marketplace", "install"])
        self.commands.calls.clear()
        with mock.patch.object(S, "run_command", self.commands):
            S.install_claude(self.commands.claude_plugins)
        self.assertIn("update", [c[2] for c in self.commands.calls])
        self.commands.claude_marketplaces = [{"name": S.CLAUDE_MARKETPLACE, "source": "github", "repo": "evil/x", "ref": S.TARGET_RELEASE}]
        with mock.patch.object(S, "run_command", self.commands), self.assertRaisesRegex(S.SetupError, "different source"):
            S.install_claude([])
        # right repository, wrong pin: re-pinned, and the package installed afresh
        self.commands.claude_marketplaces = [{"name": S.CLAUDE_MARKETPLACE, "source": "github", "repo": S.REPOSITORY_SLUG, "ref": "feat/branch"}]
        self.commands.calls.clear()
        with mock.patch.object(S, "run_command", self.commands):
            S.install_claude(self.commands.claude_plugins)
        verbs = [c[2] if c[2] != "marketplace" else f"marketplace {c[3]}" for c in self.commands.calls if c[0] == "claude" and c[1] == "plugin"]
        self.assertEqual(verbs[:4], ["marketplace list", "marketplace remove", "marketplace add", "install"])

    def test_claude_version_mismatch_is_rejected(self) -> None:
        original = self.commands.claude
        def wrong(argv, expect_json):
            result = original(argv, expect_json)
            if argv[1:3] == ["plugin", "install"]:
                self.commands.claude_plugins[-1]["version"] = "0.0.1"
            return result
        self.commands.claude = wrong
        with mock.patch.object(S, "run_command", self.commands), self.assertRaisesRegex(S.SetupError, "expected Builder Pulse version"):
            S.install_claude([])

    def test_shared_runtime_is_copied_atomically_and_idempotently(self) -> None:
        cli_path = S.install_shared_runtime(ROOT)
        self.assertEqual(cli_path.read_bytes(), (ROOT / "scripts" / "builder_pulse.py").read_bytes())
        self.assertEqual(S.read_object(cli_path.parent.parent / ".codex-plugin" / "plugin.json")["version"], S.TARGET_VERSION)
        cli_path.write_text("tampered\n")
        self.assertEqual(S.install_shared_runtime(ROOT).read_bytes(), (ROOT / "scripts" / "builder_pulse.py").read_bytes())

    def test_activate_results(self) -> None:
        def completed(rc, stdout, stderr=""):
            return mock.Mock(returncode=rc, stdout=stdout, stderr=stderr)
        with mock.patch.object(S.subprocess, "run", return_value=completed(0, '{"activationReady": true, "serverVerified": true}')):
            self.assertTrue(S.activate(self.cli_path, "codex")["activationReady"])
        with mock.patch.object(S.subprocess, "run", return_value=completed(3, '{"reviewRequired": true, "hookStatus": "review_required"}')):
            self.assertTrue(S.activate(self.cli_path, "codex")["reviewRequired"])
        with (mock.patch.object(S.subprocess, "run", return_value=completed(3, '{"reviewRequired": false, "hookStatus": "not_loaded", "detail": "plugin ids seen: none"}')),
              self.assertRaisesRegex(S.SetupError, r"activation failed for codex \(hookStatus=not_loaded; plugin ids seen: none\)")):
            S.activate(self.cli_path, "codex")
        with (mock.patch.object(S.subprocess, "run", return_value=completed(1, "", f"Activation failed: unauthorized Bearer {TOKEN}")),
              self.assertRaisesRegex(S.SetupError, "unauthorized Bearer \\[redacted\\]")):
            S.activate(self.cli_path, "codex")
        with mock.patch.object(S.subprocess, "run", side_effect=OSError("nope")), self.assertRaisesRegex(S.SetupError, "could not start"):
            S.activate(self.cli_path, "codex")


# -------------------------------------------------------------- enrollment


class EnrollmentTests(SetupCase):
    def test_refusals(self) -> None:
        self.assertIsNone(S.enrollment_refusal(self.project))
        self.assertIn("home", S.enrollment_refusal(self.home))
        self.assertIn("home", S.enrollment_refusal(self.home.parent))
        self.assertIn("filesystem root", S.enrollment_refusal(Path(self.home.anchor)))
        self.assertIn("temporary folder", S.enrollment_refusal(self.home / "tmp" / "proj"))
        self.assertTrue(any(str(p).startswith(str(Path(tempfile.gettempdir()).resolve())) for p in S.temporary_roots.__wrapped__()) if hasattr(S.temporary_roots, "__wrapped__") else True)
        clone = self.home / "clone"
        (clone / "scripts").mkdir(parents=True)
        (clone / "scripts" / "setup_builder_pulse.py").write_text("#\n")
        self.assertIn("installer folder", S.enrollment_refusal(clone / "sub"))
        with self.assertRaisesRegex(S.SetupError, "does not exist"):
            S.validated_project(str(self.home / "missing"), "App")
        with self.assertRaisesRegex(S.SetupError, "project name is invalid"):
            S.validated_project(str(self.project), " ")
        self.assertIsNone(S.validated_project("", ""))
        self.assertEqual(S.validated_project(str(self.project), "App"), (self.project, "App"))


# ------------------------------------------------------------------- setup


class SetupFlowTests(SetupCase):
    def test_fresh_setup_claims_enrolls_resumes_and_activates(self) -> None:
        outcome = self.run_setup()
        self.assertEqual(outcome.review_required, ())
        self.assertEqual(outcome.enrolled, self.project)
        self.assertEqual(self.claim_env, "harness-invite-code-0001-abcdef")
        self.assertEqual(self.server_calls, [("privacy-resume", S.TARGET_VERSION)], "no token before the claim, so no pause")
        self.assertTrue(S.read_object(self.data_dir / "config.json")["enabled"])
        self.assertIn("App", S.read_object(self.data_dir / "contexts.json"))
        self.assertEqual(self.commands.install_versions, [S.TARGET_VERSION])
        self.assertTrue(any(c[-1] == "flush" for c in self.commands.calls))

    def test_upgrade_pauses_with_old_version_and_keeps_identity(self) -> None:
        self.seed_codex("0.4.5")
        self.write_identity(self.legacy_dir, claimed_identity())
        outcome = self.run_setup()
        self.assertEqual(self.server_calls, [("privacy-pause", "0.4.5"), ("privacy-resume", S.TARGET_VERSION)])
        self.assertEqual(S.read_object(self.data_dir / "identity.json")["installationToken"], TOKEN)
        self.assertEqual(self.claim_env, "harness-invite-code-0001-abcdef", "setup mode re-claims with the new invite; the server reuses the installation")
        self.assertEqual(outcome.cli.parent.parent.name, S.TARGET_VERSION)
        removed = [c for c in self.commands.calls if c[1:3] == ["plugin", "remove"]]
        self.assertEqual(len(removed), 1)

    def test_repair_reuses_identity_and_never_enrolls_or_claims(self) -> None:
        self.write_identity(self.data_dir, claimed_identity())
        outcome = self.run_setup(repair=True, project=False)
        self.assertIsNone(outcome.enrolled)
        self.assertIsNone(self.claim_env)
        self.assertFalse(any(c[-2:] == ["work", "enroll"] or "enroll" in c for c in self.commands.calls))
        self.assertEqual(S.read_object(self.data_dir / "identity.json"), claimed_identity())
        self.assertEqual([r for r, _ in self.server_calls], ["privacy-pause", "privacy-resume"])

    def test_repair_from_quarantined_legacy_only_and_skeleton_states(self) -> None:
        identity = claimed_identity()
        self.write_identity(self.legacy_dir, {k: v for k, v in identity.items() if k != "installationToken"})
        self.write_identity(self.legacy_dir, identity, "setup-paused-identity.json")
        self.write_identity(self.data_dir, {"installationId": "8c8006e7-0000-4000-8000-000000000001", "promptCapture": "off"})
        (self.data_dir / "runtime" / "0.5.0").mkdir(parents=True)
        outcome = self.run_setup(repair=True, project=False)
        self.assertEqual(outcome.review_required, ())
        self.assertEqual(S.read_object(self.data_dir / "identity.json"), identity)
        self.assertFalse((self.data_dir / "setup-paused-identity.json").exists())
        self.assertTrue((self.data_dir / "runtime" / "0.5.0").is_dir())

    def test_repair_without_any_claim_fails_closed_before_mutation(self) -> None:
        self.write_identity(self.data_dir, {"installationId": "skeleton", "promptCapture": "off"})
        with self.assertRaisesRegex(S.SetupError, "not fully claimed"):
            self.run_setup(repair=True, project=False)
        self.assertEqual(self.server_calls, [])
        self.assertFalse(any(c[0] in {"codex", "claude"} and c[2] in {"add", "remove"} for c in self.commands.calls))

    def test_repair_refuses_a_changed_identity(self) -> None:
        self.write_identity(self.data_dir, claimed_identity())
        original = self.commands.plugin_cli
        def swap(argv, env, expect_json):
            result = original(argv, env, expect_json)
            if "status" in argv and isinstance(result, dict):
                result["identity"]["memberId"] = "someone-else"
            return result
        self.commands.plugin_cli = swap
        with self.assertRaisesRegex(S.SetupError, "identity changed during repair"):
            self.run_setup(repair=True, project=False)
        self.assertEqual([r for r, _ in self.server_calls], ["privacy-pause"], "still paused from before; no resume happened")
        self.assertFalse(S.read_object(self.data_dir / "config.json")["enabled"])

    def test_review_required_is_a_state_not_a_failure(self) -> None:
        self.seed_codex("0.4.6")
        self.write_identity(self.data_dir, claimed_identity())
        outcome = self.run_setup(repair=True, project=False, activation=lambda c, a: {"reviewRequired": True, "hookStatus": "review_required"})
        self.assertEqual(outcome.review_required, ("codex",))
        self.assertTrue(S.read_object(self.data_dir / "config.json")["enabled"])
        self.assertEqual(S.read_object(self.data_dir / "identity.json")["installationToken"], TOKEN)
        self.assertEqual([r for r, _ in self.server_calls], ["privacy-pause", "privacy-resume"])
        self.assertEqual(self.commands.install_versions, [S.TARGET_VERSION], "no rollback")

    def test_activation_failure_after_resume_repauses_and_restores_previous_tag(self) -> None:
        self.seed_codex("0.4.5")
        self.write_identity(self.data_dir, claimed_identity())
        def failing(cli_path, agent):
            raise S.SetupError("Builder Pulse activation failed for codex (hookStatus=not_loaded)")
        with self.assertRaisesRegex(S.SetupError, "hookStatus=not_loaded"):
            self.run_setup(repair=True, project=False, activation=failing)
        self.assertEqual([r for r, _ in self.server_calls], ["privacy-pause", "privacy-resume", "privacy-pause"])
        self.assertFalse(S.read_object(self.data_dir / "config.json")["enabled"])
        self.assertNotIn("installationToken", S.read_object(self.data_dir / "identity.json"))
        self.assertEqual(S.read_object(self.data_dir / "setup-paused-identity.json")["installationToken"], TOKEN)
        self.assertEqual(self.commands.install_versions, [S.TARGET_VERSION, "0.4.5"])
        self.assertEqual(self.commands.codex_installed["version"], "0.4.5")

    def test_codex_install_failure_restores_previous_tag_and_leaves_paused(self) -> None:
        self.seed_codex("0.4.5")
        self.write_identity(self.data_dir, claimed_identity())
        original = self.commands.codex
        state = {"adds": 0}
        def flaky(argv, expect_json):
            if argv[1:3] == ["plugin", "add"]:
                state["adds"] += 1
                if state["adds"] == 1:
                    raise S.SetupError("network down")
            return original(argv, expect_json)
        self.commands.codex = flaky
        with self.assertRaisesRegex(S.SetupError, "network down"):
            self.run_setup(repair=True, project=False)
        self.assertEqual(self.commands.codex_installed["version"], "0.4.5")
        self.assertEqual(self.server_calls, [("privacy-pause", "0.4.5")], "already paused; not resumed")
        self.assertFalse(S.read_object(self.data_dir / "config.json")["enabled"])

    def test_rollback_problems_are_reported_not_hidden(self) -> None:
        self.seed_codex("0.4.5")
        self.write_identity(self.data_dir, claimed_identity())
        self.commands.fail_on["plugin marketplace add GrowthX-Club/builder-pulse-plugin --ref v0.4.5"] = "github unreachable"
        def failing(cli_path, agent):
            raise S.SetupError("activation failed")
        with self.assertRaisesRegex(S.SetupError, "activation failed; previous Codex package v0.4.5 not restored"):
            self.run_setup(repair=True, project=False, activation=failing)

    def test_both_agents_activate_and_old_claude_plugin_is_removed_last(self) -> None:
        self.commands.claude_plugins = [{"id": "builder-pulse-claude-posix@growthx-builder-tools-v0-5-3", "version": "0.5.3", "scope": "user", "enabled": True, "installPath": "/old"}]
        self.write_identity(self.data_dir, claimed_identity())
        activated: list[str] = []
        def activation(cli_path, agent):
            activated.append(agent)
            return {"activationReady": True, "serverVerified": True}
        self.run_setup(repair=True, project=False, claude=True, activation=activation)
        self.assertEqual(activated, ["codex", "claude_code"])
        ids = [p["id"] for p in self.commands.claude_plugins]
        self.assertEqual(ids, [S.claude_plugin_id()])
        uninstall_index = next(i for i, c in enumerate(self.commands.calls) if c[1:3] == ["plugin", "uninstall"])
        flush_index = next(i for i, c in enumerate(self.commands.calls) if c[-1] == "flush")
        self.assertGreater(uninstall_index, flush_index)

    def test_claude_only_setup_uses_the_shared_runtime(self) -> None:
        outcome = self.run_setup(codex=False, claude=True)
        self.assertEqual(outcome.cli, self.cli_path)
        self.assertFalse(any(c[0] == "codex" for c in self.commands.calls))

    def test_setup_mode_requires_a_project_and_a_valid_invite(self) -> None:
        with self.assertRaisesRegex(S.SetupError, "project folder is required"):
            self.run_setup(project=False)
        with self.assertRaisesRegex(S.SetupError, "invite code is invalid"):
            self.run_setup(invite="short")
        with self.assertRaisesRegex(S.SetupError, "must not use a new invite"):
            with mock.patch.object(S.shutil, "which", return_value="/usr/bin/x"):
                S.setup("harness-invite-code-0001-abcdef", "https://pulse.example", "", "", repair=True)
        self.assertEqual(self.commands.calls, [])

    def test_no_agent_is_an_error(self) -> None:
        with self.assertRaisesRegex(S.SetupError, "requires Codex, Claude Code, or both"):
            self.run_setup(codex=False, claude=False)


# -------------------------------------------------------------------- main


class MainTests(SetupCase):
    def run_main(self, argv, *, tty=False, outcome=None, error=None, answers=(), invite="harness-invite-code-0001-abcdef"):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (mock.patch.object(sys, "argv", ["setup", *argv]), mock.patch.object(sys.stdin, "isatty", return_value=tty),
              mock.patch.object(S.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")),  # git rev-parse in the folder prompt
              mock.patch("builtins.input", side_effect=list(answers) or EOFError),
              mock.patch.object(S.getpass, "getpass", side_effect=EOFError if invite is None else None, return_value=invite),
              mock.patch.object(S, "setup", side_effect=error, return_value=outcome) as setup_call,
              mock.patch.object(S, "run_command", return_value='{"projects": []}'),
              contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr)):
            code = S.main()
        return code, stdout.getvalue(), stderr.getvalue(), setup_call

    def test_success_prints_sentence_on_stdout_and_details_on_stderr(self) -> None:
        code, out, err, _ = self.run_main(["--reuse-existing-claim"], outcome=S.Outcome(self.cli_path, None, ()))
        self.assertEqual(code, 0)
        self.assertIn("prior confirmed projects were kept unchanged", out)
        self.assertIn("Details: ", err)
        self.assertTrue(Path(err.split("Details: ")[1].strip()).is_file())

    def test_review_required_exits_3_with_hooks_instruction(self) -> None:
        code, out, err, _ = self.run_main(["--reuse-existing-claim"], outcome=S.Outcome(self.cli_path, self.project, ("codex",)))
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("/hooks", err)
        self.assertIn("no rerun of this installer is needed", err)
        self.assertIn("Details: ", err)

    def test_failure_exits_1_with_redacted_message_and_details(self) -> None:
        S.LOG.mask("harness-invite-code-0001-abcdef")
        code, out, err, _ = self.run_main(["--code", "harness-invite-code-0001-abcdef"], error=S.SetupError(f"boom Bearer {TOKEN}"))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Builder Pulse setup stopped: boom Bearer [redacted]", err)
        self.assertNotIn(TOKEN, err)
        log_path = Path(err.split("Details: ")[1].strip())
        self.assertEqual(log_path.parent, self.data_dir / "logs")
        self.assertNotIn(TOKEN, log_path.read_text())
        self.assertNotIn("harness-invite-code", log_path.read_text())

    def test_interactive_repair_defaults_to_no_enrollment(self) -> None:
        code, _, _, setup_call = self.run_main(["--reuse-existing-claim"], tty=True, answers=[""], outcome=S.Outcome(self.cli_path, None, ()))
        self.assertEqual(code, 0)
        self.assertEqual(setup_call.call_args.args[2:4], ("", ""))

    def test_interactive_setup_collects_folder_and_name_in_the_terminal(self) -> None:
        with mock.patch.object(Path, "cwd", return_value=self.project):
            code, _, err, setup_call = self.run_main([], tty=True, answers=["", "My App"], outcome=S.Outcome(self.cli_path, self.project, ()))
        self.assertEqual(code, 0)
        self.assertEqual(setup_call.call_args.args[0], "harness-invite-code-0001-abcdef")
        self.assertEqual(setup_call.call_args.args[2:4], (str(self.project), "My App"))
        self.assertIn("shown only in this terminal", err)

    def test_installer_clone_is_never_the_default_project(self) -> None:
        clone = self.home / "clone"
        (clone / "scripts").mkdir(parents=True)
        (clone / "scripts" / "setup_builder_pulse.py").write_text("#\n")
        with mock.patch.object(Path, "cwd", return_value=clone):
            code, _, err, setup_call = self.run_main([], tty=True, answers=["", str(self.project), "My App"], outcome=S.Outcome(self.cli_path, self.project, ()))
        self.assertEqual(code, 0)
        self.assertIn("installer clone, not your project", err)
        self.assertEqual(setup_call.call_args.args[2], str(self.project))

    def test_closed_terminal_stops_cleanly(self) -> None:
        code, _, err, setup_call = self.run_main(["--reuse-existing-claim"], tty=True)
        self.assertEqual(code, 1)
        setup_call.assert_not_called()
        self.assertIn("terminal closed before the answer was given", err)
        self.assertNotIn("Traceback", err)
        code, _, err, setup_call = self.run_main([], tty=True, invite=None)
        self.assertEqual(code, 1)
        self.assertIn("terminal closed before the invite code", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
