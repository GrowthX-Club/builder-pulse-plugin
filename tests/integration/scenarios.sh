#!/bin/sh
# End-to-end scenarios for an unreleased Builder Pulse installer checkout.
#   scenarios.sh <checkout> <sha> <scenario> [claude]
# scenarios: upgrade-v045 | repair-inder | review-v046 | repair-v050-marker | fresh
set -u
CHECKOUT="$1"; SHA="$2"; SCENARIO="$3"; WITH_CLAUDE="${4:-}"
. "$(cd "$(dirname "$0")" && pwd)/harness_env.sh"
rm -f "$H/bin/claude"
if [ -n "$WITH_CLAUDE" ]; then ln -sf "$(command -v claude)" "$H/bin/claude"; fi
LOG="$H/logs/scenario-$SCENARIO$WITH_CLAUDE-$(date +%H%M%S)"
mkdir -p "$H/logs"
STEP() { echo "[$(date +%H:%M:%S)] $*"; }

restart_server() {
  pkill -f "fake_server.py 8765" 2>/dev/null || true
  sleep 0.5
  (cd "$H" && nohup python3 "$I/fake_server.py" 8765 > "$H/logs/fake_server.out" 2>&1 < /dev/null &)
  sleep 1
}

restore() {  # restore <home-snapshot> <server-state-snapshot|none>
  rm -rf "$HOME"; cp -a "$1" "$HOME"
  rm -f "$H/fake_server.log.jsonl" "$H/fake_server.state.json"
  [ "$2" != none ] && cp "$2" "$H/fake_server.state.json"
  restart_server
}

server_summary() {
  STEP server_summary
  python3 - "$H" <<'PY'
import json, sys
H = sys.argv[1]
try:
    s = json.load(open(f"{H}/fake_server.state.json"))
except Exception:
    print("server: no state"); raise SystemExit
for v in s["installations"].values():
    print("server install:", v["installationId"][:8], "paused=", bool(v["privacyPausedAt"]), "pluginVersion=", v["pluginVersion"], "lastSignal=", v["lastSignalPluginVersion"], v["lastSignalAgentPlatform"], "hooksVerified=", sorted(v["hooksVerifiedAt"]))
print("events:", [(e["pluginVersion"], e["agentPlatform"], e["state"]) for e in s["events"]][-5:])
print("prompts:", len(s["prompts"]))
PY
  echo "server calls:"; python3 -c '
import json
for l in open("'$H'/fake_server.log.jsonl"):
    r=json.loads(l); print("  ", r["route"], r["status"], r.get("pluginVersion"), r.get("agentPlatform"), str(r.get("response"))[:70])' 2>/dev/null | tail -14
}

local_summary() {
  STEP local_summary
  echo "--- codex registrations ---"; codex plugin list 2>&1 | grep "builder-pulse@growthx" || echo "  (none)"
  grep -E "^ref =" "$HOME/.codex/config.toml" 2>/dev/null
  STEP hooks; echo "--- hooks ---"; python3 "$I/trust_hooks.py" "$P" 2>&1 | sed -n '2,8p'
  STEP data; echo "--- data ---"
  for d in "$HOME/.builder-pulse" "$HOME/.codex/plugins/data/builder-pulse-growthx-builder-tools"; do
    [ -d "$d" ] || continue
    echo "  $d: $(ls "$d" | tr '\n' ' ')"
    python3 - "$d" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
for name in ("identity.json", "setup-paused-identity.json", "config.json"):
    p = d / name
    if p.exists():
        v = json.loads(p.read_text())
        v = {k: ("<64hex>" if k in ("installationToken", "pendingInstallationToken", "scopeSecret") else val) for k, val in v.items()}
        print("   ", name, json.dumps(v))
PY
  done
  CLI=$(ls "$HOME"/.codex/plugins/cache/growthx-builder-tools/builder-pulse/*/scripts/builder_pulse.py 2>/dev/null | head -1)
  [ -z "$CLI" ] && CLI=$(ls "$HOME"/.builder-pulse/runtime/*/scripts/builder_pulse.py 2>/dev/null | tail -1)
  if [ -n "$CLI" ]; then
    STEP status; echo "--- status ($CLI) ---"; (cd "$P" && python3 -I -S "$CLI" status 2>&1 | head -3; python3 -I -S "$CLI" work list 2>&1 | grep -c contextKey | sed 's/^/  enrolled projects: /')
  fi
  echo "--- setup logs ---"; ls -la "$HOME/.builder-pulse/logs" 2>/dev/null | tail -4
  [ -n "$WITH_CLAUDE" ] && { echo "--- claude ---"; claude plugin list --json 2>/dev/null | grep -E '"id"|"version"|"enabled"'; }
}

run_installer() {  # run_installer <label> [args...]
  label="$1"; shift
  if [ "$CHECKOUT" = "real" ]; then
    clone_release "$SHA"
    # released tag: run the exact installer members run, no shim at all
    (cd "$P" && python3 "$H/clones/$SHA/scripts/setup_builder_pulse.py" --endpoint http://127.0.0.1:8765 "$@" > "$LOG-$label.out" 2> "$LOG-$label.err" < /dev/null)
  else
    (cd "$P" && python3 "$I/run_branch_installer.py" "$CHECKOUT" "$SHA" --endpoint http://127.0.0.1:8765 "$@" > "$LOG-$label.out" 2> "$LOG-$label.err" < /dev/null)
  fi
  code=$?
  echo "=== installer [$label] exit=$code ==="
  cat "$LOG-$label.out"
  grep -v "^Builder Pulse installs hooks\|^\[shim\]" "$LOG-$label.err" | cut -c1-900
  return $code
}

simulate_turn() {  STEP simulate_turn  # one Codex SessionStart from the enrolled project using the exact hook command
  CLI=$(ls "$HOME"/.codex/plugins/cache/growthx-builder-tools/builder-pulse/*/scripts/builder_pulse.py 2>/dev/null | head -1)
  (cd "$P" && printf '{"hook_event_name":"SessionStart","session_id":"sess-%s","cwd":"%s"}\n' "$(date +%s)" "$P" | CLAUDE_PLUGIN_ROOT="$(dirname "$(dirname "$CLI")")" PLUGIN_ROOT="$(dirname "$(dirname "$CLI")")" python3 "$CLI" hook)
  sleep 1
}

activate() {
  STEP activate
  CLI=$(ls "$HOME"/.codex/plugins/cache/growthx-builder-tools/builder-pulse/*/scripts/builder_pulse.py 2>/dev/null | head -1)
  (cd "$P" && python3 -I -S "$CLI" activate --agent "${1:-codex}" 2>&1 || python3 -I -S "$CLI" activate 2>&1); echo "activate exit=$?"
}

case "$SCENARIO" in
  upgrade-v045)
    restore "$H/home.seed-v045" "$H/fake_server.state.seed-v045.json"
    echo "### member on v0.4.5, trusted + reporting → v0.5.2 setup prompt with NEW invite"
    BUILDER_PULSE_INVITE_CODE="harness-invite-code-0003-upgrade" run_installer setup --project-root "$P" --project-label "Harness Project"
    local_summary; simulate_turn; activate codex; server_summary ;;
  repair-inder)
    restore "$H/home.inder" "$H/fake_server.state.inder.json"
    echo "### Inder: registrations stripped, identity quarantined, stale hooks.state, 3 enrollments → v0.5.2 --reuse-existing-claim (no project args)"
    run_installer repair --reuse-existing-claim
    local_summary; simulate_turn; activate codex; server_summary ;;
  review-v046)
    restore "$H/home.seed-v045" "$H/fake_server.state.seed-v045.json"
    echo "### member whose trusted hashes are v0.4.6's sh-launcher hooks → v0.5.2 must exit 3, keep plugin, resume server"
    # rewrite hooks.state to the v0.4.6 hashes captured earlier in this session
    python3 - "$HOME/.codex/config.toml" <<'PY'
import sys, re
p = sys.argv[1]; t = open(p).read()
new = {"permission_request": "9ad204fa7e92a993ae1db6713daba65b03c52e89790a573be91fbed479f8cefa",
       "post_tool_use": "a5acd0134d5ea46e152362c72674293a823aec96464841509d63b9552d079939",
       "session_start": "fa5a4e063f8554fa1efc173cd9afda01a35e738115c3d3be6f174a0d64c18eb2",
       "session_end": "2f3653f166adfb42bf865788ba775b22a0ce44c289e5f96a127af6c95ddbe663",
       "user_prompt_submit": "f0fd2a428e34ca63d793e255c100c622988941ba40a90386b0a978e2cf1e54ed"}
out=[]; cur=None
for line in t.split("\n"):
    m = re.match(r'\[hooks\.state\."[^"]*:(\w+):0:0"\]', line)
    if m: cur = m.group(1)
    if cur and line.startswith("trusted_hash"):
        line = f'trusted_hash = "sha256:{new[cur]}"'
    out.append(line)
open(p,"w").write("\n".join(out))
PY
    BUILDER_PULSE_INVITE_CODE="harness-invite-code-0004-review" run_installer setup --project-root "$P" --project-label "Harness Project"
    local_summary; server_summary
    echo "### member approves via /hooks (simulated), then activate"
    python3 "$I/trust_hooks.py" "$P" --approve | tail -1; activate codex; simulate_turn; activate codex; server_summary ;;
  repair-v050-marker)
    restore "$H/home.seed-v045" "$H/fake_server.state.seed-v045.json"
    echo "### member registered on v0.5.0 (188 KB commit) with marker file in cache, quarantined → v0.5.2 repair"
    (cd "$P" && codex plugin remove builder-pulse@growthx-builder-tools --json >/dev/null 2>&1; codex plugin marketplace remove growthx-builder-tools --json >/dev/null 2>&1; codex plugin marketplace add GrowthX-Club/builder-pulse-plugin --ref v0.5.0 --json >/dev/null 2>&1; codex plugin add builder-pulse@growthx-builder-tools --json >/dev/null 2>&1; codex plugin marketplace upgrade >/dev/null 2>&1)
    find "$HOME/.codex" -name .codex-marketplace-install.json | sed 's/^/  marker: /'
    run_installer repair --reuse-existing-claim
    local_summary; server_summary ;;
  repair-skeleton)
    restore "$H/home.seed-v045" "$H/fake_server.state.seed-v045.json"
    echo "### member on v0.4.5 with a stray unclaimed identity skeleton in ~/.builder-pulse (as on Udayan's Mac) → v0.5.2 --reuse-existing-claim must use the legacy claimed identity"
    mkdir -p "$HOME/.builder-pulse/runtime/0.5.0"
    printf '{"installationId": "8c8006e7-0000-4000-8000-000000000001", "promptCapture": "off"}\n' > "$HOME/.builder-pulse/identity.json"
    printf '{"installationId": "8c8006e7-0000-4000-8000-000000000001"}\n' > "$HOME/.builder-pulse/setup-paused-identity.json"
    printf '{"enabled": false}\n' > "$HOME/.builder-pulse/config.json"
    chmod 600 "$HOME/.builder-pulse"/*.json
    run_installer repair --reuse-existing-claim
    local_summary; server_summary ;;
  repair-legacy-only)
    restore "$H/home.seed-v045" "$H/fake_server.state.seed-v045.json"
    echo "### member still on v0.4.5 (identity only in the legacy Codex data dir, no ~/.builder-pulse) → v0.5.2 --reuse-existing-claim"
    ls -d "$HOME/.builder-pulse" 2>/dev/null && echo "UNEXPECTED shared dir" || echo "  (no shared dir, as expected)"
    run_installer repair --reuse-existing-claim
    local_summary; simulate_turn; activate codex; server_summary ;;
  fresh)
    rm -rf "$HOME"; mkdir -p "$HOME"; rm -f "$H/fake_server.log.jsonl" "$H/fake_server.state.json"; restart_server
    echo "### brand-new member, first setup"
    BUILDER_PULSE_INVITE_CODE="harness-invite-code-0005-fresh00" run_installer setup --project-root "$P" --project-label "Harness Project"
    local_summary; server_summary
    echo "### approve via /hooks (simulated), then a turn + activate"
    python3 "$I/trust_hooks.py" "$P" --approve | tail -1; simulate_turn; activate codex; server_summary ;;
  *) echo "unknown scenario"; exit 2 ;;
esac
