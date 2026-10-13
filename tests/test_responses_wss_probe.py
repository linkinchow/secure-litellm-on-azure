import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import websocket

from tests.responses_wss_probe import ProbeError, completed_response, connect, endpoint, read_completed_response, rejected, run, tool_turn


def completed(identifier="resp-private", output=None):
    return json.dumps({"type": "response.completed", "response": {
        "id": identifier, "status": "completed", "output": output or [],
    }})


class ResponsesWssProbeTests(unittest.TestCase):
    def test_origin_validation(self):
        self.assertEqual(endpoint("https://api.example.com/"), "wss://api.example.com/v1/responses")
        for value in ("http://api.example.com", "https://key@api.example.com", "https://api.example.com:8443",
                      "https://api.example.com/path", "https://api.example.com?key=private", "https://api.example.com#fragment"):
            with self.subTest(value=value), self.assertRaises(ProbeError):
                endpoint(value)

    def test_tls_and_redirects_are_not_weakened(self):
        with patch("tests.responses_wss_probe.websocket.create_connection") as create:
            connect("wss://api.example.com/v1/responses", "private-test-key", direct=True)
        options = create.call_args.kwargs
        self.assertEqual(options["redirect_limit"], 0)
        self.assertTrue(options["sslopt"]["check_hostname"])
        self.assertEqual(options["http_no_proxy"], ["*"])

    def test_auth_rejections_and_accidental_accept(self):
        for status in (401, 403):
            with patch("tests.responses_wss_probe.connect", side_effect=websocket.WebSocketBadStatusException(
                    "private response text", status_code=status)):
                self.assertTrue(rejected("wss://api.example.com/v1/responses")["denied"])
        connection = Mock()
        with patch("tests.responses_wss_probe.connect", return_value=connection), self.assertRaises(ProbeError):
            rejected("wss://api.example.com/v1/responses")
        connection.close.assert_called_once()

    def test_two_turn_continuation_and_tool_roundtrip(self):
        connection = Mock()
        connection.recv.side_effect = [json.dumps({"type": "response.created"}), completed(), completed("second")]
        identifier, _, result = completed_response(connection, "coding")
        completed_response(connection, "coding", identifier)
        second = json.loads(connection.send.call_args_list[1].args[0])
        self.assertEqual(second["previous_response_id"], "resp-private")
        self.assertTrue(result["completed"])
        connection.reset_mock()
        connection.recv.side_effect = [
            completed(output=[{"type": "function_call", "name": "workshop_ping", "call_id": "private-call"}]),
            completed("second"),
        ]
        self.assertEqual(len(tool_turn(connection, "coding")), 2)
        first, second = [json.loads(call.args[0]) for call in connection.send.call_args_list]
        self.assertEqual(first["tool_choice"]["name"], "workshop_ping")
        self.assertEqual(second["input"], [{"type": "function_call_output", "call_id": "private-call", "output": "OK"}])

    def test_events_are_bounded_and_failed_response_is_not_success(self):
        for message in ('{"type":"error","error":{"message":"private"}}',
                        '{"type":"response.failed"}', '{"type":"response.incomplete"}',
                        '{"type":"response.completed","response":{"status":"failed"}}',
                        "invalid", b"binary", "", "x" * 262145, "[]"):
            connection = Mock()
            connection.recv.return_value = message
            with self.subTest(message=message[:60]), self.assertRaises(ProbeError):
                read_completed_response(connection)
        connection = Mock()
        connection.recv.return_value = '{"type":"response.created"}'
        with self.assertRaisesRegex(ProbeError, "event count"):
            read_completed_response(connection)
        self.assertEqual(connection.recv.call_count, 256)

    def test_exclusive_private_report_without_key_or_response_body(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "report.json"
            args = SimpleNamespace(confirm_authorized_target=True, api_url="https://api.example.com",
                                   model="coding", direct=True, tool_roundtrip=False, output_json=path)
            connection = Mock()
            connection.getstatus.return_value = 101
            connection.recv.side_effect = [completed(), completed("private-second-response")]
            with patch("tests.responses_wss_probe.getpass.getpass", return_value="private-test-key"), \
                    patch("tests.responses_wss_probe.rejected", return_value={"status": 403, "denied": True}), \
                    patch("tests.responses_wss_probe.connect", return_value=connection):
                run(args)
            text = path.read_text()
            self.assertNotIn("private-test-key", text)
            self.assertNotIn("private-second-response", text)
            self.assertTrue(json.loads(text)["accepted"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ProbeError):
                run(args)

    def test_failure_writes_failure_not_acceptance(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "failure.json"
            args = SimpleNamespace(confirm_authorized_target=True, api_url="https://api.example.com",
                                   model="coding", direct=True, tool_roundtrip=False, output_json=path)
            with patch("tests.responses_wss_probe.getpass.getpass", return_value="private-key"), \
                    patch("tests.responses_wss_probe.rejected", side_effect=ProbeError("private-error")), \
                    self.assertRaises(ProbeError):
                run(args)
            self.assertFalse(json.loads(path.read_text())["accepted"])
            self.assertEqual(json.loads(path.read_text())["phase"], "missing-key")
            self.assertNotIn("private-error", path.read_text())

    def test_failure_diagnostics_are_allowlisted(self):
        cases = [
            ({"type": "error", "status": 400, "error": {
                "code": "unsupported_parameter", "type": "invalid_request_error",
                "param": "tool_choice", "message": "private-key private-body"}},
             {"errorCode": "unsupported_parameter", "errorType": "invalid_request_error",
              "errorParam": "tool_choice", "upstreamStatus": 400}),
            ({"type": "response.failed", "response": {"error": {
                "code": "private-key", "type": ["private-body"], "param": "private-param"}}},
             {"errorCode": "unrecognized", "errorType": "unrecognized", "errorParam": "unrecognized"}),
            ({"type": "response.incomplete", "response": {
                "incomplete_details": {"reason": "max_output_tokens"}, "output": ["private-body"]}},
             {"incompleteReason": "max_output_tokens"}),
            ({"type": "response.incomplete", "response": {
                "incomplete_details": {"reason": {"private-body": "private-key"}}}},
             {"incompleteReason": "unrecognized"}),
        ]
        for event, expected in cases:
            connection = Mock()
            connection.recv.side_effect = [
                json.dumps({"type": "private-key"}), json.dumps(event)]
            with self.subTest(event=event), self.assertRaises(ProbeError) as failure:
                read_completed_response(connection)
            self.assertEqual(failure.exception.code, event["type"])
            self.assertEqual(failure.exception.diagnostics,
                             {"eventTypes": sorted(["other", event["type"]]), **expected})
            self.assertNotIn("private", json.dumps(failure.exception.diagnostics))

    def test_failed_tool_continuation_preserves_first_turn(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "failure.json"
            args = SimpleNamespace(confirm_authorized_target=True, api_url="https://api.example.com",
                                   model="coding", direct=True, tool_roundtrip=True, output_json=path)
            connection = Mock()
            connection.getstatus.return_value = 101
            connection.recv.side_effect = [
                completed(output=[{"type": "function_call", "name": "workshop_ping",
                                   "call_id": "private-call"}]),
                json.dumps({"type": "error", "error": {
                    "code": "previous_response_not_found", "message": "private-response private-key"}}),
            ]
            with patch("tests.responses_wss_probe.getpass.getpass", return_value="private-key"), \
                    patch("tests.responses_wss_probe.rejected", return_value={"status": 403, "denied": True}), \
                    patch("tests.responses_wss_probe.connect", return_value=connection), \
                    self.assertRaises(ProbeError):
                run(args)
            report = json.loads(path.read_text())
            self.assertFalse(report["accepted"])
            self.assertEqual(report["phase"], "tool-result-continuation")
            self.assertEqual(report["failureCode"], "error")
            self.assertEqual(report["failureDetails"]["errorCode"], "previous_response_not_found")
            self.assertEqual(len(report["turns"]), 1)
            self.assertTrue(report["turns"][0]["completed"])
            self.assertNotIn("private", path.read_text())
            connection.close.assert_called_once()

    def test_missing_tool_call_is_distinct_from_backend_failure(self):
        connection = Mock()
        connection.recv.return_value = completed()
        progress = {}
        with self.assertRaises(ProbeError) as failure:
            tool_turn(connection, "coding", progress)
        self.assertEqual(failure.exception.code, "expected-tool-call-missing")
        self.assertEqual(progress["phase"], "tool-call-validation")
        self.assertEqual(len(progress["turns"]), 1)
        self.assertEqual(connection.send.call_count, 1)
