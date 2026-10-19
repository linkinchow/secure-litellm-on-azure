import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.api_protocol import api_method_rules
from tests import waf_security_probe as probe
from tests.test_edge_security_probe import Response


SUB = "11111111-1111-4111-8111-111111111111"
ROOT = f"/subscriptions/{SUB}/resourceGroups/customer-rg/providers/"
PROFILE = ROOT + "Microsoft.Cdn/profiles/afd-test"


def config():
    return {"environment": "test", "location": "westus3", "baseDomain": "customer.invalid",
            "azure": {"subscriptionId": SUB, "tenantId": SUB}, "target": {"resourceGroup": "customer-rg"},
            "application": {"authentication": {"mode": "native"}},
            "parameters": {"edge": {"logAnalyticsWorkspaceName": "customer-law",
                                   "adminAllowedCidrs": ["192.0.2.1/32"], "rateLimitPerMinute": 600}}}


def policy(plane="api"):
    cfg = config()
    rules = api_method_rules() if plane == "api" else [{
        "name": "BlockUnapprovedAdminSources", "priority": 5, "enabledState": "Enabled",
        "ruleType": "MatchRule", "action": "Block", "matchConditions": [{
            "matchVariable": "SocketAddr", "operator": "IPMatch", "negateCondition": True,
            "matchValue": cfg["parameters"]["edge"]["adminAllowedCidrs"]}]},
        {"name": "BlockUnsafeMethods", "priority": 10, "enabledState": "Enabled", "ruleType": "MatchRule",
         "action": "Block", "matchConditions": [{
             "matchVariable": "RequestMethod", "operator": "Equal", "matchValue": ["TRACE", "TRACK"]}]}]
    rules.append({"name": "RateLimitApi" if plane == "api" else "RateLimitAdmin", "priority": 20,
                  "enabledState": "Enabled", "ruleType": "RateLimitRule", "action": "Block",
                  "rateLimitThreshold": 600 if plane == "api" else 120, "rateLimitDurationInMinutes": 1,
                  "matchConditions": [{"matchVariable": "RequestUri", "operator": "BeginsWith", "matchValue": ["/"]}]})
    return {"properties": {"policySettings": {"enabledState": "Enabled", "mode": "Prevention",
            "requestBodyCheck": "Enabled", "logScrubbing": {"state": "Enabled"}},
            "customRules": {"rules": rules}, "managedRules": {"managedRuleSets": [
                {"ruleSetType": kind, "ruleSetVersion": version, "ruleSetAction": "Block"}
                for kind, version in (("Microsoft_DefaultRuleSet", "2.1"), ("Microsoft_BotManagerRuleSet", "1.1"))]}}}


def snapshot():
    components = {"api.policy": probe.digest({"synthetic": True})}
    return {"profileId": PROFILE, "workspaceId": SUB, "fingerprint": probe.digest(components),
            "fingerprintVersion": 2, "componentFingerprints": components,
            "checks": [probe.check("synthetic", True)],
            "policies": {plane: {"id": ROOT + "Microsoft.Network/frontDoorWebApplicationFirewallPolicies/" + plane,
                                "name": plane, "mode": "Prevention"} for plane in ("api", "admin")}}


def report():
    return {"operation": "waf-security-verification", "startedAt": "2026-10-19T01:00:00+00:00",
            "completedAt": "2026-10-19T01:01:00+00:00", "snapshot": snapshot(),
            "apiOrigin": "https://llm-api.customer.invalid", "adminOrigin": "https://llm-admin.customer.invalid",
            "observations": [{"name": "method", "plane": "api", "rule": "BlockUnsupportedApiMethods",
                "trackingReference": "abc/123+=_test", "status": 403, "expectedStatusObserved": True}]}


def row(rule="BlockUnsupportedApiMethods", action="Block", mode="Prevention"):
    return {"reference": "abc/123+=_test", "rule": rule, "action": action, "mode": mode,
            "policy": "api", "host": "llm-api.customer.invalid"}


class WafSecurityProbeTests(unittest.TestCase):
    def test_snapshot_resolves_associations_and_approved_workspace_without_resource_writes(self):
        cfg = config()
        class Azure:
            def run(self, args):
                self.assert_read(args)
                if args[:2] == ["account", "show"]:
                    return {"id": SUB, "tenantId": SUB}
                if args[:3] == ["deployment", "group", "show"]:
                    return {"state": "Succeeded", "edge": {
                        "routeId": PROFILE + "/afdEndpoints/api/routes/api-only",
                        "adminRouteId": PROFILE + "/afdEndpoints/admin/routes/admin-only",
                        "privateOrigin": {"privateLinkServiceId": ROOT + "Microsoft.Network/privateLinkServices/api"},
                        "adminPrivateOrigin": {"privateLinkServiceId": ROOT + "Microsoft.Network/privateLinkServices/admin"},
                        "apiHost": "llm-api.customer.invalid", "adminHost": "llm-admin.customer.invalid"}}
                if args[0] == "rest":
                    plane = "admin" if "/private-admin/" in args[-1] else "api"
                    return {"value": [{"properties": {"enforceCertificateNameCheck": True,
                            "sharedPrivateLinkResource": {"status": "Approved",
                                "privateLink": {"id": ROOT + "Microsoft.Network/privateLinkServices/" + plane}}}}]}
                if args[:2] == ["monitor", "diagnostic-settings"]:
                    return [{"workspaceId": ROOT + "Microsoft.OperationalInsights/workspaces/customer-law",
                             "logs": [{"category": "FrontDoorWebApplicationFirewallLog", "enabled": True}]}]
                if args[:3] == ["monitor", "log-analytics", "workspace"]:
                    return {"id": ROOT + "Microsoft.OperationalInsights/workspaces/customer-law", "customerId": SUB}
                raise AssertionError(args)

            def assert_read(self, args):
                if any(value in args for value in ("create", "delete", "update", "set", "list-keys")):
                    raise AssertionError("Unexpected write")

            def resource(self, identifier, version):
                plane = "admin" if "admin" in identifier else "api"
                if "/securityPolicies/" in identifier:
                    return {"properties": {"parameters": {
                        "wafPolicy": {"id": ROOT + "Microsoft.Network/frontDoorWebApplicationFirewallPolicies/" + plane},
                        "associations": [{"patternsToMatch": ["/*"], "domains": [{
                            "id": PROFILE + "/customDomains/llm-" + plane}]}]}}}
                if "/frontDoorWebApplicationFirewallPolicies/" in identifier:
                    return policy(plane)
                if "/routes/" in identifier:
                    return {"properties": {"enabledState": "Enabled", "supportedProtocols": ["Https"],
                            "forwardingProtocol": "HttpsOnly", "linkToDefaultDomain": "Disabled"}}
                if "/customDomains/" in identifier:
                    return {"properties": {"tlsSettings": {"certificateType": "ManagedCertificate",
                                                          "minimumTlsVersion": "TLS12"}}}
                raise AssertionError(identifier)
        result = probe.snapshot(cfg, Azure())
        self.assertEqual(result["workspaceId"], SUB)
        self.assertTrue(all(item["result"] == "pass" for item in result["checks"]))
        self.assertNotEqual(result["policies"]["api"]["id"], result["policies"]["admin"]["id"])

    def test_policy_contract_detects_drift_not_just_rule_name(self):
        for plane in ("api", "admin"):
            self.assertTrue(all(item["result"] == "pass" for item in probe.policy_checks(policy(plane), plane, config())))
        for change in ("method", "allow", "scrubbing", "threshold", "managed-exclusion"):
            live = policy()
            if change == "method":
                live["properties"]["customRules"]["rules"][0]["matchConditions"][0]["matchValue"] = ["POST"]
            elif change == "allow":
                live["properties"]["customRules"]["rules"].append({"action": "Allow"})
            elif change == "scrubbing":
                live["properties"]["policySettings"]["logScrubbing"]["state"] = "Disabled"
            elif change == "managed-exclusion":
                live["properties"]["managedRules"]["managedRuleSets"][0]["exclusions"] = [{"synthetic": True}]
            else:
                live["properties"]["customRules"]["rules"][-1]["rateLimitThreshold"] = 1
            self.assertTrue(any(item["result"] == "fail" for item in probe.policy_checks(live, "api", config())))

    def test_default_condition_fields_do_not_create_false_drift(self):
        live = policy()
        live["properties"]["customRules"]["rules"][-1]["groupBy"] = [{"variableName": "SocketAddr"}]
        for rule in live["properties"]["customRules"]["rules"]:
            for condition in rule["matchConditions"]:
                condition.setdefault("negateCondition", False)
                condition.setdefault("transforms", [])
                condition.setdefault("selector", None)
        self.assertTrue(all(item["result"] == "pass" for item in probe.policy_checks(live, "api", config())))
        live["properties"]["customRules"]["rules"][-1]["groupBy"] = [{"variableName": "ClientAddr"}]
        self.assertTrue(any(item["result"] == "fail" for item in probe.policy_checks(live, "api", config())))

    def test_bounded_cases_never_send_real_credentials_or_model_calls(self):
        cases = probe.cases("https://llm-admin.customer.invalid", "unapproved")
        self.assertEqual(len(cases), 10)
        self.assertTrue(all(not item.get("realKey") for item in cases))
        self.assertTrue(all("model" not in item.get("body", {}) or
                            item["body"]["model"] == "security-probe-no-model" for item in cases))
        self.assertTrue(any(item.get("headers", {}).get("Connection") == "close" for item in cases))
        self.assertTrue(any(item["name"] == "admin-spoofed-forwarded-source" for item in cases))
        self.assertTrue(all(not any(word in item["path"] for word in ("/generate", "/delete", "/update")) for item in cases))

    def test_tracking_ref_supports_real_frontdoor_base64_and_withholds_payload(self):
        opener = Mock()
        opener.open.return_value = Response(403, b"synthetic-private-body", {"X-Azure-Ref": "abc/123+=_test"})
        case = probe.cases(None, None)[3]
        observation = probe.request_case(opener, "https://llm-api.customer.invalid", case)
        self.assertEqual(observation["trackingReference"], "abc/123+=_test")
        self.assertNotIn("synthetic-private-body", json.dumps(observation))
        self.assertEqual(opener.open.call_args.args[0].get_header("Upgrade"), "websocket")

    def test_403_requires_matching_policy_host_reference_rule_mode_and_action(self):
        self.assertEqual(probe.correlate(report(), [row()])[0]["result"], "prevention-block-verified")
        for field, value in (("policy", "admin"), ("host", "other.invalid"), ("reference", "different"),
                             ("rule", "RateLimitApi"), ("action", "Log"), ("mode", "Detection")):
            wrong = row()
            wrong[field] = value
            self.assertEqual(probe.correlate(report(), [wrong])[0]["result"], "inconclusive")
        self.assertEqual(probe.correlate(report(), [])[0]["result"], "inconclusive")

    def test_detection_only_never_counts_as_prevention(self):
        document = report()
        document["snapshot"]["policies"]["api"]["mode"] = "Detection"
        self.assertEqual(probe.correlate(document, [row(action="Log", mode="Detection")])[0]["result"],
                         "detection-match-only")

    def test_managed_rule_needs_matching_attack_category_and_block_not_generic_bot(self):
        document = report()
        del document["observations"][0]["rule"]
        document["observations"][0]["ruleCategory"] = "SQLI"
        self.assertEqual(probe.correlate(document, [row("Microsoft_BotManagerRuleSet-1.1-Bot")])[0]["result"], "inconclusive")
        self.assertEqual(probe.correlate(document, [
            row("Microsoft_DefaultRuleSet-2.1-SQLI-942100", "AnomalyScoring"),
            row("Microsoft_DefaultRuleSet-2.1-BLOCK-949110"),
        ])[0]["result"], "prevention-block-verified")

    def test_kql_only_projects_metadata_and_rejects_injection(self):
        query = probe.waf_query(report())
        self.assertIn("trackingReference_s", query)
        self.assertIn(PROFILE, query)
        self.assertNotIn("matchVariableValue", query)
        self.assertNotIn("requestUri", query)
        for key, value in (("trackingReference", "x'); print secret"),):
            bad = report()
            bad["observations"][0][key] = value
            with self.assertRaises(probe.VerificationError):
                probe.waf_query(bad)
        bad = report()
        bad["completedAt"] = "2026-10-20T01:00:00+00:00"
        with self.assertRaises(probe.VerificationError):
            probe.waf_query(bad)

    def run_args(self, directory, extra=()):
        cfg = Path(directory) / "customer.json"
        cfg.write_text(json.dumps(config()))
        return probe.parser().parse_args(["run", "--config", str(cfg),
            "--api-url", "https://llm-api.customer.invalid", "--confirm-authorized-target",
            "--output-json", str(Path(directory) / "report.json"), *extra])

    def test_configuration_only_does_not_send_network_requests_and_preserves_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.run_args(directory, ["--configuration-only"])
            live = snapshot()
            live["checks"] = [probe.check("unsafe", False)]
            with patch.object(probe, "snapshot", return_value=live), patch.object(probe, "backend_checks", return_value=[]), \
                    patch.object(probe, "build_opener") as opener, patch("sys.stdout", io.StringIO()):
                self.assertEqual(probe.run(args), 1)
            opener.assert_not_called()
            self.assertEqual(json.loads(args.output_json.read_text())["snapshot"]["checks"][0]["result"], "fail")

    def test_active_suite_stops_at_429_503_or_transport_error(self):
        for status in (429, 503, None):
            with tempfile.TemporaryDirectory() as directory:
                args = self.run_args(directory)
                with patch.object(probe, "snapshot", return_value=snapshot()), \
                        patch.object(probe, "backend_checks", return_value=[]), \
                        patch.object(probe, "request_case", return_value={"status": status, "name": "test"}) as request, \
                        patch("sys.stdout", io.StringIO()):
                    self.assertEqual(probe.run(args), 1)
                self.assertEqual(request.call_count, 1)
                self.assertTrue(json.loads(args.output_json.read_text())["stoppedEarly"])

    def test_default_suite_runs_sequentially_with_delay_and_keeps_evidence_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.run_args(directory)
            live = snapshot()
            live["policies"]["api"]["mode"] = "Detection"
            def response(opener, origin, case):
                status = 401 if case.get("rule") else case["expected"][0]
                return {"name": case["name"], "status": status, "trackingReference": case["name"],
                        "expectedStatusObserved": status in case["expected"]}
            with patch.object(probe, "snapshot", return_value=live), \
                    patch.object(probe, "backend_checks", return_value=[]), \
                    patch.object(probe, "request_case", side_effect=response) as request, \
                    patch.object(probe.time, "sleep") as sleep, patch("sys.stdout", io.StringIO()):
                self.assertEqual(probe.run(args), 0)
            self.assertEqual(request.call_count, 7)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [2] * 6)
            output = json.loads(args.output_json.read_text())
            self.assertEqual(output["requestCount"], 7)
            self.assertTrue(output["pendingWafEvidence"])
            self.assertFalse(output["rateLimitTested"])
            self.assertFalse(output["modelCallsRequested"])

    def test_verify_rejects_snapshot_drift_before_log_query(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.run_args(directory)
            path = Path(directory) / "input.json"
            path.write_text(json.dumps(report()))
            args.report = path
            live = snapshot()
            live["componentFingerprints"]["api.policy"] = probe.digest({"different": True})
            live["fingerprint"] = probe.digest(live["componentFingerprints"])
            with patch.object(probe, "snapshot", return_value=live), patch.object(probe, "AzureRead") as azure:
                with self.assertRaisesRegex(probe.VerificationError, "changed"):
                    probe.verify(args)
                azure.return_value.run.assert_not_called()

    def test_fingerprint_ignores_only_known_arm_metadata_not_security_configuration(self):
        original = {"id": PROFILE + "/customDomains/llm-api", "etag": "old",
                    "systemData": {"lastModifiedAt": "old"},
                    "properties": {"provisioningState": "Succeeded", "deploymentStatus": "Succeeded",
                        "hostName": "llm-api.customer.invalid", "domainValidationState": "Approved",
                        "validationProperties": {"validationToken": "old", "expirationDate": "old"},
                        "tlsSettings": {"certificateType": "ManagedCertificate", "minimumTlsVersion": "TLS12"}}}
        bookkeeping = copy.deepcopy(original)
        bookkeeping["etag"] = "new"
        bookkeeping["systemData"]["lastModifiedAt"] = "new"
        bookkeeping["properties"]["deploymentStatus"] = "InProgress"
        bookkeeping["properties"]["validationProperties"]["validationToken"] = "new"
        self.assertEqual(probe.resource_contract(original, "domain"), probe.resource_contract(bookkeeping, "domain"))
        for field, value in (("hostName", "other.invalid"), ("tlsSettings", {"minimumTlsVersion": "TLS10"})):
            changed = copy.deepcopy(original)
            changed["properties"][field] = value
            self.assertNotEqual(probe.resource_contract(original, "domain"), probe.resource_contract(changed, "domain"))
        waf = policy()
        before = probe.digest(probe.resource_contract(waf, "policy"))
        waf["properties"]["policySettings"]["mode"] = "Detection"
        self.assertNotEqual(before, probe.digest(probe.resource_contract(waf, "policy")))
        # A field with a metadata-like name inside a custom rule must not be stripped.
        nested = {"properties": {"customRules": {"rules": [{"systemData": {"synthetic": True}}]}}}
        self.assertIn("systemData", probe.resource_contract(nested, "policy")["properties"]["customRules"]["rules"][0])

    def test_semantic_collection_order_is_stable_and_component_drift_is_named(self):
        first = {"id": "one", "properties": {"value": 1}}
        second = {"id": "two", "properties": {"value": 2}}
        self.assertEqual(probe.collection_contract([first, second], "origin"),
                         probe.collection_contract([second, first], "origin"))
        before, after = snapshot(), snapshot()
        after["componentFingerprints"]["api.policy"] = probe.digest({"newMode": True})
        with self.assertRaisesRegex(probe.VerificationError, "api.policy"):
            probe.check_snapshot(before, after)
        after = snapshot()
        probe.check_snapshot(before, after)

    def test_legacy_reports_are_not_silently_upgraded_after_raw_drift(self):
        current = snapshot()
        current["legacyFingerprint"] = "a" * 64
        probe.check_snapshot({"fingerprint": "a" * 64}, current)
        with self.assertRaisesRegex(probe.VerificationError, "Legacy raw"):
            probe.check_snapshot({"fingerprint": "b" * 64}, current)
        with self.assertRaisesRegex(probe.VerificationError, "Legacy raw"):
            probe.check_snapshot({}, current)
        inconsistent = snapshot()
        inconsistent["componentFingerprints"]["api.policy"] = "changed"
        with self.assertRaisesRegex(probe.VerificationError, "inconsistent"):
            probe.check_snapshot(inconsistent, current)

    def test_verify_persists_only_bounded_rule_evidence_and_source_report_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.run_args(directory)
            args.report = Path(directory) / "input.json"
            args.report.write_text(json.dumps(report()))
            with patch.object(probe, "snapshot", return_value=snapshot()), \
                    patch.object(probe, "AzureRead") as azure, patch("sys.stdout", io.StringIO()):
                azure.return_value.run.return_value = [{**row(), "notProjectedByQuery": "private-body"}]
                self.assertEqual(probe.verify(args), 0)
            result = json.loads(args.output_json.read_text())
            self.assertTrue(result["allWafPreventionVerified"])
            self.assertNotIn("private-body", args.output_json.read_text())
            self.assertEqual(args.output_json.stat().st_mode & 0o777, 0o600)

    def test_rate_subcommand_stops_at_first_denial_and_never_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            base = self.run_args(directory)
            cfg = config()
            cfg["parameters"]["edge"]["rateLimitPerMinute"] = 5
            base.config.write_text(json.dumps(cfg))
            args = probe.parser().parse_args(["rate-limit", "--config", str(base.config),
                "--api-url", base.api_url, "--confirm-authorized-target", "--confirm-rate-limit-impact",
                "--request-budget", "10", "--output-json", str(Path(directory) / "rate.json")])
            with patch.object(probe, "snapshot", return_value=snapshot()), \
                    patch.object(probe, "request_case", return_value={
                        "name": "rate-1", "status": 403, "trackingReference": "abc/123+=_test"}) as request, \
                    patch("sys.stdout", io.StringIO()):
                self.assertEqual(probe.rate_limit(args), 0)
            self.assertEqual(request.call_count, 1)
            output = json.loads(args.output_json.read_text())
            self.assertEqual(output["maximumRequestsPerSecond"], 2)
            self.assertEqual(output["stopReason"], "denial-observed-check-correlated-rule")

    def test_rate_requires_second_confirmation_and_rejects_high_threshold_before_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            run_args = self.run_args(directory)
            args = probe.parser().parse_args(["rate-limit", "--config", str(run_args.config),
                "--api-url", run_args.api_url, "--confirm-authorized-target", "--request-budget", "100",
                "--output-json", str(Path(directory) / "rate.json")])
            with patch.object(probe, "snapshot", return_value=snapshot()), patch.object(probe, "build_opener") as opener:
                with self.assertRaisesRegex(probe.VerificationError, "explicit acceptance"):
                    probe.rate_limit(args)
                args.confirm_rate_limit_impact = True
                with self.assertRaisesRegex(probe.VerificationError, "exceeds bounded budget"):
                    probe.rate_limit(args)
                opener.assert_not_called()

    def test_read_only_azure_wrapper_never_emits_raw_service_errors(self):
        with patch.object(probe.subprocess, "run", return_value=Mock(returncode=1, stdout="private", stderr="private")):
            with self.assertRaises(probe.VerificationError) as failure:
                probe.AzureRead(SUB).run(["resource", "list"])
            self.assertNotIn("private", str(failure.exception).split(";")[0])

    def test_redis_null_public_state_is_unverified_not_pass(self):
        azure = Mock()
        resource_id = ROOT + "Microsoft.Cache/redisEnterprise/redis"
        azure.run.return_value = [{"id": resource_id, "type": "Microsoft.Cache/redisEnterprise"}]
        azure.resource.side_effect = [{"properties": {"publicNetworkAccess": None}},
            {"properties": {"accessKeysAuthentication": "Disabled", "clientProtocol": "Encrypted"}}]
        results = probe.backend_checks(config(), azure)
        self.assertEqual(results[0]["result"], "unverified")


if __name__ == "__main__":
    unittest.main()
