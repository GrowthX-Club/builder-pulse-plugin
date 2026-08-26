from __future__ import annotations

import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "builder_pulse.py"
SPEC = importlib.util.spec_from_file_location("builder_pulse", SCRIPT)
assert SPEC and SPEC.loader
builder_pulse = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder_pulse)


class FakeResponse:
    def __init__(self, body: dict | None = None, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(body or {}).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        if amount == 0:
            return b""
        return self._body if amount < 0 else self._body[:amount]


class BuilderPulseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.config = builder_pulse.load_config(self.data_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def claim_locally(self, endpoint: str = "https://pulse.example") -> dict:
        identity = builder_pulse.ensure_identity(self.data_dir)
        identity.update(
            {
                "builderId": "builder-1",
                "builderName": "Builder One",
                "installationToken": "a" * 64,
                "claimedEndpoint": endpoint,
            }
        )
        builder_pulse.atomic_write_json(
            builder_pulse.identity_path(self.data_dir), identity
        )
        builder_pulse.atomic_write_json(
            self.data_dir / "config.json",
            {
                "endpoint": endpoint,
                "project_id": "product-alpha",
                "feature_id": "member-search",
                "feature_label": "Member search",
            },
        )
        self.config = builder_pulse.load_config(self.data_dir)
        return identity

    def record(self, payload: dict, now_ms: int) -> dict | None:
        with mock.patch.object(builder_pulse, "utc_now_ms", return_value=now_ms):
            return builder_pulse.record_hook_event(
                self.data_dir, payload, self.config
            )

    def test_claim_uses_exact_contract_and_never_prints_token(self) -> None:
        response = {
            "builderId": "builder-17",
            "name": "Asha Builder",
            "defaultProject": "community-app",
            "heartbeatMinutes": 15,
            "promptCapture": "off",
        }
        args = argparse.Namespace(
            endpoint="https://pulse.example", code="one-time-invite"
        )
        output = io.StringIO()
        with mock.patch.object(
            builder_pulse.urlrequest,
            "urlopen",
            return_value=FakeResponse(response),
        ) as opened, contextlib.redirect_stdout(output):
            result = builder_pulse.command_claim(args, self.data_dir)

        self.assertEqual(result, 0)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://pulse.example/v1/claim")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            set(body),
            {
                "schemaVersion",
                "inviteCode",
                "installationId",
                "installationToken",
                "pluginVersion",
            },
        )
        self.assertEqual(body["inviteCode"], "one-time-invite")
        self.assertRegex(body["installationToken"], r"^[0-9a-f]{64}$")
        self.assertNotIn(body["installationToken"], output.getvalue())
        self.assertNotIn("one-time-invite", output.getvalue())
        identity = builder_pulse.read_json(
            builder_pulse.identity_path(self.data_dir), {}
        )
        self.assertEqual(identity["builderId"], "builder-17")
        self.assertEqual(identity["builderName"], "Asha Builder")
        self.assertEqual(identity["installationToken"], body["installationToken"])
        self.assertEqual(identity["claimedEndpoint"], "https://pulse.example")
        self.assertNotIn("pendingInstallationToken", identity)
        if os.name != "nt":
            self.assertEqual(
                builder_pulse.identity_path(self.data_dir).stat().st_mode & 0o777,
                0o600,
            )
        config = builder_pulse.load_config(self.data_dir)
        self.assertEqual(config["project_id"], "community-app")

    def test_existing_project_wins_over_claim_default(self) -> None:
        builder_pulse.atomic_write_json(
            self.data_dir / "config.json", {"project_id": "explicit-product"}
        )
        response = {
            "builderId": "builder-17",
            "name": "Asha Builder",
            "defaultProject": "server-default",
            "heartbeatMinutes": 15,
            "promptCapture": "off",
        }
        args = argparse.Namespace(
            endpoint="https://pulse.example", code="one-time-invite"
        )
        with mock.patch.object(
            builder_pulse.urlrequest,
            "urlopen",
            return_value=FakeResponse(response),
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder_pulse.command_claim(args, self.data_dir), 0)
        self.assertEqual(
            builder_pulse.load_config(self.data_dir)["project_id"],
            "explicit-product",
        )

    def test_claim_timeout_reuses_the_persisted_pending_token(self) -> None:
        response = {
            "builderId": "builder-17",
            "name": "Asha Builder",
            "defaultProject": None,
            "heartbeatMinutes": 15,
            "promptCapture": "off",
        }
        args = argparse.Namespace(
            endpoint="https://pulse.example", code="one-time-invite"
        )
        with mock.patch.object(
            builder_pulse.urlrequest,
            "urlopen",
            side_effect=builder_pulse.urlerror.URLError("response lost"),
        ), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(builder_pulse.command_claim(args, self.data_dir), 1)
        pending = builder_pulse.read_json(builder_pulse.identity_path(self.data_dir), {})
        pending_token = pending["pendingInstallationToken"]
        self.assertRegex(pending_token, r"^[0-9a-f]{64}$")

        with mock.patch.object(
            builder_pulse.urlrequest, "urlopen", return_value=FakeResponse(response)
        ) as opened, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder_pulse.command_claim(args, self.data_dir), 0)
        retry_body = json.loads(opened.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(retry_body["installationToken"], pending_token)
        claimed = builder_pulse.read_json(builder_pulse.identity_path(self.data_dir), {})
        self.assertEqual(claimed["installationToken"], pending_token)

    def test_concurrent_first_claims_serialize_and_reuse_pending_token(self) -> None:
        requests: list[dict] = []
        requests_lock = threading.Lock()
        second_request_seen = threading.Event()
        first_request_overlapped: list[bool] = []

        class ClaimHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                with requests_lock:
                    request_index = len(requests)
                    requests.append(payload)

                if request_index == 0:
                    first_request_overlapped.append(second_request_seen.wait(1.0))
                    self.close_connection = True
                    return

                second_request_seen.set()
                response = json.dumps(
                    {
                        "builderId": "builder-17",
                        "name": "Asha Builder",
                        "defaultProject": None,
                        "heartbeatMinutes": 15,
                        "promptCapture": "off",
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), ClaimHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        command = [
            sys.executable,
            str(SCRIPT),
            "--data-dir",
            str(self.data_dir),
            "claim",
            "--endpoint",
            endpoint,
            "--code",
            "one-time-invite",
        ]

        processes: list[subprocess.Popen[str]] = []
        try:
            for _ in range(2):
                processes.append(
                    subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            results = [process.communicate(timeout=10) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertEqual(sorted(process.returncode for process in processes), [0, 1])
        self.assertEqual(len(requests), 2)
        self.assertEqual(first_request_overlapped, [False])
        self.assertEqual(requests[0]["installationId"], requests[1]["installationId"])
        self.assertEqual(
            requests[0]["installationToken"], requests[1]["installationToken"]
        )
        token = requests[0]["installationToken"]
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        identity = builder_pulse.read_json(builder_pulse.identity_path(self.data_dir), {})
        self.assertEqual(identity["installationToken"], token)
        self.assertEqual(identity["builderId"], "builder-17")
        combined_output = "".join(
            stdout + stderr for stdout, stderr in results
        )
        self.assertNotIn(token, combined_output)

    def test_claim_refuses_to_move_an_existing_identity_to_another_endpoint(self) -> None:
        self.claim_locally()
        args = argparse.Namespace(
            endpoint="https://other.example", code="another-invite"
        )
        with mock.patch.object(builder_pulse.urlrequest, "urlopen") as opened, \
            contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(builder_pulse.command_claim(args, self.data_dir), 2)
        opened.assert_not_called()

    def test_telemetry_payload_is_exact_and_authorized(self) -> None:
        identity = self.claim_locally()
        event = self.record(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-one",
                "prompt": "raw prompt must disappear",
                "cwd": "/private/product-alpha",
            },
            1_787_721_000_000,
        )
        assert event is not None
        self.assertEqual(
            set(event),
            {
                "schemaVersion",
                "eventId",
                "installationId",
                "sessionKey",
                "projectId",
                "featureId",
                "featureLabel",
                "state",
                "occurredAt",
                "pluginVersion",
            },
        )
        self.assertIsInstance(event["occurredAt"], int)
        self.assertEqual(event["installationId"], identity["installationId"])

        with mock.patch.object(
            builder_pulse.urlrequest,
            "urlopen",
            return_value=FakeResponse(),
        ) as opened:
            ok, result = builder_pulse.deliver_event(
                event, self.config, identity["installationToken"]
            )
        self.assertTrue(ok)
        self.assertEqual(result, "delivered")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://pulse.example/v1/telemetry")
        self.assertEqual(
            request.get_header("Authorization"),
            f"Bearer {'a' * 64}",
        )
        self.assertEqual(json.loads(request.data.decode("utf-8")), event)

    def test_raw_sensitive_values_and_prompt_length_are_never_persisted(self) -> None:
        self.claim_locally()
        markers = [
            "secret-customer-prompt",
            "secret-shell-token",
            "secret-source-output",
            "/private/secret-parent",
        ]
        self.record(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-one",
                "prompt": markers[0],
                "cwd": "/private/secret-parent/product-alpha",
                "source": "private source patch",
            },
            1_787_721_000_000,
        )
        event = self.record(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-one",
                "tool_name": "Bash",
                "tool_input": {"command": f"pytest --token {markers[1]}"},
                "tool_response": {"exit_code": 0, "output": markers[2]},
                "cwd": "/private/secret-parent/product-alpha",
            },
            1_787_721_001_000,
        )
        assert event is not None
        self.assertEqual(event["state"], "testing")
        self.assertNotIn("promptLength", event)

        persisted = ""
        for path in self.data_dir.rglob("*"):
            if path.is_file() and path.name != ".lock":
                persisted += path.read_text(encoding="utf-8", errors="ignore")
        for marker in markers:
            self.assertNotIn(marker, persisted)
        self.assertNotIn("private source patch", persisted)
        self.assertNotIn("promptLength", persisted)

    def test_same_state_under_heartbeat_does_not_emit_or_queue(self) -> None:
        self.claim_locally()
        first = self.record(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-one",
                "prompt": "first private prompt",
            },
            1_787_721_000_000,
        )
        second = self.record(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-one",
                "prompt": "second private prompt",
            },
            1_787_721_100_000,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        queued = builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl")
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["eventId"], first["eventId"])

    def test_long_inactivity_never_creates_an_active_interval(self) -> None:
        self.claim_locally()
        start = 1_787_721_000_000
        self.record(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-one",
            },
            start,
        )
        heartbeat = self.record(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-one",
            },
            start + 30 * 60 * 1000,
        )
        assert heartbeat is not None
        self.assertNotIn("activeFrom", heartbeat)

    def test_observed_continuous_activity_emits_a_capped_interval(self) -> None:
        self.claim_locally()
        start = 1_787_721_000_000
        first = self.record(
            {"hook_event_name": "SessionStart", "session_id": "session-one"}, start
        )
        assert first is not None
        self.assertNotIn("activeFrom", first)
        for offset in (5, 10):
            self.assertIsNone(
                self.record(
                    {"hook_event_name": "UserPromptSubmit", "session_id": "session-one"},
                    start + offset * 60 * 1000,
                )
            )
        heartbeat = self.record(
            {"hook_event_name": "UserPromptSubmit", "session_id": "session-one"},
            start + 15 * 60 * 1000,
        )
        assert heartbeat is not None
        self.assertEqual(heartbeat["activeFrom"], start)

    def test_retry_keeps_event_id_stable_until_success(self) -> None:
        self.claim_locally()
        event = self.record(
            {
                "hook_event_name": "SessionStart",
                "session_id": "session-one",
            },
            1_787_721_000_000,
        )
        assert event is not None
        original_id = event["eventId"]

        with mock.patch.object(
            builder_pulse, "deliver_event", return_value=(False, "network_error")
        ):
            result = builder_pulse.flush_outbox(self.data_dir, self.config)
        self.assertEqual(result, {"delivered": 0, "quarantined": 0, "remaining": 1})
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl")[0]["eventId"],
            original_id,
        )

        delivered: list[str] = []

        def succeed(
            payload: dict, config: dict, token: str, endpoint: str
        ) -> tuple[bool, str]:
            delivered.append(payload["eventId"])
            return True, "delivered"

        with mock.patch.object(builder_pulse, "deliver_event", side_effect=succeed):
            result = builder_pulse.flush_outbox(self.data_dir, self.config)
        self.assertEqual(delivered, [original_id])
        self.assertEqual(result, {"delivered": 1, "quarantined": 0, "remaining": 0})

    def test_session_end_emits_idle(self) -> None:
        self.claim_locally()
        self.record(
            {
                "hook_event_name": "SessionStart",
                "session_id": "session-one",
            },
            1_787_721_000_000,
        )
        ended = self.record(
            {
                "hook_event_name": "SessionEnd",
                "session_id": "session-one",
            },
            1_787_721_100_000,
        )
        assert ended is not None
        self.assertEqual(ended["state"], "idle")
        self.assertEqual(ended["activeFrom"], 1_787_721_000_000)

    def test_feature_validation_and_sanitized_id(self) -> None:
        args = argparse.Namespace(
            work_command="set",
            project="community-app",
            feature="Member Search Filters",
            feature_id=None,
            root=str(self.data_dir),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder_pulse.command_work(args, self.data_dir), 0)
        context = builder_pulse.load_work_contexts(self.data_dir)[
            builder_pulse.repository_key(self.data_dir)
        ]
        self.assertEqual(context["feature_label"], "Member Search Filters")
        self.assertEqual(context["feature_id"], "member-search-filters")
        with self.assertRaises(ValueError):
            builder_pulse.validate_feature_label("x" * 121)

    def test_work_context_is_scoped_by_repository(self) -> None:
        first_root = self.data_dir / "first"
        second_root = self.data_dir / "second"
        (first_root / ".git").mkdir(parents=True)
        (second_root / ".git").mkdir(parents=True)
        first_args = argparse.Namespace(
            work_command="set",
            project="product-one",
            feature="Feature one",
            feature_id=None,
            root=str(first_root),
        )
        second_args = argparse.Namespace(
            work_command="set",
            project="product-two",
            feature="Feature two",
            feature_id=None,
            root=str(second_root),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder_pulse.command_work(first_args, self.data_dir), 0)
            self.assertEqual(builder_pulse.command_work(second_args, self.data_dir), 0)
        contexts = builder_pulse.load_work_contexts(self.data_dir)
        self.assertEqual(len(contexts), 2)
        first_key = builder_pulse.repository_key(first_root)
        second_key = builder_pulse.repository_key(second_root)
        self.assertEqual(contexts[first_key]["feature_label"], "Feature one")
        self.assertEqual(contexts[second_key]["feature_label"], "Feature two")
        persisted = (self.data_dir / "contexts.json").read_text(encoding="utf-8")
        self.assertNotIn(str(first_root), persisted)
        self.assertNotIn(str(second_root), persisted)

    def test_permanent_client_error_is_quarantined_without_starving_queue(self) -> None:
        self.claim_locally()
        event = self.record(
            {"hook_event_name": "SessionStart", "session_id": "session-one"},
            1_787_721_000_000,
        )
        assert event is not None
        with mock.patch.object(
            builder_pulse, "deliver_event", return_value=(False, "http_422")
        ):
            result = builder_pulse.flush_outbox(self.data_dir, self.config)
        self.assertEqual(result, {"delivered": 0, "quarantined": 1, "remaining": 0})
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "quarantine.jsonl")[0]["eventId"],
            event["eventId"],
        )

    def test_concurrent_flush_returns_busy_without_duplicate_delivery(self) -> None:
        self.claim_locally()
        self.record(
            {"hook_event_name": "SessionStart", "session_id": "session-one"},
            1_787_721_000_000,
        )
        with builder_pulse.delivery_lease(self.data_dir) as acquired:
            self.assertTrue(acquired)
            with mock.patch.object(builder_pulse, "deliver_event") as delivered:
                result = builder_pulse.flush_outbox(self.data_dir, self.config)
        self.assertEqual(result["busy"], 1)
        delivered.assert_not_called()

    def test_burst_of_same_state_hooks_emits_only_once(self) -> None:
        self.claim_locally()
        start = 1_787_721_000_000
        began = time.monotonic()
        emitted = 0
        for offset in range(250):
            event = self.record(
                {"hook_event_name": "UserPromptSubmit", "session_id": "burst"},
                start + offset,
            )
            emitted += event is not None
        elapsed = time.monotonic() - began
        self.assertEqual(emitted, 1)
        self.assertEqual(len(builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl")), 1)
        self.assertLess(elapsed, 2.5)

    def test_endpoint_rejects_embedded_secrets(self) -> None:
        for endpoint in (
            "https://user:password@pulse.example",
            "https://pulse.example?token=secret",
            "https://pulse.example#secret",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    builder_pulse.validate_endpoint(endpoint)

    def test_endpoint_requires_https_except_loopback(self) -> None:
        with self.assertRaises(ValueError):
            builder_pulse.validate_endpoint("http://pulse.example")
        self.assertEqual(
            builder_pulse.validate_endpoint("http://localhost:3210/"),
            "http://localhost:3210",
        )
        self.assertEqual(
            builder_pulse.validate_endpoint("http://[::1]:3210"),
            "http://[::1]:3210",
        )

    def test_config_refuses_endpoint_change_after_claim(self) -> None:
        self.claim_locally()
        args = argparse.Namespace(
            config_command="set", key="endpoint", value="https://other.example"
        )
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(builder_pulse.command_config(args, self.data_dir), 2)


if __name__ == "__main__":
    unittest.main()
