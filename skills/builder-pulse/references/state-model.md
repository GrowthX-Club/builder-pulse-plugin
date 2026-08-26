# Builder Pulse P0 state and delivery model

## States

| State | Meaning | Signal |
| --- | --- | --- |
| `building` | The builder is actively implementing or working through a request. | Session/prompt start, file edit, general shell or coordination work. |
| `testing` | The builder is running a recognized test, lint, typecheck, check, or build command. | Recognized verification command. |
| `blocked` | Work needs an approval or is explicitly marked blocked. | Permission request or explicit mark. |
| `ready` | A review artifact exists or the builder explicitly marks the feature ready. | Successful recognized PR/MR creation or explicit mark. |
| `idle` | The session ended or local state is stale. | `SessionEnd`; status derives stale idle after 30 minutes. |

Successful tests remain `testing`; they do not automatically prove review
readiness. Session overlap and multiple devices are deduplicated server-side.

## Identity without login

The plugin creates a stable UUID `installationId` and client-generated 64-hex
`installationToken`. It persists the token as pending before `POST /v1/claim`,
so a response-loss retry reuses the same values. The request uses one
`inviteCode` to bind that installation to a roster entry. The response returns
`builderId`, `name`, `defaultProject`, `heartbeatMinutes: 15`, and
`promptCapture: "off"`, but never returns the token.

The installation token is stored in `identity.json` with mode `0600` and sent
only as the bearer header to `/v1/telemetry`. It is never printed in full. A
server default project is adopted only when no project is already configured.
The token is bound to the claimed HTTPS endpoint; HTTP is permitted only for a
loopback development server.

## Exact telemetry contract

`POST /v1/telemetry` sends exactly:

- `schemaVersion: 1`;
- stable UUID `eventId` (unchanged across retries);
- stable UUID `installationId`;
- one-way hashed `sessionKey`;
- sanitized `projectId`;
- optional explicit `featureId` and `featureLabel` (maximum 120 characters);
- `state`;
- epoch-millisecond `occurredAt`;
- optional epoch-millisecond `activeFrom`, capped to 15 minutes before the event;
- `pluginVersion`.

No builder name or ID is needed in telemetry because the bearer token resolves
the claimed roster entry.

## Emission and retry

Hooks may inspect event metadata in memory, but an event is persisted, queued,
or sent only on a state transition or when the 15-minute heartbeat is due.
There is no per-prompt or per-tool event log.

Active intervals require observed hook continuity. Local state tracks the last
observed hook even when no telemetry event is emitted; a gap longer than 15
minutes starts a new window and never credits the preceding inactive period.

Claimed installations first append the minimal event to `outbox.jsonl`, then
attempt delivery. Failures remain queued with their original `eventId`.
Normally the outbox is append-only; file-locked atomic compaction removes
delivered records and keeps the queue bounded. Telemetry failure is swallowed
by the hook process and never breaks Codex.
Only one process holds the nonblocking delivery lease. Non-retryable client
errors are moved to a local minimal-event quarantine so they cannot starve newer
events.

## Privacy contract

Raw prompt capture is off. Builder Pulse must never persist, queue, or forward:

- prompt text or prompt length;
- shell commands;
- tool input or output;
- source, patches, full paths, or transcript paths;
- environment variables, authorization tokens, or invite codes;
- telemetry endpoint response bodies.

A shell command may be inspected in memory only long enough to distinguish
testing or successful review-artifact creation. The product/project defaults to
a sanitized folder basename when not explicitly configured. Feature labels are
explicit and must not be inferred from prompt content.

Project and feature overrides are keyed by a one-way hash of the repository root.
Full repository paths are not persisted. Global configured values are explicit
fallbacks only.
