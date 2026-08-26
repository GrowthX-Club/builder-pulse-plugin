# Builder Pulse

Builder Pulse answers four small questions: **who is building, which product,
which feature, and what coarse state are they in?** It is intentionally not a
prompt recorder or employee-monitoring agent.

## Consent and data boundary

Installing and claiming Builder Pulse enables the minimal telemetry described
below. A builder should see this disclosure before using their invite code.

Builder Pulse sends only:

- a random installation ID and hashed Codex session key;
- the configured product/project ID (or a sanitized folder basename fallback);
- an optional feature ID and feature label explicitly set by the builder;
- `building`, `testing`, `blocked`, `ready`, or `idle`;
- event time, an optional active interval capped at 15 minutes, and plugin version.

Raw prompts are **off**. Prompt text, prompt length, commands, full paths,
source code, patches, tool input/output, transcript data, environment variables,
and endpoint response bodies are never persisted, queued, or forwarded. A shell
command may be inspected in process just long enough to recognize testing or a
review artifact, then discarded.

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
python3 <plugin-root>/scripts/builder_pulse.py \
  claim --endpoint https://precious-ant-429.convex.site
```

The CLI asks for the invite code without echoing it. For managed setup, use
`BUILDER_PULSE_ENDPOINT` and `BUILDER_PULSE_INVITE_CODE`; the invite code is
used for that claim request only and is never stored. `--code` is also
supported, but interactive entry avoids leaving the code in shell history.

Claim request:

```json
{
  "schemaVersion": 1,
  "inviteCode": "one-time-code",
  "installationId": "stable-uuid",
  "installationToken": "64-lowercase-hex-characters",
  "pluginVersion": "0.3.0"
}
```

The response supplies `builderId`, `name`, `defaultProject`,
`heartbeatMinutes: 15`, and `promptCapture: "off"`; it does not return the
installation token. A
non-null default project is used only when the builder has not configured one.

## Product and feature context

Use a concise, non-sensitive label. Feature labels are limited to 120
characters and never inferred from prompt text.

```bash
python3 <plugin-root>/scripts/builder_pulse.py work set \
  --project growthx-community \
  --feature "Member search filters"

python3 <plugin-root>/scripts/builder_pulse.py work show
python3 <plugin-root>/scripts/builder_pulse.py work clear-feature
```

An optional `--feature-id member-search-filters` preserves a stable ID when the
display label changes. Without it, Builder Pulse derives a sanitized ID. Work
context is scoped to the current repository root, so concurrent repositories do
not inherit each other's product or feature. Use `--root /path/to/repository`
with `work set`, `work show`, or `work clear-feature` to target another checkout.
`config set project_id ...` remains an explicit global fallback only.

## Delivery behavior

Hooks run asynchronously and reduce rich hook input to the coarse state in
memory. Only essential lifecycle hooks and matched post-tool events launch the
runtime. A telemetry event is created only when state changes or a 15-minute
heartbeat is due. Observed hook continuity can create an active interval; a gap
over 15 minutes never receives active credit. `SessionEnd` changes the state to
`idle`.

Claimed installations append the exact minimal event to a bounded local
`outbox.jsonl`, then make a best-effort `POST /v1/telemetry` with the
installation token as a bearer header. Failed events retain the same `eventId`
for server-side deduplication and retry on a later state change/heartbeat. Queue
updates are file-locked and atomic; normal enqueue is append-only, with bounded
compaction at 500 events. Delivery failure never interrupts Codex.

The wire payload contains only:

```json
{
  "schemaVersion": 1,
  "eventId": "uuid",
  "installationId": "uuid",
  "sessionKey": "short-hash",
  "projectId": "growthx-community",
  "featureId": "member-search-filters",
  "featureLabel": "Member search filters",
  "state": "building",
  "occurredAt": 1787721000000,
  "activeFrom": 1787720100000,
  "pluginVersion": "0.3.0"
}
```

`featureId`, `featureLabel`, and `activeFrom` are omitted when unavailable.

## Status and configuration

```bash
python3 <plugin-root>/scripts/builder_pulse.py status
python3 <plugin-root>/scripts/builder_pulse.py status --json
python3 <plugin-root>/scripts/builder_pulse.py config show
python3 <plugin-root>/scripts/builder_pulse.py mark blocked
python3 <plugin-root>/scripts/builder_pulse.py flush
```

The hook runtime normally writes to Codex's `PLUGIN_DATA`. Set
`BUILDER_PULSE_DATA_DIR` or pass `--data-dir` for explicit CLI access. Supported
environment context overrides are `BUILDER_PULSE_ENDPOINT`,
`BUILDER_PULSE_PROJECT_ID`, `BUILDER_PULSE_FEATURE_ID`, and
`BUILDER_PULSE_FEATURE_LABEL`.

## Prepared installation and lifecycle contract

Builder Pulse has not been published yet. The rollout distribution source is a
placeholder until an approved marketplace is configured:

```bash
codex plugin add builder-pulse@<approved-marketplace>
```

The admin-provided claim command must use the installed plugin root; this build
defaults to `https://precious-ant-429.convex.site`. To upgrade, run the same `codex plugin add` command
after an announced version is available, then start a new Codex task. To pause
without removing local identity, run `config set enabled false`. To uninstall,
run `codex plugin remove builder-pulse`; uninstalling is not token revocation.

For a replacement or second device, issue a new one-time invite for the same
roster email, claim on the new installation, and revoke the old installation
server-side. Never copy `identity.json` between devices. A lost or compromised
device requires server-side revocation before replacement.

## Verification

```bash
python3 -m unittest discover -s <plugin-root>/tests -v
python3 <skill-creator-root>/scripts/quick_validate.py <plugin-root>/skills/builder-pulse
python3 <plugin-creator-root>/scripts/validate_plugin.py <plugin-root>
```

Operational settings live in the plugin data directory and survive upgrades.
Product logic changes require a version bump, validation, release to the
approved distribution source, and plugin reinstall. None of those rollout steps
are implied merely by changing this source directory.
