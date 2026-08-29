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
import uuid
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
        self.workspace = Path(self.temp.name)
        self.data_dir = self.workspace / "plugin-data"
        self.codex_home = self.workspace / "codex-home"
        self.data_dir.mkdir()
        (self.codex_home / "sessions").mkdir(parents=True)
        self.environment = mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.codex_home)}
        )
        self.environment.start()
        self.config = builder_pulse.load_config(self.data_dir)
        self.primary_transcript = self.write_transcript(
            "primary-session.jsonl",
            {"type": "session_meta", "payload": {"source": "cli"}},
        )

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def token_totals(self, **overrides: object) -> dict[str, object]:
        totals: dict[str, object] = {
            "input_tokens": 120,
            "cached_input_tokens": 30,
            "cache_write_input_tokens": 10,
            "output_tokens": 40,
            "reasoning_output_tokens": 12,
            "total_tokens": 160,
        }
        totals.update(overrides)
        return totals

    def token_count_record(
        self,
        totals: dict[str, object] | None = None,
        last: dict[str, object] | None = None,
    ) -> dict:
        return {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": totals or self.token_totals(),
                    "last_token_usage": last or self.token_totals(),
                },
            },
        }

    def write_transcript(self, name: str, *records: dict) -> Path:
        path = self.codex_home / "sessions" / "2026" / "08" / "28" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    def claim_locally(self, endpoint: str = "https://pulse.example") -> dict:
        identity = builder_pulse.ensure_identity(self.data_dir)
        identity.update(
            {
                "builderId": "builder-1",
                "builderName": "Builder One",
                "installationToken": "a" * 64,
                "claimedEndpoint": endpoint,
                "promptCapture": "on",
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

    def record_prompt(
        self, payload: dict, now_ms: int, *, add_primary_transcript: bool = True
    ) -> dict | None:
        hook_payload = dict(payload)
        if (
            add_primary_transcript
            and hook_payload.get("hook_event_name") == "UserPromptSubmit"
            and "transcript_path" not in hook_payload
        ):
            hook_payload["transcript_path"] = str(self.primary_transcript)
        with mock.patch.object(builder_pulse, "utc_now_ms", return_value=now_ms):
            return builder_pulse.record_prompt_event(
                self.data_dir, hook_payload, self.config
            )

    def test_claim_uses_exact_contract_and_never_prints_token(self) -> None:
        response = {
            "builderId": "builder-17",
            "memberId": "growthx-member-17",
            "name": "Asha Builder",
            "defaultProject": "community-app",
            "heartbeatMinutes": 15,
            "promptCapture": "on",
        }
        args = argparse.Namespace(
            endpoint="https://pulse.example", code="one-time-invite"
        )
        output = io.StringIO()
        disclosure = io.StringIO()
        with mock.patch.object(
            builder_pulse.urlrequest,
            "urlopen",
            return_value=FakeResponse(response),
        ) as opened, contextlib.redirect_stdout(output), contextlib.redirect_stderr(
            disclosure
        ):
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
        self.assertEqual(body["schemaVersion"], 2)
        self.assertEqual(disclosure.getvalue().strip(), builder_pulse.SETUP_DISCLOSURE)
        self.assertRegex(body["installationToken"], r"^[0-9a-f]{64}$")
        self.assertNotIn(body["installationToken"], output.getvalue())
        self.assertNotIn("one-time-invite", output.getvalue())
        identity = builder_pulse.read_json(
            builder_pulse.identity_path(self.data_dir), {}
        )
        self.assertEqual(identity["builderId"], "builder-17")
        self.assertEqual(identity["memberId"], "growthx-member-17")
        self.assertEqual(identity["builderName"], "Asha Builder")
        self.assertEqual(identity["installationToken"], body["installationToken"])
        self.assertEqual(identity["claimedEndpoint"], "https://pulse.example")
        self.assertEqual(identity["promptCapture"], "on")
        self.assertNotIn("pendingInstallationToken", identity)
        if os.name != "nt":
            self.assertEqual(
                builder_pulse.identity_path(self.data_dir).stat().st_mode & 0o777,
                0o600,
            )
        config = builder_pulse.load_config(self.data_dir)
        self.assertEqual(config["project_id"], "community-app")

    def test_member_id_validation_is_strict(self) -> None:
        self.assertEqual(
            builder_pulse.validate_member_id("member_17:cohort-a"),
            "member_17:cohort-a",
        )
        for invalid in (None, "", " member-17", "member 17", "member/17", "x" * 129):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    builder_pulse.validate_member_id(invalid)

    def test_installed_cli_derives_the_same_marketplace_data_directory_as_hooks(self) -> None:
        root = Path("/tmp/codex/plugins/cache/growthx-builder-tools/builder-pulse/0.4.0")
        with mock.patch.object(builder_pulse, "PLUGIN_ROOT", root), mock.patch.dict(
            os.environ,
            {"BUILDER_PULSE_DATA_DIR": "", "PLUGIN_DATA": "", "CLAUDE_PLUGIN_DATA": ""},
            clear=False,
        ):
            self.assertEqual(
                builder_pulse.resolve_data_dir(),
                Path("/tmp/codex/plugins/data/builder-pulse-growthx-builder-tools"),
            )

    def test_session_end_queues_without_network_flush(self) -> None:
        self.claim_locally()
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "primary-session",
            "cwd": str(self.workspace),
        }
        stdin = io.StringIO(json.dumps(payload))
        with mock.patch("sys.stdin", stdin), mock.patch.object(
            builder_pulse, "flush_outbox"
        ) as telemetry_flush, mock.patch.object(
            builder_pulse, "flush_prompt_outbox"
        ) as prompt_flush, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder_pulse.ingest_hook(self.data_dir), 0)
        telemetry_flush.assert_not_called()
        prompt_flush.assert_not_called()

    def test_existing_project_wins_over_claim_default(self) -> None:
        builder_pulse.atomic_write_json(
            self.data_dir / "config.json", {"project_id": "explicit-product"}
        )
        response = {
            "builderId": "builder-17",
            "memberId": "growthx-member-17",
            "name": "Asha Builder",
            "defaultProject": "server-default",
            "heartbeatMinutes": 15,
            "promptCapture": "on",
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
            "memberId": "growthx-member-17",
            "name": "Asha Builder",
            "defaultProject": None,
            "heartbeatMinutes": 15,
            "promptCapture": "on",
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
        first_request_seen = threading.Event()
        release_first_request = threading.Event()

        class ClaimHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                with requests_lock:
                    request_index = len(requests)
                    requests.append(payload)

                if request_index == 0:
                    first_request_seen.set()
                    release_first_request.wait(5.0)
                    self.close_connection = True
                    return

                response = json.dumps(
                    {
                        "builderId": "builder-17",
                        "memberId": "growthx-member-17",
                        "name": "Asha Builder",
                        "defaultProject": None,
                        "heartbeatMinutes": 15,
                        "promptCapture": "on",
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
            processes.append(
                subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            self.assertTrue(first_request_seen.wait(5.0))
            processes.append(
                subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            release_first_request.set()
            results = [process.communicate(timeout=10) for process in processes]
        finally:
            release_first_request.set()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertEqual(sorted(process.returncode for process in processes), [0, 1])
        self.assertEqual(len(requests), 2)
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

    def test_primary_user_prompt_payload_is_exact_separate_and_authorized(self) -> None:
        identity = self.claim_locally()
        prompt_text = "Help me design a member search experience."
        event = self.record_prompt(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "prompt-session",
                "prompt": prompt_text,
                "cwd": "/private/product-alpha",
                "tool_input": {"command": "must not be copied"},
                "assistant_response": "must not be copied",
            },
            1_787_721_000_000,
        )
        assert event is not None
        self.assertEqual(
            set(event),
            {
                "schemaVersion",
                "promptId",
                "installationId",
                "sessionKey",
                "projectId",
                "featureId",
                "featureLabel",
                "promptText",
                "occurredAt",
                "pluginVersion",
                "redacted",
                "truncated",
            },
        )
        self.assertEqual(event["schemaVersion"], 1)
        self.assertEqual(event["promptText"], prompt_text)
        self.assertFalse(event["redacted"])
        self.assertFalse(event["truncated"])
        self.assertEqual(uuid.UUID(event["promptId"]).version, 4)
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "prompt-outbox.jsonl"),
            [event],
        )
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl"), []
        )
        serialized = json.dumps(event)
        self.assertNotIn("must not be copied", serialized)
        self.assertNotIn("tool_input", serialized)
        self.assertNotIn("assistant_response", serialized)

        with mock.patch.object(
            builder_pulse.urlrequest, "urlopen", return_value=FakeResponse()
        ) as opened:
            ok, result = builder_pulse.deliver_prompt(
                event, self.config, identity["installationToken"]
            )
        self.assertTrue(ok)
        self.assertEqual(result, "delivered")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://pulse.example/v1/prompts")
        self.assertEqual(
            request.get_header("Authorization"), f"Bearer {'a' * 64}"
        )
        self.assertEqual(json.loads(request.data.decode("utf-8")), event)

    def test_prompt_redaction_is_high_confidence_and_persisted_secrets_are_absent(self) -> None:
        self.claim_locally()
        secrets_to_remove = (
            "private-key-secret-material",
            "Basic dXNlcjpwYXNzd29yZA==",
            "bearer-token-value-1234567890",
            "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            "ghp_abcdefghijklmnopqrstuvwxyz123456",
            "AKIA1234567890ABCDEF",
            "AIza1234567890abcdefghijklmnopqrstuvwxy",
            "xoxb-1234567890-abcdefghijklmnop",
        )
        prompt_text = "\n".join(
            (
                "Keep ordinary project-123 and the word bearer unchanged.",
                "-----BEGIN PRIVATE KEY-----\n"
                f"{secrets_to_remove[0]}\n"
                "-----END PRIVATE KEY-----",
                f"Authorization: {secrets_to_remove[1]}",
                f"Use Bearer {secrets_to_remove[2]}",
                *secrets_to_remove[3:],
            )
        )
        event = self.record_prompt(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "redaction-session",
                "prompt": prompt_text,
            },
            1_787_721_010_000,
        )
        assert event is not None
        self.assertTrue(event["redacted"])
        self.assertFalse(event["truncated"])
        self.assertIn("ordinary project-123", event["promptText"])
        self.assertIn("the word bearer unchanged", event["promptText"])
        persisted = (self.data_dir / "prompt-outbox.jsonl").read_text(
            encoding="utf-8"
        )
        for secret in secrets_to_remove:
            self.assertNotIn(secret, event["promptText"])
            self.assertNotIn(secret, persisted)

    def test_prompt_utf8_truncation_never_splits_a_character(self) -> None:
        self.claim_locally()
        prompt_text = "a" * (builder_pulse.PROMPT_MAX_BYTES - 1) + "🔥tail"
        event = self.record_prompt(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "truncated-session",
                "prompt": prompt_text,
            },
            1_787_721_020_000,
        )
        assert event is not None
        self.assertTrue(event["truncated"])
        self.assertFalse(event["redacted"])
        self.assertEqual(len(event["promptText"].encode("utf-8")), 65_535)
        self.assertNotIn("🔥", event["promptText"])
        self.assertLessEqual(
            len(event["promptText"].encode("utf-8")),
            builder_pulse.PROMPT_MAX_BYTES,
        )

        boundary_secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
        boundary_event = self.record_prompt(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "redacted-boundary-session",
                "prompt": "b" * (builder_pulse.PROMPT_MAX_BYTES - 11)
                + "\n"
                + boundary_secret,
            },
            1_787_721_020_001,
        )
        assert boundary_event is not None
        self.assertTrue(boundary_event["redacted"])
        self.assertTrue(boundary_event["truncated"])
        self.assertNotIn(boundary_secret, boundary_event["promptText"])
        self.assertLessEqual(
            len(boundary_event["promptText"].encode("utf-8")),
            builder_pulse.PROMPT_MAX_BYTES,
        )

    def test_prompt_capture_requires_claim_policy_and_primary_user_submit(self) -> None:
        base = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "primary-only",
            "prompt": "private user prompt",
        }
        self.assertIsNone(self.record_prompt(base, 1_787_721_030_000))

        identity = self.claim_locally()
        identity["promptCapture"] = "off"
        builder_pulse.atomic_write_json(
            builder_pulse.identity_path(self.data_dir), identity
        )
        self.assertIsNone(self.record_prompt(base, 1_787_721_030_001))

        identity["promptCapture"] = "on"
        builder_pulse.atomic_write_json(
            builder_pulse.identity_path(self.data_dir), identity
        )
        self.assertIsNone(
            self.record_prompt(
                base,
                1_787_721_030_002,
                add_primary_transcript=False,
            )
        )
        child_transcript = self.write_transcript(
            "child-prompt.jsonl",
            {
                "type": "session_meta",
                "payload": {"source": {"subagent": {"thread_spawn": {}}}},
            },
        )
        rejected = (
            {**base, "hook_event_name": "SessionStart"},
            {**base, "hook_event_name": "PostToolUse", "tool_input": {"prompt": base["prompt"]}},
            {**base, "parent_thread_id": "parent-thread"},
            {**base, "is_fork": True},
            {**base, "transcript_path": str(child_transcript)},
        )
        for index, payload in enumerate(rejected):
            with self.subTest(index=index):
                self.assertIsNone(
                    self.record_prompt(payload, 1_787_721_030_100 + index)
                )
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "prompt-outbox.jsonl"), []
        )

    def test_prompt_capture_fails_closed_for_untrusted_or_malformed_transcripts(self) -> None:
        self.claim_locally()
        malformed = self.write_transcript("malformed-primary.jsonl")
        malformed.write_text("not-json\n", encoding="utf-8")
        wrong_first_record = self.write_transcript(
            "wrong-first-record.jsonl",
            {"type": "event_msg", "payload": {"type": "user_message"}},
        )
        parent_marked = self.write_transcript(
            "parent-marked.jsonl",
            {
                "type": "session_meta",
                "payload": {"parent_thread_id": 17, "source": "cli"},
            },
        )
        fork_marked = self.write_transcript(
            "fork-marked.jsonl",
            {
                "type": "session_meta",
                "payload": {"is_fork": True, "source": "cli"},
            },
        )
        outside = self.workspace / "outside.jsonl"
        outside.write_text(
            json.dumps({"type": "session_meta", "payload": {"source": "cli"}})
            + "\n",
            encoding="utf-8",
        )
        base = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "fail-closed",
            "prompt": "must stay uncaptured",
        }
        paths = (malformed, wrong_first_record, parent_marked, fork_marked, outside)
        for index, path in enumerate(paths):
            with self.subTest(path=path.name):
                self.assertIsNone(
                    self.record_prompt(
                        {**base, "transcript_path": str(path)},
                        1_787_721_031_000 + index,
                    )
                )
        self.assertFalse((self.data_dir / "prompt-outbox.jsonl").exists())

    def test_prompt_retry_keeps_id_and_bounded_queue_discards_oldest(self) -> None:
        self.claim_locally()
        first = self.record_prompt(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "retry-prompt",
                "prompt": "first prompt",
            },
            1_787_721_040_000,
        )
        assert first is not None
        prompt_id = first["promptId"]
        with mock.patch.object(
            builder_pulse, "deliver_prompt", return_value=(False, "network_error")
        ):
            result = builder_pulse.flush_prompt_outbox(self.data_dir, self.config)
        self.assertEqual(result, {"delivered": 0, "discarded": 0, "remaining": 1})
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "prompt-outbox.jsonl")[0][
                "promptId"
            ],
            prompt_id,
        )

        delivered: list[dict] = []

        def succeed(payload: dict, *args: object) -> tuple[bool, str]:
            delivered.append(payload)
            return True, "delivered"

        with mock.patch.object(builder_pulse, "deliver_prompt", side_effect=succeed):
            result = builder_pulse.flush_prompt_outbox(self.data_dir, self.config)
        self.assertEqual(result, {"delivered": 1, "discarded": 0, "remaining": 0})
        self.assertEqual(delivered, [first])

        events = []
        for index in range(3):
            event = builder_pulse.prompt_payload(
                installation_id="installation",
                key="session",
                project_id="project",
                feature_id=None,
                feature_label=None,
                prompt_text=f"prompt-{index}",
                occurred_at=index,
                redacted=False,
                truncated=False,
            )
            events.append(event)
            with builder_pulse.data_lock(self.data_dir):
                builder_pulse.enqueue_prompt_unlocked(
                    self.data_dir, event, 2, now_ms=2
                )
        queued = builder_pulse.read_jsonl(self.data_dir / "prompt-outbox.jsonl")
        self.assertEqual(
            [event["promptId"] for event in queued],
            [events[1]["promptId"], events[2]["promptId"]],
        )

    def test_hook_keeps_prompt_and_lifecycle_queues_strictly_separate(self) -> None:
        self.claim_locally()
        payloads = (
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "integrated-prompt-session",
                "prompt": "first private prompt",
                "tool_input": {"command": "private command"},
                "transcript_path": str(self.primary_transcript),
            },
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "integrated-prompt-session",
                "prompt": "second private prompt",
                "assistant_response": "private response",
                "transcript_path": str(self.primary_transcript),
            },
        )
        with mock.patch.object(
            builder_pulse, "utc_now_ms", side_effect=[1_787_721_050_000, 1_787_721_050_000,
                                                       1_787_721_050_001, 1_787_721_050_001]
        ), mock.patch.object(builder_pulse, "attempt_current_prompt"), mock.patch.object(
            builder_pulse, "flush_outbox"
        ), mock.patch.object(
            builder_pulse, "flush_prompt_outbox"
        ):
            for payload in payloads:
                output = io.StringIO()
                with mock.patch.object(
                    sys, "stdin", io.StringIO(json.dumps(payload))
                ), contextlib.redirect_stdout(output):
                    self.assertEqual(builder_pulse.ingest_hook(self.data_dir), 0)
                self.assertEqual(output.getvalue(), "{}\n")

        lifecycle = builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl")
        prompts = builder_pulse.read_jsonl(self.data_dir / "prompt-outbox.jsonl")
        self.assertEqual(len(lifecycle), 1)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(
            [event["promptText"] for event in prompts],
            ["first private prompt", "second private prompt"],
        )
        lifecycle_storage = (self.data_dir / "outbox.jsonl").read_text("utf-8")
        state_storage = "".join(
            path.read_text("utf-8") for path in (self.data_dir / "states").glob("*.json")
        )
        for sensitive in (
            "first private prompt",
            "second private prompt",
            "private command",
            "private response",
        ):
            self.assertNotIn(sensitive, lifecycle_storage)
            self.assertNotIn(sensitive, state_storage)

    def test_synchronous_hook_attempts_current_prompt_before_backlog(self) -> None:
        self.claim_locally()
        older = self.record_prompt(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "older-session",
                "prompt": "older queued prompt",
            },
            1_787_721_055_000,
        )
        lifecycle = self.record(
            {"hook_event_name": "SessionStart", "session_id": "older-lifecycle"},
            1_787_721_055_001,
        )
        assert older is not None and lifecycle is not None
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "current-session",
            "prompt": "current prompt must go first",
            "transcript_path": str(self.primary_transcript),
        }
        self.config["delivery_timeout_seconds"] = 3.0
        output = io.StringIO()
        with mock.patch.object(
            builder_pulse, "load_config", return_value=self.config
        ), mock.patch.object(
            builder_pulse, "utc_now_ms", return_value=1_787_721_055_002
        ), mock.patch.object(
            builder_pulse, "deliver_prompt", return_value=(True, "delivered")
        ) as delivered, mock.patch.object(
            builder_pulse, "deliver_event"
        ) as lifecycle_delivery, mock.patch.object(
            builder_pulse, "flush_outbox"
        ) as lifecycle_flush, mock.patch.object(
            builder_pulse, "flush_prompt_outbox"
        ) as prompt_flush, mock.patch.object(
            sys, "stdin", io.StringIO(json.dumps(payload))
        ), contextlib.redirect_stdout(output):
            self.assertEqual(builder_pulse.ingest_hook(self.data_dir), 0)

        self.assertEqual(output.getvalue(), "{}\n")
        delivered_event, delivered_config, _, _ = delivered.call_args.args
        self.assertEqual(delivered_event["promptText"], "current prompt must go first")
        self.assertEqual(
            delivered_config["delivery_timeout_seconds"],
            builder_pulse.CURRENT_PROMPT_DELIVERY_TIMEOUT_SECONDS,
        )
        lifecycle_delivery.assert_not_called()
        lifecycle_flush.assert_not_called()
        prompt_flush.assert_not_called()
        queued_prompts = builder_pulse.read_jsonl(
            self.data_dir / "prompt-outbox.jsonl"
        )
        self.assertEqual([event["promptId"] for event in queued_prompts], [older["promptId"]])
        self.assertGreaterEqual(
            len(builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl")), 1
        )

    def test_current_prompt_attempt_bypasses_busy_backlog_lease(self) -> None:
        self.claim_locally()
        current = self.record_prompt(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "concurrent-current",
                "prompt": "deliver despite concurrent backlog flush",
            },
            1_787_721_055_010,
        )
        assert current is not None
        with builder_pulse.delivery_lease(self.data_dir) as acquired:
            self.assertTrue(acquired)
            with mock.patch.object(
                builder_pulse, "deliver_prompt", return_value=(True, "delivered")
            ) as delivered:
                result = builder_pulse.attempt_current_prompt(
                    self.data_dir, self.config, current
                )

        self.assertEqual(result, {"delivered": 1, "discarded": 0, "remaining": 0})
        delivered.assert_called_once()
        self.assertFalse((self.data_dir / "prompt-outbox.jsonl").exists())

    def test_prompt_capture_failure_never_breaks_hook(self) -> None:
        output = io.StringIO()
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "failure-safe-prompt",
            "prompt": "private prompt",
        }
        with mock.patch.object(
            builder_pulse, "record_prompt_event", side_effect=RuntimeError("failure")
        ), mock.patch.object(
            sys, "stdin", io.StringIO(json.dumps(payload))
        ), contextlib.redirect_stdout(output):
            self.assertEqual(builder_pulse.ingest_hook(self.data_dir), 0)
        self.assertEqual(output.getvalue(), "{}\n")
        self.assertFalse((self.data_dir / "prompt-outbox.jsonl").exists())

    def test_prompt_auth_rejection_disables_capture_and_purges_without_content(self) -> None:
        now_ms = 1_787_721_070_000
        for status in (401, 403):
            with self.subTest(status=status):
                identity = self.claim_locally()
                first = self.record_prompt(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": f"auth-{status}-one",
                        "prompt": f"private-auth-prompt-{status}-one",
                    },
                    now_ms,
                )
                second = self.record_prompt(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": f"auth-{status}-two",
                        "prompt": f"private-auth-prompt-{status}-two",
                    },
                    now_ms + 1,
                )
                assert first is not None and second is not None
                with mock.patch.object(
                    builder_pulse,
                    "utc_now_ms",
                    return_value=now_ms + 2,
                ), mock.patch.object(
                    builder_pulse,
                    "deliver_prompt",
                    return_value=(False, f"http_{status}"),
                ):
                    result = builder_pulse.flush_prompt_outbox(
                        self.data_dir, self.config
                    )

                self.assertEqual(
                    result,
                    {"delivered": 0, "discarded": 2, "remaining": 0},
                )
                stored_identity = builder_pulse.read_json(
                    builder_pulse.identity_path(self.data_dir), {}
                )
                self.assertEqual(stored_identity["promptCapture"], "off")
                self.assertEqual(
                    stored_identity["installationToken"],
                    identity["installationToken"],
                )
                self.assertFalse((self.data_dir / "prompt-outbox.jsonl").exists())
                self.assertNotIn(
                    f"private-auth-prompt-{status}", json.dumps(result)
                )
                self.assertIsNone(
                    self.record_prompt(
                        {
                            "hook_event_name": "UserPromptSubmit",
                            "session_id": f"auth-{status}-disabled",
                            "prompt": "must not be queued after rejection",
                        },
                        now_ms + 3,
                    )
                )

    def test_prompt_outbox_expires_records_older_than_sixty_days(self) -> None:
        self.claim_locally()
        now_ms = 1_787_721_080_000
        events = []
        for name, occurred_at in (
            ("expired", now_ms - builder_pulse.PROMPT_RETENTION_MS - 1),
            ("boundary", now_ms - builder_pulse.PROMPT_RETENTION_MS),
            ("fresh", now_ms - 1),
        ):
            events.append(
                builder_pulse.prompt_payload(
                    installation_id="installation",
                    key="session",
                    project_id="project",
                    feature_id=None,
                    feature_label=None,
                    prompt_text=name,
                    occurred_at=occurred_at,
                    redacted=False,
                    truncated=False,
                )
            )
        path = self.data_dir / "prompt-outbox.jsonl"
        for event in events:
            builder_pulse.append_jsonl(path, event)

        with mock.patch.object(
            builder_pulse, "utc_now_ms", return_value=now_ms
        ), mock.patch.object(
            builder_pulse, "deliver_prompt", return_value=(False, "network_error")
        ):
            result = builder_pulse.flush_prompt_outbox(self.data_dir, self.config)

        self.assertEqual(
            result,
            {"delivered": 0, "discarded": 1, "remaining": 2},
        )
        retained = builder_pulse.read_jsonl(path)
        self.assertEqual(
            [event["promptId"] for event in retained],
            [events[1]["promptId"], events[2]["promptId"]],
        )

    def test_prompt_outbox_is_private_before_its_first_write(self) -> None:
        path = self.data_dir / "prompt-outbox.jsonl"
        real_open = os.open
        opened_modes: list[int] = []

        def tracked_open(file: object, flags: int, mode: int = 0o777) -> int:
            opened_modes.append(mode)
            return real_open(file, flags, mode)

        with mock.patch.object(builder_pulse.os, "open", side_effect=tracked_open):
            builder_pulse.append_jsonl(path, {"promptText": "private"})

        self.assertEqual(opened_modes, [0o600])
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_token_usage_snapshot_uses_cumulative_totals_and_schema_v2(self) -> None:
        identity = self.claim_locally()
        private_marker = "private prompt content must not escape"
        transcript = self.write_transcript(
            "supported.jsonl",
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": private_marker},
            },
            self.token_count_record(
                totals=self.token_totals(
                    input_tokens=900,
                    cached_input_tokens=300,
                    cache_write_input_tokens=75,
                    output_tokens=220,
                    reasoning_output_tokens=80,
                    total_tokens=1120,
                ),
                last=self.token_totals(
                    input_tokens=9,
                    cached_input_tokens=3,
                    cache_write_input_tokens=1,
                    output_tokens=2,
                    reasoning_output_tokens=1,
                    total_tokens=11,
                ),
            ),
        )
        event = self.record(
            {
                "hook_event_name": "SessionStart",
                "session_id": "token-session",
                "transcript_path": str(transcript),
                "prompt": private_marker,
            },
            1_787_721_000_000,
        )
        assert event is not None
        self.assertEqual(event["schemaVersion"], 2)
        self.assertEqual(
            event["tokenUsage"],
            {
                "inputTokens": 900,
                "cachedInputTokens": 300,
                "outputTokens": 220,
                "reasoningOutputTokens": 80,
                "totalTokens": 1120,
            },
        )
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
                "tokenUsage",
            },
        )
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl"), [event]
        )
        with mock.patch.object(
            builder_pulse.urlrequest, "urlopen", return_value=FakeResponse()
        ) as opened:
            ok, result = builder_pulse.deliver_event(
                event, self.config, identity["installationToken"]
            )
        self.assertTrue(ok)
        self.assertEqual(result, "delivered")
        self.assertEqual(json.loads(opened.call_args.args[0].data), event)
        zero_snapshot = builder_pulse.token_usage_from_record(
            self.token_count_record(
                totals={key: 0 for key in self.token_totals()}
            )
        )
        assert zero_snapshot is not None
        self.assertEqual(
            zero_snapshot,
            {
                "inputTokens": 0,
                "cachedInputTokens": 0,
                "outputTokens": 0,
                "reasoningOutputTokens": 0,
                "totalTokens": 0,
            },
        )
        self.assertIsNone(
            builder_pulse.validated_token_usage(
                {**zero_snapshot, "prompt": private_marker}
            )
        )
        persisted = "".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in self.data_dir.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        )
        self.assertNotIn(str(transcript), persisted)
        self.assertNotIn(private_marker, persisted)
        self.assertNotIn("transcript_path", persisted)

    def test_cache_write_counter_is_optional_locally_and_never_transported(self) -> None:
        self.claim_locally()
        without_cache_write = self.token_totals()
        without_cache_write.pop("cache_write_input_tokens")
        transcript = self.write_transcript(
            "current-five-counters.jsonl",
            self.token_count_record(totals=without_cache_write),
        )
        event = self.record(
            {
                "hook_event_name": "SessionStart",
                "session_id": "five-counter-session",
                "transcript_path": str(transcript),
            },
            1_787_721_050_000,
        )
        assert event is not None
        self.assertEqual(event["schemaVersion"], 2)
        self.assertEqual(set(event["tokenUsage"]), set(builder_pulse.TOKEN_USAGE_KEYS))
        self.assertNotIn("cacheWriteInputTokens", event["tokenUsage"])

        with_unknown = {**without_cache_write, "private_counter": 12}
        self.assertIsNone(
            builder_pulse.token_usage_from_record(
                self.token_count_record(totals=with_unknown)
            )
        )

    def test_missing_or_untrusted_transcript_preserves_v1(self) -> None:
        self.claim_locally()
        missing_counts = self.write_transcript(
            "missing.jsonl",
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "token_count"},
            },
        )
        outside = self.workspace / "outside.jsonl"
        outside.write_text(
            json.dumps(self.token_count_record()) + "\n", encoding="utf-8"
        )

        for index, transcript in enumerate((missing_counts, outside)):
            with self.subTest(transcript=transcript.name):
                event = self.record(
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": f"missing-{index}",
                        "transcript_path": str(transcript),
                    },
                    1_787_721_000_000 + index,
                )
                assert event is not None
                self.assertEqual(event["schemaVersion"], 1)
                self.assertNotIn("tokenUsage", event)

    def test_missing_or_malformed_token_counters_are_omitted(self) -> None:
        malformed: list[tuple[str, dict[str, object]]] = []
        missing = self.token_totals()
        missing.pop("output_tokens")
        malformed.append(("missing", missing))
        malformed.extend(
            [
                ("negative", self.token_totals(output_tokens=-1)),
                ("float", self.token_totals(output_tokens=1.0)),
                ("boolean", self.token_totals(output_tokens=True)),
                ("string", self.token_totals(output_tokens="40")),
                (
                    "unsafe",
                    self.token_totals(output_tokens=builder_pulse.MAX_SAFE_INTEGER + 1),
                ),
            ]
        )

        for index, (name, totals) in enumerate(malformed):
            with self.subTest(name=name):
                transcript = self.write_transcript(
                    f"malformed-{index}.jsonl",
                    self.token_count_record(totals=totals),
                )
                event = self.record(
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": f"malformed-{index}",
                        "transcript_path": str(transcript),
                    },
                    1_787_721_100_000 + index,
                )
                assert event is not None
                self.assertEqual(event["schemaVersion"], 1)
                self.assertNotIn("tokenUsage", event)

    def test_subagent_and_fork_events_never_include_token_snapshots(self) -> None:
        self.claim_locally()
        private_marker = "private-parent-thread-id"
        metadata_transcript = self.write_transcript(
            "child-metadata.jsonl",
            {
                "type": "session_meta",
                "payload": {
                    "source": {
                        "subagent": {
                            "thread_spawn": {"parent_thread_id": private_marker}
                        }
                    }
                },
            },
            self.token_count_record(),
        )
        path_marked_transcript = (
            self.codex_home
            / "sessions"
            / "2026"
            / "08"
            / "28"
            / "subagents"
            / "child.jsonl"
        )
        path_marked_transcript.parent.mkdir(parents=True, exist_ok=True)
        path_marked_transcript.write_text(
            json.dumps(self.token_count_record()) + "\n", encoding="utf-8"
        )
        direct_marker_transcript = self.write_transcript(
            "child-hook-marker.jsonl", self.token_count_record()
        )

        payloads = (
            {
                "hook_event_name": "SessionStart",
                "session_id": "child-metadata",
                "transcript_path": str(metadata_transcript),
            },
            {
                "hook_event_name": "SessionStart",
                "session_id": "child-path",
                "transcript_path": str(path_marked_transcript),
            },
            {
                "hook_event_name": "SessionStart",
                "session_id": "child-parent-field",
                "parent_thread_id": private_marker,
                "transcript_path": str(direct_marker_transcript),
            },
            {
                "hook_event_name": "SessionStart",
                "session_id": "fork-flag",
                "is_fork": True,
                "transcript_path": str(direct_marker_transcript),
            },
        )
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                event = self.record(payload, 1_787_721_200_000 + index)
                assert event is not None
                self.assertEqual(event["schemaVersion"], 1)
                self.assertNotIn("tokenUsage", event)

        persisted = "".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in self.data_dir.rglob("*")
            if path.is_file() and path.name != ".lock"
        )
        self.assertNotIn(private_marker, persisted)
        self.assertNotIn(str(metadata_transcript), persisted)
        self.assertNotIn(str(path_marked_transcript), persisted)

    def test_token_usage_is_sampled_only_on_emission_and_is_cumulative(self) -> None:
        self.claim_locally()
        start = 1_787_721_000_000
        first_totals = self.token_totals(input_tokens=100, total_tokens=140)
        second_totals = self.token_totals(input_tokens=240, total_tokens=310)
        transcript = self.write_transcript(
            "cumulative.jsonl", self.token_count_record(totals=first_totals)
        )
        first = self.record(
            {
                "hook_event_name": "SessionStart",
                "session_id": "cumulative",
                "transcript_path": str(transcript),
            },
            start,
        )
        assert first is not None
        self.assertEqual(first["tokenUsage"]["inputTokens"], 100)

        self.write_transcript(
            "cumulative.jsonl",
            self.token_count_record(totals=first_totals),
            self.token_count_record(totals=second_totals),
        )
        with mock.patch.object(
            builder_pulse,
            "token_usage_snapshot",
            wraps=builder_pulse.token_usage_snapshot,
        ) as sampled:
            self.assertIsNone(
                self.record(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "cumulative",
                        "transcript_path": str(transcript),
                    },
                    start + 60_000,
                )
            )
        sampled.assert_not_called()

        heartbeat = self.record(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "cumulative",
                "transcript_path": str(transcript),
            },
            start + 15 * 60 * 1000,
        )
        assert heartbeat is not None
        self.assertEqual(heartbeat["schemaVersion"], 2)
        self.assertEqual(heartbeat["tokenUsage"]["inputTokens"], 240)
        self.assertEqual(heartbeat["tokenUsage"]["totalTokens"], 310)
        queued = builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl")
        self.assertEqual(len(queued), 2)

    def test_token_usage_retry_keeps_snapshot_and_sensitive_data_private(self) -> None:
        self.claim_locally()
        private_marker = "raw session content must remain local"
        transcript = self.write_transcript(
            "retry.jsonl",
            {
                "type": "response_item",
                "payload": {"type": "message", "content": private_marker},
            },
            self.token_count_record(),
        )
        event = self.record(
            {
                "hook_event_name": "SessionStart",
                "session_id": "retry-token-usage",
                "transcript_path": str(transcript),
                "source": private_marker,
            },
            1_787_721_000_000,
        )
        assert event is not None
        original_id = event["eventId"]
        original_usage = event["tokenUsage"]

        with mock.patch.object(
            builder_pulse, "deliver_event", return_value=(False, "network_error")
        ):
            result = builder_pulse.flush_outbox(self.data_dir, self.config)
        self.assertEqual(result, {"delivered": 0, "quarantined": 0, "remaining": 1})
        queued = builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl")
        self.assertEqual(queued[0]["eventId"], original_id)
        self.assertEqual(queued[0]["tokenUsage"], original_usage)
        queued_text = (self.data_dir / "outbox.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(private_marker, queued_text)
        self.assertNotIn(str(transcript), queued_text)
        self.assertNotIn("transcript_path", queued_text)

        delivered: list[dict] = []

        def succeed(payload: dict, *args: object) -> tuple[bool, str]:
            delivered.append(payload)
            return True, "delivered"

        with mock.patch.object(builder_pulse, "deliver_event", side_effect=succeed):
            result = builder_pulse.flush_outbox(self.data_dir, self.config)
        self.assertEqual(result, {"delivered": 1, "quarantined": 0, "remaining": 0})
        self.assertEqual(delivered, [event])
        serialized = json.dumps(event)
        self.assertNotIn(private_marker, serialized)
        self.assertNotIn(str(transcript), serialized)
        self.assertNotIn("transcript_path", serialized)

    def test_token_snapshot_failure_never_breaks_hook_ingestion(self) -> None:
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "reader-failure",
            "transcript_path": str(self.codex_home / "sessions" / "missing.jsonl"),
        }
        output = io.StringIO()
        with mock.patch.object(
            builder_pulse, "token_usage_snapshot", side_effect=RuntimeError("boom")
        ), mock.patch.object(
            builder_pulse.sys, "stdin", io.StringIO(json.dumps(payload))
        ), contextlib.redirect_stdout(output):
            result = builder_pulse.ingest_hook(self.data_dir)
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().strip(), "{}")

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
        transcript = self.write_transcript(
            "session-end.jsonl", self.token_count_record()
        )
        self.record(
            {
                "hook_event_name": "SessionStart",
                "session_id": "session-one",
                "transcript_path": str(transcript),
            },
            1_787_721_000_000,
        )
        self.write_transcript(
            "session-end.jsonl",
            self.token_count_record(),
            self.token_count_record(
                totals=self.token_totals(input_tokens=400, total_tokens=500)
            ),
        )
        ended = self.record(
            {
                "hook_event_name": "SessionEnd",
                "session_id": "session-one",
                "transcript_path": str(transcript),
            },
            1_787_721_100_000,
        )
        assert ended is not None
        self.assertEqual(ended["state"], "idle")
        self.assertEqual(ended["activeFrom"], 1_787_721_000_000)
        self.assertEqual(ended["schemaVersion"], 2)
        self.assertEqual(ended["tokenUsage"]["inputTokens"], 400)
        self.assertEqual(ended["tokenUsage"]["totalTokens"], 500)

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


class HookManifestTests(unittest.TestCase):
    def test_codex_hook_commands_use_runtime_plugin_root(self) -> None:
        manifest = json.loads((builder_pulse.PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
        commands = [
            hook
            for registrations in manifest["hooks"].values()
            for registration in registrations
            for hook in registration["hooks"]
        ]
        self.assertTrue(commands)
        for hook in commands:
            self.assertIn("CLAUDE_PLUGIN_ROOT", hook["command"])
            self.assertIn("CLAUDE_PLUGIN_ROOT", hook["commandWindows"])

    def test_session_end_is_synchronous_for_codex(self) -> None:
        manifest = json.loads((builder_pulse.PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
        hook = manifest["hooks"]["SessionEnd"][0]["hooks"][0]
        self.assertNotIn("async", hook)

    def test_user_prompt_submit_is_synchronous_for_reliable_delivery(self) -> None:
        manifest = json.loads((builder_pulse.PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
        hook = manifest["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertNotIn("async", hook)


if __name__ == "__main__":
    unittest.main()
