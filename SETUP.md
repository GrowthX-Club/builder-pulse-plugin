# Builder Pulse setup

This is the stable Builder Pulse install and update entrypoint. The handbook
links here so it never needs to know a release number.

The installer targets the current stable release, preserves the existing plugin
data directory, safely reverifies an existing GrowthX member identity, rolls
back a failed package update, activates the official Codex hooks, and flushes
queued telemetry.

Before it changes the installed package, the installer verifies both the exact
Git tag and its published GitHub Release. The release API must report the exact
target tag, `draft: false`, and `immutable: true`; a tag existing by itself is
not proof of immutability. Setup fails closed if either check cannot be proved.

Builder Pulse is installed machine-wide, but it sends data only from project folders you explicitly enroll. GrowthX stores the claimed member ID, name, email address, and any optional default project or program copied from the member record so telemetry can be linked to the right person. For each enrolled project, it receives a stable installation ID, a one-way hashed session ID, the display name you confirm and a sanitized project ID, any feature name and ID you explicitly set, coarse work state and event/activity timestamps, plugin version, optional cumulative token counts, and each primary prompt you submit after secret redaction and a 64 KiB limit. GrowthX's authenticated Builder Pulse admins can view these identity and telemetry fields for learning feedback. Raw lifecycle events and activity buckets are retained for 30 days; submitted prompts and their feedback are retained for 60 days; the member identity fields, installation/member link, latest status, and compacted session, daily, and all-time token aggregates remain until GrowthX removes them. It does not send folder paths, files, patches, commands, tool input or output, assistant replies, transcripts, or environment variables. Secret redaction is a safety layer, not a guarantee, so do not put secrets in prompts.

## Run the installer

Before any install or update, inspect only the current working directory and,
if different, its nearest repository root. Tell the member both detected folders,
then ask which exact folder or folders to enroll:

> Which project folder or folders should Builder Pulse monitor, and what display
> name should GrowthX use for each? Reply `current project only — <name>` or list
> each folder with its display name.

Stop and wait for the answer. Do not scan the home directory, infer a name from
the folder or prompt text, or reuse the roster's cohort/default-project value.
Proceed only with existing folders and display names the member explicitly
confirms. Never enroll the member's home directory, a parent of that home
directory, or a filesystem root.

Use Python 3.11+ (`python3` on macOS/Linux or `py -3` on Windows). Clone this
repository to a temporary directory and run the installer for the first
confirmed folder:

```text
<python> <temporary-repository>/scripts/setup_builder_pulse.py --project-root "<confirmed-folder>" --project-label "<confirmed-display-name>"
```

The installer securely asks for the personalized handbook code without echoing
it. A Codex agent that already received the code in its setup prompt may instead
pass it only through the `BUILDER_PULSE_INVITE_CODE` environment variable for
that process. Never print the code or place it in shell history.

A Codex agent should create the temporary directory, clone
`https://github.com/GrowthX-Club/builder-pulse-plugin.git` with `--depth 1`,
run that one installer command, and delete only that temporary clone after it
finishes. It must never delete Builder Pulse's Codex plugin data directory.
When upgrading an existing installation, the installer pauses its old capture
before replacing the package and enables capture again only after the confirmed
project is enrolled.
The confirmed folder path is used locally to enforce scope and is represented
only by an HMAC keyed with a random secret private to that installation; the path
is never sent to GrowthX. The confirmed display name and a stable sanitized
project ID are sent with events.

The local setup is complete only when the installer prints:

```text
Builder Pulse is installed and its hooks are trusted. Only the confirmed project folder is enrolled. Exit all running Codex sessions, start a fresh Codex session, then send one normal prompt to verify server receipt.
```

For every additional confirmed folder, use the installed script with:

```text
<python> <installed-plugin>/scripts/builder_pulse.py work enroll --root "<confirmed-folder>" --project "<confirmed-display-name>"
```

Verify each folder with `work show --root "<confirmed-folder>"` and the final
count with `work list`. Do not enroll anything else. Updating from an older
version preserves identity. The service rejects every unscoped payload,
including those from older plugin versions, so an old client stops reporting
until this update and explicit enrollment are complete. Older context records remain inactive until the
member explicitly enrolls that folder with a display name; the first explicit
enrollment clears any ambiguous legacy feature label. The update also discards
legacy queued events and local state that lack explicit project scope.
Each enrollment covers the confirmed folder and its descendants, including
one package inside a monorepo and projects without a Git repository. It never
widens a confirmed subfolder to the surrounding repository root.

Exit every running Codex session so no process keeps the previous hook manifest
or version path in memory. Start a fresh Codex session, send one ordinary
prompt from inside an enrolled project, then run `activate` again in a
subsequent turn. Only `telemetryReceivedSincePreviousActivation: true` with a
non-null `lastSignalAt` and `lastSignalPluginVersion: "0.4.6"` is current
end-to-end server-receipt proof. `telemetryReceived: true` alone can describe a
historical event. `hooksTrusted: true`, `serverVerified: true`, or empty local
queues alone do not prove that a repaired hook delivered telemetry.
