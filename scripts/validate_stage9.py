#!/usr/bin/env python3
"""Check compiled Stage 9 ARM and rendered manifests, without deployment."""

import json
import sys
from pathlib import Path

import yaml
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.api_protocol import api_method_rules, app_gateway_api_method_rules

API_PATHS = {"/chat/completions", "/v1/chat/completions", "/responses", "/v1/responses", "/embeddings", "/v1/embeddings"}


def resources(template):
    value = template["resources"]
    return [item for item in (value.values() if isinstance(value, dict) else value) if not item.get("existing")]


def validate_templates(edge, origin):
    for parameter in ("deployEdge", "enableApiTraffic", "enableAdminTraffic"):
        assert edge["parameters"][parameter]["defaultValue"] is False
    assert edge["parameters"]["wafMode"]["defaultValue"] == "Detection"
    assert origin["parameters"]["deployPrivateOrigin"]["defaultValue"] is False
    managed = [item for item in resources(edge) if item.get("condition") == "[parameters('deployEdge')]" ]
    by_type = {}
    for item in managed:
        by_type.setdefault(item["type"], []).append(item)
    assert {kind: len(items) for kind, items in by_type.items()} == {
        "Microsoft.Cdn/profiles": 1,
        "Microsoft.Cdn/profiles/afdEndpoints": 2,
        "Microsoft.Cdn/profiles/customDomains": 2,
        "Microsoft.Cdn/profiles/originGroups": 2,
        "Microsoft.Cdn/profiles/originGroups/origins": 2,
        "Microsoft.Network/FrontDoorWebApplicationFirewallPolicies": 2,
        "Microsoft.Cdn/profiles/securityPolicies": 2,
        "Microsoft.Cdn/profiles/afdEndpoints/routes": 2,
        "Microsoft.Insights/diagnosticSettings": 1,
    }
    profile = by_type["Microsoft.Cdn/profiles"][0]
    assert profile["sku"]["name"] == "Premium_AzureFrontDoor"
    assert "identity" not in profile
    endpoints = {"admin" if "llm-admin" in item["name"] else "api": item for item in by_type["Microsoft.Cdn/profiles/afdEndpoints"]}
    assert set(endpoints) == {"api", "admin"}
    assert endpoints["api"]["properties"]["enabledState"] == "[variables('apiTrafficState')]"
    assert endpoints["admin"]["properties"]["enabledState"] == "[variables('adminTrafficState')]"
    assert endpoints["api"]["apiVersion"] == endpoints["admin"]["apiVersion"] == "2025-04-15"
    assert all("enforceMtls" not in endpoint["properties"] for endpoint in endpoints.values())
    domains = {"admin" if "llm-admin" in item["name"] else "api": item for item in by_type["Microsoft.Cdn/profiles/customDomains"]}
    assert set(domains) == {"api", "admin"}
    assert domains["api"]["properties"]["hostName"] == "[variables('apiHost')]"
    assert domains["admin"]["properties"]["hostName"] == "[variables('adminHost')]"
    assert all(domain["apiVersion"] == "2025-04-15" and "mtlsSettings" not in domain["properties"] for domain in domains.values())
    assert edge["variables"]["apiHost"] == "[format('llm-api.{0}', parameters('baseDomain'))]"
    assert edge["variables"]["adminHost"] == "[format('llm-admin.{0}', parameters('baseDomain'))]"
    origin_groups = {"admin" if "private-admin" in item["name"] else "api": item for item in by_type["Microsoft.Cdn/profiles/originGroups"]}
    origins = {"admin" if "private-admin" in item["name"] else "api": item for item in by_type["Microsoft.Cdn/profiles/originGroups/origins"]}
    assert set(origin_groups) == set(origins) == {"api", "admin"}
    for plane in ("api", "admin"):
        properties = origins[plane]["properties"]
        assert properties["enforceCertificateNameCheck"] is True
        assert properties["originHostHeader"] == properties["hostName"] == f"[variables('{plane}Host')]"
        origin_parameter = "privateOrigin" if plane == "api" else "adminPrivateOrigin"
        assert properties["sharedPrivateLinkResource"]["privateLink"]["id"] == f"[parameters('{origin_parameter}').privateLinkServiceId]"
        assert "status" not in properties["sharedPrivateLinkResource"]
        assert origin_groups[plane]["properties"]["healthProbeSettings"]["probePath"] == "/readyz"
    wafs = {"admin" if "wafllmadmin" in item["name"] else "api": item for item in by_type["Microsoft.Network/FrontDoorWebApplicationFirewallPolicies"]}
    assert set(wafs) == {"api", "admin"}
    for waf_resource in wafs.values():
        waf = waf_resource["properties"]
        assert waf["policySettings"]["requestBodyCheck"] == "Enabled"
        assert waf["policySettings"]["logScrubbing"]["state"] == "Enabled"
        assert {(item["ruleSetType"], item["ruleSetVersion"], item.get("ruleSetAction")) for item in waf["managedRules"]["managedRuleSets"]} == {
            ("Microsoft_DefaultRuleSet", "2.1", "Block"),
            ("Microsoft_BotManagerRuleSet", "1.1", "Block"),
        }
        assert not waf["managedRules"].get("exclusions")
        assert all(rule["action"] != "Allow" for rule in waf["customRules"]["rules"])
    api_rules = wafs["api"]["properties"]["customRules"]["rules"]
    assert [rule for rule in api_rules if rule["name"] != "RateLimitApi"] == api_method_rules()
    admin_waf = wafs["admin"]["properties"]
    assert admin_waf["policySettings"]["mode"] == "Prevention"
    assert {rule["name"] for rule in admin_waf["customRules"]["rules"]} == {"BlockUnapprovedAdminSources", "BlockUnsafeMethods", "RateLimitAdmin"}
    allowlist = next(rule for rule in admin_waf["customRules"]["rules"] if rule["name"] == "BlockUnapprovedAdminSources")
    assert allowlist["action"] == "Block" and allowlist["priority"] == 5
    assert allowlist["matchConditions"] == [{"matchVariable": "SocketAddr", "operator": "IPMatch", "negateCondition": True, "matchValue": "[parameters('adminAllowedCidrs')]"}]
    policies = {"admin" if "admin-waf" in item["name"] else "api": item for item in by_type["Microsoft.Cdn/profiles/securityPolicies"]}
    routes = {"admin" if "admin-ip-allowlist-only" in item["name"] else "api": item for item in by_type["Microsoft.Cdn/profiles/afdEndpoints/routes"]}
    assert set(policies) == set(routes) == {"api", "admin"}
    for plane in ("api", "admin"):
        association = policies[plane]["properties"]["parameters"]["associations"]
        assert len(association) == 1 and len(association[0]["domains"]) == 1 and association[0]["patternsToMatch"] == ["/*"]
        route = routes[plane]["properties"]
        assert route["supportedProtocols"] == ["Https"] and route["forwardingProtocol"] == "HttpsOnly"
        assert route["linkToDefaultDomain"] == "Disabled" and "cacheConfiguration" not in route and route["ruleSets"] == []
        assert len(route["customDomains"]) == 1
    assert set(routes["api"]["properties"]["patternsToMatch"]) == API_PATHS
    assert routes["api"]["properties"]["enabledState"] == "[variables('apiTrafficState')]"
    assert routes["admin"]["properties"]["patternsToMatch"] == ["/*"]
    assert routes["admin"]["properties"]["enabledState"] == "[variables('adminTrafficState')]"
    output = edge["outputs"]["edge"]["value"]
    assert output["privateOrigin"] == "[parameters('privateOrigin')]"
    assert output["adminPrivateOrigin"] == "[parameters('adminPrivateOrigin')]"
    assert output["adminAllowedCidrs"] == "[parameters('adminAllowedCidrs')]"
    assert output["adminRateLimitPerMinute"] == "[parameters('adminRateLimitPerMinute')]"
    assert output["adminAccessMode"] == "SourceIpAllowlistAndNativeLogin"
    assert "adminMtls" not in output
    assert output["endpointHost"] != output["adminEndpointHost"] and output["routeId"] != output["adminRouteId"]
    origin_resources = resources(origin)
    assert len(origin_resources) == 2 and all(item["type"] == "Microsoft.Network/privateLinkServices" for item in origin_resources)
    private_links = {"admin" if "adminPrivateLinkServiceName" in item["name"] else "api": item for item in origin_resources}
    assert set(private_links) == {"api", "admin"}
    for plane, pls in private_links.items():
        assert pls["condition"] == "[parameters('deployPrivateOrigin')]"
        assert pls["properties"]["autoApproval"]["subscriptions"] == []
        assert pls["properties"]["enableProxyProtocol"] is False
        frontend = pls["properties"]["loadBalancerFrontendIpConfigurations"][0]["id"]
        assert f"parameters('{plane}LoadBalancer')" in frontend


def validate_app_gateway_template(template):
    for parameter in ("deployEdge", "deployGateway", "enableApiTraffic", "enableAdminTraffic", "networkIsolation", "wafExcludePromptFields"):
        assert template["parameters"][parameter]["defaultValue"] is False
    assert template["parameters"]["wafMode"]["defaultValue"] == "Detection"
    assert template["variables"]["gateway"] == "[and(parameters('deployEdge'), parameters('deployGateway'))]"
    managed = [item for item in resources(template) if item.get("condition") in {"[parameters('deployEdge')]", "[variables('gateway')]"}]
    counts = {}
    for item in managed:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    assert counts == {
        "Microsoft.Network/networkSecurityGroups": 1,
        "Microsoft.Network/virtualNetworks/subnets": 1,
        "Microsoft.Network/publicIPAddresses": 1,
        "Microsoft.ManagedIdentity/userAssignedIdentities": 1,
        "Microsoft.Authorization/roleAssignments": 2,
        "Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies": 3,
        "Microsoft.Network/applicationGateways": 1,
        "Microsoft.Insights/diagnosticSettings": 2,
        "Microsoft.KeyVault/vaults": 1,
        "Microsoft.Network/privateEndpoints": 1,
        "Microsoft.Network/privateEndpoints/privateDnsZoneGroups": 1,
    }, counts
    assert sum(item["condition"] == "[variables('gateway')]" for item in managed) == 2
    vault = next(item for item in managed if item["type"] == "Microsoft.KeyVault/vaults")["properties"]
    assert vault["publicNetworkAccess"] == "Disabled" and vault["networkAcls"] == {"bypass": "AzureServices", "defaultAction": "Deny"} and vault["enableRbacAuthorization"] is True
    by_name = {item["name"]: item for item in managed}
    policies = {item["name"]: item["properties"] for item in managed if item["type"].endswith("FirewallPolicies")}
    api = policies["[variables('apiWafName')]"]
    assert api["customRules"][:4] == app_gateway_api_method_rules()
    assert api["customRules"][4]["ruleType"] == "RateLimitRule" and api["customRules"][4]["groupByUserSession"] == [{"groupByVariables": [{"variableName": "ClientAddr"}]}]
    admin = policies["[variables('adminWafName')]"]
    assert "'Prevention'" in admin["policySettings"]
    assert [rule["name"] for rule in admin["customRules"]] == ["BlockUnapprovedAdminSources", "BlockUnsafeMethods", "RateLimitAdmin"]
    assert admin["customRules"][0]["matchConditions"] == [{"matchVariables": [{"variableName": "RemoteAddr"}], "operator": "IPMatch", "negationConditon": True, "matchValues": "[parameters('adminAllowedCidrs')]"}]
    gate = policies["[variables('gateWafName')]"]
    assert [rule["name"] for rule in gate["customRules"]] == ["TrafficDisabled"] and gate["customRules"][0]["matchConditions"][0]["matchValues"] == ["0.0.0.0/0"]
    gateway = by_name["[variables('gatewayName')]"]["properties"]
    assert gateway["enableHttp2"] is False and gateway["globalConfiguration"] == {"enableRequestBuffering": True, "enableResponseBuffering": False}
    assert gateway["sslPolicy"] == {"policyType": "Predefined", "policyName": "AppGwSslPolicy20220101S"}
    assert gateway["firewallPolicy"] == {"id": "[variables('gateWafId')]"}
    assert set(template["variables"]["apiPaths"]) == API_PATHS and template["variables"]["edgeHeader"] == "X-LLMGW-Edge-Id"
    assert gateway["rewriteRuleSets"][0]["properties"]["rewriteRules"][0]["actionSet"]["requestHeaderConfigurations"] == [{"headerName": "[variables('edgeHeader')]", "headerValue": "[variables('edgeId')]"}]
    path_map = gateway["urlPathMaps"][0]["properties"]
    assert path_map["defaultBackendAddressPool"]["id"] == "[format('{0}/backendAddressPools/deny', variables('gatewayId'))]" and path_map["pathRules"][0]["properties"]["paths"] == "[variables('apiPaths')]"
    subnet = by_name["[format('{0}/{1}', parameters('virtualNetworkName'), parameters('subnetName'))]"]["properties"]
    assert "routeTable" not in subnet and "networkSecurityGroup" in subnet
    public_ip = by_name["[format('pip-agw-llm-{0}', parameters('environmentName'))]"]["properties"]
    assert public_ip["idleTimeoutInMinutes"] == 30 and public_ip["dnsSettings"]["domainNameLabelScope"] == "ResourceGroupReuse"


def validate_manifests(documents):
    objects = {(item["kind"], item["metadata"]["name"]): item for item in documents}
    ingresses = [item for item in documents if item["kind"] == "Ingress"]
    assert len(ingresses) == 2
    api = objects[("Ingress", "llm-api")]["spec"]
    admin = objects[("Ingress", "llm-admin")]["spec"]
    assert api["ingressClassName"] == "REPLACE_PRIVATE_API_INGRESS_CLASS"
    assert admin["ingressClassName"] == "REPLACE_PRIVATE_ADMIN_INGRESS_CLASS"
    assert api["rules"][0]["host"] == "llm-api.example.com"
    assert admin["rules"][0]["host"] == "llm-admin.example.com"
    paths = api["rules"][0]["http"]["paths"]
    assert {item["path"] for item in paths} == API_PATHS | {"/readyz"}
    assert all(item["pathType"] == "Exact" and item["backend"]["service"]["name"] == "llm-api-proxy" for item in paths)
    for plane in ("api", "admin"):
        ingress = objects[("Ingress", f"llm-{plane}")]["spec"]
        assert ingress["tls"][0]["hosts"] == [f"llm-{plane}.example.com"]
        env = objects[("Deployment", f"llm-{plane}-proxy")]["spec"]["template"]["spec"]["containers"][0]["env"]
        edge_values = [item for item in env if item["name"] == "FRONT_DOOR_ID"]
        assert edge_values == [{"name": "FRONT_DOOR_ID", "value": "REPLACE_EXPECTED_FRONT_DOOR_ID"}]
        policy = objects[("NetworkPolicy", f"llm-{plane}-proxy-ingress")]["spec"]
        assert policy["ingress"][0]["from"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": f"llm-{plane}-ingress"}}, "podSelector": {"matchLabels": {"app.kubernetes.io/component": "controller"}}}]
    assert objects[("NetworkPolicy", "allow-litellm-required-traffic")]["spec"]["ingress"] == [{"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "entra-auth-proxy"}}}], "ports": [{"protocol": "TCP", "port": 4000}]}]
    assert all(item["spec"]["type"] == "ClusterIP" for item in documents if item["kind"] == "Service")
    pods = [item["spec"]["template"]["spec"] for item in documents if item["kind"] == "Deployment"] + [item["spec"]["jobTemplate"]["spec"]["template"]["spec"] for item in documents if item["kind"] == "CronJob"]
    assert pods and all(pod.get("nodeSelector") == {"workload": "litellm"} for pod in pods)
    settings = next(item["data"]["config.json"] for item in documents if item["kind"] == "ConfigMap" and "config.json" in item.get("data", {}))
    assert json.loads(settings)["l3"]["enabled"] is False


if __name__ == "__main__":
    validate_templates(json.loads(Path(sys.argv[1]).read_text()), json.loads(Path(sys.argv[2]).read_text()))
    validate_manifests(list(yaml.safe_load_all(Path(sys.argv[3]).read_text())))
    if len(sys.argv) > 4:
        validate_app_gateway_template(json.loads(Path(sys.argv[4]).read_text()))
    print("Stage 9 compiled edge/PLS templates and isolated API/admin manifests passed.")