from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib import error as urlerror


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
PREFLIGHT_AGENT_INSTALLATION_SUPPORT = (
    setup_builder_pulse.preflight_agent_installation_support
)
GIT_EXECUTABLE = shutil.which("git")


def codex_only_which(command: str) -> str | None:
    if command == "git":
        return GIT_EXECUTABLE
    return f"/usr/bin/{command}" if command == "codex" else None


def claude_only_which(command: str) -> str | None:
    if command == "git":
        return GIT_EXECUTABLE
    return f"/usr/bin/{command}" if command == "claude" else None


class SetupCaseBase(unittest.TestCase):
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
        self.preflight = mock.patch.object(
            setup_builder_pulse,
            "preflight_agent_installation_support",
        )
        self.preflight_mock = self.preflight.start()
        self.addCleanup(self.preflight.stop)
        # A plain project folder outside every refused location. Temporary
        # roots are refused by design, so the shared temporary-root rule is
        # disabled here and exercised on its own in EnrollmentRefusalTests.
        self.project_root = root / "project"
        self.project_root.mkdir()
        self.temporary_roots = mock.patch.object(
            setup_builder_pulse, "temporary_roots", return_value=()
        )
        self.temporary_roots.start()
        self.addCleanup(self.temporary_roots.stop)
        setup_builder_pulse.SETUP_LOG.path = None
        setup_builder_pulse.SETUP_LOG.secrets = []
        setup_builder_pulse.SETUP_LOG._buffer = []
        self.addCleanup(self.test_home.cleanup)


class SetupBuilderPulseTests(SetupCaseBase):
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
                (data_dir / filename).write_bytes(b"{}\n")

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
                self.assertEqual(
                    dict(paused.locations[0].queues),
                    {
                        "outbox.jsonl": b"{}\n",
                        "prompt-outbox.jsonl": b"{}\n",
                        "quarantine.jsonl": b"{}\n",
                    },
                )

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

    def test_local_pause_failure_restores_prior_files_and_server_policy(self) -> None:
        shared = setup_builder_pulse.canonical_plugin_data_dir()
        legacy = setup_builder_pulse.legacy_codex_plugin_data_dir()
        identity = {
            "installationId": "installation-1",
            "installationToken": "delivery-token",
            "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
        }
        snapshot = setup_builder_pulse.LocalCaptureSnapshot(
            shared,
            identity,
            None,
            {"enabled": True},
            (("prompt-outbox.jsonl", b'{"prompt":"preserve"}\n'),),
        )

        with (
            mock.patch.object(
                setup_builder_pulse,
                "existing_plugin_data_dir",
                return_value=shared,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "legacy_codex_plugin_data_dir",
                return_value=legacy,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "authoritative_identity",
                side_effect=[identity, identity],
            ),
            mock.patch.object(
                setup_builder_pulse,
                "pause_server_capture",
                return_value=True,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "pause_local_capture",
                side_effect=[
                    snapshot,
                    setup_builder_pulse.SetupError("legacy pause failed"),
                ],
            ),
            mock.patch.object(
                setup_builder_pulse,
                "restore_local_capture_snapshot",
            ) as restore_local,
            mock.patch.object(
                setup_builder_pulse,
                "resume_server_capture",
            ) as resume,
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "could not be disabled locally",
            ),
        ):
            legacy.mkdir(parents=True)
            setup_builder_pulse.pause_existing_capture({"version": "0.4.4"})

        restore_local.assert_called_once_with(snapshot)
        resume.assert_called_once_with(identity, "0.4.4")

    def test_restore_previous_capture_restores_exact_local_and_server_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory).resolve()
            identity = {
                "installationId": "installation-1",
                "installationToken": "delivery-token",
                "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
                "promptCapture": "on",
            }
            config = {"enabled": True, "customSetting": "preserve-me"}
            setup_builder_pulse.atomic_write_object(
                data_dir / "setup-paused-identity.json",
                identity,
            )
            setup_builder_pulse.atomic_write_object(
                data_dir / "identity.json",
                {"installationId": "installation-1", "promptCapture": "off"},
            )
            setup_builder_pulse.atomic_write_object(
                data_dir / "config.json",
                {"enabled": False, "customSetting": "preserve-me"},
            )
            queues = {
                "outbox.jsonl": b'{"event":"lifecycle"}\n',
                "prompt-outbox.jsonl": b'{"prompt":"preserve me"}\n',
                "quarantine.jsonl": b'{"event":"retry"}\n',
            }
            paused = setup_builder_pulse.PausedCapture(
                data_dir,
                identity,
                (
                    setup_builder_pulse.LocalCaptureSnapshot(
                        data_dir,
                        identity,
                        None,
                        config,
                        tuple(queues.items()),
                    ),
                ),
                True,
                "0.4.4",
            )

            with mock.patch.object(
                setup_builder_pulse,
                "resume_server_capture",
            ) as resume:
                setup_builder_pulse.restore_previous_capture(paused)

            self.assertEqual(
                setup_builder_pulse.read_object(data_dir / "identity.json"),
                identity,
            )
            self.assertEqual(
                setup_builder_pulse.read_object(data_dir / "config.json"),
                config,
            )
            self.assertFalse((data_dir / "setup-paused-identity.json").exists())
            for filename, contents in queues.items():
                self.assertEqual((data_dir / filename).read_bytes(), contents)
            resume.assert_called_once_with(identity, "0.4.4")

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
            server_pause.assert_called_once_with(identity, "0.5.2")
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
                self.project_root,
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
            "0.5.2",
        )
        calls = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any("claim" in arguments for arguments in calls))
        enroll_calls = [arguments for arguments in calls if "enroll" in arguments]
        self.assertEqual(len(enroll_calls), 1)
        self.assertEqual(
            enroll_calls[0][-6:],
            [
                "work",
                "enroll",
                "--root",
                str(self.project_root.resolve()),
                "--project",
                "Builder Pulse",
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
                self.project_root,
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
                self.project_root,
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
                self.project_root,
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
                        self.project_root,
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
                    "0.5.2",
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
                        self.project_root,
                        "Builder Pulse",
                    )

                resume.assert_called_once_with(identity, "0.5.2")
                repause.assert_called_once_with(identity, "0.5.2")
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
                self.project_root,
                "Builder Pulse",
            )

        resume.assert_called_once_with(identity, "0.5.2")
        repause.assert_called_once_with(identity, "0.5.2")
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

    def test_run_command_launches_the_resolved_windows_command_wrapper(self) -> None:
        completed = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        resolved = r"C:\\Users\\member\\AppData\\Local\\bin\\codex.cmd"
        with (
            mock.patch.object(
                setup_builder_pulse.shutil,
                "which",
                return_value=resolved,
            ),
            mock.patch.object(
                setup_builder_pulse.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            self.assertEqual(
                RUN_COMMAND(["codex", "plugin", "list"]),
                "ok\n",
            )

        self.assertEqual(
            run.call_args.args[0],
            [resolved, "plugin", "list"],
        )

    def test_preflight_failure_cannot_pause_or_remove_a_working_install(self) -> None:
        self.preflight_mock.side_effect = setup_builder_pulse.SetupError(
            "current marketplace format is incompatible"
        )
        with (
            mock.patch.object(
                setup_builder_pulse.shutil,
                "which",
                side_effect=lambda command: f"/usr/bin/{command}",
            ),
            mock.patch.object(
                setup_builder_pulse,
                "verify_release_exists",
                return_value=TARGET_COMMIT,
            ),
            mock.patch.object(setup_builder_pulse, "installed_builder") as installed,
            mock.patch.object(
                setup_builder_pulse, "pause_existing_capture"
            ) as pause,
            mock.patch.object(setup_builder_pulse, "remove_current") as remove,
            mock.patch.object(setup_builder_pulse, "install_release") as install,
            mock.patch.object(
                setup_builder_pulse, "install_claude_release"
            ) as install_claude,
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "marketplace format is incompatible",
            ),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                self.project_root,
                "Builder Pulse",
            )

        installed.assert_not_called()
        pause.assert_not_called()
        remove.assert_not_called()
        install.assert_not_called()
        install_claude.assert_not_called()

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
                    self.project_root,
                    "Builder Pulse",
                )

        cleanup.assert_called_once_with()
        install.assert_called_once_with(
            setup_builder_pulse.TARGET_RELEASE,
            expected_commit=TARGET_COMMIT,
        )
        restore.assert_called_once_with(rollback)

    def test_failed_codex_replacement_rolls_back_every_mutated_surface(self) -> None:
        rollback = setup_builder_pulse.RollbackSource(
            "0.4.4",
            "b" * 40,
            setup_builder_pulse.REPOSITORY,
        )
        previous_claude = [
            {
                "id": "builder-pulse-claude-posix@growthx-builder-tools-v0-4-6",
                "version": "0.4.6",
                "enabled": True,
            }
        ]
        paused = setup_builder_pulse.PausedCapture(ROOT, {})
        events: list[str] = []

        with (
            mock.patch.object(
                setup_builder_pulse.shutil,
                "which",
                side_effect=lambda command: f"/usr/bin/{command}",
            ),
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
            mock.patch.object(
                setup_builder_pulse,
                "installed_claude_builders",
                side_effect=[
                    previous_claude,
                    [
                        *previous_claude,
                        {
                            "id": setup_builder_pulse.target_claude_plugin_id(),
                            "version": "0.5.2",
                            "enabled": True,
                        },
                    ],
                ],
            ),
            mock.patch.object(
                setup_builder_pulse,
                "install_claude_release",
                side_effect=lambda *args, **kwargs: events.append("install claude"),
            ) as install_claude,
            mock.patch.object(
                setup_builder_pulse,
                "pause_existing_capture",
                side_effect=lambda previous: events.append("pause") or paused,
            ),
            mock.patch.object(setup_builder_pulse, "remove_current"),
            mock.patch.object(
                setup_builder_pulse,
                "install_release",
                side_effect=setup_builder_pulse.SetupError("codex replacement failed"),
            ),
            mock.patch.object(setup_builder_pulse, "cleanup_partial"),
            mock.patch.object(
                setup_builder_pulse,
                "install_verified_rollback",
                side_effect=lambda source: events.append("restore codex"),
            ) as restore_codex,
            mock.patch.object(
                setup_builder_pulse,
                "remove_claude_plugin",
                side_effect=lambda plugin_id: events.append("remove new claude"),
            ) as remove_claude,
            mock.patch.object(
                setup_builder_pulse,
                "restore_previous_capture",
                side_effect=lambda state: events.append("restore capture"),
            ) as restore_capture,
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "codex replacement failed",
            ),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                self.project_root,
                "Builder Pulse",
            )

        install_claude.assert_called_once_with(
            TARGET_COMMIT,
            existing_entries=previous_claude,
            remove_previous=False,
        )
        self.assertLess(events.index("install claude"), events.index("pause"))
        restore_codex.assert_called_once_with(rollback)
        remove_claude.assert_called_once_with(
            setup_builder_pulse.target_claude_plugin_id()
        )
        restore_capture.assert_called_once_with(paused)
        self.assertEqual(
            events[-3:],
            ["restore codex", "remove new claude", "restore capture"],
        )

    def test_activation_failure_rolls_back_packages_capture_and_queues(self) -> None:
        rollback = setup_builder_pulse.RollbackSource(
            "0.4.4",
            "b" * 40,
            setup_builder_pulse.REPOSITORY,
        )
        previous_claude = [
            {
                "id": "builder-pulse-claude-posix@growthx-builder-tools-v0-4-6",
                "version": "0.4.6",
                "enabled": True,
            }
        ]
        target = setup_builder_pulse.target_claude_plugin_id()
        identity = {
            "installationId": "installation-1",
            "installationToken": "token-1",
            "claimedEndpoint": setup_builder_pulse.DEFAULT_ENDPOINT,
        }
        snapshot = setup_builder_pulse.LocalCaptureSnapshot(
            ROOT,
            identity,
            None,
            {"enabled": True},
            (("prompt-outbox.jsonl", b'{"prompt":"queued"}\n'),),
        )
        paused = setup_builder_pulse.PausedCapture(
            ROOT,
            identity,
            (snapshot,),
            True,
            "0.4.4",
        )
        cli = ROOT / "scripts" / "builder_pulse.py"

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse.shutil,
                    "which",
                    side_effect=lambda command: f"/usr/bin/{command}",
                )
            )
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "verify_release_exists",
                    return_value=TARGET_COMMIT,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "installed_builder",
                    return_value={"version": "0.4.4"},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "marketplace_state",
                    return_value={
                        "marketplaceSource": {
                            "source": setup_builder_pulse.REPOSITORY
                        }
                    },
                )
            )
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "verified_rollback_source",
                    return_value=rollback,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "installed_claude_builders",
                    side_effect=[
                        previous_claude,
                        [
                            *previous_claude,
                            {"id": target, "version": "0.5.2", "enabled": True},
                        ],
                    ],
                )
            )
            stack.enter_context(
                mock.patch.object(setup_builder_pulse, "install_claude_release")
            )
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "pause_existing_capture",
                    return_value=paused,
                )
            )
            stack.enter_context(mock.patch.object(setup_builder_pulse, "remove_current"))
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "install_release",
                    return_value=cli,
                )
            )
            stack.enter_context(
                mock.patch.object(setup_builder_pulse, "restore_paused_identity")
            )
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "plugin_data_dir",
                    return_value=ROOT,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "authoritative_identity",
                    return_value=identity,
                )
            )
            stack.enter_context(
                mock.patch.object(setup_builder_pulse, "resume_server_capture")
            )
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "activate",
                    side_effect=setup_builder_pulse.SetupError("activation failed"),
                )
            )
            repause = stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "pause_server_capture",
                    return_value=True,
                )
            )
            stack.enter_context(
                mock.patch.object(setup_builder_pulse, "quarantine_local_capture")
            )
            stack.enter_context(mock.patch.object(setup_builder_pulse, "cleanup_partial"))
            restore_codex = stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "install_verified_rollback",
                )
            )
            remove_claude = stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "remove_claude_plugin",
                )
            )
            restore_capture = stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "restore_previous_capture",
                )
            )
            stack.enter_context(
                mock.patch.object(setup_builder_pulse, "run_command", return_value="")
            )
            stack.enter_context(
                self.assertRaisesRegex(
                    setup_builder_pulse.SetupError,
                    "activation failed",
                )
            )
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                self.project_root,
                "Builder Pulse",
            )

        repause.assert_called_once_with(identity, "0.5.2")
        restore_codex.assert_called_once_with(rollback)
        remove_claude.assert_called_once_with(target)
        restore_capture.assert_called_once_with(paused)

    def test_partial_unverified_claude_target_is_removed(self) -> None:
        previous_claude = [
            {
                "id": "builder-pulse-claude-posix@growthx-builder-tools-v0-4-6",
                "version": "0.4.6",
                "enabled": True,
            }
        ]
        target = setup_builder_pulse.target_claude_plugin_id()

        with (
            mock.patch.object(
                setup_builder_pulse.shutil,
                "which",
                side_effect=claude_only_which,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "verify_release_exists",
                return_value=TARGET_COMMIT,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "installed_claude_builders",
                side_effect=[
                    previous_claude,
                    [
                        *previous_claude,
                        {"id": target, "version": "0.5.2", "enabled": True},
                    ],
                ],
            ),
            mock.patch.object(
                setup_builder_pulse,
                "install_claude_release",
                side_effect=setup_builder_pulse.SetupError(
                    "installed tree verification failed"
                ),
            ),
            mock.patch.object(
                setup_builder_pulse,
                "remove_claude_plugin",
            ) as remove_claude,
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "installed tree verification failed",
            ),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                self.project_root,
                "Builder Pulse",
            )

        remove_claude.assert_called_once_with(target)

    def test_partial_claude_target_removal_is_attempted_when_lists_fail(self) -> None:
        previous_claude = [
            {
                "id": "builder-pulse-claude-posix@growthx-builder-tools-v0-4-6",
                "version": "0.4.6",
                "enabled": True,
            }
        ]
        target = setup_builder_pulse.target_claude_plugin_id()
        list_failure = setup_builder_pulse.SetupError(
            "Claude Code returned an invalid plugin list"
        )

        def fail_after_install_attempt(*args: object, **kwargs: object) -> None:
            setup_builder_pulse.installed_claude_builders()

        with (
            mock.patch.object(
                setup_builder_pulse.shutil,
                "which",
                side_effect=claude_only_which,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "verify_release_exists",
                return_value=TARGET_COMMIT,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "installed_claude_builders",
                side_effect=[previous_claude, list_failure, list_failure],
            ),
            mock.patch.object(
                setup_builder_pulse,
                "install_claude_release",
                side_effect=fail_after_install_attempt,
            ),
            mock.patch.object(
                setup_builder_pulse,
                "remove_claude_plugin",
            ) as remove_claude,
            self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                "Claude Code returned an invalid plugin list",
            ),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                self.project_root,
                "Builder Pulse",
            )

        remove_claude.assert_called_once_with(target)

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
                        self.project_root,
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
                    self.project_root,
                    "Builder Pulse",
                    reuse_existing_claim=True,
                )

            self.assertEqual(server_pause.call_count, 2)
            server_resume.assert_called_once_with(identity, "0.5.2")
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

    def test_claude_marketplace_accepts_the_current_git_checkout_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory) / "marketplace"
            (installed / ".claude-plugin").mkdir(parents=True)
            shutil.copy2(
                ROOT / ".claude-plugin" / "marketplace.json",
                installed / ".claude-plugin" / "marketplace.json",
            )
            shutil.copytree(ROOT / "claude-plugins", installed / "claude-plugins")
            commands = (
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
                    "current Claude marketplace layout",
                ],
            )
            for command in commands:
                subprocess.run(command, cwd=installed, check=True)
            expected_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=installed,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
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

    def test_agent_preflight_validates_both_package_formats_without_mutation(self) -> None:
        commands: list[list[str]] = []
        with mock.patch.object(
            setup_builder_pulse,
            "run_command",
            side_effect=lambda arguments: commands.append(arguments) or "",
        ):
            PREFLIGHT_AGENT_INSTALLATION_SUPPORT(
                codex_available=True,
                claude_available=True,
            )

        self.assertEqual(
            commands,
            [
                ["codex", "plugin", "marketplace", "add", "--help"],
                [
                    "claude",
                    "plugin",
                    "validate",
                    str(ROOT / ".claude-plugin" / "marketplace.json"),
                ],
                [
                    "claude",
                    "plugin",
                    "validate",
                    str(setup_builder_pulse.expected_claude_package_root()),
                ],
            ],
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
                "version": "0.5.2",
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
                / "0.5.2"
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
            self.assertEqual(runtime_module.PLUGIN_VERSION, "0.5.2")
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
            previous_claude = [
                {
                    "id": "builder-pulse-claude-posix@growthx-builder-tools-v0-5-0",
                    "version": "0.5.0",
                    "enabled": True,
                }
            ]

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
                    / "0.5.2"
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
                self.assertEqual(runtime_module.PLUGIN_VERSION, "0.5.2")
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
                    "installed_claude_builders",
                    return_value=previous_claude,
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
                    "remove_previous_claude_builders",
                ) as remove_previous_claude,
                mock.patch.object(
                    setup_builder_pulse,
                    "run_command",
                    side_effect=run_command,
                ),
            ):
                setup_builder_pulse.setup(
                    "InviteCode_1234567890",
                    setup_builder_pulse.DEFAULT_ENDPOINT,
                    self.project_root,
                    "Builder Pulse",
                )

            install_claude.assert_called_once_with(
                expected,
                existing_entries=previous_claude,
                remove_previous=False,
            )
            remove_previous_claude.assert_called_once_with(
                previous_claude,
                setup_builder_pulse.target_claude_plugin_id(),
            )
            resume.assert_called_once_with(identity, "0.5.2")
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
                    self.project_root,
                    "Builder Pulse",
                )

            self.assertEqual(
                installed_runtime,
                [shared / "runtime" / "0.5.2" / "scripts" / "builder_pulse.py"],
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
                    self.project_root,
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
                self.project_root,
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
                self.project_root,
                "",
            )


class SetupLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / ".builder-pulse"
        self.data_dir.mkdir()

    def test_log_redacts_tokens_bearers_invite_codes_and_home(self) -> None:
        log = setup_builder_pulse.SetupLog()
        invite = "InviteCode_1234567890"
        token = "a" * 64
        log.mask(invite)
        path = log.open(self.data_dir)
        assert path is not None
        log.write(
            "claim finished",
            stderr=f"code {invite} token {token} Authorization: Bearer abcdefghijkl",
            body='{"inviteCode": "' + invite + '", "installationToken": "' + token + '"}',
            env="BUILDER_PULSE_INVITE_CODE=" + invite,
        )
        content = path.read_text(encoding="utf-8")
        self.assertNotIn(invite, content)
        self.assertNotIn(token, content)
        self.assertNotIn("abcdefghijkl", content)
        self.assertIn("[redacted]", content)
        self.assertIn("claim finished", content)

    def test_log_never_contains_the_home_prefix_or_a_project_root(self) -> None:
        fake_home = Path("/Users/fakehome")
        project = "/Users/fakehome/code/my-secret-project"
        with mock.patch.object(setup_builder_pulse.Path, "home", return_value=fake_home):
            log = setup_builder_pulse.SetupLog()
            path = log.open(self.data_dir)
            assert path is not None
            shown = setup_builder_pulse.display_arguments(
                ["python3", "cli.py", "work", "enroll", "--root", project, "--project", "Name"]
            )
            log.write("command finished", argv=shown)
            log.write("codex package installed", cli="/Users/fakehome/.codex/plugins/cache/x")
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("/Users/fakehome", content)
        self.assertNotIn(project, content)
        self.assertNotIn("code/my-secret-project", content)
        self.assertIn("…/my-secret-project", content)
        self.assertIn("~/.codex/plugins/cache/x", content)

    def test_display_arguments_masks_secrets_and_folders(self) -> None:
        self.assertEqual(
            setup_builder_pulse.display_arguments(
                ["cli", "claim", "--code", "secret-code-value", "--root=/x/y/proj", "--project-root", "/a/b"]
            ),
            ["cli", "claim", "--code", "[redacted]", "--root=…/proj", "--project-root", "…/b"],
        )

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_log_file_and_directory_are_private(self) -> None:
        log = setup_builder_pulse.SetupLog()
        path = log.open(self.data_dir)
        assert path is not None
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_open_keeps_only_the_newest_ten_logs(self) -> None:
        logs = self.data_dir / "logs"
        logs.mkdir()
        for index in range(12):
            (logs / f"setup-20260101-0000{index:02d}.log").write_text("old\n")
        log = setup_builder_pulse.SetupLog()
        path = log.open(self.data_dir)
        remaining = sorted(entry.name for entry in logs.glob("setup-*.log"))
        self.assertEqual(len(remaining), setup_builder_pulse.SETUP_LOG_KEEP)
        assert path is not None
        self.assertIn(path.name, remaining)
        self.assertNotIn("setup-20260101-000000.log", remaining)

    def test_buffered_lines_are_flushed_when_the_log_opens(self) -> None:
        log = setup_builder_pulse.SetupLog()
        log.write("before open", step=1)
        path = log.open(self.data_dir)
        assert path is not None
        self.assertIn("before open", path.read_text(encoding="utf-8"))


class ProvenanceTests(unittest.TestCase):
    def test_checkout_is_pristine_accepts_only_allowlisted_noise(self) -> None:
        for porcelain, expected in (
            ("", True),
            ("?? .codex-marketplace-install.json\n", True),
            ("!! scripts/__pycache__/x.pyc\n", True),
            ("?? .DS_Store\n", True),
            ("!! scripts/builder_pulse.pyc\n", True),
            (" M scripts/builder_pulse.py\n", False),
            ("?? urllib/\n", False),
            ("!! json.py\n", False),
            ("?? .codex-marketplace-install.json\n M README.md\n", False),
            ("D  scripts/setup_builder_pulse.py\n", False),
        ):
            with self.subTest(porcelain=porcelain):
                self.assertIs(setup_builder_pulse.checkout_is_pristine(porcelain), expected)

    def test_verified_git_checkout_tolerates_codex_marketplace_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(
                setup_builder_pulse,
                "run_command",
                side_effect=[
                    str(root),
                    setup_builder_pulse.REPOSITORY,
                    "?? .codex-marketplace-install.json\n?? .DS_Store\n",
                    "c" * 40,
                ],
            ):
                self.assertEqual(
                    setup_builder_pulse.verified_git_checkout(root),
                    (setup_builder_pulse.REPOSITORY, "c" * 40),
                )

    def test_remote_commit_is_verified_with_a_shallow_git_fetch(self) -> None:
        commit = "d" * 40
        calls: list[list[str]] = []

        def run_command(arguments, *, env=None, expect_json=False):
            del env, expect_json
            calls.append(arguments)
            if arguments[-2:] == ["-t", commit]:
                return "commit\n"
            return ""

        with mock.patch.object(setup_builder_pulse, "run_command", side_effect=run_command):
            setup_builder_pulse.verify_remote_commit(setup_builder_pulse.REPOSITORY, commit)
        fetch = [arguments for arguments in calls if "fetch" in arguments][0]
        self.assertEqual(fetch[-1], commit)
        self.assertIn("https://github.com/GrowthX-Club/builder-pulse-plugin.git", fetch)
        self.assertIn("--depth", fetch)
        self.assertEqual(sum(1 for arguments in calls if "init" in arguments), 1)

    def test_remote_commit_probe_never_prompts_and_rejects_non_commit_objects(self) -> None:
        commit = "d" * 40
        environments: list[dict | None] = []

        def run_command(arguments, *, env=None, expect_json=False):
            del expect_json
            environments.append(env)
            if arguments[-2:] == ["-t", commit]:
                return "tag\n"
            return ""

        with mock.patch.object(setup_builder_pulse, "run_command", side_effect=run_command):
            with self.assertRaisesRegex(setup_builder_pulse.SetupError, "could not be verified"):
                setup_builder_pulse.verify_remote_commit(setup_builder_pulse.REPOSITORY, commit)
        self.assertTrue(environments)
        for env in environments:
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(env["GIT_ASKPASS"], "echo")

    def test_remote_commit_verification_fails_closed(self) -> None:
        commit = "d" * 40

        def failing(arguments, *, env=None, expect_json=False):
            del env, expect_json
            if "fetch" in arguments:
                raise setup_builder_pulse.SetupError("fatal: remote error: upload-pack: not our ref")
            return ""

        with mock.patch.object(setup_builder_pulse, "run_command", side_effect=failing):
            with self.assertRaisesRegex(setup_builder_pulse.SetupError, "could not be verified on GitHub"):
                setup_builder_pulse.verify_remote_commit(setup_builder_pulse.REPOSITORY, commit)
        with self.assertRaisesRegex(setup_builder_pulse.SetupError, "could not be verified"):
            setup_builder_pulse.verify_remote_commit(setup_builder_pulse.REPOSITORY, "not-a-sha")

    def test_release_verification_reports_a_github_rate_limit(self) -> None:
        error = urlerror.HTTPError(
            "https://api.github.com/x",
            403,
            "rate limited",
            http.client.HTTPMessage(),
            io.BytesIO(b"{}"),
        )
        with (
            mock.patch.object(setup_builder_pulse, "verified_remote_tag_commit", return_value="e" * 40),
            mock.patch.object(setup_builder_pulse.urlrequest, "urlopen", side_effect=error),
            self.assertRaisesRegex(setup_builder_pulse.SetupError, "rate limit"),
        ):
            setup_builder_pulse.verify_release_exists(setup_builder_pulse.TARGET_RELEASE)


class EnrollmentRefusalTests(unittest.TestCase):
    def test_temporary_folders_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            refusal = setup_builder_pulse.enrollment_refusal(root)
        self.assertIsNotNone(refusal)
        self.assertIn("temporary folder, not your project", refusal)

    def test_builder_pulse_checkout_and_its_subfolders_are_refused(self) -> None:
        refusal = setup_builder_pulse.enrollment_refusal(ROOT.resolve())
        self.assertIsNotNone(refusal)
        self.assertIn("is the Builder Pulse installer folder, not your project", refusal)
        nested = setup_builder_pulse.enrollment_refusal((ROOT / "scripts").resolve())
        self.assertIsNotNone(nested)
        self.assertIn("installer folder", nested)

    def test_plain_project_folder_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "project"
            root.mkdir()
            with mock.patch.object(setup_builder_pulse, "temporary_roots", return_value=()):
                self.assertIsNone(setup_builder_pulse.enrollment_refusal(root))


class ReviewFlowTests(SetupCaseBase):
    def repair_stack(self, stack: contextlib.ExitStack, activation: dict, run_command):
        cli = ROOT / "scripts" / "builder_pulse.py"
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
        stack.enter_context(mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "verify_release_exists", return_value=TARGET_COMMIT))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "installed_builder", return_value=None))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "marketplace_state", return_value=None))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "verified_rollback_source", return_value=None))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "existing_plugin_data_dir", return_value=ROOT))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "plugin_data_dir", return_value=ROOT))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "authoritative_identity", return_value=identity))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "claimed_identity", return_value=setup_builder_pulse.claimed_identity_fields(status_identity)))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "pause_existing_capture", return_value=None))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "remove_current"))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "install_release", return_value=cli))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "resume_server_capture"))
        stack.enter_context(mock.patch.object(setup_builder_pulse, "activate", return_value=activation))
        run = stack.enter_context(mock.patch.object(setup_builder_pulse, "run_command", side_effect=run_command))
        repause = stack.enter_context(mock.patch.object(setup_builder_pulse, "pause_server_capture"))
        quarantine = stack.enter_context(mock.patch.object(setup_builder_pulse, "quarantine_local_capture"))
        cleanup = stack.enter_context(mock.patch.object(setup_builder_pulse, "cleanup_partial"))
        rollback = stack.enter_context(mock.patch.object(setup_builder_pulse, "install_verified_rollback"))
        return cli, run, repause, quarantine, cleanup, rollback

    def test_review_required_keeps_the_install_active_and_reports_it(self) -> None:
        calls: list[list[str]] = []

        def run_command(arguments, *, env=None, expect_json=False):
            del env, expect_json
            calls.append(arguments)
            return ""

        activation = {
            "connected": False,
            "activationReady": False,
            "reviewRequired": True,
            "hookStatus": "modified",
            "agentPlatform": "codex",
            "detail": "run /hooks",
        }
        with contextlib.ExitStack() as stack:
            cli, run, repause, quarantine, cleanup, rollback = self.repair_stack(
                stack, activation, run_command
            )
            outcome = setup_builder_pulse.setup(
                "",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                "",
                "",
                reuse_existing_claim=True,
            )
        self.assertEqual(outcome.review_required, ("codex",))
        self.assertEqual(outcome.cli, cli)
        self.assertIsNone(outcome.enrolled_root)
        repause.assert_not_called()
        quarantine.assert_not_called()
        cleanup.assert_not_called()
        rollback.assert_not_called()
        enabled_calls = [arguments[-1] for arguments in calls if arguments[-4:-1] == ["config", "set", "enabled"]]
        self.assertEqual(enabled_calls[-1], "true")
        self.assertTrue(any(arguments[-1] == "flush" for arguments in calls))
        self.assertFalse(any("enroll" in arguments for arguments in calls))

    def test_non_review_activation_failure_still_rolls_back(self) -> None:
        activation = {
            "connected": False,
            "activationReady": False,
            "reviewRequired": False,
            "hookStatus": "not_loaded",
            "agentPlatform": "codex",
            "detail": "no hooks",
        }
        with contextlib.ExitStack() as stack:
            cli, run, repause, quarantine, cleanup, rollback = self.repair_stack(
                stack, activation, lambda arguments, *, env=None, expect_json=False: ""
            )
            with self.assertRaisesRegex(
                setup_builder_pulse.SetupError,
                r"not verified for Codex \(hookStatus=not_loaded; detail=no hooks\)",
            ):
                setup_builder_pulse.setup(
                    "",
                    setup_builder_pulse.DEFAULT_ENDPOINT,
                    "",
                    "",
                    reuse_existing_claim=True,
                )
        repause.assert_called_once()
        quarantine.assert_called_once()
        cleanup.assert_called_once()

    def test_repair_enrolls_only_an_explicitly_named_folder(self) -> None:
        calls: list[list[str]] = []

        def run_command(arguments, *, env=None, expect_json=False):
            del env, expect_json
            calls.append(arguments)
            return ""

        ready = {"activationReady": True, "hooksTrusted": True, "serverVerified": True}
        with contextlib.ExitStack() as stack:
            self.repair_stack(stack, ready, run_command)
            outcome = setup_builder_pulse.setup(
                "",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                self.project_root,
                "My Project",
                reuse_existing_claim=True,
            )
        self.assertEqual(outcome.enrolled_root, self.project_root.resolve())
        enroll = [arguments for arguments in calls if "enroll" in arguments]
        self.assertEqual(len(enroll), 1)
        self.assertNotIn("--replace-existing", enroll[0])

    def test_setup_mode_refuses_the_installer_clone_and_temporary_folders(self) -> None:
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
            self.assertRaisesRegex(setup_builder_pulse.SetupError, "installer folder, not your project"),
        ):
            setup_builder_pulse.setup(
                "InviteCode_1234567890",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                ROOT,
                "Builder Pulse",
            )
        self.temporary_roots.stop()
        try:
            with (
                mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which),
                self.assertRaisesRegex(setup_builder_pulse.SetupError, "temporary folder, not your project"),
            ):
                setup_builder_pulse.setup(
                    "InviteCode_1234567890",
                    setup_builder_pulse.DEFAULT_ENDPOINT,
                    self.project_root,
                    "Builder Pulse",
                )
        finally:
            self.temporary_roots.start()


class MainEntrypointTests(SetupCaseBase):
    def test_review_outcome_exits_3_with_hooks_instructions_and_no_success_line(self) -> None:
        cli = ROOT / "scripts" / "builder_pulse.py"
        outcome = setup_builder_pulse.SetupOutcome(cli, self.project_root, ("codex",))
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["setup", "--reuse-existing-claim"]),
            mock.patch.object(sys.stdin, "isatty", return_value=False),
            mock.patch.object(setup_builder_pulse, "setup", return_value=outcome),
            mock.patch.object(setup_builder_pulse, "run_command", return_value="harness-project\n"),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(setup_builder_pulse.main(), 3)
        self.assertIn("/hooks", stderr.getvalue())
        self.assertIn("has not approved its hooks yet", stderr.getvalue())
        self.assertIn("activate --agent codex", stderr.getvalue())
        self.assertIn("Details: ", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("harness-project", stderr.getvalue())

    def test_failure_exit_1_ends_with_a_details_line_and_a_private_log(self) -> None:
        data_dir = Path(os.environ["BUILDER_PULSE_DATA_DIR"])
        self.assertFalse(data_dir.exists())
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["setup", "--reuse-existing-claim"]),
            mock.patch.object(sys.stdin, "isatty", return_value=False),
            mock.patch.object(
                setup_builder_pulse,
                "setup",
                side_effect=setup_builder_pulse.SetupError("Builder Pulse activation failed (hookStatus=not_loaded)"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(setup_builder_pulse.main(), 1)
        lines = [line for line in stderr.getvalue().splitlines() if line.strip()]
        self.assertTrue(lines[-1].startswith("Details: "), lines[-1])
        log_path = Path(lines[-1].removeprefix("Details: "))
        self.assertTrue(log_path.is_file())
        # The log always lives at the stable shared location, even when the
        # failure happened before the shared directory existed; only the
        # secret-free logs directory is created for it.
        self.assertEqual(log_path.parent, data_dir.resolve() / "logs")
        self.assertEqual({entry.name for entry in data_dir.iterdir()}, {"logs"})
        self.assertIn("hookStatus=not_loaded", log_path.read_text(encoding="utf-8"))
        self.assertIn("Builder Pulse setup stopped: Builder Pulse activation failed", stderr.getvalue())

    def test_success_prints_the_completion_sentence_last(self) -> None:
        cli = ROOT / "scripts" / "builder_pulse.py"
        outcome = setup_builder_pulse.SetupOutcome(cli, None, ())
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["setup", "--reuse-existing-claim"]),
            mock.patch.object(sys.stdin, "isatty", return_value=False),
            mock.patch.object(setup_builder_pulse, "setup", return_value=outcome),
            mock.patch.object(setup_builder_pulse, "run_command", return_value=""),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(setup_builder_pulse.main(), 0)
        self.assertTrue(
            stdout.getvalue().strip().endswith("verify separate server receipts.")
        )
        self.assertIn("prior confirmed projects were kept unchanged", stdout.getvalue())

    def test_interactive_repair_defaults_to_no_new_enrollment(self) -> None:
        captured: dict = {}

        def fake_setup(invite_code, endpoint, project_root, project_label, *, reuse_existing_claim=False):
            captured.update(root=project_root, label=project_label, reuse=reuse_existing_claim)
            return setup_builder_pulse.SetupOutcome(ROOT / "scripts" / "builder_pulse.py", None, ())

        with (
            mock.patch.object(sys, "argv", ["setup", "--reuse-existing-claim"]),
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", return_value=""),
            mock.patch.object(setup_builder_pulse, "setup", side_effect=fake_setup),
            mock.patch.object(setup_builder_pulse, "run_command", return_value=""),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(setup_builder_pulse.main(), 0)
        self.assertEqual(captured, {"root": "", "label": "", "reuse": True})

    def test_interactive_prompt_never_defaults_to_the_installer_clone(self) -> None:
        captured: dict = {}
        answers = iter(["", str(self.project_root), "My Project"])

        def fake_setup(invite_code, endpoint, project_root, project_label, *, reuse_existing_claim=False):
            captured.update(root=project_root, label=project_label)
            return setup_builder_pulse.SetupOutcome(ROOT / "scripts" / "builder_pulse.py", Path(project_root), ())

        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["setup"]),
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch.dict(setup_builder_pulse.os.environ, {"BUILDER_PULSE_INVITE_CODE": "InviteCode_1234567890"}),
            mock.patch.object(setup_builder_pulse.Path, "cwd", return_value=ROOT),
            mock.patch("builtins.input", side_effect=lambda _prompt="": next(answers)),
            mock.patch.object(setup_builder_pulse, "setup", side_effect=fake_setup),
            mock.patch.object(setup_builder_pulse, "run_command", return_value=""),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(setup_builder_pulse.main(), 0)
        self.assertIn("installer clone, not your project", stderr.getvalue())
        self.assertNotIn(f"- Current folder: {ROOT}", stderr.getvalue())
        self.assertEqual(captured, {"root": str(self.project_root), "label": "My Project"})


class RealDirectoryRepairMixin:
    def claimed_identity(self) -> dict:
        return {
            "installationId": "11111111-2222-4333-8444-555555555555",
            "builderId": "builder-legacy",
            "memberId": "member-legacy",
            "builderName": "Legacy Member",
            "installationToken": "f" * 64,
            "claimedEndpoint": "https://pulse.example",
            "promptCapture": "on",
        }

    def repair_with_real_directories(self):
        cli = ROOT / "scripts" / "builder_pulse.py"
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(setup_builder_pulse.shutil, "which", side_effect=codex_only_which))
            stack.enter_context(mock.patch.object(setup_builder_pulse, "verify_release_exists", return_value=TARGET_COMMIT))
            stack.enter_context(mock.patch.object(setup_builder_pulse, "installed_builder", return_value={"version": "0.4.6"}))
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "marketplace_state",
                    return_value={"marketplaceSource": {"source": setup_builder_pulse.REPOSITORY}},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "verified_rollback_source",
                    return_value=setup_builder_pulse.RollbackSource("0.4.6", "a" * 40, setup_builder_pulse.REPOSITORY),
                )
            )
            stack.enter_context(mock.patch.object(setup_builder_pulse, "pause_server_capture", return_value=True))
            stack.enter_context(mock.patch.object(setup_builder_pulse, "resume_server_capture"))
            stack.enter_context(mock.patch.object(setup_builder_pulse, "remove_current"))
            stack.enter_context(mock.patch.object(setup_builder_pulse, "install_release", return_value=cli))
            stack.enter_context(
                mock.patch.object(
                    setup_builder_pulse,
                    "activate",
                    return_value={"activationReady": True, "hooksTrusted": True, "serverVerified": True},
                )
            )
            return setup_builder_pulse.setup(
                "",
                setup_builder_pulse.DEFAULT_ENDPOINT,
                "",
                "",
                reuse_existing_claim=True,
            )



class LegacyIdentityRepairTests(RealDirectoryRepairMixin, SetupCaseBase):
    def test_repair_recovers_an_identity_that_exists_only_in_the_legacy_directory(self) -> None:
        legacy = setup_builder_pulse.legacy_codex_plugin_data_dir()
        legacy.mkdir(parents=True)
        identity = self.claimed_identity()
        (legacy / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
        shared = setup_builder_pulse.canonical_plugin_data_dir()
        self.assertFalse(shared.exists())

        outcome = self.repair_with_real_directories()

        self.assertEqual(outcome.review_required, ())
        restored = json.loads((shared / "identity.json").read_text(encoding="utf-8"))
        self.assertEqual(restored["installationId"], identity["installationId"])
        self.assertEqual(restored["builderId"], identity["builderId"])
        self.assertEqual(restored["installationToken"], identity["installationToken"])
        self.assertFalse((shared / "setup-paused-identity.json").exists())
        self.assertTrue((legacy / "identity.json").is_file())
        self.assertEqual(
            json.loads((legacy / "setup-paused-identity.json").read_text(encoding="utf-8"))["installationId"],
            identity["installationId"],
        )
        log_text = (setup_builder_pulse.SETUP_LOG.path or Path("/nonexistent")).read_text(encoding="utf-8")
        self.assertIn('"source": "legacy"', log_text)
        self.assertNotIn("f" * 64, log_text)

    def test_repair_still_fails_closed_when_no_directory_holds_an_identity(self) -> None:
        legacy = setup_builder_pulse.legacy_codex_plugin_data_dir()
        legacy.mkdir(parents=True)
        (legacy / "config.json").write_text("{}", encoding="utf-8")
        shared = setup_builder_pulse.canonical_plugin_data_dir()
        shared.mkdir(parents=True)
        (shared / "logs").mkdir()
        with self.assertRaisesRegex(setup_builder_pulse.SetupError, "not fully claimed"):
            self.repair_with_real_directories()

    def test_repair_identity_dir_prefers_a_shared_claim_over_legacy(self) -> None:
        legacy = setup_builder_pulse.legacy_codex_plugin_data_dir()
        legacy.mkdir(parents=True)
        (legacy / "identity.json").write_text(
            json.dumps({"installationId": "legacy", "builderId": "builder-1"}), encoding="utf-8"
        )
        shared = setup_builder_pulse.canonical_plugin_data_dir()
        self.assertEqual(setup_builder_pulse.repair_identity_dir(None), legacy)
        shared.mkdir(parents=True)
        (shared / "logs").mkdir()
        self.assertEqual(setup_builder_pulse.repair_identity_dir(None), legacy)
        # an unclaimed skeleton does not count
        (shared / "identity.json").write_text(
            json.dumps({"installationId": "skeleton", "promptCapture": "off"}), encoding="utf-8"
        )
        self.assertEqual(setup_builder_pulse.repair_identity_dir(None), legacy)
        (shared / "setup-paused-identity.json").write_text(
            json.dumps({"installationId": "shared", "pendingInstallationToken": "c" * 64}),
            encoding="utf-8",
        )
        self.assertEqual(setup_builder_pulse.repair_identity_dir(None), shared)

    def test_migration_merges_into_a_logs_only_shared_directory_and_refuses_more(self) -> None:
        legacy = setup_builder_pulse.legacy_codex_plugin_data_dir()
        legacy.mkdir(parents=True)
        identity = self.claimed_identity()
        (legacy / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
        (legacy / "contexts.json").write_text("{}", encoding="utf-8")
        shared = setup_builder_pulse.canonical_plugin_data_dir()
        (shared / "logs").mkdir(parents=True)
        (shared / "logs" / "setup-20260101-000000.log").write_text("earlier failure\n")

        self.assertEqual(setup_builder_pulse.migrate_existing_data_to_shared(None), shared)
        self.assertEqual(
            json.loads((shared / "identity.json").read_text(encoding="utf-8")), identity
        )
        self.assertTrue((shared / "contexts.json").is_file())
        self.assertTrue((shared / "logs" / "setup-20260101-000000.log").is_file())

        partial = shared.parent / "partial-shared"
        (partial / "logs").mkdir(parents=True)
        (partial / "contexts.json").write_text("{}", encoding="utf-8")
        with mock.patch.dict(setup_builder_pulse.os.environ, {"BUILDER_PULSE_DATA_DIR": str(partial)}):
            with self.assertRaisesRegex(setup_builder_pulse.SetupError, "exists without the prior identity"):
                setup_builder_pulse.migrate_existing_data_to_shared(None)


class SharedSkeletonTests(RealDirectoryRepairMixin, SetupCaseBase):
    def skeleton(self) -> dict:
        return {"installationId": "99999999-8888-4777-8666-555555555555", "promptCapture": "off"}

    def test_repair_ignores_an_unclaimed_shared_skeleton(self) -> None:
        legacy = setup_builder_pulse.legacy_codex_plugin_data_dir()
        legacy.mkdir(parents=True)
        identity = self.claimed_identity()
        (legacy / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
        shared = setup_builder_pulse.canonical_plugin_data_dir()
        (shared / "logs").mkdir(parents=True)
        (shared / "identity.json").write_text(json.dumps(self.skeleton()), encoding="utf-8")
        (shared / ".lock").write_bytes(b"")

        outcome = self.repair_with_real_directories()

        self.assertEqual(outcome.review_required, ())
        restored = json.loads((shared / "identity.json").read_text(encoding="utf-8"))
        self.assertEqual(restored["installationId"], identity["installationId"])
        self.assertEqual(restored["installationToken"], identity["installationToken"])

    def test_skeleton_without_a_legacy_claim_is_not_a_claim(self) -> None:
        shared = setup_builder_pulse.canonical_plugin_data_dir()
        (shared / "logs").mkdir(parents=True)
        (shared / "setup-paused-identity.json").write_text(
            json.dumps({"installationId": self.skeleton()["installationId"]}), encoding="utf-8"
        )
        with self.assertRaisesRegex(setup_builder_pulse.SetupError, "not fully claimed"):
            self.repair_with_real_directories()

    def test_two_different_claimed_identities_still_differ(self) -> None:
        legacy = setup_builder_pulse.legacy_codex_plugin_data_dir()
        legacy.mkdir(parents=True)
        (legacy / "identity.json").write_text(json.dumps(self.claimed_identity()), encoding="utf-8")
        shared = setup_builder_pulse.canonical_plugin_data_dir()
        shared.mkdir(parents=True)
        other = dict(self.claimed_identity())
        other["installationId"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        other["builderId"] = "builder-other"
        (shared / "identity.json").write_text(json.dumps(other), encoding="utf-8")
        with self.assertRaisesRegex(setup_builder_pulse.SetupError, "identities differ"):
            self.repair_with_real_directories()

    def test_partial_migration_leaves_no_identity_and_the_retry_migrates_everything(self) -> None:
        legacy = setup_builder_pulse.legacy_codex_plugin_data_dir()
        legacy.mkdir(parents=True)
        identity = self.claimed_identity()
        (legacy / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
        (legacy / "contexts.json").write_text('{"projects": 1}', encoding="utf-8")
        shared = setup_builder_pulse.canonical_plugin_data_dir()
        (shared / "logs").mkdir(parents=True)
        (shared / "logs" / "setup-20260101-000000.log").write_text("earlier\n")
        real_copytree = shutil.copytree

        def failing_copytree(source, destination, **kwargs):
            destination = Path(destination)
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(source) / "identity.json", destination / "identity.json")
            raise OSError(28, "No space left on device")

        with mock.patch.object(setup_builder_pulse.shutil, "copytree", side_effect=failing_copytree):
            with self.assertRaisesRegex(setup_builder_pulse.SetupError, "could not be migrated safely"):
                setup_builder_pulse.migrate_existing_data_to_shared(None)
        self.assertFalse((shared / "identity.json").exists())
        self.assertFalse((shared / "contexts.json").exists())
        self.assertTrue((shared / "logs" / "setup-20260101-000000.log").is_file())
        self.assertFalse((shared.parent / f".{shared.name}-migration").exists())

        self.assertEqual(setup_builder_pulse.migrate_existing_data_to_shared(None), shared)
        self.assertEqual(json.loads((shared / "identity.json").read_text(encoding="utf-8")), identity)
        self.assertEqual((shared / "contexts.json").read_text(encoding="utf-8"), '{"projects": 1}')
        self.assertTrue((shared / "logs" / "setup-20260101-000000.log").is_file())
        del real_copytree


if __name__ == "__main__":
    unittest.main()
