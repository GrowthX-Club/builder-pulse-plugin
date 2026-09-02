#!/bin/sh
# From a working v0.4.5 seed, reproduce Inder's state: a v0.5.x installer removed both
# Codex registrations, the v0.5.1 repair restored the identity, failed on hook review and
# rolled back (quarantined identity, capture off), leaving stale hooks.state and extra
# enrollments. Snapshots the result as home.inder + fake_server.state.inder.json.
set -eu
. "$(cd "$(dirname "$0")" && pwd)/harness_env.sh"
rm -f "$H/bin/claude"

clone_release v0.5.0; clone_release v0.5.1
echo "== v0.5.0 setup prompt with a new invite (real release installer): migrates identity, fails on hook review =="
cd "$P"
BUILDER_PULSE_INVITE_CODE="harness-invite-code-0002-second" python3 "$H/clones/v0.5.0/scripts/setup_builder_pulse.py" --endpoint http://127.0.0.1:8765 --project-root "$P" --project-label "Harness Project" > "$H/logs/seed-inder-v050.out" 2> "$H/logs/seed-inder-v050.err" < /dev/null || true
grep -v "^Builder Pulse installs hooks" "$H/logs/seed-inder-v050.err" | tail -1

echo "== strip both Codex registrations (what v0.5.0 did on Inder's machine) =="
codex plugin remove builder-pulse@growthx-builder-tools --json > /dev/null 2>&1 || true
codex plugin marketplace remove growthx-builder-tools --json > /dev/null 2>&1 || true

echo "== v0.5.1 --reuse-existing-claim (real release installer) =="
python3 "$H/clones/v0.5.1/scripts/setup_builder_pulse.py" --reuse-existing-claim --endpoint http://127.0.0.1:8765 --project-root "$P" --project-label "Harness Project" > "$H/logs/seed-inder-v051.out" 2> "$H/logs/seed-inder-v051.err" < /dev/null || true
grep -v "^Builder Pulse installs hooks" "$H/logs/seed-inder-v051.err" | tail -2

echo "== two stale enrollments from earlier attempts (temp clone dirs) =="
RT=$(ls "$HOME"/.builder-pulse/runtime/*/scripts/builder_pulse.py | tail -1)
mkdir -p "$H/stale1" "$H/stale2"
python3 -I -S "$RT" work enroll --root "$H/stale1" --project "builder-pulse-plugin" > /dev/null 2>&1 || true
python3 -I -S "$RT" work enroll --root "$H/stale2" --project "tmp-clone" > /dev/null 2>&1 || true

echo "== resulting state =="
codex plugin list 2>&1 | grep -c "builder-pulse@growthx" || true
grep -c trusted_hash "$HOME/.codex/config.toml"
python3 -I -S "$RT" status 2>&1 | head -1
python3 -I -S "$RT" work list 2>&1 | grep -c contextKey
python3 - "$HOME/.builder-pulse" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
for n in ("identity.json", "setup-paused-identity.json", "config.json"):
    v = json.loads((d / n).read_text())
    print(n, {k: ("<64hex>" if k in ("installationToken", "scopeSecret") else x) for k, x in v.items()})
PY
rm -rf "$H/home.inder"; cp -a "$HOME" "$H/home.inder"; cp "$H/fake_server.state.json" "$H/fake_server.state.inder.json"
echo "snapshot: $H/home.inder"
