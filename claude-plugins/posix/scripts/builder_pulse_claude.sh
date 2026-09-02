#!/bin/sh
set -eu

runtime_dir=${BUILDER_PULSE_RUNTIME_DIR:-"$HOME/.builder-pulse/runtime/0.5.3"}
runtime_script="$runtime_dir/scripts/builder_pulse.py"
if [ ! -f "$runtime_script" ]; then
  printf '{}\n'
  exit 0
fi

export BUILDER_PULSE_AGENT_PLATFORM=claude_code
export BUILDER_PULSE_PLUGIN_VERSION=0.5.3
for python_command in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$python_command" >/dev/null 2>&1; then
    if "$python_command" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
      "$python_command" "$runtime_script" hook >/dev/null 2>&1 || :
      printf '{}\n'
      exit 0
    fi
  fi
done

printf '{}\n'
exit 0
