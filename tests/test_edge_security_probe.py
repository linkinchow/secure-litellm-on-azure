import argparse
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from tests import edge_security_probe as probe


class Response(io.BytesIO):
    def __init__(self, status, body=b"{}", headers=None):
        super().__init__(body)
        self.code = status
        self.headers = headers or {}


class EdgeSecurityProbeTests(unittest.TestCase):
    def test_only_explicit_https_origins_without_credential_or_path(self):
        self.assertEqual(probe.base_url("https://api.example.invalid/"), "https://api.example.invalid")
        for value in ("http://api.example.invalid", "https://user:key@api.example.invalid",
                      "https://api.example.invalid/ui", "https://api.example.invalid?key=private",
                      "https://api.example.invalid:444", "https://api.example.invalid:bad"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                probe.base_url(value)

    def test_suite_is_bounded_and_negative_cases_never_use_real_key(self):
        cases = probe.cases("https://admin.example.invalid", "approved-model")
        self.assertEqual(len(cases), 14)
        self.assertEqual({(item["method"], item["path"]) for item in cases if "/models" in item["path"]},
                         {("POST", "/v1/models"), ("GET", "/v1/models"), ("GET", "/v1/models/security-probe-no-model")})
        self.assertEqual(sum(bool(item.get("realKey")) for item in cases), 1)
        self.assertTrue(all(not item.get("realKey") for item in cases if item["goal"] != "authorized-inference"))
        self.assertTrue(all(len(item["path"]) < 200 for item in cases))

    def test_real_key_sent_only_to_authorized_origin_and_never_recorded(self):
        case = probe.cases(baseline_model="approved-model")[-1]
        opener = Mock()
        opener.open.return_value = Response(200, b'{"private":"sensitive-output"}',
                                            {"X-Azure-Ref": "20261010T000000Z-example"})
        result = probe.request_case(opener, "https://api.example.invalid", case, "sk-local-private")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.example.invalid/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-local-private")
        self.assertNotIn("sk-local-private", json.dumps(result))
        self.assertNotIn("sensitive-output", json.dumps(result))
        self.assertEqual(result["trackingReference"], "20261010T000000Z-example")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)

    def test_negative_sample_does_not_use_supplied_real_key(self):
        opener = Mock()
        opener.open.return_value = Response(401)
        case = next(case for case in probe.cases() if case["name"] == "waf-xss-sample")
        result = probe.request_case(opener, "https://api.example.invalid", case, "sk-private")
        self.assertIsNone(opener.open.call_args.args[0].get_header("Authorization"))
        self.assertEqual(result["result"], "waf-evidence-required")

    def test_403_is_not_automatically_classified_as_waf_success(self):
        case = probe.cases()[0]
        self.assertEqual(probe.classification(case, 403, False), "authentication-denial-observed-layer-unverified")
        self.assertEqual(probe.classification(case, 403, True), "edge-rejection-suspected-log-required")

    def test_http_error_response_retains_only_safe_evidence(self):
        opener = Mock()
        opener.open.side_effect = HTTPError("https://api.example.invalid", 403, "private error",
                                            {"X-Azure-Ref": "safe-reference"},
                                            io.BytesIO(b"<html>The request is blocked.</html>"))
        result = probe.request_case(opener, "https://api.example.invalid", probe.cases()[0])
        self.assertEqual(result["status"], 403)
        self.assertEqual(result["trackingReference"], "safe-reference")
        self.assertNotIn("private error", json.dumps(result))

    def test_transport_error_withholds_exception_and_credentials(self):
        opener = Mock()
        opener.open.side_effect = URLError("private-proxy-password")
        result = probe.request_case(opener, "https://api.example.invalid", probe.cases()[0])
        self.assertEqual(result["result"], "transport-error")
        self.assertNotIn("private-proxy-password", json.dumps(result))

    def test_redirect_handler_does_not_forward_credentials(self):
        self.assertIsNone(probe.NoRedirect().redirect_request(None, None, 302, "", {},
                                                             "https://other.invalid"))

    def test_redirect_is_recorded_not_followed_or_called_success(self):
        opener = Mock()
        opener.open.side_effect = HTTPError("https://api.example.invalid", 302, "",
                                            {"Location": "https://other.invalid"}, io.BytesIO(b""))
        result = probe.request_case(opener, "https://api.example.invalid",
                                    probe.cases(baseline_model="approved-model")[-1], "sk-private")
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(result["result"], "unexpected-status")
        self.assertNotIn("other.invalid", json.dumps(result))

    def test_stop_on_throttling_and_transport_failure(self):
        for status in (429, None):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.json"
                args = probe.parser().parse_args(["--api-url", "https://api.example.invalid",
                                                  "--confirm-authorized-target", "--output-json", str(path)])
                opener = Mock()
                if status is None:
                    opener.open.side_effect = URLError("synthetic-error")
                else:
                    opener.open.return_value = Response(status)
                with patch.object(probe, "build_opener", return_value=opener), \
                        patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                    self.assertEqual(probe.run(args), 1)
                self.assertEqual(opener.open.call_count, 1)
                self.assertFalse(json.loads(path.read_text())["completed"])

    def test_existing_reports_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            probe.write_report(path, {"ddosTested": False})
            self.assertEqual(json.loads(path.read_text()), {"ddosTested": False})
            with self.assertRaises(ValueError):
                probe.write_report(path, {"changed": True})

    def test_confirmation_required_before_network(self):
        args = probe.parser().parse_args(["--api-url", "https://api.example.invalid",
                                          "--output-json", "unused.json"])
        with patch.object(probe, "build_opener") as opener, self.assertRaises(ValueError):
            probe.run(args)
        opener.assert_not_called()

    def test_stop_on_server_failure_without_retry_and_write_incomplete_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            args = probe.parser().parse_args(["--api-url", "https://api.example.invalid",
                                              "--confirm-authorized-target", "--output-json", str(path)])
            opener = Mock()
            opener.open.return_value = Response(503)
            with patch.object(probe, "build_opener", return_value=opener), patch("sys.stdout", io.StringIO()):
                self.assertEqual(probe.run(args), 1)
            self.assertEqual(opener.open.call_count, 1)
            report = json.loads(path.read_text())
            self.assertFalse(report["completed"])
            self.assertFalse(report["rateLimitTested"])
            self.assertFalse(report["ddosTested"])

    def test_sequential_suite_with_delay_and_no_key_read_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            args = probe.parser().parse_args(["--api-url", "https://api.example.invalid",
                                              "--confirm-authorized-target", "--direct",
                                              "--output-json", str(path)])
            opener = Mock()
            opener.open.side_effect = [Response(case["expected"][0]) for case in probe.cases()]
            with patch.object(probe, "build_opener", return_value=opener), \
                    patch.object(probe.time, "sleep") as sleep, patch("sys.stdout", io.StringIO()), \
                    patch.object(probe.os.environ, "get") as environment:
                self.assertEqual(probe.run(args), 0)
            environment.assert_not_called()
            self.assertEqual(sleep.call_count, 10)
            self.assertTrue(all(call.args == (2,) for call in sleep.call_args_list))
            self.assertEqual(json.loads(path.read_text())["requestCount"], 11)


if __name__ == "__main__":
    unittest.main()
