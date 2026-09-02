#!/usr/bin/env python3
"""Harness-only shim: run an UNRELEASED installer checkout before its tag exists.

The shipped installer verifies an immutable GitHub release for TARGET_RELEASE
(e.g. v0.5.2) and installs that git ref. Before the tag exists we still want to
exercise the real code end to end, so this shim loads the checkout's setup
module and changes exactly two things:

1. verify_release_exists(TARGET_RELEASE) returns the checkout's commit SHA
   instead of querying the GitHub release API (the tag does not exist yet).
2. run_command rewrites the git refs the installer passes to the agent CLIs:
   `--ref v0.5.2` -> `--ref <sha>` for Codex (exactly what the installer's own
   rollback path does) and `@v0.5.2` -> `@<branch>` for Claude Code (which
   cannot clone a bare SHA). The branch tip must be the SHA; the installer's
   own provenance checks still compare the installed checkout HEAD to the SHA.

TARGET_RELEASE, plugin versions, server payloads and every other code path
are untouched.

usage: HARNESS_BRANCH=<branch> run_branch_installer.py <checkout> <sha> [installer args...]
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    checkout = Path(sys.argv[1]).resolve()
    sha = sys.argv[2]
    argv = sys.argv[3:]
    branch = os.environ.get("HARNESS_BRANCH")
    script = checkout / "scripts" / "setup_builder_pulse.py"
    spec = importlib.util.spec_from_file_location("setup_builder_pulse", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if head != sha:
        raise SystemExit(f"checkout HEAD {head} != requested {sha}")
    if branch:
        tip = subprocess.run(
            ["git", "ls-remote", module.REPOSITORY, f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if not tip or tip[0] != sha:
            raise SystemExit(f"origin/{branch} tip {tip[:1]} != {sha}")

    release = module.TARGET_RELEASE
    original_run_command = module.run_command

    def run_command(arguments, **kwargs):
        rewritten = []
        previous = None
        for argument in arguments:
            if isinstance(argument, str):
                if previous == "--ref" and argument == release:
                    argument = sha
                elif branch and argument.endswith(f"@{release}"):
                    argument = argument[: -len(release)] + branch
            rewritten.append(argument)
            previous = argument
        return original_run_command(rewritten, **kwargs)

    for name in ("verify_release", "verify_release_exists"):  # v0.6.0 / v0.5.x names
        if hasattr(module, name):
            setattr(module, name, lambda _release: sha)
    module.run_command = run_command
    print(
        f"[shim] running {script} as {release} from commit {sha[:12]}"
        + (f" (Claude ref: {branch})" if branch else ""),
        file=sys.stderr,
    )
    sys.argv = [str(script), *argv]
    return module.main()


if __name__ == "__main__":
    raise SystemExit(main())
