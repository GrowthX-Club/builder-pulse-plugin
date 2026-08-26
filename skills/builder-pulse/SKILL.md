---
name: builder-pulse
description: "Claim, inspect, explain, configure, or explicitly update minimal Builder Pulse telemetry. Use when the user asks how a builder is doing, wants to set product or feature context, needs current state, or wants to retry delivery."
---

# Builder Pulse

Builder Pulse reports claimed builder identity, product/project, an explicitly
labeled feature, coarse state, and a capped active interval. Raw prompts and
prompt metadata are always off.

## Locate the CLI

Resolve `../../scripts/builder_pulse.py` relative to this `SKILL.md`, then use
that resolved absolute path in commands below.

## Consent boundary

Before claiming an installation, explain that Builder Pulse sends only the
minimal fields in [references/state-model.md](references/state-model.md). It
never persists or forwards prompt text, prompt length, commands, paths, source,
patches, tool I/O, transcripts, environment variables, or response bodies.

Do not claim an installation or configure an external endpoint unless the user
has explicitly authorized that external write. Never print, read back, or copy
the installation token. Invite codes should be entered interactively when
possible so they do not remain in shell history.

## Claim once (no login)

```bash
python3 <resolved-cli-path> claim --endpoint https://your-convex-site.example
```

The CLI first persists a client-generated 64-hex token as pending, then sends it
with the claim. A response-loss retry reuses that exact token. The server's
returned name and builder ID are recorded locally; the installation token is
mode `0600`, never printed, and bound to the claimed HTTPS endpoint.

## Set product and feature

Feature context must be concise and non-sensitive. Never copy a raw prompt into
the feature label.

```bash
python3 <resolved-cli-path> work set \
  --project growthx-community \
  --feature "Member search filters"
python3 <resolved-cli-path> work show
python3 <resolved-cli-path> work clear-feature
```

Feature labels are limited to 120 characters. A stable feature ID is sanitized
or can be given with `--feature-id`. Context is scoped to the current repository;
use `--root <repository>` to target another checkout. Global config values are
fallbacks, not the place to label concurrent work.

## Read status

```bash
python3 <resolved-cli-path> status --json
```

Summarize claimed identity, product, feature, state, last event time, staleness,
and queued-event count. Active time means approximate Codex activity, not total
working hours. Session overlap is deduplicated server-side.

## Explicit coarse state

Use an explicit mark only when the state is known:

```bash
python3 <resolved-cli-path> mark blocked
python3 <resolved-cli-path> mark ready
```

Allowed states are `building`, `testing`, `blocked`, and `ready`; `SessionEnd`
sets `idle`. Do not infer that successful tests alone mean `ready`.

## Delivery

State changes and 15-minute heartbeats append one minimal event to a bounded,
file-locked outbox. Long unobserved gaps receive no active-time credit. Later
state changes/heartbeats retry failed events using the same `eventId`; one
nonblocking delivery lease prevents duplicate concurrent flushes and permanent
client errors are quarantined. Manual retry:

```bash
python3 <resolved-cli-path> flush
```

Delivery is best effort and must never interrupt Codex. Read the reference when
changing state semantics, claim behavior, privacy guarantees, or the wire
contract.
