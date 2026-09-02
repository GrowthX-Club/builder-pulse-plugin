# Builder Pulse integration harness

Real-Codex, real-GitHub, isolated-HOME scenarios for the installer. **No
installer change merges without these passing.** Fifty-one earlier iterations
were verified only with mocked unit tests; every one of the five root causes
fixed in v0.5.2 was reproduced here first.

The harness is manual (CI does not run it): it needs the real `codex` CLI,
network access to GitHub, and about a minute per scenario.

## Prerequisites

- `codex` on the PATH (Codex CLI 0.151+); optionally `claude` (Claude Code CLI)
  for the `claude` variant.
- `git`, `python3` ≥ 3.11, and on macOS `perl` (used for timeouts).
- Two environment variables:
  - `HARNESS_ROOT`: a writable scratch directory. It receives an isolated
    `home/` (used as `HOME`, so `~/.codex`, `~/.claude`, `~/.builder-pulse` are
    all private to the harness), release clones, logs, and state snapshots.
  - `HARNESS_PROJECT`: a real project folder to enroll. It must not live under
    a temp directory (the installer refuses those) and is created as a tiny git
    repo when missing.

Nothing touches your real `~/.codex` or `~/.builder-pulse`.

## Pieces

| File | Role |
| --- | --- |
| `fake_server.py` | Loopback stand-in for the Convex service (`/v1/claim`, `privacy-pause`, `privacy-resume`, `activation`, `telemetry`, `prompts`) with the same status/body semantics; logs every request (tokens hashed) to `fake_server.log.jsonl`, state in `fake_server.state.json`. |
| `trust_hooks.py` | Reads `hooks/list` from the local app-server exactly like the runtime does; `--approve` writes `hooks.state.<key>.trusted_hash` into `config.toml` the way the Codex TUI does, simulating the member's one-time `/hooks` approval. |
| `seed.sh <tag>` | Builds the "working member" state for one historical release: install, approve hooks, claim, set/enroll the project, one Codex turn, activate. Snapshot it afterwards (see below). |
| `seed_inder.sh` | From a v0.4.5 seed, reproduces the real broken state: v0.5.0 attempt, both registrations stripped, v0.5.1 repair that rolled back, three enrollments. Snapshots as `home.inder`. |
| `scenarios.sh` | Runs one scenario against a checkout (via the shim) or a released tag (`real`), then prints local state, a simulated Codex turn, `activate`, and the fake server's view. |
| `run_branch_installer.py` | Pre-release shim: runs an unreleased checkout as if it were the release by swapping only the release lookup (`--ref <sha>` for Codex, `@<branch>` for Claude). Everything else is the checkout's real code. |
| `pty_drive.py` | Drives the interactive installer through a real pty, answering prompts in order (how a member's terminal behaves). |

## Typical run

```sh
export HARNESS_ROOT=/path/to/scratch HARNESS_PROJECT=$HOME/Projects/harness-project
I=tests/integration

# 1. seed and snapshot a v0.4.5 member (trusted hooks, claimed, reporting)
$I/seed.sh v0.4.5
cp -a "$HARNESS_ROOT/home" "$HARNESS_ROOT/home.seed-v045"
cp "$HARNESS_ROOT/fake_server.state.json" "$HARNESS_ROOT/fake_server.state.seed-v045.json"
$I/seed_inder.sh          # writes home.inder + fake_server.state.inder.json

# 2. scenarios against an unreleased branch (push it first; HEAD must be the branch tip)
HARNESS_BRANCH=feat/my-branch $I/scenarios.sh /path/to/checkout <sha> upgrade-v045
HARNESS_BRANCH=feat/my-branch $I/scenarios.sh /path/to/checkout <sha> repair-inder
HARNESS_BRANCH=feat/my-branch $I/scenarios.sh /path/to/checkout <sha> upgrade-v045 claude

# 3. scenarios against a published tag (the exact installer members run)
$I/scenarios.sh real v0.6.0 upgrade-v045
```

## Scenarios

| Name | State | Must show |
| --- | --- | --- |
| `upgrade-v045` | trusted v0.4.5 member, setup prompt with a new invite | exit 0, hooks still `trusted` (no `/hooks`), a real hook turn delivered with the new version, `telemetryReceivedSincePreviousActivation: true` |
| `repair-inder` | registrations stripped, identity quarantined, stale `hooks.state`, three enrollments | exit 0, same installation id, no new enrollment, server resumed, turn delivered |
| `repair-skeleton` | claimed legacy identity plus a stray unclaimed skeleton, `runtime/`, `config.json` in `~/.builder-pulse` | exit 0, legacy identity wins, runtime/logs preserved |
| `repair-legacy-only` | identity only in the legacy Codex data dir | exit 0, identity migrated |
| `review-v046` | member trusted on the v0.4.6-style hooks | exit 3, plugin stays installed and enabled, server resumed; after `trust_hooks.py --approve` a turn delivers |
| `repair-v050-marker` | v0.5.0 registered with Codex's `.codex-marketplace-install.json` in the cache | exit 0 |
| `fresh` | brand-new member | exit 3 (first review), claimed, resumed; after approval a turn delivers |
| any + `claude` | same with Claude Code on the PATH | both agents activate; the Claude launcher delivers `claude_code` events |

Read the `Details:` log the installer prints; it is the same file a member
would send when asking for help and must never contain a token, an invite
code, a bearer header, a home directory, or a project path.
