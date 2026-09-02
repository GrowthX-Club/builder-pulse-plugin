#!/bin/sh
# Seed the isolated harness home with a claimed, enrolled, trusted, reporting
# Builder Pulse install of the given release, exactly as a member who completed
# the original setup and used Codex once would have it.
#   usage: seed.sh v0.4.5
set -eu
VERSION="$1"
. "$(cd "$(dirname "$0")" && pwd)/harness_env.sh"

echo "== reset isolated home =="
rm -rf "$HOME"
mkdir -p "$HOME"
rm -f "$H/fake_server.state.json" "$H/fake_server.log.jsonl"
# the fake server reloads state lazily; restart it so its in-memory state is empty
pkill -f "fake_server.py 8765" || true
sleep 0.5
(cd "$H" && nohup python3 "$I/fake_server.py" 8765 > "$H/logs/fake_server.out" 2>&1 < /dev/null &)
sleep 1

cd "$P"
echo "== install $VERSION =="
codex plugin marketplace add GrowthX-Club/builder-pulse-plugin --ref "$VERSION" --json > /dev/null
codex plugin add builder-pulse@growthx-builder-tools --json > /dev/null
CACHE=$(ls -d "$HOME"/.codex/plugins/cache/growthx-builder-tools/builder-pulse/*/ | head -1)
CLI="$CACHE/scripts/builder_pulse.py"
echo "cache=$CACHE"

echo "== approve hook review (as the member would in Codex) =="
python3 "$I/trust_hooks.py" "$P" --approve | grep -E "after approve|builder-pulse hooks"

echo "== claim + enroll + one Codex turn =="
BUILDER_PULSE_INVITE_CODE="harness-invite-code-0001-abcdef" python3 -I -S "$CLI" claim --endpoint http://127.0.0.1:8765 > /dev/null
ENROLL_ARGS="--root $P --project HarnessProject"
if python3 -I -S "$CLI" work --help 2>&1 | grep -q enroll; then
  python3 -I -S "$CLI" work enroll $ENROLL_ARGS > /dev/null 2>&1 || python3 -I -S "$CLI" work enroll $ENROLL_ARGS --replace-existing > /dev/null
else
  # v0.4.5 and older: project context is set, not enrolled
  python3 -I -S "$CLI" work set $ENROLL_ARGS > /dev/null
fi
if [ -f "$CACHE/scripts/builder_pulse.sh" ]; then
  printf '{"hook_event_name":"SessionStart","session_id":"sess-seed","cwd":"%s"}\n' "$P" | sh "$CACHE/scripts/builder_pulse.sh" > /dev/null
else
  printf '{"hook_event_name":"SessionStart","session_id":"sess-seed","cwd":"%s"}\n' "$P" | python3 "$CLI" hook > /dev/null
fi
sleep 0.5
echo "== activate =="
python3 -I -S "$CLI" activate | grep -E '"(activationReady|hookStatus|serverVerified|telemetryReceived|lastSignalPluginVersion)"'
echo "== data locations =="
find "$HOME" -name identity.json -o -name setup-paused-identity.json
echo "== config.toml =="
cat "$HOME/.codex/config.toml"
