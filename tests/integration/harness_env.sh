# Shared environment for the Builder Pulse integration harness. Sourced by the
# scenario scripts; never run directly.
#   HARNESS_ROOT     writable scratch directory (isolated HOME, clones, logs, snapshots)
#   HARNESS_PROJECT  a real project folder to enroll (must not live under a temp dir)
I="$(cd "$(dirname "$0")" && pwd)"
H="${HARNESS_ROOT:?set HARNESS_ROOT to a writable scratch directory}"
P="${HARNESS_PROJECT:?set HARNESS_PROJECT to a real (non-temp) project folder}"
mkdir -p "$H/bin" "$H/logs" "$H/clones" "$P"
# The isolated PATH exposes only the tools the installer may use. `claude` is
# added per scenario so Codex-only and both-agent shapes can be exercised.
for tool in codex python3 git node perl; do
  if [ ! -e "$H/bin/$tool" ] && command -v "$tool" >/dev/null 2>&1; then
    ln -sf "$(command -v "$tool")" "$H/bin/$tool"
  fi
done
if [ ! -d "$P/.git" ]; then
  (cd "$P" && git init -q . && echo "# harness project" > README.md && git add README.md && git -c user.email=h@x -c user.name=h commit -qm init)
fi
export HOME="$H/home"
export PATH="$H/bin:/usr/bin:/bin:/usr/sbin:/sbin"
unset CODEX_HOME CLAUDE_CONFIG_DIR BUILDER_PULSE_DATA_DIR PLUGIN_DATA CLAUDE_PLUGIN_DATA BUILDER_PULSE_INVITE_CODE || true
clone_release() {  # clone_release <tag>: shallow clone of one immutable tag under $H/clones
  if [ ! -d "$H/clones/$1" ]; then
    git clone -q --depth 1 --branch "$1" --single-branch https://github.com/GrowthX-Club/builder-pulse-plugin.git "$H/clones/$1" 2>/dev/null
  fi
}
