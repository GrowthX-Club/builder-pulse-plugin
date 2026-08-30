# Builder Pulse setup

This is the stable Builder Pulse install and update entrypoint. The handbook
links here so it never needs to know a release number.

The installer targets the current stable release, preserves the existing plugin
data directory, safely reverifies an existing GrowthX member identity, rolls
back a failed package update, activates the official Codex hooks, and flushes
queued telemetry.

## Run the installer

Use Python 3.11+ (`python3` on macOS/Linux or `py -3` on Windows). Clone this
repository to a temporary directory and run one command:

```text
<python> <temporary-repository>/scripts/setup_builder_pulse.py
```

The installer securely asks for the personalized handbook code without echoing
it. A Codex agent that already received the code in its setup prompt may instead
pass it only through the `BUILDER_PULSE_INVITE_CODE` environment variable for
that process. Never print the code or place it in shell history.

A Codex agent should create the temporary directory, clone
`https://github.com/udayanwalvekar/builder-pulse-plugin.git` with `--depth 1`,
run that one installer command, and delete only that temporary clone after it
finishes. It must never delete Builder Pulse's Codex plugin data directory.

The setup is complete only when the installer prints:

```text
Builder Pulse is connected. Send one new normal Codex prompt to verify telemetry.
```

Send one ordinary Codex prompt after setup. That subsequent prompt is the
end-to-end telemetry proof.
