#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export BUILDER_PULSE_AGENT_PLATFORM=codex
export BUILDER_PULSE_PLUGIN_VERSION=0.5.1
for python_command in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$python_command" >/dev/null 2>&1; then
    if "$python_command" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
      "$python_command" "$script_dir/builder_pulse.py" hook >/dev/null 2>&1 || :
      printf '{}\n'
      exit 0
    fi
  fi
done

printf '{}\n'
exit 0
