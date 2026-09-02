#!/usr/bin/env python3
"""Install, update, or repair Builder Pulse for Codex and Claude Code.

One straight-line sequence. Every step maps to an invariant:

1. only the published immutable release is installed (tag, release API, and
   this checkout must agree on one commit);
2. the member's identity is never created twice or replaced; repair reuses the
   claimed identity from the shared or legacy data directory;
3. nothing is captured while packages are swapped (local off + server pause
   first, resume only after the new package and identity are in place); on any
   failure in between capture stays paused and the previous Codex tag is put
   back;
4. no enrollment without an explicit member answer; never home, temp folders,
   or the installer clone; repair never enrolls by itself;
5. a pending Codex hook review is a state (exit 3), not a failure;
6. every run leaves a redacted log; every failure ends with `Details: <path>`.
"""

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
from typing import Any, NamedTuple
from urllib import error as urlerror
from urllib import request as urlrequest


REPOSITORY = "https://github.com/GrowthX-Club/builder-pulse-plugin.git"
REPOSITORY_SLUG = "GrowthX-Club/builder-pulse-plugin"
APPROVED_SLUGS = {REPOSITORY_SLUG, "udayanwalvekar/builder-pulse-plugin"}
MARKETPLACE = "growthx-builder-tools"
PLUGIN = f"builder-pulse@{MARKETPLACE}"
TARGET_RELEASE = "v0.6.0"
TARGET_VERSION = TARGET_RELEASE.removeprefix("v")
CLAUDE_MARKETPLACE = f"growthx-builder-tools-{TARGET_RELEASE.replace('.', '-')}"
DEFAULT_ENDPOINT = "https://precious-ant-429.convex.site"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY_SLUG}/releases/tags/"
DESKTOP_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
HOOK_REVIEW_EXIT_CODE = 3
CLAIM_KEYS = ("installationToken", "pendingInstallationToken", "builderId")
IDENTITY_FILES = ("identity.json", "setup-paused-identity.json")
QUEUE_FILES = ("outbox.jsonl", "prompt-outbox.jsonl", "quarantine.jsonl")
# Untracked files an official tool or the OS may leave inside a clean checkout:
# Codex's marketplace marker, Finder's .DS_Store, Python bytecode, and the
# session/link markers Claude Code writes at a plugin root.
NOISE_FILES = frozenset({".codex-marketplace-install.json", ".DS_Store"})
NOISE_ROOT_FILES = frozenset({".orphaned_at", ".links_materialized"})
HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{64}\b")
BEARER = re.compile(r"(?i)\bbearer[ \t]+[^\s\"',;]+")
SECRET_FIELD = re.compile(
    r"(?i)(\\?\"?(?:inviteCode|installationToken|pendingInstallationToken|"
    r"BUILDER_PULSE_INVITE_CODE)\\?\"?\s*[:=]\s*)(\\?\"[^\"\\]*\\?\"|'[^']*'|[^\s,;}]+)"
)
SETUP_DISCLOSURE = (
    "Builder Pulse installs hooks for Codex and Claude Code when those agents are "
    "available on this computer, but it sends data only from project folders you "
    "explicitly enroll. One shared identity and project allowlist apply to both agents. "
    "GrowthX stores the claimed member ID, name, email address, and any optional "
    "roster or program label supplied by GrowthX so telemetry can be linked to the "
    "right person. A roster or program label is never used as a telemetry project. "
    "For each enrolled project, it receives a stable installation ID, a one-way hashed "
    "session ID, the display name you confirm and a sanitized project ID, any feature "
    "name and ID you explicitly set, coarse work state and event/activity timestamps, "
    "agent name, plugin version, optional cumulative Codex token counts, and each primary "
    "prompt you submit after secret redaction and a 64 KiB limit. GrowthX's authenticated "
    "Builder Pulse admins can view these identity and telemetry fields for learning "
    "feedback. Raw lifecycle events and activity buckets are retained for 30 days; "
    "submitted prompts and their feedback are retained for 60 days; the member identity "
    "fields, installation/member link, latest status, and compacted session, daily, and "
    "all-time token aggregates remain until GrowthX removes them. It does not send "
    "folder paths, files, patches, commands, tool input or output, assistant replies, "
    "transcripts, or environment variables. Secret redaction is a safety layer, not a "
    "guarantee, so do not put secrets in prompts."
)


class SetupError(RuntimeError):
    pass


# --------------------------------------------------------------------------- log


class SetupLog:
    """Redacted, append-only record of one run; logging never changes behaviour."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.secrets: list[str] = []
        self.buffer: list[str] = []

    def mask(self, secret: str | None) -> None:
        if isinstance(secret, str) and len(secret) >= 8 and secret not in self.secrets:
            self.secrets.append(secret)

    def redact(self, text: Any) -> str:
        value = str(text)
        for secret in self.secrets:
            value = value.replace(secret, "[redacted]")
        value = shorten_home(value)
        value = HEX_TOKEN.sub("[redacted]", value)
        value = BEARER.sub("Bearer [redacted]", value)
        return SECRET_FIELD.sub(lambda m: f"{m.group(1)}[redacted]", value)

    def open(self, data_dir: Path) -> Path | None:
        if self.path is not None:
            return self.path
        try:
            log_dir = data_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(log_dir, 0o700)
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
            path, suffix = log_dir / f"setup-{stamp}.log", 1
            while path.exists():
                path, suffix = log_dir / f"setup-{stamp}-{suffix}.log", suffix + 1
            path.touch(mode=0o600)
            os.chmod(path, 0o600)
            self.path = path
            for line in self.buffer:
                self._append(line)
            self.buffer = []
            logs = sorted(p for p in log_dir.glob("setup-*.log") if p.is_file())
            for stale in logs[:-10]:
                with contextlib.suppress(OSError):
                    stale.unlink()
        except OSError:
            self.path = None
        return self.path

    def _append(self, line: str) -> None:
        assert self.path is not None
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def write(self, message: str, **fields: Any) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        extra = ""
        if fields:
            safe = {k: self.redact(v) if isinstance(v, str) else v for k, v in fields.items()}
            extra = " " + json.dumps(safe, sort_keys=True, default=str, ensure_ascii=False)
        line = self.redact(f"{stamp} {message}{extra}") + "\n"
        if self.path is None:
            self.buffer = (self.buffer + [line])[-500:]
            return
        with contextlib.suppress(OSError):
            self._append(line)

    @staticmethod
    def bounded(text: Any, limit: int = 600) -> str:
        value = str(text or "").strip()
        return value if len(value) <= limit else "…" + value[-limit:]


LOG = SetupLog()


def shorten_home(text: str) -> str:
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):
        return text
    candidates = {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}  # raw, POSIX-style, JSON-escaped
    with contextlib.suppress(OSError):
        resolved = str(Path.home().resolve(strict=False))
        candidates.update({resolved, resolved.replace("\\", "\\\\")})
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate and candidate not in {"/", "\\"}:
            text = text.replace(candidate, "~")
    return text


def folder_label(value: str) -> str:
    name = PurePath(str(value).replace("\\", "/")).name
    return f"…/{name}" if name else "…"


def display_arguments(arguments: list[str]) -> list[str]:
    """argv for the log: secrets masked, project folders reduced to a basename."""
    shown: list[str] = []
    mask: str | None = None
    for argument in arguments:
        if mask:
            shown.append("[redacted]" if mask == "secret" else folder_label(argument))
            mask = None
        elif argument in {"--code", "--token"}:
            shown.append(argument)
            mask = "secret"
        elif argument in {"--root", "--project-root"}:
            shown.append(argument)
            mask = "folder"
        elif argument.startswith("--code="):
            shown.append("--code=[redacted]")
        elif argument.startswith(("--root=", "--project-root=")):
            flag, _, value = argument.partition("=")
            shown.append(f"{flag}={folder_label(value)}")
        else:
            shown.append(argument)
    return shown


# ---------------------------------------------------------------- tools & files


def codex_executable() -> str | None:
    """The Codex CLI: PATH first, then the macOS desktop app's bundled binary."""
    found = shutil.which("codex")
    if found or sys.platform != "darwin" or not DESKTOP_CODEX.is_file():
        return found
    try:
        subprocess.run([str(DESKTOP_CODEX), "--version"], capture_output=True, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return str(DESKTOP_CODEX)


def tool_environment() -> dict[str, str]:
    """Environment for plugin CLI calls; exposes the desktop Codex to activation."""
    env = dict(os.environ)
    codex = codex_executable()
    if codex and not shutil.which("codex"):
        env["PATH"] = str(Path(codex).parent) + os.pathsep + env.get("PATH", "")
    return env


def run_command(arguments: list[str], *, env: dict[str, str] | None = None, expect_json: bool = False) -> Any:
    name = arguments[0]
    if name == "codex":
        executable = codex_executable()
    elif name in {"git", "claude"}:
        executable = shutil.which(name)
    else:
        executable = name
    if executable is None:
        raise SetupError(f"Command could not start: {name}")
    arguments = [executable, *arguments[1:]]
    shown = display_arguments(arguments)
    try:
        completed = subprocess.run(arguments, check=False, capture_output=True, text=True, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.write("command could not start", argv=shown, error=str(exc))
        raise SetupError(f"Command could not start: {name}") from exc
    LOG.write("command finished", argv=shown, returncode=completed.returncode,
              stderr=LOG.bounded(completed.stderr), stdout=LOG.bounded(completed.stdout, 300))
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SetupError(LOG.redact(detail) or f"Command failed: {name}")
    if not expect_json:
        return completed.stdout
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SetupError(f"{name} returned invalid JSON") from exc


def cli(cli_path: Path, *arguments: str, env: dict[str, str] | None = None, expect_json: bool = False) -> Any:
    """Run the verified single-file plugin CLI without import shadowing."""
    return run_command([sys.executable, "-I", "-S", str(cli_path), *arguments],
                       env=env or tool_environment(), expect_json=expect_json)


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"Builder Pulse data is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"Builder Pulse data is invalid: {path.name}")
    return value


def write_object(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def is_filesystem_root(path: PurePath) -> bool:
    return bool(path.anchor) and path == type(path)(path.anchor)


# ------------------------------------------------------------------- release


def approved_slug(source: Any) -> str | None:
    if not isinstance(source, str):
        return None
    slug = source.removesuffix(".git").rstrip("/").removeprefix("https://github.com/")
    return slug if slug in APPROVED_SLUGS else None


def verify_release(release: str) -> str:
    """Return the tag's commit after proving it is a published immutable release."""
    direct, peeled = f"refs/tags/{release}", f"refs/tags/{release}^{{}}"
    refs: dict[str, str] = {}
    for line in str(run_command(["git", "ls-remote", "--exit-code", REPOSITORY, direct, peeled])).splitlines():
        fields = line.split()
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-fA-F]{40}", fields[0]):
            raise SetupError("GitHub returned an invalid Builder Pulse release tag")
        refs[fields[1]] = fields[0].lower()
    commit = refs.get(peeled) or refs.get(direct)
    if commit is None:
        raise SetupError("The immutable Builder Pulse release tag could not be verified")
    request = urlrequest.Request(f"{RELEASE_API}{release}", headers={
        "Accept": "application/vnd.github+json", "User-Agent": "builder-pulse-installer",
        "X-GitHub-Api-Version": "2026-03-10"})
    try:
        with urlrequest.urlopen(request, timeout=10) as response:
            raw = response.read(1024 * 1024 + 1)
    except urlerror.HTTPError as exc:
        if exc.code in {403, 429}:
            raise SetupError("GitHub rate limit reached while verifying the Builder Pulse release; retry later") from exc
        raise SetupError("The immutable Builder Pulse release could not be verified") from exc
    except (OSError, ValueError, urlerror.URLError) as exc:
        raise SetupError("The immutable Builder Pulse release could not be verified") from exc
    try:
        data = json.loads(raw.decode("utf-8")) if len(raw) <= 1024 * 1024 else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    if not (isinstance(data, dict) and data.get("tag_name") == release
            and data.get("draft") is False and data.get("immutable") is True):
        raise SetupError("Builder Pulse setup requires a published immutable GitHub release")
    LOG.write("release verified", release=release, commit=commit)
    return commit


def checkout_is_pristine(porcelain: str) -> bool:
    """Tracked files untouched; untracked entries only from the noise allowlist."""
    for line in porcelain.splitlines():
        if len(line) < 3:
            continue
        status, path = line[:2], line[3:].strip().strip('"')
        if status not in {"??", "!!"}:
            return False
        parts = PurePath(path.replace("\\", "/")).parts
        if not parts or parts[-1] in NOISE_FILES or parts[-1].endswith(".pyc") or "__pycache__" in parts:
            continue
        if len(parts) == 1 and parts[0] in NOISE_ROOT_FILES:
            continue
        if len(parts) == 2 and parts[0] == ".in_use" and parts[1].isdigit():
            continue
        return False
    return True


def verify_installer_checkout(expected_commit: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    origin = str(run_command(["git", "-C", str(root), "remote", "get-url", "origin"])).strip()
    head = str(run_command(["git", "-C", str(root), "rev-parse", "HEAD"])).strip().lower()
    status = str(run_command(["git", "-C", str(root), "status", "--porcelain", "--ignored=matching", "--untracked-files=all"]))
    if approved_slug(origin) is None or head != expected_commit or not checkout_is_pristine(status):
        raise SetupError("The Builder Pulse installer checkout does not match the immutable release")
    for relative in ("scripts/builder_pulse.py", "config/defaults.json", "hooks/hooks.json"):
        if not (root / relative).is_file():
            raise SetupError("The Builder Pulse release runtime is incomplete")
    return root


# --------------------------------------------------------------------- data


def data_dir() -> Path:
    explicit = os.environ.get("BUILDER_PULSE_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)
    return (Path.home() / ".builder-pulse").resolve(strict=False)


def legacy_data_dir() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return (codex_home / "plugins" / "data" / f"builder-pulse-{MARKETPLACE}").expanduser().resolve(strict=False)


def holds_claim(directory: Path) -> bool:
    """A claimed or pending identity lives here; an unclaimed hook skeleton does not count."""
    for name in IDENTITY_FILES:
        record = read_object(directory / name)  # unreadable data fails closed here
        if any(isinstance(record.get(key), str) and record.get(key) for key in CLAIM_KEYS):
            return True
    return False


def migrate_legacy_data(target: Path, source: Path) -> None:
    """Copy the legacy Codex-owned data into the shared directory exactly once.

    A shared directory that already holds a claim is never touched. One that
    holds only logs, config, a stale runtime, or an unclaimed skeleton is merged:
    legacy files win, the skeleton is dropped, and the result replaces the
    directory as one unit so a failure leaves it exactly as it was.
    """
    if not any((source / name).is_file() for name in IDENTITY_FILES):
        return
    if holds_claim(target):
        if holds_claim(source) and current_identity(source).get("installationId") != current_identity(target).get("installationId"):
            raise SetupError("The legacy and shared Builder Pulse identities differ")
        return
    for root in (source, target):
        for candidate in root.rglob("*") if root.exists() else ():
            if candidate.is_symlink():
                raise SetupError("The existing Builder Pulse data contains a symbolic link")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}-migration"
    replaced = target.parent / f".{target.name}-replaced"
    for leftover in (temporary, replaced):
        if leftover.exists():
            raise SetupError(
                f"A previous Builder Pulse data migration is incomplete: {leftover} still "
                "exists; move it aside after checking its contents, then run setup again"
            )
    set_aside = False
    try:
        if target.exists():
            shutil.copytree(target, temporary, symlinks=False)
            for name in IDENTITY_FILES:
                with contextlib.suppress(FileNotFoundError):
                    (temporary / name).unlink()
            shutil.copytree(source, temporary, symlinks=False, dirs_exist_ok=True)
            os.replace(target, replaced)
            set_aside = True
        else:
            shutil.copytree(source, temporary, symlinks=False)
        os.replace(temporary, target)
    except OSError as exc:
        # Never delete on the failure path. Put the previous directory back only
        # when nothing else has taken its place (a running hook may recreate it);
        # otherwise leave every directory for the next run to refuse loudly.
        if set_aside and not target.exists():
            with contextlib.suppress(OSError):
                os.replace(replaced, target)
        if target.exists() and not set_aside:
            shutil.rmtree(temporary, ignore_errors=True)
        raise SetupError("The existing Builder Pulse data could not be migrated safely") from exc
    if set_aside:
        shutil.rmtree(replaced, ignore_errors=True)
    LOG.write("legacy data migrated")


def current_identity(directory: Path) -> dict[str, Any]:
    """The identity to preserve: a paused copy wins over a stripped identity.json."""
    paused, current = read_object(directory / "setup-paused-identity.json"), read_object(directory / "identity.json")
    if paused:
        if current.get("installationId") and current.get("installationId") != paused.get("installationId"):
            raise SetupError("The paused Builder Pulse identity does not match local data")
        return paused
    return current


def require_claimed(identity: dict[str, Any]) -> dict[str, str]:
    fields = {key: identity.get(key) for key in ("installationId", "builderId", "memberId")}
    if not (isinstance(identity.get("installationToken"), str) and identity.get("installationToken")
            and isinstance(identity.get("claimedEndpoint"), str) and identity.get("claimedEndpoint")
            and all(isinstance(v, str) and v for v in fields.values())):
        raise SetupError("The existing Builder Pulse identity is not fully claimed")
    return {key: str(value) for key, value in fields.items()}


def pause_local(directory: Path, identity: dict[str, Any]) -> None:
    """Stop local capture: token moves to the paused copy, queues are dropped."""
    directory.mkdir(parents=True, exist_ok=True)
    if identity and not (directory / "setup-paused-identity.json").exists():
        write_object(directory / "setup-paused-identity.json", identity)
    stripped = {k: v for k, v in identity.items() if k not in {"installationToken", "pendingInstallationToken"}}
    if stripped:
        stripped["promptCapture"] = "off"
        write_object(directory / "identity.json", stripped)
    config = read_object(directory / "config.json")
    config["enabled"] = False
    write_object(directory / "config.json", config)
    for name in QUEUE_FILES:
        with contextlib.suppress(FileNotFoundError):
            (directory / name).unlink()


def restore_identity(directory: Path, expected: dict[str, Any]) -> None:
    paused = directory / "setup-paused-identity.json"
    stored = read_object(paused)
    if not stored:
        return
    if stored != expected:
        raise SetupError("The preserved Builder Pulse identity changed during setup")
    write_object(directory / "identity.json", stored)
    paused.unlink()


def server_call(identity: dict[str, Any], route: str, version: str, confirm: str) -> None:
    token, endpoint, installation = identity.get("installationToken"), identity.get("claimedEndpoint"), identity.get("installationId")
    if not (isinstance(token, str) and token and isinstance(endpoint, str) and endpoint and isinstance(installation, str) and installation):
        raise SetupError("The existing Builder Pulse delivery identity is incomplete")
    payload = json.dumps({"installationId": installation, "pluginVersion": version}, separators=(",", ":")).encode()
    request = urlrequest.Request(f"{endpoint.rstrip('/')}/v1/{route}", data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "User-Agent": f"builder-pulse-installer/{TARGET_VERSION}"})
    try:
        with urlrequest.urlopen(request, timeout=10) as response:
            result = json.loads(response.read(65_536).decode("utf-8"))
    except (OSError, ValueError, urlerror.URLError, http.client.HTTPException) as exc:
        raise SetupError(f"GrowthX could not {route.replace('privacy-', '')} the Builder Pulse installation safely") from exc
    if not (isinstance(result, dict) and result.get(confirm) is True and result.get("installationId") == installation):
        raise SetupError(f"GrowthX did not confirm the Builder Pulse {route}")
    LOG.write(f"server {route}", version=version)


# ------------------------------------------------------------------ packages


def codex_state() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    plugins = run_command(["codex", "plugin", "list", "--json"], expect_json=True)
    installed = [p for p in (plugins.get("installed") if isinstance(plugins, dict) else []) or []
                 if isinstance(p, dict) and p.get("pluginId") == PLUGIN]
    markets = run_command(["codex", "plugin", "marketplace", "list", "--json"], expect_json=True)
    configured = [m for m in (markets.get("marketplaces") if isinstance(markets, dict) else []) or []
                  if isinstance(m, dict) and m.get("name") == MARKETPLACE]
    if len(installed) > 1 or len(configured) > 1:
        raise SetupError("Codex reported more than one Builder Pulse registration")
    return (installed[0] if installed else None), (configured[0] if configured else None)


def marketplace_checkout_commit(configured: dict[str, Any]) -> str | None:
    """HEAD of Codex's marketplace snapshot; the list output carries no ref."""
    root = configured.get("root")
    if not isinstance(root, str) or not root:
        return None
    try:
        head = str(run_command(["git", "-C", root, "rev-parse", "HEAD"])).strip().lower()
    except SetupError:
        return None
    return head if re.fullmatch(r"[0-9a-f]{40}", head) else None


def install_codex(ref: str, version: str, expected_commit: str | None = None) -> Path:
    """Register the marketplace at `ref` and install the plugin; verify the version.

    A rerun that finds the same version already registered from an approved
    marketplace whose checkout is exactly `expected_commit` changes nothing
    (Codex keeps hook trust by content anyway). Anything else is replaced.
    """
    installed, configured = codex_state()
    source = (configured or {}).get("marketplaceSource") or {}
    if configured is not None and approved_slug(source.get("source")) is None:
        raise SetupError("The GrowthX marketplace name points to a different source")
    root: Path | None = None
    if (installed is not None and installed.get("version") == version and configured is not None
            and expected_commit is not None and marketplace_checkout_commit(configured) == expected_commit):
        # `codex plugin list` does not report the cache path, so derive it.
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
        reported = installed.get("installedPath")
        candidate = Path(reported) if isinstance(reported, str) and Path(reported).is_absolute() \
            else codex_home / "plugins" / "cache" / MARKETPLACE / "builder-pulse" / version
        candidate = candidate.resolve(strict=False)
        if (candidate / "scripts" / "builder_pulse.py").is_file() \
                and read_object(candidate / ".codex-plugin" / "plugin.json").get("version") == version:
            root = candidate
    if root is None:
        if installed is not None:
            run_command(["codex", "plugin", "remove", PLUGIN, "--json"])
        if configured is not None:
            run_command(["codex", "plugin", "marketplace", "remove", MARKETPLACE, "--json"])
        run_command(["codex", "plugin", "marketplace", "add", REPOSITORY_SLUG, "--ref", ref, "--json"])
        added = run_command(["codex", "plugin", "add", PLUGIN, "--json"], expect_json=True)
        reported = added.get("installedPath") if isinstance(added, dict) else None
        if not isinstance(reported, str) or not Path(reported).is_absolute():
            raise SetupError("Codex did not report where it installed Builder Pulse")
        root = Path(reported).resolve(strict=False)
    manifest = read_object(root / ".codex-plugin" / "plugin.json") if root.is_dir() else {}
    if manifest.get("version") != version or not (root / "scripts" / "builder_pulse.py").is_file():
        raise SetupError(f"Codex installed an unexpected Builder Pulse version; expected {version}")
    LOG.write("codex package installed", ref=ref, version=version)
    return root / "scripts" / "builder_pulse.py"


def install_shared_runtime(source_root: Path) -> Path:
    """Copy this release's runtime under the shared data dir for Claude Code."""
    target = data_dir() / "runtime" / TARGET_VERSION
    files = {"scripts/builder_pulse.py": source_root / "scripts" / "builder_pulse.py",
             "config/defaults.json": source_root / "config" / "defaults.json"}
    if all((target / rel).is_file() and (target / rel).read_bytes() == src.read_bytes() for rel, src in files.items()):
        return target / "scripts" / "builder_pulse.py"
    temporary = target.parent / f".{TARGET_VERSION}-installing"
    shutil.rmtree(temporary, ignore_errors=True)
    try:
        for rel, src in files.items():
            (temporary / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, temporary / rel)
        write_object(temporary / ".codex-plugin" / "plugin.json", {"name": "builder-pulse-runtime", "version": TARGET_VERSION})
        shutil.rmtree(target, ignore_errors=True)
        os.replace(temporary, target)
    except OSError as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise SetupError("The shared Builder Pulse runtime could not be installed") from exc
    return target / "scripts" / "builder_pulse.py"


def claude_plugin_id() -> str:
    return f"builder-pulse-claude-{'windows' if os.name == 'nt' else 'posix'}@{CLAUDE_MARKETPLACE}"


def claude_builders() -> list[dict[str, Any]]:
    entries = run_command(["claude", "plugin", "list", "--json"], expect_json=True)
    if not isinstance(entries, list):
        raise SetupError("Claude Code returned an invalid plugin list")
    return [e for e in entries if isinstance(e, dict) and str(e.get("id", "")).startswith("builder-pulse-claude-")]


def install_claude(previous: list[dict[str, Any]]) -> None:
    """Add the release-scoped marketplace and install or update the plugin."""
    markets = run_command(["claude", "plugin", "marketplace", "list", "--json"], expect_json=True)
    match = [m for m in (markets if isinstance(markets, list) else []) if isinstance(m, dict) and m.get("name") == CLAUDE_MARKETPLACE]
    plugin_id = claude_plugin_id()
    verb = "update" if any(e.get("id") == plugin_id for e in previous) else "install"
    if match and (match[0].get("source") != "github" or match[0].get("repo") != REPOSITORY_SLUG):
        raise SetupError("The Claude Code GrowthX marketplace name points to a different source")
    if match and str(match[0].get("ref") or TARGET_RELEASE) != TARGET_RELEASE:
        # Right repository, wrong pin: re-pin. Claude drops the plugins that
        # came from a removed marketplace, so the package is installed afresh.
        run_command(["claude", "plugin", "marketplace", "remove", CLAUDE_MARKETPLACE, "--scope", "user"])
        match, verb = [], "install"
    if not match:
        run_command(["claude", "plugin", "marketplace", "add", f"{REPOSITORY_SLUG}@{TARGET_RELEASE}", "--scope", "user"])
    run_command(["claude", "plugin", verb, plugin_id, "--scope", "user", "--yes"])
    entries = [e for e in claude_builders() if e.get("id") == plugin_id]
    root = Path(str(entries[0].get("installPath", ""))).expanduser() if len(entries) == 1 else Path()
    manifest = read_object(root / ".claude-plugin" / "plugin.json") if root.is_dir() else {}
    if (len(entries) != 1 or entries[0].get("enabled") is not True or entries[0].get("version") != TARGET_VERSION
            or manifest.get("version") != TARGET_VERSION or manifest.get("name") != plugin_id.partition("@")[0]):
        raise SetupError("Claude Code did not install the expected Builder Pulse version")
    LOG.write("claude package installed", version=TARGET_VERSION)


def remove_old_claude(previous: list[dict[str, Any]]) -> None:
    for entry in previous:
        if entry.get("id") != claude_plugin_id():
            with contextlib.suppress(SetupError):
                run_command(["claude", "plugin", "uninstall", str(entry["id"]), "--scope", "user", "--keep-data", "--yes"])


def activate(cli_path: Path, agent: str) -> dict[str, Any]:
    """Run the runtime's activate; a pending hook review is a result, not an error."""
    command = [sys.executable, "-I", "-S", str(cli_path), "activate", "--agent", agent]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, env=tool_environment())
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError("Builder Pulse activation could not start") from exc
    try:
        result = json.loads(completed.stdout.strip() or "null")
    except json.JSONDecodeError:
        result = None
    result = result if isinstance(result, dict) else {}
    LOG.write("activation finished", agentPlatform=agent, returncode=completed.returncode,
              result=result or None, stderr=LOG.bounded(completed.stderr))
    if completed.returncode == 0 or result.get("reviewRequired") is True:
        return result
    reason = "; ".join(f"{k}={result[k]}" for k in ("hookStatus", "hookCount", "stage") if result.get(k) not in (None, ""))
    if isinstance(result.get("detail"), str) and result["detail"]:
        reason = f"{reason}; {result['detail']}" if reason else result["detail"]
    detail = LOG.redact(completed.stderr.strip())
    raise SetupError(f"Builder Pulse activation failed for {agent}" + (f" ({reason})" if reason else "") + (f": {detail}" if detail else ""))


# -------------------------------------------------------------- enrollment


def is_builder_pulse_checkout(root: Path) -> bool:
    for candidate in (root, *root.parents):
        if (candidate / "scripts" / "setup_builder_pulse.py").is_file():
            return True
        with contextlib.suppress(SetupError):
            if read_object(candidate / ".codex-plugin" / "plugin.json").get("name") == "builder-pulse":
                return True
    return False


def temporary_roots() -> tuple[Path, ...]:
    candidates = [Path(tempfile.gettempdir()), Path("/tmp"), Path("/private/tmp"), Path("/var/folders"), Path("/private/var/folders")]
    resolved = {c.resolve(strict=False) for c in candidates}
    return tuple(r for r in resolved if not is_filesystem_root(r))


def enrollment_refusal(root: Path) -> str | None:
    home = Path.home().resolve(strict=False)
    if root == home or root in home.parents or is_filesystem_root(root):
        return "The confirmed folder must be a project folder, not the home, one of its parents, or the filesystem root"
    for resolved in temporary_roots():
        if root == resolved or resolved in root.parents:
            return f"{root} is a temporary folder, not your project; run the installer again from inside the project folder Builder Pulse should monitor"
    if is_builder_pulse_checkout(root):
        return f"{root} is the Builder Pulse installer folder, not your project"
    return None


def validated_project(project_root: str, project_label: str) -> tuple[Path, str] | None:
    if not project_root.strip() and not project_label.strip():
        return None
    root = Path(project_root).expanduser().resolve(strict=False)
    label = project_label.strip()
    if not project_root.strip() or not root.is_dir():
        raise SetupError("The confirmed Builder Pulse project folder does not exist")
    if not label or len(label) > 160 or any(ord(c) < 32 for c in label):
        raise SetupError("The confirmed Builder Pulse project name is invalid")
    refusal = enrollment_refusal(root)
    if refusal:
        raise SetupError(refusal)
    return root, label


def hook_review_message(agent: str, cli_path: Path, enrolled: Path | None) -> str:
    folder = str(enrolled) if enrolled else "an enrolled project folder"
    python = "py -3" if os.name == "nt" else "python3"
    return ("Builder Pulse is installed but Codex has not approved its hooks yet. One-time step: "
            f"start Codex inside {folder}; it warns that hooks need review. Run /hooks, select the "
            "builder-pulse hooks and trust them (Codex desktop app: type /hooks in the composer). "
            "Telemetry starts as soon as they are trusted - no rerun of this installer is needed. "
            f"To confirm later: {python} {cli_path} activate --agent {agent}")


# ------------------------------------------------------------------- setup


class Outcome(NamedTuple):
    cli: Path
    enrolled: Path | None
    review_required: tuple[str, ...]


def setup(invite_code: str, endpoint: str, project_root: str, project_label: str, *, repair: bool) -> Outcome:
    if sys.version_info < (3, 11):
        raise SetupError("Builder Pulse requires Python 3.11 or newer")
    if shutil.which("git") is None:
        raise SetupError("Builder Pulse requires git")
    codex, claude = codex_executable(), shutil.which("claude")
    if not codex and not claude:
        raise SetupError("Builder Pulse requires Codex, Claude Code, or both")
    if repair and invite_code:
        raise SetupError("Existing-claim repair must not use a new invite code")
    if not repair and not 16 <= len(invite_code) <= 256:
        raise SetupError("The Builder Pulse invite code is invalid")
    project = validated_project(project_root, project_label)
    if not repair and project is None:
        raise SetupError("A member-confirmed Builder Pulse project folder is required")
    LOG.write("setup started", mode="repair" if repair else "setup", release=TARGET_RELEASE,
              codex=bool(codex), codexSource="desktop app" if codex and not shutil.which("codex") else "PATH",
              claude=bool(claude), python=sys.version.split()[0], platform=sys.platform)

    commit = verify_release(TARGET_RELEASE)
    source_root = verify_installer_checkout(commit)
    shared, legacy = data_dir(), legacy_data_dir()
    LOG.open(shared)
    previous, _ = codex_state() if codex else (None, None)
    previous_version = str(previous.get("version") or "").removeprefix("v") if previous else ""
    previous_claude = claude_builders() if claude else []
    migrate_legacy_data(shared, legacy)
    identity = current_identity(shared)
    LOG.mask(identity.get("installationToken"))
    if repair:
        preserved = require_claimed(identity)
        LOG.write("identity located", installationId=preserved["installationId"])

    # Nothing may be captured while packages are swapped: local off + server pause.
    server_paused = False
    if identity.get("installationToken"):
        server_call(identity, "privacy-pause", previous_version or TARGET_VERSION, "paused")
        server_paused = True
    pause_local(shared, identity)
    LOG.write("capture paused", server=server_paused)

    codex_replaced = False
    try:
        shared_cli = install_shared_runtime(source_root)
        if claude:
            install_claude(previous_claude)
        cli_path = shared_cli
        if codex:
            codex_replaced = previous is not None and previous_version != TARGET_VERSION
            cli_path = install_codex(TARGET_RELEASE, TARGET_VERSION, commit)
        cli(cli_path, "config", "set", "enabled", "false")
        restore_identity(shared, identity)
        if repair:
            status = cli(cli_path, "status", "--json", expect_json=True)
            current = status.get("identity") if isinstance(status, dict) else {}
            if not isinstance(current, dict) or current.get("claimed") is not True or any(
                    current.get(key) != preserved[key] for key in preserved):
                raise SetupError("The Builder Pulse identity changed during repair")
        else:
            env = tool_environment()
            env["BUILDER_PULSE_INVITE_CODE"] = invite_code
            cli(cli_path, "claim", "--endpoint", endpoint, env=env)
        if project is not None:
            cli(cli_path, "work", "enroll", "--root", str(project[0]), "--project", project[1])
        identity = current_identity(shared)
        LOG.mask(identity.get("installationToken"))
        server_call(identity, "privacy-resume", TARGET_VERSION, "resumed")
        server_paused = False
        cli(cli_path, "config", "set", "enabled", "true")
        LOG.write("capture resumed")
        review: list[str] = []
        for agent, run in (("codex", codex), ("claude_code", claude)):
            if not run:
                continue
            result = activate(cli_path, agent)
            if result.get("reviewRequired") is True:
                review.append(agent)
            elif not (result.get("activationReady") is True and result.get("serverVerified") is True):
                raise SetupError(f"Builder Pulse activation was not verified for {agent}")
        cli(cli_path, "flush")
        if claude:
            remove_old_claude(previous_claude)
        LOG.write("setup verified", reviewRequired=review)
        return Outcome(cli_path, project[0] if project else None, tuple(review))
    except BaseException as error:
        # Fail closed: capture stays off locally and paused on the server; the
        # previous Codex tag goes back so already-trusted hooks keep working.
        problems: list[str] = []
        try:
            pause_local(shared, current_identity(shared))
        except BaseException as local_error:  # noqa: BLE001
            problems.append(f"local capture could not be paused again: {local_error}")
        if identity.get("installationToken") and not server_paused:
            try:
                server_call(identity, "privacy-pause", TARGET_VERSION, "paused")
            except BaseException as pause_error:  # noqa: BLE001
                problems.append(f"server pause status unknown: {pause_error}")
        if codex_replaced and previous_version:
            try:
                install_codex(f"v{previous_version}", previous_version)
            except BaseException as restore_error:  # noqa: BLE001
                problems.append(f"previous Codex package v{previous_version} not restored: {restore_error}")
        LOG.write("setup failed", error=str(error), rollbackProblems=problems)
        if problems and not isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise SetupError(f"{error}; {'; '.join(problems)}") from error
        raise


# -------------------------------------------------------------------- main


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError as exc:
        raise SetupError("The terminal closed before the answer was given; run the installer again in an interactive terminal") from exc


def prompt_for_project(current: Path) -> tuple[str, str]:
    print("Builder Pulse project choices (shown only in this terminal):", file=sys.stderr)
    default: Path | None = None if is_builder_pulse_checkout(current) else current
    if default is None:
        print("The current folder is the Builder Pulse installer clone, not your project.", file=sys.stderr)
    else:
        print(f"- Current folder: {current}", file=sys.stderr)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        found = subprocess.run(["git", "-C", str(current), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=2, check=False)
        top = Path(found.stdout.strip()).resolve(strict=False) if found.returncode == 0 and found.stdout.strip() else None
        if top and top != current and not is_builder_pulse_checkout(top):
            print(f"- Nearest Git repository root: {top}", file=sys.stderr)
    root = ""
    while not root:
        root = ask(f"Which exact project folder should Builder Pulse monitor? [{default}]: " if default else
                   "Which exact project folder should Builder Pulse monitor? (type the full path): ") or (str(default) if default else "")
        if not root:
            print("A project folder path is required.", file=sys.stderr)
    return root, ask("Project name GrowthX should display: ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--code")
    parser.add_argument("--project-root")
    parser.add_argument("--project-label")
    parser.add_argument("--reuse-existing-claim", action="store_true", help="Repair an already-claimed installation without a new invite")
    args = parser.parse_args()
    print(SETUP_DISCLOSURE, file=sys.stderr)
    invite = args.code or os.environ.get("BUILDER_PULSE_INVITE_CODE") or ""
    root, label = args.project_root or "", args.project_label or ""
    interactive = sys.stdin.isatty()
    try:
        if not args.reuse_existing_claim and not invite and interactive:
            try:
                invite = getpass.getpass("Builder Pulse invite code: ")
            except EOFError as exc:
                raise SetupError("The terminal closed before the invite code was given; run the installer again in an interactive terminal") from exc
        LOG.mask(invite)
        if interactive and not root:
            cwd = Path.cwd().resolve(strict=False)
            if not args.reuse_existing_claim or ask("Existing enrollments are kept. Enroll an additional project folder now? [y/N]: ").lower() in {"y", "yes"}:
                root, label = prompt_for_project(cwd)
        elif interactive and root and not label:
            label = ask("Project name GrowthX should display: ")
        outcome = setup(invite, args.endpoint, root, label, repair=args.reuse_existing_claim)
    except (Exception, KeyboardInterrupt) as exc:  # every failure ends with a Details line
        message = "interrupted" if isinstance(exc, KeyboardInterrupt) else (str(exc) or exc.__class__.__name__)
        LOG.write("setup stopped", error=message)
        print(f"Builder Pulse setup stopped: {LOG.redact(message)}", file=sys.stderr)
        if LOG.open(data_dir()):
            print(f"Details: {LOG.path}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    log_path = LOG.open(data_dir())
    with contextlib.suppress(SetupError):
        print("Enrolled project folders:\n" + str(cli(outcome.cli, "work", "list")).strip(), file=sys.stderr)
    if outcome.review_required:
        for agent in outcome.review_required:
            print(hook_review_message(agent, outcome.cli, outcome.enrolled), file=sys.stderr)
        print(f"Details: {log_path}", file=sys.stderr)
        LOG.write("setup finished; hook review pending")
        return HOOK_REVIEW_EXIT_CODE
    print("Builder Pulse is installed for every supported agent found on this computer. Only project folders "
          "you explicitly confirmed are enrolled; " + ("this confirmed project was added without removing prior "
          "confirmed projects. " if outcome.enrolled else "prior confirmed projects were kept unchanged. ")
          + "Exit all running Claude Code and Codex sessions, start a fresh session in each agent you use, "
          "then send one normal prompt in each to verify separate server receipts.")
    print(f"Details: {log_path}", file=sys.stderr)
    LOG.write("setup finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
