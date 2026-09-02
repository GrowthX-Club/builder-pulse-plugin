# Builder Pulse v0.6.0 setup

This guide belongs to the immutable `v0.6.0` release. Use it only from the
release-pinned URL; never run an installer from a default branch.

Builder Pulse installs hooks for Codex and Claude Code when those agents are available on this computer, but it sends data only from project folders you explicitly enroll. One shared identity and project allowlist apply to both agents. GrowthX stores the claimed member ID, name, email address, and any optional roster or program label supplied by GrowthX so telemetry can be linked to the right person. A roster or program label is never used as a telemetry project. For each enrolled project, it receives a stable installation ID, a one-way hashed session ID, the display name you confirm and a sanitized project ID, any feature name and ID you explicitly set, coarse work state and event/activity timestamps, agent name, plugin version, optional cumulative Codex token counts, and each primary prompt you submit after secret redaction and a 64 KiB limit. GrowthX's authenticated Builder Pulse admins can view these identity and telemetry fields for learning feedback. Raw lifecycle events and activity buckets are retained for 30 days; submitted prompts and their feedback are retained for 60 days; the member identity fields, installation/member link, latest status, and compacted session, daily, and all-time token aggregates remain until GrowthX removes them. It does not send folder paths, files, patches, commands, tool input or output, assistant replies, transcripts, or environment variables. Secret redaction is a safety layer, not a guarantee, so do not put secrets in prompts.

## Run the installer

Prerequisites: Python 3.11+ (`python3` on macOS/Linux, `py -3` on Windows),
git, and Codex and/or Claude Code. On macOS the installer also finds the Codex
bundled with the desktop app when `codex` is not on the PATH.

1. Verify the release before executing anything from it:
   `git ls-remote --exit-code --refs https://github.com/GrowthX-Club/builder-pulse-plugin.git refs/tags/v0.6.0`
   must return one ref, and an unauthenticated `GET` to
   `https://api.github.com/repos/GrowthX-Club/builder-pulse-plugin/releases/tags/v0.6.0`
   must report `tag_name: "v0.6.0"`, `draft: false`, `immutable: true`.
2. Clone only that tag into a fresh temporary directory:
   `git clone --depth 1 --branch v0.6.0 --single-branch …` and require
   `git describe --tags --exact-match HEAD` to print `v0.6.0`.
3. Run the installer **by its absolute path while your working directory is
   your project folder**. Never `cd` into the clone: the installer offers the
   current folder as the default project and the clone must never be enrolled.

```text
<python> <absolute path to the clone>/scripts/setup_builder_pulse.py
```

The installer asks for the invite code without echoing it (an agent that
already has the code passes it only through `BUILDER_PULSE_INVITE_CODE` for
that one process), then shows the current folder and its nearest Git root and
asks which exact folder to enroll and what display name GrowthX should show.
Delete only the temporary clone afterwards; never delete Builder Pulse data.

### Repair (already claimed, no new invite)

```text
<python> <absolute path to the clone>/scripts/setup_builder_pulse.py --reuse-existing-claim
```

Repair reuses the claimed identity from `~/.builder-pulse` or, when an older
attempt never migrated it, from the legacy Codex data directory. It never
creates or replaces an identity and never enrolls a folder by itself; it only
offers to enroll an additional folder when asked in the terminal. Run
`work list` afterwards and, if the folder you build in is missing, run
`work enroll` interactively from inside that folder.

## What the installer does, in order

Verify the release → migrate legacy data once → locate or require the identity
→ pause capture (locally and on the GrowthX service) → install this release for
Claude Code and Codex (the Codex marketplace is re-pinned to the tag when it
points elsewhere) → restore the identity → claim (setup mode only) → enroll
(only when confirmed) → resume capture → activate each agent → flush.

If anything fails after the pause, capture stays off locally and paused on the
service, the previous Codex release tag is put back, and every data directory
is kept. Nothing is captured while packages are swapped.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Installed and verified for every agent found. |
| 3 | Installed and capture is on, but Codex has not approved the hooks yet. One-time step: start Codex inside the enrolled folder, run `/hooks`, select the builder-pulse hooks and trust them. Telemetry starts as soon as they are trusted; no rerun is needed. |
| 1 | Failed. The last line is `Details: <log path>`. |

Members trusted on v0.4.2–v0.4.5 (and v0.5.2+) need no `/hooks` step: the
hook definition is unchanged. Members trusted on v0.4.6–v0.5.1 see exit 3 once.

## Log

Every run writes `~/.builder-pulse/logs/setup-<utc>.log` (mode 0600, newest
10 kept). It contains no invite codes, tokens, bearer headers, home directory
paths, or project folder paths, so it can be shared with GrowthX when asking
for help. `activate` appends to `~/.builder-pulse/logs/activate.log`.

## Verify

Exit every running Codex and Claude Code session, start a fresh one inside an
enrolled folder, send one normal prompt, then run
`<python> <installed plugin>/scripts/builder_pulse.py activate --agent codex`
(or `--agent claude_code`). Only `telemetryReceivedSincePreviousActivation:
true` with `lastSignalPluginVersion: "0.6.0"` and the matching
`lastSignalAgentPlatform` proves end-to-end delivery for that agent.

## Uninstall

`codex plugin remove builder-pulse@growthx-builder-tools` and
`claude plugin uninstall builder-pulse-claude-posix@growthx-builder-tools-v0-6-0 --scope user`
remove the packages. Identity and enrollments stay in `~/.builder-pulse`, so a
later repair reuses the same installation with no new invite.
