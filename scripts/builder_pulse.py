#!/usr/bin/env python3
"""Builder Pulse: minimal, privacy-bounded Codex lifecycle telemetry.

Hook payloads can contain prompts, commands, paths, source, and tool I/O. This
module may inspect a command in memory to classify a state, but it never writes
or forwards those sensitive values. Only state transitions and a 15-minute
heartbeat can create a telemetry event.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
from typing import Any, Iterable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
import uuid


SCHEMA_VERSION = 1
HEARTBEAT_MINUTES = 15
MAX_ACTIVE_MINUTES = 15
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_PATH = PLUGIN_ROOT / "config" / "defaults.json"
MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
FALLBACK_ROOT = Path.home() / ".builder-pulse"

STATES = {"building", "testing", "blocked", "ready", "idle"}

CONFIG_KEYS = {
    "enabled",
    "endpoint",
    "project_id",
    "feature_id",
    "feature_label",
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


def read_json(path: Path, fallback: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return fallback


def plugin_version() -> str:
    manifest = read_json(MANIFEST_PATH, {})
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return str(version) if isinstance(version, str) and version else "0.0.0"


PLUGIN_VERSION = plugin_version()


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

            handle.seek(0)
            if handle.read(1) == b"":
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

                handle.seek(0)
                if handle.read(1) == b"":
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
def claim_lease(data_dir: Path) -> Iterable[None]:
    """Serialize first-claim attempts so the first pending token is never replaced."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".claim.lock"
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
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
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, separators=(",", ":"), sort_keys=True))
        handle.write("\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


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


def resolve_data_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    configured = os.environ.get("BUILDER_PULSE_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    plugin_data = os.environ.get("PLUGIN_DATA") or os.environ.get(
        "CLAUDE_PLUGIN_DATA"
    )
    if plugin_data:
        return Path(plugin_data).expanduser()
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
    text = str(value).strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return cleaned[:128] or fallback


def validate_identifier(value: Any, field: str) -> str:
    text = str(value).strip()
    if not text:
        return ""
    if len(text) > 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", text):
        raise ValueError(
            f"{field} must use only letters, numbers, dot, underscore, or hyphen"
        )
    return text


def validate_feature_label(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    if len(text) > 120:
        raise ValueError("feature_label must be at most 120 characters")
    if any(ord(character) < 32 for character in text):
        raise ValueError("feature_label must not contain control characters")
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
    if key in {"project_id", "feature_id"}:
        return validate_identifier(value, key)
    if key == "feature_label":
        return validate_feature_label(value)
    if key == "endpoint":
        return validate_endpoint(value)
    return value


def env_override(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value is not None and value != "" else None


def load_config(data_dir: Path) -> dict[str, Any]:
    config = read_json(DEFAULTS_PATH, {})
    overrides = read_json(data_dir / "config.json", {})
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
        "project_id": "BUILDER_PULSE_PROJECT_ID",
        "feature_id": "BUILDER_PULSE_FEATURE_ID",
        "feature_label": "BUILDER_PULSE_FEATURE_LABEL",
        "claim_timeout_seconds": "BUILDER_PULSE_CLAIM_TIMEOUT_SECONDS",
    }
    for key, env_name in env_map.items():
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
        installation_id = valid_uuid(identity.get("installationId"))
        if not installation_id:
            identity = {"installationId": str(uuid.uuid4())}
            atomic_write_json(path, identity)
        else:
            identity["installationId"] = installation_id
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


def session_key(raw_session_id: str) -> str:
    value = raw_session_id or "unknown-session"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def repository_root(value: str | Path | None) -> Path:
    candidate = Path(value or Path.cwd()).expanduser().resolve(strict=False)
    if candidate.is_file():
        candidate = candidate.parent
    for path in (candidate, *candidate.parents):
        if (path / ".git").exists():
            return path
    return candidate


def repository_key(value: str | Path | None) -> str:
    root = repository_root(value)
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def load_work_contexts(data_dir: Path) -> dict[str, dict[str, str]]:
    raw = read_json(data_dir / "contexts.json", {})
    if not isinstance(raw, dict):
        return {}
    contexts: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, dict):
            contexts[key] = {
                field: str(value[field])
                for field in ("project_id", "feature_id", "feature_label")
                if isinstance(value.get(field), str) and value[field]
            }
    return contexts


def scoped_work_context(
    data_dir: Path, cwd: str | Path | None
) -> tuple[str, dict[str, str], str]:
    root = repository_root(cwd)
    key = repository_key(root)
    return key, load_work_contexts(data_dir).get(key, {}), root.name


def safe_project_id(
    payload: dict[str, Any], config: dict[str, Any], scoped: dict[str, str]
) -> str:
    scoped_project = str(scoped.get("project_id", "")).strip()
    if scoped_project:
        return scoped_project
    configured = str(config.get("project_id", "")).strip()
    if configured:
        return configured
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return sanitize_identifier(repository_root(cwd).name, "unknown-project")
    return "unknown-project"


def feature_context(
    config: dict[str, Any], scoped: dict[str, str]
) -> tuple[str | None, str | None]:
    label = str(scoped.get("feature_label") or config.get("feature_label") or "").strip()
    configured_id = str(
        scoped.get("feature_id") or config.get("feature_id") or ""
    ).strip()
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


def state_path(data_dir: Path, key: str) -> Path:
    return data_dir / "states" / f"{key}.json"


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


def telemetry_payload(
    *,
    installation_id: str,
    key: str,
    project_id: str,
    feature_id: str | None,
    feature_label: str | None,
    state: str,
    occurred_at: int,
    active_from: int | None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "eventId": str(uuid.uuid4()),
        "installationId": installation_id,
        "sessionKey": key,
        "projectId": project_id,
        "state": state,
        "occurredAt": occurred_at,
        "pluginVersion": PLUGIN_VERSION,
    }
    if feature_id and feature_label:
        event["featureId"] = feature_id
        event["featureLabel"] = feature_label
    if active_from:
        event["activeFrom"] = active_from
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


def record_hook_event(
    data_dir: Path, payload: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any] | None:
    explicit_key = payload.get("_session_key")
    if isinstance(explicit_key, str) and explicit_key:
        key = validate_identifier(explicit_key, "session_key")
    else:
        raw_session_id = str(
            payload.get("session_id")
            or os.environ.get("CODEX_SESSION_ID")
            or "unknown-session"
        )
        key = session_key(raw_session_id)
    identity = ensure_identity(data_dir)
    installation_id = str(identity["installationId"])
    now_ms = utc_now_ms()
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    context_key, scoped, _ = scoped_work_context(data_dir, cwd)
    project_id = safe_project_id(payload, config, scoped)
    feature_id, feature_label = feature_context(config, scoped)

    with data_lock(data_dir):
        path = state_path(data_dir, key)
        previous = read_json(path, {})
        if not isinstance(previous, dict):
            previous = {}
        previous_state = str(previous.get("state") or "idle")
        next_state = classify_state(payload, previous_state)
        changed = next_state != previous_state
        should_emit = changed or heartbeat_due(previous, now_ms)

        active_from = capped_active_from(previous, now_ms) if should_emit else None
        event = (
            telemetry_payload(
                installation_id=installation_id,
                key=key,
                project_id=project_id,
                feature_id=feature_id,
                feature_label=feature_label,
                state=next_state,
                occurred_at=now_ms,
                active_from=active_from,
            )
            if should_emit
            else None
        )
        previous_last_observed = previous.get("lastObservedAt")
        continuous = (
            previous_state != "idle"
            and isinstance(previous_last_observed, int)
            and now_ms - previous_last_observed <= MAX_ACTIVE_MINUTES * 60 * 1000
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
            "projectId": project_id,
            "state": next_state,
            "stateChangedAt": (
                now_ms if changed else previous.get("stateChangedAt", now_ms)
            ),
            "lastObservedAt": now_ms,
            "activeWindowStartAt": next_window,
            "lastEmittedAt": now_ms if should_emit else previous.get("lastEmittedAt"),
            "lastEventId": event["eventId"] if event else previous.get("lastEventId"),
        }
        if feature_id and feature_label:
            state["featureId"] = feature_id
            state["featureLabel"] = feature_label
        atomic_write_json(path, state)

        if event is None:
            return None
        token = identity.get("installationToken")
        claimed_endpoint = identity.get("claimedEndpoint")
        if isinstance(token, str) and token and isinstance(claimed_endpoint, str):
            enqueue_event_unlocked(
                data_dir,
                event,
                int(config.get("max_outbox_events", 500)),
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
) -> tuple[bool, str, dict[str, Any] | None]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"builder-pulse/{PLUGIN_VERSION}",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
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
        event,
        token=token,
        timeout=float(config.get("delivery_timeout_seconds", 1.0)),
        expect_json=False,
    )
    return ok, result


def permanent_delivery_failure(result: str) -> bool:
    match = re.fullmatch(r"http_(\d{3})", result)
    if not match:
        return False
    status = int(match.group(1))
    return 400 <= status < 500 and status not in {401, 403, 408, 425, 429}


def flush_outbox(data_dir: Path, config: dict[str, Any]) -> dict[str, int]:
    identity = claimed_identity(data_dir)
    token = identity.get("installationToken")
    endpoint = identity.get("claimedEndpoint")
    path = data_dir / "outbox.jsonl"
    if not isinstance(token, str) or not token or not endpoint:
        return {"delivered": 0, "quarantined": 0, "remaining": len(read_jsonl(path))}

    with delivery_lease(data_dir) as acquired:
        if not acquired:
            return {
                "delivered": 0,
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
            ok, result = deliver_event(event, config, token, str(endpoint))
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
            "quarantined": len(quarantined),
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
        config = load_config(data_dir)
        if config.get("enabled", True):
            event = record_hook_event(data_dir, payload, config)
            if event is not None:
                flush_outbox(data_dir, config)
    except Exception:
        # Telemetry is strictly best effort and may never break a builder session.
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
        "builderName": identity.get("builderName"),
        "tokenConfigured": bool(identity.get("installationToken")),
        "claimedEndpoint": sanitized_endpoint(str(identity.get("claimedEndpoint") or "")),
    }


def command_status(args: argparse.Namespace, data_dir: Path) -> int:
    records = status_records(data_dir)
    if args.project:
        records = [item for item in records if item.get("projectId") == args.project]
    identity = safe_identity_summary(data_dir)
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "identity": identity,
        "heartbeatMinutes": HEARTBEAT_MINUTES,
        "promptCapture": "off",
        "outboxEvents": len(read_jsonl(data_dir / "outbox.jsonl")),
        "sessions": records,
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    builder = identity.get("builderName") or identity.get("builderId") or "unclaimed"
    print(f"Builder: {builder} | prompt capture: off | queued: {result['outboxEvents']}")
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
    }
    event = record_hook_event(data_dir, payload, config)
    if event is not None:
        flush_outbox(data_dir, config)
    print(json.dumps({"state": args.state, "sessionKey": key}, indent=2))
    return 0


def display_config(config: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    result = dict(config)
    result["endpoint"] = sanitized_endpoint(str(result.get("endpoint") or ""))
    result["dataDir"] = str(data_dir)
    result["heartbeatMinutes"] = HEARTBEAT_MINUTES
    result["promptCapture"] = "off"
    result["identity"] = safe_identity_summary(data_dir)
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
            if args.key == "endpoint":
                bound = claimed_identity(data_dir).get("claimedEndpoint")
                if isinstance(bound, str) and bound and value != bound:
                    raise ValueError(
                        "endpoint is bound to the claimed identity and cannot be changed"
                    )
            overrides[args.key] = value
        except (TypeError, ValueError) as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        save_config_overrides(data_dir, overrides)
        print(json.dumps(display_config(load_config(data_dir), data_dir), indent=2, sort_keys=True))
        return 0
    if args.config_command == "unset":
        overrides.pop(args.key, None)
        save_config_overrides(data_dir, overrides)
        print(json.dumps(display_config(load_config(data_dir), data_dir), indent=2, sort_keys=True))
        return 0
    return 2


def command_work(args: argparse.Namespace, data_dir: Path) -> int:
    target_root = repository_root(getattr(args, "root", None))
    key = repository_key(target_root)
    path = data_dir / "contexts.json"
    contexts = load_work_contexts(data_dir)
    scoped = dict(contexts.get(key, {}))
    if args.work_command == "show":
        config = load_config(data_dir)
        print(
            json.dumps(
                {
                    "contextKey": key,
                    "rootLabel": target_root.name,
                    "projectId": scoped.get("project_id")
                    or config.get("project_id")
                    or sanitize_identifier(target_root.name, "unknown-project"),
                    "featureId": scoped.get("feature_id")
                    or config.get("feature_id")
                    or None,
                    "featureLabel": scoped.get("feature_label")
                    or config.get("feature_label")
                    or None,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.work_command == "clear-feature":
        scoped.pop("feature_id", None)
        scoped.pop("feature_label", None)
        with data_lock(data_dir):
            contexts = load_work_contexts(data_dir)
            if scoped:
                contexts[key] = scoped
            else:
                contexts.pop(key, None)
            atomic_write_json(path, contexts)
        print(f"Feature cleared for repository {target_root.name}; project remains unchanged.")
        return 0
    if args.work_command == "set":
        if args.project is None and args.feature is None:
            print("Set --project, --feature, or both.", file=sys.stderr)
            return 2
        try:
            if args.project is not None:
                scoped["project_id"] = validate_identifier(args.project, "project_id")
            if args.feature is not None:
                label = validate_feature_label(args.feature)
                if not label:
                    raise ValueError("feature must not be empty")
                scoped["feature_label"] = label
                scoped["feature_id"] = (
                    validate_identifier(args.feature_id, "feature_id")
                    if args.feature_id
                    else sanitize_identifier(label.lower(), "feature")
                )
            elif args.feature_id:
                raise ValueError("--feature-id requires --feature")
        except ValueError as exc:
            print(f"Work context error: {exc}", file=sys.stderr)
            return 2
        with data_lock(data_dir):
            contexts = load_work_contexts(data_dir)
            contexts[key] = scoped
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
    if isinstance(claimed_token, str) and claimed_token:
        if not isinstance(claimed_endpoint, str) or claimed_endpoint != endpoint:
            print(
                "Claim error: this installation is already bound to a different endpoint; "
                "use the documented replacement-device/reset flow.",
                file=sys.stderr,
            )
            return 2
        print(
            json.dumps(
                {
                    "claimed": True,
                    "alreadyClaimed": True,
                    "installationId": identity["installationId"],
                    "builderId": identity.get("builderId"),
                    "builderName": identity.get("builderName"),
                    "tokenConfigured": True,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    invite_code = args.code or os.environ.get("BUILDER_PULSE_INVITE_CODE")
    if not invite_code and sys.stdin.isatty():
        invite_code = getpass.getpass("One-time invite code: ")
    if not invite_code:
        print("Claim error: provide --code or BUILDER_PULSE_INVITE_CODE.", file=sys.stderr)
        return 2

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
        "schemaVersion": SCHEMA_VERSION,
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
        builder_id = validate_identifier(response.get("builderId"), "builderId")
        builder_name = validate_builder_name(response.get("name"))
        if not builder_id:
            raise ValueError("invalid claim identity")
        if response.get("heartbeatMinutes") != HEARTBEAT_MINUTES:
            raise ValueError("unsupported heartbeat policy")
        if response.get("promptCapture") != "off":
            raise ValueError("unsupported prompt capture policy")
    except (TypeError, ValueError):
        print("Claim failed: invalid_response.", file=sys.stderr)
        return 1

    claimed = {
        "installationId": identity["installationId"],
        "installationToken": pending_token,
        "builderId": builder_id,
        "builderName": builder_name,
        "claimedEndpoint": endpoint,
    }
    atomic_write_json(identity_path(data_dir), claimed)

    overrides = read_json(data_dir / "config.json", {})
    if not isinstance(overrides, dict):
        overrides = {}
    overrides["endpoint"] = endpoint
    default_project = response.get("defaultProject")
    effective_project = str(config.get("project_id") or "").strip()
    if isinstance(default_project, str) and default_project.strip() and not effective_project:
        overrides["project_id"] = sanitize_identifier(default_project, "unknown-project")
    save_config_overrides(data_dir, overrides)

    print(
        json.dumps(
            {
                "claimed": True,
                "installationId": claimed["installationId"],
                "builderId": builder_id,
                "builderName": builder_name,
                "tokenConfigured": True,
                "heartbeatMinutes": HEARTBEAT_MINUTES,
                "promptCapture": "off",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_flush(data_dir: Path) -> int:
    result = flush_outbox(data_dir, load_config(data_dir))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Minimal Builder Pulse lifecycle telemetry (raw prompts off)"
    )
    parser.add_argument(
        "--data-dir",
        help="Override plugin data directory (normally PLUGIN_DATA)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("hook", help="Ingest one Codex hook payload from stdin")

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
    work_show = work_sub.add_parser("show")
    work_show.add_argument("--root", help="Repository root (defaults to current directory)")
    work_set = work_sub.add_parser("set")
    work_set.add_argument("--root", help="Repository root (defaults to current directory)")
    work_set.add_argument("--project", help="Concise product/project identifier")
    work_set.add_argument("--feature", help="Explicit feature label (max 120 chars)")
    work_set.add_argument("--feature-id", help="Optional stable feature identifier")
    work_clear = work_sub.add_parser("clear-feature")
    work_clear.add_argument("--root", help="Repository root (defaults to current directory)")

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
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
