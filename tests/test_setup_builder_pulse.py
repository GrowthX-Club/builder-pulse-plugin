from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "setup_builder_pulse", ROOT / "scripts" / "setup_builder_pulse.py"
)
assert SPEC is not None and SPEC.loader is not None
setup_builder_pulse = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup_builder_pulse)


class SetupBuilderPulseTests(unittest.TestCase):
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
            mock.patch.object(setup_builder_pulse, "install_release", return_value=cli) as install,
            mock.patch.object(
                setup_builder_pulse,
                "activate",
                return_value={
                    "connected": True,
                    "hooksTrusted": True,
                    "serverVerified": True,
                },
            ) as activate,
            mock.patch.object(setup_builder_pulse, "run_command", side_effect=run_command) as run,
        ):
            setup_builder_pulse.setup(invite_code, setup_builder_pulse.DEFAULT_ENDPOINT)

        verify.assert_called_once_with(setup_builder_pulse.TARGET_RELEASE)
        remove.assert_called_once_with(plugin_installed=True, marketplace_configured=True)
        install.assert_called_once_with(setup_builder_pulse.TARGET_RELEASE)
        activate.assert_called_once_with(cli)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any("claim" in arguments for arguments in calls))
        self.assertTrue(any(arguments[-1] == "flush" for arguments in calls))

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
                )

        cleanup.assert_called_once_with()
        self.assertEqual(
            [call.args[0] for call in install.call_args_list],
            [setup_builder_pulse.TARGET_RELEASE, "v0.4.4"],
        )

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
                )


if __name__ == "__main__":
    unittest.main()
