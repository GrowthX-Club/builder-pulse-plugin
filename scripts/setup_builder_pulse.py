#!/usr/bin/env python3
"""Install or update Builder Pulse through one stable bootstrap command."""

from __future__ import annotations

import argparse
import contextlib
import getpass
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


def configured_plugin_data_dir() -> Path | None:
    explicit = os.environ.get("BUILDER_PULSE_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)
    plugin_data = os.environ.get("PLUGIN_DATA") or os.environ.get(
        "CLAUDE_PLUGIN_DATA"
    )
    if plugin_data:
        return Path(plugin_data).expanduser().resolve(strict=False)
    return None


def canonical_plugin_data_dir() -> Path:
    configured = configured_plugin_data_dir()
    if configured is not None:
        return configured
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return (
        codex_home / "plugins" / "data" / f"builder-pulse-{MARKETPLACE}"
    ).expanduser().resolve(strict=False)


def plugin_data_dir(cli: Path) -> Path:
    configured = configured_plugin_data_dir()
    if configured is not None:
        return configured
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
    raise SetupError("The existing Builder Pulse data directory could not be derived safely")


def existing_plugin_data_dir(installation: dict[str, Any] | None) -> Path:
    if installation is None:
        return canonical_plugin_data_dir()
    return plugin_data_dir(installed_cli(installation))


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
    present = [
        isinstance(token, str) and bool(token),
        isinstance(endpoint, str) and bool(endpoint),
        isinstance(installation_id, str) and bool(installation_id),
    ]
    if not any(present):
        return False
    if not all(present):
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
    except (OSError, ValueError, urlerror.URLError) as exc:
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
    except (OSError, ValueError, urlerror.URLError) as exc:
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


def pause_existing_capture(
    installation: dict[str, Any] | None,
) -> PausedCapture:
    data_dir = existing_plugin_data_dir(installation)
    identity_path = data_dir / "identity.json"
    paused_path = data_dir / "setup-paused-identity.json"
    config_path = data_dir / "config.json"

    identity = authoritative_identity(data_dir)
    installed_version = (
        installation.get("version") if isinstance(installation, dict) else None
    )
    pause_server_capture(
        identity,
        str(installed_version or TARGET_RELEASE.removeprefix("v")),
    )

    # The server barrier above rejects an old process that already cached this
    # token. The local quarantine then stops new delivery and queueing even when
    # no plugin is currently registered or an old session inherited an enable
    # environment override. The complete identity is restored only after the
    # verified replacement is installed disabled.
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


def activate(cli: Path) -> dict[str, Any]:
    completed = subprocess.run(
        cli_command(cli, "activate"),
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

    target_commit = verify_release_exists(TARGET_RELEASE)
    previous = installed_builder()
    previous_marketplace = marketplace_state()
    if previous_marketplace:
        source = previous_marketplace.get("marketplaceSource")
        repository = source.get("source") if isinstance(source, dict) else None
        if not approved_existing_repository(repository):
            raise SetupError("The GrowthX marketplace name points to a different source")

    # Resolve and remotely verify the exact currently installed commit before
    # reading its data or changing either Codex registration. A tag is not
    # rollback provenance because it may move later.
    rollback_source = verified_rollback_source(previous, previous_marketplace)
    preserved_identity: dict[str, str] | None = None
    if reuse_existing_claim:
        preserved_identity = local_claimed_identity_fields(
            authoritative_identity(existing_plugin_data_dir(previous))
        )

    # Stop older machine-wide capture without executing the old checkout. The
    # authentication token remains quarantined on any failed replacement, so
    # an inherited legacy BUILDER_PULSE_ENABLED=1 cannot resume capture.
    paused = pause_existing_capture(previous)
    remove_current(
        plugin_installed=previous is not None,
        marketplace_configured=previous_marketplace is not None,
        rollback_source=rollback_source,
    )
    try:
        cli = install_release(TARGET_RELEASE, expected_commit=target_commit)
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

    # A bare package is inert. Preserve that state while restoring or claiming
    # identity and replacing the project allowlist.
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
            "--replace-existing",
        )
    )
    target_plugin_version = TARGET_RELEASE.removeprefix("v")
    resume_attempted = False
    delivery_identity = authoritative_identity(plugin_data_dir(cli))
    try:
        # A network failure can happen after the service commits the resume but
        # before this process receives the acknowledgement. Treat every attempt
        # as potentially successful and restore the server barrier on any later
        # error, including an ambiguous resume response.
        resume_attempted = True
        resume_server_capture(delivery_identity, target_plugin_version)
        run_command(cli_command(cli, "config", "set", "enabled", "true"))
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
        run_command(cli_command(cli, "flush"))
    except SetupError as setup_error:
        pause_error: SetupError | None = None
        if resume_attempted:
            try:
                pause_server_capture(delivery_identity, target_plugin_version)
            except SetupError as exc:
                pause_error = exc
        try:
            run_command(cli_command(cli, "config", "set", "enabled", "false"))
        except SetupError as disable_error:
            pause_detail = (
                f"; server capture could not be paused after failure: {pause_error}"
                if pause_error is not None
                else ""
            )
            raise SetupError(
                f"{setup_error}{pause_detail}; capture could not be disabled after failure: "
                f"{disable_error}"
            ) from disable_error
        if pause_error is not None:
            raise SetupError(
                f"{setup_error}; server capture could not be paused after failure: "
                f"{pause_error}"
            ) from pause_error
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
        "The prior project allowlist was replaced; only the confirmed project "
        "folder is enrolled. "
        "Exit all running Codex sessions, start a fresh Codex session, "
        "then send one normal prompt to verify server receipt."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
