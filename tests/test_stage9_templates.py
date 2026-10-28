import copy
import json
from pathlib import Path
import subprocess
import unittest

import yaml

from scripts.validate_stage9 import validate_manifests, validate_templates

ROOT = Path(__file__).resolve().parents[1]


class Stage9TemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def compile_template(path):
            return json.loads(subprocess.run(["az", "bicep", "build", "--file", path, "--stdout"], cwd=ROOT, capture_output=True, text=True, check=True).stdout)
        cls.edge = compile_template("infra/edge/main.bicep")
        cls.origin = compile_template("infra/edge-origin/main.bicep")
        cls.documents = list(yaml.safe_load_all(subprocess.run(["kubectl", "kustomize", "deploy/validation/stage9"], cwd=ROOT, capture_output=True, text=True, check=True).stdout))

    def test_defaults_and_composed_manifests(self):
        validate_templates(self.edge, self.origin)
        validate_manifests(self.documents)

    def test_public_default_domain_cache_and_wildcard_routes_are_rejected(self):
        for property_name, value in (("linkToDefaultDomain", "Enabled"), ("patternsToMatch", ["/*"]), ("cacheConfiguration", {}), ("forwardingProtocol", "HttpOnly")):
            with self.subTest(property_name=property_name):
                edge = copy.deepcopy(self.edge)
                resources = edge["resources"]
                route = next(item for item in (resources.values() if isinstance(resources, dict) else resources) if item["type"].endswith("/routes"))
                route["properties"][property_name] = value
                with self.assertRaises(AssertionError):
                    validate_templates(edge, self.origin)

    def test_model_list_route_is_exact_get_only_and_rate_limited(self):
        resources = self.edge["resources"]
        resources = list(resources.values() if isinstance(resources, dict) else resources)
        route = next(item for item in resources if item["type"].endswith("/routes") and "admin" not in item["name"])
        self.assertTrue({"/models", "/v1/models"} <= set(route["properties"]["patternsToMatch"]))
        self.assertFalse(any(pattern.startswith("/v1/models/") or "*" in pattern for pattern in route["properties"]["patternsToMatch"]))
        waf = next(item for item in resources if item["type"] == "Microsoft.Network/FrontDoorWebApplicationFirewallPolicies" and "wafllmadmin" not in item["name"])
        rules = {rule["name"]: rule for rule in waf["properties"]["customRules"]["rules"]}
        self.assertEqual([rules[name]["priority"] for name in ("BlockModelListNonGet", "RateLimitModelList", "RateLimitApi")], [14, 15, 20])
        rate = rules["RateLimitModelList"]
        self.assertEqual((rate["ruleType"], rate["action"], rate["rateLimitDurationInMinutes"], rate["rateLimitThreshold"], rate["groupBy"]),
                         ("RateLimitRule", "Block", 1, "[parameters('modelListRateLimitPerMinute')]", [{"variableName": "SocketAddr"}]))
        self.assertEqual(self.edge["parameters"]["modelListRateLimitPerMinute"]["defaultValue"], 120)
        for change in ("detail-pattern", "threshold", "rate-scope", "non-get"):
            with self.subTest(change=change):
                edge = copy.deepcopy(self.edge)
                items = list(edge["resources"].values() if isinstance(edge["resources"], dict) else edge["resources"])
                edge_route = next(item for item in items if item["type"].endswith("/routes") and "admin" not in item["name"])
                edge_rules = {rule["name"]: rule for rule in next(item for item in items if item["type"] == waf["type"] and item["name"] == waf["name"])["properties"]["customRules"]["rules"]}
                if change == "detail-pattern":
                    edge_route["properties"]["patternsToMatch"].append("/v1/models/*")
                elif change == "threshold":
                    edge_rules["RateLimitModelList"]["rateLimitThreshold"] = 600
                elif change == "rate-scope":
                    edge_rules["RateLimitModelList"]["matchConditions"][0]["matchValue"] = ["^/"]
                else:
                    edge_rules["BlockModelListNonGet"]["enabledState"] = "Disabled"
                with self.assertRaises(AssertionError):
                    validate_templates(edge, self.origin)

    def test_ingress_wildcard_is_rejected(self):
        documents = copy.deepcopy(self.documents)
        ingress = next(item for item in documents if item["kind"] == "Ingress" and item["metadata"]["name"] == "llm-api")
        ingress["spec"]["rules"][0]["http"]["paths"][0]["path"] = "/"
        with self.assertRaises(AssertionError):
            validate_manifests(documents)

    def test_new_resources_cannot_be_enabled_by_default(self):
        edge = copy.deepcopy(self.edge)
        edge["parameters"]["deployEdge"]["defaultValue"] = True
        with self.assertRaises(AssertionError):
            validate_templates(edge, self.origin)

    def test_managed_waf_rule_sets_require_explicit_supported_actions(self):
        edge = copy.deepcopy(self.edge)
        resources = edge["resources"]
        resources = resources.values() if isinstance(resources, dict) else resources
        waf = next(item for item in resources if item["type"] == "Microsoft.Network/FrontDoorWebApplicationFirewallPolicies")
        del waf["properties"]["managedRules"]["managedRuleSets"][0]["ruleSetAction"]
        with self.assertRaises(AssertionError):
            validate_templates(edge, self.origin)

    def test_admin_domain_requires_source_ip_allowlist(self):
        resources = self.edge["resources"]
        resources = resources.values() if isinstance(resources, dict) else resources
        endpoints = [item for item in resources if item["type"] == "Microsoft.Cdn/profiles/afdEndpoints"]
        admin_endpoint = next(item for item in endpoints if "llm-admin" in item["name"])
        self.assertEqual(admin_endpoint["apiVersion"], "2025-04-15")
        self.assertNotIn("enforceMtls", admin_endpoint["properties"])
        domains = [item for item in resources if item["type"] == "Microsoft.Cdn/profiles/customDomains"]
        admin = next(item for item in domains if "llm-admin" in item["name"])
        self.assertEqual(admin["apiVersion"], "2025-04-15")
        self.assertNotIn("mtlsSettings", admin["properties"])
        wafs = [item for item in resources if item["type"] == "Microsoft.Network/FrontDoorWebApplicationFirewallPolicies"]
        admin_waf = next(item for item in wafs if "wafllmadmin" in item["name"])
        self.assertEqual(admin_waf["properties"]["policySettings"]["mode"], "Prevention")
        allowlist = next(item for item in admin_waf["properties"]["customRules"]["rules"] if item["name"] == "BlockUnapprovedAdminSources")
        self.assertEqual(allowlist["action"], "Block")
        self.assertEqual(allowlist["matchConditions"], [{
            "matchVariable": "SocketAddr", "operator": "IPMatch", "negateCondition": True,
            "matchValue": "[parameters('adminAllowedCidrs')]",
        }])