from __future__ import annotations

import importlib.util
import json
from pathlib import Path, PureWindowsPath
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


class SetupBuilderPulseTests(unittest.TestCase):
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

    def test_setup_reinstalls_current_release_and_keeps_code_out_of_arguments(self) -> None:
        invite_code = "InviteCode_1234567890"
        cli = ROOT / "scripts" / "builder_pulse.py"

        def run_command(arguments, *, env=None, expect_json=False):
            del expect_json
            if arguments[-1] == "claim":
                self.fail("claim arguments unexpectedly ended with claim")
            if "claim" in arguments:
                self.assertNotIn(invite_code, arguments)
                self.assertEqual(env["BUILDER_PULSE_INVITE_CODE"], invite_code)
            return ""

        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", return_value="ok"),
            mock.patch.object(setup_builder_pulse, "verify_release_exists") as verify,
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
            mock.patch.object(setup_builder_pulse, "remove_current") as remove,
            mock.patch.object(
                setup_builder_pulse, "pause_existing_capture"
            ) as pause,
            mock.patch.object(setup_builder_pulse, "install_release", return_value=cli) as install,
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
            rollback_version="0.4.4",
        )
        pause.assert_called_once_with({"version": "0.4.4"})
        install.assert_called_once_with(setup_builder_pulse.TARGET_RELEASE)
        activate.assert_called_once_with(cli)
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
                str(ROOT.resolve()),
                "--project",
                "Builder Pulse",
            ],
        )
        self.assertTrue(
            any(arguments[-4:] == ["config", "set", "enabled", "true"] for arguments in calls)
        )
        self.assertTrue(any(arguments[-1] == "flush" for arguments in calls))

    def test_setup_rejects_home_as_an_enrollment_root(self) -> None:
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", return_value="ok"),
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
            mock.patch.object(setup_builder_pulse.shutil, "which", return_value="ok"),
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

        with mock.patch.object(setup_builder_pulse, "run_command") as run, \
            mock.patch.object(
                setup_builder_pulse.urlrequest,
                "urlopen",
                return_value=response,
            ) as opened:
            setup_builder_pulse.verify_release_exists("v0.4.6")

        self.assertEqual(run.call_args.args[0][-1], "refs/tags/v0.4.6")
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

        with mock.patch.object(setup_builder_pulse, "run_command"), \
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

                def activate(_cli: Path):
                    if failure == "activation":
                        raise setup_builder_pulse.SetupError("activation failed")
                    return {
                        "activationReady": True,
                        "hooksTrusted": True,
                        "serverVerified": True,
                    }

                with (
                    mock.patch.object(
                        setup_builder_pulse.shutil, "which", return_value="ok"
                    ),
                    mock.patch.object(setup_builder_pulse, "verify_release_exists"),
                    mock.patch.object(
                        setup_builder_pulse, "installed_builder", return_value=None
                    ),
                    mock.patch.object(
                        setup_builder_pulse, "marketplace_state", return_value=None
                    ),
                    mock.patch.object(setup_builder_pulse, "pause_existing_capture"),
                    mock.patch.object(setup_builder_pulse, "remove_current"),
                    mock.patch.object(
                        setup_builder_pulse, "install_release", return_value=cli
                    ),
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
                        str(cli),
                        "config",
                        "set",
                        "enabled",
                        "false",
                    ],
                    calls,
                )

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
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", return_value="ok"),
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
                    "marketplaceSource": {
                        "source": setup_builder_pulse.REPOSITORY,
                    }
                },
            ),
            mock.patch.object(setup_builder_pulse, "pause_existing_capture"),
            mock.patch.object(setup_builder_pulse, "remove_current"),
            mock.patch.object(setup_builder_pulse, "cleanup_partial") as cleanup,
            mock.patch.object(
                setup_builder_pulse,
                "install_release",
                side_effect=[
                    setup_builder_pulse.SetupError("update failed"),
                    ROOT / "scripts" / "builder_pulse.py",
                ],
            ) as install,
        ):
            with self.assertRaisesRegex(setup_builder_pulse.SetupError, "update failed"):
                setup_builder_pulse.setup(
                    "InviteCode_1234567890",
                    setup_builder_pulse.DEFAULT_ENDPOINT,
                    ROOT,
                    "Builder Pulse",
                )

        cleanup.assert_called_once_with()
        self.assertEqual(
            [call.args[0] for call in install.call_args_list],
            [setup_builder_pulse.TARGET_RELEASE, "v0.4.4"],
        )

    def test_partial_removal_failure_restores_plugin_from_existing_marketplace(self) -> None:
        state = {"plugin": True, "marketplace": True}
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = Path(directory)
            (plugin_root / "scripts").mkdir()
            (plugin_root / "scripts" / "builder_pulse.py").touch()
            (plugin_root / ".codex-plugin").mkdir()
            (plugin_root / ".codex-plugin" / "plugin.json").write_text(
                '{"version":"0.4.4"}',
                encoding="utf-8",
            )

            def run_command(arguments, *, env=None, expect_json=False):
                del env
                if arguments[:3] == ["codex", "plugin", "remove"]:
                    state["plugin"] = False
                    return ""
                if arguments[:4] == ["codex", "plugin", "marketplace", "remove"]:
                    self.assertTrue(state["marketplace"])
                    raise setup_builder_pulse.SetupError("marketplace removal failed")
                if arguments[:3] == ["codex", "plugin", "add"]:
                    self.assertTrue(expect_json)
                    self.assertTrue(state["marketplace"])
                    state["plugin"] = True
                    return {"installedPath": str(plugin_root)}
                self.fail(f"Unexpected command: {arguments}")

            with mock.patch.object(
                setup_builder_pulse,
                "run_command",
                side_effect=run_command,
            ):
                with self.assertRaisesRegex(
                    setup_builder_pulse.SetupError,
                    "marketplace removal failed",
                ):
                    setup_builder_pulse.remove_current(
                        plugin_installed=True,
                        marketplace_configured=True,
                        rollback_version="0.4.4",
                    )

        self.assertTrue(state["plugin"])
        self.assertTrue(state["marketplace"])

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

    def test_rejects_marketplace_name_pointing_to_another_repository(self) -> None:
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", return_value="ok"),
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

    def test_setup_requires_a_confirmed_existing_project_and_name(self) -> None:
        with (
            mock.patch.object(setup_builder_pulse.shutil, "which", return_value="ok"),
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
            mock.patch.object(setup_builder_pulse.shutil, "which", return_value="ok"),
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
            mock.patch.object(setup_builder_pulse.shutil, "which", return_value="ok"),
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
