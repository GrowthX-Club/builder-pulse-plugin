# Builder Pulse

Builder Pulse connects a builder's claimed installation to GrowthX, reports
coarse progress, and sends submitted Codex prompts so GrowthX can provide
learning feedback.

In commands below, `<python>` means `python3` on macOS/Linux and `py -3` on
Windows.

## Stable setup and update

The permanent setup entrypoint is [SETUP.md](SETUP.md). Builder-facing prompts
pin an immutable release and link to that release's guide instead of duplicating
installation logic.

## Consent and data boundary

The plugin is installed machine-wide, but capture is fail-closed outside the
project folders a member explicitly enrolls. Installing and claiming Builder
Pulse enables the data collection described below only for those folders. The
claim command displays this exact disclosure before making the request:

> Builder Pulse is installed machine-wide, but it sends data only from project folders you explicitly enroll. GrowthX links telemetry to your claimed GrowthX member record. For each enrolled project, it receives a stable installation ID, a one-way hashed session ID, the display name you confirm and a sanitized project ID, any feature name and ID you explicitly set, coarse work state and event/activity timestamps, plugin version, optional cumulative token counts, and each primary prompt you submit after secret redaction and a 64 KiB limit. GrowthX's authenticated Builder Pulse admins can view this data for learning feedback. Raw lifecycle events and activity buckets are retained for 30 days; submitted prompts and their feedback are retained for 60 days; the installation/member link, latest status, and compacted session, daily, and all-time token aggregates remain until GrowthX removes them. It does not send folder paths, files, patches, commands, tool input or output, assistant replies, transcripts, or environment variables.

Builder Pulse sends only:

- a random installation ID and hashed Codex session key;
- a stable sanitized project ID and the member-confirmed project display name;
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

Folder paths are used locally only to match a hook's working directory to
the one-way keyed enrollment record; folder paths are never persisted in
that record or transmitted. An enrollment covers the confirmed folder and its
descendants, including one package inside a monorepo and projects without Git.
The home directory, its parent directories, and filesystem root cannot be
enrolled. A hook without a working directory, or
from a folder outside every explicit enrollment, sends and queues nothing and
is rejected before prompt text or transcript metadata is inspected.

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
  "pluginVersion": "0.4.6"
}
```

The response supplies the internal `builderId`, the stable GrowthX `memberId`,
`name`, `defaultProject`,
`heartbeatMinutes: 15`, and `promptCapture: "on"`; it does not return the
installation token. `defaultProject` is legacy roster/program metadata and is
never adopted as telemetry project context. Project identity always comes from
a member-confirmed local enrollment.

## Project enrollment and feature context

Before enrollment, show the disclosure above, identify only the current working
directory and, if different, its nearest repository root, and ask the member which exact folder or
folders to monitor and what display name to use for each. Do not scan broadly or
infer a project name from the folder, prompts, or roster metadata.

Use a concise, non-sensitive member-confirmed project name. Enrollment derives a
stable sanitized project ID unless `--project-id` is supplied. Feature labels
are limited to 120 characters and never inferred from prompt text. The project
and optional feature context is attached to both lifecycle and prompt events.

```bash
<python> <plugin-root>/scripts/builder_pulse.py work enroll --root /confirmed/project-folder --project "GrowthX Community"
<python> <plugin-root>/scripts/builder_pulse.py work set --root /confirmed/project-folder --feature "Member search filters"

<python> <plugin-root>/scripts/builder_pulse.py work show
<python> <plugin-root>/scripts/builder_pulse.py work list
<python> <plugin-root>/scripts/builder_pulse.py work clear-feature
<python> <plugin-root>/scripts/builder_pulse.py work unenroll --root /confirmed/project-folder
```

An optional `--feature-id member-search-filters` preserves a stable ID when the
display label changes. Without it, Builder Pulse derives a sanitized ID. Work
context and lifecycle heartbeat are scoped to the exact enrolled folder, so a
monorepo package does not implicitly enroll its siblings and concurrent projects
do not inherit or suppress each other's project or feature state.
On upgrade, an older context record remains inactive until that exact folder is
explicitly enrolled with a display name. Its legacy feature label is cleared on
first enrollment instead of being silently attributed to the confirmed project.
`work unenroll` removes that mapping and clears only pending local telemetry,
prompts, and state for the removed project; pending data for other enrolled
projects remains intact. There is no global project fallback.

## Delivery behavior

Hooks run asynchronously except `SessionEnd`, which Codex always runs
synchronously. `UserPromptSubmit` records and attempts the current prompt in a
background hook so an interpreter, path, or network failure can never block a
builder's prompt. The current prompt is attempted first under a 750 ms network
timeout; older prompt and lifecycle backlog is left for later asynchronous
hooks so an outage cannot stall every submitted prompt behind retries.
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
  "projectLabel": "GrowthX Community",
  "projectScope": "explicit",
  "featureId": "member-search-filters",
  "featureLabel": "Member search filters",
  "promptText": "Help me improve the member search experience.",
  "occurredAt": 1787721000000,
  "pluginVersion": "0.4.6",
  "redacted": false,
  "truncated": false
}
```

`projectLabel` and `projectScope` are required for every event the service
accepts. The service rejects unscoped payloads from every plugin version, so
v0.4.5 and older stop reporting until they update and the member explicitly
enrolls a project folder.
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
  "projectLabel": "GrowthX Community",
  "projectScope": "explicit",
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
  "pluginVersion": "0.4.6"
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
<python> <plugin-root>/scripts/builder_pulse.py activate
```

Status reports lifecycle and prompt queue counts separately. `flush` retries
both queues. `activate` reads the local Codex app-server's official
`hooks/list` result and exits successfully only when every Builder Pulse hook
is current, enabled, and trusted or managed and the Builder Pulse service
accepts the claimed installation. That proves activation readiness, not event
delivery. `telemetryReceived: true` means the server has received something at
some point; it can be historical. Current repair proof requires
`telemetryReceivedSincePreviousActivation: true`, a non-null `lastSignalAt`, and
`lastSignalPluginVersion: "0.4.6"`. That proof uses the server receipt time, not
the member computer's event clock. Activation does not create a lifecycle event
or change the builder's work state.

The hook runtime writes to Codex's `PLUGIN_DATA`. Interactive commands launched
from an installed marketplace cache derive that same directory automatically.
Set `BUILDER_PULSE_DATA_DIR` or pass `--data-dir` only for explicit local/testing
access. Supported environment overrides include `BUILDER_PULSE_ENABLED`,
`BUILDER_PULSE_ENDPOINT`, and `BUILDER_PULSE_CLAIM_TIMEOUT_SECONDS`. Project and
feature context has no environment or global fallback; it comes only from the
per-folder enrollment file.

## Prepared installation and lifecycle contract

Builder Pulse ships from the GrowthX Builder Tools marketplace manifest in this
repository. Python 3.11 or newer is the only host prerequisite; verify it with
`python3 --version` on macOS/Linux or `py -3 --version` on Windows before
installation. The runtime uses only Python's standard library. For manual
recovery, install the current immutable v0.4.6 release with:

```bash
codex plugin marketplace add GrowthX-Club/builder-pulse-plugin --ref v0.4.6
codex plugin add builder-pulse@growthx-builder-tools
```

The admin-provided claim command must use the installed plugin root; this build
defaults to `https://precious-ant-429.convex.site`. To upgrade after an announced
release, remove the configured marketplace, re-add it with the announced
immutable tag, then run the same `codex plugin add` command. Exit every running
Codex session before starting a fresh task so no process keeps the previous hook
manifest or version path in memory. To pause
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
