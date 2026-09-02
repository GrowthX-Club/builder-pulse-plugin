#!/usr/bin/env python3
"""Loopback stand-in for the Builder Pulse Convex service.

Implements the six routes the plugin and installer call, with the same
status/body semantics as convex/http.ts, so the real installer and hook runtime
can be exercised end to end without touching production. Every request is
appended to a JSONL log (bearer tokens are hashed, never stored raw).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_PATH = os.environ.get("FAKE_SERVER_LOG", "fake_server.log.jsonl")
STATE_PATH = os.environ.get("FAKE_SERVER_STATE", "fake_server.state.json")
LOCK = threading.Lock()

STATE = {
    "installations": {},  # tokenHash -> record
    "events": [],
    "prompts": [],
}


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def load_state() -> None:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as handle:
            STATE.update(json.load(handle))


def save_state() -> None:
    with open(STATE_PATH, "w") as handle:
        json.dump(STATE, handle, indent=1)


def log(record: dict) -> None:
    record["at"] = time.time()
    with open(LOG_PATH, "a") as handle:
        handle.write(json.dumps(record) + "\n")


def supports_scope(version: str) -> bool:
    parts = version.lstrip("v").split(".")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return False
    return nums >= [0, 4, 6]


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-builder-pulse/1"

    def log_message(self, *_args):  # silence default logging
        pass

    def _json(self, body, status=200):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _bearer(self):
        auth = self.headers.get("authorization") or ""
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer "):].strip()
        return token or None

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = None
        token = self._bearer()
        token_hash = sha(token) if token else None
        with LOCK:
            status, response = self.route(self.path, body, token, token_hash)
            log(
                {
                    "route": self.path,
                    "status": status,
                    "tokenHash8": (token_hash or "")[:8],
                    "pluginVersion": (body or {}).get("pluginVersion") if isinstance(body, dict) else None,
                    "agentPlatform": (body or {}).get("agentPlatform") if isinstance(body, dict) else None,
                    "schemaVersion": (body or {}).get("schemaVersion") if isinstance(body, dict) else None,
                    "projectScope": (body or {}).get("projectScope") if isinstance(body, dict) else None,
                    "ua": self.headers.get("user-agent"),
                    "response": response,
                }
            )
            save_state()
        self._json(response, status)

    def route(self, path, body, token, token_hash):
        installs = STATE["installations"]
        if path == "/v1/claim":
            if not isinstance(body, dict):
                return 400, {"error": "invalid_request"}
            code = body.get("inviteCode")
            if not isinstance(code, str) or len(code) < 16:
                return 400, {"error": "invalid_request"}
            if code == "REJECT-THIS-INVITE-CODE":
                return 404, {"error": "invalid_invite"}
            inst_id = body.get("installationId")
            inst_token = body.get("installationToken")
            th = sha(inst_token)
            record = installs.get(th)
            if record is None:
                record = {
                    "installationId": inst_id,
                    "builderId": "builder_" + sha(code)[:10],
                    "memberId": "member_" + sha(code)[:8],
                    "name": "Harness Member",
                    "pluginVersion": body.get("pluginVersion"),
                    "privacyPausedAt": None,
                    "privacyResumedAt": None,
                    "privacyResumedPluginVersion": None,
                    "lastReceivedAt": None,
                    "lastSignalPluginVersion": None,
                    "lastSignalAgentPlatform": None,
                    "hooksVerifiedAt": {},
                }
                installs[th] = record
            return 200, {
                "accepted": True,
                "builderId": record["builderId"],
                "memberId": record["memberId"],
                "name": record["name"],
                "defaultProject": None,
                "heartbeatMinutes": 15,
                "promptCapture": "on",
            }

        if token_hash is None or token_hash not in installs:
            return 401, {"error": "unauthorized"}
        record = installs[token_hash]

        if path in ("/v1/privacy-pause", "/v1/privacy-resume"):
            if not isinstance(body, dict):
                return 400, {"error": "invalid_request"}
            if body.get("installationId") != record["installationId"]:
                return 403, {"error": "installation_mismatch"}
            version = str(body.get("pluginVersion") or "")
            if path.endswith("pause"):
                record["privacyPausedAt"] = time.time()
                return 200, {"paused": True, "installationId": record["installationId"]}
            if not supports_scope(version):
                return 400, {"error": "invalid_request"}
            record["privacyPausedAt"] = None
            record["privacyResumedAt"] = time.time()
            record["privacyResumedPluginVersion"] = version
            return 200, {"resumed": True, "installationId": record["installationId"]}

        if path == "/v1/activation":
            if not isinstance(body, dict) or body.get("schemaVersion") != 1:
                return 400, {"error": "invalid_request"}
            if body.get("installationId") != record["installationId"]:
                return 403, {"error": "installation_mismatch"}
            if record.get("privacyPausedAt"):
                return 403, {"error": "privacy_pause_active"}
            platform = body.get("agentPlatform") or "codex"
            version = body.get("pluginVersion")
            previous = record["hooksVerifiedAt"].get(platform)
            last = record.get("lastReceivedAt")
            since = bool(
                last
                and previous
                and last > previous
                and record.get("lastSignalPluginVersion") == version
                and record.get("lastSignalAgentPlatform") == platform
            )
            record["hooksVerifiedAt"][platform] = time.time()
            record["pluginVersion"] = version
            return 200, {
                "accepted": True,
                "telemetryReceived": bool(last),
                "telemetryReceivedSincePreviousActivation": since,
                "lastSignalAt": int(last * 1000) if last else None,
                "lastSignalPluginVersion": record.get("lastSignalPluginVersion"),
                "lastSignalAgentPlatform": record.get("lastSignalAgentPlatform"),
            }

        if path in ("/v1/telemetry", "/v1/prompts"):
            if not isinstance(body, dict):
                return 400, {"error": "invalid_request"}
            if record.get("privacyPausedAt"):
                return 403, {"error": "privacy_pause_active"}
            if body.get("projectScope") != "explicit" or not body.get("projectLabel"):
                return 403, {"error": "explicit_project_scope_required"}
            bucket = STATE["events" if path.endswith("telemetry") else "prompts"]
            ids = {e.get("id") for e in bucket}
            event_id = body.get("eventId") or body.get("promptId")
            duplicate = event_id in ids
            if not duplicate:
                bucket.append(
                    {
                        "id": event_id,
                        "pluginVersion": body.get("pluginVersion"),
                        "agentPlatform": body.get("agentPlatform"),
                        "state": body.get("state"),
                        "hasTokenUsage": "tokenUsage" in body,
                        "promptLength": len(body.get("promptText") or "") if path.endswith("prompts") else None,
                    }
                )
            record["lastReceivedAt"] = time.time()
            record["lastSignalPluginVersion"] = body.get("pluginVersion")
            record["lastSignalAgentPlatform"] = body.get("agentPlatform") or "codex"
            return 200, {"accepted": True, "duplicate": duplicate}

        return 404, {"error": "not_found"}


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    load_state()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"fake builder pulse server on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
