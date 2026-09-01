---
name: builder-pulse
description: "Claim, inspect, explain, configure, or explicitly update Builder Pulse progress and learning-feedback telemetry. Use when the user asks how a builder is doing, wants to set product or feature context, needs current state, or wants to retry delivery."
---

# Builder Pulse

Builder Pulse is installed machine-wide but reports only from explicitly
enrolled project folders. It sends the claimed builder's member-confirmed
project name and stable ID, an optionally labeled feature, coarse state, a
capped active interval, and—when Codex exposes it—an optional cumulative numeric
per-session token snapshot. On each primary `UserPromptSubmit` inside an
enrolled project, it also sends bounded, high-confidence-secret-redacted prompt
text to GrowthX for learning feedback.

## Locate the CLI

Resolve `../../scripts/builder_pulse.py` relative to this `SKILL.md`, then use
that resolved absolute path in commands below. In the examples, `<python>` means
`python3` on macOS/Linux and `py -3` on Windows. Before claiming, verify Python
3.11 or newer with `python3 --version` on macOS/Linux or `py -3 --version` on
Windows. Stop and explain the missing prerequisite if that check fails; hooks
cannot report telemetry without this standard-library-only runtime.

## Consent boundary

Before claiming or enrolling an installation, show this exact disclosure:

> Builder Pulse installs hooks for Codex and Claude Code when those agents are available on this computer, but it sends data only from project folders you explicitly enroll. One shared identity and project allowlist apply to both agents. GrowthX stores the claimed member ID, name, email address, and any optional roster or program label supplied by GrowthX so telemetry can be linked to the right person. A roster or program label is never used as a telemetry project. For each enrolled project, it receives a stable installation ID, a one-way hashed session ID, the display name you confirm and a sanitized project ID, any feature name and ID you explicitly set, coarse work state and event/activity timestamps, agent name, plugin version, optional cumulative Codex token counts, and each primary prompt you submit after secret redaction and a 64 KiB limit. GrowthX's authenticated Builder Pulse admins can view these identity and telemetry fields for learning feedback. Raw lifecycle events and activity buckets are retained for 30 days; submitted prompts and their feedback are retained for 60 days; the member identity fields, installation/member link, latest status, and compacted session, daily, and all-time token aggregates remain until GrowthX removes them. It does not send folder paths, files, patches, commands, tool input or output, assistant replies, transcripts, or environment variables. Secret redaction is a safety layer, not a guarantee, so do not put secrets in prompts.

Explain the exact fields in
[references/state-model.md](references/state-model.md). Primary submitted prompt
text is captured, high-confidence-secret-redacted, UTF-8 bounded to 64 KiB,
temporarily queued locally, and sent to GrowthX. The redactor is not guaranteed
to recognize every possible secret. Separate commands, paths, source, patches,
tool I/O, transcripts, assistant responses, environment variables, and endpoint
response fields from hook payloads are never sent; a submitted prompt may itself
mention such content. Token usage, when present on an already-emitted
primary-session lifecycle event, contains only five nonnegative cumulative
counters; transcript paths and session content are never retained or sent.
Subagent and fork prompt capture and token snapshots remain off. Secret
redaction is a safety layer, not a guarantee; advise the user not to put secrets
in prompts.

Do not claim an installation or configure an external endpoint unless the user
has explicitly authorized that external write. Never print, read back, or copy
the installation token. Invite codes should be entered interactively when
possible so they do not remain in shell history.

## Claim once (no login)

```bash
<python> <resolved-cli-path> claim --endpoint https://your-convex-site.example
```

The CLI first persists a client-generated 64-hex token as pending, then sends it
with the schema v2 claim. A response-loss retry reuses that exact token. The
server's returned name, builder ID, and stable GrowthX member ID are recorded
locally; the installation token is mode `0600`, never printed, and bound to the
claimed HTTPS endpoint.
The claim must return `promptCapture: "on"` before prompt capture starts.

## Confirm and enroll projects

Do not ask for the folder or display name in a primary agent conversation. An
older machine-wide hook may still capture that answer. Once the user has
authorized setup or enrollment, run the CLI interactively in a local terminal.
It displays only the current working folder and, when different, the nearest
Git repository root, then asks locally:

> Which exact project folder should Builder Pulse monitor?
>
> What display name should GrowthX use for this project?

Do not scan the home directory or recent projects, infer names from folder
basenames or prompts, or reuse a server `defaultProject`; that field is
cohort/roster metadata. The member's local answers are the confirmation.

For an update or recovery install, verify both the exact Git tag and its
published GitHub Release before replacing anything. Continue only when the
release API reports the exact target `tag_name`, `draft: false`, and
`immutable: true`; tag existence alone is not proof of immutability. The
prepared installer performs both checks and fails closed.
For recovery of an already-claimed installation, use the pinned installer with
`--reuse-existing-claim`; never create or substitute an identity or invite. It
verifies the old package provenance and exact identity before replacement and
requires those identity fields to remain unchanged afterward.

```bash
<python> <resolved-cli-path> work enroll
<python> <resolved-cli-path> work show
<python> <resolved-cli-path> work list
```

The confirmed folder path is used locally only and is represented in `contexts.json`
by an HMAC keyed with a random secret private to that installation; it is never
sent to GrowthX. Hooks with no working directory
or outside enrolled folders fail closed and queue nothing. Enrollment covers
that exact folder and its descendants; it does not widen a monorepo package to
the repository root. The home directory, its parents, and filesystem root are
invalid enrollment targets. An older context without a member-confirmed project
label stays inactive, and its legacy feature label is cleared on first explicit
enrollment. To stop capture for a project, run
`work unenroll --root <confirmed-folder>`.
Parent and child enrollment boundaries cannot overlap; ask for one deliberate
boundary by rerunning the same local enrollment command from the intended
folder.

## Set feature context

Feature context must be concise and non-sensitive. Never copy a raw prompt into
the feature label.

```bash
<python> <resolved-cli-path> work set --root <enrolled-folder> --feature "Member search filters"
<python> <resolved-cli-path> work show --root <enrolled-folder>
<python> <resolved-cli-path> work clear-feature --root <enrolled-folder>
```

Feature labels are limited to 120 characters. A stable feature ID is sanitized
or can be given with `--feature-id`. Context is scoped to an enrolled folder;
use `--root <folder>` to target another checkout. There is no global project
or feature fallback.

## Read status

```bash
<python> <resolved-cli-path> status --json
```

Summarize claimed identity, prompt-capture policy and scope, enrollment count,
current-project enrollment, project, feature, state, last event time, staleness,
lifecycle queue count, and prompt queue count. Active time means approximate
Codex or Claude Code activity, not total working hours. Session overlap is deduplicated
server-side. Empty queues prove only that nothing is waiting locally; they do
not prove server receipt.

## Explicit coarse state

Use an explicit mark only when the state is known:

```bash
<python> <resolved-cli-path> mark blocked
<python> <resolved-cli-path> mark ready
```

Allowed states are `building`, `testing`, `blocked`, and `ready`; `SessionEnd`
sets `idle`. Do not infer that successful tests alone mean `ready`.

## Pause capture

```bash
<python> <resolved-cli-path> config set enabled false
```

This is a global fail-closed pause. It is serialized with delivery, deletes
unsent lifecycle and prompt queues plus current local work states, and cannot
be overridden by a stale `BUILDER_PULSE_ENABLED=1` environment variable. It
does not delete the claimed identity or project allowlist. Report the discarded
counts printed by the command.

## Delivery

State changes and 15-minute heartbeats append one minimal lifecycle event to a
bounded, file-locked outbox. A due event may include one validated cumulative
numeric primary-session token snapshot; subagent and fork snapshots are
suppressed. Long unobserved gaps receive no active-time credit. Later state
changes/heartbeats retry failed events using the same `eventId`; one nonblocking
delivery lease prevents duplicate concurrent flushes and permanent client errors
are quarantined.

Separately, every primary `UserPromptSubmit` from an explicitly enrolled
project folder with a trusted transcript whose
first bounded record is structurally primary appends one exact prompt event to a
bounded prompt outbox created as `0600` before any prompt bytes. It sends only
the bounded redacted prompt plus the claim/session/project/feature identifiers
and timing/processing flags. It never captures tool-hook prompts, command-hook
fields, tool I/O, transcripts, assistant responses, subagent prompts, or fork
prompts. Uncertain transcript provenance fails closed. Network retries preserve
`promptId` for at most 60 days; a permanent client rejection is discarded rather
than quarantining prompt text. A prompt `401` or `403` disables local prompt
capture and purges its outbox without exposing text.
Manual retry handles both queues:

```bash
<python> <resolved-cli-path> flush
```

Delivery is best effort and must never interrupt Codex or Claude Code. Hook readiness proves only
activation readiness; only a server receipt timestamp proves telemetry reached
GrowthX. Read the reference when changing state semantics, claim behavior,
privacy guarantees, or the wire contract.
