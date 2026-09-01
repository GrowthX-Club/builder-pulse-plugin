from __future__ import annotations

import http.client
import importlib.util
import io
import json
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "setup_builder_pulse", ROOT / "scripts" / "setup_builder_pulse.py"
)
assert SPEC is not None and SPEC.loader is not None
setup_builder_pulse = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup_builder_pulse)

BUILDER_SPEC = importlib.util.spec_from_file_location(
    "builder_pulse_for_setup_test", ROOT / "scripts" / "builder_pulse.py"
)
assert BUILDER_SPEC is not None and BUILDER_SPEC.loader is not None
builder_pulse = importlib.util.module_from_spec(BUILDER_SPEC)
BUILDER_SPEC.loader.exec_module(builder_pulse)

TARGET_COMMIT = "e" * 40
INSTALL_SHARED_RUNTIME = setup_builder_pulse.install_shared_runtime
VERIFIED_INSTALLER_CHECKOUT = setup_builder_pulse.verified_installer_checkout
RUN_COMMAND = setup_builder_pulse.run_command


def codex_only_which(command: str) -> str | None:
    return f"/usr/bin/{command}" if command in {"git", "codex"} else None


def claude_only_which(command: str) -> str | None:
    return f"/usr/bin/{command}" if command in {"git", "claude"} else None


class SetupBuilderPulseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_home = tempfile.TemporaryDirectory()
        root = Path(self.test_home.name)
        self.environment = mock.patch.dict(
            setup_builder_pulse.os.environ,
            {
                "BUILDER_PULSE_DATA_DIR": str(root / ".builder-pulse"),
                "CODEX_HOME": str(root / ".codex"),
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.installer_checkout = mock.patch.object(
            setup_builder_pulse,
            "verified_installer_checkout",
            return_value=ROOT,
        )
        self.installer_checkout.start()
        self.addCleanup(self.installer_checkout.stop)
        self.shared_runtime = mock.patch.object(
            setup_builder_pulse,
            "install_shared_runtime",
            return_value=ROOT / "scripts" / "builder_pulse.py",
        )
        self.shared_runtime.start()
        self.addCleanup(self.shared_runtime.stop)
        self.addCleanup(self.test_home.cleanup)

    def test_standalone_installer_uses_the_canonical_privacy_disclosure(self) -> None:
        self.assertEqual(
            setup_builder_pulse.SETUP_DISCLOSURE,
            builder_pulse.SETUP_DISCLOSURE,
        )

    def test_setup_cli_help_exposes_confirmed_project_arguments(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "setup_builder_pulse.py"), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--project-root", completed.stdout)
        self.assertIn("--project-label", completed.stdout)
        self.assertIn("--reuse-existing-claim", completed.stdout)

    def test_plugin_cli_runs_without_local_or_site_import_shadowing(self) -> None:
        cli = ROOT / "scripts" / "builder_pulse.py"
        self.assertEqual(
            setup_builder_pulse.cli_command(cli, "status", "--json"),
            [sys.executable, "-I", "-S", str(cli), "status", "--json"],
        )

    def test_existing_cli_prefers_codex_reported_install_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Builder Pulse 0.4.4"
            (root / "scripts").mkdir(parents=True)
            cli = root / "scripts" / "builder_pulse.py"
            cli.touch()
            (root / ".codex-plugin").mkdir()
            (root / ".codex-plugin" / "plugin.json").write_text(
                '{"version":"0.4.4"}',
                encoding="utf-8",
            )

            self.assertEqual(
                setup_builder_pulse.installed_cli(
                    {"version": "0.4.4", "installedPath": str(root)}
                ),
                cli.resolve(),
            )

    def test_existing_cli_rejects_reported_path_with_wrong_manifest_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "builder-pulse"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "builder_pulse.py").touch()
            (root / ".codex-plugin").mkdir()
            (root / ".codex-plugin" / "plugin.json").write_text(
                '{"version":"0.4.3"}',
                encoding="utf-8",
            )

            with (
                mock.patch.dict(
                    setup_builder_pulse.os.environ,
                    {"CODEX_HOME": str(Path(directory) / "missing-codex-home")},
                ),
                self.assertRaisesRegex(
                    setup_builder_pulse.SetupError,
                    "could not be located safely",
                ),
            ):
                setup_builder_pulse.installed_cli(
                    {"version": "0.4.4", "installedPath": str(root)}
                )

    def test_pause_quarantines_credentials_even_when_legacy_env_enables_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_root = Path(directory) / "plugins"
            plugin_root = (
                codex_root
                / "cache"
                / setup_builder_pulse.MARKETPLACE
                / "builder-pulse"
                / "0.4.5"
            )
            (plugin_root / "scripts").mkdir(parents=True)
            cli = plugin_root / "scripts" / "builder_pulse.py"
            cli.touch()
            (plugin_root / ".codex-plugin").mkdir()
            (plugin_root / ".codex-plugin" / "plugin.json").write_text(
                '{"version":"0.4.5"}', encoding="utf-8"
            )
            data_dir = (
                codex_root
                / "data"
                / f"builder-pulse-{setup_builder_pulse.MARKETPLACE}"
            )
            data_dir.mkdir(parents=True)
            identity = {
                "installationId": "installation-1",
                "scopeSecret": "scope-secret",
                "installationToken": "delivery-token",
                "pendingInstallationToken": "pending-token",
                "builderId": "builder-1",
                "memberId": "member-1",
                "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
                "promptCapture": "on",
            }
            (data_dir / "identity.json").write_text(
                json.dumps(identity), encoding="utf-8"
            )
            (data_dir / "config.json").write_text(
                '{"enabled":true}', encoding="utf-8"
            )
            for filename in ("outbox.jsonl", "prompt-outbox.jsonl", "quarantine.jsonl"):
                (data_dir / filename).write_text("{}\n", encoding="utf-8")

            with mock.patch.dict(
                setup_builder_pulse.os.environ,
                {
                    "BUILDER_PULSE_ENABLED": "1",
                    "BUILDER_PULSE_DATA_DIR": str(data_dir),
                },
                clear=True,
            ), mock.patch.object(
                setup_builder_pulse,
                "pause_server_capture",
                return_value=True,
            ) as server_pause:
                paused = setup_builder_pulse.pause_existing_capture(
                    {"version": "0.4.5", "installedPath": str(plugin_root)}
                )

                self.assertIsNotNone(paused)
                assert paused is not None
                self.assertEqual(paused.identity, identity)
                active_identity = json.loads(
                    (data_dir / "identity.json").read_text(encoding="utf-8")
                )
                self.assertNotIn("installationToken", active_identity)
                self.assertNotIn("pendingInstallationToken", active_identity)
                self.assertEqual(active_identity["promptCapture"], "off")
                self.assertFalse(
                    builder_pulse.load_config(data_dir)["enabled"],
                    "a legacy enable environment must not defeat the persisted pause",
                )
                self.assertEqual(
                    json.loads(
                        (data_dir / "setup-paused-identity.json").read_text(
                            encoding="utf-8"
                        )
                    ),
                    identity,
                )
                for filename in (
                    "outbox.jsonl",
                    "prompt-outbox.jsonl",
                    "quarantine.jsonl",
                ):
                    self.assertFalse((data_dir / filename).exists())

                setup_builder_pulse.restore_paused_identity(cli, paused)

            server_pause.assert_called_once_with(identity, "0.4.5")

            self.assertEqual(
                json.loads((data_dir / "identity.json").read_text(encoding="utf-8")),
                identity,
            )
            self.assertFalse((data_dir / "setup-paused-identity.json").exists())

    def test_server_pause_is_a_barrier_for_a_cached_legacy_token(self) -> None:
        identity = {
            "installationId": "installation-1",
            "installationToken": "cached-legacy-token",
            "claimedEndpoint": "https://pulse.example",
        }
        server_state = {"paused": False}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(
                    {"paused": True, "installationId": "installation-1"}
                ).encode("utf-8")

        def urlopen(request, timeout):
            self.assertEqual(timeout, 10)
            self.assertEqual(
                request.full_url,
                "https://pulse.example/v1/privacy-pause",
            )
            self.assertEqual(
                json.loads(request.data.decode("utf-8")),
                {"installationId": "installation-1", "pluginVersion": "0.4.5"},
            )
            server_state["paused"] = True
            return Response()

        # A supported v0.4.5 process may already hold the token in memory. The
        # acknowledged server pause, rather than a local file lock, is what
        # prevents that process from delivering after quarantine begins.
        with mock.patch.object(
            setup_builder_pulse.urlrequest, "urlopen", side_effect=urlopen
        ):
            self.assertTrue(
                setup_builder_pulse.pause_server_capture(identity, "0.4.5")
            )

        legacy_delivery_accepted = not server_state["paused"]
        self.assertFalse(legacy_delivery_accepted)

    def test_pending_and_unclaimed_identities_survive_pause_for_claim_retry(self) -> None:
        identities = (
            {
                "installationId": "installation-pending",
                "scopeSecret": "a" * 64,
                "pendingInstallationToken": "b" * 64,
                "pendingEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
            },
            {
                "installationId": "installation-unclaimed",
                "scopeSecret": "c" * 64,
            },
        )
        for identity in identities:
            with self.subTest(installation_id=identity["installationId"]):
                with tempfile.TemporaryDirectory() as directory:
                    data_dir = Path(directory).resolve()
                    (data_dir / "identity.json").write_text(
                        json.dumps(identity), encoding="utf-8"
                    )
                    (data_dir / "config.json").write_text(
                        '{"enabled":true}', encoding="utf-8"
                    )
                    with mock.patch.object(
                        setup_builder_pulse,
                        "existing_plugin_data_dir",
                        return_value=data_dir,
                    ):
                        paused = setup_builder_pulse.pause_existing_capture(None)

                    self.assertEqual(paused.identity, identity)
                    self.assertEqual(
                        json.loads(
                            (data_dir / "setup-paused-identity.json").read_text()
                        ),
                        identity,
                    )
                    active = json.loads((data_dir / "identity.json").read_text())
                    self.assertNotIn("pendingInstallationToken", active)
                    self.assertFalse(
                        json.loads((data_dir / "config.json").read_text())["enabled"]
                    )

                    with mock.patch.object(
                        setup_builder_pulse,
                        "plugin_data_dir",
                        return_value=data_dir,
                    ):
                        setup_builder_pulse.restore_paused_identity(
                            ROOT / "scripts" / "builder_pulse.py", paused
                        )
                    self.assertEqual(
                        json.loads((data_dir / "identity.json").read_text()),
                        identity,
                    )

    def test_pause_network_failure_still_quarantines_locally_and_reports_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory).resolve()
            identity = {
                "installationId": "installation-1",
                "scopeSecret": "a" * 64,
                "installationToken": "delivery-token",
                "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
                "promptCapture": "on",
            }
            (data_dir / "identity.json").write_text(
                json.dumps(identity), encoding="utf-8"
            )
            (data_dir / "config.json").write_text(
                '{"enabled":true}', encoding="utf-8"
            )
            for filename in ("outbox.jsonl", "prompt-outbox.jsonl", "quarantine.jsonl"):
                (data_dir / filename).write_text("{}\n", encoding="utf-8")

            with (
                mock.patch.object(
                    setup_builder_pulse,
                    "existing_plugin_data_dir",
                    return_value=data_dir,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "pause_server_capture",
                    side_effect=setup_builder_pulse.SetupError("network unavailable"),
                ),
                self.assertRaisesRegex(
                    setup_builder_pulse.SetupError,
                    "status is unknown.*Exit all running Claude Code and Codex sessions",
                ),
            ):
                setup_builder_pulse.pause_existing_capture(None)

            active = json.loads((data_dir / "identity.json").read_text())
            self.assertNotIn("installationToken", active)
            self.assertEqual(active["promptCapture"], "off")
            self.assertFalse(
                json.loads((data_dir / "config.json").read_text())["enabled"]
            )
            self.assertEqual(
                json.loads((data_dir / "setup-paused-identity.json").read_text()),
                identity,
            )
            for filename in ("outbox.jsonl", "prompt-outbox.jsonl", "quarantine.jsonl"):
                self.assertFalse((data_dir / filename).exists())

    def test_server_resume_requires_the_exact_acknowledged_installation(self) -> None:
        identity = {
            "installationId": "installation-1",
            "installationToken": "enrolled-token",
            "claimedEndpoint": "https://pulse.example",
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(
                    {"resumed": True, "installationId": "installation-1"}
                ).encode("utf-8")

        def urlopen(request, timeout):
            self.assertEqual(timeout, 10)
            self.assertEqual(
                request.full_url,
                "https://pulse.example/v1/privacy-resume",
            )
            self.assertEqual(
                json.loads(request.data.decode("utf-8")),
                {"installationId": "installation-1", "pluginVersion": "0.4.6"},
            )
            return Response()

        with mock.patch.object(
            setup_builder_pulse.urlrequest, "urlopen", side_effect=urlopen
        ):
            setup_builder_pulse.resume_server_capture(identity, "0.4.6")

    def test_server_resume_normalizes_an_incomplete_http_response(self) -> None:
        identity = {
            "installationId": "installation-1",
            "installationToken": "enrolled-token",
            "claimedEndpoint": "https://pulse.example",
        }
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = http.client.IncompleteRead(b"partial")
        with (
            mock.patch.object(
                setup_builder_pulse.urlrequest,
                "urlopen",
                return_value=response,
            ),
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "could not resume",
            ),
        ):
            setup_builder_pulse.resume_server_capture(identity, "0.4.6")

    def test_server_resume_rejects_the_wrong_acknowledged_installation(self) -> None:
        identity = {
            "installationId": "installation-1",
            "installationToken": "enrolled-token",
            "claimedEndpoint": "https://pulse.example",
        }
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {"resumed": True, "installationId": "another-installation"}
        ).encode("utf-8")
        with (
            mock.patch.object(
                setup_builder_pulse.urlrequest,
                "urlopen",
                return_value=response,
            ),
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "did not confirm",
            ),
        ):
            setup_builder_pulse.resume_server_capture(identity, "0.4.6")

    def test_orphan_data_is_quarantined_without_a_registered_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = (Path(directory) / "builder-pulse-data").resolve()
            data_dir.mkdir()
            identity = {
                "installationId": "installation-1",
                "scopeSecret": "a" * 64,
                "installationToken": "delivery-token",
                "builderId": "builder-1",
                "memberId": "member-1",
                "claimedEndpoint": "https://pulse.example",
                "promptCapture": "on",
            }
            (data_dir / "identity.json").write_text(
                json.dumps(identity), encoding="utf-8"
            )
            (data_dir / "config.json").write_text(
                '{"enabled":true}', encoding="utf-8"
            )

            with mock.patch.object(
                setup_builder_pulse,
                "existing_plugin_data_dir",
                return_value=data_dir,
            ), mock.patch.object(
                setup_builder_pulse,
                "pause_server_capture",
                return_value=True,
            ) as server_pause:
                paused = setup_builder_pulse.pause_existing_capture(None)

            self.assertEqual(paused.data_dir, data_dir)
            server_pause.assert_called_once_with(identity, "0.5.0")
            self.assertFalse(
                json.loads((data_dir / "config.json").read_text())["enabled"]
            )
            self.assertNotIn(
                "installationToken",
                json.loads((data_dir / "identity.json").read_text()),
            )
            self.assertEqual(
                json.loads((data_dir / "setup-paused-identity.json").read_text()),
                identity,
            )

    def test_setup_reinstalls_current_release_and_keeps_code_out_of_arguments(self) -> None:
        invite_code = "InviteCode_1234567890"
        cli = ROOT / "scripts" / "builder_pulse.py"
        rollback = setup_builder_pulse.RollbackSource(
            "0.4.4",
            "a" * 40,
            setup_builder_pulse.REPOSITORY,
        )

        def run_command(arguments, *, env=None, expect_json=False):
            del expect_json
            if arguments[-1] == "claim":
                self.fail("claim arguments unexpectedly ended with claim")
            if "claim" in arguments:
                self.assertNotIn(invite_code, arguments)
                self.assertEqual(env["BUILDER_PULSE_INVITE_CODE"], invite_code)
            return ""

        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            mock.patch.object(
                setup_builder_pulse,
                "verify_release_exists",
                return_value=TARGET_COMMIT,
            ) as verify,
            mock.patch.object(
                setup_builder_pulse,
                "installed_builder",
                return_value={"version": "0.4.4"},
            ),
            mock.patch.object(
                setup_builder_pulse,
                "marketplace_state",
                return_value={
                    "marketplaceSource": {
                        "source": setup_builder_pulse.REPOSITORY,
                    }
                },
            ),
            mock.patch.object(
                setup_builder_pulse,
                "verified_rollback_source",
                return_value=rollback,
            ) as verified_rollback,
            mock.patch.object(setup_builder_pulse, "remove_current") as remove,
            mock.patch.object(
                setup_builder_pulse, "pause_existing_capture", return_value=None
            ) as pause,
            mock.patch.object(setup_builder_pulse, "install_release", return_value=cli) as install,
            mock.patch.object(setup_builder_pulse, "plugin_data_dir", return_value=ROOT),
            mock.patch.object(
                setup_builder_pulse,
                "authoritative_identity",
                return_value={
                    "installationId": "installation-1",
                    "installationToken": "token-1",
                    "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
                },
            ),
            mock.patch.object(setup_builder_pulse, "resume_server_capture") as resume,
            mock.patch.object(
                setup_builder_pulse,
                "activate",
                return_value={
                    "connected": False,
                    "activationReady": True,
                    "hooksTrusted": True,
                    "serverVerified": True,
                    "telemetryReceived": False,
                },
            ) as activate,
            mock.patch.object(setup_builder_pulse, "run_command", side_effect=run_command) as run,
        ):
            setup_builder_pulse.setup(
                invite_code,
                setup_builder_pulse.DEFAULT_ENDPOINT,
                ROOT,
                "Builder Pulse",
            )

        verify.assert_called_once_with(setup_builder_pulse.TARGET_RELEASE)
        remove.assert_called_once_with(
            plugin_installed=True,
            marketplace_configured=True,
            rollback_source=rollback,
        )
        verified_rollback.assert_called_once()
        pause.assert_called_once_with({"version": "0.4.4"})
        install.assert_called_once_with(
            setup_builder_pulse.TARGET_RELEASE,
            expected_commit=TARGET_COMMIT,
        )
        activate.assert_called_once_with(cli, "codex")
        resume.assert_called_once_with(
            {
                "installationId": "installation-1",
                "installationToken": "token-1",
                "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
            },
            "0.5.0",
        )
        calls = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any("claim" in arguments for arguments in calls))
        enroll_calls = [arguments for arguments in calls if "enroll" in arguments]
        self.assertEqual(len(enroll_calls), 1)
        self.assertEqual(
            enroll_calls[0][-7:],
            [
                "work",
                "enroll",
                "--root",
                str(ROOT.resolve()),
                "--project",
                "Builder Pulse",
                "--replace-existing",
            ],
        )
        self.assertTrue(
            any(arguments[-4:] == ["config", "set", "enabled", "false"] for arguments in calls)
        )
        self.assertTrue(
            any(arguments[-4:] == ["config", "set", "enabled", "true"] for arguments in calls)
        )
        self.assertTrue(any(arguments[-1] == "flush" for arguments in calls))

    def test_repair_reuses_and_rechecks_the_exact_existing_identity(self) -> None:
        cli = ROOT / "scripts" / "builder_pulse.py"
        rollback = setup_builder_pulse.RollbackSource(
            "0.4.4",
            "a" * 40,
            setup_builder_pulse.REPOSITORY,
        )
        identity = {
            "installationId": "installation-1",
            "builderId": "builder-1",
            "memberId": "member-1",
            "installationToken": "token-1",
            "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
        }
        status_identity = {
            "claimed": True,
            "tokenConfigured": True,
            "installationId": "installation-1",
            "builderId": "builder-1",
            "memberId": "member-1",
        }

        def run_command(arguments, *, env=None, expect_json=False):
            del env, expect_json
            self.assertNotIn("claim", arguments)
            return ""

        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            mock.patch.object(
                setup_builder_pulse,
                "verify_release_exists",
                return_value=TARGET_COMMIT,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "installed_builder",
                return_value={"version": "0.4.4"},
            ),
            mock.patch.object(
                setup_builder_pulse,
                "marketplace_state",
                return_value={
                    "marketplaceSource": {"source": setup_builder_pulse.REPOSITORY}
                },
            ),
            mock.patch.object(
                setup_builder_pulse,
                "verified_rollback_source",
                return_value=rollback,
            ),
            mock.patch.object(setup_builder_pulse, "installed_cli", return_value=cli),
            mock.patch.object(
                setup_builder_pulse,
                "plugin_data_dir",
                return_value=ROOT,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "authoritative_identity",
                return_value=identity,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "claimed_identity",
                return_value=setup_builder_pulse.claimed_identity_fields(status_identity),
            ) as claimed,
            mock.patch.object(setup_builder_pulse, "resume_server_capture"),
            mock.patch.object(
                setup_builder_pulse, "pause_existing_capture", return_value=None
            ),
            mock.patch.object(setup_builder_pulse, "remove_current"),
            mock.patch.object(setup_builder_pulse, "install_release", return_value=cli),
            mock.patch.object(
                setup_builder_pulse,
                "activate",
                return_value={
                    "activationReady": True,
                    "hooksTrusted": True,
                    "serverVerified": True,
                },
            ),
            mock.patch.object(
                setup_builder_pulse, "run_command", side_effect=run_command
            ) as run,
        ):
            setup_builder_pulse.setup(
                "",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                ROOT,
                "Builder Pulse",
                reuse_existing_claim=True,
            )

        self.assertEqual(claimed.call_count, 1)
        self.assertFalse(
            any("claim" in arguments for arguments in (call.args[0] for call in run.call_args_list))
        )

    def test_existing_claim_repair_rejects_new_invite_or_missing_identity(self) -> None:
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            self.assertRaisesRegex(setup_builder_pulse.SetupError, "must not use"),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                ROOT,
                "Builder Pulse",
                reuse_existing_claim=True,
            )

        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            mock.patch.object(
                setup_builder_pulse,
                "verify_release_exists",
                return_value=TARGET_COMMIT,
            ),
            mock.patch.object(setup_builder_pulse, "installed_builder", return_value=None),
            mock.patch.object(setup_builder_pulse, "marketplace_state", return_value=None),
            mock.patch.object(
                setup_builder_pulse, "verified_rollback_source", return_value=None
            ),
            mock.patch.object(
                setup_builder_pulse, "existing_plugin_data_dir", return_value=ROOT
            ),
            mock.patch.object(
                setup_builder_pulse, "authoritative_identity", return_value={}
            ),
            self.assertRaisesRegex(setup_builder_pulse.SetupError, "fully claimed"),
        ):
            setup_builder_pulse.setup(
                "",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                ROOT,
                "Builder Pulse",
                reuse_existing_claim=True,
            )

    def test_setup_rejects_home_as_an_enrollment_root(self) -> None:
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            self.assertRaisesRegex(setup_builder_pulse.SetupError, "project folder"),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                Path.home(),
                "Home",
            )

    def test_setup_rejects_a_parent_of_home_as_an_enrollment_root(self) -> None:
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            self.assertRaisesRegex(setup_builder_pulse.SetupError, "project folder"),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                Path.home().parent,
                "Home parent",
            )

    def test_filesystem_root_detection_covers_windows_drives_and_unc_shares(self) -> None:
        for root in (
            PureWindowsPath("C:\\"),
            PureWindowsPath("D:\\"),
            PureWindowsPath("\\\\server\\share\\"),
        ):
            with self.subTest(root=str(root)):
                self.assertTrue(setup_builder_pulse.is_filesystem_root(root))

        self.assertFalse(
            setup_builder_pulse.is_filesystem_root(PureWindowsPath("C:\\project"))
        )

    def test_release_verification_requires_a_published_immutable_release(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "tag_name": "v0.4.6",
                "draft": False,
                "immutable": True,
            }
        ).encode("utf-8")

        with mock.patch.object(
            setup_builder_pulse,
            "run_command",
            return_value=(
                f"{'a' * 40}\trefs/tags/v0.4.6\n"
                f"{TARGET_COMMIT}\trefs/tags/v0.4.6^{{}}\n"
            ),
        ) as run, \
            mock.patch.object(
                setup_builder_pulse.urlrequest,
                "urlopen",
                return_value=response,
            ) as opened:
            result = setup_builder_pulse.verify_release_exists("v0.4.6")

        self.assertEqual(result, TARGET_COMMIT)
        self.assertEqual(run.call_args.args[0][-2:], [
            "refs/tags/v0.4.6",
            "refs/tags/v0.4.6^{}",
        ])
        request = opened.call_args.args[0]
        self.assertEqual(
            request.full_url,
            f"{setup_builder_pulse.RELEASE_API}v0.4.6",
        )

    def test_release_verification_rejects_a_movable_tag(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "tag_name": "v0.4.6",
                "draft": False,
                "immutable": False,
            }
        ).encode("utf-8")

        with mock.patch.object(
            setup_builder_pulse,
            "run_command",
            return_value=f"{TARGET_COMMIT}\trefs/tags/v0.4.6\n",
        ), \
            mock.patch.object(
                setup_builder_pulse.urlrequest,
                "urlopen",
                return_value=response,
            ), self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "published immutable GitHub release",
            ):
            setup_builder_pulse.verify_release_exists("v0.4.6")

    def test_activation_or_flush_failure_disables_capture(self) -> None:
        cli = ROOT / "scripts" / "builder_pulse.py"

        for failure in ("activation", "flush"):
            with self.subTest(failure=failure):
                calls: list[list[str]] = []

                def run_command(arguments, *, env=None, expect_json=False):
                    del env, expect_json
                    calls.append(arguments)
                    if failure == "flush" and arguments[-1] == "flush":
                        raise setup_builder_pulse.SetupError("flush failed")
                    return ""

                def activate(_cli: Path, agent_platform: str):
                    self.assertEqual(agent_platform, "codex")
                    if failure == "activation":
                        raise setup_builder_pulse.SetupError("activation failed")
                    return {
                        "activationReady": True,
                        "hooksTrusted": True,
                        "serverVerified": True,
                    }

                with (
                    mock.patch.object(
                        setup_builder_pulse.shutil, "which", side_effect=codex_only_which
                    ),
                    mock.patch.object(
                        setup_builder_pulse,
                        "verify_release_exists",
                        return_value=TARGET_COMMIT,
                    ),
                    mock.patch.object(
                        setup_builder_pulse, "installed_builder", return_value=None
                    ),
                    mock.patch.object(
                        setup_builder_pulse, "marketplace_state", return_value=None
                    ),
                    mock.patch.object(
                        setup_builder_pulse,
                        "pause_existing_capture",
                        return_value=None,
                    ),
                    mock.patch.object(setup_builder_pulse, "remove_current"),
                    mock.patch.object(
                        setup_builder_pulse, "install_release", return_value=cli
                    ),
                    mock.patch.object(
                        setup_builder_pulse,
                        "plugin_data_dir",
                        return_value=ROOT,
                    ),
                    mock.patch.object(
                        setup_builder_pulse,
                        "authoritative_identity",
                        return_value={
                            "installationId": "installation-1",
                            "installationToken": "token-1",
                            "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
                        },
                    ),
                    mock.patch.object(
                        setup_builder_pulse,
                        "resume_server_capture",
                    ) as resume,
                    mock.patch.object(
                        setup_builder_pulse,
                        "pause_server_capture",
                        return_value=True,
                    ) as server_pause,
                    mock.patch.object(
                        setup_builder_pulse,
                        "quarantine_local_capture",
                    ) as local_quarantine,
                    mock.patch.object(
                        setup_builder_pulse, "activate", side_effect=activate
                    ),
                    mock.patch.object(
                        setup_builder_pulse,
                        "run_command",
                        side_effect=run_command,
                    ),
                    self.assertRaisesRegex(
                        setup_builder_pulse.SetupError, f"{failure} failed"
                    ),
                ):
                    setup_builder_pulse.setup(
                        "InviteCode_1234567890",
                        setup_builder_pulse.DEFAULT_ENDPOINT,
                        ROOT,
                        "Builder Pulse",
                    )

                self.assertIn(
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        str(cli),
                        "config",
                        "set",
                        "enabled",
                        "false",
                    ],
                    calls,
                )
                resume.assert_called_once()
                server_pause.assert_called_once_with(
                    {
                        "installationId": "installation-1",
                        "installationToken": "token-1",
                        "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
                    },
                    "0.5.0",
                )
                local_quarantine.assert_called_once_with(
                    ROOT,
                    {
                        "installationId": "installation-1",
                        "installationToken": "token-1",
                        "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
                    },
                )

    def test_resume_and_repause_fail_closed(self) -> None:
        cli = ROOT / "scripts" / "builder_pulse.py"
        identity = {
            "installationId": "installation-1",
            "installationToken": "token-1",
            "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
        }

        for failure in ("resume", "repause"):
            with self.subTest(failure=failure):
                calls: list[list[str]] = []

                def run_command(arguments, *, env=None, expect_json=False):
                    del env, expect_json
                    calls.append(arguments)
                    return ""

                resume_effect = (
                    setup_builder_pulse.SetupError("resume rejected")
                    if failure == "resume"
                    else None
                )
                pause_effect = (
                    setup_builder_pulse.SetupError("repause rejected")
                    if failure == "repause"
                    else None
                )
                expected_error = (
                    "resume rejected"
                    if failure == "resume"
                    else "server privacy-pause status is unknown"
                )

                with (
                    mock.patch.object(
                        setup_builder_pulse.shutil, "which", side_effect=codex_only_which
                    ),
                    mock.patch.object(
                        setup_builder_pulse,
                        "verify_release_exists",
                        return_value=TARGET_COMMIT,
                    ),
                    mock.patch.object(
                        setup_builder_pulse, "installed_builder", return_value=None
                    ),
                    mock.patch.object(
                        setup_builder_pulse, "marketplace_state", return_value=None
                    ),
                    mock.patch.object(
                        setup_builder_pulse,
                        "pause_existing_capture",
                        return_value=None,
                    ),
                    mock.patch.object(setup_builder_pulse, "remove_current"),
                    mock.patch.object(
                        setup_builder_pulse, "install_release", return_value=cli
                    ),
                    mock.patch.object(
                        setup_builder_pulse,
                        "plugin_data_dir",
                        return_value=ROOT,
                    ),
                    mock.patch.object(
                        setup_builder_pulse,
                        "authoritative_identity",
                        return_value=identity,
                    ),
                    mock.patch.object(
                        setup_builder_pulse,
                        "resume_server_capture",
                        side_effect=resume_effect,
                    ) as resume,
                    mock.patch.object(
                        setup_builder_pulse,
                        "pause_server_capture",
                        side_effect=pause_effect,
                    ) as repause,
                    mock.patch.object(
                        setup_builder_pulse,
                        "quarantine_local_capture",
                    ) as local_quarantine,
                    mock.patch.object(
                        setup_builder_pulse,
                        "activate",
                        side_effect=setup_builder_pulse.SetupError(
                            "activation failed"
                        ),
                    ),
                    mock.patch.object(
                        setup_builder_pulse,
                        "run_command",
                        side_effect=run_command,
                    ),
                    self.assertRaisesRegex(
                        setup_builder_pulse.SetupError,
                        expected_error,
                    ),
                ):
                    setup_builder_pulse.setup(
                        "InviteCode_1234567890",
                        setup_builder_pulse.DEFAULT_ENDPOINT,
                        ROOT,
                        "Builder Pulse",
                    )

                resume.assert_called_once_with(identity, "0.5.0")
                repause.assert_called_once_with(identity, "0.5.0")
                local_quarantine.assert_called_once_with(ROOT, identity)

    def test_keyboard_interrupt_after_resume_still_repauses_and_quarantines(self) -> None:
        cli = ROOT / "scripts" / "builder_pulse.py"
        identity = {
            "installationId": "installation-1",
            "installationToken": "token-1",
            "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
        }
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            mock.patch.object(
                setup_builder_pulse,
                "verify_release_exists",
                return_value=TARGET_COMMIT,
            ),
            mock.patch.object(
                setup_builder_pulse, "installed_builder", return_value=None
            ),
            mock.patch.object(
                setup_builder_pulse, "marketplace_state", return_value=None
            ),
            mock.patch.object(
                setup_builder_pulse, "pause_existing_capture", return_value=None
            ),
            mock.patch.object(setup_builder_pulse, "remove_current"),
            mock.patch.object(
                setup_builder_pulse, "install_release", return_value=cli
            ),
            mock.patch.object(
                setup_builder_pulse, "plugin_data_dir", return_value=ROOT
            ),
            mock.patch.object(
                setup_builder_pulse,
                "authoritative_identity",
                return_value=identity,
            ),
            mock.patch.object(
                setup_builder_pulse, "resume_server_capture"
            ) as resume,
            mock.patch.object(
                setup_builder_pulse, "pause_server_capture", return_value=True
            ) as repause,
            mock.patch.object(
                setup_builder_pulse,
                "quarantine_local_capture",
            ) as local_quarantine,
            mock.patch.object(
                setup_builder_pulse,
                "activate",
                side_effect=KeyboardInterrupt,
            ),
            mock.patch.object(setup_builder_pulse, "run_command", return_value=""),
            self.assertRaises(KeyboardInterrupt),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                ROOT,
                "Builder Pulse",
            )

        resume.assert_called_once_with(identity, "0.5.0")
        repause.assert_called_once_with(identity, "0.5.0")
        local_quarantine.assert_called_once_with(ROOT, identity)

    def test_activation_normalizes_a_process_start_failure(self) -> None:
        with (
            mock.patch.object(
                setup_builder_pulse.subprocess,
                "run",
                side_effect=OSError("process unavailable"),
            ),
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "activation could not start",
            ),
        ):
            setup_builder_pulse.activate(ROOT / "scripts" / "builder_pulse.py")

    def test_main_does_not_claim_complete_safety_for_a_stopped_setup(self) -> None:
        error = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "setup_builder_pulse.py",
                    "--code",
                    "InviteCode_1234567890",
                    "--project-root",
                    str(ROOT),
                    "--project-label",
                    "Builder Pulse",
                ],
            ),
            mock.patch.object(
                setup_builder_pulse,
                "setup",
                side_effect=setup_builder_pulse.SetupError(
                    "server privacy-pause status is unknown"
                ),
            ),
            mock.patch("sys.stderr", error),
        ):
            self.assertEqual(setup_builder_pulse.main(), 1)

        self.assertIn("Builder Pulse setup stopped", error.getvalue())
        self.assertNotIn("failed safely", error.getvalue())

    def test_activation_review_result_is_preserved_even_with_nonzero_exit(self) -> None:
        completed = mock.Mock(
            returncode=3,
            stdout='{\n  "connected": false,\n  "reviewRequired": true\n}\n',
            stderr="",
        )
        with mock.patch.object(
            setup_builder_pulse.subprocess,
            "run",
            return_value=completed,
        ):
            result = setup_builder_pulse.activate(ROOT / "scripts" / "builder_pulse.py")
        self.assertEqual(result["reviewRequired"], True)

    def test_nonzero_activation_cannot_report_connected(self) -> None:
        completed = mock.Mock(
            returncode=3,
            stdout=(
                '{"connected":true,"hooksTrusted":true,'
                '"serverVerified":true}'
            ),
            stderr="",
        )
        with mock.patch.object(
            setup_builder_pulse.subprocess,
            "run",
            return_value=completed,
        ):
            with self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "activation failed",
            ):
                setup_builder_pulse.activate(
                    ROOT / "scripts" / "builder_pulse.py"
                )

    def test_failed_update_restores_previous_immutable_release(self) -> None:
        rollback = setup_builder_pulse.RollbackSource(
            "0.4.4",
            "b" * 40,
            setup_builder_pulse.REPOSITORY,
        )
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            mock.patch.object(
                setup_builder_pulse,
                "verify_release_exists",
                return_value=TARGET_COMMIT,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "installed_builder",
                return_value={"version": "0.4.4"},
            ),
            mock.patch.object(
                setup_builder_pulse,
                "marketplace_state",
                return_value={
                    "marketplaceSource": {
                        "source": setup_builder_pulse.REPOSITORY,
                    }
                },
            ),
            mock.patch.object(
                setup_builder_pulse, "pause_existing_capture", return_value=None
            ),
            mock.patch.object(
                setup_builder_pulse,
                "verified_rollback_source",
                return_value=rollback,
            ),
            mock.patch.object(setup_builder_pulse, "remove_current"),
            mock.patch.object(setup_builder_pulse, "cleanup_partial") as cleanup,
            mock.patch.object(
                setup_builder_pulse,
                "install_release",
                side_effect=setup_builder_pulse.SetupError("update failed"),
            ) as install,
            mock.patch.object(
                setup_builder_pulse,
                "install_verified_rollback",
                return_value=ROOT / "scripts" / "builder_pulse.py",
            ) as restore,
        ):
            with self.assertRaisesRegex(setup_builder_pulse.SetupError, "update failed"):
                setup_builder_pulse.setup(
                    "InviteCode_1234567890",
                    setup_builder_pulse.DEFAULT_ENDPOINT,
                    ROOT,
                    "Builder Pulse",
                )

        cleanup.assert_called_once_with()
        install.assert_called_once_with(
            setup_builder_pulse.TARGET_RELEASE,
            expected_commit=TARGET_COMMIT,
        )
        restore.assert_called_once_with(rollback)

    def test_retry_recovers_identity_after_target_and_rollback_both_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = (Path(directory) / "builder-pulse-data").resolve()
            data_dir.mkdir()
            identity = {
                "installationId": "installation-1",
                "scopeSecret": "a" * 64,
                "installationToken": "delivery-token",
                "builderId": "builder-1",
                "memberId": "member-1",
                "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
                "promptCapture": "on",
            }
            (data_dir / "identity.json").write_text(
                json.dumps(identity), encoding="utf-8"
            )
            (data_dir / "config.json").write_text(
                '{"enabled":true}', encoding="utf-8"
            )
            rollback = setup_builder_pulse.RollbackSource(
                "0.4.5",
                "b" * 40,
                setup_builder_pulse.REPOSITORY,
            )
            cli = ROOT / "scripts" / "builder_pulse.py"
            install_attempt = 0
            commands: list[list[str]] = []

            def install_release(_release, *, expected_commit=None):
                nonlocal install_attempt
                self.assertEqual(expected_commit, TARGET_COMMIT)
                install_attempt += 1
                if install_attempt == 1:
                    raise setup_builder_pulse.SetupError("target install failed")
                return cli

            def run_command(arguments, *, env=None, expect_json=False):
                del env, expect_json
                commands.append(arguments)
                return ""

            with (
                mock.patch.object(
                    setup_builder_pulse.shutil, "which", side_effect=codex_only_which
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "verify_release_exists",
                    return_value=TARGET_COMMIT,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "installed_builder",
                    side_effect=[{"version": "0.4.5"}, None],
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "marketplace_state",
                    side_effect=[
                        {
                            "marketplaceSource": {
                                "source": setup_builder_pulse.REPOSITORY
                            }
                        },
                        None,
                    ],
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "verified_rollback_source",
                    side_effect=[rollback, None],
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "existing_plugin_data_dir",
                    return_value=data_dir,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "pause_server_capture",
                    return_value=True,
                ) as server_pause,
                mock.patch.object(setup_builder_pulse, "remove_current"),
                mock.patch.object(setup_builder_pulse, "cleanup_partial"),
                mock.patch.object(
                    setup_builder_pulse,
                    "install_release",
                    side_effect=install_release,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "install_verified_rollback",
                    side_effect=setup_builder_pulse.SetupError("rollback failed"),
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "plugin_data_dir",
                    return_value=data_dir,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "resume_server_capture",
                ) as server_resume,
                mock.patch.object(
                    setup_builder_pulse,
                    "claimed_identity",
                    return_value={
                        "installationId": "installation-1",
                        "builderId": "builder-1",
                        "memberId": "member-1",
                    },
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "activate",
                    return_value={
                        "activationReady": True,
                        "hooksTrusted": True,
                        "serverVerified": True,
                    },
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "run_command",
                    side_effect=run_command,
                ),
            ):
                with self.assertRaisesRegex(
                    setup_builder_pulse.SetupError,
                    "previous version could not be restored",
                ):
                    setup_builder_pulse.setup(
                        "InviteCode_1234567890",
                        setup_builder_pulse.DEFAULT_ENDPOINT,
                        ROOT,
                        "Builder Pulse",
                    )

                self.assertTrue((data_dir / "setup-paused-identity.json").exists())
                self.assertNotIn(
                    "installationToken",
                    json.loads((data_dir / "identity.json").read_text()),
                )

                # This models a fresh process after both package paths vanished.
                setup_builder_pulse.setup(
                    "",
                    setup_builder_pulse.DEFAULT_ENDPOINT,
                    ROOT,
                    "Builder Pulse",
                    reuse_existing_claim=True,
                )

            self.assertEqual(server_pause.call_count, 2)
            server_resume.assert_called_once_with(identity, "0.5.0")
            self.assertFalse((data_dir / "setup-paused-identity.json").exists())
            self.assertEqual(
                json.loads((data_dir / "identity.json").read_text()),
                identity,
            )
            self.assertFalse(any("claim" in command for command in commands))

    def test_partial_removal_failure_repins_the_exact_previous_commit(self) -> None:
        rollback = setup_builder_pulse.RollbackSource(
            "0.4.4",
            "c" * 40,
            setup_builder_pulse.REPOSITORY,
        )
        marketplace_removals = 0

        def run_command(arguments, *, env=None, expect_json=False):
            nonlocal marketplace_removals
            del env, expect_json
            if arguments[:3] == ["codex", "plugin", "remove"]:
                return ""
            if arguments[:4] == ["codex", "plugin", "marketplace", "remove"]:
                marketplace_removals += 1
                if marketplace_removals == 1:
                    raise setup_builder_pulse.SetupError("marketplace removal failed")
                return ""
            self.fail(f"Unexpected command: {arguments}")

        with (
            mock.patch.object(
                setup_builder_pulse,
                "run_command",
                side_effect=run_command,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "install_verified_rollback",
            ) as restore,
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "marketplace removal failed",
            ),
        ):
            setup_builder_pulse.remove_current(
                plugin_installed=True,
                marketplace_configured=True,
                rollback_source=rollback,
            )

        self.assertEqual(marketplace_removals, 2)
        restore.assert_called_once_with(rollback)

    def test_install_rejects_stale_package_version(self) -> None:
        add_response = {"installedPath": str(ROOT)}

        def run_command(arguments, *, env=None, expect_json=False):
            del env
            if arguments[:3] == ["codex", "plugin", "add"]:
                self.assertTrue(expect_json)
                return add_response
            return ""

        with (
            mock.patch.object(setup_builder_pulse, "run_command", side_effect=run_command),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(
                Path,
                "read_text",
                return_value='{"version":"0.4.4"}',
            ),
        ):
            with self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "expected 0.4.6",
            ):
                setup_builder_pulse.install_release("v0.4.6")

    def test_claude_install_tree_must_match_the_immutable_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory) / "installed"
            shutil.copytree(
                setup_builder_pulse.expected_claude_package_root(),
                installed,
            )
            setup_builder_pulse.verify_claude_install_tree(installed)

            (installed / "hooks" / "hooks.json").write_text(
                '{"hooks":{}}', encoding="utf-8"
            )
            with self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "differs from the immutable release",
            ):
                setup_builder_pulse.verify_claude_install_tree(installed)

    def test_claude_marketplace_name_collision_is_rejected_before_mutation(
        self,
    ) -> None:
        commands: list[list[str]] = []

        def run_command(arguments, *, env=None, expect_json=False):
            del env, expect_json
            commands.append(arguments)
            if arguments == ["claude", "plugin", "list", "--json"]:
                return []
            if arguments == [
                "claude",
                "plugin",
                "marketplace",
                "list",
                "--json",
            ]:
                return [
                    {
                        "name": setup_builder_pulse.CLAUDE_MARKETPLACE,
                        "source": "github",
                        "repo": "attacker/builder-pulse-plugin",
                        "installLocation": "/tmp/not-growthx",
                    }
                ]
            self.fail(f"unexpected mutating command: {arguments}")

        with (
            mock.patch.object(
                setup_builder_pulse,
                "run_command",
                side_effect=run_command,
            ),
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "points to a different source",
            ),
        ):
            setup_builder_pulse.install_claude_release("f" * 40)

        self.assertEqual(
            commands,
            [
                ["claude", "plugin", "list", "--json"],
                ["claude", "plugin", "marketplace", "list", "--json"],
            ],
        )

    def test_claude_marketplace_must_match_commit_and_release_files(self) -> None:
        expected_commit = "f" * 40
        source_root = ROOT

        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory) / "marketplace"
            (installed / ".claude-plugin").mkdir(parents=True)
            shutil.copy2(
                source_root / ".claude-plugin" / "marketplace.json",
                installed / ".claude-plugin" / "marketplace.json",
            )
            shutil.copytree(
                source_root / "claude-plugins",
                installed / "claude-plugins",
            )
            (installed / ".gcs-sha").write_text(expected_commit, encoding="utf-8")
            marketplace = {
                "name": setup_builder_pulse.CLAUDE_MARKETPLACE,
                "source": "github",
                "repo": "GrowthX-Club/builder-pulse-plugin",
                "installLocation": str(installed),
            }

            setup_builder_pulse.verify_claude_marketplace(
                marketplace,
                expected_commit,
            )

            (installed / ".gcs-sha").write_text("a" * 40, encoding="utf-8")
            with self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "does not match the immutable release",
            ):
                setup_builder_pulse.verify_claude_marketplace(
                    marketplace,
                    expected_commit,
                )

            (installed / ".gcs-sha").write_text(expected_commit, encoding="utf-8")
            (installed / ".claude-plugin" / "marketplace.json").write_text(
                '{"plugins": [{"name": "attacker", "source": "./run"}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "differs from the immutable release",
            ):
                setup_builder_pulse.verify_claude_marketplace(
                    marketplace,
                    expected_commit,
                )

    def test_claude_update_keeps_current_plugin_until_replacement_is_verified(
        self,
    ) -> None:
        target = (
            setup_builder_pulse.CLAUDE_WINDOWS_PLUGIN
            if setup_builder_pulse.os.name == "nt"
            else setup_builder_pulse.CLAUDE_POSIX_PLUGIN
        )
        old_marketplace = "growthx-builder-tools-v0-4-6"
        old_platform = (
            "builder-pulse-claude-windows"
            if setup_builder_pulse.os.name == "nt"
            else "builder-pulse-claude-posix"
        )
        old_other_platform = (
            "builder-pulse-claude-posix"
            if setup_builder_pulse.os.name == "nt"
            else "builder-pulse-claude-windows"
        )
        old_target = f"{old_platform}@{old_marketplace}"
        old_other = f"{old_other_platform}@{old_marketplace}"
        installed_root = setup_builder_pulse.expected_claude_package_root()
        before = [
            {"id": old_target, "version": "0.4.6", "enabled": True},
            {"id": old_other, "version": "0.4.6", "enabled": True},
        ]
        after = [
            {
                "id": target,
                "version": "0.5.0",
                "enabled": True,
                "scope": "user",
                "installPath": str(installed_root),
            },
            *before,
        ]
        commands: list[list[str]] = []

        def run_command(arguments, *, env=None, expect_json=False):
            del env, expect_json
            commands.append(arguments)
            return ""

        with (
            mock.patch.object(
                setup_builder_pulse,
                "installed_claude_builders",
                side_effect=[before, after],
            ),
            mock.patch.object(
                setup_builder_pulse,
                "ensure_claude_marketplace",
            ),
            mock.patch.object(
                setup_builder_pulse,
                "run_command",
                side_effect=run_command,
            ),
            mock.patch.object(setup_builder_pulse, "verify_claude_install_tree"),
        ):
            self.assertEqual(
                setup_builder_pulse.install_claude_release("f" * 40),
                installed_root.resolve(strict=False),
            )

        self.assertIn(
            ["claude", "plugin", "install", target, "--scope", "user", "--yes"],
            commands,
        )
        uninstall_commands = [
            command for command in commands if command[:3] == ["claude", "plugin", "uninstall"]
        ]
        self.assertEqual(
            uninstall_commands,
            [
                [
                    "claude",
                    "plugin",
                    "uninstall",
                    old_target,
                    "--scope",
                    "user",
                    "--keep-data",
                    "--yes",
                ],
                [
                    "claude",
                    "plugin",
                    "uninstall",
                    old_other,
                    "--scope",
                    "user",
                    "--keep-data",
                    "--yes",
                ]
            ],
        )

    def test_claude_failed_replacement_never_uninstalls_existing_plugins(self) -> None:
        target = (
            setup_builder_pulse.CLAUDE_WINDOWS_PLUGIN
            if setup_builder_pulse.os.name == "nt"
            else setup_builder_pulse.CLAUDE_POSIX_PLUGIN
        )
        old_marketplace = "growthx-builder-tools-v0-4-6"
        old_platform = (
            "builder-pulse-claude-windows"
            if setup_builder_pulse.os.name == "nt"
            else "builder-pulse-claude-posix"
        )
        old_other_platform = (
            "builder-pulse-claude-posix"
            if setup_builder_pulse.os.name == "nt"
            else "builder-pulse-claude-windows"
        )
        before = [
            {
                "id": f"{old_platform}@{old_marketplace}",
                "version": "0.4.6",
                "enabled": True,
            },
            {
                "id": f"{old_other_platform}@{old_marketplace}",
                "version": "0.4.6",
                "enabled": True,
            },
        ]
        after = [
            {
                "id": target,
                "version": "0.4.6",
                "enabled": True,
                "scope": "user",
                "installPath": str(setup_builder_pulse.expected_claude_package_root()),
            },
            *before,
        ]
        commands: list[list[str]] = []

        def run_command(arguments, *, env=None, expect_json=False):
            del env, expect_json
            commands.append(arguments)
            return ""

        with (
            mock.patch.object(
                setup_builder_pulse,
                "installed_claude_builders",
                side_effect=[before, after],
            ),
            mock.patch.object(
                setup_builder_pulse,
                "ensure_claude_marketplace",
            ),
            mock.patch.object(
                setup_builder_pulse,
                "run_command",
                side_effect=run_command,
            ),
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "unexpected Builder Pulse version",
            ),
        ):
            setup_builder_pulse.install_claude_release("f" * 40)

        self.assertFalse(
            any(command[:3] == ["claude", "plugin", "uninstall"] for command in commands)
        )

    def test_install_rejects_a_checkout_that_is_not_the_verified_commit(self) -> None:
        expected = "f" * 40
        cli = ROOT / "scripts" / "builder_pulse.py"
        with (
            mock.patch.object(setup_builder_pulse, "run_command", return_value=""),
            mock.patch.object(
                setup_builder_pulse,
                "add_plugin_from_configured_marketplace",
                return_value=cli,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "verified_git_checkout",
                return_value=(setup_builder_pulse.REPOSITORY, "a" * 40),
            ),
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "different provenance",
            ),
        ):
            setup_builder_pulse.install_release(
                "v0.4.6",
                expected_commit=expected,
            )

    def test_shared_runtime_requires_the_clean_exact_current_release_checkout(self) -> None:
        expected = "f" * 40
        for checkout, message in (
            (
                ("https://github.com/udayanwalvekar/builder-pulse-plugin.git", expected),
                "does not match the immutable release",
            ),
            (
                (setup_builder_pulse.REPOSITORY, "a" * 40),
                "does not match the immutable release",
            ),
        ):
            with self.subTest(checkout=checkout), mock.patch.object(
                setup_builder_pulse,
                "verified_installer_checkout",
                side_effect=VERIFIED_INSTALLER_CHECKOUT,
            ), mock.patch.object(
                setup_builder_pulse,
                "verified_git_checkout",
                return_value=checkout,
            ), self.assertRaisesRegex(setup_builder_pulse.SetupError, message):
                INSTALL_SHARED_RUNTIME(expected)

        with mock.patch.object(
            setup_builder_pulse,
            "verified_installer_checkout",
            side_effect=VERIFIED_INSTALLER_CHECKOUT,
        ), mock.patch.object(
            setup_builder_pulse,
            "verified_git_checkout",
            side_effect=setup_builder_pulse.SetupError(
                "The existing Builder Pulse checkout has modified, untracked, or ignored files"
            ),
        ), self.assertRaisesRegex(
            setup_builder_pulse.SetupError,
            "modified, untracked, or ignored files",
        ):
            INSTALL_SHARED_RUNTIME(expected)

    def test_shared_runtime_has_a_real_plugin_root_layout_for_claude_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_root = Path(directory).resolve()
            (source_root / "scripts").mkdir()
            (source_root / "config").mkdir()
            shutil.copy2(
                ROOT / "scripts" / "builder_pulse.py",
                source_root / "scripts" / "builder_pulse.py",
            )
            (source_root / "scripts" / "setup_builder_pulse.py").write_text(
                "# installer\n", encoding="utf-8"
            )
            shutil.copy2(
                ROOT / "config" / "defaults.json",
                source_root / "config" / "defaults.json",
            )
            for command in (
                ["git", "init", "-q"],
                ["git", "remote", "add", "origin", setup_builder_pulse.REPOSITORY],
                ["git", "add", "."],
                [
                    "git",
                    "-c",
                    "user.name=Builder Pulse Test",
                    "-c",
                    "user.email=builder-pulse-test@example.invalid",
                    "commit",
                    "-qm",
                    "verified release",
                ],
            ):
                subprocess.run(command, cwd=source_root, check=True)
            expected = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with mock.patch.object(
                setup_builder_pulse,
                "__file__",
                str(source_root / "scripts" / "setup_builder_pulse.py"),
            ), mock.patch.object(
                setup_builder_pulse,
                "verified_installer_checkout",
                side_effect=VERIFIED_INSTALLER_CHECKOUT,
            ):
                installed_cli = INSTALL_SHARED_RUNTIME(expected)

            runtime_root = (
                setup_builder_pulse.canonical_plugin_data_dir()
                / "runtime"
                / "0.5.0"
            )
            self.assertEqual(
                installed_cli,
                runtime_root / "scripts" / "builder_pulse.py",
            )
            runtime_spec = importlib.util.spec_from_file_location(
                "builder_pulse_shared_runtime_test",
                installed_cli,
            )
            assert runtime_spec is not None and runtime_spec.loader is not None
            runtime_module = importlib.util.module_from_spec(runtime_spec)
            runtime_spec.loader.exec_module(runtime_module)
            self.assertEqual(runtime_module.PLUGIN_ROOT, runtime_root)
            self.assertEqual(runtime_module.PLUGIN_VERSION, "0.5.0")
            self.assertTrue(runtime_module.DEFAULTS_PATH.is_file())
            self.assertTrue(runtime_module.MANIFEST_PATH.is_file())

    def test_claude_only_setup_claims_and_activates_with_the_real_shared_layout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_root = Path(directory).resolve()
            (source_root / "scripts").mkdir()
            (source_root / "config").mkdir()
            shutil.copy2(
                ROOT / "scripts" / "builder_pulse.py",
                source_root / "scripts" / "builder_pulse.py",
            )
            (source_root / "scripts" / "setup_builder_pulse.py").write_text(
                "# installer\n", encoding="utf-8"
            )
            shutil.copy2(
                ROOT / "config" / "defaults.json",
                source_root / "config" / "defaults.json",
            )
            for command in (
                ["git", "init", "-q"],
                ["git", "remote", "add", "origin", setup_builder_pulse.REPOSITORY],
                ["git", "add", "."],
                [
                    "git",
                    "-c",
                    "user.name=Builder Pulse Test",
                    "-c",
                    "user.email=builder-pulse-test@example.invalid",
                    "commit",
                    "-qm",
                    "verified release",
                ],
            ):
                subprocess.run(command, cwd=source_root, check=True)
            expected = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            commands: list[list[str]] = []
            activated: list[Path] = []
            identity = {
                "installationId": "installation-1",
                "installationToken": "token-1",
                "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
            }

            def run_command(arguments, *, env=None, expect_json=False):
                if arguments[0] == "git":
                    return RUN_COMMAND(arguments, env=env, expect_json=expect_json)
                commands.append(arguments)
                if "claim" in arguments:
                    self.assertEqual(
                        env["BUILDER_PULSE_INVITE_CODE"],
                        "InviteCode_1234567890",
                    )
                return ""

            def activate(cli: Path, agent_platform: str) -> dict[str, object]:
                self.assertEqual(agent_platform, "claude_code")
                runtime_root = (
                    setup_builder_pulse.canonical_plugin_data_dir()
                    / "runtime"
                    / "0.5.0"
                )
                self.assertEqual(cli, runtime_root / "scripts" / "builder_pulse.py")
                runtime_spec = importlib.util.spec_from_file_location(
                    "builder_pulse_claude_only_setup_test",
                    cli,
                )
                assert runtime_spec is not None and runtime_spec.loader is not None
                runtime_module = importlib.util.module_from_spec(runtime_spec)
                runtime_spec.loader.exec_module(runtime_module)
                self.assertEqual(runtime_module.PLUGIN_ROOT, runtime_root)
                self.assertEqual(runtime_module.PLUGIN_VERSION, "0.5.0")
                activated.append(cli)
                return {
                    "activationReady": True,
                    "hooksVerified": True,
                    "serverVerified": True,
                    "agentPlatform": "claude_code",
                }

            with (
                mock.patch.object(
                    setup_builder_pulse.shutil,
                    "which",
                    side_effect=claude_only_which,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "verify_release_exists",
                    return_value=expected,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "__file__",
                    str(source_root / "scripts" / "setup_builder_pulse.py"),
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "verified_installer_checkout",
                    side_effect=VERIFIED_INSTALLER_CHECKOUT,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "install_shared_runtime",
                    side_effect=INSTALL_SHARED_RUNTIME,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "pause_existing_capture",
                    return_value=None,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "install_claude_release",
                ) as install_claude,
                mock.patch.object(
                    setup_builder_pulse,
                    "authoritative_identity",
                    return_value=identity,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "resume_server_capture",
                ) as resume,
                mock.patch.object(
                    setup_builder_pulse,
                    "activate",
                    side_effect=activate,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "run_command",
                    side_effect=run_command,
                ),
            ):
                setup_builder_pulse.setup(
                    "InviteCode_1234567890",
                    setup_builder_pulse.DEFAULT_ENDPOINT,
                    ROOT,
                    "Builder Pulse",
                )

            install_claude.assert_called_once_with(expected)
            resume.assert_called_once_with(identity, "0.5.0")
            self.assertEqual(len(activated), 1)
            self.assertTrue(any("claim" in arguments for arguments in commands))
            self.assertTrue(any("enroll" in arguments for arguments in commands))
            self.assertTrue(any(arguments[-1] == "flush" for arguments in commands))

    def test_legacy_identity_migrates_before_shared_runtime_is_installed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_root = Path(directory).resolve()
            (source_root / "scripts").mkdir()
            (source_root / "config").mkdir()
            shutil.copy2(
                ROOT / "scripts" / "builder_pulse.py",
                source_root / "scripts" / "builder_pulse.py",
            )
            (source_root / "scripts" / "setup_builder_pulse.py").write_text(
                "# installer\n", encoding="utf-8"
            )
            shutil.copy2(
                ROOT / "config" / "defaults.json",
                source_root / "config" / "defaults.json",
            )
            for command in (
                ["git", "init", "-q"],
                ["git", "remote", "add", "origin", setup_builder_pulse.REPOSITORY],
                ["git", "add", "."],
                [
                    "git",
                    "-c",
                    "user.name=Builder Pulse Test",
                    "-c",
                    "user.email=builder-pulse-test@example.invalid",
                    "commit",
                    "-qm",
                    "verified release",
                ],
            ):
                subprocess.run(command, cwd=source_root, check=True)
            expected = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            legacy = setup_builder_pulse.legacy_codex_plugin_data_dir()
            legacy.mkdir(parents=True)
            identity = {
                "installationId": "legacy-installation",
                "installationToken": "legacy-token",
            }
            (legacy / "identity.json").write_text(
                json.dumps(identity), encoding="utf-8"
            )
            shared = setup_builder_pulse.canonical_plugin_data_dir()
            self.assertFalse(shared.exists())

            installed_runtime: list[Path] = []

            def install_after_migration(commit: str) -> Path:
                self.assertEqual(
                    json.loads((shared / "identity.json").read_text(encoding="utf-8")),
                    identity,
                )
                cli = INSTALL_SHARED_RUNTIME(commit)
                installed_runtime.append(cli)
                raise setup_builder_pulse.SetupError("stop after shared runtime")

            rollback = setup_builder_pulse.RollbackSource(
                "0.4.4",
                "a" * 40,
                setup_builder_pulse.REPOSITORY,
            )
            with (
                mock.patch.object(
                    setup_builder_pulse.shutil,
                    "which",
                    side_effect=codex_only_which,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "verify_release_exists",
                    return_value=expected,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "__file__",
                    str(source_root / "scripts" / "setup_builder_pulse.py"),
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "verified_installer_checkout",
                    side_effect=VERIFIED_INSTALLER_CHECKOUT,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "install_shared_runtime",
                    side_effect=install_after_migration,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "installed_builder",
                    return_value={"version": "0.4.4"},
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "marketplace_state",
                    return_value={
                        "marketplaceSource": {
                            "source": setup_builder_pulse.REPOSITORY,
                        }
                    },
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "verified_rollback_source",
                    return_value=rollback,
                ),
                self.assertRaisesRegex(
                    setup_builder_pulse.SetupError,
                    "stop after shared runtime",
                ),
            ):
                setup_builder_pulse.setup(
                    "InviteCode_1234567890",
                    setup_builder_pulse.DEFAULT_ENDPOINT,
                    ROOT,
                    "Builder Pulse",
                )

            self.assertEqual(
                installed_runtime,
                [shared / "runtime" / "0.5.0" / "scripts" / "builder_pulse.py"],
            )
            self.assertEqual(
                json.loads((shared / "identity.json").read_text(encoding="utf-8")),
                identity,
            )
            self.assertTrue((legacy / "identity.json").is_file())

    def test_shared_runtime_rejects_runtime_tampering_when_claude_wrapper_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "scripts").mkdir()
            (root / "config").mkdir()
            wrapper = root / "claude-plugins" / "posix" / "scripts"
            wrapper.mkdir(parents=True)
            runtime = root / "scripts" / "builder_pulse.py"
            runtime.write_text("print('verified runtime')\n", encoding="utf-8")
            (root / "scripts" / "setup_builder_pulse.py").write_text(
                "# installer\n", encoding="utf-8"
            )
            (root / "config" / "defaults.json").write_text("{}\n", encoding="utf-8")
            (wrapper / "builder_pulse_claude.sh").write_text(
                "#!/bin/sh\n", encoding="utf-8"
            )
            for command in (
                ["git", "init", "-q"],
                ["git", "remote", "add", "origin", setup_builder_pulse.REPOSITORY],
                ["git", "add", "."],
                [
                    "git",
                    "-c",
                    "user.name=Builder Pulse Test",
                    "-c",
                    "user.email=builder-pulse-test@example.invalid",
                    "commit",
                    "-qm",
                    "verified release",
                ],
            ):
                subprocess.run(command, cwd=root, check=True)
            expected = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            # Only the shared telemetry runtime changes; the validated Claude
            # wrapper package remains byte-for-byte identical.
            runtime.write_text("print('tampered runtime')\n", encoding="utf-8")
            with mock.patch.object(
                setup_builder_pulse,
                "__file__",
                str(root / "scripts" / "setup_builder_pulse.py"),
            ), mock.patch.object(
                setup_builder_pulse,
                "verified_installer_checkout",
                side_effect=VERIFIED_INSTALLER_CHECKOUT,
            ), self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "modified, untracked, or ignored files",
            ):
                INSTALL_SHARED_RUNTIME(expected)

    def test_rejects_marketplace_name_pointing_to_another_repository(self) -> None:
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            mock.patch.object(setup_builder_pulse, "verify_release_exists"),
            mock.patch.object(setup_builder_pulse, "installed_builder", return_value=None),
            mock.patch.object(
                setup_builder_pulse,
                "marketplace_state",
                return_value={
                    "marketplaceSource": {
                        "source": "https://github.com/example/not-builder-pulse.git"
                    }
                },
            ),
        ):
            with self.assertRaisesRegex(setup_builder_pulse.SetupError, "different source"):
                setup_builder_pulse.setup(
                    "InviteCode_1234567890",
                    setup_builder_pulse.DEFAULT_ENDPOINT,
                    ROOT,
                    "Builder Pulse",
                )

    def test_accepts_only_current_and_historical_official_marketplace_sources(self) -> None:
        for source in (
            setup_builder_pulse.REPOSITORY,
            setup_builder_pulse.REPOSITORY.removesuffix(".git"),
            "https://github.com/udayanwalvekar/builder-pulse-plugin.git",
            "https://github.com/udayanwalvekar/builder-pulse-plugin",
        ):
            self.assertTrue(setup_builder_pulse.approved_existing_repository(source))

        for source in (
            "https://github.com/example/builder-pulse-plugin.git",
            "https://github.com/GrowthX-Club/builder-pulse-plugin-fake.git",
            "git@github.com:udayanwalvekar/builder-pulse-plugin.git",
            None,
        ):
            self.assertFalse(setup_builder_pulse.approved_existing_repository(source))

    def test_verified_git_checkout_rejects_wrong_origin_or_checkout_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for outputs, message in (
                (
                    [str(root), "https://github.com/example/builder-pulse.git"],
                    "unapproved origin",
                ),
                (
                    [str(root), setup_builder_pulse.REPOSITORY, " M scripts/setup.py"],
                    "modified, untracked, or ignored files",
                ),
                (
                    [str(root), setup_builder_pulse.REPOSITORY, "?? urllib/"],
                    "modified, untracked, or ignored files",
                ),
                (
                    [str(root), setup_builder_pulse.REPOSITORY, "!! json.py"],
                    "modified, untracked, or ignored files",
                ),
            ):
                with self.subTest(message=message), mock.patch.object(
                    setup_builder_pulse,
                    "run_command",
                    side_effect=outputs,
                ), self.assertRaisesRegex(setup_builder_pulse.SetupError, message):
                    setup_builder_pulse.verified_git_checkout(root)

    def test_verified_rollback_requires_matching_exact_checkouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed_cli = root / "installed" / "scripts" / "builder_pulse.py"
            marketplace_root = root / "marketplace"
            marketplace_root.mkdir()
            installation = {"version": "0.4.4"}
            marketplace = {
                "root": str(marketplace_root),
                "marketplaceSource": {
                    "source": setup_builder_pulse.REPOSITORY,
                },
            }
            with (
                mock.patch.object(
                    setup_builder_pulse,
                    "installed_cli",
                    return_value=installed_cli,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "verified_git_checkout",
                    side_effect=[
                        (setup_builder_pulse.REPOSITORY, "a" * 40),
                        (setup_builder_pulse.REPOSITORY, "b" * 40),
                    ],
                ),
                mock.patch.object(
                    setup_builder_pulse, "verify_remote_commit"
                ) as remote,
                self.assertRaisesRegex(
                    setup_builder_pulse.SetupError,
                    "provenance differ",
                ),
            ):
                setup_builder_pulse.verified_rollback_source(
                    installation, marketplace
                )
            remote.assert_not_called()

    def test_verified_rollback_pins_and_remotely_checks_full_commit(self) -> None:
        commit = "d" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed_cli = root / "installed" / "scripts" / "builder_pulse.py"
            marketplace_root = root / "marketplace"
            marketplace_root.mkdir()
            with (
                mock.patch.object(
                    setup_builder_pulse,
                    "installed_cli",
                    return_value=installed_cli,
                ),
                mock.patch.object(
                    setup_builder_pulse,
                    "verified_git_checkout",
                    side_effect=[
                        (setup_builder_pulse.REPOSITORY, commit),
                        (setup_builder_pulse.REPOSITORY, commit),
                    ],
                ),
                mock.patch.object(
                    setup_builder_pulse, "verify_remote_commit"
                ) as remote,
            ):
                result = setup_builder_pulse.verified_rollback_source(
                    {"version": "0.4.4"},
                    {
                        "root": str(marketplace_root),
                        "marketplaceSource": {
                            "source": setup_builder_pulse.REPOSITORY,
                        },
                    },
                )

        self.assertEqual(
            result,
            setup_builder_pulse.RollbackSource(
                "0.4.4", commit, setup_builder_pulse.REPOSITORY
            ),
        )
        remote.assert_called_once_with(setup_builder_pulse.REPOSITORY, commit)

    def test_unverified_previous_package_stops_before_pause_or_removal(self) -> None:
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            mock.patch.object(setup_builder_pulse, "verify_release_exists"),
            mock.patch.object(
                setup_builder_pulse,
                "installed_builder",
                return_value={"version": "0.4.4"},
            ),
            mock.patch.object(
                setup_builder_pulse,
                "marketplace_state",
                return_value={
                    "root": str(ROOT),
                    "marketplaceSource": {
                        "source": setup_builder_pulse.REPOSITORY,
                    },
                },
            ),
            mock.patch.object(
                setup_builder_pulse,
                "verified_rollback_source",
                side_effect=setup_builder_pulse.SetupError("unverified previous"),
            ),
            mock.patch.object(setup_builder_pulse, "pause_existing_capture") as pause,
            mock.patch.object(setup_builder_pulse, "remove_current") as remove,
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError, "unverified previous"
            ),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                ROOT,
                "Builder Pulse",
            )
        pause.assert_not_called()
        remove.assert_not_called()

    def test_setup_requires_a_confirmed_existing_project_and_name(self) -> None:
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "member-confirmed Builder Pulse project folder",
            ),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                "",
                "Builder Pulse",
            )

        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "confirmed Builder Pulse project folder does not exist",
            ),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                ROOT / "does-not-exist",
                "Builder Pulse",
            )

        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "confirmed Builder Pulse project name is invalid",
            ),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                ROOT,
                "",
            )


if __name__ == "__main__":
    unittest.main()
