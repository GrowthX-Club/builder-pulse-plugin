# Builder Pulse v0.5.2 setup

This guide belongs to the immutable `v0.5.2` release. Run it only from the
release-pinned URL. Do not use a guide or installer from a default branch.

v0.5.2 is the repair release for the v0.5.0 and v0.5.1 upgrade failures. It
keeps the Codex hook definition byte-identical to v0.4.5, so members who set up
Builder Pulse v0.4.2 through v0.4.5 upgrade without a new hook review; it
verifies package provenance with git instead of a size-capped GitHub API
response; it never uninstalls a package that is only waiting for the member's
one-time `/hooks` approval; it never enrolls a folder the member did not name;
and it writes a private, secret-free log for every run.

The installer targets the current stable release, preserves the existing plugin
data directory, safely reverifies an existing GrowthX member identity, rolls
back a failed package update, installs native hooks for every available Codex
and Claude Code agent, and flushes queued telemetry.

Before it changes the installed package, the installer verifies both the exact
Git tag and its published GitHub Release. The release API must report the exact
target tag, `draft: false`, and `immutable: true`; a tag existing by itself is
not proof of immutability. Setup fails closed if either check cannot be proved.

Builder Pulse installs hooks for Codex and Claude Code when those agents are available on this computer, but it sends data only from project folders you explicitly enroll. One shared identity and project allowlist apply to both agents. GrowthX stores the claimed member ID, name, email address, and any optional roster or program label supplied by GrowthX so telemetry can be linked to the right person. A roster or program label is never used as a telemetry project. For each enrolled project, it receives a stable installation ID, a one-way hashed session ID, the display name you confirm and a sanitized project ID, any feature name and ID you explicitly set, coarse work state and event/activity timestamps, agent name, plugin version, optional cumulative Codex token counts, and each primary prompt you submit after secret redaction and a 64 KiB limit. GrowthX's authenticated Builder Pulse admins can view these identity and telemetry fields for learning feedback. Raw lifecycle events and activity buckets are retained for 30 days; submitted prompts and their feedback are retained for 60 days; the member identity fields, installation/member link, latest status, and compacted session, daily, and all-time token aggregates remain until GrowthX removes them. It does not send folder paths, files, patches, commands, tool input or output, assistant replies, transcripts, or environment variables. Secret redaction is a safety layer, not a guarantee, so do not put secrets in prompts.

## Exit code contract

Every installer run ends in exactly one of these states:

| Exit code | Meaning | What to do |
| --- | --- | --- |
| `0` | Installed, claimed, enrolled, and verified for every agent found. | Exit running agent sessions, start a fresh one inside an enrolled folder, send one normal prompt. |
| `3` | Installed and waiting for the member's one-time Codex `/hooks` approval. **This is not an error and needs no rerun.** | Start Codex inside the enrolled folder, run `/hooks`, trust the builder-pulse hooks. Telemetry starts immediately. Confirm later with `activate --agent codex`. |
| `1` | Setup stopped. Every previous package, identity, and queue was restored or left quarantined exactly as reported. | Read the line `Details: <log path>` at the end of the output and send that file when asking for help. It contains no secrets and no project folder paths. |

The log lives under the shared data directory at `~/.builder-pulse/logs/`
(`setup-<UTC timestamp>.log`, newest ten kept; `activate.log` for later
`activate` runs). The installer creates that `logs` directory at start, so the
`Details:` path is the same stable location however early a run stops; a shared
directory holding nothing but `logs` is still treated as absent when the legacy
identity is migrated. Invite codes, installation tokens, bearer values, prompts,
transcripts, the working directory, and enrolled folder paths are never written
to it; only a folder's last path component appears.

## Run the installer

Do not ask for a project folder or project name in the primary agent
conversation. An older machine-wide hook may still capture that answer. The
installer collects the choice locally in the terminal, after showing only the
current folder and, when different, its nearest Git repository root. It never
scans the home directory or recent projects. The member must confirm the exact
folder and type the display name GrowthX should receive; neither value is
inferred from folder names, prompt text, or the roster's legacy
`defaultProject` value.

Use Python 3.11+ (`python3` on macOS/Linux or `py -3` on Windows). Before
executing repository code, verify the external release facts:

1. `git ls-remote --exit-code --refs https://github.com/GrowthX-Club/builder-pulse-plugin.git refs/tags/v0.5.2`
   must return exactly one tag ref.
2. An unauthenticated `GET` to
   `https://api.github.com/repos/GrowthX-Club/builder-pulse-plugin/releases/tags/v0.5.2`
   must return `tag_name: "v0.5.2"`, `draft: false`, and `immutable: true`.

Stop safely if either proof is unavailable. Create a fresh temporary directory,
then clone only the verified tag with `--depth 1 --branch v0.5.2
--single-branch`. Before running anything from the clone, require
`git describe --tags --exact-match HEAD` to print `v0.5.2` and require the
clone's `refs/tags/v0.5.2` object ID to equal the object ID returned by
`ls-remote`.

**Run the installer by its absolute path while the working directory is the
member's project folder.** Never change directory into the temporary clone: the
current folder is the default enrollment choice, and a temporary or installer
folder is refused. Run it interactively without project arguments:

```text
cd <member project folder>
<python> <absolute path to temporary-v0.5.2-clone>/scripts/setup_builder_pulse.py
```

For an already-claimed recovery that deliberately has no new invite, run the
same command with `--reuse-existing-claim`. It fails closed unless a verified
preserved local identity is complete, and it requires the same installation,
builder, and GrowthX member IDs after replacement. A repair keeps every existing
enrollment and never adds one on its own: it asks
`Enroll an additional project folder now? [y/N]` and defaults to no. After a
repair, run `work list`; if the member's real project folder is missing, run
`work enroll` interactively from inside that folder.

### Recovery from the v0.5.0 and v0.5.1 upgrade regressions

Do not retry the v0.5.0 or v0.5.1 installers. If one of them removed an
existing Codex or Claude Code registration, or left the claim quarantined
(`status` shows `Claimed: no`), preserve both `~/.builder-pulse` and the legacy
Codex Builder Pulse data directory. After independently verifying the immutable
v0.5.2 release as described above, run its installer with
`--reuse-existing-claim` from inside the member's project folder. The repair
restores the quarantined claimed identity, whether it sits in `~/.builder-pulse`
or only in the legacy Codex data directory (an attempt that stopped before its
migration step leaves it there); it does not require or consume a new invite
code. Do not delete or hand-edit the preserved data directories.

The installer securely asks for the personalized handbook code without echoing
it. An agent that already received the code in its setup prompt may instead
pass it only through the `BUILDER_PULSE_INVITE_CODE` environment variable for
that process. Never print the code or place it in shell history.

The agent should run the installer in an interactive local terminal so the
folder, display name, and invite-code answers do not become primary agent
prompts. It may pass a code already present in the setup request only through
the `BUILDER_PULSE_INVITE_CODE` environment of that one process; it must not
echo the value. Delete only the temporary clone after setup finishes. Never
delete Builder Pulse's shared data directory or legacy Codex data directory.
When upgrading an existing installation, the installer pauses its old capture
on the GrowthX service before replacing the package. It resumes server acceptance
only after the confirmed project is enrolled, requires the service to acknowledge
that exact installation, and re-pauses the service if resume acknowledgement,
activation, or flush fails for any reason other than a pending hook review.
The confirmed folder path is used locally to enforce scope and is represented
only by an HMAC keyed with a random secret private to that installation; the path
is never sent to GrowthX. The confirmed display name and a stable sanitized
project ID are sent with events.

## Codex hook review

Codex trusts a hook definition by hashing its normalized content, and it skips
plugin hooks until the member has trusted that exact definition once. The
Codex hook definition in this release is byte-identical to v0.4.5, so a member
who trusted v0.4.2 through v0.4.5 needs no new review. A member who trusted
v0.4.6, v0.5.0, or v0.5.1 sees Codex report the hooks as modified. In that
case the installer exits `3`, leaves the package installed and capture enabled
(untrusted hooks never execute), and prints:

```text
Builder Pulse is installed but Codex has not approved its hooks yet. One-time step: start Codex inside <folder>; it warns that hooks need review. Run /hooks, select the builder-pulse hooks and trust them (Codex desktop app: type /hooks in the composer). Telemetry starts as soon as they are trusted - no rerun of this installer is needed.
```

Never approve hooks on the member's behalf and never edit `hooks.state` in
`config.toml` by hand.

The local setup is complete only when the installer exits `0` and prints:

```text
Builder Pulse is installed for every supported agent found on this computer. Only project folders you explicitly confirmed are enrolled; this confirmed project was added without removing prior confirmed projects. Exit all running Claude Code and Codex sessions, start a fresh session in each agent you use, then send one normal prompt in each to verify separate server receipts.
```

(A repair that enrolled nothing prints `prior confirmed projects were kept
unchanged` in place of the second sentence.)

For every additional folder, run the installed script interactively. It shows
the local current-folder/repository choices and asks for the exact folder and
display name in the terminal:

```text
<python> <installed-plugin>/scripts/builder_pulse.py work enroll
```

Run `work show` from each selected folder and verify the final count with
`work list`. Do not enroll anything else. Parent/child enrollment boundaries
cannot overlap; choose one deliberate boundary for a monorepo or package.
Updating from an older version preserves identity. The service rejects every unscoped payload,
including those from older plugin versions, so an old client stops reporting
until this update and explicit enrollment are complete. Older context records remain inactive until the
member explicitly enrolls that folder with a display name; the first explicit
enrollment clears any ambiguous legacy feature label. The update also discards
legacy queued events and local state that lack explicit project scope.
Each enrollment covers the confirmed folder and its descendants, including
one package inside a monorepo and projects without a Git repository. It never
widens a confirmed subfolder to the surrounding repository root.

`config set enabled false` is an explicit global pause. It is serialized with
delivery, deletes unsent lifecycle and prompt queues plus current local work
states, and cannot be overridden by a stale `BUILDER_PULSE_ENABLED=1`
environment variable. It preserves the claimed identity and project allowlist.

Exit every running Claude Code and Codex session so no process keeps the
previous hook manifest or version path in memory. Start a fresh session in each
agent you use and send one ordinary prompt from inside an enrolled project.
Then run `activate --agent codex` and/or `activate --agent claude_code` in a
subsequent turn. Only `telemetryReceivedSincePreviousActivation: true` with a
non-null `lastSignalAt`, `lastSignalPluginVersion: "0.5.2"`, and the matching
`lastSignalAgentPlatform` is current end-to-end proof for that agent.
`telemetryReceived: true` alone can describe a historical event. Hook readiness,
`serverVerified: true`, or empty local queues alone do not prove delivery. Every
`activate` result that is not ready carries a `detail` field naming the exact
local condition; the same record is appended to `~/.builder-pulse/logs/activate.log`.
