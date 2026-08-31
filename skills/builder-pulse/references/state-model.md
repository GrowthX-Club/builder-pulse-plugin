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
`inviteCode` and `schemaVersion: 2` to bind that installation to a roster entry
and enable prompt capture. The response returns
`builderId`, stable GrowthX `memberId`, `name`, `defaultProject`, `heartbeatMinutes: 15`, and
`promptCapture: "on"`, but never returns the token. Prompt capture remains off
unless the claimed identity stores that exact server policy.

The installation token is stored in `identity.json` with mode `0600` and sent
only as the bearer header to `/v1/telemetry` and `/v1/prompts`. It is never
printed in full. A
`defaultProject` is legacy roster/program metadata and is never adopted as
telemetry project context. Project identity comes only from an explicit local
folder enrollment.
Older context records without a member-confirmed project label remain inactive.
Their legacy feature fields are cleared when that folder is first explicitly
enrolled, so inferred or stale labels cannot silently become active telemetry.
The token is bound to the claimed HTTPS endpoint; HTTP is permitted only for a
loopback development server.

Raw lifecycle events and activity buckets are retained for 30 days. Submitted
prompts and their feedback are retained for 60 days. The installation/member
link, latest state, and compacted per-session, daily, and all-time token
aggregates remain until GrowthX removes them.

## Data labels

| Data group | GrowthX receives | GrowthX does not receive |
| --- | --- | --- |
| Claimed identity | Member ID, name, email, optional roster/program default, installation ID, claim policy. | Local identity-file path or installation token in response bodies. |
| Project context | Member-confirmed display name, sanitized stable project ID, `projectScope: "explicit"`. | Enrolled folder path or the private HMAC keys used to match it. |
| Optional feature | Explicit feature display name and sanitized stable feature ID. | A feature inferred from prompts, commands, or folder names. |
| Lifecycle | Hashed session ID, coarse state, event/activity timestamps, plugin version, optional five cumulative token counters. | Files, patches, commands, tool I/O, transcripts, assistant replies, paths, or environment variables. |
| Prompt | The primary submitted prompt after local secret redaction and the 64 KiB bound, plus project/feature identifiers and redacted/truncated flags. | Tool-hook text, subagent/fork prompts, assistant replies, or transcript content. |

The roster/program `defaultProject` is identity metadata only. It is never used
as event project context. Legacy or incomplete events without both a
member-confirmed `projectLabel` and `projectScope: "explicit"` are rejected,
not relabeled.

## Exact telemetry contract

Without a valid Codex token snapshot, `POST /v1/telemetry` preserves the exact
schema v1 payload:

- `schemaVersion: 1`;
- stable UUID `eventId` (unchanged across retries);
- stable UUID `installationId`;
- one-way hashed `sessionKey`;
- sanitized `projectId`;
- member-confirmed `projectLabel` (maximum 160 characters);
- `projectScope: "explicit"`;
- optional explicit `featureId` and `featureLabel` (maximum 120 characters);
- `state`;
- epoch-millisecond `occurredAt`;
- optional epoch-millisecond `activeFrom`, capped to 15 minutes before the event;
- `pluginVersion`.

When a valid cumulative Codex token snapshot is available on that same emitted
event, the payload uses `schemaVersion: 2` and adds exactly one object:

```json
{
  "tokenUsage": {
    "inputTokens": 1200,
    "cachedInputTokens": 300,
    "outputTokens": 240,
    "reasoningOutputTokens": 80,
    "totalTokens": 1440
  }
}
```

All five fields are required nonnegative JSON-safe integers and represent the
latest cumulative `total_token_usage` for the hashed Codex session. They are
not per-prompt measurements. If the local record is missing, inaccessible, or
malformed, `tokenUsage` is omitted and the payload remains schema v1.
The legacy local `cache_write_input_tokens` field, when present, is allowlisted
for parsing but ignored and never transported.

No builder name or ID is needed in telemetry because the bearer token resolves
the claimed roster entry.

## Emission and retry

Hooks may inspect event metadata in memory, but a lifecycle event is persisted,
queued, or sent only on a state transition or when the 15-minute heartbeat is
due. Prompt events follow the separate exact contract below. There is no
per-tool event log.

Only after an existing state transition, heartbeat, or session-end event is due,
the plugin may best-effort inspect a bounded tail of an absolute regular Codex
session file resolving beneath the active `sessions` or `archived_sessions`
root. It parses only the latest numeric `event_msg` / `token_count` /
`total_token_usage` record. This local read never creates another event or
network request and any failure is ignored.

Subagent and fork state events never include `tokenUsage`, preventing a child
transcript from replaying a parent session's cumulative counters. Detection is
limited to explicit child-run hook fields, exact child transcript path segments,
and the structural `session_meta.payload.source` marker. These paths and
metadata are inspected only in memory and never retained.

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

## Exact prompt contract

On a claimed primary `UserPromptSubmit`, `POST /v1/prompts` sends exactly:

- `schemaVersion: 1`;
- stable UUID `promptId` (unchanged across retries);
- stable UUID `installationId`;
- one-way hashed `sessionKey`;
- sanitized `projectId`;
- member-confirmed `projectLabel` (maximum 160 characters);
- `projectScope: "explicit"`;
- optional explicit `featureId` and `featureLabel`;
- UTF-8-bounded `promptText` (at most 65,536 bytes);
- epoch-millisecond `occurredAt`;
- `pluginVersion`;
- boolean `redacted` and `truncated` flags.

Before bounding, prompt text is scanned for high-confidence private-key blocks,
Authorization/Bearer values, common API-token formats, and labeled Builder
Pulse invite-code forms used by older setup prompts. Recognized values are
replaced. This is deliberately not described as complete secret detection.
The event is stored in a separate bounded `prompt-outbox.jsonl`, created with
mode `0600` before any prompt bytes are written. Network failures retain the
same `promptId` for idempotent retry for at most 60 days; older local captures
are deleted. Permanently rejected prompt events are discarded instead of copied
into the lifecycle quarantine. A prompt endpoint `401` or `403` turns local
prompt capture off and purges the prompt outbox without exposing its contents.

Prompt capture requires exact `UserPromptSubmit`, the claimed `"on"` policy,
and a trusted transcript path whose first bounded record is valid `session_meta`
with no subagent/fork source or parent markers. Uncertain provenance fails
closed. It never derives prompt events from a tool hook, transcript, assistant
response, subagent, or fork.

## Privacy and exclusion contract

Builder Pulse is installed machine-wide, but it captures only hooks whose
working directory is inside a project folder the member explicitly enrolled.
An enrollment includes that folder's descendants, including non-Git projects;
it does not widen a confirmed monorepo package to the repository root. The home
directory, its parents, and filesystem root cannot be enrolled. Missing
working-directory metadata, missing enrollment, or incomplete project context
fails closed before prompt text or transcript metadata is inspected and before
any lifecycle or prompt event is created. Builder Pulse captures only the
submitted user prompt under the exact contract above.
It must never add these values to either wire contract or outbox:

- shell commands;
- tool input or output;
- source, patches, full paths, transcript paths, or raw transcript content;
- assistant responses;
- environment variables or invite codes;
- telemetry endpoint response bodies.

The local transcript path and raw transcript content are never added to plugin
state, outbox, quarantine, logs, or wire payloads. Only the five validated
numeric cumulative counters may be attached to an otherwise due primary-session
lifecycle event. The prompt redactor replaces recognized authorization and
API-token values before the bounded prompt enters its outbox, but it is not a
complete secret detector. A submitted prompt may itself mention command, path,
or source content; that user-authored content remains part of `promptText`.

A shell command may be inspected in memory only long enough to distinguish
testing or successful review-artifact creation. Project names are explicitly
confirmed by the member and must not be inferred from a folder basename, prompt
content, or roster default. Feature labels are explicit and must not be inferred
from prompt content.

Project and feature overrides are keyed by private HMACs of the exact enrolled
folder and its ancestors so descendants can be matched without storing paths.
Full folder paths are not persisted or sent. There is no global project or
feature fallback. The v0.4.6 migration deletes ambiguous legacy queued records
and local state that do not carry both `projectLabel` and
`projectScope: "explicit"`; it preserves claimed identity and explicit
per-folder contexts.
