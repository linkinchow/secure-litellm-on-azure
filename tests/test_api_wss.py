import copy
from contextlib import ExitStack
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from local_execution.api_wss import assert_protocol_changes, desired_waf, update
from scripts.api_protocol import api_method_rules
from scripts.customer_migration import ROOT, MigrationError
from scripts.migration_deploy import group_id
from scripts.private_ingress import native_api_rule, preserve_native_front_door_binding, render_ingress
from tests.test_private_ingress_runtime import ingress_config


def live_waf(identifier):
    return {
        "id": identifier, "location": "Global",
        "sku": {"name": "Premium_AzureFrontDoor", "capacity": None, "family": None,
                "model": None, "size": None, "tier": None},
        "tags": {"owner": "test"},
        "properties": {
            "provisioningState": "Succeeded", "resourceState": "Enabled", "securityPolicyLinks": [],
            "policySettings": {"enabledState": "Enabled", "mode": "Detection",
                               "requestBodyCheck": "Enabled", "logScrubbing": {"state": "Enabled"}},
            "managedRules": {"managedRuleSets": [{"ruleSetType": "Microsoft_DefaultRuleSet", "ruleSetVersion": "2.1"}]},
            "customRules": {"rules": [
                {"name": "BlockNonPost", "priority": 10, "enabledState": "Enabled", "ruleType": "MatchRule",
                 "action": "Block", "matchConditions": [
                     {"matchVariable": "RequestMethod", "operator": "Equal", "negateCondition": True, "matchValue": ["POST"]},
                 ]},
                {"name": "RateLimitApi", "priority": 20, "enabledState": "Enabled", "ruleType": "RateLimitRule",
                 "action": "Block", "rateLimitThreshold": 600, "rateLimitDurationInMinutes": 1,
                 "groupBy": [{"variableName": "SocketAddr"}]},
            ]},
        },
    }


class ApiWssTests(unittest.TestCase):
    def test_ingress_scopes_both_branches_and_preserves_binding(self):
        config = ingress_config()
        config["application"] = {"authentication": {"mode": "native", "adminUsername": "gateway-admin"}}
        host = "llm-api." + config["baseDomain"]
        fdid = "11111111-1111-4111-8111-111111111111"
        rule = native_api_rule(host, fdid)
        self.assertTrue(rule.startswith(f"Host(`{host}`) && ((Method(`POST`)"))
        self.assertIn("Method(`GET`) && (Path(`/responses`) || Path(`/v1/responses`))", rule)
        self.assertTrue(rule.endswith(f"&& HeaderRegexp(`X-Azure-FDID`, `(?i)^{fdid}$`)"))
        self.assertIn("HeaderRegexp(`Upgrade`", rule)
        self.assertIn("HeaderRegexp(`Connection`", rule)
        documents = render_ingress(config, "api", "example.azurecr.io/traefik@sha256:" + "a" * 64, ["10.30.0.0/16"])
        current = copy.deepcopy(next(item for item in documents if item["kind"] == "ConfigMap"))
        routes = yaml.safe_load(current["data"]["routes.yaml"])
        paths = " || ".join(f"Path(`{path}`)" for path in
                           ("/chat/completions", "/v1/chat/completions", "/responses", "/v1/responses", "/embeddings", "/v1/embeddings"))
        routes["http"]["routers"]["api"]["rule"] = (
            f"Host(`{host}`) && Method(`POST`) && ({paths})"
            f" && HeaderRegexp(`X-Azure-FDID`, `(?i)^{fdid}$`)"
        )
        current["data"]["routes.yaml"] = yaml.safe_dump(routes)
        upgraded = preserve_native_front_door_binding(config, documents, current)
        observed = yaml.safe_load(next(item for item in upgraded if item["kind"] == "ConfigMap")["data"]["routes.yaml"])
        self.assertEqual(observed["http"]["routers"]["api"]["rule"], rule)

    def test_waf_method_matrix(self):
        def blocked(method, path, upgrade="", connection=""):
            variables = {"RequestMethod": method, "RequestUri": path,
                         "Upgrade": upgrade, "Connection": connection}
            def matches(condition):
                value = variables[condition.get("selector", condition["matchVariable"])]
                if condition.get("transforms") == ["Lowercase"]:
                    value = value.lower()
                if condition["operator"] == "Equal":
                    match = value in condition["matchValue"]
                else:
                    match = any(re.search(pattern, value) for pattern in condition["matchValue"])
                return not match if condition.get("negateCondition") else match
            return any(all(matches(condition) for condition in rule["matchConditions"]) for rule in api_method_rules())
        for path in ("/responses", "/v1/responses", "/v1/responses?model=coding",
                     "https://api.example.com/responses", "https://api.example.com:443/v1/responses?model=coding"):
            self.assertFalse(blocked("POST", path))
            self.assertFalse(blocked("GET", path, "WebSocket", "keep-alive, Upgrade"))
            self.assertFalse(blocked("GET", path, "websocket", "keep-alive,\tupgrade"))
            for upgrade, connection in (("", ""), ("websocket", ""), ("http", "upgrade"), ("websocket", "not-upgrade")):
                self.assertTrue(blocked("GET", path, upgrade, connection))
        for path in ("/v1/chat/completions", "/v1/embeddings", "/v1/models", "/ui/", "/responses/extra",
                     "https://api.example.com/v1/models", "https://api.example.com/v1/embeddings?path=/responses",
                     "http://api.example.com/responses"):
            self.assertTrue(blocked("GET", path, "websocket", "upgrade"))
        for method in ("HEAD", "PUT", "DELETE", "TRACE", "OPTIONS"):
            self.assertTrue(blocked(method, "/v1/responses", "websocket", "upgrade"))
        self.assertTrue(all(rule["action"] == "Block" for rule in api_method_rules()))

    def test_single_resource_preserves_mode_rate_managed_rules_and_tags(self):
        live = live_waf("/synthetic/api")
        before = copy.deepcopy(live)
        desired = desired_waf(live, live["id"])
        self.assertEqual(live, before)
        self.assertEqual(desired["properties"]["customRules"]["rules"][:-1], api_method_rules())
        self.assertEqual(desired["properties"]["customRules"]["rules"][-1], live["properties"]["customRules"]["rules"][-1])
        for key in ("policySettings", "managedRules"):
            self.assertEqual(desired["properties"][key], live["properties"][key])
        self.assertEqual(desired["tags"], live["tags"])
        self.assertEqual(desired["sku"], {"name": "Premium_AzureFrontDoor"})
        self.assertNotIn("securityPolicyLinks", desired["properties"])
        new_live = {**live, "properties": {**live["properties"], **desired["properties"]}}
        for condition in new_live["properties"]["customRules"]["rules"][1]["matchConditions"]:
            condition.setdefault("negateCondition", False)
            condition.setdefault("transforms", [])
            condition.setdefault("selector", None)
        for rule in new_live["properties"]["customRules"]["rules"][:-1]:
            rule.update(groupBy=[], rateLimitDurationInMinutes=None, rateLimitThreshold=None)
        self.assertEqual(desired_waf(new_live, live["id"])["properties"]["policySettings"]["mode"], "Detection")

    def test_sku_requires_valid_name_and_excludes_get_only_fields(self):
        for sku in (None, {}, {"name": None}, {"name": "Classic_AzureFrontDoor"}, "invalid"):
            live = live_waf("/synthetic/api")
            live["sku"] = sku
            with self.subTest(sku=sku), self.assertRaisesRegex(MigrationError, "reviewed.*SKU"):
                desired_waf(live, live["id"])
        live = live_waf("/synthetic/api")
        live["sku"].update(tier="read-only-tier", capacity=1)
        self.assertEqual(desired_waf(live, live["id"])["sku"], {"name": "Premium_AzureFrontDoor"})

    def test_live_azure_null_and_empty_defaults_do_not_hide_rule_changes(self):
        live = live_waf("/synthetic/api")
        rule = live["properties"]["customRules"]["rules"][0]
        rule.update(groupBy=[], rateLimitDurationInMinutes=None, rateLimitThreshold=None)
        rule["matchConditions"][0].update(selector=None, transforms=[])
        self.assertEqual(desired_waf(live, live["id"])["properties"]["customRules"]["rules"][:-1], api_method_rules())
        rule["groupBy"] = [{"variableName": "Header", "selector": "unapproved"}]
        with self.assertRaisesRegex(MigrationError, "baseline"):
            desired_waf(live, live["id"])

    def test_unknown_policy_and_rules_are_not_overwritten(self):
        for change in ("unknown-property", "unknown-rule", "mode", "disabled", "rate-priority", "scope"):
            live = live_waf("/synthetic/api")
            if change == "unknown-property":
                live["properties"]["unknown"] = True
            elif change == "unknown-rule":
                live["properties"]["customRules"]["rules"][0]["action"] = "Allow"
            elif change == "mode":
                live["properties"]["policySettings"]["mode"] = "unexpected"
            elif change == "disabled":
                live["properties"]["policySettings"]["enabledState"] = "Disabled"
            elif change == "rate-priority":
                live["properties"]["customRules"]["rules"][1]["priority"] = 10
            else:
                live["id"] = "/synthetic/admin"
            with self.subTest(change=change), self.assertRaises(MigrationError):
                desired_waf(live, "/synthetic/api")

    def test_what_if_scope_is_fail_closed(self):
        change = {"changeType": "Modify", "resourceId": "/synthetic/api",
                  "delta": [{"path": "properties.customRules.rules"}]}
        assert_protocol_changes([change], "/synthetic/api")
        for bad in (
            {**change, "changeType": "Create"}, {**change, "changeType": "Delete"},
            {**change, "resourceId": "/synthetic/admin"}, {**change, "delta": []},
            {**change, "delta": [{"path": "properties.policySettings.mode"}]},
            {**change, "delta": [{"path": "properties.managedRules"}]},
            {**change, "delta": [{"path": "tags"}]},
        ):
            with self.subTest(bad=bad), self.assertRaises(MigrationError):
                assert_protocol_changes([bad], "/synthetic/api")

    def test_plan_execute_approval_and_post_read_verification(self):
        config = ingress_config()
        config["application"] = {"authentication": {"mode": "native", "adminUsername": "gateway-admin"}}
        settings = {"features": {"allowTrafficRelease": True}}
        identifier = group_id(config) + "/providers/Microsoft.Network/frontDoorWebApplicationFirewallPolicies/api"
        live = live_waf(identifier)
        desired = desired_waf(live, identifier)
        after = {**live, "properties": {**live["properties"], **desired["properties"]}}
        edge = {"profileResourceId": group_id(config) + "/providers/Microsoft.Cdn/profiles/front",
                "adminWafId": group_id(config) + "/providers/Microsoft.Network/frontDoorWebApplicationFirewallPolicies/admin"}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as name, ExitStack() as stack:
            directory = Path(name)
            stack.enter_context(patch("local_execution.api_wss.load_config", return_value=(config, settings)))
            stack.enter_context(patch("local_execution.api_wss.reviewed_revision", return_value="a" * 40))
            stack.enter_context(patch("local_execution.api_wss.operation_root", return_value=directory))
            auth = stack.enter_context(patch("local_execution.api_wss.authenticate_azure"))
            approval = stack.enter_context(patch("local_execution.api_wss.generate_report",
                                                return_value={"phase": "canary", "wafMode": "Detection"}))
            stack.enter_context(patch("local_execution.api_wss.deployed_edge", return_value=edge))
            resource = stack.enter_context(patch("local_execution.api_wss.deployed_resource"))
            azure = Mock()
            azure.scoped.side_effect = lambda args: (
                {"status": "Succeeded", "changes": [{"changeType": "Modify", "resourceId": identifier,
                 "delta": [{"path": "properties.customRules.rules"}]}]} if "what-if" in args
                else {"properties": {"provisioningState": "Succeeded"}}
            )
            stack.enter_context(patch("local_execution.api_wss.AzureCommands", return_value=azure))
            association = {"properties": {"parameters": {"wafPolicy": {"id": identifier}}}}
            resource.side_effect = [association, live]
            plan = update(directory / "customer.local.json", "plan", "", "approved-change", ["approver"])
            self.assertEqual(approval.call_args.args[3:5], ("approved-change", ["approver"]))
            auth.assert_called_once()
            self.assertFalse(plan["deployed"])
            self.assertFalse(any("create" in call.args[0] for call in azure.scoped.call_args_list))
            resource.side_effect = [association, live]
            with self.assertRaisesRegex(MigrationError, "plan changed"):
                update(directory / "customer.local.json", "execute", "f" * 64, "approved-change", ["approver"])
            resource.side_effect = [association, live, live, association, after, association]
            result = update(directory / "customer.local.json", "execute", plan["planSha256"], "approved-change", ["approver"])
            self.assertTrue(result["deployed"])
            self.assertEqual(result["status"], "completed")
            self.assertFalse(result["protocolAccepted"])
            self.assertEqual(json.loads((directory / "api-wss-template.json").read_text())["resources"], [desired])
            resource.side_effect = [association, live, {**live, "tags": {"drift": "changed"}}]
            calls = len(azure.scoped.call_args_list)
            with self.assertRaisesRegex(MigrationError, "Live WAF"):
                update(directory / "customer.local.json", "execute", plan["planSha256"], "approved-change", ["approver"])
            self.assertFalse(any("create" in call.args[0] for call in azure.scoped.call_args_list[calls:]))
            resource.side_effect = [association, live, live, association, live]
            with self.assertRaisesRegex(MigrationError, "approved WSS rules"):
                update(directory / "customer.local.json", "execute", plan["planSha256"], "approved-change", ["approver"])
            self.assertEqual(json.loads((directory / "wss-summary.json").read_text())["status"], "failed")
            resource.side_effect = [association, live, live, {"properties": {"parameters": {"wafPolicy": {"id": "/changed"}}}}]
            calls = len(azure.scoped.call_args_list)
            with self.assertRaisesRegex(MigrationError, "Live WAF"):
                update(directory / "customer.local.json", "execute", plan["planSha256"], "approved-change", ["approver"])
            self.assertFalse(any("create" in call.args[0] for call in azure.scoped.call_args_list[calls:]))
