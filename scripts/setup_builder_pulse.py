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


REPOSITORY = "https://github.com/udayanwalvekar/builder-pulse-plugin.git"
MARKETPLACE = "growthx-builder-tools"
PLUGIN = f"builder-pulse@{MARKETPLACE}"
TARGET_RELEASE = "v0.4.5"
DEFAULT_ENDPOINT = "https://precious-ant-429.convex.site"


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
            "udayanwalvekar/builder-pulse-plugin",
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


def setup(invite_code: str, endpoint: str) -> None:
    if sys.version_info < (3, 11):
        raise SetupError("Builder Pulse requires Python 3.11 or newer")
    if shutil.which("git") is None:
        raise SetupError("Builder Pulse requires git")
    if shutil.which("codex") is None:
        raise SetupError("Builder Pulse requires Codex")
    if len(invite_code) < 16 or len(invite_code) > 256:
        raise SetupError("The Builder Pulse invite code is invalid")

    verify_release_exists(TARGET_RELEASE)
    previous = installed_builder()
    previous_version = previous.get("version") if previous else None
    previous_marketplace = marketplace_state()
    if previous_marketplace:
        source = previous_marketplace.get("marketplaceSource")
        repository = source.get("source") if isinstance(source, dict) else None
        if repository not in {REPOSITORY, REPOSITORY.removesuffix(".git")}:
            raise SetupError("The GrowthX marketplace name points to a different source")

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
    activation = activate(cli)
    if not (
        activation.get("connected") is True
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
    args = parser.parse_args()
    invite_code = args.code or os.environ.get("BUILDER_PULSE_INVITE_CODE") or ""
    if not invite_code and sys.stdin.isatty():
        invite_code = getpass.getpass("Builder Pulse invite code: ")
    try:
        setup(invite_code, args.endpoint)
    except SetupError as exc:
        print(f"Builder Pulse setup failed safely: {exc}", file=sys.stderr)
        return 1
    print("Builder Pulse is connected. Send one new normal Codex prompt to verify telemetry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
