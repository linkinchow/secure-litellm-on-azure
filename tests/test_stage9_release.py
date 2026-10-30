import copy
from datetime import datetime, timezone
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import yaml

from scripts.stage9_release import ROOT, PRODUCTION_CHECKS, generate, release_checks, validate_origin_snapshot, validate_release, validate_what_if
from scripts.preview_stage9 import preview


def evidence_config(phase="canary", environment="test"):
    return {
        "phase": phase, "baseDomain": "customer.test.invalid", "environmentName": environment,
        "privateOrigin": {"privateLinkServiceId": "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/synthetic/providers/Microsoft.Network/privateLinkServices/api", "privateLinkLocation": "westus3"},
        "adminPrivateOrigin": {"privateLinkServiceId": "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/synthetic/providers/Microsoft.Network/privateLinkServices/admin", "privateLinkLocation": "westus3"},
        "adminAllowedCidrs": ["20.30.40.50/32"],
        "logAnalyticsWorkspaceName": "synthetic-logs", "frontDoorId": "22222222-2222-4222-8222-222222222222",
        "wafMode": "Prevention" if phase == "production" else "Detection", "rateLimitPerMinute": 600, "adminRateLimitPerMinute": 120,
        "changeTicket": "SYNTHETIC-ONLY", "approvedBy": ["33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"],
        "checks": {name: {"passed": True, "observedAt": datetime.now(timezone.utc).isoformat(), "report": "synthetic://offline-test"} for name in PRODUCTION_CHECKS},
    }


class Stage9ReleaseTests(unittest.TestCase):
    def test_test_canary_uses_live_gates_without_manual_check_index(self):
        config = evidence_config()
        config.update(authenticationMode="native", auditMode="native", telemetryEnabled=False)
        config.pop("checks")
        self.assertEqual(release_checks(config), set())
        validate_release(config)
        config["checks"] = {name: {"passed": False, "observedAt": "", "report": ""} for name in PRODUCTION_CHECKS}
        validate_release(config)
        for patch in ({"wafMode": "Prevention"}, {"frontDoorId": "REPLACE_ID"}, {"approvedBy": []}):
            with self.assertRaises(ValueError):
                validate_release({**config, **patch})
        for patch in ({"authenticationMode": "unsupported"}, {"telemetryEnabled": None}):
            with self.assertRaises(ValueError):
                validate_release({**config, **patch})

        entra = evidence_config()
        self.assertNotEqual(release_checks(entra), set())
        entra.pop("checks")
        with self.assertRaisesRegex(ValueError, "Missing passing evidence"):
            validate_release(entra)

    def test_native_authentication_release_requires_virtual_key_and_private_admin_login(self):
        config = evidence_config(environment="prod")
        config["authenticationMode"] = "native"
        with self.assertRaisesRegex(ValueError, "native_admin_password_login|native_virtual_key_acl"):
            validate_release(config)
        required = release_checks(config)
        self.assertNotIn("entra_backend_acl", required)
        self.assertIn("native_virtual_key_acl", required)
        self.assertIn("native_admin_password_login", required)
        config["checks"] = {name: {"passed": True, "observedAt": datetime.now(timezone.utc).isoformat(), "report": "synthetic://native-auth-contract"} for name in required}
        validate_release(config)
        with self.assertRaisesRegex(ValueError, "managed customer-runtime"):
            generate(config, ROOT / "temp")
        invalid = copy.deepcopy(config)
        invalid["authenticationMode"] = "unsupported"
        with self.assertRaisesRegex(ValueError, "authenticationMode"):
            validate_release(invalid)

    def test_optional_l3_alert_opt_out_needs_a_decision_instead_of_alert_evidence(self):
        config = evidence_config(environment="prod")
        config.update(auditMode="native", telemetryEnabled=False, authenticationMode="native", optionalL3Enabled=True)
        enabled = release_checks(config)
        self.assertTrue({"l3_protocol_capture", "l3_delivery_recovery", "l3_query_hold", "l3_alerts"} <= enabled)
        self.assertNotIn("l3_alerts_disabled", enabled)
        config["optionalL3AlertsEnabled"] = False
        disabled = release_checks(config)
        self.assertTrue({"l3_protocol_capture", "l3_delivery_recovery", "l3_query_hold", "l3_alerts_disabled"} <= disabled)
        self.assertNotIn("l3_alerts", disabled)
        config["checks"] = {name: {"passed": True, "observedAt": datetime.now(timezone.utc).isoformat(), "report": "synthetic://l3"} for name in disabled}
        validate_release(config)
        del config["checks"]["l3_alerts_disabled"]
        with self.assertRaisesRegex(ValueError, "l3_alerts_disabled"):
            validate_release(config)
        config["optionalL3AlertsEnabled"] = "false"
        with self.assertRaisesRegex(ValueError, "optionalL3AlertsEnabled"):
            release_checks(config)
        self.assertNotIn("l3_alerts_disabled", release_checks({**config, "optionalL3AlertsEnabled": False, "phase": "prepare"}))

    def test_native_release_requires_real_native_checks_not_l3_placeholders(self):
        config = evidence_config(environment="prod")
        config.update(auditMode="native", telemetryEnabled=False)
        with self.assertRaisesRegex(ValueError, "native_"):
            validate_release(config)
        required = release_checks(config)
        self.assertNotIn("l3_governance_and_recovery", required)
        self.assertIn("native_audit_access", required)
        self.assertIn("telemetry_disabled", required)
        config["checks"] = {name: {"passed": True, "observedAt": datetime.now(timezone.utc).isoformat(), "report": "synthetic://native-contract-only"} for name in required}
        validate_release(config)
        for name in ("native_spend_logs", "native_audit_access", "native_retention_recovery"):
            invalid = copy.deepcopy(config)
            invalid["checks"][name]["passed"] = False
            with self.assertRaisesRegex(ValueError, name):
                validate_release(invalid)
        with self.assertRaisesRegex(ValueError, "managed customer-runtime"):
            generate(config, ROOT / "temp")
        config["telemetryEnabled"] = True
        with self.assertRaisesRegex(ValueError, "telemetry_alerts"):
            validate_release(config)

    def test_template_cannot_authorize_a_release(self):
        config = json.loads((ROOT / "infra/edge/release.example.json").read_text())
        with self.assertRaises(ValueError):
            validate_release(config)

    def test_prod_canary_requires_stage8_and_protocol_evidence(self):
        config = evidence_config(environment="prod")
        validate_release(config)
        for key in ("stage8_capture_architecture", "l3_governance_and_recovery", "required_protocol_matrix"):
            invalid = copy.deepcopy(config)
            del invalid["checks"][key]
            with self.assertRaisesRegex(ValueError, key):
                validate_release(invalid)

    def test_production_requires_prevention_and_recent_distinct_approvals(self):
        config = evidence_config("production")
        validate_release(config)
        validate_release({**config, "modelListRateLimitPerMinute": 60})
        for patch in ({"wafMode": "Detection"}, {"approvedBy": [config["approvedBy"][0]]}, {"frontDoorId": "REPLACE_ID"},
                      {"modelListRateLimitPerMinute": 0}, {"modelListRateLimitPerMinute": 10001}, {"modelListRateLimitPerMinute": "120"}):
            with self.assertRaises(ValueError):
                validate_release({**config, **patch})
        config["checks"]["canary_slo"]["observedAt"] = "2000-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            validate_release(config)

    def test_what_if_does_not_silently_accept_changes_or_unsupported_analysis(self):
        self.assertEqual(validate_what_if({"status": "Succeeded", "changes": [{"changeType": "Create"}]}), {"Create": 1})
        for kind in ("Modify", "Delete", "Unsupported", "Deploy", None):
            with self.assertRaises(ValueError):
                validate_what_if({"status": "Succeeded", "changes": [{"changeType": kind}]})
        with self.assertRaises(ValueError):
            validate_what_if({"status": "Failed", "changes": []})

    def test_private_origin_rejects_public_frontend_and_wrong_subnet_policy(self):
        frontend_id = "/synthetic/frontend"
        load_balancer = {"sku": {"name": "Standard"}, "properties": {"frontendIPConfigurations": [{"id": frontend_id, "properties": {"privateIPAddress": "10.30.4.10", "subnet": {"id": "/synthetic/subnet"}}}]}}
        subnet = {"properties": {"privateLinkServiceNetworkPolicies": "Disabled"}}
        validate_origin_snapshot(load_balancer, frontend_id, subnet)
        load_balancer["properties"]["frontendIPConfigurations"][0]["properties"]["publicIPAddress"] = {"id": "/synthetic/public-ip"}
        with self.assertRaises(ValueError):
            validate_origin_snapshot(load_balancer, frontend_id, subnet)
        with self.assertRaisesRegex(ValueError, "missing"):
            validate_origin_snapshot({"sku": {"name": "Standard"}, "properties": {"frontendIPConfigurations": []}}, frontend_id, subnet)

    def test_admin_release_requires_distinct_origin_and_bounded_source_allowlist(self):
        config = evidence_config()
        validate_release(config)
        invalid = copy.deepcopy(config)
        invalid["adminPrivateOrigin"] = invalid["privateOrigin"]
        with self.assertRaisesRegex(ValueError, "separate"):
            validate_release(invalid)
        invalid = copy.deepcopy(config)
        invalid["adminAllowedCidrs"] = ["0.0.0.0/0"]
        with self.assertRaisesRegex(ValueError, "public|broader"):
            validate_release(invalid)

    def test_render_has_no_dns_or_deployment_side_effect_and_prepare_disables_traffic(self):
        (ROOT / "temp").mkdir(exist_ok=True)
        for phase in ("prepare", "canary", "production"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
                generate(evidence_config(phase), Path(directory))
                parameters = json.loads((Path(directory) / "edge.parameters.json").read_text())["parameters"]
                self.assertEqual(parameters["enableApiTraffic"]["value"], phase != "prepare")
                self.assertEqual(parameters["enableAdminTraffic"]["value"], phase != "prepare")
                self.assertEqual(parameters["baseDomain"]["value"], "customer.test.invalid")
                self.assertEqual(parameters["modelListRateLimitPerMinute"]["value"], 120)
                self.assertFalse(json.loads((Path(directory) / "release-summary.json").read_text())["deploymentPerformed"])

    def test_preview_uses_only_what_if_and_keeps_payloads_ignored(self):
        (ROOT / "temp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.preview_stage9.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"status":"Succeeded","changes":[]}'
            run.return_value.stderr = ''
            result = preview("synthetic-rg", "edge", Path(directory))
            command = run.call_args.args[0]
            self.assertEqual(command[:4], ["az", "deployment", "group", "what-if"])
            self.assertNotIn("create", command)
            self.assertFalse(result["deploymentPerformed"])
            self.assertEqual((Path(directory) / "edge.what-if.json").stat().st_mode & 0o777, 0o600)

    def test_approved_preview_binds_edge_domain_and_id_to_only_api_proxy(self):
        config = evidence_config()
        (ROOT / "temp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            generate(config, Path(directory))
            result = subprocess.run(["kubectl", "kustomize", directory], capture_output=True, text=True, check=True)
            documents = list(yaml.safe_load_all(result.stdout))
            for plane in ("api", "admin"):
                deployment = next(item for item in documents if item["kind"] == "Deployment" and item["metadata"]["name"] == f"llm-{plane}-proxy")
                env = {item["name"]: item.get("value") for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertEqual(env.get("FRONT_DOOR_ID"), config["frontDoorId"])
                ingress = next(item for item in documents if item["kind"] == "Ingress" and item["metadata"]["name"] == f"llm-{plane}")
                self.assertEqual(ingress["spec"]["rules"][0]["host"], f"llm-{plane}.{config['baseDomain']}")