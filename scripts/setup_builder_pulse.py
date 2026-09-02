#!/usr/bin/env python3
"""Install or update Builder Pulse through one stable bootstrap command."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
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
TARGET_RELEASE = "v0.5.2"
CLAUDE_MARKETPLACE = f"growthx-builder-tools-{TARGET_RELEASE.replace('.', '-')}"
CLAUDE_POSIX_PLUGIN = f"builder-pulse-claude-posix@{CLAUDE_MARKETPLACE}"
CLAUDE_WINDOWS_PLUGIN = f"builder-pulse-claude-windows@{CLAUDE_MARKETPLACE}"
DEFAULT_ENDPOINT = "https://precious-ant-429.convex.site"
RELEASE_API = (
    "https://api.github.com/repos/GrowthX-Club/builder-pulse-plugin/releases/tags/"
)
RELEASE_RESPONSE_MAX_BYTES = 1024 * 1024
SETUP_LOG_KEEP = 10
SETUP_LOG_MAX_OUTPUT = 600
HOOK_REVIEW_EXIT_CODE = 3
# Entries the installer itself may create in the shared data directory before
# the legacy identity has been migrated. They never hold secrets.
INSTALLER_OWNED_SHARED_ENTRIES = frozenset({"logs"})
# Files the runtime may create in the shared directory before any claim exists
# (an unclaimed identity skeleton and its lock files). Together with the logs
# directory they never represent data worth preserving over a legacy claim.
UNCLAIMED_SKELETON_ENTRIES = frozenset(
    {
        "identity.json",
        "setup-paused-identity.json",
        ".lock",
        ".delivery.lock",
        ".scope-delivery.lock",
    }
)
CLAIM_KEYS = ("installationToken", "pendingInstallationToken", "builderId")
# Files an official tool or the OS may leave inside an installed package
# without changing any tracked file. Anything else in the checkout is a reason
# to refuse provenance.
CHECKOUT_NOISE_FILES = frozenset({".codex-marketplace-install.json", ".DS_Store"})
CHECKOUT_NOISE_PARTS = frozenset({"__pycache__"})
CHECKOUT_NOISE_SUFFIXES = (".pyc",)
DIAGNOSTIC_HEX_TOKEN_PATTERN = re.compile(r"\b[0-9a-fA-F]{64}\b")
DIAGNOSTIC_BEARER_PATTERN = re.compile(r"(?i)\bbearer[ \t]+[^\s\"',;]+")
DIAGNOSTIC_FIELD_PATTERN = re.compile(
    r"(?i)(\\?\"?(?:inviteCode|installationToken|pendingInstallationToken|"
    r"BUILDER_PULSE_INVITE_CODE)\\?\"?\s*[:=]\s*)"
    r"(\\?\"[^\"\\]*\\?\"|'[^']*'|[^\s,;}]+)"
)
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


class HookReviewRequired(Exception):
    """Setup finished but the agent must approve the hooks once before they run."""

    def __init__(self, message: str, agent_platform: str) -> None:
        super().__init__(message)
        self.agent_platform = agent_platform


class SetupLog:
    """Privacy-safe, append-only record of one installer run.

    Every line is redacted before it is written: invite codes, installation
    tokens, bearer values, and 64-hex secrets never reach disk. Failure to log
    never changes installer behaviour.
    """

    def __init__(self) -> None:
        self.path: Path | None = None
        self.secrets: list[str] = []
        self._buffer: list[str] = []

    def mask(self, secret: str | None) -> None:
        if isinstance(secret, str) and len(secret) >= 8 and secret not in self.secrets:
            self.secrets.append(secret)

    def redact(self, text: Any) -> str:
        redacted = str(text)
        for secret in self.secrets:
            redacted = redacted.replace(secret, "[redacted]")
        redacted = shorten_home(redacted)
        redacted = DIAGNOSTIC_HEX_TOKEN_PATTERN.sub("[redacted]", redacted)
        redacted = DIAGNOSTIC_BEARER_PATTERN.sub("Bearer [redacted]", redacted)
        redacted = DIAGNOSTIC_FIELD_PATTERN.sub(
            lambda match: f"{match.group(1)}[redacted]", redacted
        )
        return redacted

    def open(self, data_dir: Path) -> Path | None:
        if self.path is not None:
            return self.path
        try:
            log_dir = data_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(log_dir, 0o700)
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
            path = log_dir / f"setup-{stamp}.log"
            suffix = 1
            while path.exists():
                path = log_dir / f"setup-{stamp}-{suffix}.log"
                suffix += 1
            path.touch(mode=0o600)
            os.chmod(path, 0o600)
            self.path = path
            for line in self._buffer:
                self._append(line)
            self._buffer = []
            self.prune(log_dir)
        except OSError:
            self.path = None
        return self.path

    @staticmethod
    def prune(log_dir: Path) -> None:
        try:
            logs = sorted(
                (entry for entry in log_dir.glob("setup-*.log") if entry.is_file()),
                key=lambda entry: entry.name,
            )
            for stale in logs[:-SETUP_LOG_KEEP]:
                with contextlib.suppress(OSError):
                    stale.unlink()
        except OSError:
            pass

    def _append(self, line: str) -> None:
        assert self.path is not None
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def write(self, message: str, **fields: Any) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        extra = ""
        if fields:
            safe_fields = {
                key: self.redact(value) if isinstance(value, str) else value
                for key, value in fields.items()
            }
            extra = " " + json.dumps(
                safe_fields, sort_keys=True, default=str, ensure_ascii=False
            )
        line = self.redact(f"{stamp} {message}{extra}") + "\n"
        if self.path is None:
            self._buffer.append(line)
            if len(self._buffer) > 500:
                del self._buffer[0]
            return
        try:
            self._append(line)
        except OSError:
            pass

    def bounded(self, text: Any, limit: int = SETUP_LOG_MAX_OUTPUT) -> str:
        value = str(text or "").strip()
        if len(value) <= limit:
            return value
        return "…" + value[-limit:]


SETUP_LOG = SetupLog()


def shorten_home(text: str) -> str:
    """Replace the home directory prefix with ~ in logged text."""
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):
        return text
    if not home or home in {"/", "\\"}:
        return text
    candidates = {home}
    with contextlib.suppress(OSError):
        candidates.add(str(Path.home().resolve(strict=False)))
    candidates.add(home.replace("\\", "/"))
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate and candidate not in {"/", "\\"}:
            text = text.replace(candidate, "~")
    return text


def folder_label(value: str) -> str:
    """Log only the last path component of a project folder."""
    name = PurePath(str(value).replace("\\", "/")).name
    return f"…/{name}" if name else "…"


def display_arguments(arguments: list[str]) -> list[str]:
    """Return argv with secrets masked and project folders reduced to a label.

    The working directory, the enrolled project root, and any --project-root
    value are never written to the log; only their basename is.
    """
    shown: list[str] = []
    mask_next: str | None = None
    for argument in arguments:
        if mask_next == "secret":
            shown.append("[redacted]")
            mask_next = None
            continue
        if mask_next == "folder":
            shown.append(folder_label(argument))
            mask_next = None
            continue
        if argument in {"--code", "--token"}:
            shown.append(argument)
            mask_next = "secret"
            continue
        if argument in {"--root", "--project-root"}:
            shown.append(argument)
            mask_next = "folder"
            continue
        if argument.startswith("--code="):
            shown.append("--code=[redacted]")
            continue
        if argument.startswith(("--root=", "--project-root=")):
            flag, _, value = argument.partition("=")
            shown.append(f"{flag}={folder_label(value)}")
            continue
        shown.append(argument)
    return shown


class LocalPauseRollbackError(SetupError):
    def __init__(self, snapshot: LocalCaptureSnapshot, detail: str) -> None:
        super().__init__(detail)
        self.snapshot = snapshot


class RollbackSource(NamedTuple):
    version: str
    commit: str
    repository: str


class LocalCaptureSnapshot(NamedTuple):
    data_dir: Path
    identity: dict[str, Any] | None
    paused_identity: dict[str, Any] | None
    config: dict[str, Any] | None
    queues: tuple[tuple[str, bytes], ...]


class PausedCapture(NamedTuple):
    data_dir: Path
    identity: dict[str, Any]
    locations: tuple[LocalCaptureSnapshot, ...] = ()
    server_paused: bool = False
    plugin_version: str = ""


def is_filesystem_root(path: PurePath) -> bool:
    return bool(path.anchor) and path == type(path)(path.anchor)


def run_command(
    arguments: list[str],
    *,
    env: dict[str, str] | None = None,
    expect_json: bool = False,
) -> Any:
    display_command = arguments[0]
    if display_command in {"git", "codex", "claude"}:
        executable = shutil.which(display_command)
        if executable is None:
            raise SetupError(f"Command could not start: {display_command}")
        arguments = [executable, *arguments[1:]]
    shown = display_arguments(arguments)
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        SETUP_LOG.write("command could not start", argv=shown, error=str(exc))
        raise SetupError(f"Command could not start: {display_command}") from exc
    SETUP_LOG.write(
        "command finished",
        argv=shown,
        returncode=completed.returncode,
        stderr=SETUP_LOG.bounded(completed.stderr),
        stdout=SETUP_LOG.bounded(completed.stdout, 300),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SetupError(
            SETUP_LOG.redact(detail) or f"Command failed: {display_command}"
        )
    if not expect_json:
        return completed.stdout
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SetupError(f"{display_command} returned invalid JSON") from exc


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


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        handle.write(value)
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
    disposable_target = False
    if target.exists():
        if not source_has_identity or directory_holds_claim(target):
            return target
        if directory_holds_identity(target) and not shared_directory_is_disposable(target):
            # A claimed-looking record that is not a claim, next to other data:
            # an earlier partial migration. Fail closed.
            raise SetupError(
                "The shared Builder Pulse data directory exists without the prior identity"
            )
        # The installer creates only the secret-free logs directory before this
        # step, and the runtime may add an unclaimed identity skeleton. A shared
        # directory holding nothing else is still "absent" for migration
        # purposes; anything more is a partial earlier migration.
        if not shared_directory_is_disposable(target):
            raise SetupError(
                "The shared Builder Pulse data directory exists without the prior identity"
            )
        disposable_target = True
    if not source_has_identity:
        return target
    for candidate in source.rglob("*"):
        if candidate.is_symlink():
            raise SetupError("The existing Builder Pulse data contains a symbolic link")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}-migration"
    if temporary.exists():
        raise SetupError("A previous Builder Pulse data migration is incomplete")
    # Build the complete replacement beside the target, then swap it in with a
    # single rename. A failure at any point leaves the target exactly as it was
    # (no identity), so a retry migrates everything again instead of trusting a
    # half-copied directory.
    try:
        shutil.copytree(source, temporary, symlinks=False)
        if disposable_target and (target / "logs").is_dir():
            shutil.copytree(
                target / "logs", temporary / "logs", symlinks=False, dirs_exist_ok=True
            )
        if disposable_target:
            shutil.rmtree(target)
        os.replace(temporary, target)
    except OSError as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise SetupError("The existing Builder Pulse data could not be migrated safely") from exc
    return target


def existing_plugin_data_dir(installation: dict[str, Any] | None) -> Path:
    del installation
    return canonical_plugin_data_dir()


def directory_holds_identity(directory: Path) -> bool:
    return bool(
        (directory / "identity.json").is_file()
        or (directory / "setup-paused-identity.json").is_file()
    )


def identity_holds_claim(identity: Any) -> bool:
    """True when an identity record carries something worth preserving."""
    return isinstance(identity, dict) and any(
        isinstance(identity.get(key), str) and identity.get(key)
        for key in CLAIM_KEYS
    )


def directory_holds_claim(directory: Path) -> bool:
    """True when identity.json or the paused copy carries a claim or pending token.

    The runtime's hook creates an unclaimed skeleton (installationId and
    promptCapture only) the first time it runs against an empty directory.
    Such a skeleton must never mask a complete legacy identity.
    """
    for name in ("setup-paused-identity.json", "identity.json"):
        path = directory / name
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if identity_holds_claim(record):
            return True
    return False


def shared_directory_is_disposable(directory: Path) -> bool:
    """True when the shared directory holds only logs and an unclaimed skeleton."""
    try:
        entries = {entry.name for entry in directory.iterdir()}
    except OSError:
        return False
    if not entries <= (INSTALLER_OWNED_SHARED_ENTRIES | UNCLAIMED_SKELETON_ENTRIES):
        return False
    return not directory_holds_claim(directory)


def repair_identity_dir(installation: dict[str, Any] | None) -> Path:
    """Locate the identity a repair must preserve.

    The shared directory wins whenever it holds an identity. A member whose
    earlier v0.5.x attempt stopped before the migration step still has the
    claimed identity only in the legacy Codex-owned directory, so fall back to
    it there; the migration step copies it into the shared directory exactly as
    for any other upgrade, and the pause step still refuses two directories
    whose installation IDs differ.
    """
    shared = existing_plugin_data_dir(installation)
    if directory_holds_claim(shared):
        return shared
    legacy_cli: Path | None = None
    if installation is not None:
        try:
            legacy_cli = installed_cli(installation)
        except SetupError:
            legacy_cli = None
    legacy = legacy_codex_plugin_data_dir(legacy_cli)
    if directory_holds_claim(legacy):
        return legacy
    return shared


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


def local_capture_snapshot(data_dir: Path) -> LocalCaptureSnapshot:
    def optional_object(path: Path) -> dict[str, Any] | None:
        return read_object(path, required=True) if path.exists() else None

    queues: list[tuple[str, bytes]] = []
    try:
        for filename in ("outbox.jsonl", "prompt-outbox.jsonl", "quarantine.jsonl"):
            path = data_dir / filename
            if path.exists():
                if path.is_symlink() or not path.is_file():
                    raise SetupError(f"Builder Pulse data is invalid: {filename}")
                queues.append((filename, path.read_bytes()))
    except OSError as exc:
        raise SetupError("Builder Pulse pending delivery data is unreadable") from exc
    return LocalCaptureSnapshot(
        data_dir=data_dir.resolve(strict=False),
        identity=optional_object(data_dir / "identity.json"),
        paused_identity=optional_object(data_dir / "setup-paused-identity.json"),
        config=optional_object(data_dir / "config.json"),
        queues=tuple(queues),
    )


def restore_local_capture_snapshot(snapshot: LocalCaptureSnapshot) -> None:
    data_dir = snapshot.data_dir

    def restore_object(path: Path, value: dict[str, Any] | None) -> None:
        if value is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            atomic_write_object(path, value)

    restore_object(data_dir / "identity.json", snapshot.identity)
    restore_object(
        data_dir / "setup-paused-identity.json", snapshot.paused_identity
    )
    restore_object(data_dir / "config.json", snapshot.config)
    queued_names = {filename for filename, _contents in snapshot.queues}
    for filename in ("outbox.jsonl", "prompt-outbox.jsonl", "quarantine.jsonl"):
        path = data_dir / filename
        if filename not in queued_names:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    for filename, contents in snapshot.queues:
        atomic_write_bytes(data_dir / filename, contents)


def pause_local_capture(
    data_dir: Path, identity: dict[str, Any]
) -> LocalCaptureSnapshot:
    """Quarantine one data root while retaining an exact in-process rollback."""
    with exclusive_file_lock(data_dir / ".delivery.lock"):
        with exclusive_file_lock(data_dir / ".scope-delivery.lock"):
            with exclusive_file_lock(data_dir / ".lock"):
                snapshot = local_capture_snapshot(data_dir)
                try:
                    identity_path = data_dir / "identity.json"
                    paused_path = data_dir / "setup-paused-identity.json"
                    config_path = data_dir / "config.json"
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
                    for filename in (
                        "outbox.jsonl",
                        "prompt-outbox.jsonl",
                        "quarantine.jsonl",
                    ):
                        try:
                            (data_dir / filename).unlink()
                        except FileNotFoundError:
                            pass
                except BaseException as pause_error:
                    try:
                        restore_local_capture_snapshot(snapshot)
                    except BaseException as restore_error:
                        raise LocalPauseRollbackError(
                            snapshot,
                            "Builder Pulse could not be disabled locally and its "
                            f"previous state could not be restored: {restore_error}",
                        ) from pause_error
                    raise
                return snapshot


def pause_existing_capture(
    installation: dict[str, Any] | None,
) -> PausedCapture:
    data_dir = existing_plugin_data_dir(installation)
    identity = authoritative_identity(data_dir)
    installed_version = (
        installation.get("version") if isinstance(installation, dict) else None
    )
    locations: list[LocalCaptureSnapshot] = []
    legacy_data_dir: Path | None = None
    legacy_identity: dict[str, Any] = {}
    if installation is not None:
        try:
            legacy_cli = installed_cli(installation)
        except SetupError:
            legacy_cli = None
        candidate = legacy_codex_plugin_data_dir(legacy_cli)
        if candidate != data_dir.resolve(strict=False) and candidate.exists():
            legacy_data_dir = candidate
            legacy_identity = authoritative_identity(candidate)
            if (
                identity
                and identity_holds_claim(identity)
                and legacy_identity.get("installationId")
                != identity.get("installationId")
            ):
                raise SetupError("The legacy and shared Builder Pulse identities differ")

    server_pause_error: BaseException | None = None
    server_paused = False
    try:
        server_paused = pause_server_capture(
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
        locations.append(pause_local_capture(data_dir, identity))
        if legacy_data_dir is not None:
            locations.append(pause_local_capture(legacy_data_dir, legacy_identity))
    except BaseException as exc:
        restore_errors: list[str] = []
        if isinstance(exc, LocalPauseRollbackError):
            locations.append(exc.snapshot)
        for snapshot in reversed(locations):
            try:
                with exclusive_file_lock(snapshot.data_dir / ".delivery.lock"):
                    with exclusive_file_lock(snapshot.data_dir / ".scope-delivery.lock"):
                        with exclusive_file_lock(snapshot.data_dir / ".lock"):
                            restore_local_capture_snapshot(snapshot)
            except BaseException as restore_error:
                restore_errors.append(str(restore_error))
        if server_paused and not restore_errors:
            try:
                resume_server_capture(
                    identity,
                    str(installed_version or TARGET_RELEASE.removeprefix("v")),
                )
            except BaseException as restore_error:
                restore_errors.append(str(restore_error))
        detail = (
            " GrowthX server privacy-pause status is also unknown."
            if server_pause_error is not None
            else ""
        )
        if restore_errors:
            detail += " The previous capture state could not be restored completely."
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            print(
                "Builder Pulse pause was interrupted; local and server rollback was attempted.",
                file=sys.stderr,
            )
            raise
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
    return PausedCapture(
        data_dir=data_dir,
        identity=identity,
        locations=tuple(locations),
        server_paused=server_paused,
        plugin_version=str(installed_version or TARGET_RELEASE.removeprefix("v")),
    )


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


def restore_previous_capture(paused: PausedCapture | None) -> None:
    """Restore every pre-upgrade local state and the prior server policy."""
    if paused is None:
        return
    locations = paused.locations or (
        LocalCaptureSnapshot(
            paused.data_dir,
            paused.identity,
            None,
            None,
            (),
        ),
    )
    for snapshot in locations:
        with exclusive_file_lock(snapshot.data_dir / ".delivery.lock"):
            with exclusive_file_lock(snapshot.data_dir / ".scope-delivery.lock"):
                with exclusive_file_lock(snapshot.data_dir / ".lock"):
                    restore_local_capture_snapshot(snapshot)
    if paused.server_paused:
        resume_server_capture(paused.identity, paused.plugin_version)


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
    )
    if not checkout_is_pristine(checkout_changes):
        raise SetupError(
            "The existing Builder Pulse checkout has modified, untracked, or ignored files"
        )
    commit = str(
        run_command(["git", "-C", str(resolved), "rev-parse", "HEAD"])
    ).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SetupError("The existing Builder Pulse checkout commit is invalid")
    return repository, commit


def checkout_noise(path: str) -> bool:
    """True for files an official tool or the OS may leave in a clean package."""
    normalized = path.strip().strip('"')
    if not normalized:
        return True
    parts = PurePath(normalized.replace("\\", "/")).parts
    if not parts:
        return True
    if parts[-1] in CHECKOUT_NOISE_FILES:
        return True
    if any(part in CHECKOUT_NOISE_PARTS for part in parts):
        return True
    return parts[-1].endswith(CHECKOUT_NOISE_SUFFIXES)


def checkout_is_pristine(porcelain: str) -> bool:
    """Accept only tracked-file cleanliness plus allowlisted untracked noise.

    Codex writes `.codex-marketplace-install.json` into every installed package
    when it refreshes a marketplace, Finder writes `.DS_Store`, and Python may
    write bytecode caches. None of those can alter the executed scripts, so
    they must not block an upgrade; every other change still does.
    """
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        path = line[3:] if len(line) > 3 else ""
        if status in {"??", "!!"}:
            if checkout_noise(path):
                continue
            return False
        return False
    return True


def verify_remote_commit(repository: str, commit: str) -> None:
    """Prove the exact commit exists on the approved remote with git itself.

    The GitHub commits API embeds every file patch and exceeds any sane read
    cap for large merges, and it is rate limited per address. A shallow fetch
    of the single object has neither problem and fails closed for unknown
    commits ("not our ref").
    """
    slug = repository_slug(repository)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SetupError("The previous Builder Pulse commit could not be verified")
    probe = tempfile.mkdtemp(prefix="builder-pulse-provenance-")
    # A public fetch must never block on a credential prompt.
    git_env = dict(os.environ)
    git_env["GIT_TERMINAL_PROMPT"] = "0"
    git_env["GIT_ASKPASS"] = "echo"
    try:
        try:
            run_command(["git", "init", "--quiet", "--bare", probe], env=git_env)
            run_command(
                [
                    "git",
                    "-C",
                    probe,
                    "fetch",
                    "--quiet",
                    "--depth",
                    "1",
                    f"https://github.com/{slug}.git",
                    commit,
                ],
                env=git_env,
            )
            object_type = str(
                run_command(["git", "-C", probe, "cat-file", "-t", commit], env=git_env)
            ).strip()
        except SetupError as exc:
            raise SetupError(
                "The previous Builder Pulse commit could not be verified on GitHub"
            ) from exc
        if object_type != "commit":
            raise SetupError("The previous Builder Pulse commit could not be verified")
    finally:
        shutil.rmtree(probe, ignore_errors=True)


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

    # Claude Code may materialize a marketplace as either a Git checkout or a
    # Git-less snapshot with a .gcs-sha revision marker. Validate whichever
    # immutable-revision representation it supplied, then prove every package
    # file matches the already verified installer checkout. Source metadata
    # alone is insufficient because a local marketplace cache can be modified.
    root = Path(install_location).expanduser().resolve(strict=False)
    marker = root / ".gcs-sha"
    if marker.is_file():
        try:
            commit = marker.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SetupError("The Claude Code GrowthX marketplace is unreadable") from exc
        if not re.fullmatch(r"[0-9a-f]{40}", commit) or commit != expected_commit:
            raise SetupError(
                "The Claude Code GrowthX marketplace does not match the immutable release"
            )
    elif (root / ".git").exists():
        repository, commit = verified_git_checkout(root)
        if (
            normalized_repository(repository) != normalized_repository(REPOSITORY)
            or commit != expected_commit
        ):
            raise SetupError(
                "The Claude Code GrowthX marketplace does not match the immutable release"
            )
    else:
        raise SetupError("The Claude Code GrowthX marketplace is unreadable")

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


def target_claude_plugin_id() -> str:
    return CLAUDE_WINDOWS_PLUGIN if os.name == "nt" else CLAUDE_POSIX_PLUGIN


def preflight_agent_installation_support(
    *, codex_available: bool, claude_available: bool
) -> None:
    """Prove installed agent CLIs can parse the target package before replacement."""
    if codex_available:
        run_command(["codex", "plugin", "marketplace", "add", "--help"])
    if claude_available:
        source_root = Path(__file__).resolve().parent.parent
        run_command(
            [
                "claude",
                "plugin",
                "validate",
                str(source_root / ".claude-plugin" / "marketplace.json"),
            ]
        )
        run_command(
            [
                "claude",
                "plugin",
                "validate",
                str(expected_claude_package_root()),
            ]
        )


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


def install_claude_release(
    expected_commit: str,
    *,
    existing_entries: list[dict[str, Any]] | None = None,
    remove_previous: bool = True,
) -> Path:
    if existing_entries is None:
        existing_entries = installed_claude_builders()
    plugin_id = target_claude_plugin_id()
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

    # Direct callers may remove older registrations after the replacement is
    # proven. Transactional setup defers that cleanup until both agents and
    # server delivery are verified, keeping the prior Claude plugin available
    # throughout every fallible installation and activation step.
    if remove_previous:
        remove_previous_claude_builders(existing_entries, plugin_id)
    return root


def remove_previous_claude_builders(
    existing_entries: list[dict[str, Any]],
    target_plugin_id: str,
) -> None:
    for existing_entry in existing_entries:
        existing_id = existing_entry.get("id")
        if isinstance(existing_id, str) and existing_id != target_plugin_id:
            remove_claude_plugin(existing_id)


def cleanup_partial() -> None:
    remove_current(
        plugin_installed=installed_builder() is not None,
        marketplace_configured=marketplace_state() is not None,
    )


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
            raw = response.read(RELEASE_RESPONSE_MAX_BYTES + 1)
    except urlerror.HTTPError as exc:
        if exc.code in {403, 429}:
            reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
            wait = ""
            try:
                if reset:
                    seconds = int(reset) - int(
                        dt.datetime.now(dt.timezone.utc).timestamp()
                    )
                    wait = f" Retry in about {max(1, (seconds + 59) // 60)} minutes."
            except ValueError:
                wait = ""
            raise SetupError(
                "GitHub rate limit reached while verifying the Builder Pulse release."
                + wait
            ) from exc
        raise SetupError("The immutable Builder Pulse release could not be verified") from exc
    except (OSError, ValueError, urlerror.URLError) as exc:
        raise SetupError("The immutable Builder Pulse release could not be verified") from exc
    if len(raw) > RELEASE_RESPONSE_MAX_BYTES:
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
    result: dict[str, Any] | None = None
    if output:
        try:
            result = parse_activation(output)
        except SetupError:
            result = None
    SETUP_LOG.write(
        "activation finished",
        agentPlatform=agent_platform,
        returncode=completed.returncode,
        result=result if isinstance(result, dict) else None,
        stderr=SETUP_LOG.bounded(completed.stderr),
    )
    if isinstance(result, dict):
        if completed.returncode == 0:
            return result
        if result.get("reviewRequired") is True:
            return result
    detail = SETUP_LOG.redact(completed.stderr.strip())
    reason_parts: list[str] = []
    if isinstance(result, dict):
        for key in ("agentPlatform", "hookStatus", "hookCount", "stage"):
            if result.get(key) not in (None, ""):
                reason_parts.append(f"{key}={result[key]}")
        if isinstance(result.get("detail"), str) and result["detail"]:
            reason_parts.append(str(result["detail"]))
    reason = "; ".join(reason_parts)
    message = "Builder Pulse activation failed"
    if reason:
        message += f" ({reason})"
    if detail:
        message += f": {detail}"
    raise SetupError(message)


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


class SetupOutcome(NamedTuple):
    cli: Path
    enrolled_root: Path | None
    review_required: tuple[str, ...] = ()


def temporary_roots() -> tuple[Path, ...]:
    candidates = [Path(tempfile.gettempdir())]
    for literal in ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders"):
        candidates.append(Path(literal))
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=False)
        except OSError:
            continue
        if resolved not in roots and not is_filesystem_root(resolved):
            roots.append(resolved)
    return tuple(roots)


def is_builder_pulse_checkout(root: Path) -> bool:
    """True when the folder, or any parent, is a Builder Pulse package checkout."""
    for candidate in (root, *root.parents):
        if (candidate / "scripts" / "setup_builder_pulse.py").is_file():
            return True
        manifest = candidate / ".codex-plugin" / "plugin.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name") == "builder-pulse":
            return True
    return False


def enrollment_refusal(root: Path) -> str | None:
    """Return why a folder may never be enrolled, or None when it is acceptable."""
    home = Path.home().resolve(strict=False)
    if root == home or root in home.parents or is_filesystem_root(root):
        return (
            "The confirmed folder must be a project folder, not the home, one of "
            "its parents, or the filesystem root"
        )
    for temporary in temporary_roots():
        if root == temporary or temporary in root.parents:
            return (
                f"{root} is a temporary folder, not your project; run the installer "
                "again from inside the project folder Builder Pulse should monitor"
            )
    if is_builder_pulse_checkout(root):
        return f"{root} is the Builder Pulse installer folder, not your project"
    return None


def hook_review_message(agent_platform: str, cli: Path, enrolled_root: Path | None) -> str:
    folder = str(enrolled_root) if enrolled_root is not None else "an enrolled project folder"
    python_name = "py -3" if os.name == "nt" else "python3"
    return (
        "Builder Pulse is installed but Codex has not approved its hooks yet. "
        f"One-time step: start Codex inside {folder}; it warns that hooks need "
        "review. Run /hooks, select the builder-pulse hooks and trust them (Codex "
        "desktop app: type /hooks in the composer). Telemetry starts as soon as they "
        "are trusted - no rerun of this installer is needed. To confirm later: "
        f"{python_name} {cli} activate --agent {agent_platform}"
    )


def setup(
    invite_code: str,
    endpoint: str,
    project_root: str | Path,
    project_label: str,
    *,
    reuse_existing_claim: bool = False,
) -> SetupOutcome:
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
    SETUP_LOG.mask(invite_code)
    SETUP_LOG.open(canonical_plugin_data_dir())
    SETUP_LOG.write(
        "setup started",
        release=TARGET_RELEASE,
        mode="repair" if reuse_existing_claim else "setup",
        codex=codex_available,
        claude=claude_available,
        python=".".join(str(part) for part in sys.version_info[:3]),
        platform=sys.platform,
    )

    # A repair never invents a project. It enrolls only a folder the member
    # named explicitly; the existing allowlist is preserved either way.
    enroll_requested = bool(str(project_root).strip()) or bool(project_label.strip())
    enrolled_root: Path | None = None
    confirmed_label = ""
    if enroll_requested or not reuse_existing_claim:
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
        refusal = enrollment_refusal(enrolled_root)
        if refusal:
            raise SetupError(refusal)

    target_commit = verify_release_exists(TARGET_RELEASE)
    SETUP_LOG.write("release verified", release=TARGET_RELEASE, commit=target_commit)
    # Verify the installer itself before reading or changing identity state.
    # The runtime installer repeats this check immediately before copying code.
    verified_installer_checkout(target_commit)
    preflight_agent_installation_support(
        codex_available=codex_available,
        claude_available=claude_available,
    )
    previous = installed_builder() if codex_available else None
    previous_marketplace = marketplace_state() if codex_available else None
    SETUP_LOG.write(
        "existing codex registration",
        installedVersion=previous.get("version") if isinstance(previous, dict) else None,
        marketplaceConfigured=previous_marketplace is not None,
    )
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
        identity_dir = repair_identity_dir(previous)
        preserved_identity = local_claimed_identity_fields(
            authoritative_identity(identity_dir)
        )
        SETUP_LOG.write(
            "existing identity verified",
            installationId=preserved_identity.get("installationId"),
            source="legacy"
            if identity_dir != existing_plugin_data_dir(previous)
            else "shared",
        )

    previous_claude = installed_claude_builders() if claude_available else []
    claude_target = target_claude_plugin_id()
    claude_target_was_installed = any(
        entry.get("id") == claude_target for entry in previous_claude
    )
    claude_install_attempted = False
    paused: PausedCapture | None = None
    codex_mutated = False
    codex_cli: Path | None = None
    delivery_data_dir: Path | None = None
    delivery_identity: dict[str, Any] | None = None
    resume_attempted = False
    setup_succeeded = False
    review_required: list[str] = []
    try:
        # Claude's release-scoped target is additive. Install and verify it while
        # every previous registration and Codex capture path are still intact.
        # A marketplace-format or package failure therefore cannot strand a
        # previously working member.
        if claude_available:
            claude_install_attempted = True
            install_claude_release(
                target_commit,
                existing_entries=previous_claude,
                remove_previous=False,
            )
            SETUP_LOG.write("claude package installed", pluginId=claude_target)

        # Move the old Codex-owned identity to the agent-neutral data directory
        # only after every target agent has proved it can install. The old
        # directory remains available for an exact rollback.
        migrate_existing_data_to_shared(previous)

        # Claude executes this immutable shared runtime directly.
        shared_cli = install_shared_runtime(target_commit)

        # Stop older machine-wide capture immediately before the one package
        # operation that cannot be side-by-side. Any failure below restores the
        # exact previous Codex package and capture state.
        paused = pause_existing_capture(previous)
        SETUP_LOG.write(
            "previous capture paused",
            serverPaused=paused.server_paused if paused is not None else None,
        )
        if codex_available:
            codex_mutated = True
            remove_current(
                plugin_installed=previous is not None,
                marketplace_configured=previous_marketplace is not None,
                rollback_source=rollback_source,
            )
            codex_cli = install_release(TARGET_RELEASE, expected_commit=target_commit)
            SETUP_LOG.write("codex package installed", cli=str(codex_cli))

        cli = codex_cli or shared_cli

        # A bare package is inert. Preserve that state while restoring or claiming
        # identity and adding the member-confirmed project to the allowlist.
        delivery_data_dir = plugin_data_dir(cli)
        run_command(cli_command(cli, "config", "set", "enabled", "false"))
        restore_paused_identity(cli, paused)
        if reuse_existing_claim:
            if claimed_identity(cli) != preserved_identity:
                raise SetupError("The Builder Pulse identity changed during repair")
            SETUP_LOG.write("identity restored")
        else:
            claim_env = dict(os.environ)
            claim_env["BUILDER_PULSE_INVITE_CODE"] = invite_code
            run_command(
                cli_command(cli, "claim", "--endpoint", endpoint),
                env=claim_env,
            )
            SETUP_LOG.write("installation claimed")
        if enrolled_root is not None:
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
            SETUP_LOG.write("project enrolled", rootLabel=enrolled_root.name)
        target_plugin_version = TARGET_RELEASE.removeprefix("v")
        delivery_identity = authoritative_identity(delivery_data_dir)
        # A network failure can happen after the service commits the resume but
        # before this process receives the acknowledgement. Treat every attempt
        # as potentially successful and restore the server barrier on any later
        # error, including an ambiguous resume response.
        resume_attempted = True
        resume_server_capture(delivery_identity, target_plugin_version)
        run_command(cli_command(cli, "config", "set", "enabled", "true"))
        SETUP_LOG.write("capture resumed")
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
            agent_platform = (
                "claude_code"
                if activation.get("agentPlatform") == "claude_code"
                else "codex"
            )
            if (
                activation.get("activationReady") is True
                and hooks_verified
                and activation.get("serverVerified") is True
            ):
                continue
            if activation.get("reviewRequired") is True:
                # The package, identity, enrollment, and server policy are all
                # verified. Untrusted hooks never execute, so leaving the
                # installation active is privacy-safe; only the member's one-time
                # /hooks approval is missing. Rolling back here would uninstall
                # the very hooks the member is asked to review.
                review_required.append(agent_platform)
                SETUP_LOG.write(
                    "hook review required",
                    agentPlatform=agent_platform,
                    hookStatus=activation.get("hookStatus"),
                )
                continue
            agent_name = "Claude Code" if agent_platform == "claude_code" else "Codex"
            reason = "; ".join(
                f"{key}={activation[key]}"
                for key in ("hookStatus", "detail")
                if activation.get(key) not in (None, "")
            )
            raise SetupError(
                f"Builder Pulse activation was not verified for {agent_name}"
                + (f" ({reason})" if reason else "")
            )
        run_command(cli_command(cli, "flush"))
        # The target package, hooks, server policy, and first delivery are now
        # verified. Only at this point may older Claude registrations be
        # removed. A cleanup failure leaves the verified target and capture
        # active instead of quarantining a working installation again.
        setup_succeeded = True
        SETUP_LOG.write("setup verified", reviewRequired=review_required)
        if claude_available:
            remove_previous_claude_builders(previous_claude, claude_target)
    except BaseException as setup_error:
        if setup_succeeded:
            raise
        SETUP_LOG.write("setup failed; rolling back", error=str(setup_error))

        rollback_errors: list[str] = []
        target_capture_safe = True
        if resume_attempted and delivery_identity is not None:
            try:
                pause_server_capture(delivery_identity, target_plugin_version)
            except BaseException as rollback_error:
                target_capture_safe = False
                rollback_errors.append(
                    f"target server privacy-pause status is unknown: {rollback_error}"
                )
        if delivery_data_dir is not None:
            try:
                current_identity = authoritative_identity(delivery_data_dir)
                quarantine_local_capture(delivery_data_dir, current_identity)
            except BaseException as rollback_error:
                target_capture_safe = False
                rollback_errors.append(
                    f"target local capture could not be disabled: {rollback_error}"
                )

        codex_restored = not codex_mutated
        if codex_mutated:
            try:
                cleanup_partial()
                if rollback_source is not None:
                    install_verified_rollback(rollback_source)
                codex_restored = True
            except BaseException as rollback_error:
                rollback_errors.append(f"previous Codex package: {rollback_error}")

        # install_claude_release may register the target and then fail during
        # version/tree verification. Re-read the registration list when
        # possible, but if that list is itself unavailable, still attempt the
        # uninstall: an unverified enabled hook must not survive rollback.
        claude_restored = True
        if (
            claude_available
            and claude_install_attempted
            and not claude_target_was_installed
        ):
            try:
                try:
                    target_is_installed = any(
                        entry.get("id") == claude_target
                        for entry in installed_claude_builders()
                    )
                except BaseException:
                    target_is_installed = True
                if target_is_installed:
                    remove_claude_plugin(claude_target)
            except BaseException as rollback_error:
                claude_restored = False
                rollback_errors.append(f"new Claude Code package: {rollback_error}")

        # Restore the exact pre-upgrade files, pending queues, and server policy
        # only after the target is privacy-safe and every prior package is back.
        if (
            paused is not None
            and target_capture_safe
            and codex_restored
            and claude_restored
        ):
            try:
                restore_previous_capture(paused)
            except BaseException as rollback_error:
                rollback_errors.append(f"previous capture state: {rollback_error}")

        SETUP_LOG.write("rollback finished", errors=rollback_errors)
        if rollback_errors and not isinstance(
            setup_error, (KeyboardInterrupt, SystemExit)
        ):
            raise SetupError(
                f"{setup_error}; the previous version could not be restored "
                "completely: " + "; ".join(rollback_errors)
            ) from setup_error
        if rollback_errors:
            print(
                "Builder Pulse rollback was incomplete: "
                + "; ".join(rollback_errors)
                + ". Exit all running Claude Code and Codex sessions now.",
                file=sys.stderr,
            )
        raise
    return SetupOutcome(cli, enrolled_root, tuple(review_required))


def open_setup_log_for_report() -> Path | None:
    """Open the log at its stable location under the shared data directory.

    Only the secret-free `logs` directory is created here; the migration step
    treats a shared directory holding nothing else as absent.
    """
    if SETUP_LOG.path is not None:
        return SETUP_LOG.path
    return SETUP_LOG.open(canonical_plugin_data_dir())


def prompt_for_project(current_folder: Path) -> tuple[str, str]:
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
    default_root: Path | None = current_folder
    if is_builder_pulse_checkout(current_folder):
        print(
            "The current folder is the Builder Pulse installer clone, not your project.",
            file=sys.stderr,
        )
        default_root = None
    else:
        print(f"- Current folder: {current_folder}", file=sys.stderr)
    if repository_root is not None and not is_builder_pulse_checkout(repository_root):
        print(
            f"- Nearest Git repository root: {repository_root}",
            file=sys.stderr,
        )
    prompt = "Which exact project folder should Builder Pulse monitor? "
    prompt += f"[{default_root}]: " if default_root is not None else "(type the full path): "
    project_root = ""
    while not project_root:
        entered_root = input(prompt).strip()
        project_root = entered_root or (str(default_root) if default_root is not None else "")
        if not project_root:
            print("A project folder path is required.", file=sys.stderr)
    project_label = input("Project name GrowthX should display: ").strip()
    return project_root, project_label


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
    SETUP_LOG.mask(invite_code)
    if not args.reuse_existing_claim and not invite_code and sys.stdin.isatty():
        invite_code = getpass.getpass("Builder Pulse invite code: ")
        SETUP_LOG.mask(invite_code)
    project_root = args.project_root or ""
    project_label = args.project_label or ""
    interactive = sys.stdin.isatty()
    if interactive and not project_root:
        current_folder = Path.cwd().resolve(strict=False)
        if args.reuse_existing_claim:
            answer = input(
                "Existing enrollments are kept. Enroll an additional project folder "
                "now? [y/N]: "
            ).strip().lower()
            if answer in {"y", "yes"}:
                project_root, project_label = prompt_for_project(current_folder)
        else:
            project_root, project_label = prompt_for_project(current_folder)
    elif interactive and not project_label:
        project_label = input("Project name GrowthX should display: ").strip()
    try:
        outcome = setup(
            invite_code,
            args.endpoint,
            project_root,
            project_label,
            reuse_existing_claim=args.reuse_existing_claim,
        )
    except SetupError as exc:
        log_path = open_setup_log_for_report()
        SETUP_LOG.write("setup stopped", error=str(exc))
        print(f"Builder Pulse setup stopped: {SETUP_LOG.redact(exc)}", file=sys.stderr)
        if log_path is not None:
            print(f"Details: {log_path}", file=sys.stderr)
        return 1

    log_path = open_setup_log_for_report()
    try:
        enrolled = str(run_command(cli_command(outcome.cli, "work", "list"))).strip()
    except SetupError:
        enrolled = ""
    if enrolled:
        print("Enrolled project folders:", file=sys.stderr)
        print(enrolled, file=sys.stderr)
    if outcome.review_required:
        for agent_platform in outcome.review_required:
            print(
                hook_review_message(agent_platform, outcome.cli, outcome.enrolled_root),
                file=sys.stderr,
            )
        if log_path is not None:
            print(f"Details: {log_path}", file=sys.stderr)
        SETUP_LOG.write("setup finished; hook review pending")
        return HOOK_REVIEW_EXIT_CODE
    print(
        "Builder Pulse is installed for every supported agent found on this computer. "
        "Only project folders you explicitly confirmed are enrolled; "
        + (
            "this confirmed project was added without removing prior confirmed projects. "
            if outcome.enrolled_root is not None
            else "prior confirmed projects were kept unchanged. "
        )
        + "Exit all running Claude Code and Codex sessions, start a fresh session "
        "in each agent you use, then send one normal prompt in each to verify "
        "separate server receipts."
    )
    if log_path is not None:
        print(f"Details: {log_path}", file=sys.stderr)
    SETUP_LOG.write("setup finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
