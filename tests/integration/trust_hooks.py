#!/usr/bin/env python3
"""Simulate the member approving Codex's one-time hook review.

Reads hooks/list from the local app-server (same handshake the plugin uses),
prints the Builder Pulse hook trust statuses, and, when --approve is given,
writes hooks.state.<key>.trusted_hash = currentHash into config.toml exactly
like the Codex TUI's write_hook_trusts does (config/batchWrite upsert).
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PLUGIN_ID = "builder-pulse@growthx-builder-tools"


def hooks_list(cwd: str, timeout: float = 30.0):
    codex = shutil.which("codex")
    p = subprocess.Popen(
        [codex, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    q: "queue.Queue[str | None]" = queue.Queue()

    def rd():
        for line in p.stdout:
            q.put(line)
        q.put(None)

    threading.Thread(target=rd, daemon=True).start()

    def send(m):
        p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()

    def recv(i):
        dl = time.time() + timeout
        while time.time() < dl:
            try:
                line = q.get(timeout=max(0.01, dl - time.time()))
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                r = json.loads(line)
            except Exception:
                continue
            if isinstance(r, dict) and r.get("id") == i:
                return r
        return None

    t0 = time.time()
    send({"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "harness", "title": "Harness", "version": "0"}, "capabilities": {}}})
    init = recv(1)
    send({"method": "initialized", "params": {}})
    send({"method": "hooks/list", "id": 2, "params": {"cwds": [cwd]}})
    resp = recv(2)
    elapsed = time.time() - t0
    p.stdin.close()
    p.terminate()
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        p.kill()
    err = p.stderr.read()
    return init, resp, elapsed, err


def main():
    cwd = sys.argv[1]
    approve = "--approve" in sys.argv
    init, resp, elapsed, err = hooks_list(cwd)
    print(f"init ok={bool(init and 'result' in init)} elapsed={elapsed:.2f}s")
    if not resp or "result" not in resp:
        print("hooks/list RAW:", json.dumps(resp)[:1500])
        print("STDERR:", err[-2000:])
        return 1
    entry = resp["result"]["data"][0]
    print("errors:", entry["errors"], "warnings:", entry["warnings"])
    bp = [h for h in entry["hooks"] if h.get("pluginId") == PLUGIN_ID]
    print(f"builder-pulse hooks: {len(bp)}")
    for h in bp:
        print(f"  {h['eventName']:<18} trust={h['trustStatus']:<10} enabled={h['enabled']} hash={h['currentHash'][7:19]} src={h['sourcePath']}")
    if approve and bp:
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        config = codex_home / "config.toml"
        text = config.read_text() if config.exists() else ""
        for h in bp:
            key = h["key"]
            block = f'\n[hooks.state."{key}"]\ntrusted_hash = "{h["currentHash"]}"\n'
            if f'[hooks.state."{key}"]' in text:
                # replace existing trusted_hash line for this key
                lines = text.split("\n")
                out = []
                in_block = False
                for line in lines:
                    if line.strip() == f'[hooks.state."{key}"]':
                        in_block = True
                        out.append(line)
                        continue
                    if in_block and line.startswith("["):
                        in_block = False
                    if in_block and line.startswith("trusted_hash"):
                        line = f'trusted_hash = "{h["currentHash"]}"'
                    out.append(line)
                text = "\n".join(out)
            else:
                text += block
        config.write_text(text)
        print("wrote trust for", len(bp), "hooks into", config)
        _, resp2, _, _ = hooks_list(cwd)
        bp2 = [h for h in resp2["result"]["data"][0]["hooks"] if h.get("pluginId") == PLUGIN_ID]
        print("after approve:", sorted({h["trustStatus"] for h in bp2}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
