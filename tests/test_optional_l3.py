import copy
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml

from scripts.customer_migration import ROOT, stage_fingerprint, validate_config
from scripts.native_audit import render_native_audit
from scripts.optional_l3 import enabled, upstream
from scripts.private_ingress import render_ingress, preserve_native_front_door_binding
from scripts.edge_binding import native_ingress_document_state
from scripts.stage9_release import release_checks
from tests.test_customer_migration import customer_config


class OptionalL3Tests(unittest.TestCase):
    def test_action_group_is_created_and_alerts_reference_it(self):
        template = (ROOT / "infra/optional-l3/main.bicep").read_text()
        self.assertIn("resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01'", template)
        self.assertIn("emailAddress: alertEmail", template)
        self.assertIn("useCommonAlertSchema: true", template)
        self.assertIn("actions: { actionGroups: [actionGroup.id] }", template)
        self.assertIn("output alertActionGroupId string = actionGroup.id", template)
        self.assertNotIn("param alertActionGroupId", template)
        self.assertNotIn("wangpeter@", template)

    def test_documented_email_resolution_uses_customer_config_and_user_mail_only(self):
        runbook = (ROOT / "docs/litellm-optional-l3-runbook-zh.md").read_text()
        source = next(snippet for snippet in re.findall(r"```bash\n(.*?)\n```", runbook, re.S)
                      if snippet.startswith('ALERT_EMAIL="$(jq'))
        cases = (
            ("customer-owner@example.com", "servicePrincipal", "", 1, True, False),
            ("", "user", "signed-in-customer@example.com", 0, True, True),
            ("", "servicePrincipal", "", 0, False, False),
            ("", "user", "", 1, False, True),
            ("", "user", "", 0, False, True),
            ("invalid-email", "user", "signed-in-customer@example.com", 0, False, False),
        )
        for email, login_type, user_mail, graph_exit, success, graph_called in cases:
            with self.subTest(email=email, login=login_type, success=success), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                cfg = {"ownerEmail": email, "location": "synthetic-region",
                       "parameters": {"platform": {"logAnalyticsWorkspaceName": "customer-law",
                           "stage4Network": {"virtualNetworkName": "customer-vnet",
                               "privateEndpointSubnetName": "customer-pe"}}}}
                (path / "customer.json").write_text(json.dumps(cfg))
                (path / "infra.parameters.json").write_text('{"parameters":{}}')
                harness = '''
set -euo pipefail
umask 077
L3_DIR="$1"
CFG="$L3_DIR/customer.json"
AKS_FACTS='{"issuer":"https://synthetic-issuer/"}'
az() {
  if [[ "$1" == "account" ]]; then
    printf '%s\\n' "$LOGIN_TYPE_TEST"
  else
    printf 'graph-called\\n' >> "$L3_DIR/graph.log"
    printf '%s\\n' "$USER_MAIL_TEST"
    return "$GRAPH_EXIT_TEST"
  fi
}
'''
                import os
                result = subprocess.run(["bash", "-c", harness + source, "test", directory],
                    env={**os.environ, "LOGIN_TYPE_TEST": login_type, "USER_MAIL_TEST": user_mail,
                         "GRAPH_EXIT_TEST": str(graph_exit)},
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                self.assertEqual((path / "graph.log").exists(), graph_called)
                updated = json.loads((path / "infra.parameters.json").read_text())
                if success:
                    self.assertEqual(updated["parameters"]["alertEmail"]["value"], email or user_mail)
                    self.assertEqual(updated["parameters"]["virtualNetworkName"]["value"], "customer-vnet")
                    self.assertEqual(updated["parameters"]["aksOidcIssuer"]["value"], "https://synthetic-issuer/")
                    self.assertEqual((path / "infra.parameters.json").stat().st_mode & 0o777, 0o600)
                else:
                    self.assertEqual(updated, {"parameters": {}})

    def setUp(self):
        self.config = customer_config()
        self.config["application"] = {
            "backendImage": "customerregistry.azurecr.io/litellm@sha256:" + "a" * 64,
            "models": [{"modelGroup": "coding", "connectionAlias": "primary",
                        "deploymentName": "model", "id": "primary-coding", "apiVersion": "v1"}],
            "authentication": {"mode": "native", "adminUsername": "gateway-admin"},
        }
        self.config["contentAudit"] = {"mode": "native", "retentionDays": 30, "contentPolicyAccepted": True}
        self.config["parameters"]["platform"]["stage4Network"].update(
            ingressSubnetName="snet-ingress", ingressSubnetPrefix="10.30.4.0/24")
        self.config["parameters"]["platform"]["azureOpenAIConnections"] = [
            {"alias": "primary", "accountName": "synthetic-model"}]
        self.config["privateIngress"] = {
            plane: {"tlsSecretId": f"https://synthetic.vault.azure.net/secrets/{plane}-tls",
                    "allowedCidrs": ["10.30.0.0/16"]}
            for plane in ("api", "admin")
        }
        self.image = "registry.invalid/traefik@sha256:" + "a" * 64

    def test_baseline_unchanged_and_explicit_native_opt_in(self):
        self.assertFalse(enabled(self.config))
        self.assertEqual(upstream(self.config, "api"), "http://litellm.litellm.svc.cluster.local:4000")
        for value in (False, {}, {"enabled": False}, {"enabled": True, "extra": True}):
            config = copy.deepcopy(self.config)
            config["l3Audit"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                enabled(config)
        self.config["l3Audit"] = {"enabled": True}
        self.assertTrue(enabled(self.config))
        validate_config(self.config, "test")
        self.assertEqual(upstream(self.config, "admin"), "http://litellm.litellm.svc.cluster.local:4000")
        self.config["application"]["authentication"]["mode"] = "entra"
        with self.assertRaises(ValueError):
            enabled(self.config)

    def test_early_stage_receipts_stay_valid_and_routing_stages_change(self):
        config = copy.deepcopy(self.config)
        config["l3Audit"] = {"enabled": True}
        for stage in (0, 1, 2, 3, 5, 6, 7):
            self.assertEqual(stage_fingerprint(config, stage), stage_fingerprint(self.config, stage))
        for stage in (4, 8, 9):
            self.assertNotEqual(stage_fingerprint(config, stage), stage_fingerprint(self.config, stage))

    def test_proxy_changes_api_only_preserves_fdid_and_network_policy(self):
        original = render_ingress(self.config, "api", self.image, ["10.30.0.0/16"])
        identifier = "11111111-1111-4111-8111-111111111111"
        old = next(item for item in original if item["kind"] == "ConfigMap")
        dynamic = yaml.safe_load(old["data"]["routes.yaml"])
        dynamic["http"]["routers"]["api"]["rule"] += f" && HeaderRegexp(`X-Azure-FDID`, `(?i)^{identifier}$`)"
        old["data"]["routes.yaml"] = yaml.safe_dump(dynamic)
        admin_before = render_ingress(self.config, "admin", self.image, ["10.30.0.0/16"])
        self.config["l3Audit"] = {"enabled": True}
        self.assertEqual(admin_before, render_ingress(self.config, "admin", self.image, ["10.30.0.0/16"]))
        documents = preserve_native_front_door_binding(
            self.config, render_ingress(self.config, "api", self.image, ["10.30.0.0/16"]), old)
        routes = yaml.safe_load(next(item for item in documents if item["kind"] == "ConfigMap")["data"]["routes.yaml"])
        self.assertEqual(routes["http"]["services"]["api"]["loadBalancer"]["servers"],
                         [{"url": "http://l3-audit.litellm.svc.cluster.local:8080"}])
        self.assertIn(identifier, routes["http"]["routers"]["api"]["rule"])
        policy = next(item for item in documents if item["kind"] == "NetworkPolicy")
        self.assertEqual(policy["spec"]["egress"][1]["ports"][0]["port"], 8080)
        self.assertEqual(policy["spec"]["egress"][1]["to"][0]["podSelector"]["matchLabels"],
                         {"app.kubernetes.io/name": "l3-audit"})
        objects = {item["kind"]: copy.deepcopy(item) for item in documents}
        for kind in ("ConfigMap", "Deployment", "Service"):
            objects[kind]["metadata"]["uid"] = "synthetic-" + kind
        state = native_ingress_document_state(self.config, objects["ConfigMap"],
                                             objects["Deployment"], objects["Service"], identifier)
        self.assertEqual(state["frontDoorHeaderBound"], identifier)

    def test_pg_content_disabled_only_when_optional_l3_is_selected_and_rollback_restores(self):
        source = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "config"},
                   "data": {"config.yaml": yaml.safe_dump({"general_settings": {}})}}]
        def general(config):
            result = render_native_audit(config, source)
            return yaml.safe_load(result[0]["data"]["config.yaml"])["general_settings"]
        self.assertTrue(general(self.config)["store_prompts_in_spend_logs"])
        self.config["l3Audit"] = {"enabled": True}
        self.assertFalse(general(self.config)["store_prompts_in_spend_logs"])
        self.assertFalse(general(self.config)["disable_spend_logs"])
        del self.config["l3Audit"]
        self.assertTrue(general(self.config)["store_prompts_in_spend_logs"])

    def test_production_release_requires_actual_optional_l3_evidence(self):
        report = {"phase": "production", "environmentName": "prod", "auditMode": "native",
                  "authenticationMode": "native", "telemetryEnabled": False, "optionalL3Enabled": True}
        self.assertTrue({"l3_protocol_capture", "l3_delivery_recovery", "l3_query_hold", "l3_alerts"}
                        <= release_checks(report))
        report["optionalL3Enabled"] = "true"
        with self.assertRaises(ValueError):
            release_checks(report)
