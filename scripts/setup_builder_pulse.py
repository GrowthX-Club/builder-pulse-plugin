#!/usr/bin/env python3
"""Install or update Builder Pulse through one stable bootstrap command."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import http.client
import json
import os
from pathlib import Path, PurePath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable, NamedTuple
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
TARGET_RELEASE = "v0.5.0"
CLAUDE_MARKETPLACE = f"growthx-builder-tools-{TARGET_RELEASE.replace('.', '-')}"
CLAUDE_POSIX_PLUGIN = f"builder-pulse-claude-posix@{CLAUDE_MARKETPLACE}"
CLAUDE_WINDOWS_PLUGIN = f"builder-pulse-claude-windows@{CLAUDE_MARKETPLACE}"
DEFAULT_ENDPOINT = "https://precious-ant-429.convex.site"
RELEASE_API = (
    "https://api.github.com/repos/GrowthX-Club/builder-pulse-plugin/releases/tags/"
)
COMMITS_API = "https://api.github.com/repos/{repository}/commits/{commit}"
SETUP_DISCLOSURE = (
    "Builder Pulse installs hooks for Codex and Claude Code when those agents are "
    "available on this computer, but it sends data only from project folders you "
    "explicitly enroll. One shared identity and project allowlist apply to both agents. "
    "GrowthX stores the claimed member ID, name, email address, and any optional "
    "roster or program label supplied by GrowthX so telemetry can be linked to the "
    "right person. A roster or program label is never used as a telemetry project. "
    "For each enrolled project, it "
    "receives a stable installation ID, "
    "a one-way hashed session ID, the display name you confirm and a sanitized project "
    "ID, any feature name and ID you explicitly set, coarse work state and event/activity "
    "timestamps, agent name, plugin version, optional cumulative Codex token counts, "
    "and each primary "
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


class PausedCapture(NamedTuple):
    data_dir: Path
    identity: dict[str, Any]


def is_filesystem_root(path: PurePath) -> bool:
    return bool(path.anchor) and path == type(path)(path.anchor)


def run_command(
    arguments: list[str],
    *,
    env: dict[str, str] | None = None,
    expect_json: bool = False,
) -> Any:
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(f"Command could not start: {arguments[0]}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SetupError(detail or f"Command failed: {arguments[0]}")
    if not expect_json:
        return completed.stdout
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SetupError(f"{arguments[0]} returned invalid JSON") from exc


def cli_command(cli: Path, *arguments: str) -> list[str]:
    """Run a verified single-file CLI without local/site import shadowing."""
    return [sys.executable, "-I", "-S", str(cli), *arguments]


def read_object(path: Path, *, required: bool = False) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise SetupError(f"Required Builder Pulse data is missing: {path.name}")
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"Builder Pulse data is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"Builder Pulse data is invalid: {path.name}")
    return value


def atomic_write_object(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def exclusive_file_lock(path: Path) -> Iterable[None]:
    """Use the same one-byte lock protocol as every supported plugin release."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if os.fstat(handle.fileno()).st_size == 0:
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    def verified_cli(plugin_root: Path) -> Path | None:
        cli = plugin_root / "scripts" / "builder_pulse.py"
        if not cli.is_file():
            return None
        manifest = plugin_root / ".codex-plugin" / "plugin.json"
        try:
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            isinstance(manifest_data, dict)
            and manifest_data.get("version") == version.removeprefix("v")
        ):
            return cli
        return None

    installed_path = installation.get("installedPath")
    if isinstance(installed_path, str) and installed_path:
        reported_cli = verified_cli(
            Path(installed_path).expanduser().resolve(strict=False)
        )
        if reported_cli is not None:
            return reported_cli

    # Only consult the user-home fallback when Codex did not report a usable
    # absolute installation path. Some restricted Windows environments omit
    # HOME and USERPROFILE, but a valid installedPath is still sufficient.
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    fallback_cli = verified_cli(
        codex_home
        / "plugins"
        / "cache"
        / MARKETPLACE
        / "builder-pulse"
        / version.removeprefix("v")
    )
    if fallback_cli is not None:
        return fallback_cli
    raise SetupError("The existing Builder Pulse script could not be located safely")


def configured_plugin_data_dir() -> Path | None:
    explicit = os.environ.get("BUILDER_PULSE_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)
    return None


def canonical_plugin_data_dir() -> Path:
    configured = configured_plugin_data_dir()
    if configured is not None:
        return configured
    return (Path.home() / ".builder-pulse").resolve(strict=False)


def plugin_data_dir(cli: Path) -> Path:
    del cli
    return canonical_plugin_data_dir()


def legacy_codex_plugin_data_dir(cli: Path | None = None) -> Path:
    if cli is not None:
        plugin_root = cli.parent.parent.resolve(strict=False)
        try:
            cache_dir = plugin_root.parent.parent.parent
            if (
                cache_dir.name == "cache"
                and plugin_root.parent.name == "builder-pulse"
                and plugin_root.parent.parent.name == MARKETPLACE
            ):
                return (
                    cache_dir.parent / "data" / f"builder-pulse-{MARKETPLACE}"
                ).resolve(strict=False)
        except (IndexError, OSError):
            pass
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return (
        codex_home / "plugins" / "data" / f"builder-pulse-{MARKETPLACE}"
    ).expanduser().resolve(strict=False)


def migrate_existing_data_to_shared(
    installation: dict[str, Any] | None,
) -> Path:
    target = canonical_plugin_data_dir()
    legacy_cli: Path | None = None
    if installation is not None:
        try:
            legacy_cli = installed_cli(installation)
        except SetupError:
            # A half-installed or already-removed Codex package must not make
            # its separately stored identity unrecoverable. The legacy data
            # directory has a stable CODEX_HOME fallback and no package code is
            # executed during migration.
            legacy_cli = None
    source = legacy_codex_plugin_data_dir(legacy_cli)
    source_has_identity = bool(
        (source / "identity.json").is_file()
        or (source / "setup-paused-identity.json").is_file()
    )
    if target.exists():
        target_has_identity = bool(
            (target / "identity.json").is_file()
            or (target / "setup-paused-identity.json").is_file()
        )
        if source_has_identity and not target_has_identity:
            raise SetupError(
                "The shared Builder Pulse data directory exists without the prior identity"
            )
        return target
    if not source_has_identity:
        return target
    for candidate in source.rglob("*"):
        if candidate.is_symlink():
            raise SetupError("The existing Builder Pulse data contains a symbolic link")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}-migration"
    if temporary.exists():
        raise SetupError("A previous Builder Pulse data migration is incomplete")
    try:
        shutil.copytree(source, temporary, symlinks=False)
        os.replace(temporary, target)
    except OSError as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise SetupError("The existing Builder Pulse data could not be migrated safely") from exc
    return target


def existing_plugin_data_dir(installation: dict[str, Any] | None) -> Path:
    del installation
    return canonical_plugin_data_dir()


def authoritative_identity(data_dir: Path) -> dict[str, Any]:
    paused_path = data_dir / "setup-paused-identity.json"
    current_path = data_dir / "identity.json"
    paused = read_object(paused_path) if paused_path.exists() else {}
    current = read_object(current_path) if current_path.exists() else {}
    if paused:
        paused_installation = paused.get("installationId")
        current_installation = current.get("installationId")
        if (
            isinstance(current_installation, str)
            and current_installation
            and current_installation != paused_installation
        ):
            raise SetupError("The paused Builder Pulse identity does not match local data")
        return paused
    return current


def pause_server_capture(identity: dict[str, Any], plugin_version: str) -> bool:
    """Create a server barrier before legacy processes lose local credentials."""
    token = identity.get("installationToken")
    endpoint = identity.get("claimedEndpoint")
    installation_id = identity.get("installationId")
    token_present = token is not None
    endpoint_present = endpoint is not None
    if token_present != endpoint_present:
        raise SetupError("The existing Builder Pulse delivery identity is incomplete")
    if not token_present:
        # A first claim whose response was lost has an installation ID and a
        # pending token, but no finalized delivery credential pair. Preserve it
        # for an idempotent retry; there is no authenticated server capture to
        # pause yet.
        return False
    if not (
        isinstance(token, str)
        and token
        and isinstance(endpoint, str)
        and endpoint
        and isinstance(installation_id, str)
        and installation_id
    ):
        raise SetupError("The existing Builder Pulse delivery identity is incomplete")
    payload = json.dumps(
        {
            "installationId": installation_id,
            "pluginVersion": plugin_version,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urlrequest.Request(
        f"{str(endpoint).rstrip('/')}/v1/privacy-pause",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"builder-pulse-installer/{TARGET_RELEASE.removeprefix('v')}",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=10) as response:
            raw = response.read(65_537)
    except (
        OSError,
        ValueError,
        urlerror.URLError,
        http.client.HTTPException,
    ) as exc:
        raise SetupError(
            "GrowthX could not pause the existing Builder Pulse installation safely"
        ) from exc
    if len(raw) > 65_536:
        raise SetupError("GrowthX returned an invalid Builder Pulse privacy-pause response")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError(
            "GrowthX returned an invalid Builder Pulse privacy-pause response"
        ) from exc
    if not (
        isinstance(result, dict)
        and result.get("paused") is True
        and result.get("installationId") == installation_id
    ):
        raise SetupError("GrowthX did not confirm the Builder Pulse privacy pause")
    return True


def resume_server_capture(identity: dict[str, Any], plugin_version: str) -> None:
    """Resume only after the replacement is installed and locally enrolled."""
    token = identity.get("installationToken")
    endpoint = identity.get("claimedEndpoint")
    installation_id = identity.get("installationId")
    if not (
        isinstance(token, str)
        and token
        and isinstance(endpoint, str)
        and endpoint
        and isinstance(installation_id, str)
        and installation_id
    ):
        raise SetupError("The Builder Pulse delivery identity is incomplete")
    payload = json.dumps(
        {
            "installationId": installation_id,
            "pluginVersion": plugin_version,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urlrequest.Request(
        f"{str(endpoint).rstrip('/')}/v1/privacy-resume",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"builder-pulse-installer/{TARGET_RELEASE.removeprefix('v')}",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=10) as response:
            raw = response.read(65_537)
    except (
        OSError,
        ValueError,
        urlerror.URLError,
        http.client.HTTPException,
    ) as exc:
        raise SetupError(
            "GrowthX could not resume the enrolled Builder Pulse installation safely"
        ) from exc
    if len(raw) > 65_536:
        raise SetupError("GrowthX returned an invalid Builder Pulse privacy-resume response")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError(
            "GrowthX returned an invalid Builder Pulse privacy-resume response"
        ) from exc
    if not (
        isinstance(result, dict)
        and result.get("resumed") is True
        and result.get("installationId") == installation_id
    ):
        raise SetupError("GrowthX did not confirm the Builder Pulse privacy resume")


def quarantine_local_capture(data_dir: Path, identity: dict[str, Any]) -> None:
    """Disable local capture and preserve any retryable identity atomically."""
    identity_path = data_dir / "identity.json"
    paused_path = data_dir / "setup-paused-identity.json"
    config_path = data_dir / "config.json"
    with exclusive_file_lock(data_dir / ".delivery.lock"):
        with exclusive_file_lock(data_dir / ".scope-delivery.lock"):
            with exclusive_file_lock(data_dir / ".lock"):
                if identity and not paused_path.exists():
                    atomic_write_object(paused_path, identity)
                paused_identity = dict(identity)
                for key in ("installationToken", "pendingInstallationToken"):
                    paused_identity.pop(key, None)
                if paused_identity:
                    paused_identity["promptCapture"] = "off"
                    atomic_write_object(identity_path, paused_identity)

                config = read_object(config_path) if config_path.exists() else {}
                config["enabled"] = False
                atomic_write_object(config_path, config)

                # Queued machine-wide records cannot be safely attributed to the
                # project the member is about to confirm.
                for relative in (
                    "outbox.jsonl",
                    "prompt-outbox.jsonl",
                    "quarantine.jsonl",
                ):
                    try:
                        (data_dir / relative).unlink()
                    except FileNotFoundError:
                        pass


def pause_existing_capture(
    installation: dict[str, Any] | None,
) -> PausedCapture:
    data_dir = existing_plugin_data_dir(installation)
    identity = authoritative_identity(data_dir)
    installed_version = (
        installation.get("version") if isinstance(installation, dict) else None
    )
    server_pause_error: BaseException | None = None
    try:
        pause_server_capture(
            identity,
            str(installed_version or TARGET_RELEASE.removeprefix("v")),
        )
    except BaseException as exc:
        server_pause_error = exc

    # Quarantine locally even when the server cannot acknowledge the barrier.
    # This stops every new process and preserves pending first-claim tokens for
    # an idempotent retry. A running legacy process may have cached a token, so
    # the caller must stop until the server pause is known and old sessions exit.
    try:
        quarantine_local_capture(data_dir, identity)
        if installation is not None:
            try:
                legacy_cli = installed_cli(installation)
            except SetupError:
                legacy_cli = None
            legacy_data_dir = legacy_codex_plugin_data_dir(legacy_cli)
            if (
                legacy_data_dir != data_dir.resolve(strict=False)
                and legacy_data_dir.exists()
            ):
                legacy_identity = authoritative_identity(legacy_data_dir)
                if (
                    identity
                    and legacy_identity.get("installationId")
                    != identity.get("installationId")
                ):
                    raise SetupError(
                        "The legacy and shared Builder Pulse identities differ"
                    )
                quarantine_local_capture(legacy_data_dir, legacy_identity)
    except (OSError, SetupError) as exc:
        detail = (
            " GrowthX server privacy-pause status is also unknown."
            if server_pause_error is not None
            else ""
        )
        raise SetupError(
            "Builder Pulse could not be disabled locally."
            f"{detail} Exit all running Claude Code and Codex sessions now and do not continue "
            "until setup is repaired."
        ) from exc

    if server_pause_error is not None:
        detail = (
            "GrowthX server privacy-pause status is unknown. Builder Pulse was "
            "disabled locally and its pending queues were removed. Exit all "
            "running Claude Code and Codex sessions now, then retry setup when the server is reachable."
        )
        if isinstance(server_pause_error, (KeyboardInterrupt, SystemExit)):
            print(detail, file=sys.stderr)
            raise server_pause_error
        raise SetupError(detail) from server_pause_error
    return PausedCapture(data_dir=data_dir, identity=identity)


def restore_paused_identity(cli: Path, paused: PausedCapture | None) -> None:
    if paused is None or not paused.identity:
        return
    target_data_dir = plugin_data_dir(cli)
    if target_data_dir != paused.data_dir.resolve(strict=False):
        raise SetupError("The replacement plugin resolved a different data directory")
    paused_path = target_data_dir / "setup-paused-identity.json"
    with exclusive_file_lock(target_data_dir / ".delivery.lock"):
        with exclusive_file_lock(target_data_dir / ".scope-delivery.lock"):
            with exclusive_file_lock(target_data_dir / ".lock"):
                stored = read_object(paused_path, required=True)
                if stored != paused.identity:
                    raise SetupError(
                        "The preserved Builder Pulse identity changed during repair"
                    )
                atomic_write_object(target_data_dir / "identity.json", stored)
                paused_path.unlink()


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
    checkout_changes = str(
        run_command(
            [
                "git",
                "-C",
                str(resolved),
                "status",
                "--porcelain",
                "--ignored=matching",
                "--untracked-files=all",
            ]
        )
    ).strip()
    if checkout_changes:
        raise SetupError(
            "The existing Builder Pulse checkout has modified, untracked, or ignored files"
        )
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
                    # Do not trust a marketplace merely because it was exact
                    # earlier. Remove it on a second attempt, then pin and
                    # verify the full approved commit during restoration.
                    run_command(
                        [
                            "codex",
                            "plugin",
                            "marketplace",
                            "remove",
                            MARKETPLACE,
                            "--json",
                        ]
                    )
                    install_verified_rollback(rollback_source)
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
    expected_commit: str | None = None,
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
    cli = add_plugin_from_configured_marketplace(package_version)
    if expected_commit is not None:
        installed_repository, installed_commit = verified_git_checkout(cli.parent.parent)
        if (
            normalized_repository(installed_repository)
            != normalized_repository(repository)
            or installed_commit != expected_commit
        ):
            raise SetupError("The installed Builder Pulse package has different provenance")
    return cli


def install_verified_rollback(source: RollbackSource) -> Path:
    return install_release(
        source.commit,
        expected_version=source.version,
        repository=source.repository,
        expected_commit=source.commit,
    )


def verified_installer_checkout(expected_commit: str) -> Path:
    """Return the clean release checkout only when its provenance is exact."""
    source_root = Path(__file__).resolve().parent.parent
    source_repository, source_commit = verified_git_checkout(source_root)
    if (
        normalized_repository(source_repository) != normalized_repository(REPOSITORY)
        or source_commit != expected_commit
    ):
        raise SetupError(
            "The Builder Pulse installer checkout does not match the immutable release"
        )
    source_cli = source_root / "scripts" / "builder_pulse.py"
    source_defaults = source_root / "config" / "defaults.json"
    if not source_cli.is_file() or not source_defaults.is_file():
        raise SetupError("The Builder Pulse release runtime is incomplete")
    return source_root


def install_shared_runtime(expected_commit: str) -> Path:
    """Install the immutable runtime Claude and local repair commands share."""
    version = TARGET_RELEASE.removeprefix("v")
    source_root = verified_installer_checkout(expected_commit)
    source_cli = source_root / "scripts" / "builder_pulse.py"
    source_defaults = source_root / "config" / "defaults.json"
    target_root = canonical_plugin_data_dir() / "runtime" / version
    target_cli = target_root / "scripts" / "builder_pulse.py"
    if target_root.exists():
        try:
            same_cli = target_cli.read_bytes() == source_cli.read_bytes()
            same_defaults = (
                target_root / "config" / "defaults.json"
            ).read_bytes() == source_defaults.read_bytes()
        except OSError as exc:
            raise SetupError("The shared Builder Pulse runtime is invalid") from exc
        if not same_cli or not same_defaults:
            raise SetupError(
                "The shared Builder Pulse runtime differs from the immutable release"
            )
        return target_cli

    target_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = target_root.parent / f".{version}-installing"
    if temporary.exists():
        raise SetupError("A previous Builder Pulse runtime install is incomplete")
    try:
        (temporary / "scripts").mkdir(parents=True)
        (temporary / "config").mkdir(parents=True)
        (temporary / ".codex-plugin").mkdir(parents=True)
        shutil.copy2(source_cli, temporary / "scripts" / "builder_pulse.py")
        shutil.copy2(source_defaults, temporary / "config" / "defaults.json")
        atomic_write_object(
            temporary / ".codex-plugin" / "plugin.json",
            {"name": "builder-pulse-runtime", "version": version},
        )
        os.replace(temporary, target_root)
    except OSError as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise SetupError("The shared Builder Pulse runtime could not be installed") from exc
    return target_cli


def installed_claude_builders() -> list[dict[str, Any]]:
    response = run_command(["claude", "plugin", "list", "--json"], expect_json=True)
    if not isinstance(response, list):
        raise SetupError("Claude Code returned an invalid plugin list")
    expected_names = {
        "builder-pulse-claude-posix",
        "builder-pulse-claude-windows",
    }
    return [
        entry
        for entry in response
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and entry["id"].partition("@")[0] in expected_names
    ]


def claude_marketplace_state() -> dict[str, Any] | None:
    response = run_command(
        ["claude", "plugin", "marketplace", "list", "--json"],
        expect_json=True,
    )
    if not isinstance(response, list):
        raise SetupError("Claude Code returned an invalid marketplace list")
    matches = [
        entry
        for entry in response
        if isinstance(entry, dict) and entry.get("name") == CLAUDE_MARKETPLACE
    ]
    if len(matches) > 1:
        raise SetupError("Claude Code reported more than one GrowthX marketplace")
    return matches[0] if matches else None


def verify_claude_marketplace(
    marketplace: dict[str, Any], expected_commit: str
) -> None:
    """Prove Claude's release-scoped marketplace is the immutable GrowthX checkout."""
    if (
        marketplace.get("source") != "github"
        or marketplace.get("repo") != "GrowthX-Club/builder-pulse-plugin"
    ):
        raise SetupError(
            "The Claude Code GrowthX marketplace name points to a different source"
        )
    install_location = marketplace.get("installLocation")
    if not isinstance(install_location, str) or not install_location:
        raise SetupError("Claude Code did not report the GrowthX marketplace checkout")

    # Claude Code materializes marketplaces as Git-less snapshots. It records
    # the resolved revision in .gcs-sha, so validate that marker and then prove
    # every executable/declarative marketplace file matches the already
    # verified immutable installer checkout. The source/revision metadata alone
    # is not sufficient because a local marketplace cache can be modified.
    root = Path(install_location).expanduser().resolve(strict=False)
    try:
        commit = (root / ".gcs-sha").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SetupError("The Claude Code GrowthX marketplace is unreadable") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or commit != expected_commit:
        raise SetupError(
            "The Claude Code GrowthX marketplace does not match the immutable release"
        )

    source_root = Path(__file__).resolve().parent.parent
    relative_roots = (
        Path(".claude-plugin") / "marketplace.json",
        Path("claude-plugins"),
    )
    expected_files: dict[Path, bytes] = {}
    installed_files: dict[Path, bytes] = {}
    try:
        for relative_root in relative_roots:
            source = source_root / relative_root
            installed = root / relative_root
            source_paths = [source] if source.is_file() else source.rglob("*")
            installed_paths = [installed] if installed.is_file() else installed.rglob("*")
            for path in source_paths:
                if path.is_symlink():
                    raise SetupError(
                        "The Claude Code Builder Pulse marketplace contains a symlink"
                    )
                if path.is_file():
                    expected_files[path.relative_to(source_root)] = path.read_bytes()
            for path in installed_paths:
                if path.is_symlink():
                    raise SetupError(
                        "The installed Claude Code GrowthX marketplace contains a symlink"
                    )
                if path.is_file():
                    installed_files[path.relative_to(root)] = path.read_bytes()
    except OSError as exc:
        raise SetupError("The Claude Code GrowthX marketplace is unreadable") from exc
    if not expected_files or installed_files != expected_files:
        raise SetupError(
            "The Claude Code GrowthX marketplace differs from the immutable release"
        )


def ensure_claude_marketplace(expected_commit: str) -> None:
    marketplace = claude_marketplace_state()
    if marketplace is None:
        run_command(
            [
                "claude",
                "plugin",
                "marketplace",
                "add",
                f"GrowthX-Club/builder-pulse-plugin@{TARGET_RELEASE}",
                "--scope",
                "user",
            ]
        )
        marketplace = claude_marketplace_state()
        if marketplace is None:
            raise SetupError("Claude Code did not add the GrowthX marketplace")
    verify_claude_marketplace(marketplace, expected_commit)


def remove_claude_plugin(plugin_id: str) -> None:
    run_command(
        [
            "claude",
            "plugin",
            "uninstall",
            plugin_id,
            "--scope",
            "user",
            "--keep-data",
            "--yes",
        ]
    )


def expected_claude_package_root() -> Path:
    platform_directory = "windows" if os.name == "nt" else "posix"
    return Path(__file__).resolve().parent.parent / "claude-plugins" / platform_directory


def verify_claude_install_tree(root: Path) -> None:
    """Prove Claude installed the exact package from this immutable checkout."""
    source_root = expected_claude_package_root().resolve(strict=True)
    expected_files: dict[Path, bytes] = {}
    installed_files: dict[Path, bytes] = {}
    try:
        for source in source_root.rglob("*"):
            if source.is_symlink():
                raise SetupError("The Claude Code Builder Pulse release contains a symlink")
            if source.is_file():
                expected_files[source.relative_to(source_root)] = source.read_bytes()
        for installed in root.rglob("*"):
            if installed.is_symlink():
                raise SetupError("The installed Claude Code Builder Pulse package contains a symlink")
            if installed.is_file():
                installed_files[installed.relative_to(root)] = installed.read_bytes()
    except OSError as exc:
        raise SetupError("The installed Claude Code Builder Pulse package is unreadable") from exc
    if not expected_files or installed_files != expected_files:
        raise SetupError(
            "The installed Claude Code Builder Pulse package differs from the immutable release"
        )


def install_claude_release(expected_commit: str) -> Path:
    existing_entries = installed_claude_builders()
    plugin_id = (
        CLAUDE_WINDOWS_PLUGIN if os.name == "nt" else CLAUDE_POSIX_PLUGIN
    )
    target_was_installed = any(
        entry.get("id") == plugin_id for entry in existing_entries
    )

    # Claude removes plugin registrations when their marketplace is removed.
    # Each immutable Builder Pulse release therefore gets a release-scoped
    # marketplace ID. Install and verify the replacement before removing any
    # older Builder Pulse plugin, so a failed update leaves working hooks intact.
    # A marketplace can declare a command that Claude executes when --yes is
    # used. Prove its exact source and release checkout before invoking any
    # plugin install/update command; a matching display name is not provenance.
    ensure_claude_marketplace(expected_commit)
    run_command(
        [
            "claude",
            "plugin",
            "update" if target_was_installed else "install",
            plugin_id,
            "--scope",
            "user",
            "--yes",
        ]
    )
    matches = [
        entry
        for entry in installed_claude_builders()
        if entry.get("id") == plugin_id
    ]
    if len(matches) != 1:
        raise SetupError("Claude Code did not install Builder Pulse exactly once")
    entry = matches[0]
    if entry.get("enabled") is not True or entry.get("version") != TARGET_RELEASE.removeprefix("v"):
        raise SetupError("Claude Code installed an unexpected Builder Pulse version")
    install_path = entry.get("installPath")
    if not isinstance(install_path, str) or not install_path:
        raise SetupError("Claude Code did not report the Builder Pulse install path")
    root = Path(install_path).expanduser().resolve(strict=False)
    manifest = read_object(root / ".claude-plugin" / "plugin.json", required=True)
    if manifest.get("version") != TARGET_RELEASE.removeprefix("v"):
        raise SetupError("The installed Claude Code Builder Pulse manifest is invalid")
    verify_claude_install_tree(root)

    # Only remove older releases or the other OS package after the replacement
    # is proven. Dormant older marketplace declarations are harmless and remain
    # available for recovery; their plugins no longer register hooks.
    for existing_entry in existing_entries:
        existing_id = existing_entry.get("id")
        if isinstance(existing_id, str) and existing_id != plugin_id:
            remove_claude_plugin(existing_id)
    return root


def cleanup_partial() -> None:
    try:
        remove_current(
            plugin_installed=installed_builder() is not None,
            marketplace_configured=marketplace_state() is not None,
        )
    except SetupError:
        pass


def verified_remote_tag_commit(release: str) -> str:
    direct_ref = f"refs/tags/{release}"
    peeled_ref = f"{direct_ref}^{{}}"
    output = str(
        run_command(
            [
                "git",
                "ls-remote",
                "--exit-code",
                REPOSITORY,
                direct_ref,
                peeled_ref,
            ]
        )
    )
    refs: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-fA-F]{40}", fields[0]):
            raise SetupError("GitHub returned an invalid Builder Pulse release tag")
        if fields[1] in {direct_ref, peeled_ref}:
            refs[fields[1]] = fields[0].lower()
    commit = refs.get(peeled_ref) or refs.get(direct_ref)
    if commit is None:
        raise SetupError("The immutable Builder Pulse release tag could not be verified")
    return commit


def verify_release_exists(release: str) -> str:
    commit = verified_remote_tag_commit(release)
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
    return commit


def parse_activation(output: str) -> dict[str, Any]:
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        raise SetupError("Builder Pulse activation returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise SetupError("Builder Pulse activation returned an invalid result")
    return result


def activate(cli: Path, agent_platform: str = "codex") -> dict[str, Any]:
    try:
        completed = subprocess.run(
            cli_command(cli, "activate", "--agent", agent_platform),
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError("Builder Pulse activation could not start") from exc
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


def claimed_identity_fields(identity: Any) -> dict[str, str]:
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


def local_claimed_identity_fields(identity: Any) -> dict[str, str]:
    if not isinstance(identity, dict):
        raise SetupError("The existing Builder Pulse identity is invalid")
    fields = {
        key: identity.get(key)
        for key in ("installationId", "builderId", "memberId")
    }
    if (
        not isinstance(identity.get("installationToken"), str)
        or not identity.get("installationToken")
        or not isinstance(identity.get("claimedEndpoint"), str)
        or not identity.get("claimedEndpoint")
        or any(not isinstance(value, str) or not value for value in fields.values())
    ):
        raise SetupError("The existing Builder Pulse identity is not fully claimed")
    return {key: str(value) for key, value in fields.items()}


def claimed_identity(cli: Path) -> dict[str, str]:
    status = run_command(cli_command(cli, "status", "--json"), expect_json=True)
    identity = status.get("identity") if isinstance(status, dict) else None
    return claimed_identity_fields(identity)


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
    codex_available = shutil.which("codex") is not None
    claude_available = shutil.which("claude") is not None
    if not codex_available and not claude_available:
        raise SetupError("Builder Pulse requires Codex, Claude Code, or both")
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

    target_commit = verify_release_exists(TARGET_RELEASE)
    # Verify the installer itself before reading or changing identity state.
    # The runtime installer repeats this check immediately before copying code.
    verified_installer_checkout(target_commit)
    previous = installed_builder() if codex_available else None
    previous_marketplace = marketplace_state() if codex_available else None
    if previous_marketplace:
        source = previous_marketplace.get("marketplaceSource")
        repository = source.get("source") if isinstance(source, dict) else None
        if not approved_existing_repository(repository):
            raise SetupError("The GrowthX marketplace name points to a different source")

    # Move the old Codex-owned identity to the agent-neutral data directory
    # before pausing or changing either agent. The old directory is retained.
    migrate_existing_data_to_shared(previous)

    # Resolve and remotely verify the exact currently installed commit before
    # reading its data or changing either Codex registration. A tag is not
    # rollback provenance because it may move later.
    rollback_source = verified_rollback_source(previous, previous_marketplace)
    preserved_identity: dict[str, str] | None = None
    if reuse_existing_claim:
        preserved_identity = local_claimed_identity_fields(
            authoritative_identity(existing_plugin_data_dir(previous))
        )

    # Claude executes this shared runtime directly. Install it only after any
    # legacy Codex identity has moved into the shared root so runtime creation
    # cannot make that migration look like a conflicting identity directory.
    shared_cli = install_shared_runtime(target_commit)

    # Stop older machine-wide capture without executing the old checkout. The
    # authentication token remains quarantined on any failed replacement, so
    # an inherited legacy BUILDER_PULSE_ENABLED=1 cannot resume capture.
    paused = pause_existing_capture(previous)
    codex_cli: Path | None = None
    if codex_available:
        remove_current(
            plugin_installed=previous is not None,
            marketplace_configured=previous_marketplace is not None,
            rollback_source=rollback_source,
        )
        try:
            codex_cli = install_release(TARGET_RELEASE, expected_commit=target_commit)
        except SetupError:
            cleanup_partial()
            if rollback_source is not None:
                try:
                    install_verified_rollback(rollback_source)
                except SetupError as rollback_error:
                    raise SetupError(
                        "Builder Pulse update failed and the previous version could not be restored: "
                        f"{rollback_error}"
                    ) from rollback_error
            raise

    if claude_available:
        install_claude_release(target_commit)
    cli = codex_cli or shared_cli

    # A bare package is inert. Preserve that state while restoring or claiming
    # identity and adding the member-confirmed project to the allowlist.
    run_command(cli_command(cli, "config", "set", "enabled", "false"))
    restore_paused_identity(cli, paused)
    if reuse_existing_claim:
        if claimed_identity(cli) != preserved_identity:
            raise SetupError("The Builder Pulse identity changed during repair")
    else:
        claim_env = dict(os.environ)
        claim_env["BUILDER_PULSE_INVITE_CODE"] = invite_code
        run_command(
            cli_command(cli, "claim", "--endpoint", endpoint),
            env=claim_env,
        )
    run_command(
        cli_command(
            cli,
            "work",
            "enroll",
            "--root",
            str(enrolled_root),
            "--project",
            confirmed_label,
        )
    )
    target_plugin_version = TARGET_RELEASE.removeprefix("v")
    resume_attempted = False
    delivery_data_dir = plugin_data_dir(cli)
    delivery_identity = authoritative_identity(delivery_data_dir)
    setup_succeeded = False
    setup_failure: BaseException | None = None
    try:
        # A network failure can happen after the service commits the resume but
        # before this process receives the acknowledgement. Treat every attempt
        # as potentially successful and restore the server barrier on any later
        # error, including an ambiguous resume response.
        resume_attempted = True
        resume_server_capture(delivery_identity, target_plugin_version)
        run_command(cli_command(cli, "config", "set", "enabled", "true"))
        activations: list[dict[str, Any]] = []
        if codex_available:
            activations.append(activate(codex_cli or cli, "codex"))
        if claude_available:
            activations.append(activate(cli, "claude_code"))
        for activation in activations:
            hooks_verified = bool(
                activation.get("hooksVerified") is True
                or activation.get("hooksTrusted") is True
            )
            if not (
                activation.get("activationReady") is True
                and hooks_verified
                and activation.get("serverVerified") is True
            ):
                if activation.get("reviewRequired") is True:
                    raise SetupError(
                        "Codex requires its official one-time Builder Pulse hook review"
                    )
                agent_name = (
                    "Claude Code"
                    if activation.get("agentPlatform") == "claude_code"
                    else "Codex"
                )
                raise SetupError(
                    f"Builder Pulse activation was not verified for {agent_name}"
                )
        run_command(cli_command(cli, "flush"))
        setup_succeeded = True
    except BaseException as exc:
        setup_failure = exc
        raise
    finally:
        cleanup_errors: list[str] = []
        if not setup_succeeded and resume_attempted:
            try:
                pause_server_capture(delivery_identity, target_plugin_version)
            except BaseException:
                cleanup_errors.append("server privacy-pause status is unknown")
        if not setup_succeeded:
            try:
                quarantine_local_capture(delivery_data_dir, delivery_identity)
            except BaseException:
                cleanup_errors.append("local capture could not be disabled")
        if not setup_succeeded and cleanup_errors and not isinstance(
            setup_failure, (KeyboardInterrupt, SystemExit)
        ):
            original = (
                str(setup_failure)
                if setup_failure is not None and str(setup_failure)
                else "Builder Pulse setup did not complete"
            )
            raise SetupError(
                f"{original}; " + "; ".join(cleanup_errors)
            ) from setup_failure
        if not setup_succeeded and cleanup_errors:
            print(
                "Builder Pulse emergency shutdown was incomplete: "
                + "; ".join(cleanup_errors)
                + ". Exit all running Claude Code and Codex sessions now.",
                file=sys.stderr,
            )


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
        print(f"Builder Pulse setup stopped: {exc}", file=sys.stderr)
        return 1
    print(
        "Builder Pulse is installed for every supported agent found on this computer. "
        "Only project folders you explicitly confirmed are enrolled; this confirmed "
        "project was added without removing prior confirmed projects. "
        "Exit all running Claude Code and Codex sessions, start a fresh session "
        "in each agent you use, then send one normal prompt in each to verify "
        "separate server receipts."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
