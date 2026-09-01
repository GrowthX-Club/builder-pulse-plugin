#!/usr/bin/env python3
"""Builder Pulse: privacy-bounded agent lifecycle and prompt telemetry.

Hook payloads can contain prompts, commands, paths, source, and tool I/O. This
module may inspect a command in memory to classify a state, but it never writes
or forwards commands, paths, source, tool I/O, transcripts, or assistant
responses. Primary UserPromptSubmit text is separately redacted, bounded, and
queued for learning feedback. Only state transitions and a 15-minute heartbeat
can create a lifecycle event. An already-due primary Codex event may include
exactly five allowlisted cumulative numeric token counters from a local Codex
token_count record; Claude Code events never synthesize token counters.
Subagent and fork snapshots are suppressed; transcript paths and all other
content are discarded.
"""

from __future__ import annotations

import argparse
from collections import Counter
import contextlib
import datetime as dt
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePath
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import queue
from typing import Any, Iterable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
import uuid


SCHEMA_VERSION = 1
TOKEN_USAGE_SCHEMA_VERSION = 2
CLAIM_SCHEMA_VERSION = 2
HEARTBEAT_MINUTES = 15
MAX_ACTIVE_MINUTES = 15
MAX_SAFE_INTEGER = (1 << 53) - 1
TOKEN_USAGE_TAIL_BYTES = 512 * 1024
TOKEN_USAGE_MAX_RECORD_BYTES = 64 * 1024
PROMPT_MAX_BYTES = 64 * 1024
PROMPT_RETENTION_MS = 60 * 24 * 60 * 60 * 1000
PROMPT_CAPTURE_POLICY = "on"
PROJECT_SCOPE_POLICY = "explicit"
PROJECT_SCOPE_TRANSPORT = "explicit-v1"
PROJECT_SCOPE_MIGRATION_VERSION = 2
CURRENT_PROMPT_DELIVERY_TIMEOUT_SECONDS = 0.75
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
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_PATH = PLUGIN_ROOT / "config" / "defaults.json"
MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
FALLBACK_ROOT = Path.home() / ".builder-pulse"
AGENT_PLATFORMS = {"codex", "claude_code"}
DEFAULT_AGENT_PLATFORM = "codex"

STATES = {"building", "testing", "blocked", "ready", "idle"}

CONFIG_KEYS = {
    "enabled",
    "endpoint",
    "delivery_timeout_seconds",
    "claim_timeout_seconds",
    "max_flush_events",
    "max_outbox_events",
}
BOOL_KEYS = {"enabled"}
INT_KEYS = {"max_flush_events", "max_outbox_events"}
FLOAT_KEYS = {"delivery_timeout_seconds", "claim_timeout_seconds"}

TEST_PATTERNS = (
    r"(?:^|\s)(?:pytest|py\.test|vitest|jest|rspec)(?:\s|$)",
    r"(?:^|\s)(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|lint|typecheck|check|build)(?:\s|$)",
    r"(?:^|\s)(?:cargo|go|mvn|gradle)\s+test(?:\s|$)",
    r"(?:^|\s)(?:tsc|eslint|ruff|mypy|pyright)(?:\s|$)",
    r"(?:^|\s)playwright\s+test(?:\s|$)",
)

REVIEW_PATTERNS = (
    r"(?:^|\s)gh\s+pr\s+(?:create|ready)(?:\s|$)",
    r"(?:^|\s)glab\s+mr\s+create(?:\s|$)",
)

PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 -]*PRIVATE KEY(?: BLOCK)?)-----"
    r".*?(?:-----END (?P=label)-----|$)",
    re.DOTALL,
)
AUTHORIZATION_PATTERN = re.compile(
    r"(?im)(\bauthorization\s*[:=]\s*)[^\r\n]+"
)
BEARER_PATTERN = re.compile(
    r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{12,}"
)
API_TOKEN_PATTERNS = (
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
        r"[A-Za-z0-9_-]{10,}\b"
    ),
)

TOKEN_USAGE_FIELDS = (
    ("input_tokens", "inputTokens"),
    ("cached_input_tokens", "cachedInputTokens"),
    ("output_tokens", "outputTokens"),
    ("reasoning_output_tokens", "reasoningOutputTokens"),
    ("total_tokens", "totalTokens"),
)
TOKEN_USAGE_KEYS = tuple(destination for _, destination in TOKEN_USAGE_FIELDS)
TOKEN_USAGE_SOURCE_KEYS = frozenset(source for source, _ in TOKEN_USAGE_FIELDS) | {
    "cache_write_input_tokens"
}
SUBAGENT_SOURCE_KEYS = frozenset({"subagent", "fork"})
SUBAGENT_PARENT_KEYS = frozenset(
    {
        "parent_agent_id",
        "parent_session_id",
        "parent_thread_id",
        "parentAgentId",
        "parentSessionId",
        "parentThreadId",
        "agent_id",
        "agentId",
    }
)
SUBAGENT_FLAG_KEYS = frozenset(
    {
        "is_fork",
        "is_forked",
        "is_subagent",
        "isFork",
        "isForked",
        "isSubagent",
        "isSidechain",
    }
)
SUBAGENT_PATH_PARTS = frozenset({"fork", "forks", "subagent", "subagents"})


def read_json(path: Path, fallback: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return fallback


def plugin_version() -> str:
    configured = os.environ.get("BUILDER_PULSE_PLUGIN_VERSION")
    if isinstance(configured, str) and re.fullmatch(r"\d+\.\d+\.\d+", configured):
        return configured
    for manifest_path in (
        MANIFEST_PATH,
        PLUGIN_ROOT / ".claude-plugin" / "plugin.json",
    ):
        manifest = read_json(manifest_path, {})
        version = manifest.get("version") if isinstance(manifest, dict) else None
        if isinstance(version, str) and version:
            return version
    return "0.0.0"


PLUGIN_VERSION = plugin_version()


def current_agent_platform() -> str:
    value = os.environ.get("BUILDER_PULSE_AGENT_PLATFORM", DEFAULT_AGENT_PLATFORM)
    return value if value in AGENT_PLATFORMS else DEFAULT_AGENT_PLATFORM


def utc_now_ms() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    try:
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        os.chmod(path, mode)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_jsonl(
    path: Path, records: list[dict[str, Any]], mode: int = 0o600
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True))
            handle.write("\n")
        temp_path = Path(handle.name)
    try:
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        os.chmod(path, mode)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def data_lock(data_dir: Path) -> Iterable[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".lock"
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            # Do not read byte zero to initialize the lock file. Another
            # Windows process may already hold a byte-range lock there, and a
            # read then fails with PermissionError before LK_LOCK can wait.
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


@contextlib.contextmanager
def delivery_lease(data_dir: Path) -> Iterable[bool]:
    """Hold one cross-process delivery lease without blocking hook processes."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".delivery.lock"
    with path.open("a+b") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(handle.fileno()).st_size == 0:
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError):
            acquired = False
        try:
            yield acquired
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def scope_delivery_lock(data_dir: Path) -> Iterable[None]:
    """Serialize scope mutations with the final pre-request privacy check."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".scope-delivery.lock"
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


@contextlib.contextmanager
def claim_lease(data_dir: Path) -> Iterable[None]:
    """Serialize first-claim attempts so the first pending token is never replaced."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".claim.lock"
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


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        os.chmod(path, 0o600)
        handle = os.fdopen(descriptor, "a", encoding="utf-8")
        descriptor = -1
    except Exception:
        os.close(descriptor)
        raise
    with handle:
        handle.write(json.dumps(value, separators=(",", ":"), sort_keys=True))
        handle.write("\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except OSError:
        pass
    return records


def legacy_data_dirs() -> tuple[Path, ...]:
    """Return old agent-owned data roots without treating them as canonical."""
    candidates: list[Path] = []
    for variable in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value).expanduser())
    candidates.append(
        Path.home()
        / ".codex"
        / "plugins"
        / "data"
        / "builder-pulse-growthx-builder-tools"
    )
    try:
        cache_dir = PLUGIN_ROOT.parent.parent.parent
        if cache_dir.name == "cache":
            marketplace = PLUGIN_ROOT.parent.parent.name
            plugin_name = PLUGIN_ROOT.parent.name
            candidates.append(
                cache_dir.parent / "data" / f"{plugin_name}-{marketplace}"
            )
    except (IndexError, OSError):
        pass
    unique: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            continue
        if resolved != FALLBACK_ROOT.resolve(strict=False) and resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def migrate_legacy_data_dir(canonical: Path) -> None:
    """Copy one existing identity to the shared root; never delete the source."""
    if canonical.exists():
        return
    for source in legacy_data_dirs():
        identity = read_json(source / "identity.json", {})
        if not isinstance(identity, dict) or not valid_uuid(identity.get("installationId")):
            continue
        canonical.parent.mkdir(parents=True, exist_ok=True)
        temporary = canonical.parent / f".{canonical.name}-migration-{uuid.uuid4().hex}"
        try:
            shutil.copytree(source, temporary, symlinks=False)
            try:
                os.replace(temporary, canonical)
            except FileExistsError:
                shutil.rmtree(temporary, ignore_errors=True)
        except OSError:
            shutil.rmtree(temporary, ignore_errors=True)
        return


def resolve_data_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    configured = os.environ.get("BUILDER_PULSE_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    migrate_legacy_data_dir(FALLBACK_ROOT)
    return FALLBACK_ROOT


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected true or false")


def sanitize_identifier(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return cleaned[:128] or fallback


def project_identifier_from_label(label: str) -> str:
    """Derive a stable non-secret ID without collapsing distinct labels."""
    normalized = label.strip().lower()
    sanitized = sanitize_identifier(normalized)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    whitespace_normalized = re.sub(r"\s+", "-", normalized).strip("-.")
    if not sanitized:
        return f"project-{digest}"
    if sanitized != whitespace_normalized:
        return f"{sanitized[:115]}-{digest}"
    return sanitized


def validate_identifier(value: Any, field: str) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) > 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", text):
        raise ValueError(
            f"{field} must use only letters, numbers, dot, underscore, or hyphen"
        )
    return text


def validate_feature_label(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) > 120:
        raise ValueError("feature_label must be at most 120 characters")
    if any(ord(character) < 32 for character in text):
        raise ValueError("feature_label must not contain control characters")
    return text


def validate_project_label(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) > 160:
        raise ValueError("project_label must be at most 160 characters")
    if any(ord(character) < 32 for character in text):
        raise ValueError("project_label must not contain control characters")
    return text


def validate_endpoint(value: Any) -> str:
    text = str(value).strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("endpoint must be an http(s) base URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "endpoint must not contain credentials, a query string, or a fragment"
        )
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("endpoint must use https (http is allowed only for loopback development)")
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = f"{rendered_host}:{parsed.port}" if parsed.port else rendered_host
    path = parsed.path.rstrip("/")
    return urlparse.urlunparse((parsed.scheme.lower(), netloc, path, "", "", ""))


def validate_config_value(key: str, value: Any) -> Any:
    if key not in CONFIG_KEYS:
        raise ValueError(f"unsupported config key: {key}")
    if key in BOOL_KEYS:
        return parse_bool(value)
    if key in INT_KEYS:
        parsed = int(value)
        if parsed < 1:
            raise ValueError(f"{key} must be at least 1")
        if key == "max_outbox_events" and parsed > 5000:
            raise ValueError("max_outbox_events must be at most 5000")
        if key == "max_flush_events" and parsed > 50:
            raise ValueError("max_flush_events must be at most 50")
        return parsed
    if key in FLOAT_KEYS:
        parsed = float(value)
        maximum = 30 if key == "claim_timeout_seconds" else 3
        if parsed <= 0 or parsed > maximum:
            raise ValueError(f"{key} must be greater than 0 and at most {maximum}")
        return parsed
    if key == "endpoint":
        return validate_endpoint(value)
    return value


def env_override(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value is not None and value != "" else None


def load_config(data_dir: Path) -> dict[str, Any]:
    config = read_json(DEFAULTS_PATH, {})
    overrides = read_json(data_dir / "config.json", {})
    locally_disabled = isinstance(overrides, dict) and overrides.get("enabled") is False
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if key in CONFIG_KEYS:
                try:
                    config[key] = validate_config_value(key, value)
                except (TypeError, ValueError):
                    continue

    env_map = {
        "enabled": "BUILDER_PULSE_ENABLED",
        "endpoint": "BUILDER_PULSE_ENDPOINT",
        "claim_timeout_seconds": "BUILDER_PULSE_CLAIM_TIMEOUT_SECONDS",
    }
    for key, env_name in env_map.items():
        # An explicit local pause is a privacy boundary. A stale inherited
        # BUILDER_PULSE_ENABLED=1 must never silently turn capture back on.
        if key == "enabled" and locally_disabled:
            continue
        value = env_override(env_name)
        if value is not None:
            try:
                config[key] = validate_config_value(key, value)
            except (TypeError, ValueError):
                continue
    return config


def save_config_overrides(data_dir: Path, overrides: dict[str, Any]) -> None:
    atomic_write_json(data_dir / "config.json", overrides)


def identity_path(data_dir: Path) -> Path:
    return data_dir / "identity.json"


def valid_uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def ensure_identity(data_dir: Path) -> dict[str, Any]:
    with data_lock(data_dir):
        path = identity_path(data_dir)
        identity = read_json(path, {})
        if not isinstance(identity, dict):
            identity = {}
        changed = False
        installation_id = valid_uuid(identity.get("installationId"))
        if not installation_id:
            identity = {
                "installationId": str(uuid.uuid4()),
                "scopeSecret": secrets.token_hex(32),
            }
            changed = True
        elif identity.get("installationId") != installation_id:
            identity["installationId"] = installation_id
            changed = True
        scope_secret = identity.get("scopeSecret")
        if not isinstance(scope_secret, str) or re.fullmatch(
            r"[0-9a-f]{64}", scope_secret
        ) is None:
            identity["scopeSecret"] = secrets.token_hex(32)
            changed = True
        if changed:
            atomic_write_json(path, identity)
        else:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return identity


def claimed_identity(data_dir: Path) -> dict[str, Any]:
    identity = ensure_identity(data_dir)
    token = identity.get("installationToken")
    builder_id = identity.get("builderId")
    if not isinstance(token, str) or not token or not isinstance(builder_id, str):
        return identity
    return identity


def session_key(raw_session_id: str, agent_platform: str | None = None) -> str:
    value = raw_session_id or "unknown-session"
    platform = agent_platform or current_agent_platform()
    return hashlib.sha256(f"{platform}:{value}".encode("utf-8")).hexdigest()[:16]


def raw_session_identifier(payload: dict[str, Any]) -> str:
    platform = current_agent_platform()
    environment_key = (
        "CLAUDE_SESSION_ID" if platform == "claude_code" else "CODEX_SESSION_ID"
    )
    return str(
        payload.get("session_id")
        or os.environ.get(environment_key)
        or "unknown-session"
    )


def repository_key(data_dir: Path, value: str | Path | None) -> str:
    return path_key(data_dir, resolved_directory(value))


def resolved_directory(value: str | Path | None) -> Path:
    candidate = Path(value or Path.cwd()).expanduser().resolve(strict=False)
    if candidate.is_file():
        candidate = candidate.parent
    return candidate


def path_key(data_dir: Path, root: Path) -> str:
    identity = ensure_identity(data_dir)
    scope_secret = str(identity["scopeSecret"])
    return hmac.new(
        bytes.fromhex(scope_secret),
        str(root).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def is_filesystem_root(path: PurePath) -> bool:
    """Return true for POSIX, drive, and UNC filesystem anchors."""
    return bool(path.anchor) and path == type(path)(path.anchor)


def validate_enrollment_root(root: Path) -> Path:
    resolved = root.expanduser().resolve(strict=False)
    home = Path.home().resolve(strict=False)
    if (
        resolved == home
        or resolved in home.parents
        or is_filesystem_root(resolved)
    ):
        raise ValueError(
            "enrollment must target a project folder, not the home, one of its "
            "parents, or the filesystem root"
        )
    return resolved


def load_work_contexts(data_dir: Path) -> dict[str, dict[str, Any]]:
    raw = read_json(data_dir / "contexts.json", {})
    if not isinstance(raw, dict):
        return {}
    contexts: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, dict):
            context: dict[str, Any] = {
                field: str(value[field])
                for field in (
                    "project_id",
                    "project_label",
                    "feature_id",
                    "feature_label",
                    "scope_key",
                )
                if isinstance(value.get(field), str) and value[field]
            }
            ancestor_keys = value.get("ancestor_keys")
            if isinstance(ancestor_keys, list):
                context["ancestor_keys"] = [
                    ancestor_key
                    for ancestor_key in ancestor_keys
                    if isinstance(ancestor_key, str)
                    and re.fullmatch(r"[0-9a-f]{32}", ancestor_key)
                ]
            contexts[key] = context
    return contexts


def explicit_work_context(context: dict[str, Any]) -> bool:
    try:
        project_id = validate_identifier(context.get("project_id"), "project_id")
        project_label = validate_project_label(context.get("project_label"))
    except (TypeError, ValueError):
        return False
    return bool(project_id and project_label and valid_uuid(context.get("scope_key")))


def enrolled_work_context(
    data_dir: Path, cwd: str | Path | None
) -> tuple[str, dict[str, Any], Path] | None:
    if cwd is None or not str(cwd).strip():
        return None
    root = resolved_directory(cwd)
    contexts = load_work_contexts(data_dir)
    # A project enrollment covers only the exact member-confirmed folder and its
    # descendants. Start at the actual working directory instead of collapsing
    # to a Git root: a member may deliberately enroll one package in a monorepo.
    # Raw paths are reduced to one-way hashes before they touch persisted state.
    for candidate in (root, *root.parents):
        try:
            validate_enrollment_root(candidate)
        except ValueError:
            continue
        key = path_key(data_dir, candidate)
        scoped = contexts.get(key, {})
        if explicit_work_context(scoped):
            return key, scoped, candidate
    return None


def enrollment_ancestor_keys(data_dir: Path, root: Path) -> list[str]:
    """Return private keys for candidate ancestors without persisting raw paths."""
    keys: list[str] = []
    for candidate in root.parents:
        try:
            validate_enrollment_root(candidate)
        except ValueError:
            continue
        keys.append(path_key(data_dir, candidate))
    return keys


def overlapping_enrollment(
    contexts: dict[str, dict[str, Any]],
    key: str,
    ancestor_keys: list[str],
) -> bool:
    """Reject nested allowlist roots so removal always has unambiguous semantics."""
    if any(
        explicit_work_context(contexts.get(ancestor_key, {}))
        for ancestor_key in ancestor_keys
    ):
        return True
    return any(
        existing_key != key
        and explicit_work_context(context)
        and key in context.get("ancestor_keys", [])
        for existing_key, context in contexts.items()
    )


def feature_context(scoped: dict[str, Any]) -> tuple[str | None, str | None]:
    label = str(scoped.get("feature_label") or "").strip()
    configured_id = str(scoped.get("feature_id") or "").strip()
    if not label:
        return None, None
    feature_id = configured_id or sanitize_identifier(label.lower(), "feature")
    return feature_id, label


def extract_command(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "cmd"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return ""


def matches_any(value: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)


def tool_category(tool_name: str) -> str:
    normalized = tool_name.lower()
    if normalized in {"bash", "shell", "exec", "exec_command", "write_stdin"}:
        return "shell"
    if normalized in {"apply_patch", "edit", "write", "notebookedit"}:
        return "file_edit"
    if "agent" in normalized or normalized in {"task", "update_plan"}:
        return "coordination"
    return "other"


def tool_failed(response: Any, payload: dict[str, Any]) -> bool:
    if payload.get("is_error") is True or payload.get("isError") is True:
        return True
    if isinstance(response, dict):
        if response.get("is_error") is True or response.get("isError") is True:
            return True
        if response.get("success") is False:
            return True
        for key in ("exit_code", "exitCode", "code"):
            value = response.get(key)
            if isinstance(value, int) and value != 0:
                return True
    return False


def classify_state(payload: dict[str, Any], previous_state: str) -> str:
    event_name = str(payload.get("hook_event_name") or "Unknown")
    if event_name == "ExplicitStateMark":
        requested = str(payload.get("explicit_state") or "")
        return requested if requested in STATES else previous_state
    if event_name == "SessionEnd":
        return "idle"
    if event_name == "PermissionRequest":
        return "blocked"
    if event_name in {"SessionStart", "SubagentStart", "UserPromptSubmit"}:
        return "building"
    if event_name in {"Stop", "SubagentStop"}:
        return previous_state
    if event_name not in {"PreToolUse", "PostToolUse"}:
        return previous_state

    category = tool_category(str(payload.get("tool_name") or ""))
    if category == "file_edit":
        return "building"
    if category == "coordination":
        return "building"
    if category != "shell":
        return previous_state

    # Commands are inspected only in memory and never added to state or events.
    command = extract_command(payload)
    if matches_any(command, TEST_PATTERNS):
        return "testing"
    if matches_any(command, REVIEW_PATTERNS):
        if event_name == "PostToolUse" and not tool_failed(
            payload.get("tool_response"), payload
        ):
            return "ready"
        return "building"
    return "building"


def state_path(data_dir: Path, key: str, context_key: str) -> Path:
    # A Codex session can move between repositories. Keep state and heartbeat
    # windows separate per enrolled folder so one project's activity can never
    # suppress or inherit another project's lifecycle event.
    return data_dir / "states" / f"{key}-{context_key}.json"


def heartbeat_due(previous: dict[str, Any], now_ms: int) -> bool:
    last = previous.get("lastEmittedAt")
    if not isinstance(last, int):
        return True
    return now_ms - last >= HEARTBEAT_MINUTES * 60 * 1000


def capped_active_from(previous: dict[str, Any], now_ms: int) -> int | None:
    if previous.get("state") == "idle":
        return None
    last_observed = previous.get("lastObservedAt")
    window_start = previous.get("activeWindowStartAt")
    if not isinstance(last_observed, int) or not isinstance(window_start, int):
        return None
    if now_ms - last_observed > MAX_ACTIVE_MINUTES * 60 * 1000:
        return None
    floor = now_ms - MAX_ACTIVE_MINUTES * 60 * 1000
    start = max(window_start, floor)
    if start >= now_ms:
        return None
    return start


def allowed_transcript_roots(agent_platform: str | None = None) -> tuple[Path, ...]:
    platform = agent_platform or current_agent_platform()
    if platform == "claude_code":
        configured_home = os.environ.get("CLAUDE_CONFIG_DIR")
        candidate = (
            Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".claude"
        )
        homes = [candidate] if candidate.is_absolute() else []
        directories = ("projects",)
    else:
        configured_home = os.environ.get("CODEX_HOME")
        candidate = (
            Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".codex"
        )
        homes = [candidate] if candidate.is_absolute() else []
        directories = ("sessions", "archived_sessions")

    roots: list[Path] = []
    for home in homes:
        for directory in directories:
            try:
                root = (home / directory).resolve(strict=False)
            except OSError:
                continue
            if root not in roots:
                roots.append(root)
    return tuple(roots)


def validated_transcript_path(
    payload: dict[str, Any], agent_platform: str | None = None
) -> Path | None:
    value = payload.get("transcript_path")
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\x00" in value
    ):
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or candidate.suffix.lower() != ".jsonl":
        return None
    try:
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            return None
    except OSError:
        return None
    if not any(
        resolved == root or root in resolved.parents
        for root in allowed_transcript_roots(agent_platform)
    ):
        return None
    return resolved


def source_marks_subagent_or_fork(source: Any) -> bool:
    """Recognize only explicit Codex child-session metadata, never free text."""
    if isinstance(source, str):
        return source.strip().lower() in SUBAGENT_SOURCE_KEYS
    return isinstance(source, dict) and bool(SUBAGENT_SOURCE_KEYS & set(source))


def payload_marks_subagent_or_fork(payload: dict[str, Any]) -> bool:
    event_name = payload.get("hook_event_name")
    if event_name in {"SubagentStart", "SubagentStop"}:
        return True
    if any(
        key in payload and payload.get(key) not in (None, False, "")
        for key in SUBAGENT_FLAG_KEYS
    ):
        return True
    if any(
        key in payload and payload.get(key) not in (None, False, "")
        for key in SUBAGENT_PARENT_KEYS
    ):
        return True
    return source_marks_subagent_or_fork(payload.get("source"))


def transcript_marks_subagent_or_fork(
    path: Path, agent_platform: str | None = None
) -> bool:
    """Inspect only bounded structural session metadata and retain nothing."""
    if any(part.lower() in SUBAGENT_PATH_PARTS for part in path.parts):
        return True
    if (agent_platform or current_agent_platform()) == "claude_code":
        return not transcript_structurally_marks_primary(path, "claude_code")
    try:
        with path.open("rb") as handle:
            raw_line = handle.readline(TOKEN_USAGE_MAX_RECORD_BYTES + 1)
        if not raw_line or len(raw_line) > TOKEN_USAGE_MAX_RECORD_BYTES:
            return False
        record = json.loads(raw_line.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return False
    metadata = record.get("payload")
    return isinstance(metadata, dict) and source_marks_subagent_or_fork(
        metadata.get("source")
    )


def transcript_structurally_marks_primary(
    path: Path, agent_platform: str | None = None
) -> bool:
    """Require trusted, bounded session metadata with no child-run markers."""
    if any(part.lower() in SUBAGENT_PATH_PARTS for part in path.parts):
        return False
    platform = agent_platform or current_agent_platform()
    if platform == "claude_code":
        # Claude may put bounded queue metadata before the first conversation
        # record. Require that first conversation record to be the primary user
        # thread; fail closed for sidechains, agent transcripts, and ambiguity.
        total_bytes = 0
        try:
            with path.open("rb") as handle:
                for _ in range(64):
                    raw_line = handle.readline(TOKEN_USAGE_MAX_RECORD_BYTES + 1)
                    if not raw_line:
                        break
                    total_bytes += len(raw_line)
                    if (
                        len(raw_line) > TOKEN_USAGE_MAX_RECORD_BYTES
                        or total_bytes > 256 * 1024
                    ):
                        return False
                    try:
                        record = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        return False
                    if not isinstance(record, dict):
                        return False
                    if record.get("type") not in {"user", "assistant"}:
                        continue
                    return bool(
                        record.get("type") == "user"
                        and record.get("isSidechain") is False
                        and not record.get("agentId")
                        and not record.get("agent_id")
                    )
        except OSError:
            return False
        return False
    try:
        with path.open("rb") as handle:
            raw_line = handle.readline(TOKEN_USAGE_MAX_RECORD_BYTES + 1)
        if not raw_line or len(raw_line) > TOKEN_USAGE_MAX_RECORD_BYTES:
            return False
        record = json.loads(raw_line.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return False
    metadata = record.get("payload")
    return isinstance(metadata, dict) and not payload_marks_subagent_or_fork(metadata)


def is_primary_user_prompt(payload: dict[str, Any]) -> bool:
    if payload.get("hook_event_name") != "UserPromptSubmit":
        return False
    if payload_marks_subagent_or_fork(payload):
        return False
    platform = current_agent_platform()
    path = validated_transcript_path(payload, platform)
    return path is not None and transcript_structurally_marks_primary(path, platform)


def truncate_utf8(text: str, max_bytes: int = PROMPT_MAX_BYTES) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def redact_prompt_text(text: str) -> tuple[str, bool]:
    redacted = False

    def replace_private_key(match: re.Match[str]) -> str:
        nonlocal redacted
        redacted = True
        return "[REDACTED PRIVATE KEY]"

    def replace_authorization(match: re.Match[str]) -> str:
        nonlocal redacted
        redacted = True
        return f"{match.group(1)}[REDACTED]"

    text = PRIVATE_KEY_PATTERN.sub(replace_private_key, text)
    text = AUTHORIZATION_PATTERN.sub(replace_authorization, text)
    text, bearer_count = BEARER_PATTERN.subn("Bearer [REDACTED]", text)
    redacted = redacted or bearer_count > 0
    for pattern in API_TOKEN_PATTERNS:
        text, count = pattern.subn("[REDACTED API TOKEN]", text)
        redacted = redacted or count > 0
    text, invite_argument_count = re.subn(
        r"(?i)(--code\s+)(['\"]?)[A-Za-z0-9_-]{16,256}\2",
        r"\1\2[REDACTED INVITE CODE]\2",
        text,
    )
    text, invite_label_count = re.subn(
        r"(?i)((?:builder\s+pulse\s+)?invite\s+code\s*[:=]\s*)[A-Za-z0-9_-]{16,256}",
        r"\1[REDACTED INVITE CODE]",
        text,
    )
    text, invite_environment_count = re.subn(
        r"(?i)(BUILDER_PULSE_INVITE_CODE\s*=\s*)(['\"]?)[A-Za-z0-9_-]{16,256}\2",
        r"\1\2[REDACTED INVITE CODE]\2",
        text,
    )
    redacted = (
        redacted
        or invite_argument_count > 0
        or invite_label_count > 0
        or invite_environment_count > 0
    )
    return text, redacted


def bounded_redacted_prompt(value: Any) -> tuple[str, bool, bool] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        original_exceeds_limit = len(value.encode("utf-8")) > PROMPT_MAX_BYTES
    except UnicodeEncodeError:
        return None
    redacted_text, redacted = redact_prompt_text(value)
    bounded, wire_truncated = truncate_utf8(redacted_text)
    return bounded, redacted, original_exceeds_limit or wire_truncated


def token_usage_from_record(record: Any) -> dict[str, int] | None:
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    totals = info.get("total_token_usage")
    if (
        not isinstance(totals, dict)
        or not set(totals).issubset(TOKEN_USAGE_SOURCE_KEYS)
        or not all(source in totals for source, _ in TOKEN_USAGE_FIELDS)
    ):
        return None

    snapshot: dict[str, int] = {}
    for source, destination in TOKEN_USAGE_FIELDS:
        value = totals[source]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MAX_SAFE_INTEGER
        ):
            return None
        snapshot[destination] = value
    return snapshot


def validated_token_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict) or set(value) != set(TOKEN_USAGE_KEYS):
        return None
    snapshot: dict[str, int] = {}
    for key in TOKEN_USAGE_KEYS:
        counter = value[key]
        if (
            isinstance(counter, bool)
            or not isinstance(counter, int)
            or counter < 0
            or counter > MAX_SAFE_INTEGER
        ):
            return None
        snapshot[key] = counter
    return snapshot


def token_usage_snapshot(payload: dict[str, Any]) -> dict[str, int] | None:
    """Read one cumulative numeric snapshot without retaining path or transcript data."""
    try:
        if current_agent_platform() != "codex":
            return None
        if payload_marks_subagent_or_fork(payload):
            return None
        path = validated_transcript_path(payload, "codex")
        if path is None or transcript_marks_subagent_or_fork(path, "codex"):
            return None
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - TOKEN_USAGE_TAIL_BYTES)
            handle.seek(start)
            raw_tail = handle.read(TOKEN_USAGE_TAIL_BYTES)
        lines = raw_tail.splitlines()
        if start and lines:
            lines = lines[1:]
        for raw_line in reversed(lines):
            if (
                b'"token_count"' not in raw_line
                or len(raw_line) > TOKEN_USAGE_MAX_RECORD_BYTES
            ):
                continue
            try:
                record = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            record_payload = record.get("payload") if isinstance(record, dict) else None
            if (
                isinstance(record_payload, dict)
                and record.get("type") == "event_msg"
                and record_payload.get("type") == "token_count"
            ):
                return token_usage_from_record(record)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return None


def telemetry_payload(
    *,
    installation_id: str,
    key: str,
    project_id: str,
    project_label: str,
    feature_id: str | None,
    feature_label: str | None,
    state: str,
    occurred_at: int,
    active_from: int | None,
    token_usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    safe_token_usage = validated_token_usage(token_usage)
    event: dict[str, Any] = {
        "schemaVersion": (
            TOKEN_USAGE_SCHEMA_VERSION
            if safe_token_usage is not None
            else SCHEMA_VERSION
        ),
        "eventId": str(uuid.uuid4()),
        "installationId": installation_id,
        "sessionKey": key,
        "projectId": project_id,
        "projectLabel": project_label,
        "projectScope": PROJECT_SCOPE_POLICY,
        "state": state,
        "occurredAt": occurred_at,
        "pluginVersion": PLUGIN_VERSION,
        "agentPlatform": current_agent_platform(),
    }
    if feature_id and feature_label:
        event["featureId"] = feature_id
        event["featureLabel"] = feature_label
    if active_from:
        event["activeFrom"] = active_from
    if safe_token_usage is not None:
        event["tokenUsage"] = safe_token_usage
    return event


def prompt_payload(
    *,
    installation_id: str,
    key: str,
    project_id: str,
    project_label: str,
    feature_id: str | None,
    feature_label: str | None,
    prompt_text: str,
    occurred_at: int,
    redacted: bool,
    truncated: bool,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "promptId": str(uuid.uuid4()),
        "installationId": installation_id,
        "sessionKey": key,
        "projectId": project_id,
        "projectLabel": project_label,
        "projectScope": PROJECT_SCOPE_POLICY,
        "promptText": prompt_text,
        "occurredAt": occurred_at,
        "pluginVersion": PLUGIN_VERSION,
        "agentPlatform": current_agent_platform(),
        "redacted": redacted,
        "truncated": truncated,
    }
    if feature_id and feature_label:
        event["featureId"] = feature_id
        event["featureLabel"] = feature_label
    return event


def enqueue_event_unlocked(
    data_dir: Path, event: dict[str, Any], max_events: int
) -> None:
    path = data_dir / "outbox.jsonl"
    records = read_jsonl(path)
    event_id = event.get("eventId")
    if any(record.get("eventId") == event_id for record in records):
        return
    append_jsonl(path, event)
    records.append(event)
    if len(records) > max_events:
        atomic_write_jsonl(path, records[-max_events:])


def enqueue_prompt_unlocked(
    data_dir: Path,
    event: dict[str, Any],
    max_events: int,
    now_ms: int | None = None,
) -> None:
    path = data_dir / "prompt-outbox.jsonl"
    records, _ = prune_prompt_outbox_unlocked(
        path,
        utc_now_ms() if now_ms is None else now_ms,
    )
    prompt_id = event.get("promptId")
    if any(record.get("promptId") == prompt_id for record in records):
        return
    append_jsonl(path, event)
    records.append(event)
    if len(records) > max_events:
        atomic_write_jsonl(path, records[-max_events:])


def retained_prompt_records(
    records: list[dict[str, Any]], now_ms: int
) -> tuple[list[dict[str, Any]], int]:
    cutoff = now_ms - PROMPT_RETENTION_MS
    retained: list[dict[str, Any]] = []
    expired = 0
    for record in records:
        occurred_at = record.get("occurredAt")
        if (
            not isinstance(occurred_at, int)
            or isinstance(occurred_at, bool)
            or occurred_at < cutoff
        ):
            expired += 1
        else:
            retained.append(record)
    return retained, expired


def prune_prompt_outbox_unlocked(
    path: Path, now_ms: int
) -> tuple[list[dict[str, Any]], int]:
    records = read_jsonl(path)
    retained, expired = retained_prompt_records(records, now_ms)
    if expired:
        if retained:
            atomic_write_jsonl(path, retained)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return retained, expired


def explicit_project_record(record: dict[str, Any]) -> bool:
    try:
        project_id = validate_identifier(record.get("projectId"), "projectId")
        project_label = validate_project_label(record.get("projectLabel"))
    except (TypeError, ValueError):
        return False
    return bool(
        project_id
        and project_label
        and record.get("projectScope") == PROJECT_SCOPE_POLICY
    )


def local_scope_record(record: dict[str, Any]) -> bool:
    context_key = record.get("_contextKey")
    scope_key = valid_uuid(record.get("_scopeKey"))
    return bool(
        explicit_project_record(record)
        and isinstance(context_key, str)
        and re.fullmatch(r"[0-9a-f]{32}", context_key)
        and scope_key
    )


def record_scope_active_unlocked(data_dir: Path, record: dict[str, Any]) -> bool:
    if not local_scope_record(record):
        return False
    context = load_work_contexts(data_dir).get(str(record["_contextKey"]), {})
    feature_id, feature_label = feature_context(context)
    return bool(
        explicit_work_context(context)
        and context.get("scope_key") == record.get("_scopeKey")
        and context.get("project_id") == record.get("projectId")
        and context.get("project_label") == record.get("projectLabel")
        and feature_id == record.get("featureId")
        and feature_label == record.get("featureLabel")
    )


def wire_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Remove local-only allowlist keys before any network serialization."""
    return {key: value for key, value in record.items() if not key.startswith("_")}


def prune_unscoped_records_unlocked(
    data_dir: Path,
    path: Path,
) -> tuple[list[dict[str, Any]], int]:
    records = read_jsonl(path)
    retained = [
        record for record in records if record_scope_active_unlocked(data_dir, record)
    ]
    discarded = len(records) - len(retained)
    if discarded:
        if retained:
            atomic_write_jsonl(path, retained)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return retained, discarded


def discard_project_scope_data_unlocked(
    data_dir: Path,
    scope_key: str,
) -> dict[str, int]:
    """Discard pending data for one removed enrollment, preserving others."""

    def belongs_to_removed_project(record: dict[str, Any]) -> bool:
        return record.get("_scopeKey") == scope_key

    discarded: dict[str, int] = {"lifecycle": 0, "prompts": 0, "states": 0}
    for name, filename in (
        ("lifecycle", "outbox.jsonl"),
        ("prompts", "prompt-outbox.jsonl"),
    ):
        path = data_dir / filename
        records = read_jsonl(path)
        retained = [record for record in records if not belongs_to_removed_project(record)]
        discarded[name] = len(records) - len(retained)
        if discarded[name]:
            if retained:
                atomic_write_jsonl(path, retained)
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    states_dir = data_dir / "states"
    if states_dir.exists():
        for path in states_dir.glob("*.json"):
            state = read_json(path, {})
            if not isinstance(state, dict):
                continue
            if state.get("scopeKey") != scope_key:
                continue
            try:
                path.unlink()
                discarded["states"] += 1
            except FileNotFoundError:
                pass
    return discarded


def discard_all_pending_data_unlocked(data_dir: Path) -> dict[str, int]:
    """Purge unsent telemetry when capture is explicitly disabled."""
    discarded = {
        "lifecycle": len(read_jsonl(data_dir / "outbox.jsonl")),
        "prompts": len(read_jsonl(data_dir / "prompt-outbox.jsonl")),
        "states": 0,
    }
    for filename in ("outbox.jsonl", "prompt-outbox.jsonl"):
        try:
            (data_dir / filename).unlink()
        except FileNotFoundError:
            pass
    states_dir = data_dir / "states"
    if states_dir.exists():
        for path in states_dir.glob("*.json"):
            try:
                path.unlink()
                discarded["states"] += 1
            except FileNotFoundError:
                pass
    return discarded


def prune_unscoped_scope_data_unlocked(data_dir: Path) -> dict[str, int]:
    contexts_path = data_dir / "contexts.json"
    raw_contexts = read_json(contexts_path, {})
    retained_contexts = {
        key: value
        for key, value in raw_contexts.items()
        if isinstance(key, str)
        and isinstance(value, dict)
        and explicit_work_context(value)
    } if isinstance(raw_contexts, dict) else {}
    discarded_contexts = (
        len(raw_contexts) - len(retained_contexts)
        if isinstance(raw_contexts, dict)
        else 0
    )
    if discarded_contexts:
        atomic_write_json(contexts_path, retained_contexts)

    _, lifecycle = prune_unscoped_records_unlocked(
        data_dir, data_dir / "outbox.jsonl"
    )
    _, prompts = prune_unscoped_records_unlocked(
        data_dir, data_dir / "prompt-outbox.jsonl"
    )

    states = 0
    states_dir = data_dir / "states"
    if states_dir.exists():
        for path in states_dir.glob("*.json"):
            state = read_json(path, {})
            if (
                isinstance(state, dict)
                and isinstance(state.get("contextKey"), str)
                and valid_uuid(state.get("scopeKey"))
                and explicit_project_record(state)
                and load_work_contexts(data_dir)
                .get(str(state["contextKey"]), {})
                .get("scope_key")
                == state.get("scopeKey")
            ):
                continue
            try:
                path.unlink()
                states += 1
            except FileNotFoundError:
                pass
    return {
        "contexts": discarded_contexts,
        "lifecycle": lifecycle,
        "prompts": prompts,
        "states": states,
    }


def ensure_project_scope_migration(data_dir: Path) -> dict[str, Any]:
    marker_path = data_dir / "project-scope-migration.json"
    marker = read_json(marker_path, {})
    if (
        isinstance(marker, dict)
        and marker.get("version") == PROJECT_SCOPE_MIGRATION_VERSION
    ):
        return marker
    with data_lock(data_dir):
        marker = read_json(marker_path, {})
        if (
            isinstance(marker, dict)
            and marker.get("version") == PROJECT_SCOPE_MIGRATION_VERSION
        ):
            return marker
        discarded = prune_unscoped_scope_data_unlocked(data_dir)
        marker = {
            "version": PROJECT_SCOPE_MIGRATION_VERSION,
            "migratedAt": utc_now_ms(),
            "discardedUnscoped": discarded,
        }
        atomic_write_json(marker_path, marker)
        return marker


def record_prompt_event(
    data_dir: Path, payload: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any] | None:
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    enrolled = enrolled_work_context(data_dir, cwd)
    if enrolled is None:
        return None
    context_key, scoped, _ = enrolled

    if not is_primary_user_prompt(payload):
        return None
    prepared = bounded_redacted_prompt(payload.get("prompt"))
    if prepared is None:
        return None

    identity = claimed_identity(data_dir)
    if (
        identity.get("promptCapture") != PROMPT_CAPTURE_POLICY
        or not isinstance(identity.get("installationToken"), str)
        or not isinstance(identity.get("claimedEndpoint"), str)
    ):
        return None

    key = session_key(raw_session_identifier(payload))
    project_id = str(scoped["project_id"])
    project_label = str(scoped["project_label"])
    scope_key = str(scoped["scope_key"])
    feature_id, feature_label = feature_context(scoped)
    prompt_text, redacted, truncated = prepared
    occurred_at = utc_now_ms()
    event = prompt_payload(
        installation_id=str(identity["installationId"]),
        key=key,
        project_id=project_id,
        project_label=project_label,
        feature_id=feature_id,
        feature_label=feature_label,
        prompt_text=prompt_text,
        occurred_at=occurred_at,
        redacted=redacted,
        truncated=truncated,
    )
    event["_contextKey"] = context_key
    event["_scopeKey"] = scope_key
    with scope_delivery_lock(data_dir):
        with data_lock(data_dir):
            current_identity = read_json(identity_path(data_dir), {})
            current_context = load_work_contexts(data_dir).get(context_key, {})
            if (
                load_config(data_dir).get("enabled") is not True
                or not isinstance(current_identity, dict)
                or current_identity.get("promptCapture") != PROMPT_CAPTURE_POLICY
                or current_identity.get("installationToken")
                != identity.get("installationToken")
                or current_identity.get("claimedEndpoint")
                != identity.get("claimedEndpoint")
                or current_context.get("project_id") != project_id
                or current_context.get("project_label") != project_label
                or current_context.get("scope_key") != scope_key
                or current_context.get("feature_id") != feature_id
                or current_context.get("feature_label") != feature_label
            ):
                return None
            enqueue_prompt_unlocked(
                data_dir,
                event,
                int(config.get("max_outbox_events", 500)),
                occurred_at,
            )
    return event


def record_hook_event(
    data_dir: Path, payload: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any] | None:
    agent_platform = current_agent_platform()
    if payload_marks_subagent_or_fork(payload):
        return None

    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    enrolled = enrolled_work_context(data_dir, cwd)
    if enrolled is None:
        return None
    context_key, scoped, _ = enrolled

    transcript_path = validated_transcript_path(payload, agent_platform)
    if transcript_path is not None and transcript_marks_subagent_or_fork(
        transcript_path, agent_platform
    ):
        return None

    explicit_key = payload.get("_session_key")
    if isinstance(explicit_key, str) and explicit_key:
        key = validate_identifier(explicit_key, "session_key")
    else:
        key = session_key(raw_session_identifier(payload))
    identity = ensure_identity(data_dir)
    installation_id = str(identity["installationId"])
    now_ms = utc_now_ms()
    project_id = str(scoped["project_id"])
    project_label = str(scoped["project_label"])
    scope_key = str(scoped["scope_key"])
    feature_id, feature_label = feature_context(scoped)

    with scope_delivery_lock(data_dir):
        with data_lock(data_dir):
            current_context = load_work_contexts(data_dir).get(context_key, {})
            if (
                load_config(data_dir).get("enabled") is not True
                or current_context.get("project_id") != project_id
                or current_context.get("project_label") != project_label
                or current_context.get("scope_key") != scope_key
                or current_context.get("feature_id") != feature_id
                or current_context.get("feature_label") != feature_label
            ):
                return None
            path = state_path(data_dir, key, context_key)
            previous = read_json(path, {})
            if not isinstance(previous, dict):
                previous = {}
            previous_state = str(previous.get("state") or "idle")
            next_state = classify_state(payload, previous_state)
            changed = next_state != previous_state
            should_emit = changed or heartbeat_due(previous, now_ms)

            active_from = capped_active_from(previous, now_ms) if should_emit else None
            token_usage = token_usage_snapshot(payload) if should_emit else None
            event = (
                telemetry_payload(
                    installation_id=installation_id,
                    key=key,
                    project_id=project_id,
                    project_label=project_label,
                    feature_id=feature_id,
                    feature_label=feature_label,
                    state=next_state,
                    occurred_at=now_ms,
                    active_from=active_from,
                    token_usage=token_usage,
                )
                if should_emit
                else None
            )
            previous_last_observed = previous.get("lastObservedAt")
            continuous = (
                previous_state != "idle"
                and isinstance(previous_last_observed, int)
                and now_ms - previous_last_observed
                <= MAX_ACTIVE_MINUTES * 60 * 1000
            )
            previous_window = previous.get("activeWindowStartAt")
            next_window = (
                previous_window
                if continuous and isinstance(previous_window, int) and not should_emit
                else now_ms
            )
            state = {
                "schemaVersion": SCHEMA_VERSION,
                "sessionKey": key,
                "contextKey": context_key,
                "scopeKey": scope_key,
                "projectId": project_id,
                "projectLabel": project_label,
                "projectScope": PROJECT_SCOPE_POLICY,
                "agentPlatform": current_agent_platform(),
                "state": next_state,
                "stateChangedAt": (
                    now_ms if changed else previous.get("stateChangedAt", now_ms)
                ),
                "lastObservedAt": now_ms,
                "activeWindowStartAt": next_window,
                "lastEmittedAt": (
                    now_ms if should_emit else previous.get("lastEmittedAt")
                ),
                "lastEventId": (
                    event["eventId"] if event else previous.get("lastEventId")
                ),
            }
            if feature_id and feature_label:
                state["featureId"] = feature_id
                state["featureLabel"] = feature_label
            atomic_write_json(path, state)

            if event is None:
                return None
            event["_contextKey"] = context_key
            event["_scopeKey"] = scope_key
            token = identity.get("installationToken")
            claimed_endpoint = identity.get("claimedEndpoint")
            if isinstance(token, str) and token and isinstance(claimed_endpoint, str):
                enqueue_event_unlocked(
                    data_dir,
                    event,
                    int(load_config(data_dir).get("max_outbox_events", 500)),
                )
            return event


def sanitized_endpoint(endpoint: str) -> str:
    if not endpoint:
        return ""
    parsed = urlparse.urlparse(endpoint)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlparse.urlunparse((parsed.scheme, host, parsed.path, "", "", ""))


def endpoint_url(base: str, suffix: str) -> str:
    return f"{base.rstrip('/')}{suffix}"


def http_post_json(
    url: str,
    payload: dict[str, Any],
    *,
    token: str | None,
    timeout: float,
    expect_json: bool,
    explicit_project_scope_transport: bool = False,
) -> tuple[bool, str, dict[str, Any] | None]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"builder-pulse/{PLUGIN_VERSION}",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if explicit_project_scope_transport:
        headers["X-Builder-Pulse-Project-Scope"] = PROJECT_SCOPE_TRANSPORT
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urlrequest.Request(url, data=body, headers=headers, method="POST")
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            if not 200 <= status < 300:
                return False, f"http_{status}", None
            if not expect_json:
                response.read(0)
                return True, "delivered", None
            raw = response.read(65_537)
            if len(raw) > 65_536:
                return False, "invalid_response", None
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False, "invalid_response", None
            return (
                (True, "delivered", parsed)
                if isinstance(parsed, dict)
                else (False, "invalid_response", None)
            )
    except urlerror.HTTPError as exc:
        return False, f"http_{exc.code}", None
    except (OSError, ValueError, urlerror.URLError):
        return False, "network_error", None


def deliver_event(
    event: dict[str, Any], config: dict[str, Any], token: str, endpoint: str | None = None
) -> tuple[bool, str]:
    bound_endpoint = endpoint or str(config.get("endpoint") or "")
    if not bound_endpoint or not token:
        return False, "not_claimed"
    ok, result, _ = http_post_json(
        endpoint_url(bound_endpoint, "/v1/telemetry"),
        wire_payload(event),
        token=token,
        timeout=float(config.get("delivery_timeout_seconds", 1.0)),
        expect_json=False,
        explicit_project_scope_transport=True,
    )
    return ok, result


def deliver_prompt(
    event: dict[str, Any], config: dict[str, Any], token: str, endpoint: str | None = None
) -> tuple[bool, str]:
    bound_endpoint = endpoint or str(config.get("endpoint") or "")
    if not bound_endpoint or not token:
        return False, "not_claimed"
    ok, result, _ = http_post_json(
        endpoint_url(bound_endpoint, "/v1/prompts"),
        wire_payload(event),
        token=token,
        timeout=float(config.get("delivery_timeout_seconds", 1.0)),
        expect_json=False,
        explicit_project_scope_transport=True,
    )
    return ok, result


def deliver_scoped_record(
    data_dir: Path,
    event: dict[str, Any],
    config: dict[str, Any],
    token: str,
    endpoint: str,
    *,
    prompt: bool,
) -> tuple[bool, str]:
    """Revalidate the exact local enrollment immediately before sending."""
    with scope_delivery_lock(data_dir):
        with data_lock(data_dir):
            current_config = load_config(data_dir)
            if current_config.get("enabled") is not True:
                return False, "disabled"
            if not record_scope_active_unlocked(data_dir, event):
                return False, "scope_inactive"
        if prompt:
            current_config["delivery_timeout_seconds"] = min(
                float(current_config.get("delivery_timeout_seconds", 1.0)),
                float(config.get("delivery_timeout_seconds", 1.0)),
            )
            return deliver_prompt(event, current_config, token, endpoint)
        return deliver_event(event, current_config, token, endpoint)


def permanent_delivery_failure(result: str) -> bool:
    match = re.fullmatch(r"http_(\d{3})", result)
    if not match:
        return False
    status = int(match.group(1))
    return 400 <= status < 500 and status not in {401, 403, 408, 425, 429}


def flush_outbox(data_dir: Path, config: dict[str, Any]) -> dict[str, int]:
    ensure_project_scope_migration(data_dir)
    path = data_dir / "outbox.jsonl"
    config = load_config(data_dir)
    if config.get("enabled") is not True:
        return {
            "delivered": 0,
            "discarded": 0,
            "quarantined": 0,
            "remaining": len(read_jsonl(path)),
        }
    with data_lock(data_dir):
        records, unscoped_count = prune_unscoped_records_unlocked(data_dir, path)
    identity = claimed_identity(data_dir)
    token = identity.get("installationToken")
    endpoint = identity.get("claimedEndpoint")
    if not isinstance(token, str) or not token or not endpoint:
        return {
            "delivered": 0,
            "discarded": unscoped_count,
            "quarantined": 0,
            "remaining": len(records),
        }

    with delivery_lease(data_dir) as acquired:
        if not acquired:
            return {
                "delivered": 0,
                "discarded": unscoped_count,
                "quarantined": 0,
                "remaining": len(read_jsonl(path)),
                "busy": 1,
            }
        with data_lock(data_dir):
            records = read_jsonl(path)
            limit = int(config.get("max_flush_events", 3))
            attempted = records[:limit]

        delivered_ids: set[str] = set()
        quarantined: list[dict[str, Any]] = []
        for event in attempted:
            ok, result = deliver_scoped_record(
                data_dir,
                event,
                config,
                token,
                str(endpoint),
                prompt=False,
            )
            if ok and isinstance(event.get("eventId"), str):
                delivered_ids.add(event["eventId"])
            elif permanent_delivery_failure(result):
                quarantined.append(event)

        removed_ids = delivered_ids | {
            str(event["eventId"])
            for event in quarantined
            if isinstance(event.get("eventId"), str)
        }
        if removed_ids:
            with data_lock(data_dir):
                for event in quarantined:
                    append_jsonl(data_dir / "quarantine.jsonl", event)
                current = read_jsonl(path)
                remaining = [
                    event
                    for event in current
                    if event.get("eventId") not in removed_ids
                ]
                if remaining:
                    atomic_write_jsonl(path, remaining)
                else:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
        else:
            remaining = read_jsonl(path)
        return {
            "delivered": len(delivered_ids),
            "discarded": unscoped_count,
            "quarantined": len(quarantined),
            "remaining": len(remaining),
        }


def flush_prompt_outbox(data_dir: Path, config: dict[str, Any]) -> dict[str, int]:
    ensure_project_scope_migration(data_dir)
    now_ms = utc_now_ms()
    path = data_dir / "prompt-outbox.jsonl"
    config = load_config(data_dir)
    if config.get("enabled") is not True:
        return {
            "delivered": 0,
            "discarded": 0,
            "remaining": len(read_jsonl(path)),
        }
    with data_lock(data_dir):
        _, unscoped_count = prune_unscoped_records_unlocked(data_dir, path)
        records, expired_count = prune_prompt_outbox_unlocked(path, now_ms)

    identity = claimed_identity(data_dir)
    token = identity.get("installationToken")
    endpoint = identity.get("claimedEndpoint")
    if (
        identity.get("promptCapture") != PROMPT_CAPTURE_POLICY
        or not isinstance(token, str)
        or not token
        or not endpoint
    ):
        return {
            "delivered": 0,
            "discarded": unscoped_count + expired_count,
            "remaining": len(records),
        }

    with delivery_lease(data_dir) as acquired:
        if not acquired:
            return {
                "delivered": 0,
                "discarded": unscoped_count + expired_count,
                "remaining": len(read_jsonl(path)),
                "busy": 1,
            }
        with data_lock(data_dir):
            records, newly_expired = prune_prompt_outbox_unlocked(path, utc_now_ms())
            expired_count += newly_expired
            limit = int(config.get("max_flush_events", 3))
            attempted = records[:limit]

        delivered_ids: set[str] = set()
        discarded_ids: set[str] = set()
        capture_rejected = False
        for event in attempted:
            ok, result = deliver_scoped_record(
                data_dir,
                event,
                config,
                token,
                str(endpoint),
                prompt=True,
            )
            prompt_id = event.get("promptId")
            if ok and isinstance(prompt_id, str):
                delivered_ids.add(prompt_id)
            elif result in {"http_401", "http_403"}:
                capture_rejected = True
                break
            elif permanent_delivery_failure(result) and isinstance(prompt_id, str):
                discarded_ids.add(prompt_id)

        if capture_rejected:
            with data_lock(data_dir):
                current = read_jsonl(path)
                current_identity = read_json(identity_path(data_dir), {})
                if isinstance(current_identity, dict):
                    current_identity["promptCapture"] = "off"
                    atomic_write_json(identity_path(data_dir), current_identity)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            return {
                "delivered": len(delivered_ids),
                "discarded": unscoped_count
                + expired_count
                + max(0, len(current) - len(delivered_ids)),
                "remaining": 0,
            }

        removed_ids = delivered_ids | discarded_ids
        if removed_ids:
            with data_lock(data_dir):
                current = read_jsonl(path)
                remaining = [
                    event
                    for event in current
                    if event.get("promptId") not in removed_ids
                ]
                if remaining:
                    atomic_write_jsonl(path, remaining)
                else:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
        else:
            remaining = read_jsonl(path)
        return {
            "delivered": len(delivered_ids),
            "discarded": unscoped_count + expired_count + len(discarded_ids),
            "remaining": len(remaining),
        }


def attempt_current_prompt(
    data_dir: Path, config: dict[str, Any], event: dict[str, Any]
) -> dict[str, int]:
    """Attempt only the just-queued prompt under a short delivery deadline."""
    path = data_dir / "prompt-outbox.jsonl"
    identity = claimed_identity(data_dir)
    token = identity.get("installationToken")
    endpoint = identity.get("claimedEndpoint")
    prompt_id = event.get("promptId")
    if (
        identity.get("promptCapture") != PROMPT_CAPTURE_POLICY
        or not isinstance(token, str)
        or not token
        or not isinstance(endpoint, str)
        or not endpoint
        or not isinstance(prompt_id, str)
    ):
        return {"delivered": 0, "discarded": 0, "remaining": len(read_jsonl(path))}

    # Do not use the shared backlog lease here. A concurrent async flush may
    # have snapshotted its queue before this prompt was appended. Concurrent
    # delivery is safe because the server deduplicates the unique promptId.
    prompt_config = dict(config)
    prompt_config["delivery_timeout_seconds"] = min(
        float(config.get("delivery_timeout_seconds", 1.0)),
        CURRENT_PROMPT_DELIVERY_TIMEOUT_SECONDS,
    )
    ok, result = deliver_scoped_record(
        data_dir,
        event,
        prompt_config,
        token,
        endpoint,
        prompt=True,
    )

    if result in {"http_401", "http_403"}:
        with data_lock(data_dir):
            current = read_jsonl(path)
            current_identity = read_json(identity_path(data_dir), {})
            if isinstance(current_identity, dict):
                current_identity["promptCapture"] = "off"
                atomic_write_json(identity_path(data_dir), current_identity)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return {"delivered": 0, "discarded": len(current), "remaining": 0}

    discard = permanent_delivery_failure(result)
    if ok or discard:
        with data_lock(data_dir):
            current = read_jsonl(path)
            remaining = [
                queued for queued in current if queued.get("promptId") != prompt_id
            ]
            if remaining:
                atomic_write_jsonl(path, remaining)
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
    else:
        remaining = read_jsonl(path)
    return {
        "delivered": 1 if ok else 0,
        "discarded": 1 if discard else 0,
        "remaining": len(remaining),
    }


def ingest_hook(data_dir: Path) -> int:
    # Never echo raw input or error details: hook failures must be invisible to Codex.
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        print("{}")
        return 0
    if not isinstance(payload, dict):
        print("{}")
        return 0

    try:
        ensure_project_scope_migration(data_dir)
        config = load_config(data_dir)
        if config.get("enabled", True):
            prompt_event = record_prompt_event(data_dir, payload, config)
            event = record_hook_event(data_dir, payload, config)
            if prompt_event is not None:
                # Prioritize this prompt under a strict deadline. Codex runs the
                # hook in the background so telemetry can never block submission.
                attempt_current_prompt(data_dir, config, prompt_event)
            # SessionEnd is synchronous in Codex even for async hooks. Persist
            # idle locally and let the next hook/manual flush deliver it.
            if (
                payload.get("hook_event_name") not in {"SessionEnd", "UserPromptSubmit"}
                and event is not None
            ):
                flush_outbox(data_dir, config)
                flush_prompt_outbox(data_dir, config)
    except Exception:
        # Delivery is strictly best effort and may never break a builder session.
        pass
    print("{}")
    return 0


def status_records(data_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    states_dir = data_dir / "states"
    now_ms = utc_now_ms()
    idle_after_ms = HEARTBEAT_MINUTES * 2 * 60 * 1000
    for path in states_dir.glob("*.json") if states_dir.exists() else []:
        state = read_json(path, {})
        if not isinstance(state, dict) or not state.get("lastEmittedAt"):
            continue
        derived = dict(state)
        last_emitted = state.get("lastEmittedAt")
        if not isinstance(last_emitted, int):
            derived["stale"] = True
        elif now_ms - last_emitted >= idle_after_ms:
            derived["state"] = "idle"
            derived["stale"] = True
        records.append(derived)
    records.sort(key=lambda item: str(item.get("lastEmittedAt", "")), reverse=True)
    return records


def safe_identity_summary(data_dir: Path) -> dict[str, Any]:
    identity = ensure_identity(data_dir)
    return {
        "installationId": identity.get("installationId"),
        "claimed": bool(
            identity.get("builderId") and identity.get("installationToken")
        ),
        "builderId": identity.get("builderId"),
        "memberId": identity.get("memberId"),
        "builderName": identity.get("builderName"),
        "tokenConfigured": bool(identity.get("installationToken")),
        "claimedEndpoint": sanitized_endpoint(str(identity.get("claimedEndpoint") or "")),
        "promptCapture": (
            PROMPT_CAPTURE_POLICY
            if identity.get("promptCapture") == PROMPT_CAPTURE_POLICY
            else "off"
        ),
    }


def command_status(args: argparse.Namespace, data_dir: Path) -> int:
    ensure_project_scope_migration(data_dir)
    records = status_records(data_dir)
    if args.project:
        records = [item for item in records if item.get("projectId") == args.project]
    identity = safe_identity_summary(data_dir)
    current_enrollment = enrolled_work_context(data_dir, Path.cwd())
    config = load_config(data_dir)
    enabled = config.get("enabled") is True
    current_project_enrolled = current_enrollment is not None
    effective_prompt_capture = bool(
        enabled
        and current_project_enrolled
        and identity["promptCapture"] == PROMPT_CAPTURE_POLICY
    )
    enrolled_projects = [
        context
        for context in load_work_contexts(data_dir).values()
        if explicit_work_context(context)
    ]
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "identity": identity,
        "heartbeatMinutes": HEARTBEAT_MINUTES,
        "promptCapture": identity["promptCapture"],
        "promptCapturePolicy": identity["promptCapture"],
        "builderPulseEnabled": enabled,
        "captureScope": "enrolled-projects-only",
        "currentProjectEnrolled": current_project_enrolled,
        "effectivePromptCapture": effective_prompt_capture,
        "enrolledProjects": len(enrolled_projects),
        "outboxEvents": len(read_jsonl(data_dir / "outbox.jsonl")),
        "promptOutboxEvents": len(read_jsonl(data_dir / "prompt-outbox.jsonl")),
        "sessions": records,
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    builder = identity.get("builderName") or identity.get("builderId") or "unclaimed"
    print(
        f"Builder: {builder} | prompt policy: {result['promptCapturePolicy']} | "
        f"Builder Pulse enabled: {str(enabled).lower()} | "
        f"current project enrolled: {str(current_project_enrolled).lower()} | "
        f"effective prompt capture: {str(effective_prompt_capture).lower()} | "
        f"state queued: {result['outboxEvents']} | "
        f"prompts queued: {result['promptOutboxEvents']}"
    )
    if not records:
        print("No Builder Pulse state yet. Start a Codex turn after claiming.")
        return 0
    header = f"{'PROJECT':24} {'FEATURE':28} {'STATE':10} LAST SEEN"
    print(header)
    print("-" * len(header))
    for item in records:
        print(
            f"{str(item.get('projectId', ''))[:24]:24} "
            f"{str(item.get('featureLabel', ''))[:28]:28} "
            f"{str(item.get('state', ''))[:10]:10} "
            f"{item.get('lastEmittedAt', '')}"
        )
    return 0


def choose_session(data_dir: Path, requested_key: str | None) -> str:
    if requested_key:
        return validate_identifier(requested_key, "session_key")
    records = status_records(data_dir)
    if records:
        return str(records[0]["sessionKey"])
    raw = os.environ.get("CODEX_SESSION_ID") or "manual-session"
    return session_key(raw)


def command_mark(args: argparse.Namespace, data_dir: Path) -> int:
    config = load_config(data_dir)
    key = choose_session(data_dir, args.session_key)
    payload = {
        "hook_event_name": "ExplicitStateMark",
        "_session_key": key,
        "explicit_state": args.state,
        "cwd": str(Path.cwd()),
    }
    event = record_hook_event(data_dir, payload, config)
    if event is None:
        print(
            "This project is not enrolled. Run work enroll before marking state.",
            file=sys.stderr,
        )
        return 2
    if event is not None:
        flush_outbox(data_dir, config)
        flush_prompt_outbox(data_dir, config)
    print(json.dumps({"state": args.state, "sessionKey": key}, indent=2))
    return 0


def display_config(config: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    result = dict(config)
    result["endpoint"] = sanitized_endpoint(str(result.get("endpoint") or ""))
    result["dataDir"] = str(data_dir)
    result["heartbeatMinutes"] = HEARTBEAT_MINUTES
    identity = safe_identity_summary(data_dir)
    result["promptCapture"] = identity["promptCapture"]
    result["identity"] = identity
    return result


def command_config(args: argparse.Namespace, data_dir: Path) -> int:
    path = data_dir / "config.json"
    overrides = read_json(path, {})
    if not isinstance(overrides, dict):
        overrides = {}
    if args.config_command == "show":
        print(json.dumps(display_config(load_config(data_dir), data_dir), indent=2, sort_keys=True))
        return 0
    if args.config_command == "path":
        print(path)
        return 0
    if args.config_command == "set":
        try:
            value = validate_config_value(args.key, args.value)
        except (TypeError, ValueError) as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        discarded = None
        with scope_delivery_lock(data_dir):
            with data_lock(data_dir):
                overrides = read_json(path, {})
                if not isinstance(overrides, dict):
                    overrides = {}
                if args.key == "endpoint":
                    identity = read_json(identity_path(data_dir), {})
                    bound = (
                        identity.get("claimedEndpoint")
                        if isinstance(identity, dict)
                        else None
                    )
                    if isinstance(bound, str) and bound and value != bound:
                        print(
                            "Configuration error: endpoint is bound to the claimed "
                            "identity and cannot be changed",
                            file=sys.stderr,
                        )
                        return 2
                overrides[args.key] = value
                save_config_overrides(data_dir, overrides)
                if args.key == "enabled" and value is False:
                    discarded = discard_all_pending_data_unlocked(data_dir)
        result = display_config(load_config(data_dir), data_dir)
        if discarded is not None:
            result["discardedPendingOnDisable"] = discarded
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.config_command == "unset":
        with scope_delivery_lock(data_dir):
            with data_lock(data_dir):
                overrides = read_json(path, {})
                if not isinstance(overrides, dict):
                    overrides = {}
                overrides.pop(args.key, None)
                save_config_overrides(data_dir, overrides)
        print(json.dumps(display_config(load_config(data_dir), data_dir), indent=2, sort_keys=True))
        return 0
    return 2


def command_work(args: argparse.Namespace, data_dir: Path) -> int:
    ensure_project_scope_migration(data_dir)
    requested_root = getattr(args, "root", None)
    requested_project = getattr(args, "project", None)
    if args.work_command == "enroll" and sys.stdin.isatty():
        if not requested_root:
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
            requested_root = entered_root or str(current_folder)
        if not requested_project:
            requested_project = input(
                "What display name should GrowthX use for this project?: "
            ).strip()
    target_root = resolved_directory(requested_root)
    exact_key = path_key(data_dir, target_root)
    path = data_dir / "contexts.json"
    contexts = load_work_contexts(data_dir)
    matched = enrolled_work_context(data_dir, target_root)
    if args.work_command == "show" and matched is not None:
        key, matched_context, target_root = matched
        scoped = dict(matched_context)
    else:
        key = exact_key
        scoped = dict(contexts.get(key, {}))
    if args.work_command == "list":
        projects = [
            {
                "contextKey": context_key,
                "projectId": context.get("project_id"),
                "projectLabel": context.get("project_label"),
            }
            for context_key, context in contexts.items()
            if explicit_work_context(context)
        ]
        print(
            json.dumps(
                {"scope": "explicit-project-allowlist", "projects": projects},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.work_command == "show":
        enrolled = explicit_work_context(scoped)
        print(
            json.dumps(
                {
                    "contextKey": key,
                    "root": str(target_root),
                    "rootLabel": target_root.name,
                    "enrolled": enrolled,
                    "projectId": scoped.get("project_id") if enrolled else None,
                    "projectLabel": scoped.get("project_label") if enrolled else None,
                    "featureId": scoped.get("feature_id") if enrolled else None,
                    "featureLabel": scoped.get("feature_label") if enrolled else None,
                    "capture": "enrolled-projects-only",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.work_command == "unenroll":
        with scope_delivery_lock(data_dir):
            with data_lock(data_dir):
                contexts = load_work_contexts(data_dir)
                removed_context = contexts.pop(key, None)
                removed = removed_context is not None
                atomic_write_json(path, contexts)
                discarded = (
                    discard_project_scope_data_unlocked(
                        data_dir,
                        str(removed_context.get("scope_key") or ""),
                    )
                    if isinstance(removed_context, dict)
                    else {"lifecycle": 0, "prompts": 0, "states": 0}
                )
        effective = enrolled_work_context(data_dir, target_root)
        print(
            json.dumps(
                {
                    "enrolled": effective is not None,
                    "removed": removed,
                    "root": str(target_root),
                    "discardedPending": discarded,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.work_command == "clear-feature":
        with scope_delivery_lock(data_dir):
            with data_lock(data_dir):
                contexts = load_work_contexts(data_dir)
                current = contexts.get(key, {})
                if not explicit_work_context(current):
                    print(
                        "Work context error: this exact project folder is not enrolled",
                        file=sys.stderr,
                    )
                    return 2
                updated = dict(current)
                updated.pop("feature_id", None)
                updated.pop("feature_label", None)
                contexts[key] = updated
                atomic_write_json(path, contexts)
        print(f"Feature cleared for project folder {target_root.name}; project remains unchanged.")
        return 0
    if args.work_command in {"enroll", "set"}:
        if args.work_command == "enroll":
            try:
                target_root = validate_enrollment_root(target_root)
                if not target_root.is_dir():
                    raise ValueError("project folder does not exist")
                key = path_key(data_dir, target_root)
                project_label = validate_project_label(requested_project)
                if not project_label:
                    raise ValueError(
                        "project name is required; run interactively or pass --project"
                    )
                requested_project_id = (
                    validate_identifier(args.project_id, "project_id")
                    if args.project_id
                    else None
                )
                if args.project_id and not requested_project_id:
                    raise ValueError("project name must contain letters or numbers")
                ancestor_keys = enrollment_ancestor_keys(data_dir, target_root)
            except ValueError as exc:
                print(f"Work context error: {exc}", file=sys.stderr)
                return 2
            with scope_delivery_lock(data_dir):
                with data_lock(data_dir):
                    contexts = load_work_contexts(data_dir)
                    replace_existing = bool(
                        getattr(args, "replace_existing", False)
                    )
                    candidate_contexts = {} if replace_existing else contexts
                    if overlapping_enrollment(candidate_contexts, key, ancestor_keys):
                        print(
                            "Work context error: this folder overlaps an already "
                            "enrolled project; enroll only one project boundary",
                            file=sys.stderr,
                        )
                        return 2
                    existing = {} if replace_existing else contexts.get(key, {})
                    # Contexts written before explicit project enrollment had no
                    # member-confirmed project label. Do not revive a legacy feature
                    # label merely because its old path hash happens to match the
                    # newly confirmed folder. Already-explicit contexts may retain
                    # their separately confirmed feature when renamed/re-enrolled.
                    scoped = dict(existing) if explicit_work_context(existing) else {}
                    project_id = (
                        requested_project_id
                        or scoped.get("project_id")
                        or project_identifier_from_label(project_label)
                    )
                    unchanged_scope = bool(
                        scoped.get("project_id") == project_id
                        and scoped.get("project_label") == project_label
                        and valid_uuid(scoped.get("scope_key"))
                    )
                    previous_scope_key = str(scoped.get("scope_key") or "")
                    scoped["scope_key"] = (
                        previous_scope_key if unchanged_scope else str(uuid.uuid4())
                    )
                    scoped["project_id"] = project_id
                    scoped["project_label"] = project_label
                    scoped["ancestor_keys"] = ancestor_keys
                    if replace_existing:
                        # Commit the new fail-closed allowlist before deleting
                        # anything. If this atomic write fails, the old scope and
                        # all of its pending local data remain untouched.
                        contexts = {key: scoped}
                        atomic_write_json(path, contexts)
                        discard_all_pending_data_unlocked(data_dir)
                        try:
                            (data_dir / "quarantine.jsonl").unlink()
                        except FileNotFoundError:
                            pass
                    else:
                        contexts[key] = scoped
                        atomic_write_json(path, contexts)
                    if previous_scope_key and not unchanged_scope:
                        discard_project_scope_data_unlocked(
                            data_dir, previous_scope_key
                        )
            return command_work(
                argparse.Namespace(work_command="show", root=str(target_root)), data_dir
            )

        if args.project is not None:
            print(
                "Work context error: --project cannot be changed with work set; "
                "use work enroll --project with the member-confirmed display name",
                file=sys.stderr,
            )
            return 2
        if args.feature is None:
            print("Set --feature, or use work enroll to add a project folder.", file=sys.stderr)
            return 2
        try:
            label = validate_feature_label(args.feature)
            if not label:
                raise ValueError("feature must not be empty")
            feature_id = (
                validate_identifier(args.feature_id, "feature_id")
                if args.feature_id
                else sanitize_identifier(label.lower(), "feature")
            )
        except ValueError as exc:
            print(f"Work context error: {exc}", file=sys.stderr)
            return 2
        with scope_delivery_lock(data_dir):
            with data_lock(data_dir):
                contexts = load_work_contexts(data_dir)
                current = contexts.get(key, {})
                if not explicit_work_context(current):
                    print(
                        "Work context error: enroll this exact project folder with "
                        "work enroll --project before setting a feature",
                        file=sys.stderr,
                    )
                    return 2
                updated = dict(current)
                updated["feature_label"] = label
                updated["feature_id"] = feature_id
                contexts[key] = updated
                atomic_write_json(path, contexts)
        return command_work(
            argparse.Namespace(work_command="show", root=str(target_root)), data_dir
        )
    return 2


def validate_builder_name(value: Any) -> str:
    text = str(value).strip()
    if not text or len(text) > 120 or any(ord(character) < 32 for character in text):
        raise ValueError("invalid builderName")
    return text


def validate_member_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid memberId")
    text = value.strip()
    if (
        not text
        or len(text) > 128
        or text != value
        or re.fullmatch(r"[A-Za-z0-9._:-]+", text) is None
    ):
        raise ValueError("invalid memberId")
    return text


def validate_claim_response(response: dict[str, Any]) -> tuple[str, str, str]:
    builder_id = validate_identifier(response.get("builderId"), "builderId")
    member_id = validate_member_id(response.get("memberId"))
    builder_name = validate_builder_name(response.get("name"))
    if not builder_id or not member_id:
        raise ValueError("invalid claim identity")
    if response.get("heartbeatMinutes") != HEARTBEAT_MINUTES:
        raise ValueError("unsupported heartbeat policy")
    if response.get("promptCapture") != PROMPT_CAPTURE_POLICY:
        raise ValueError("unsupported prompt capture policy")
    return builder_id, member_id, builder_name


def command_claim(args: argparse.Namespace, data_dir: Path) -> int:
    with claim_lease(data_dir):
        return _command_claim_locked(args, data_dir)


def _command_claim_locked(args: argparse.Namespace, data_dir: Path) -> int:
    config = load_config(data_dir)
    try:
        endpoint = validate_endpoint(args.endpoint or config.get("endpoint") or "")
    except ValueError as exc:
        print(f"Claim error: {exc}", file=sys.stderr)
        return 2
    if not endpoint:
        print("Claim error: configure --endpoint or BUILDER_PULSE_ENDPOINT.", file=sys.stderr)
        return 2
    identity = ensure_identity(data_dir)
    claimed_token = identity.get("installationToken")
    claimed_endpoint = identity.get("claimedEndpoint")
    invite_code = args.code or os.environ.get("BUILDER_PULSE_INVITE_CODE")
    if isinstance(claimed_token, str) and claimed_token:
        if not isinstance(claimed_endpoint, str) or claimed_endpoint != endpoint:
            print(
                "Claim error: this installation is already bound to a different endpoint; "
                "use the documented replacement-device/reset flow.",
                file=sys.stderr,
            )
            return 2
        if invite_code:
            try:
                existing_builder_id = validate_identifier(
                    identity.get("builderId"), "builderId"
                )
                existing_member_id = validate_member_id(identity.get("memberId"))
                if not existing_builder_id:
                    raise ValueError("invalid builderId")
            except (TypeError, ValueError):
                print(
                    "Claim error: the existing identity cannot be safely reverified; "
                    "use the documented replacement-device/reset flow.",
                    file=sys.stderr,
                )
                return 2

            print(SETUP_DISCLOSURE, file=sys.stderr)
            request_payload = {
                "schemaVersion": CLAIM_SCHEMA_VERSION,
                "inviteCode": invite_code,
                "installationId": identity["installationId"],
                "installationToken": claimed_token,
                "pluginVersion": PLUGIN_VERSION,
            }
            ok, result, response = http_post_json(
                endpoint_url(endpoint, "/v1/claim"),
                request_payload,
                token=None,
                timeout=float(config.get("claim_timeout_seconds", 10.0)),
                expect_json=True,
            )
            if not ok or response is None:
                print(f"Claim failed: {result}.", file=sys.stderr)
                return 1
            try:
                builder_id, member_id, builder_name = validate_claim_response(response)
            except (TypeError, ValueError):
                print("Claim failed: invalid_response.", file=sys.stderr)
                return 1
            if (
                builder_id != existing_builder_id
                or member_id != existing_member_id
            ):
                print(
                    "Claim failed: invite_identity_mismatch.",
                    file=sys.stderr,
                )
                return 1

            # The server is authoritative for mutable roster/capture fields.
            # Preserve the installation identity, token, endpoint, and local
            # scope secret exactly while repairing stale promptCapture/name.
            refreshed_identity = dict(identity)
            refreshed_identity.update(
                {
                    "builderId": builder_id,
                    "memberId": member_id,
                    "builderName": builder_name,
                    "promptCapture": PROMPT_CAPTURE_POLICY,
                }
            )
            atomic_write_json(identity_path(data_dir), refreshed_identity)

            print(
                json.dumps(
                    {
                        "claimed": True,
                        "alreadyClaimed": True,
                        "reverified": True,
                        "installationId": identity["installationId"],
                        "builderId": builder_id,
                        "memberId": member_id,
                        "builderName": builder_name,
                        "tokenConfigured": True,
                        "heartbeatMinutes": HEARTBEAT_MINUTES,
                        "promptCapture": PROMPT_CAPTURE_POLICY,
                        "captureScope": "enrolled-projects-only",
                        "projectEnrollmentRequired": True,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        print(
            json.dumps(
                {
                    "claimed": True,
                    "alreadyClaimed": True,
                    "installationId": identity["installationId"],
                    "builderId": identity.get("builderId"),
                    "memberId": identity.get("memberId"),
                    "builderName": identity.get("builderName"),
                    "tokenConfigured": True,
                    "promptCapture": (
                        PROMPT_CAPTURE_POLICY
                        if identity.get("promptCapture") == PROMPT_CAPTURE_POLICY
                        else "off"
                    ),
                    "captureScope": "enrolled-projects-only",
                    "projectEnrollmentRequired": True,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if not invite_code and sys.stdin.isatty():
        invite_code = getpass.getpass("One-time invite code: ")
    if not invite_code:
        print("Claim error: provide --code or BUILDER_PULSE_INVITE_CODE.", file=sys.stderr)
        return 2

    print(SETUP_DISCLOSURE, file=sys.stderr)

    pending_token = identity.get("pendingInstallationToken")
    pending_endpoint = identity.get("pendingEndpoint")
    if pending_token is not None or pending_endpoint is not None:
        if (
            not isinstance(pending_token, str)
            or not re.fullmatch(r"[0-9a-f]{64}", pending_token)
            or pending_endpoint != endpoint
        ):
            print(
                "Claim error: a pending claim is bound to a different endpoint or is invalid; "
                "do not replace it automatically.",
                file=sys.stderr,
            )
            return 2
    else:
        pending_token = secrets.token_hex(32)
        identity["pendingInstallationToken"] = pending_token
        identity["pendingEndpoint"] = endpoint
        atomic_write_json(identity_path(data_dir), identity)

    request_payload = {
        "schemaVersion": CLAIM_SCHEMA_VERSION,
        "inviteCode": invite_code,
        "installationId": identity["installationId"],
        "installationToken": pending_token,
        "pluginVersion": PLUGIN_VERSION,
    }
    ok, result, response = http_post_json(
        endpoint_url(endpoint, "/v1/claim"),
        request_payload,
        token=None,
        timeout=float(config.get("claim_timeout_seconds", 10.0)),
        expect_json=True,
    )
    if not ok or response is None:
        print(f"Claim failed: {result}.", file=sys.stderr)
        return 1
    try:
        builder_id, member_id, builder_name = validate_claim_response(response)
    except (TypeError, ValueError):
        print("Claim failed: invalid_response.", file=sys.stderr)
        return 1

    claimed = {
        "installationId": identity["installationId"],
        "scopeSecret": identity["scopeSecret"],
        "installationToken": pending_token,
        "builderId": builder_id,
        "memberId": member_id,
        "builderName": builder_name,
        "claimedEndpoint": endpoint,
        "promptCapture": PROMPT_CAPTURE_POLICY,
    }
    atomic_write_json(identity_path(data_dir), claimed)

    overrides = read_json(data_dir / "config.json", {})
    if not isinstance(overrides, dict):
        overrides = {}
    overrides["endpoint"] = endpoint
    # Older versions treated the roster's cohort/default value as a machine-wide
    # project fallback. Project identity is now member-confirmed per folder.
    overrides.pop("project_id", None)
    save_config_overrides(data_dir, overrides)

    print(
        json.dumps(
            {
                "claimed": True,
                "installationId": claimed["installationId"],
                "builderId": builder_id,
                "memberId": member_id,
                "builderName": builder_name,
                "tokenConfigured": True,
                "heartbeatMinutes": HEARTBEAT_MINUTES,
                "promptCapture": PROMPT_CAPTURE_POLICY,
                "captureScope": "enrolled-projects-only",
                "projectEnrollmentRequired": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_flush(data_dir: Path) -> int:
    config = load_config(data_dir)
    result = {
        "telemetry": flush_outbox(data_dir, config),
        "prompts": flush_prompt_outbox(data_dir, config),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


EXPECTED_PLUGIN_HOOK_EVENTS = {
    "permissionRequest",
    "postToolUse",
    "sessionEnd",
    "sessionStart",
    "userPromptSubmit",
}


def evaluate_builder_pulse_hooks(response: Any) -> dict[str, Any]:
    """Reduce Codex's official hooks/list response to a safe readiness result."""
    if not isinstance(response, dict):
        return {"ready": False, "hookStatus": "invalid_response"}
    result = response.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        return {"ready": False, "hookStatus": "invalid_response"}

    entry = data[0]
    if entry.get("errors"):
        return {"ready": False, "hookStatus": "discovery_error"}
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return {"ready": False, "hookStatus": "invalid_response"}
    plugin_hooks = [
        hook
        for hook in hooks
        if isinstance(hook, dict)
        and hook.get("pluginId") == "builder-pulse@growthx-builder-tools"
    ]
    if not plugin_hooks:
        return {"ready": False, "hookStatus": "not_loaded", "hookCount": 0}

    source_path = (PLUGIN_ROOT / "hooks" / "hooks.json").resolve(strict=False)
    discovered_event_counts = Counter(
        hook.get("eventName")
        for hook in plugin_hooks
        if isinstance(hook.get("eventName"), str)
    )
    expected_event_counts = Counter(
        {event_name: 1 for event_name in EXPECTED_PLUGIN_HOOK_EVENTS}
    )
    if (
        len(plugin_hooks) != len(EXPECTED_PLUGIN_HOOK_EVENTS)
        or discovered_event_counts != expected_event_counts
    ):
        return {
            "ready": False,
            "hookStatus": "incomplete",
            "hookCount": len(plugin_hooks),
        }
    try:
        current_plugin = all(
            Path(str(hook.get("sourcePath"))).resolve(strict=False) == source_path
            for hook in plugin_hooks
        )
    except (OSError, ValueError):
        current_plugin = False
    if not current_plugin:
        return {
            "ready": False,
            "hookStatus": "stale_plugin",
            "hookCount": len(plugin_hooks),
        }
    if not all(hook.get("enabled") is True for hook in plugin_hooks):
        return {
            "ready": False,
            "hookStatus": "disabled",
            "hookCount": len(plugin_hooks),
        }
    trust_statuses = {hook.get("trustStatus") for hook in plugin_hooks}
    if not trust_statuses.issubset({"trusted", "managed"}):
        status = "modified" if "modified" in trust_statuses else "review_required"
        return {
            "ready": False,
            "hookStatus": status,
            "hookCount": len(plugin_hooks),
        }
    return {
        "ready": True,
        "hookStatus": "managed" if trust_statuses == {"managed"} else "trusted",
        "hookCount": len(plugin_hooks),
    }


def inspect_codex_hooks(cwd: Path, timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Call the local Codex app-server hooks/list API without changing config."""
    codex = shutil.which("codex")
    if not codex:
        return {"ready": False, "hookStatus": "codex_not_found"}

    try:
        process = subprocess.Popen(
            [codex, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError:
        return {"ready": False, "hookStatus": "app_server_unavailable"}

    responses: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            responses.put(line)
        responses.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()

    def send(message: dict[str, Any]) -> bool:
        if process.stdin is None:
            return False
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def receive(response_id: int) -> dict[str, Any] | None:
        deadline = dt.datetime.now(dt.timezone.utc).timestamp() + timeout_seconds
        while True:
            remaining = deadline - dt.datetime.now(dt.timezone.utc).timestamp()
            if remaining <= 0:
                return None
            try:
                line = responses.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(response, dict) and response.get("id") == response_id:
                return response

    try:
        initialized = send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "builder_pulse_verifier",
                        "title": "Builder Pulse Verifier",
                        "version": PLUGIN_VERSION,
                    },
                    "capabilities": {},
                },
            }
        ) and receive(1)
        if not initialized or "error" in initialized:
            return {"ready": False, "hookStatus": "app_server_unavailable"}
        if not send({"method": "initialized", "params": {}}) or not send(
            {
                "method": "hooks/list",
                "id": 2,
                "params": {"cwds": [str(cwd.resolve(strict=False))]},
            }
        ):
            return {"ready": False, "hookStatus": "app_server_unavailable"}
        response = receive(2)
        if response is None:
            return {"ready": False, "hookStatus": "app_server_unavailable"}
        return evaluate_builder_pulse_hooks(response)
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


EXPECTED_CLAUDE_HOOK_EVENTS = {
    "PermissionRequest",
    "PostToolUse",
    "SessionEnd",
    "SessionStart",
    "UserPromptSubmit",
}


def expected_claude_plugin_id() -> str:
    name = (
        "builder-pulse-claude-windows"
        if os.name == "nt"
        else "builder-pulse-claude-posix"
    )
    marketplace = f"growthx-builder-tools-v{PLUGIN_VERSION.replace('.', '-')}"
    return f"{name}@{marketplace}"


def evaluate_claude_plugins(response: Any) -> dict[str, Any]:
    """Reduce Claude's official plugin list to a bounded hook readiness result."""
    if not isinstance(response, list):
        return {"ready": False, "hookStatus": "invalid_response"}
    expected_id = expected_claude_plugin_id()
    matches = [
        entry
        for entry in response
        if isinstance(entry, dict) and entry.get("id") == expected_id
    ]
    if len(matches) != 1:
        return {"ready": False, "hookStatus": "not_loaded", "hookCount": 0}
    entry = matches[0]
    if entry.get("enabled") is not True:
        return {"ready": False, "hookStatus": "disabled", "hookCount": 0}
    if entry.get("scope") != "user" or entry.get("version") != PLUGIN_VERSION:
        return {"ready": False, "hookStatus": "stale_plugin", "hookCount": 0}
    install_path = entry.get("installPath")
    if not isinstance(install_path, str) or not install_path:
        return {"ready": False, "hookStatus": "invalid_response", "hookCount": 0}
    try:
        plugin_root = Path(install_path).resolve(strict=True)
        manifest_path = plugin_root / "hooks" / "hooks.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise OSError
        manifest = read_json(manifest_path, {})
    except (OSError, ValueError):
        return {"ready": False, "hookStatus": "stale_plugin", "hookCount": 0}
    hooks = manifest.get("hooks") if isinstance(manifest, dict) else None
    if not isinstance(hooks, dict) or set(hooks) != EXPECTED_CLAUDE_HOOK_EVENTS:
        return {"ready": False, "hookStatus": "incomplete", "hookCount": 0}
    launcher_name = (
        "builder_pulse_claude.cmd"
        if os.name == "nt"
        else "builder_pulse_claude.sh"
    )
    for event_name in EXPECTED_CLAUDE_HOOK_EVENTS:
        groups = hooks.get(event_name)
        if not isinstance(groups, list) or len(groups) != 1:
            return {"ready": False, "hookStatus": "incomplete", "hookCount": 0}
        group = groups[0]
        commands = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(commands, list) or len(commands) != 1:
            return {"ready": False, "hookStatus": "incomplete", "hookCount": 0}
        command = commands[0]
        if (
            not isinstance(command, dict)
            or command.get("type") != "command"
            or launcher_name not in str(command.get("command") or "")
        ):
            return {"ready": False, "hookStatus": "incomplete", "hookCount": 0}
        if os.name == "nt" and command.get("shell") != "powershell":
            return {"ready": False, "hookStatus": "incomplete", "hookCount": 0}
    return {
        "ready": True,
        "hookStatus": "installed",
        "hookCount": len(EXPECTED_CLAUDE_HOOK_EVENTS),
        "_pluginRoot": str(plugin_root),
    }


def inspect_claude_hooks(timeout_seconds: float = 10.0) -> dict[str, Any]:
    claude = shutil.which("claude")
    if not claude:
        return {"ready": False, "hookStatus": "claude_not_found"}
    try:
        completed = subprocess.run(
            [claude, "plugin", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ready": False, "hookStatus": "plugin_list_unavailable"}
    if completed.returncode != 0:
        return {"ready": False, "hookStatus": "plugin_list_unavailable"}
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"ready": False, "hookStatus": "invalid_response"}
    return evaluate_claude_plugins(response)


def verify_hook_launcher(
    data_dir: Path,
    agent_platform: str = DEFAULT_AGENT_PLATFORM,
    plugin_root: Path | None = None,
) -> dict[str, Any]:
    """Execute the exact platform launcher the selected agent will invoke."""
    launcher_root = plugin_root or PLUGIN_ROOT
    environment = dict(os.environ)
    environment["BUILDER_PULSE_DATA_DIR"] = str(data_dir)
    environment["BUILDER_PULSE_AGENT_PLATFORM"] = agent_platform
    environment["BUILDER_PULSE_PLUGIN_VERSION"] = PLUGIN_VERSION
    if os.name == "nt":
        launcher_name = (
            "builder_pulse_claude.cmd"
            if agent_platform == "claude_code"
            else "builder_pulse.cmd"
        )
        if agent_platform == "claude_code":
            environment["CLAUDE_PLUGIN_ROOT"] = str(launcher_root)
            powershell = (
                shutil.which("pwsh")
                or shutil.which("powershell")
                or shutil.which("powershell.exe")
            )
            if not powershell:
                return {"ready": False, "hookStatus": "launcher_unavailable"}
            command = [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f'& "{launcher_root}\\scripts\\{launcher_name}"',
            ]
        else:
            environment["PLUGIN_ROOT"] = str(launcher_root)
            # Expand PLUGIN_ROOT exactly once. CALL reparses the expanded path and
            # corrupts legal roots containing paired percent tokens such as %TEAM%.
            command = f'cmd /d /s /c ""%PLUGIN_ROOT%\\scripts\\{launcher_name}""'
    else:
        launcher_name = (
            "builder_pulse_claude.sh"
            if agent_platform == "claude_code"
            else "builder_pulse.sh"
        )
        command = ["sh", str(launcher_root / "scripts" / launcher_name)]
    try:
        completed = subprocess.run(
            command,
            input="{}\n",
            capture_output=True,
            text=True,
            env=environment,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ready": False, "hookStatus": "launcher_unavailable"}
    if completed.returncode != 0:
        return {"ready": False, "hookStatus": "launcher_unavailable"}
    try:
        output = json.loads(completed.stdout.strip())
    except json.JSONDecodeError:
        return {"ready": False, "hookStatus": "launcher_invalid_output"}
    if output != {}:
        return {"ready": False, "hookStatus": "launcher_invalid_output"}
    return {"ready": True, "hookStatus": "launcher_verified"}


def command_activate(
    data_dir: Path,
    cwd: Path | None = None,
    agent_platform: str = DEFAULT_AGENT_PLATFORM,
) -> int:
    """Verify one agent's hook readiness and the claimed server connection."""
    identity = claimed_identity(data_dir)
    token = identity.get("installationToken")
    endpoint = identity.get("claimedEndpoint")
    if (
        not isinstance(token, str)
        or not token
        or not isinstance(endpoint, str)
        or not endpoint
    ):
        print("Activation failed: this installation has not been claimed.", file=sys.stderr)
        return 2

    config = load_config(data_dir)
    if config.get("enabled") is not True:
        print(
            json.dumps(
                {
                    "connected": False,
                    "activationReady": False,
                    "ready": False,
                    "reviewRequired": False,
                    "hookStatus": "disabled",
                    "agentPlatform": agent_platform,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 3

    hook_result = (
        inspect_codex_hooks(cwd or Path.cwd())
        if agent_platform == "codex"
        else inspect_claude_hooks()
    )
    if not hook_result.get("ready"):
        public_hook_result = {
            key: value
            for key, value in hook_result.items()
            if not key.startswith("_")
        }
        print(
            json.dumps(
                {
                    "connected": False,
                    "activationReady": False,
                    "reviewRequired": hook_result.get("hookStatus")
                    in {"modified", "review_required"},
                    "agentPlatform": agent_platform,
                    **public_hook_result,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 3

    plugin_root_value = hook_result.get("_pluginRoot")
    launcher_root = (
        Path(plugin_root_value)
        if isinstance(plugin_root_value, str) and plugin_root_value
        else None
    )
    launcher_result = verify_hook_launcher(
        data_dir,
        agent_platform,
        plugin_root=launcher_root,
    )
    if not launcher_result.get("ready"):
        print(
            json.dumps(
                {
                    "connected": False,
                    "activationReady": False,
                    "reviewRequired": False,
                    "agentPlatform": agent_platform,
                    **launcher_result,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 3

    ok, result, response = http_post_json(
        endpoint_url(endpoint, "/v1/activation"),
        {
            "schemaVersion": 1,
            "installationId": identity.get("installationId"),
            "pluginVersion": PLUGIN_VERSION,
            "agentPlatform": agent_platform,
        },
        token=token,
        timeout=float(config.get("claim_timeout_seconds", 10.0)),
        expect_json=True,
    )
    if not ok or not isinstance(response, dict) or response.get("accepted") is not True:
        print(f"Activation failed: {result}.", file=sys.stderr)
        return 1

    telemetry_received = response.get("telemetryReceived") is True
    telemetry_received_since_activation = (
        response.get("telemetryReceivedSincePreviousActivation") is True
    )
    last_signal_at = response.get("lastSignalAt")
    last_signal_plugin_version = response.get("lastSignalPluginVersion")
    last_signal_agent_platform = response.get("lastSignalAgentPlatform")
    current_version_receipt = bool(
        telemetry_received_since_activation
        and isinstance(last_signal_at, int)
        and not isinstance(last_signal_at, bool)
        and last_signal_at > 0
        and last_signal_plugin_version == PLUGIN_VERSION
        and last_signal_agent_platform == agent_platform
    )
    print(
        json.dumps(
            {
                # A successful activation proves identity and hook trust. Only
                # a same-version server receipt after the previous activation
                # proves this repaired hook path delivered telemetry.
                "connected": current_version_receipt,
                "activationReady": True,
                "hooksVerified": True,
                "hooksTrusted": agent_platform == "codex",
                "pluginInstalled": agent_platform == "claude_code",
                "serverVerified": True,
                "telemetryReceived": telemetry_received,
                "telemetryReceivedSincePreviousActivation": (
                    telemetry_received_since_activation
                ),
                "lastSignalAt": last_signal_at
                if isinstance(last_signal_at, int)
                else None,
                "lastSignalPluginVersion": last_signal_plugin_version
                if isinstance(last_signal_plugin_version, str)
                else None,
                "lastSignalAgentPlatform": last_signal_agent_platform
                if last_signal_agent_platform in AGENT_PLATFORMS
                else None,
                "agentPlatform": agent_platform,
                "hookStatus": hook_result["hookStatus"],
                "hookCount": hook_result["hookCount"],
                "installationId": identity.get("installationId"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Builder Pulse lifecycle, token, and submitted-prompt telemetry"
    )
    parser.add_argument(
        "--data-dir",
        help="Override plugin data directory (normally PLUGIN_DATA)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("hook", help="Ingest one agent hook payload from stdin")

    claim = subparsers.add_parser("claim", help="Claim this installation once")
    claim.add_argument("--code", help="One-time invite code (interactive prompt if omitted)")
    claim.add_argument("--endpoint", help="Builder Pulse base endpoint")

    status = subparsers.add_parser("status", help="Show current local state")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    status.add_argument("--project", help="Only show one project identifier")

    mark = subparsers.add_parser("mark", help="Explicitly set a coarse state")
    mark.add_argument("state", choices=sorted(STATES - {"idle"}))
    mark.add_argument("--session-key", help="Target a known hashed session key")

    work = subparsers.add_parser("work", help="Set project and feature context")
    work_sub = work.add_subparsers(dest="work_command", required=True)
    work_sub.add_parser("list", help="List explicitly enrolled projects")
    work_show = work_sub.add_parser("show")
    work_show.add_argument("--root", help="Project folder (defaults to current directory)")
    work_enroll = work_sub.add_parser(
        "enroll", help="Explicitly allow telemetry for one project folder"
    )
    work_enroll.add_argument("--root", help="Project folder (defaults to current directory)")
    work_enroll.add_argument(
        "--project", help="Member-confirmed project display name"
    )
    work_enroll.add_argument(
        "--project-id", help="Optional stable project identifier"
    )
    work_enroll.add_argument(
        "--replace-existing",
        action="store_true",
        help="Replace the project allowlist with only this confirmed folder",
    )
    work_unenroll = work_sub.add_parser(
        "unenroll", help="Stop telemetry for one project folder"
    )
    work_unenroll.add_argument(
        "--root", help="Project folder (defaults to current directory)"
    )
    work_set = work_sub.add_parser("set")
    work_set.add_argument("--root", help="Project folder (defaults to current directory)")
    work_set.add_argument(
        "--project",
        help="Deprecated; project folders and names must be confirmed with work enroll",
    )
    work_set.add_argument("--feature", help="Explicit feature label (max 120 chars)")
    work_set.add_argument("--feature-id", help="Optional stable feature identifier")
    work_clear = work_sub.add_parser("clear-feature")
    work_clear.add_argument("--root", help="Project folder (defaults to current directory)")

    config = subparsers.add_parser("config", help="Inspect or change local settings")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show")
    config_sub.add_parser("path")
    config_set = config_sub.add_parser("set")
    config_set.add_argument("key", choices=sorted(CONFIG_KEYS))
    config_set.add_argument("value")
    config_unset = config_sub.add_parser("unset")
    config_unset.add_argument("key", choices=sorted(CONFIG_KEYS))

    subparsers.add_parser("flush", help="Retry queued minimal events")
    activate = subparsers.add_parser(
        "activate",
        help="Verify one agent's official hooks and the claimed server connection",
    )
    activate.add_argument(
        "--agent",
        choices=sorted(AGENT_PLATFORMS),
        default=DEFAULT_AGENT_PLATFORM,
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    data_dir = resolve_data_dir(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "hook":
        return ingest_hook(data_dir)
    if args.command == "claim":
        return command_claim(args, data_dir)
    if args.command == "status":
        return command_status(args, data_dir)
    if args.command == "mark":
        return command_mark(args, data_dir)
    if args.command == "work":
        return command_work(args, data_dir)
    if args.command == "config":
        return command_config(args, data_dir)
    if args.command == "flush":
        return command_flush(data_dir)
    if args.command == "activate":
        return command_activate(data_dir, agent_platform=args.agent)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
