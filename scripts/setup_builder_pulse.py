#!/usr/bin/env python3
"""Install or update Builder Pulse through one stable bootstrap command."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path, PurePath
import re
import shutil
import subprocess
import sys
from typing import Any, NamedTuple
from urllib import error as urlerror
from urllib import request as urlrequest


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
RELEASE_API = (
    "https://api.github.com/repos/GrowthX-Club/builder-pulse-plugin/releases/tags/"
)
COMMITS_API = "https://api.github.com/repos/{repository}/commits/{commit}"
SETUP_DISCLOSURE = (
    "Builder Pulse is installed machine-wide, but it sends data only from project "
    "folders you explicitly enroll. GrowthX stores the claimed member ID, name, email "
    "address, and any optional default project or program copied from the member record "
    "so telemetry can be linked to the right person. For each enrolled project, it "
    "receives a stable installation ID, "
    "a one-way hashed session ID, the display name you confirm and a sanitized project "
    "ID, any feature name and ID you explicitly set, coarse work state and event/activity "
    "timestamps, plugin version, optional cumulative token counts, and each primary "
    "prompt you submit after secret redaction and a 64 KiB limit. GrowthX's authenticated "
    "Builder Pulse admins can view these identity and telemetry fields for learning "
    "feedback. Raw lifecycle events "
    "and activity buckets are retained for 30 days; submitted prompts and their feedback "
    "are retained for 60 days; the member identity fields, installation/member link, "
    "latest status, and compacted "
    "session, daily, and all-time "
    "token aggregates remain until GrowthX removes them. It does not send "
    "folder paths, files, patches, commands, tool input or output, assistant replies, "
    "transcripts, or environment variables. Secret redaction is a safety layer, not a "
    "guarantee, so do not put secrets in prompts."
)


class SetupError(RuntimeError):
    pass


class RollbackSource(NamedTuple):
    version: str
    commit: str
    repository: str


def is_filesystem_root(path: PurePath) -> bool:
    return bool(path.anchor) and path == type(path)(path.anchor)


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


def normalized_repository(value: str) -> str:
    return value.removesuffix(".git").rstrip("/")


def repository_slug(value: str) -> str:
    normalized = normalized_repository(value)
    prefix = "https://github.com/"
    if not approved_existing_repository(value) or not normalized.startswith(prefix):
        raise SetupError("The GrowthX marketplace source is not approved")
    slug = normalized.removeprefix(prefix)
    if slug not in {
        "GrowthX-Club/builder-pulse-plugin",
        "udayanwalvekar/builder-pulse-plugin",
    }:
        raise SetupError("The GrowthX marketplace source is not approved")
    return slug


def verified_git_checkout(root: Path) -> tuple[str, str]:
    try:
        resolved = root.expanduser().resolve(strict=True)
        top_level = Path(
            str(
                run_command(
                    ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"]
                )
            ).strip()
        ).resolve(strict=True)
    except OSError as exc:
        raise SetupError("The existing Builder Pulse checkout root is invalid") from exc
    if top_level != resolved:
        raise SetupError("The existing Builder Pulse checkout root is invalid")
    repository = str(
        run_command(["git", "-C", str(resolved), "remote", "get-url", "origin"])
    ).strip()
    if not approved_existing_repository(repository):
        raise SetupError("The existing Builder Pulse checkout has an unapproved origin")
    tracked_changes = str(
        run_command(
            [
                "git",
                "-C",
                str(resolved),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ]
        )
    ).strip()
    if tracked_changes:
        raise SetupError("The existing Builder Pulse checkout has modified tracked files")
    commit = str(
        run_command(["git", "-C", str(resolved), "rev-parse", "HEAD"])
    ).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SetupError("The existing Builder Pulse checkout commit is invalid")
    return repository, commit


def verify_remote_commit(repository: str, commit: str) -> None:
    request = urlrequest.Request(
        COMMITS_API.format(repository=repository_slug(repository), commit=commit),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "builder-pulse-installer",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    try:
        with urlrequest.urlopen(request, timeout=10) as response:
            raw = response.read(65_537)
    except (OSError, ValueError, urlerror.URLError) as exc:
        raise SetupError("The previous Builder Pulse commit could not be verified") from exc
    if len(raw) > 65_536:
        raise SetupError("GitHub returned an invalid Builder Pulse commit response")
    try:
        commit_data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError("GitHub returned an invalid Builder Pulse commit response") from exc
    if not isinstance(commit_data, dict) or commit_data.get("sha") != commit:
        raise SetupError("The previous Builder Pulse commit could not be verified")


def verified_rollback_source(
    installation: dict[str, Any] | None,
    marketplace: dict[str, Any] | None,
) -> RollbackSource | None:
    if installation is None:
        return None
    version = installation.get("version")
    if not isinstance(version, str) or not version:
        raise SetupError("Codex reported an invalid previous Builder Pulse version")
    installed_root = installed_cli(installation).parent.parent
    repository, commit = verified_git_checkout(installed_root)

    if marketplace is not None:
        marketplace_root = marketplace.get("root")
        if not isinstance(marketplace_root, str) or not marketplace_root:
            raise SetupError("Codex did not report the GrowthX marketplace checkout")
        marketplace_repository, marketplace_commit = verified_git_checkout(
            Path(marketplace_root)
        )
        source = marketplace.get("marketplaceSource")
        declared_repository = source.get("source") if isinstance(source, dict) else None
        if (
            not approved_existing_repository(declared_repository)
            or normalized_repository(str(declared_repository))
            != normalized_repository(marketplace_repository)
        ):
            raise SetupError(
                "The GrowthX marketplace declaration and checkout provenance differ"
            )
        if (
            normalized_repository(marketplace_repository)
            != normalized_repository(repository)
            or marketplace_commit != commit
        ):
            raise SetupError(
                "The installed Builder Pulse package and marketplace provenance differ"
            )

    verify_remote_commit(repository, commit)
    return RollbackSource(version.removeprefix("v"), commit, repository)


def remove_current(
    *,
    plugin_installed: bool,
    marketplace_configured: bool,
    rollback_source: RollbackSource | None = None,
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
            if plugin_removed and rollback_source is not None:
                try:
                    add_plugin_from_configured_marketplace(rollback_source.version)
                except SetupError as rollback_error:
                    raise SetupError(
                        "Builder Pulse removal failed and the verified previous plugin "
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


def install_release(
    release: str,
    *,
    expected_version: str | None = None,
    repository: str = REPOSITORY,
) -> Path:
    run_command(
        [
            "codex",
            "plugin",
            "marketplace",
            "add",
            repository_slug(repository),
            "--ref",
            release,
            "--json",
        ]
    )
    package_version = expected_version or release.removeprefix("v")
    return add_plugin_from_configured_marketplace(package_version)


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
    request = urlrequest.Request(
        f"{RELEASE_API}{release}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "builder-pulse-installer",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    try:
        with urlrequest.urlopen(request, timeout=10) as response:
            raw = response.read(65_537)
    except (OSError, ValueError, urlerror.URLError) as exc:
        raise SetupError("The immutable Builder Pulse release could not be verified") from exc
    if len(raw) > 65_536:
        raise SetupError("GitHub returned an invalid Builder Pulse release response")
    try:
        release_data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError("GitHub returned an invalid Builder Pulse release response") from exc
    if not (
        isinstance(release_data, dict)
        and release_data.get("tag_name") == release
        and release_data.get("draft") is False
        and release_data.get("immutable") is True
    ):
        raise SetupError(
            "Builder Pulse setup requires a published immutable GitHub release"
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


def claimed_identity(cli: Path) -> dict[str, str]:
    status = run_command(
        [sys.executable, str(cli), "status", "--json"], expect_json=True
    )
    identity = status.get("identity") if isinstance(status, dict) else None
    if not isinstance(identity, dict):
        raise SetupError("Builder Pulse status returned an invalid identity")
    fields = {
        key: identity.get(key)
        for key in ("installationId", "builderId", "memberId")
    }
    if (
        identity.get("claimed") is not True
        or identity.get("tokenConfigured") is not True
        or any(not isinstance(value, str) or not value for value in fields.values())
    ):
        raise SetupError("The existing Builder Pulse identity is not fully claimed")
    return {key: str(value) for key, value in fields.items()}


def setup(
    invite_code: str,
    endpoint: str,
    project_root: str | Path,
    project_label: str,
    *,
    reuse_existing_claim: bool = False,
) -> None:
    if sys.version_info < (3, 11):
        raise SetupError("Builder Pulse requires Python 3.11 or newer")
    if shutil.which("git") is None:
        raise SetupError("Builder Pulse requires git")
    if shutil.which("codex") is None:
        raise SetupError("Builder Pulse requires Codex")
    if reuse_existing_claim and invite_code:
        raise SetupError("Existing-claim repair must not use a new invite code")
    if not reuse_existing_claim and (len(invite_code) < 16 or len(invite_code) > 256):
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
    if (
        enrolled_root == home
        or enrolled_root in home.parents
        or is_filesystem_root(enrolled_root)
    ):
        raise SetupError(
            "The confirmed folder must be a project folder, not the home, one of "
            "its parents, or the filesystem root"
        )

    verify_release_exists(TARGET_RELEASE)
    previous = installed_builder()
    previous_marketplace = marketplace_state()
    if previous_marketplace:
        source = previous_marketplace.get("marketplaceSource")
        repository = source.get("source") if isinstance(source, dict) else None
        if not approved_existing_repository(repository):
            raise SetupError("The GrowthX marketplace name points to a different source")

    # Resolve and remotely verify the exact currently installed commit before
    # executing its pause command or changing either Codex registration. A tag
    # is not rollback provenance because it may move later.
    rollback_source = verified_rollback_source(previous, previous_marketplace)
    preserved_identity: dict[str, str] | None = None
    if reuse_existing_claim:
        if previous is None:
            raise SetupError("Existing-claim repair requires an installed Builder Pulse")
        preserved_identity = claimed_identity(installed_cli(previous))

    # Stop older machine-wide capture before replacing its package. The new
    # release is enabled again only after an explicit project is enrolled.
    pause_existing_capture(previous)
    remove_current(
        plugin_installed=previous is not None,
        marketplace_configured=previous_marketplace is not None,
        rollback_source=rollback_source,
    )
    try:
        cli = install_release(TARGET_RELEASE)
    except SetupError:
        cleanup_partial()
        if rollback_source is not None:
            try:
                install_release(
                    rollback_source.commit,
                    expected_version=rollback_source.version,
                    repository=rollback_source.repository,
                )
            except SetupError as rollback_error:
                raise SetupError(
                    "Builder Pulse update failed and the previous version could not be restored: "
                    f"{rollback_error}"
                ) from rollback_error
        raise

    if reuse_existing_claim:
        if claimed_identity(cli) != preserved_identity:
            raise SetupError("The Builder Pulse identity changed during repair")
    else:
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
    try:
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
    except SetupError as setup_error:
        try:
            run_command(
                [sys.executable, str(cli), "config", "set", "enabled", "false"]
            )
        except SetupError as disable_error:
            raise SetupError(
                f"{setup_error}; capture could not be disabled after failure: "
                f"{disable_error}"
            ) from disable_error
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--code")
    parser.add_argument("--project-root")
    parser.add_argument("--project-label")
    parser.add_argument(
        "--reuse-existing-claim",
        action="store_true",
        help="Repair an already-claimed installation without a new invite",
    )
    args = parser.parse_args()
    print(SETUP_DISCLOSURE, file=sys.stderr)
    invite_code = args.code or os.environ.get("BUILDER_PULSE_INVITE_CODE") or ""
    if not args.reuse_existing_claim and not invite_code and sys.stdin.isatty():
        invite_code = getpass.getpass("Builder Pulse invite code: ")
    project_root = args.project_root or ""
    project_label = args.project_label or ""
    if sys.stdin.isatty() and not project_root:
        current_folder = Path.cwd().resolve(strict=False)
        repository_root: Path | None = None
        try:
            detected = subprocess.run(
                [
                    "git",
                    "-C",
                    str(current_folder),
                    "rev-parse",
                    "--show-toplevel",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if detected.returncode == 0 and detected.stdout.strip():
                candidate = Path(detected.stdout.strip()).resolve(strict=False)
                if candidate.is_dir() and candidate != current_folder:
                    repository_root = candidate
        except (OSError, subprocess.SubprocessError):
            repository_root = None
        print(
            "Builder Pulse project choices (shown only in this terminal):",
            file=sys.stderr,
        )
        print(f"- Current folder: {current_folder}", file=sys.stderr)
        if repository_root is not None:
            print(
                f"- Nearest Git repository root: {repository_root}",
                file=sys.stderr,
            )
        entered_root = input(
            "Which exact project folder should Builder Pulse monitor? "
            f"[{current_folder}]: "
        ).strip()
        project_root = entered_root or str(current_folder)
    if sys.stdin.isatty() and not project_label:
        project_label = input("Project name GrowthX should display: ").strip()
    try:
        setup(
            invite_code,
            args.endpoint,
            project_root,
            project_label,
            reuse_existing_claim=args.reuse_existing_claim,
        )
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
