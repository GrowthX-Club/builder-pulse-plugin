---
name: builder-pulse
description: "Claim, inspect, explain, configure, or explicitly update Builder Pulse progress and learning-feedback telemetry. Use when the user asks how a builder is doing, wants to set product or feature context, needs current state, or wants to retry delivery."
---

# Builder Pulse

Builder Pulse reports claimed builder identity, product/project, an explicitly
labeled feature, coarse state, a capped active interval, and—when Codex exposes
it—an optional cumulative numeric per-session token snapshot. On each primary
`UserPromptSubmit`, it also sends bounded, high-confidence-secret-redacted
prompt text to GrowthX for learning feedback.

## Locate the CLI

Resolve `../../scripts/builder_pulse.py` relative to this `SKILL.md`, then use
that resolved absolute path in commands below. In the examples, `<python>` means
`python3` on macOS/Linux and `py -3` on Windows. Before claiming, verify Python
3.11 or newer with `python3 --version` on macOS/Linux or `py -3 --version` on
Windows. Stop and explain the missing prerequisite if that check fails; hooks
cannot report telemetry without this standard-library-only runtime.

## Consent boundary

Before claiming an installation, show this exact disclosure:

> Builder Pulse connects you with GrowthX so that we can track your progress and provide you learning feedback.

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
Subagent and fork prompt capture and token snapshots remain off.

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

## Set product and feature

Feature context must be concise and non-sensitive. Never copy a raw prompt into
the feature label.

```bash
<python> <resolved-cli-path> work set --project growthx-community --feature "Member search filters"
<python> <resolved-cli-path> work show
<python> <resolved-cli-path> work clear-feature
```

Feature labels are limited to 120 characters. A stable feature ID is sanitized
or can be given with `--feature-id`. Context is scoped to the current repository;
use `--root <repository>` to target another checkout. Global config values are
fallbacks, not the place to label concurrent work.

## Read status

```bash
<python> <resolved-cli-path> status --json
```

Summarize claimed identity, prompt-capture policy, product, feature, state, last
event time, staleness, lifecycle queue count, and prompt queue count. Active
time means approximate Codex activity, not total working hours. Session overlap
is deduplicated server-side.

## Explicit coarse state

Use an explicit mark only when the state is known:

```bash
<python> <resolved-cli-path> mark blocked
<python> <resolved-cli-path> mark ready
```

Allowed states are `building`, `testing`, `blocked`, and `ready`; `SessionEnd`
sets `idle`. Do not infer that successful tests alone mean `ready`.

## Delivery

State changes and 15-minute heartbeats append one minimal lifecycle event to a
bounded, file-locked outbox. A due event may include one validated cumulative
numeric primary-session token snapshot; subagent and fork snapshots are
suppressed. Long unobserved gaps receive no active-time credit. Later state
changes/heartbeats retry failed events using the same `eventId`; one nonblocking
delivery lease prevents duplicate concurrent flushes and permanent client errors
are quarantined.

Separately, every primary `UserPromptSubmit` with a trusted transcript whose
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

Delivery is best effort and must never interrupt Codex. Read the reference when
changing state semantics, claim behavior, privacy guarantees, or the wire
contract.
