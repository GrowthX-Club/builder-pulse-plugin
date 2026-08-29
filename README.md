# Builder Pulse

Builder Pulse connects a builder's claimed installation to GrowthX, reports
coarse progress, and sends submitted Codex prompts so GrowthX can provide
learning feedback.

In commands below, `<python>` means `python3` on macOS/Linux and `py -3` on
Windows.

## Consent and data boundary

Installing and claiming Builder Pulse enables the data collection described
below. The claim command displays this exact disclosure before making the
request:

> Builder Pulse connects you with GrowthX so that we can track your progress and provide you learning feedback.

Builder Pulse sends only:

- a random installation ID and hashed Codex session key;
- the configured product/project ID (or a sanitized folder basename fallback);
- an optional feature ID and feature label explicitly set by the builder;
- `building`, `testing`, `blocked`, `ready`, or `idle`;
- event time, an optional active interval capped at 15 minutes, and plugin version.
- when Codex exposes it, an optional cumulative per-session numeric token snapshot:
  input, cached input, output, reasoning output, and total tokens.
- on each primary `UserPromptSubmit`, the submitted prompt text (after
  high-confidence secret redaction and a 64 KiB UTF-8 bound), a stable prompt
  ID, and `redacted` / `truncated` flags.

Prompt capture is **on** for claim schema v2. The redactor targets private-key
blocks, Authorization/Bearer values, and common API-token formats. It is a
high-confidence safety layer, not a guarantee that every possible secret will
be recognized. The bounded redacted prompt is temporarily persisted in a
separate local prompt outbox and forwarded to GrowthX.

Separate command, path, source, patch, tool input/output, transcript, assistant
response, environment, invite-code, and endpoint-response fields from hooks are
never added to prompt or lifecycle events. A submitted prompt may itself mention
such content; that user-authored message remains part of `promptText`. A shell
command hook may be inspected in process just long enough to recognize testing
or a review artifact, then discarded. Subagent and fork prompts are not captured.
Prompt capture fails closed unless the hook supplies a trusted Codex transcript
path whose first bounded record is valid `session_meta` with no subagent/fork
source or parent markers.

For an event that is already due, the plugin may best-effort inspect a bounded
tail of the local Codex session file for the latest numeric `token_count`
record. Only five validated nonnegative safe integers can leave that process.
The transcript path and all other session content are discarded, never added to
plugin state, the outbox, quarantine, or the wire payload. Missing, inaccessible,
or malformed counters are ignored with no effect on Codex.

Subagent and fork snapshots are always omitted to prevent replayed parent
usage from being counted twice. The plugin recognizes only explicit child-run
hook fields, exact child transcript path segments, and the structural
`session_meta.payload.source` marker; none of that metadata is retained.

Builder Pulse reports approximate **Codex-active time**, not working hours.
Overlapping sessions and devices are deduplicated by the server.

## One-time claim (no login)

Each builder receives a one-time invite code. The plugin creates a stable random
`installationId` and a 64-hex installation token, persists the token as pending,
then sends both to `/v1/claim`. A lost response can therefore be retried with the
same identity and token. After success the token is stored only in the plugin
data directory in `identity.json` with mode `0600` and is never printed in full.
The claimed HTTPS endpoint is bound to that identity and cannot be changed by a
later claim or configuration override. Plain HTTP is accepted only on loopback
for local development.

```bash
<python> <plugin-root>/scripts/builder_pulse.py claim --endpoint https://precious-ant-429.convex.site
```

The CLI asks for the invite code without echoing it. For managed setup, use
`BUILDER_PULSE_ENDPOINT` and `BUILDER_PULSE_INVITE_CODE`; the invite code is
used for that claim request only and is never stored. `--code` is also
supported, but interactive entry avoids leaving the code in shell history.

Claim request:

```json
{
  "schemaVersion": 2,
  "inviteCode": "one-time-code",
  "installationId": "stable-uuid",
  "installationToken": "64-lowercase-hex-characters",
  "pluginVersion": "0.4.2"
}
```

The response supplies the internal `builderId`, the stable GrowthX `memberId`,
`name`, `defaultProject`,
`heartbeatMinutes: 15`, and `promptCapture: "on"`; it does not return the
installation token. A
non-null default project is used only when the builder has not configured one.

## Product and feature context

Use a concise, non-sensitive label. Feature labels are limited to 120
characters and never inferred from prompt text. The configured project and
feature context is attached to both lifecycle and prompt events.

```bash
<python> <plugin-root>/scripts/builder_pulse.py work set --project growthx-community --feature "Member search filters"

<python> <plugin-root>/scripts/builder_pulse.py work show
<python> <plugin-root>/scripts/builder_pulse.py work clear-feature
```

An optional `--feature-id member-search-filters` preserves a stable ID when the
display label changes. Without it, Builder Pulse derives a sanitized ID. Work
context is scoped to the current repository root, so concurrent repositories do
not inherit each other's product or feature. Use `--root /path/to/repository`
with `work set`, `work show`, or `work clear-feature` to target another checkout.
`config set project_id ...` remains an explicit global fallback only.

## Delivery behavior

Hooks run asynchronously except `UserPromptSubmit` and `SessionEnd`, which
Codex runs synchronously. Synchronous prompt capture ensures a short Codex task
cannot exit before its prompt has been queued and delivery has been attempted.
Only essential lifecycle hooks and matched post-tool events launch the runtime.
A lifecycle telemetry event is created only when
state changes or a 15-minute heartbeat is due. Observed hook continuity can
create an active interval; a gap over 15 minutes never receives active credit.
`SessionEnd` changes the state to `idle`. Reading token totals does not create
another lifecycle event or network request.

Claimed installations append the exact minimal event to a bounded local
`outbox.jsonl`, then make a best-effort `POST /v1/telemetry` with the
installation token as a bearer header. Failed events retain the same `eventId`
for server-side deduplication and retry on a later state change/heartbeat. Queue
updates are file-locked and atomic; normal enqueue is append-only, with bounded
compaction at 500 events. Delivery failure never interrupts Codex.

Separately, every eligible primary `UserPromptSubmit` creates the exact bounded
prompt event below in `prompt-outbox.jsonl` and attempts a best-effort
`POST /v1/prompts` with the same bearer token:

```json
{
  "schemaVersion": 1,
  "promptId": "uuid",
  "installationId": "uuid",
  "sessionKey": "short-hash",
  "projectId": "growthx-community",
  "featureId": "member-search-filters",
  "featureLabel": "Member search filters",
  "promptText": "Help me improve the member search experience.",
  "occurredAt": 1787721000000,
  "pluginVersion": "0.4.2",
  "redacted": false,
  "truncated": false
}
```

`featureId` and `featureLabel` are omitted when unavailable. The prompt outbox
uses the same file lock, maximum queue length, and flush batch size as the
lifecycle outbox. Its first creation is `0600` before any prompt bytes are
written. Network failures keep the same `promptId` for an idempotent later retry,
with a local 60-day maximum retention period. Older captures are deleted.
Permanently rejected prompt events are discarded rather than copied to lifecycle
quarantine. A prompt endpoint `401` or `403` disables local prompt capture and
purges the prompt outbox without logging its contents. No prompt request is
created by tool, assistant-response, transcript, subagent, or fork hooks.

When a cumulative token snapshot is available, the wire payload is schema v2:

```json
{
  "schemaVersion": 2,
  "eventId": "uuid",
  "installationId": "uuid",
  "sessionKey": "short-hash",
  "projectId": "growthx-community",
  "featureId": "member-search-filters",
  "featureLabel": "Member search filters",
  "state": "building",
  "occurredAt": 1787721000000,
  "activeFrom": 1787720100000,
  "tokenUsage": {
    "inputTokens": 1200,
    "cachedInputTokens": 300,
    "outputTokens": 240,
    "reasoningOutputTokens": 80,
    "totalTokens": 1440
  },
  "pluginVersion": "0.4.2"
}
```

`tokenUsage` is cumulative for the hashed Codex session, not a per-prompt
measurement. Each of its five fields is a required nonnegative JSON-safe integer.
The legacy local `cache_write_input_tokens` counter, when present, is ignored
and never transported.
`featureId`, `featureLabel`, and `activeFrom` are omitted when unavailable. If
no valid token snapshot is available, `tokenUsage` is omitted and the exact
existing schema v1 payload is preserved.

## Status and configuration

```bash
<python> <plugin-root>/scripts/builder_pulse.py status
<python> <plugin-root>/scripts/builder_pulse.py status --json
<python> <plugin-root>/scripts/builder_pulse.py config show
<python> <plugin-root>/scripts/builder_pulse.py mark blocked
<python> <plugin-root>/scripts/builder_pulse.py flush
```

Status reports lifecycle and prompt queue counts separately. `flush` retries
both queues.

The hook runtime writes to Codex's `PLUGIN_DATA`. Interactive commands launched
from an installed marketplace cache derive that same directory automatically.
Set `BUILDER_PULSE_DATA_DIR` or pass `--data-dir` only for explicit local/testing access. Supported
environment context overrides are `BUILDER_PULSE_ENDPOINT`,
`BUILDER_PULSE_PROJECT_ID`, `BUILDER_PULSE_FEATURE_ID`, and
`BUILDER_PULSE_FEATURE_LABEL`.

## Prepared installation and lifecycle contract

Builder Pulse ships from the GrowthX Builder Tools marketplace manifest in this
repository. Python 3.11 or newer is the only host prerequisite; verify it with
`python3 --version` on macOS/Linux or `py -3 --version` on Windows before
installation. The runtime uses only Python's standard library. Install the
immutable v0.4.2 release with:

```bash
codex plugin marketplace add udayanwalvekar/builder-pulse-plugin --ref v0.4.2
codex plugin add builder-pulse@growthx-builder-tools
```

The admin-provided claim command must use the installed plugin root; this build
defaults to `https://precious-ant-429.convex.site`. To upgrade after an announced
release, remove the configured marketplace, re-add it with the announced
immutable tag, then run the same `codex plugin add` command and start a new Codex
task. To pause
without removing local identity, run `config set enabled false`. To uninstall,
run `codex plugin remove builder-pulse`; uninstalling is not token revocation.

For a replacement or second device, issue a new one-time invite for the same
GrowthX member ID, claim on the new installation, and revoke the old installation
in the admin dashboard. Never copy `identity.json` between devices. A lost or
compromised device requires server-side revocation before replacement.

## Verification

```bash
<python> -m unittest discover -s <plugin-root>/tests -v
<python> <skill-creator-root>/scripts/quick_validate.py <plugin-root>/skills/builder-pulse
<python> <plugin-creator-root>/scripts/validate_plugin.py <plugin-root>
```

Operational settings live in the plugin data directory and survive upgrades.
Product logic changes require a version bump, validation, release to the
approved distribution source, and plugin reinstall. None of those rollout steps
are implied merely by changing this source directory.
