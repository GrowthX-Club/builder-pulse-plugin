#!/usr/bin/env python3
"""Install or update Builder Pulse through one stable bootstrap command."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


REPOSITORY = "https://github.com/GrowthX-Club/builder-pulse-plugin.git"
APPROVED_EXISTING_REPOSITORIES = {
    REPOSITORY,
    REPOSITORY.removesuffix(".git"),
    "https://github.com/udayanwalvekar/builder-pulse-plugin.git",
    "https://github.com/udayanwalvekar/builder-pulse-plugin",
}
MARKETPLACE = "growthx-builder-tools"
PLUGIN = f"builder-pulse@{MARKETPLACE}"
TARGET_RELEASE = "v0.4.6"
DEFAULT_ENDPOINT = "https://precious-ant-429.convex.site"
SETUP_DISCLOSURE = (
    "Builder Pulse is installed machine-wide, but it sends data only from project "
    "folders you explicitly enroll. GrowthX links telemetry to your claimed GrowthX "
    "member record. For each enrolled project, it receives a stable installation ID, "
    "a one-way hashed session ID, the display name you confirm and a sanitized project "
    "ID, any feature name and ID you explicitly set, coarse work state and event/activity "
    "timestamps, plugin version, optional cumulative token counts, and each primary "
    "prompt you submit after secret redaction and a 64 KiB limit. GrowthX's authenticated "
    "Builder Pulse admins can view this data for learning feedback. Raw lifecycle events "
    "and activity buckets are retained for 30 days; submitted prompts and their feedback "
    "are retained for 60 days; the installation/member link, latest status, and compacted "
    "session, daily, and all-time "
    "token aggregates remain until GrowthX removes them. It does not send "
    "folder paths, files, patches, commands, tool input or output, assistant replies, "
    "transcripts, or environment variables."
)


class SetupError(RuntimeError):
    pass


def run_command(
    arguments: list[str],
    *,
    env: dict[str, str] | None = None,
    expect_json: bool = False,
) -> Any:
    completed = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SetupError(detail or f"Command failed: {arguments[0]}")
    if not expect_json:
        return completed.stdout
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SetupError(f"{arguments[0]} returned invalid JSON") from exc


def installed_builder() -> dict[str, Any] | None:
    response = run_command(["codex", "plugin", "list", "--json"], expect_json=True)
    installed = response.get("installed") if isinstance(response, dict) else None
    if not isinstance(installed, list):
        raise SetupError("Codex returned an invalid plugin list")
    matches = [
        item
        for item in installed
        if isinstance(item, dict) and item.get("pluginId") == PLUGIN
    ]
    if len(matches) > 1:
        raise SetupError("Codex reported more than one Builder Pulse installation")
    return matches[0] if matches else None


def installed_cli(installation: dict[str, Any]) -> Path:
    version = installation.get("version")
    if not isinstance(version, str) or not version or any(
        character not in "0123456789.-_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for character in version
    ):
        raise SetupError("Codex reported an invalid Builder Pulse version")
    candidates: list[Path] = []
    installed_path = installation.get("installedPath")
    if isinstance(installed_path, str) and installed_path:
        candidates.append(
            Path(installed_path).expanduser().resolve(strict=False)
            / "scripts"
            / "builder_pulse.py"
        )
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    candidates.append(
        codex_home
        / "plugins"
        / "cache"
        / MARKETPLACE
        / "builder-pulse"
        / version.removeprefix("v")
        / "scripts"
        / "builder_pulse.py"
    )
    for cli in candidates:
        if not cli.is_file():
            continue
        manifest = cli.parent.parent / ".codex-plugin" / "plugin.json"
        try:
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(manifest_data, dict)
            and manifest_data.get("version") == version.removeprefix("v")
        ):
            return cli
    raise SetupError("The existing Builder Pulse script could not be located safely")


def pause_existing_capture(installation: dict[str, Any] | None) -> None:
    if installation is None:
        return
    run_command(
        [
            sys.executable,
            str(installed_cli(installation)),
            "config",
            "set",
            "enabled",
            "false",
        ]
    )


def marketplace_state() -> dict[str, Any] | None:
    response = run_command(
        ["codex", "plugin", "marketplace", "list", "--json"],
        expect_json=True,
    )
    marketplaces = response.get("marketplaces") if isinstance(response, dict) else None
    if not isinstance(marketplaces, list):
        raise SetupError("Codex returned an invalid marketplace list")
    matches = [
        item
        for item in marketplaces
        if isinstance(item, dict) and item.get("name") == MARKETPLACE
    ]
    if len(matches) > 1:
        raise SetupError("Codex reported more than one GrowthX marketplace")
    return matches[0] if matches else None


def approved_existing_repository(value: Any) -> bool:
    return isinstance(value, str) and value in APPROVED_EXISTING_REPOSITORIES


def remove_current(
    *,
    plugin_installed: bool,
    marketplace_configured: bool,
    rollback_version: str | None = None,
) -> None:
    plugin_removed = False
    if plugin_installed:
        run_command(["codex", "plugin", "remove", PLUGIN, "--json"])
        plugin_removed = True
    if marketplace_configured:
        try:
            run_command(
                ["codex", "plugin", "marketplace", "remove", MARKETPLACE, "--json"]
            )
        except SetupError as removal_error:
            if plugin_removed and rollback_version:
                try:
                    add_plugin_from_configured_marketplace(rollback_version)
                except SetupError:
                    try:
                        install_release(f"v{rollback_version.removeprefix('v')}")
                    except SetupError as rollback_error:
                        raise SetupError(
                            "Builder Pulse removal failed and the previous version "
                            f"could not be restored: {rollback_error}"
                        ) from rollback_error
            raise removal_error


def add_plugin_from_configured_marketplace(expected_version: str) -> Path:
    response = run_command(
        ["codex", "plugin", "add", PLUGIN, "--json"], expect_json=True
    )
    installed_path = response.get("installedPath") if isinstance(response, dict) else None
    if not isinstance(installed_path, str) or not installed_path:
        raise SetupError("Codex did not return the installed Builder Pulse path")
    cli = Path(installed_path).resolve(strict=False) / "scripts" / "builder_pulse.py"
    if not cli.is_file():
        raise SetupError("The installed Builder Pulse package is incomplete")
    manifest = cli.parent.parent / ".codex-plugin" / "plugin.json"
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError("The installed Builder Pulse manifest is invalid") from exc
    if (
        not isinstance(manifest_data, dict)
        or manifest_data.get("version") != expected_version
    ):
        raise SetupError(
            "Codex installed an unexpected Builder Pulse version; "
            f"expected {expected_version}"
        )
    return cli


def install_release(release: str) -> Path:
    run_command(
        [
            "codex",
            "plugin",
            "marketplace",
            "add",
            "GrowthX-Club/builder-pulse-plugin",
            "--ref",
            release,
            "--json",
        ]
    )
    expected_version = release.removeprefix("v")
    return add_plugin_from_configured_marketplace(expected_version)


def cleanup_partial() -> None:
    try:
        remove_current(
            plugin_installed=installed_builder() is not None,
            marketplace_configured=marketplace_state() is not None,
        )
    except SetupError:
        pass


def verify_release_exists(release: str) -> None:
    run_command(
        [
            "git",
            "ls-remote",
            "--exit-code",
            "--refs",
            REPOSITORY,
            f"refs/tags/{release}",
        ]
    )


def parse_activation(output: str) -> dict[str, Any]:
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        raise SetupError("Builder Pulse activation returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise SetupError("Builder Pulse activation returned an invalid result")
    return result


def activate(cli: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(cli), "activate"],
        check=False,
        capture_output=True,
        text=True,
    )
    output = completed.stdout.strip()
    if output:
        try:
            result = parse_activation(output)
        except SetupError:
            result = None
        if completed.returncode == 0 and isinstance(result, dict):
            return result
        if isinstance(result, dict) and result.get("reviewRequired") is True:
            return result
    detail = completed.stderr.strip()
    raise SetupError(detail or "Builder Pulse activation failed")


def setup(
    invite_code: str,
    endpoint: str,
    project_root: str | Path,
    project_label: str,
) -> None:
    if sys.version_info < (3, 11):
        raise SetupError("Builder Pulse requires Python 3.11 or newer")
    if shutil.which("git") is None:
        raise SetupError("Builder Pulse requires git")
    if shutil.which("codex") is None:
        raise SetupError("Builder Pulse requires Codex")
    if len(invite_code) < 16 or len(invite_code) > 256:
        raise SetupError("The Builder Pulse invite code is invalid")
    if not str(project_root).strip():
        raise SetupError("A member-confirmed Builder Pulse project folder is required")
    enrolled_root = Path(project_root).expanduser().resolve(strict=False)
    if not enrolled_root.is_dir():
        raise SetupError("The confirmed Builder Pulse project folder does not exist")
    confirmed_label = project_label.strip()
    if not confirmed_label or len(confirmed_label) > 160:
        raise SetupError("The confirmed Builder Pulse project name is invalid")
    if any(ord(character) < 32 for character in confirmed_label):
        raise SetupError("The confirmed Builder Pulse project name is invalid")
    home = Path.home().resolve(strict=False)
    if enrolled_root == home or enrolled_root in home.parents:
        raise SetupError(
            "The confirmed folder must be a project folder, not the home, one of "
            "its parents, or the filesystem root"
        )

    verify_release_exists(TARGET_RELEASE)
    previous = installed_builder()
    previous_version = previous.get("version") if previous else None
    previous_marketplace = marketplace_state()
    if previous_marketplace:
        source = previous_marketplace.get("marketplaceSource")
        repository = source.get("source") if isinstance(source, dict) else None
        if not approved_existing_repository(repository):
            raise SetupError("The GrowthX marketplace name points to a different source")

    # Stop older machine-wide capture before replacing its package. The new
    # release is enabled again only after an explicit project is enrolled.
    pause_existing_capture(previous)
    remove_current(
        plugin_installed=previous is not None,
        marketplace_configured=previous_marketplace is not None,
        rollback_version=previous_version if isinstance(previous_version, str) else None,
    )
    try:
        cli = install_release(TARGET_RELEASE)
    except SetupError:
        cleanup_partial()
        if isinstance(previous_version, str) and previous_version:
            try:
                install_release(f"v{previous_version.removeprefix('v')}")
            except SetupError as rollback_error:
                raise SetupError(
                    "Builder Pulse update failed and the previous version could not be restored: "
                    f"{rollback_error}"
                ) from rollback_error
        raise

    claim_env = dict(os.environ)
    claim_env["BUILDER_PULSE_INVITE_CODE"] = invite_code
    run_command(
        [sys.executable, str(cli), "claim", "--endpoint", endpoint],
        env=claim_env,
    )
    run_command(
        [
            sys.executable,
            str(cli),
            "work",
            "enroll",
            "--root",
            str(enrolled_root),
            "--project",
            confirmed_label,
        ]
    )
    run_command(
        [sys.executable, str(cli), "config", "set", "enabled", "true"]
    )
    activation = activate(cli)
    if not (
        activation.get("activationReady") is True
        and activation.get("hooksTrusted") is True
        and activation.get("serverVerified") is True
    ):
        if activation.get("reviewRequired") is True:
            raise SetupError(
                "Codex requires its official one-time Builder Pulse hook review"
            )
        raise SetupError("Builder Pulse activation was not server-verified")
    run_command([sys.executable, str(cli), "flush"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--code")
    parser.add_argument("--project-root")
    parser.add_argument("--project-label")
    args = parser.parse_args()
    print(SETUP_DISCLOSURE, file=sys.stderr)
    invite_code = args.code or os.environ.get("BUILDER_PULSE_INVITE_CODE") or ""
    if not invite_code and sys.stdin.isatty():
        invite_code = getpass.getpass("Builder Pulse invite code: ")
    project_root = args.project_root or ""
    project_label = args.project_label or ""
    if sys.stdin.isatty() and not project_root:
        entered_root = input(f"Project folder to enroll [{Path.cwd()}]: ").strip()
        project_root = entered_root or str(Path.cwd())
    if sys.stdin.isatty() and not project_label:
        project_label = input("Project name GrowthX should display: ").strip()
    try:
        setup(invite_code, args.endpoint, project_root, project_label)
    except SetupError as exc:
        print(f"Builder Pulse setup failed safely: {exc}", file=sys.stderr)
        return 1
    print(
        "Builder Pulse is installed and its hooks are trusted. "
        "Only the confirmed project folder is enrolled. "
        "Exit all running Codex sessions, start a fresh Codex session, "
        "then send one normal prompt to verify server receipt."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
