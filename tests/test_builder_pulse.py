from __future__ import annotations

import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path, PureWindowsPath
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


class InteractiveInput(io.StringIO):
    def isatty(self) -> bool:
        return True


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
                "memberId": "growthx-member-1",
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
            {"endpoint": endpoint},
        )
        self.enroll_project(self.workspace)
        self.config = builder_pulse.load_config(self.data_dir)
        return identity

    def enroll_project(
        self,
        root: str | Path,
        *,
        project_id: str = "product-alpha",
        project_label: str = "Product Alpha",
    ) -> None:
        contexts = builder_pulse.load_work_contexts(self.data_dir)
        key = builder_pulse.repository_key(self.data_dir, root)
        if key not in contexts:
            contexts[key] = {
                "project_id": project_id,
                "project_label": project_label,
                "feature_id": "member-search",
                "feature_label": "Member search",
                "scope_key": str(uuid.uuid4()),
            }
            builder_pulse.atomic_write_json(
                self.data_dir / "contexts.json", contexts
            )

    def record(self, payload: dict, now_ms: int) -> dict | None:
        payload = dict(payload)
        payload.setdefault("cwd", str(self.workspace))
        self.enroll_project(payload["cwd"])
        with mock.patch.object(builder_pulse, "utc_now_ms", return_value=now_ms):
            return builder_pulse.record_hook_event(
                self.data_dir, payload, self.config
            )

    def record_prompt(
        self, payload: dict, now_ms: int, *, add_primary_transcript: bool = True
    ) -> dict | None:
        hook_payload = dict(payload)
        hook_payload.setdefault("cwd", str(self.workspace))
        self.enroll_project(hook_payload["cwd"])
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
        self.assertNotIn("project_id", config)
        self.assertEqual(
            builder_pulse.load_work_contexts(self.data_dir),
            {},
        )

    def test_canonical_disclosure_is_present_in_all_member_facing_guides(self) -> None:
        canonical = " ".join(builder_pulse.SETUP_DISCLOSURE.split())
        for relative_path in (
            "README.md",
            "SETUP.md",
            "skills/builder-pulse/SKILL.md",
        ):
            document = " ".join(
                (SCRIPT.parents[1] / relative_path).read_text(encoding="utf-8").split()
            )
            self.assertIn(canonical, document, relative_path)

        for required_fact in (
            "authenticated Builder Pulse admins",
            "retained for 30 days",
            "retained for 60 days",
            "compacted session, daily, and all-time token aggregates",
            "folder paths",
            "environment variables",
        ):
            self.assertIn(required_fact, builder_pulse.SETUP_DISCLOSURE)

    def test_activate_succeeds_only_after_codex_and_server_verify_connection(self) -> None:
        identity = self.claim_locally()
        output = io.StringIO()
        with mock.patch.object(
            builder_pulse,
            "inspect_codex_hooks",
            return_value={
                "ready": True,
                "hookStatus": "trusted",
                "hookCount": 5,
            },
        ), mock.patch.object(
            builder_pulse,
            "http_post_json",
            return_value=(
                True,
                "delivered",
                {
                    "accepted": True,
                    "telemetryReceived": True,
                    "telemetryReceivedSincePreviousActivation": True,
                    "lastSignalAt": 1_787_721_000_000,
                    "lastSignalPluginVersion": builder_pulse.PLUGIN_VERSION,
                },
            ),
        ) as posted, contextlib.redirect_stdout(output):
            result = builder_pulse.command_activate(self.data_dir)

        self.assertEqual(result, 0)
        self.assertEqual(posted.call_args.args[0], "https://pulse.example/v1/activation")
        activation = posted.call_args.args[1]
        self.assertEqual(activation["schemaVersion"], 1)
        self.assertEqual(activation["installationId"], identity["installationId"])
        self.assertEqual(activation["pluginVersion"], builder_pulse.PLUGIN_VERSION)
        response = json.loads(output.getvalue())
        self.assertEqual(response["connected"], True)
        self.assertEqual(response["activationReady"], True)
        self.assertEqual(response["hooksTrusted"], True)
        self.assertEqual(response["serverVerified"], True)
        self.assertEqual(response["telemetryReceived"], True)
        self.assertEqual(
            response["telemetryReceivedSincePreviousActivation"], True
        )
        self.assertEqual(response["lastSignalAt"], 1_787_721_000_000)
        self.assertEqual(
            response["lastSignalPluginVersion"], builder_pulse.PLUGIN_VERSION
        )
        self.assertEqual(response["hookCount"], 5)
        self.assertNotIn(identity["installationToken"], output.getvalue())
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl"),
            [],
        )

    def test_activate_does_not_call_hook_trust_a_telemetry_receipt(self) -> None:
        self.claim_locally()
        output = io.StringIO()
        with mock.patch.object(
            builder_pulse,
            "inspect_codex_hooks",
            return_value={
                "ready": True,
                "hookStatus": "trusted",
                "hookCount": 5,
            },
        ), mock.patch.object(
            builder_pulse,
            "http_post_json",
            return_value=(
                True,
                "delivered",
                {
                    "accepted": True,
                    "telemetryReceived": False,
                    "telemetryReceivedSincePreviousActivation": False,
                    "lastSignalAt": None,
                    "lastSignalPluginVersion": None,
                },
            ),
        ), contextlib.redirect_stdout(output):
            result = builder_pulse.command_activate(self.data_dir)

        self.assertEqual(result, 0)
        response = json.loads(output.getvalue())
        self.assertEqual(response["activationReady"], True)
        self.assertEqual(response["connected"], False)
        self.assertEqual(response["telemetryReceived"], False)
        self.assertEqual(
            response["telemetryReceivedSincePreviousActivation"], False
        )
        self.assertIsNone(response["lastSignalAt"])

    def test_activate_does_not_call_old_telemetry_a_current_connection(self) -> None:
        self.claim_locally()
        output = io.StringIO()
        with mock.patch.object(
            builder_pulse,
            "inspect_codex_hooks",
            return_value={
                "ready": True,
                "hookStatus": "trusted",
                "hookCount": 5,
            },
        ), mock.patch.object(
            builder_pulse,
            "http_post_json",
            return_value=(
                True,
                "delivered",
                {
                    "accepted": True,
                    "telemetryReceived": True,
                    "telemetryReceivedSincePreviousActivation": False,
                    "lastSignalAt": 1_787_721_000_000,
                    "lastSignalPluginVersion": "0.4.5",
                },
            ),
        ), contextlib.redirect_stdout(output):
            result = builder_pulse.command_activate(self.data_dir)

        self.assertEqual(result, 0)
        response = json.loads(output.getvalue())
        self.assertEqual(response["connected"], False)
        self.assertEqual(response["telemetryReceived"], True)
        self.assertEqual(
            response["telemetryReceivedSincePreviousActivation"], False
        )
        self.assertEqual(response["lastSignalPluginVersion"], "0.4.5")

    def test_activate_rejects_boolean_receipt_without_current_version_evidence(
        self,
    ) -> None:
        self.claim_locally()
        output = io.StringIO()
        with mock.patch.object(
            builder_pulse,
            "inspect_codex_hooks",
            return_value={
                "ready": True,
                "hookStatus": "trusted",
                "hookCount": 5,
            },
        ), mock.patch.object(
            builder_pulse,
            "http_post_json",
            return_value=(
                True,
                "delivered",
                {
                    "accepted": True,
                    "telemetryReceived": True,
                    "telemetryReceivedSincePreviousActivation": True,
                    "lastSignalAt": 1_787_721_000_000,
                    "lastSignalPluginVersion": "0.4.5",
                },
            ),
        ), contextlib.redirect_stdout(output):
            self.assertEqual(builder_pulse.command_activate(self.data_dir), 0)

        response = json.loads(output.getvalue())
        self.assertFalse(response["connected"])
        self.assertTrue(response["telemetryReceivedSincePreviousActivation"])

    def test_activate_requires_official_codex_hook_review(self) -> None:
        self.claim_locally()
        output = io.StringIO()
        with mock.patch.object(
            builder_pulse,
            "inspect_codex_hooks",
            return_value={
                "ready": False,
                "hookStatus": "review_required",
                "hookCount": 5,
            },
        ), mock.patch.object(builder_pulse, "http_post_json") as posted, contextlib.redirect_stdout(output):
            result = builder_pulse.command_activate(self.data_dir)

        self.assertEqual(result, 3)
        posted.assert_not_called()
        response = json.loads(output.getvalue())
        self.assertEqual(response["connected"], False)
        self.assertEqual(response["reviewRequired"], True)
        self.assertEqual(response["hookStatus"], "review_required")

    def test_activate_rejects_a_locally_disabled_plugin_without_server_call(self) -> None:
        self.claim_locally()
        builder_pulse.save_config_overrides(self.data_dir, {"enabled": False})
        output = io.StringIO()
        with mock.patch.object(
            builder_pulse, "inspect_codex_hooks"
        ) as inspected, mock.patch.object(
            builder_pulse, "http_post_json"
        ) as posted, contextlib.redirect_stdout(output):
            result = builder_pulse.command_activate(self.data_dir)

        self.assertEqual(result, 3)
        inspected.assert_not_called()
        posted.assert_not_called()
        response = json.loads(output.getvalue())
        self.assertEqual(response["connected"], False)
        self.assertEqual(response["ready"], False)
        self.assertEqual(response["reviewRequired"], False)
        self.assertEqual(response["hookStatus"], "disabled")

    def test_activate_does_not_queue_fake_state_when_server_is_unavailable(self) -> None:
        self.claim_locally()
        error = io.StringIO()
        with mock.patch.object(
            builder_pulse,
            "inspect_codex_hooks",
            return_value={
                "ready": True,
                "hookStatus": "trusted",
                "hookCount": 5,
            },
        ), mock.patch.object(
            builder_pulse,
            "http_post_json",
            return_value=(False, "network_error", None),
        ), contextlib.redirect_stderr(error):
            result = builder_pulse.command_activate(self.data_dir)

        self.assertEqual(result, 1)
        self.assertIn("network_error", error.getvalue())
        self.assertEqual(builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl"), [])

    def test_activate_rejects_an_unclaimed_installation(self) -> None:
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            result = builder_pulse.command_activate(self.data_dir)
        self.assertEqual(result, 2)
        self.assertIn("has not been claimed", error.getvalue())

    def test_hook_readiness_requires_current_enabled_trusted_plugin_hooks(self) -> None:
        source_path = str(builder_pulse.PLUGIN_ROOT / "hooks" / "hooks.json")
        hooks = [
            {
                "pluginId": "builder-pulse@growthx-builder-tools",
                "eventName": event_name,
                "sourcePath": source_path,
                "enabled": True,
                "trustStatus": "trusted",
            }
            for event_name in builder_pulse.EXPECTED_PLUGIN_HOOK_EVENTS
        ]
        response = {"result": {"data": [{"hooks": hooks, "errors": []}]}}
        self.assertEqual(
            builder_pulse.evaluate_builder_pulse_hooks(response),
            {"ready": True, "hookStatus": "trusted", "hookCount": 5},
        )

        hooks[0]["trustStatus"] = "untrusted"
        self.assertEqual(
            builder_pulse.evaluate_builder_pulse_hooks(response),
            {"ready": False, "hookStatus": "review_required", "hookCount": 5},
        )

        hooks[0]["trustStatus"] = "trusted"
        hooks[0]["sourcePath"] = "/tmp/stale/hooks/hooks.json"
        self.assertEqual(
            builder_pulse.evaluate_builder_pulse_hooks(response),
            {"ready": False, "hookStatus": "stale_plugin", "hookCount": 5},
        )

    def test_hook_readiness_rejects_duplicate_missing_or_extra_hooks(self) -> None:
        source_path = str(builder_pulse.PLUGIN_ROOT / "hooks" / "hooks.json")

        def hook(event_name: str) -> dict[str, object]:
            return {
                "pluginId": "builder-pulse@growthx-builder-tools",
                "eventName": event_name,
                "sourcePath": source_path,
                "enabled": True,
                "trustStatus": "trusted",
            }

        events = sorted(builder_pulse.EXPECTED_PLUGIN_HOOK_EVENTS)
        cases = {
            "duplicate": [hook(event) for event in events] + [hook(events[0])],
            "missing": [hook(event) for event in events[:-1]],
            "extra": [hook(event) for event in events] + [hook("preToolUse")],
        }
        for name, hooks in cases.items():
            with self.subTest(name=name):
                response = {"result": {"data": [{"hooks": hooks, "errors": []}]}}
                result = builder_pulse.evaluate_builder_pulse_hooks(response)
                self.assertEqual(result["ready"], False)
                self.assertEqual(result["hookStatus"], "incomplete")
                self.assertEqual(result["hookCount"], len(hooks))

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

    def test_claim_removes_legacy_global_project_and_preserves_enrollment(self) -> None:
        builder_pulse.atomic_write_json(
            self.data_dir / "config.json", {"project_id": "explicit-product"}
        )
        self.enroll_project(
            self.workspace,
            project_id="confirmed-product",
            project_label="Confirmed Product",
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
        self.assertNotIn("project_id", builder_pulse.load_config(self.data_dir))
        context = builder_pulse.load_work_contexts(self.data_dir)[
            builder_pulse.repository_key(self.data_dir, self.workspace)
        ]
        self.assertEqual(context["project_id"], "confirmed-product")
        self.assertEqual(context["project_label"], "Confirmed Product")

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

    def test_claim_without_fresh_code_keeps_existing_identity_without_server_call(self) -> None:
        self.claim_locally()
        before = builder_pulse.identity_path(self.data_dir).read_bytes()
        args = argparse.Namespace(endpoint="https://pulse.example", code=None)
        output = io.StringIO()
        with mock.patch.object(builder_pulse, "http_post_json") as posted, \
            contextlib.redirect_stdout(output):
            self.assertEqual(builder_pulse.command_claim(args, self.data_dir), 0)

        posted.assert_not_called()
        self.assertEqual(builder_pulse.identity_path(self.data_dir).read_bytes(), before)
        self.assertTrue(json.loads(output.getvalue())["alreadyClaimed"])

    def test_fresh_claim_code_reverifies_existing_identity_without_replacing_it(self) -> None:
        identity = self.claim_locally()
        before = builder_pulse.identity_path(self.data_dir).read_bytes()
        response = {
            "builderId": identity["builderId"],
            "memberId": identity["memberId"],
            "name": identity["builderName"],
            "defaultProject": None,
            "heartbeatMinutes": 15,
            "promptCapture": "on",
        }
        args = argparse.Namespace(
            endpoint="https://pulse.example", code="fresh-personalized-invite"
        )
        output = io.StringIO()
        with mock.patch.object(
            builder_pulse,
            "http_post_json",
            return_value=(True, "delivered", response),
        ) as posted, contextlib.redirect_stdout(output), contextlib.redirect_stderr(
            io.StringIO()
        ):
            self.assertEqual(builder_pulse.command_claim(args, self.data_dir), 0)

        payload = posted.call_args.args[1]
        self.assertEqual(payload["inviteCode"], "fresh-personalized-invite")
        self.assertEqual(payload["installationId"], identity["installationId"])
        self.assertEqual(payload["installationToken"], identity["installationToken"])
        self.assertEqual(payload["pluginVersion"], builder_pulse.PLUGIN_VERSION)
        self.assertEqual(builder_pulse.identity_path(self.data_dir).read_bytes(), before)
        result = json.loads(output.getvalue())
        self.assertTrue(result["alreadyClaimed"])
        self.assertTrue(result["reverified"])

    def test_reverification_repairs_stale_capture_policy_and_builder_name(self) -> None:
        identity = self.claim_locally()
        stale_identity = dict(identity)
        stale_identity["builderName"] = "Old roster name"
        stale_identity["promptCapture"] = "off"
        builder_pulse.atomic_write_json(
            builder_pulse.identity_path(self.data_dir), stale_identity
        )
        preserved = {
            key: stale_identity[key]
            for key in (
                "installationId",
                "installationToken",
                "claimedEndpoint",
                "scopeSecret",
            )
        }
        response = {
            "builderId": identity["builderId"],
            "memberId": identity["memberId"],
            "name": "Current roster name",
            "defaultProject": None,
            "heartbeatMinutes": 15,
            "promptCapture": "on",
        }

        with mock.patch.object(
            builder_pulse,
            "http_post_json",
            return_value=(True, "delivered", response),
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            result = builder_pulse.command_claim(
                argparse.Namespace(
                    endpoint="https://pulse.example",
                    code="fresh-personalized-invite",
                ),
                self.data_dir,
            )

        self.assertEqual(result, 0)
        refreshed = builder_pulse.read_json(
            builder_pulse.identity_path(self.data_dir), {}
        )
        self.assertEqual(refreshed["builderName"], "Current roster name")
        self.assertEqual(refreshed["promptCapture"], "on")
        for key, value in preserved.items():
            self.assertEqual(refreshed[key], value)

    def test_fresh_claim_code_refuses_different_authoritative_identity(self) -> None:
        identity = self.claim_locally()
        before = builder_pulse.identity_path(self.data_dir).read_bytes()
        response = {
            "builderId": "builder-2",
            "memberId": "growthx-member-2",
            "name": "Builder Two",
            "defaultProject": None,
            "heartbeatMinutes": 15,
            "promptCapture": "on",
        }
        args = argparse.Namespace(
            endpoint="https://pulse.example", code="wrong-member-invite"
        )
        error = io.StringIO()
        with mock.patch.object(
            builder_pulse,
            "http_post_json",
            return_value=(True, "delivered", response),
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error):
            self.assertEqual(builder_pulse.command_claim(args, self.data_dir), 1)

        self.assertIn("invite_identity_mismatch", error.getvalue())
        self.assertEqual(builder_pulse.identity_path(self.data_dir).read_bytes(), before)
        self.assertEqual(
            builder_pulse.read_json(builder_pulse.identity_path(self.data_dir), {}),
            identity,
        )

    def test_fresh_claim_code_server_refusal_preserves_existing_identity(self) -> None:
        self.claim_locally()
        before = builder_pulse.identity_path(self.data_dir).read_bytes()
        args = argparse.Namespace(
            endpoint="https://pulse.example", code="wrong-member-invite"
        )
        error = io.StringIO()
        with mock.patch.object(
            builder_pulse,
            "http_post_json",
            return_value=(False, "installation_exists", None),
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error):
            self.assertEqual(builder_pulse.command_claim(args, self.data_dir), 1)

        self.assertIn("installation_exists", error.getvalue())
        self.assertEqual(builder_pulse.identity_path(self.data_dir).read_bytes(), before)

    def test_existing_identity_recovery_is_safe_end_to_end(self) -> None:
        identity = self.claim_locally()
        requests: list[dict] = []

        class RecoveryHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                requests.append(payload)
                same_member = payload["inviteCode"] == "same-member"
                response = json.dumps(
                    {
                        "builderId": identity["builderId"] if same_member else "builder-2",
                        "memberId": identity["memberId"] if same_member else "growthx-member-2",
                        "name": identity["builderName"] if same_member else "Builder Two",
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

        server = ThreadingHTTPServer(("127.0.0.1", 0), RecoveryHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        local_identity = dict(identity)
        local_identity["claimedEndpoint"] = endpoint
        builder_pulse.atomic_write_json(
            builder_pulse.identity_path(self.data_dir), local_identity
        )
        before = builder_pulse.identity_path(self.data_dir).read_bytes()
        try:
            same_args = argparse.Namespace(endpoint=endpoint, code="same-member")
            wrong_args = argparse.Namespace(endpoint=endpoint, code="wrong-member")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                self.assertEqual(builder_pulse.command_claim(same_args, self.data_dir), 0)
                self.assertEqual(builder_pulse.command_claim(wrong_args, self.data_dir), 1)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertEqual(len(requests), 2)
        for payload in requests:
            self.assertEqual(payload["installationId"], identity["installationId"])
            self.assertEqual(payload["installationToken"], identity["installationToken"])
        self.assertEqual(builder_pulse.identity_path(self.data_dir).read_bytes(), before)

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
        wire_event = builder_pulse.wire_payload(event)
        self.assertEqual(
            set(wire_event),
            {
                "schemaVersion",
                "eventId",
                "installationId",
                "sessionKey",
                "projectId",
                "projectLabel",
                "projectScope",
                "featureId",
                "featureLabel",
                "state",
                "occurredAt",
                "pluginVersion",
            },
        )
        self.assertIsInstance(event["occurredAt"], int)
        self.assertEqual(event["installationId"], identity["installationId"])
        self.assertEqual(event["projectLabel"], "Product Alpha")
        self.assertEqual(event["projectScope"], "explicit")

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
        self.assertEqual(json.loads(request.data.decode("utf-8")), wire_event)

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
        wire_event = builder_pulse.wire_payload(event)
        self.assertEqual(
            set(wire_event),
            {
                "schemaVersion",
                "promptId",
                "installationId",
                "sessionKey",
                "projectId",
                "projectLabel",
                "projectScope",
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
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            builder_pulse.wire_payload(event),
        )

    def test_unenrolled_or_missing_cwd_fails_closed_without_local_capture(self) -> None:
        self.claim_locally()
        builder_pulse.atomic_write_json(self.data_dir / "contexts.json", {})
        prompt_payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "private-session",
            "prompt": "This prompt belongs to another project.",
            "transcript_path": str(self.primary_transcript),
        }
        lifecycle_payload = {
            "hook_event_name": "SessionStart",
            "session_id": "private-session",
        }

        for cwd in (None, str(self.workspace), str(self.workspace / "other-project")):
            prompt = dict(prompt_payload)
            lifecycle = dict(lifecycle_payload)
            if cwd is not None:
                prompt["cwd"] = cwd
                lifecycle["cwd"] = cwd
            self.assertIsNone(
                builder_pulse.record_prompt_event(self.data_dir, prompt, self.config)
            )
            self.assertIsNone(
                builder_pulse.record_hook_event(self.data_dir, lifecycle, self.config)
            )

        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "prompt-outbox.jsonl"), []
        )
        self.assertEqual(builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl"), [])
        states_dir = self.data_dir / "states"
        self.assertFalse(states_dir.exists() and any(states_dir.iterdir()))

    def test_unenrolled_prompt_is_rejected_before_prompt_text_or_transcript_processing(
        self,
    ) -> None:
        self.claim_locally()
        builder_pulse.atomic_write_json(self.data_dir / "contexts.json", {})
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "private-session",
            "cwd": str(self.workspace / "not-enrolled"),
            "prompt": "This must not be inspected.",
            "transcript_path": str(self.primary_transcript),
        }

        with (
            mock.patch.object(builder_pulse, "is_primary_user_prompt") as primary,
            mock.patch.object(builder_pulse, "bounded_redacted_prompt") as redact,
        ):
            self.assertIsNone(
                builder_pulse.record_prompt_event(self.data_dir, payload, self.config)
            )

        primary.assert_not_called()
        redact.assert_not_called()

    def test_enrollment_is_rechecked_under_lock_before_any_event_is_queued(self) -> None:
        self.claim_locally()
        contexts = builder_pulse.load_work_contexts(self.data_dir)
        prompt_payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "race-prompt",
            "prompt": "Do not queue this after unenrollment.",
            "transcript_path": str(self.primary_transcript),
            "cwd": str(self.workspace),
        }
        with mock.patch.object(
            builder_pulse, "load_work_contexts", side_effect=[contexts, {}]
        ):
            self.assertIsNone(
                builder_pulse.record_prompt_event(
                    self.data_dir, prompt_payload, self.config
                )
            )

        lifecycle_payload = {
            "hook_event_name": "SessionStart",
            "session_id": "race-lifecycle",
            "cwd": str(self.workspace),
        }
        with mock.patch.object(
            builder_pulse, "load_work_contexts", side_effect=[contexts, {}]
        ):
            self.assertIsNone(
                builder_pulse.record_hook_event(
                    self.data_dir, lifecycle_payload, self.config
                )
            )

        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "prompt-outbox.jsonl"), []
        )
        self.assertEqual(builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl"), [])

    def test_feature_context_is_rechecked_under_lock_before_any_event_is_queued(self) -> None:
        self.claim_locally()
        contexts = builder_pulse.load_work_contexts(self.data_dir)
        changed_contexts = json.loads(json.dumps(contexts))
        context_key = builder_pulse.repository_key(self.data_dir, self.workspace)
        changed_contexts[context_key]["feature_id"] = "new-feature"
        changed_contexts[context_key]["feature_label"] = "New feature"

        prompt_payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "race-prompt-feature",
            "prompt": "Do not queue this with a stale feature.",
            "transcript_path": str(self.primary_transcript),
            "cwd": str(self.workspace),
        }
        with mock.patch.object(
            builder_pulse,
            "load_work_contexts",
            side_effect=[contexts, changed_contexts],
        ):
            self.assertIsNone(
                builder_pulse.record_prompt_event(
                    self.data_dir, prompt_payload, self.config
                )
            )

        lifecycle_payload = {
            "hook_event_name": "SessionStart",
            "session_id": "race-lifecycle-feature",
            "cwd": str(self.workspace),
        }
        with mock.patch.object(
            builder_pulse,
            "load_work_contexts",
            side_effect=[contexts, changed_contexts],
        ):
            self.assertIsNone(
                builder_pulse.record_hook_event(
                    self.data_dir, lifecycle_payload, self.config
                )
            )

        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "prompt-outbox.jsonl"), []
        )
        self.assertEqual(builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl"), [])

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
            "InviteCodeViaArgument_1234567890",
            "InviteCodeViaLabel_1234567890",
            "InviteCodeViaQuotedArgument_1234567890",
            "InviteCodeViaEnvironment_1234567890",
        )
        prompt_text = "\n".join(
            (
                "Keep ordinary project-123 and the word bearer unchanged.",
                "-----BEGIN PRIVATE KEY-----\n"
                f"{secrets_to_remove[0]}\n"
                "-----END PRIVATE KEY-----",
                f"Authorization: {secrets_to_remove[1]}",
                f"Use Bearer {secrets_to_remove[2]}",
                *secrets_to_remove[3:8],
                f"claim --code {secrets_to_remove[8]}",
                f"Builder Pulse invite code: {secrets_to_remove[9]}",
                f"claim --code '{secrets_to_remove[10]}'",
                f'BUILDER_PULSE_INVITE_CODE="{secrets_to_remove[11]}"',
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
                project_label="Project",
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
                "cwd": str(self.workspace),
                "tool_input": {"command": "private command"},
                "transcript_path": str(self.primary_transcript),
            },
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "integrated-prompt-session",
                "prompt": "second private prompt",
                "cwd": str(self.workspace),
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
            "cwd": str(self.workspace),
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
        context_key = builder_pulse.repository_key(self.data_dir, self.workspace)
        context = builder_pulse.load_work_contexts(self.data_dir)[context_key]
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
                    project_id=context["project_id"],
                    project_label=context["project_label"],
                    feature_id=context["feature_id"],
                    feature_label=context["feature_label"],
                    prompt_text=name,
                    occurred_at=occurred_at,
                    redacted=False,
                    truncated=False,
                )
            )
            events[-1]["_contextKey"] = context_key
            events[-1]["_scopeKey"] = context["scope_key"]
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
            set(builder_pulse.wire_payload(event)),
            {
                "schemaVersion",
                "eventId",
                "installationId",
                "sessionKey",
                "projectId",
                "projectLabel",
                "projectScope",
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
        self.assertEqual(
            json.loads(opened.call_args.args[0].data),
            builder_pulse.wire_payload(event),
        )
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
        self.assertEqual(
            result,
            {"delivered": 0, "discarded": 0, "quarantined": 0, "remaining": 1},
        )
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
        self.assertEqual(
            result,
            {"delivered": 1, "discarded": 0, "quarantined": 0, "remaining": 0},
        )
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
        self.assertEqual(
            result,
            {"delivered": 0, "discarded": 0, "quarantined": 0, "remaining": 1},
        )
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
        self.assertEqual(
            result,
            {"delivered": 1, "discarded": 0, "quarantined": 0, "remaining": 0},
        )

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
        self.enroll_project(
            self.data_dir,
            project_id="community-app",
            project_label="Community App",
        )
        args = argparse.Namespace(
            work_command="set",
            project=None,
            feature="Member Search Filters",
            feature_id=None,
            root=str(self.data_dir),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder_pulse.command_work(args, self.data_dir), 0)
        context = builder_pulse.load_work_contexts(self.data_dir)[
            builder_pulse.repository_key(self.data_dir, self.data_dir)
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
            project=None,
            feature="Feature one",
            feature_id=None,
            root=str(first_root),
        )
        second_args = argparse.Namespace(
            work_command="set",
            project=None,
            feature="Feature two",
            feature_id=None,
            root=str(second_root),
        )
        self.enroll_project(
            first_root,
            project_id="product-one",
            project_label="Product One",
        )
        self.enroll_project(
            second_root,
            project_id="product-two",
            project_label="Product Two",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder_pulse.command_work(first_args, self.data_dir), 0)
            self.assertEqual(builder_pulse.command_work(second_args, self.data_dir), 0)
        contexts = builder_pulse.load_work_contexts(self.data_dir)
        self.assertEqual(len(contexts), 2)
        first_key = builder_pulse.repository_key(self.data_dir, first_root)
        second_key = builder_pulse.repository_key(self.data_dir, second_root)
        self.assertEqual(contexts[first_key]["feature_label"], "Feature one")
        self.assertEqual(contexts[second_key]["feature_label"], "Feature two")
        persisted = (self.data_dir / "contexts.json").read_text(encoding="utf-8")
        self.assertNotIn(str(first_root), persisted)
        self.assertNotIn(str(second_root), persisted)

    def test_work_enroll_prompts_locally_for_folder_and_display_name(self) -> None:
        project_root = self.workspace / "prompted-project"
        project_root.mkdir()
        args = argparse.Namespace(
            work_command="enroll",
            project=None,
            project_id=None,
            root=None,
        )
        output = io.StringIO()
        local_choices = io.StringIO()
        with mock.patch.object(
            sys,
            "stdin",
            InteractiveInput(f"{project_root}\nConfirmed Product\n"),
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(
            local_choices
        ):
            self.assertEqual(builder_pulse.command_work(args, self.data_dir), 0)

        key = builder_pulse.repository_key(self.data_dir, project_root)
        context = builder_pulse.load_work_contexts(self.data_dir)[key]
        self.assertEqual(context["project_label"], "Confirmed Product")
        self.assertNotIn(
            str(project_root),
            (self.data_dir / "contexts.json").read_text(encoding="utf-8"),
        )
        self.assertIn("shown only in this terminal", local_choices.getvalue())
        self.assertIn("Current folder", local_choices.getvalue())

    def test_noninteractive_enrollment_requires_an_explicit_display_name(self) -> None:
        project_root = self.workspace / "missing-label"
        project_root.mkdir()
        error = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO()), contextlib.redirect_stderr(
            error
        ):
            result = builder_pulse.command_work(
                argparse.Namespace(
                    work_command="enroll",
                    project=None,
                    project_id=None,
                    root=str(project_root),
                ),
                self.data_dir,
            )
        self.assertEqual(result, 2)
        self.assertIn("project name is required", error.getvalue())

    def test_nested_project_enrollments_are_rejected_in_both_directions(self) -> None:
        parent = self.workspace / "nested-project"
        child = parent / "package"
        child.mkdir(parents=True)

        for first, second in ((parent, child), (child, parent)):
            with self.subTest(first=first.name):
                builder_pulse.atomic_write_json(self.data_dir / "contexts.json", {})
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        builder_pulse.command_work(
                            argparse.Namespace(
                                work_command="enroll",
                                project="First boundary",
                                project_id=None,
                                root=str(first),
                            ),
                            self.data_dir,
                        ),
                        0,
                    )
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    result = builder_pulse.command_work(
                        argparse.Namespace(
                            work_command="enroll",
                            project="Overlapping boundary",
                            project_id=None,
                            root=str(second),
                        ),
                        self.data_dir,
                    )
                self.assertEqual(result, 2)
                self.assertIn("overlaps", error.getvalue())
                self.assertEqual(len(builder_pulse.load_work_contexts(self.data_dir)), 1)

    def test_unenrolling_a_child_never_removes_its_enrolled_parent(self) -> None:
        parent = self.workspace / "parent-project"
        child = parent / "src"
        child.mkdir(parents=True)
        self.enroll_project(parent)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = builder_pulse.command_work(
                argparse.Namespace(work_command="unenroll", root=str(child)),
                self.data_dir,
            )

        self.assertEqual(result, 0)
        response = json.loads(output.getvalue())
        self.assertFalse(response["removed"])
        self.assertTrue(response["enrolled"])
        self.assertIn(
            builder_pulse.repository_key(self.data_dir, parent),
            builder_pulse.load_work_contexts(self.data_dir),
        )

    def test_work_set_cannot_bypass_explicit_project_enrollment(self) -> None:
        project_root = self.workspace / "not-enrolled"
        project_root.mkdir()
        args = argparse.Namespace(
            work_command="set",
            project="Unconfirmed Project",
            feature="Feature one",
            feature_id=None,
            root=str(project_root),
        )
        error = io.StringIO()

        with contextlib.redirect_stderr(error):
            self.assertEqual(builder_pulse.command_work(args, self.data_dir), 2)

        self.assertIn("use work enroll", error.getvalue())
        self.assertEqual(builder_pulse.load_work_contexts(self.data_dir), {})

    def test_work_set_does_not_resurrect_a_concurrently_unenrolled_project(self) -> None:
        project_root = self.workspace / "concurrent-unenroll"
        project_root.mkdir()
        self.enroll_project(project_root)
        builder_pulse.ensure_project_scope_migration(self.data_dir)
        contexts = builder_pulse.load_work_contexts(self.data_dir)
        args = argparse.Namespace(
            work_command="set",
            project=None,
            feature="Private feature",
            feature_id=None,
            root=str(project_root),
        )

        with (
            mock.patch.object(
                builder_pulse,
                "load_work_contexts",
                side_effect=[contexts, contexts, {}],
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(builder_pulse.command_work(args, self.data_dir), 2)

        self.assertEqual(
            builder_pulse.read_json(self.data_dir / "contexts.json", {}), contexts
        )

    def test_clear_feature_does_not_resurrect_a_concurrently_unenrolled_project(
        self,
    ) -> None:
        project_root = self.workspace / "concurrent-clear"
        project_root.mkdir()
        self.enroll_project(project_root)
        builder_pulse.ensure_project_scope_migration(self.data_dir)
        contexts = builder_pulse.load_work_contexts(self.data_dir)
        args = argparse.Namespace(
            work_command="clear-feature",
            root=str(project_root),
        )

        with (
            mock.patch.object(
                builder_pulse,
                "load_work_contexts",
                side_effect=[contexts, contexts, {}],
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(builder_pulse.command_work(args, self.data_dir), 2)

        self.assertEqual(
            builder_pulse.read_json(self.data_dir / "contexts.json", {}), contexts
        )

    def test_work_views_do_not_call_malformed_contexts_enrolled(self) -> None:
        project_root = self.workspace / "malformed-context"
        project_root.mkdir()
        builder_pulse.atomic_write_json(
            self.data_dir / "contexts.json",
            {
                builder_pulse.repository_key(self.data_dir, project_root): {
                    "project_id": "invalid id with spaces",
                    "project_label": "Looks enrolled",
                }
            },
        )

        list_output = io.StringIO()
        with contextlib.redirect_stdout(list_output):
            self.assertEqual(
                builder_pulse.command_work(
                    argparse.Namespace(work_command="list", root=None), self.data_dir
                ),
                0,
            )
        self.assertEqual(json.loads(list_output.getvalue())["projects"], [])

        show_output = io.StringIO()
        with contextlib.redirect_stdout(show_output):
            self.assertEqual(
                builder_pulse.command_work(
                    argparse.Namespace(work_command="show", root=str(project_root)),
                    self.data_dir,
                ),
                0,
            )
        self.assertFalse(json.loads(show_output.getvalue())["enrolled"])

    def test_reenrollment_renames_project_without_splitting_its_stable_id(self) -> None:
        project_root = self.workspace / "renamed-product"
        project_root.mkdir()
        self.enroll_project(
            project_root,
            project_id="stable-product-id",
            project_label="Old Product Name",
        )
        args = argparse.Namespace(
            work_command="enroll",
            project="New Product Name",
            project_id=None,
            root=str(project_root),
        )

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder_pulse.command_work(args, self.data_dir), 0)

        context = builder_pulse.load_work_contexts(self.data_dir)[
            builder_pulse.repository_key(self.data_dir, project_root)
        ]
        self.assertEqual(context["project_id"], "stable-product-id")
        self.assertEqual(context["project_label"], "New Product Name")

    def test_non_latin_project_name_gets_a_stable_distinct_identifier(self) -> None:
        first_root = self.workspace / "first-unicode-product"
        second_root = self.workspace / "second-unicode-product"
        first_root.mkdir()
        second_root.mkdir()

        for root, label in ((first_root, "नमस्ते"), (second_root, "こんにちは")):
            args = argparse.Namespace(
                work_command="enroll",
                project=label,
                project_id=None,
                root=str(root),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(builder_pulse.command_work(args, self.data_dir), 0)

        contexts = builder_pulse.load_work_contexts(self.data_dir)
        first = contexts[builder_pulse.repository_key(self.data_dir, first_root)]
        second = contexts[builder_pulse.repository_key(self.data_dir, second_root)]
        self.assertEqual(first["project_label"], "नमस्ते")
        self.assertEqual(second["project_label"], "こんにちは")
        self.assertRegex(first["project_id"], r"^project-[a-f0-9]{12}$")
        self.assertRegex(second["project_id"], r"^project-[a-f0-9]{12}$")
        self.assertNotEqual(first["project_id"], second["project_id"])

    def test_mixed_script_project_names_do_not_collapse_to_the_same_identifier(self) -> None:
        first = builder_pulse.project_identifier_from_label("Project 项目")
        second = builder_pulse.project_identifier_from_label("Project 产品")

        self.assertRegex(first, r"^project-[a-f0-9]{12}$")
        self.assertRegex(second, r"^project-[a-f0-9]{12}$")
        self.assertNotEqual(first, second)

    def test_project_path_keys_are_private_installation_keyed_values(self) -> None:
        first_key = builder_pulse.repository_key(self.data_dir, self.workspace)
        second_data_dir = self.workspace / "second-plugin-data"
        second_data_dir.mkdir()
        second_key = builder_pulse.repository_key(second_data_dir, self.workspace)

        self.assertRegex(first_key, r"^[a-f0-9]{32}$")
        self.assertRegex(second_key, r"^[a-f0-9]{32}$")
        self.assertNotEqual(first_key, second_key)
        self.assertNotIn(
            str(self.workspace),
            builder_pulse.identity_path(self.data_dir).read_text(encoding="utf-8"),
        )

    def test_non_git_enrollment_covers_descendants_without_storing_its_path(self) -> None:
        project_root = self.workspace / "non-git-product"
        nested = project_root / "src" / "feature"
        nested.mkdir(parents=True)
        self.enroll_project(
            project_root,
            project_id="non-git-product",
            project_label="Non Git Product",
        )

        enrolled = builder_pulse.enrolled_work_context(self.data_dir, nested)

        assert enrolled is not None
        key, context, matched_root = enrolled
        self.assertEqual(key, builder_pulse.repository_key(self.data_dir, project_root))
        self.assertEqual(context["project_label"], "Non Git Product")
        self.assertEqual(matched_root, project_root.resolve())
        self.assertNotIn(
            str(project_root),
            (self.data_dir / "contexts.json").read_text(encoding="utf-8"),
        )

    def test_monorepo_subfolder_enrollment_does_not_cover_siblings(self) -> None:
        monorepo = self.workspace / "monorepo"
        enrolled_app = monorepo / "apps" / "enrolled"
        sibling_app = monorepo / "apps" / "private"
        (monorepo / ".git").mkdir(parents=True)
        enrolled_app.mkdir(parents=True)
        sibling_app.mkdir(parents=True)

        with contextlib.redirect_stdout(io.StringIO()):
            result = builder_pulse.command_work(
                argparse.Namespace(
                    work_command="enroll",
                    project="Enrolled App",
                    project_id=None,
                    root=str(enrolled_app),
                ),
                self.data_dir,
            )

        self.assertEqual(result, 0)
        enrolled = builder_pulse.enrolled_work_context(
            self.data_dir, enrolled_app / "src"
        )
        sibling = builder_pulse.enrolled_work_context(self.data_dir, sibling_app)
        assert enrolled is not None
        self.assertEqual(enrolled[2], enrolled_app.resolve())
        self.assertEqual(enrolled[1]["project_label"], "Enrolled App")
        self.assertIsNone(sibling)
        persisted = (self.data_dir / "contexts.json").read_text(encoding="utf-8")
        self.assertNotIn(str(monorepo), persisted)
        self.assertNotIn(str(enrolled_app), persisted)

    def test_first_explicit_enrollment_does_not_revive_legacy_feature_context(self) -> None:
        project_root = self.workspace / "legacy-project"
        project_root.mkdir()
        key = builder_pulse.repository_key(self.data_dir, project_root)
        builder_pulse.atomic_write_json(
            self.data_dir / "contexts.json",
            {
                key: {
                    "project_id": "legacy-cohort-label",
                    "feature_id": "old-inferred-feature",
                    "feature_label": "Old inferred feature",
                }
            },
        )

        with contextlib.redirect_stdout(io.StringIO()):
            result = builder_pulse.command_work(
                argparse.Namespace(
                    work_command="enroll",
                    project="Confirmed Product",
                    project_id=None,
                    root=str(project_root),
                ),
                self.data_dir,
            )

        self.assertEqual(result, 0)
        context = builder_pulse.load_work_contexts(self.data_dir)[key]
        self.assertEqual(context["project_id"], "confirmed-product")
        self.assertEqual(context["project_label"], "Confirmed Product")
        self.assertNotIn("feature_id", context)
        self.assertNotIn("feature_label", context)

    def test_project_enrollment_rejects_home_and_filesystem_roots(self) -> None:
        for root in (Path.home(), Path(Path.home().anchor)):
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                result = builder_pulse.command_work(
                    argparse.Namespace(
                        work_command="enroll",
                        project="Too broad",
                        project_id=None,
                        root=str(root),
                    ),
                    self.data_dir,
                )
            self.assertEqual(result, 2)
            self.assertIn("not the home", output.getvalue())

    def test_filesystem_root_detection_covers_windows_drives_and_unc_shares(self) -> None:
        for root in (
            PureWindowsPath("C:\\"),
            PureWindowsPath("D:\\"),
            PureWindowsPath("\\\\server\\share\\"),
        ):
            with self.subTest(root=str(root)):
                self.assertTrue(builder_pulse.is_filesystem_root(root))

        self.assertFalse(builder_pulse.is_filesystem_root(PureWindowsPath("C:\\project")))
        self.assertFalse(
            builder_pulse.is_filesystem_root(
                PureWindowsPath("\\\\server\\share\\project")
            )
        )

    def test_project_enrollment_rejects_a_parent_of_the_home_directory(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = builder_pulse.command_work(
                argparse.Namespace(
                    work_command="enroll",
                    project="Too broad",
                    project_id=None,
                    root=str(Path.home().parent),
                ),
                self.data_dir,
            )
        self.assertEqual(result, 2)
        self.assertIn("one of its parents", output.getvalue())

    def test_same_session_keeps_independent_state_for_each_enrolled_project(self) -> None:
        self.claim_locally()
        first_root = self.workspace / "first-product"
        second_root = self.workspace / "second-product"
        (first_root / ".git").mkdir(parents=True)
        (second_root / ".git").mkdir(parents=True)
        self.enroll_project(
            first_root, project_id="first-product", project_label="First Product"
        )
        self.enroll_project(
            second_root, project_id="second-product", project_label="Second Product"
        )

        with mock.patch.object(
            builder_pulse, "utc_now_ms", return_value=1_787_721_000_000
        ):
            first = builder_pulse.record_hook_event(
                self.data_dir,
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "shared-session",
                    "cwd": str(first_root),
                },
                self.config,
            )
        with mock.patch.object(
            builder_pulse, "utc_now_ms", return_value=1_787_721_000_001
        ):
            second = builder_pulse.record_hook_event(
                self.data_dir,
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "shared-session",
                    "cwd": str(second_root),
                },
                self.config,
            )

        assert first is not None and second is not None
        self.assertEqual(first["projectLabel"], "First Product")
        self.assertEqual(second["projectLabel"], "Second Product")
        self.assertEqual(len(list((self.data_dir / "states").glob("*.json"))), 2)

    def test_work_enroll_list_and_unenroll_manage_only_explicit_projects(self) -> None:
        first_root = self.workspace / "first-product"
        second_root = self.workspace / "second-product"
        (first_root / ".git").mkdir(parents=True)
        (second_root / ".git").mkdir(parents=True)

        for root, label in (
            (first_root, "First Product"),
            (second_root, "Second Product"),
        ):
            output = io.StringIO()
            args = argparse.Namespace(
                work_command="enroll",
                project=label,
                project_id=None,
                root=str(root),
            )
            with contextlib.redirect_stdout(output):
                self.assertEqual(builder_pulse.command_work(args, self.data_dir), 0)
            shown = json.loads(output.getvalue())
            self.assertTrue(shown["enrolled"])
            self.assertEqual(shown["projectLabel"], label)
            self.assertEqual(shown["capture"], "enrolled-projects-only")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                builder_pulse.command_work(
                    argparse.Namespace(work_command="list", root=None), self.data_dir
                ),
                0,
            )
        listed = json.loads(output.getvalue())
        self.assertEqual(listed["scope"], "explicit-project-allowlist")
        self.assertEqual(
            {project["projectLabel"] for project in listed["projects"]},
            {"First Product", "Second Product"},
        )
        self.assertNotIn(str(first_root), output.getvalue())
        self.assertNotIn(str(second_root), output.getvalue())

        first_key = builder_pulse.repository_key(self.data_dir, first_root)
        second_key = builder_pulse.repository_key(self.data_dir, second_root)
        contexts = builder_pulse.load_work_contexts(self.data_dir)
        first_scope = {
            "_contextKey": first_key,
            "_scopeKey": contexts[first_key]["scope_key"],
            "projectId": "first-product",
            "projectLabel": "First Product",
            "projectScope": "explicit",
        }
        second_scope = {
            "_contextKey": second_key,
            "_scopeKey": contexts[second_key]["scope_key"],
            "projectId": "second-product",
            "projectLabel": "Second Product",
            "projectScope": "explicit",
        }
        builder_pulse.atomic_write_jsonl(
            self.data_dir / "outbox.jsonl",
            [
                {"eventId": "first", **first_scope},
                {"eventId": "second", **second_scope},
            ],
        )
        builder_pulse.atomic_write_jsonl(
            self.data_dir / "prompt-outbox.jsonl",
            [
                {"promptId": "first", **first_scope},
                {"promptId": "second", **second_scope},
            ],
        )
        states_dir = self.data_dir / "states"
        states_dir.mkdir(exist_ok=True)
        builder_pulse.atomic_write_json(
            states_dir / "first.json",
            {
                "contextKey": first_key,
                "scopeKey": contexts[first_key]["scope_key"],
                **builder_pulse.wire_payload(first_scope),
            },
        )
        builder_pulse.atomic_write_json(
            states_dir / "second.json",
            {
                "contextKey": second_key,
                "scopeKey": contexts[second_key]["scope_key"],
                **builder_pulse.wire_payload(second_scope),
            },
        )

        unenroll_output = io.StringIO()
        with contextlib.redirect_stdout(unenroll_output):
            self.assertEqual(
                builder_pulse.command_work(
                    argparse.Namespace(
                        work_command="unenroll", root=str(first_root)
                    ),
                    self.data_dir,
                ),
                0,
            )
        self.assertEqual(
            json.loads(unenroll_output.getvalue())["discardedPending"],
            {"lifecycle": 1, "prompts": 1, "states": 1},
        )
        contexts = builder_pulse.load_work_contexts(self.data_dir)
        self.assertNotIn(first_key, contexts)
        self.assertIn(second_key, contexts)
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl"),
            [{"eventId": "second", **second_scope}],
        )
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "prompt-outbox.jsonl"),
            [{"promptId": "second", **second_scope}],
        )
        self.assertFalse((states_dir / "first.json").exists())
        self.assertTrue((states_dir / "second.json").exists())

        second_unenroll = io.StringIO()
        with contextlib.redirect_stdout(second_unenroll):
            self.assertEqual(
                builder_pulse.command_work(
                    argparse.Namespace(work_command="unenroll", root=str(first_root)),
                    self.data_dir,
                ),
                0,
            )
        self.assertFalse(json.loads(second_unenroll.getvalue())["removed"])
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl"),
            [{"eventId": "second", **second_scope}],
        )
        self.assertIsNone(
            builder_pulse.record_hook_event(
                self.data_dir,
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "unenrolled",
                    "cwd": str(first_root),
                },
                self.config,
            )
        )

    def test_unenroll_preserves_another_folder_with_the_same_project_name(self) -> None:
        first_root = self.workspace / "same-name-one"
        second_root = self.workspace / "same-name-two"
        first_root.mkdir()
        second_root.mkdir()
        self.enroll_project(
            first_root,
            project_id="same-product",
            project_label="Same Product",
        )
        self.enroll_project(
            second_root,
            project_id="same-product",
            project_label="Same Product",
        )
        contexts = builder_pulse.load_work_contexts(self.data_dir)
        first_key = builder_pulse.repository_key(self.data_dir, first_root)
        second_key = builder_pulse.repository_key(self.data_dir, second_root)
        records = []
        for event_id, key, context in (
            ("first", first_key, contexts[first_key]),
            ("second", second_key, contexts[second_key]),
        ):
            records.append(
                {
                    "eventId": event_id,
                    "_contextKey": key,
                    "_scopeKey": context["scope_key"],
                    "projectId": "same-product",
                    "projectLabel": "Same Product",
                    "projectScope": "explicit",
                    "featureId": "member-search",
                    "featureLabel": "Member search",
                }
            )
        builder_pulse.atomic_write_jsonl(self.data_dir / "outbox.jsonl", records)

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                builder_pulse.command_work(
                    argparse.Namespace(work_command="unenroll", root=str(first_root)),
                    self.data_dir,
                ),
                0,
            )

        remaining_contexts = builder_pulse.load_work_contexts(self.data_dir)
        self.assertNotIn(first_key, remaining_contexts)
        self.assertIn(second_key, remaining_contexts)
        self.assertEqual(
            [
                record["eventId"]
                for record in builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl")
            ],
            ["second"],
        )

    def test_unenroll_waits_for_an_in_flight_send_and_blocks_later_sends(self) -> None:
        self.claim_locally()
        event = self.record(
            {"hook_event_name": "SessionStart", "session_id": "scope-race"},
            1_787_721_000_000,
        )
        assert event is not None
        send_started = threading.Event()
        release_send = threading.Event()
        unenroll_done = threading.Event()
        flush_result: dict[str, int] = {}

        def blocked_delivery(*_args: object, **_kwargs: object) -> tuple[bool, str]:
            send_started.set()
            self.assertTrue(release_send.wait(timeout=2))
            return True, "delivered"

        def run_flush() -> None:
            flush_result.update(builder_pulse.flush_outbox(self.data_dir, self.config))

        def run_unenroll() -> None:
            with contextlib.redirect_stdout(io.StringIO()):
                builder_pulse.command_work(
                    argparse.Namespace(
                        work_command="unenroll", root=str(self.workspace)
                    ),
                    self.data_dir,
                )
            unenroll_done.set()

        with mock.patch.object(
            builder_pulse, "deliver_event", side_effect=blocked_delivery
        ):
            flush_thread = threading.Thread(target=run_flush)
            flush_thread.start()
            self.assertTrue(send_started.wait(timeout=2))
            unenroll_thread = threading.Thread(target=run_unenroll)
            unenroll_thread.start()
            self.assertFalse(unenroll_done.wait(timeout=0.1))
            release_send.set()
            flush_thread.join(timeout=10)
            unenroll_thread.join(timeout=10)

        self.assertFalse(flush_thread.is_alive())
        self.assertFalse(unenroll_thread.is_alive())
        self.assertTrue(unenroll_done.is_set())
        self.assertEqual(flush_result["delivered"], 1)
        self.assertEqual(builder_pulse.load_work_contexts(self.data_dir), {})
        self.assertEqual(builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl"), [])

        with mock.patch.object(builder_pulse, "deliver_event") as delivered:
            self.assertEqual(
                builder_pulse.deliver_scoped_record(
                    self.data_dir,
                    event,
                    self.config,
                    "a" * 64,
                    "https://pulse.example",
                    prompt=False,
                ),
                (False, "scope_inactive"),
            )
        delivered.assert_not_called()

    def test_disable_purges_pending_data_and_beats_a_stale_enable_environment(
        self,
    ) -> None:
        self.claim_locally()
        self.record(
            {"hook_event_name": "SessionStart", "session_id": "disable-purge"},
            1_787_721_000_000,
        )
        self.record_prompt(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "disable-prompt",
                "prompt": "private pending prompt",
            },
            1_787_721_000_001,
        )
        output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"BUILDER_PULSE_ENABLED": "1"},
        ), contextlib.redirect_stdout(output):
            self.assertEqual(
                builder_pulse.command_config(
                    argparse.Namespace(
                        config_command="set", key="enabled", value="false"
                    ),
                    self.data_dir,
                ),
                0,
            )
            self.assertFalse(builder_pulse.load_config(self.data_dir)["enabled"])

        response = json.loads(output.getvalue())
        self.assertGreaterEqual(response["discardedPendingOnDisable"]["lifecycle"], 1)
        self.assertEqual(response["discardedPendingOnDisable"]["prompts"], 1)
        self.assertEqual(builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl"), [])
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "prompt-outbox.jsonl"), []
        )
        self.assertEqual(list((self.data_dir / "states").glob("*.json")), [])

    def test_disabled_flushes_and_final_delivery_never_call_the_network(self) -> None:
        self.claim_locally()
        event = self.record(
            {"hook_event_name": "SessionStart", "session_id": "disabled-send"},
            1_787_721_000_000,
        )
        assert event is not None
        with contextlib.redirect_stdout(io.StringIO()):
            builder_pulse.command_config(
                argparse.Namespace(
                    config_command="set", key="enabled", value="false"
                ),
                self.data_dir,
            )
        builder_pulse.atomic_write_jsonl(self.data_dir / "outbox.jsonl", [event])

        with mock.patch.object(builder_pulse, "deliver_event") as delivered:
            flush = builder_pulse.flush_outbox(self.data_dir, self.config)
            direct = builder_pulse.deliver_scoped_record(
                self.data_dir,
                event,
                self.config,
                "a" * 64,
                "https://pulse.example",
                prompt=False,
            )

        self.assertEqual(flush["delivered"], 0)
        self.assertEqual(flush["remaining"], 1)
        self.assertEqual(direct, (False, "disabled"))
        delivered.assert_not_called()

    def test_disable_waits_for_an_in_flight_send_and_blocks_later_sends(self) -> None:
        self.claim_locally()
        event = self.record(
            {"hook_event_name": "SessionStart", "session_id": "disable-race"},
            1_787_721_000_000,
        )
        assert event is not None
        send_started = threading.Event()
        release_send = threading.Event()
        disable_done = threading.Event()

        def blocked_delivery(*_args: object, **_kwargs: object) -> tuple[bool, str]:
            send_started.set()
            self.assertTrue(release_send.wait(timeout=2))
            return True, "delivered"

        def run_flush() -> None:
            builder_pulse.flush_outbox(self.data_dir, self.config)

        def run_disable() -> None:
            with contextlib.redirect_stdout(io.StringIO()):
                builder_pulse.command_config(
                    argparse.Namespace(
                        config_command="set", key="enabled", value="false"
                    ),
                    self.data_dir,
                )
            disable_done.set()

        with mock.patch.object(
            builder_pulse, "deliver_event", side_effect=blocked_delivery
        ):
            flush_thread = threading.Thread(target=run_flush)
            flush_thread.start()
            self.assertTrue(send_started.wait(timeout=2))
            disable_thread = threading.Thread(target=run_disable)
            disable_thread.start()
            self.assertFalse(disable_done.wait(timeout=0.1))
            release_send.set()
            flush_thread.join(timeout=10)
            disable_thread.join(timeout=10)

        self.assertTrue(disable_done.is_set())
        with mock.patch.object(builder_pulse, "deliver_event") as delivered:
            self.assertEqual(
                builder_pulse.deliver_scoped_record(
                    self.data_dir,
                    event,
                    self.config,
                    "a" * 64,
                    "https://pulse.example",
                    prompt=False,
                ),
                (False, "disabled"),
            )
        delivered.assert_not_called()

    def test_scope_migration_discards_only_legacy_records_and_preserves_identity(self) -> None:
        self.claim_locally()
        identity_before = builder_pulse.identity_path(self.data_dir).read_bytes()
        contexts_before = (self.data_dir / "contexts.json").read_bytes()
        context_key = builder_pulse.repository_key(self.data_dir, self.workspace)
        context = builder_pulse.load_work_contexts(self.data_dir)[context_key]
        explicit = {
            "_contextKey": context_key,
            "_scopeKey": context["scope_key"],
            "projectId": context["project_id"],
            "projectLabel": context["project_label"],
            "projectScope": "explicit",
            "featureId": context["feature_id"],
            "featureLabel": context["feature_label"],
        }
        builder_pulse.atomic_write_jsonl(
            self.data_dir / "outbox.jsonl",
            [{"eventId": "legacy"}, {"eventId": "explicit", **explicit}],
        )
        builder_pulse.atomic_write_jsonl(
            self.data_dir / "prompt-outbox.jsonl",
            [{"promptId": "legacy"}, {"promptId": "explicit", **explicit}],
        )
        states_dir = self.data_dir / "states"
        states_dir.mkdir()
        builder_pulse.atomic_write_json(states_dir / "legacy.json", {"state": "building"})
        builder_pulse.atomic_write_json(
            states_dir / "explicit.json",
            {
                "state": "building",
                "contextKey": context_key,
                "scopeKey": context["scope_key"],
                **builder_pulse.wire_payload(explicit),
            },
        )

        marker = builder_pulse.ensure_project_scope_migration(self.data_dir)

        self.assertEqual(
            marker["discardedUnscoped"],
            {"contexts": 0, "lifecycle": 1, "prompts": 1, "states": 1},
        )
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "outbox.jsonl"),
            [{"eventId": "explicit", **explicit}],
        )
        self.assertEqual(
            builder_pulse.read_jsonl(self.data_dir / "prompt-outbox.jsonl"),
            [{"promptId": "explicit", **explicit}],
        )
        self.assertFalse((states_dir / "legacy.json").exists())
        self.assertTrue((states_dir / "explicit.json").exists())
        self.assertEqual(builder_pulse.identity_path(self.data_dir).read_bytes(), identity_before)
        self.assertEqual((self.data_dir / "contexts.json").read_bytes(), contexts_before)

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
        self.assertEqual(
            result,
            {"delivered": 0, "discarded": 0, "quarantined": 1, "remaining": 0},
        )
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

    def test_status_distinguishes_policy_from_effective_capture(self) -> None:
        self.claim_locally()
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory)
            output = io.StringIO()

            with mock.patch.object(builder_pulse.Path, "cwd", return_value=outside), \
                contextlib.redirect_stdout(output):
                self.assertEqual(
                    builder_pulse.command_status(
                        argparse.Namespace(project=None, json=True), self.data_dir
                    ),
                    0,
                )

        status = json.loads(output.getvalue())
        self.assertEqual(status["promptCapturePolicy"], "on")
        self.assertTrue(status["builderPulseEnabled"])
        self.assertFalse(status["currentProjectEnrolled"])
        self.assertFalse(status["effectivePromptCapture"])


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
            self.assertIn("${PLUGIN_ROOT}", hook["command"])
            self.assertIn("builder_pulse.sh", hook["command"])
            self.assertEqual(
                hook["commandWindows"],
                'call "%PLUGIN_ROOT%\\scripts\\builder_pulse.cmd"',
            )
            self.assertNotIn("CLAUDE_PLUGIN_ROOT", hook["commandWindows"])
            self.assertNotIn("cd /d", hook["commandWindows"])

    def test_windows_hook_wrapper_quotes_the_python_script_path(self) -> None:
        wrapper = builder_pulse.PLUGIN_ROOT / "scripts" / "builder_pulse.cmd"
        contents = wrapper.read_text(encoding="utf-8")
        self.assertIn('py -3 "%~dp0builder_pulse.py" hook', contents)
        self.assertIn('sys.version_info.minor in range(11, 100)', contents)
        self.assertNotIn('sys.version_info <', contents)
        self.assertNotIn('sys.version_info ^<', contents)

    def test_platform_hook_launcher_really_starts_and_returns_valid_hook_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = builder_pulse.verify_hook_launcher(Path(directory))
        self.assertEqual(result, {"ready": True, "hookStatus": "launcher_verified"})

    def test_windows_launcher_verification_uses_the_registered_root_command(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "{}\n", "")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            builder_pulse.os, "name", "nt"
        ), mock.patch.object(
            builder_pulse.subprocess, "run", return_value=completed
        ) as run:
            result = builder_pulse.verify_hook_launcher(Path(directory))

        self.assertEqual(result, {"ready": True, "hookStatus": "launcher_verified"})
        command = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(
            command,
            [
                "cmd",
                "/d",
                "/c",
                'call "%PLUGIN_ROOT%\\scripts\\builder_pulse.cmd"',
            ],
        )
        self.assertNotIn("/s", command)
        self.assertEqual(options["env"]["PLUGIN_ROOT"], str(builder_pulse.PLUGIN_ROOT))
        self.assertNotIn("cwd", options)

    def test_windows_launcher_supports_a_unc_plugin_root_without_changing_cwd(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess([], 0, "{}\n", "")
        unc_root = Path(r"\\server\share\builder pulse")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            builder_pulse.os, "name", "nt"
        ), mock.patch.object(
            builder_pulse, "PLUGIN_ROOT", unc_root
        ), mock.patch.object(
            builder_pulse.subprocess, "run", return_value=completed
        ) as run:
            result = builder_pulse.verify_hook_launcher(Path(directory))

        self.assertEqual(result, {"ready": True, "hookStatus": "launcher_verified"})
        self.assertEqual(
            run.call_args.args[0][-1],
            'call "%PLUGIN_ROOT%\\scripts\\builder_pulse.cmd"',
        )
        self.assertEqual(run.call_args.kwargs["env"]["PLUGIN_ROOT"], str(unc_root))
        self.assertNotIn("cwd", run.call_args.kwargs)

    def test_activation_stops_before_the_server_when_launcher_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            identity = builder_pulse.ensure_identity(data_dir)
            identity.update(
                {
                    "builderId": "builder-1",
                    "memberId": "growthx-member-1",
                    "builderName": "Builder One",
                    "installationToken": "a" * 64,
                    "claimedEndpoint": "https://pulse.example",
                    "promptCapture": "on",
                }
            )
            builder_pulse.atomic_write_json(
                builder_pulse.identity_path(data_dir), identity
            )

            output = io.StringIO()
            with mock.patch.object(
                builder_pulse,
                "inspect_codex_hooks",
                return_value={"ready": True, "hookStatus": "trusted"},
            ), mock.patch.object(
                builder_pulse,
                "verify_hook_launcher",
                return_value={
                    "ready": False,
                    "hookStatus": "launcher_unavailable",
                },
            ), mock.patch.object(
                builder_pulse, "http_post_json"
            ) as posted, contextlib.redirect_stdout(output):
                result = builder_pulse.command_activate(data_dir)

            self.assertEqual(result, 3)
            self.assertEqual(
                json.loads(output.getvalue())["hookStatus"],
                "launcher_unavailable",
            )
            posted.assert_not_called()

    def test_session_end_is_synchronous_for_codex(self) -> None:
        manifest = json.loads((builder_pulse.PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
        hook = manifest["hooks"]["SessionEnd"][0]["hooks"][0]
        self.assertNotIn("async", hook)

    def test_user_prompt_submit_is_async_so_telemetry_cannot_block_a_prompt(self) -> None:
        manifest = json.loads((builder_pulse.PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
        hook = manifest["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertIs(hook.get("async"), True)


if __name__ == "__main__":
    unittest.main()
