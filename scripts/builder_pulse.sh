#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for python_command in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$python_command" >/dev/null 2>&1; then
    if "$python_command" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
      exec "$python_command" "$script_dir/builder_pulse.py" hook
    fi
  fi
done

echo "Builder Pulse requires Python 3.11 or newer." >&2
exit 127
