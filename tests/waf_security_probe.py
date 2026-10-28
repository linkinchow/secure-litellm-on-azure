"""Bounded WAF verification with read-only Azure configuration and correlated logs.

No policy writes, arbitrary payloads, credential discovery or automatic model calls.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, build_opener

from scripts.api_protocol import MODEL_LIST_RATE_LIMIT, MODEL_LIST_RATE_RULE, api_method_rules, model_list_rate_rule
from tests.edge_security_probe import NoRedirect, base_url, request_case, write_report


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resource_contract(document, kind):
    """Remove only known ARM bookkeeping, never recursively strip rule fields."""
    require(isinstance(document, dict), "Unexpected resource snapshot")
    result = {key: value for key, value in document.items() if key not in {
        "etag", "systemData", "provisioningState", "deploymentStatus"}}
    properties = document.get("properties")
    if isinstance(properties, dict):
        omitted = {"provisioningState", "deploymentStatus", "resourceState"}
        if kind == "domain":
            # DNS validation tokens/expiry are not request filtering or TLS configuration.
            omitted.update({"validationProperties", "domainValidationState"})
        result["properties"] = {key: value for key, value in properties.items() if key not in omitted}
    return result


def collection_contract(documents, kind):
    require(isinstance(documents, list) and all(isinstance(item, dict) for item in documents),
            "Unexpected resource collection snapshot")
    return sorted((resource_contract(item, kind) for item in documents), key=lambda value: digest(value))


def check_snapshot(previous, current):
    if previous.get("fingerprintVersion") != 2:
        # Legacy reports have no recoverable semantic baseline; don't upgrade them in place.
        require(isinstance(previous.get("fingerprint"), str)
                and re.fullmatch(r"[a-f0-9]{64}", previous["fingerprint"])
                and previous["fingerprint"] == current.get("legacyFingerprint"),
                "Legacy raw edge fingerprint differs; old report cannot distinguish ARM metadata from real configuration drift. "
                "Keep the report and its wafQuery, update the tool and run ONE new bounded suite to a new filename; do not bypass validation")
        return
    require(previous.get("fingerprint") == digest(previous.get("componentFingerprints")),
            "Report component fingerprints are inconsistent")
    before, after = previous.get("componentFingerprints"), current.get("componentFingerprints")
    require(isinstance(before, dict) and isinstance(after, dict), "Missing semantic edge components")
    changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
    require(not changed, "Edge security configuration changed in components: " + ", ".join(changed)
            + "; review the change before re-running tests")


class AzureRead:
    def __init__(self, subscription):
        self.subscription = subscription

    def run(self, arguments):
        scope = [] if arguments[:1] == ["extension"] else ["--subscription", self.subscription]
        try:
            result = subprocess.run(["az", *arguments, *scope,
                                     "--output", "json", "--only-show-errors"],
                                    capture_output=True, text=True, timeout=120, check=False,
                                    stdin=subprocess.DEVNULL,
                                    env={**os.environ, "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no"})
        except (OSError, subprocess.TimeoutExpired):
            raise VerificationError("Azure read failed or timed out; check az login and connectivity") from None
        require(result.returncode == 0,
                "Azure read failed; check Reader/LAW query permissions, CLI extensions and private connectivity; no raw error printed")
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise VerificationError("Azure returned invalid JSON; raw output withheld") from None

    def require_logs_extension(self):
        try:
            extension = self.run(["extension", "show", "--name", "log-analytics",
                                  "--query", "{name:name,version:version}"])
        except VerificationError:
            raise VerificationError(
                "Cannot verify Azure CLI log-analytics extension. In the same CLI environment run "
                "'az extension show --name log-analytics'; if missing, explicitly install with "
                "'az extension add --name log-analytics', then retry verify. No active test replay needed") from None
        require(extension.get("name") == "log-analytics" and bool(extension.get("version")),
                "Invalid Azure CLI log-analytics extension metadata")

    def resource(self, identifier, version):
        result = self.run(["resource", "show", "--ids", identifier, "--api-version", version])
        require(result.get("id", "").lower() == identifier.lower(), "Azure returned a different resource")
        return result


def config_scope(path):
    require(not path.is_symlink(), "CFG must not be a symlink")
    document = json.loads(path.read_text())
    require(document.get("application", {}).get("authentication", {}).get("mode") == "native",
            "This suite targets the managed native gateway, not the historical Entra proxy")
    azure = document["azure"]
    for name in ("subscriptionId", "tenantId"):
        require(re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", azure[name]) is not None,
                "CFG requires valid Azure IDs")
    require(document["environment"] in {"dev", "test", "prod"}, "Invalid environment")
    require(re.fullmatch(r"[A-Za-z0-9_.()-]{1,90}", document["target"]["resourceGroup"]) is not None,
            "Invalid target RG")
    for key in ("location", "baseDomain"):
        require(isinstance(document.get(key), str) and bool(document[key]), "Missing customer scope")
    base_url("https://llm-api." + document["baseDomain"])
    return document


def check(name, value):
    return {"name": name, "result": "pass" if value else "fail", "scope": "configuration-only"}


def condition(condition):
    result = {key: condition[key] for key in ("matchVariable", "selector", "operator", "matchValue")
              if key in condition and not (key == "selector" and condition[key] is None)}
    result["negateCondition"] = condition.get("negateCondition", False)
    result["transforms"] = condition.get("transforms", [])
    return result


def rule_matches(actual, desired):
    return (all(actual.get(key) == desired[key] for key in
                ("name", "priority", "enabledState", "ruleType", "action"))
            and (not actual.get("groupBy") or desired["ruleType"] == "RateLimitRule"
                 and actual.get("groupBy") == [{"variableName": "SocketAddr"}])
            and [condition(item) for item in actual.get("matchConditions", [])]
            == [condition(item) for item in desired["matchConditions"]])


def policy_checks(policy, plane, config):
    properties = policy.get("properties", {})
    settings = properties.get("policySettings", {})
    rules = properties.get("customRules", {}).get("rules", [])
    checks = [check(plane + "-waf-enabled", settings.get("enabledState") == "Enabled"),
              check(plane + "-waf-mode", settings.get("mode") in (
                  {"Prevention"} if plane == "admin" else {"Detection", "Prevention"})),
              check(plane + "-request-body-check", settings.get("requestBodyCheck") == "Enabled"),
              check(plane + "-log-scrubbing", settings.get("logScrubbing", {}).get("state") == "Enabled"),
              check(plane + "-no-custom-allow-bypass", not any(item.get("action") == "Allow" for item in rules))]
    managed = properties.get("managedRules", {}).get("managedRuleSets", [])
    checks.append(check(plane + "-managed-rule-sets", all(any(
        item.get("ruleSetType") == kind and item.get("ruleSetVersion") == version
        and item.get("ruleSetAction") == "Block" and not item.get("exclusions")
        and not item.get("ruleGroupOverrides") for item in managed)
        for kind, version in (("Microsoft_DefaultRuleSet", "2.1"), ("Microsoft_BotManagerRuleSet", "1.1")))))
    if plane == "api":
        for desired in api_method_rules():
            matches = [item for item in rules if item.get("name") == desired["name"]]
            checks.append(check(desired["name"], len(matches) == 1 and rule_matches(matches[0], desired)))
        desired = model_list_rate_rule(config["parameters"]["edge"].get("modelListRateLimitPerMinute", MODEL_LIST_RATE_LIMIT))
        matches = [item for item in rules if item.get("name") == MODEL_LIST_RATE_RULE]
        checks.append(check(MODEL_LIST_RATE_RULE, len(matches) == 1 and rule_matches(matches[0], desired)
                            and matches[0].get("groupBy") == desired["groupBy"]
                            and matches[0].get("rateLimitThreshold") == desired["rateLimitThreshold"]
                            and matches[0].get("rateLimitDurationInMinutes") == 1))
    else:
        matches = [item for item in rules if item.get("name") == "BlockUnapprovedAdminSources"]
        wanted = {"name": "BlockUnapprovedAdminSources", "priority": 5, "enabledState": "Enabled",
                  "ruleType": "MatchRule", "action": "Block", "matchConditions": [{
                      "matchVariable": "SocketAddr", "operator": "IPMatch", "negateCondition": True,
                      "matchValue": config["parameters"]["edge"]["adminAllowedCidrs"]}]}
        checks.append(check("BlockUnapprovedAdminSources", len(matches) == 1 and rule_matches(matches[0], wanted)))
        unsafe = [item for item in rules if item.get("name") == "BlockUnsafeMethods"]
        desired = {"name": "BlockUnsafeMethods", "priority": 10, "enabledState": "Enabled",
                   "ruleType": "MatchRule", "action": "Block", "matchConditions": [{
                       "matchVariable": "RequestMethod", "operator": "Equal", "matchValue": ["TRACE", "TRACK"]}]}
        checks.append(check("BlockUnsafeMethods", len(unsafe) == 1 and rule_matches(unsafe[0], desired)))
    rate_name = "RateLimitApi" if plane == "api" else "RateLimitAdmin"
    rate = [item for item in rules if item.get("name") == rate_name]
    threshold = config["parameters"]["edge"].get("rateLimitPerMinute" if plane == "api" else "adminRateLimitPerMinute",
                                                600 if plane == "api" else 120)
    desired = {"name": rate_name, "priority": 20, "enabledState": "Enabled",
               "ruleType": "RateLimitRule", "action": "Block", "matchConditions": [{
                   "matchVariable": "RequestUri", "operator": "BeginsWith", "matchValue": ["/"]}]}
    checks.append(check(rate_name, len(rate) == 1 and rule_matches(rate[0], desired)
                        and rate[0].get("rateLimitThreshold") == threshold
                        and rate[0].get("rateLimitDurationInMinutes") == 1))
    return checks


def snapshot(config, azure):
    """Resolve policies from live associations, never invent names from a suffix."""
    from scripts.app_gateway import require_front_door_edge
    require_front_door_edge(config, "The WAF security probe")
    subscription = config["azure"]["subscriptionId"]
    group = config["target"]["resourceGroup"]
    context = azure.run(["account", "show", "--query", "{id:id,tenantId:tenantId}"])
    require(context.get("id", "").lower() == subscription.lower()
            and context.get("tenantId", "").lower() == config["azure"]["tenantId"].lower(), "Azure tenant mismatch")
    root = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/"
    deployment = azure.run(["deployment", "group", "show", "--resource-group", group,
                            "--name", f"llmgw-{config['environment']}-s9-edge",
                            "--query", "{state:properties.provisioningState,edge:properties.outputs.edge.value}"])
    require(deployment.get("state") == "Succeeded", "Stage9 edge deployment is not Succeeded")
    edge = deployment["edge"]
    route = edge.get("routeId", "")
    require(isinstance(route, str) and route.lower().startswith((root + "Microsoft.Cdn/profiles/").lower())
            and "/afdEndpoints/" in route, "Unexpected API route scope")
    profile = route.split("/afdEndpoints/")[0]
    checks, policies, contracts = [], {}, []
    components = {"expectations": digest({
        "azure": config["azure"], "environment": config["environment"],
        "target": config["target"], "baseDomain": config["baseDomain"],
        "edge": config["parameters"]["edge"]})}
    for plane in ("api", "admin"):
        host = "llm-" + plane + "." + config["baseDomain"]
        require(edge.get(plane + "Host") == host, "Edge hosts differ from CFG")
        association = azure.resource(profile + "/securityPolicies/" + plane + "-waf", "2025-04-15")
        parameters = association.get("properties", {}).get("parameters", {})
        waf_id = parameters.get("wafPolicy", {}).get("id", "")
        require(isinstance(waf_id, str) and waf_id.lower().startswith(
            (root + "Microsoft.Network/frontDoorWebApplicationFirewallPolicies/").lower()), "WAF outside target RG")
        domain_id = profile + "/customDomains/llm-" + plane
        bound = parameters.get("associations", [])
        checks.append(check(plane + "-waf-domain-association", any(
            item.get("patternsToMatch") == ["/*"] and any(domain.get("id", "").lower() == domain_id.lower()
                for domain in item.get("domains", [])) for item in bound)))
        policy = azure.resource(waf_id, "2024-02-01")
        checks.extend(policy_checks(policy, plane, config))
        mode = policy.get("properties", {}).get("policySettings", {}).get("mode")
        policies[plane] = {"id": waf_id, "name": waf_id.rsplit("/", 1)[-1], "mode": mode}
        route_id = edge["routeId" if plane == "api" else "adminRouteId"]
        require(route_id.lower().startswith((profile + "/afdEndpoints/").lower()), "Route outside profile")
        live_route = azure.resource(route_id, "2025-04-15")
        props = live_route.get("properties", {})
        checks.append(check(plane + "-https-private-route", props.get("enabledState") == "Enabled"
            and props.get("supportedProtocols") == ["Https"] and props.get("forwardingProtocol") == "HttpsOnly"
            and props.get("linkToDefaultDomain") == "Disabled"))
        domain = azure.resource(domain_id, "2025-04-15")
        checks.append(check(plane + "-managed-edge-tls", domain.get("properties", {}).get("tlsSettings", {}).get(
            "certificateType") == "ManagedCertificate" and domain.get("properties", {}).get("tlsSettings", {}).get(
            "minimumTlsVersion") == "TLS12"))
        origins = azure.run(["rest", "--method", "get", "--url", "https://management.azure.com" + profile
                            + "/originGroups/private-" + plane + "/origins?api-version=2025-04-15"])
        origin_values = origins.get("value", [])
        require(not origins.get("nextLink"), "Origin collection is paginated; cannot verify a partial snapshot")
        expected_pls = edge.get("privateOrigin" if plane == "api" else "adminPrivateOrigin", {}).get(
            "privateLinkServiceId")
        checks.append(check(plane + "-private-origin-tls", bool(origin_values) and all(
            item.get("properties", {}).get("enforceCertificateNameCheck") is True
            and isinstance(expected_pls, str)
            and item.get("properties", {}).get("sharedPrivateLinkResource", {}).get("privateLink", {}).get(
                "id", "").lower() == expected_pls.lower() for item in origin_values)))
        statuses = [item.get("properties", {}).get("sharedPrivateLinkResource", {}).get("status") for item in origin_values]
        checks.append({"name": plane + "-private-link-approval", "scope": "configuration-only",
                       "result": "pass" if statuses and all(status == "Approved" for status in statuses)
                       else "unverified" if statuses and all(status in {None, "Approved"} for status in statuses)
                       else "fail"})
        contracts.extend([association, policy, live_route, domain, origins])
        for name, document in (("association", association), ("policy", policy),
                               ("route", live_route), ("domain", domain)):
            components[plane + "." + name] = digest(resource_contract(document, name))
        components[plane + ".origins"] = digest(collection_contract(origin_values, "origin"))
    require(policies["api"]["id"].lower() != policies["admin"]["id"].lower(), "API/Admin WAF must differ")
    diagnostics = azure.run(["monitor", "diagnostic-settings", "list", "--resource", profile])
    diagnostic_items = diagnostics if isinstance(diagnostics, list) else diagnostics.get("value", [])
    require(isinstance(diagnostic_items, list), "Unexpected diagnostic settings collection")
    workspace = azure.run(["monitor", "log-analytics", "workspace", "show", "--resource-group", group,
                           "--workspace-name", config["parameters"]["edge"]["logAnalyticsWorkspaceName"],
                           "--query", "{id:id,customerId:customerId}"])
    checks.append(check("waf-diagnostics-to-approved-law", any(
        item.get("workspaceId", "").lower() == workspace["id"].lower() and any(
            log.get("enabled") is True and (log.get("category") == "FrontDoorWebApplicationFirewallLog"
                or log.get("categoryGroup") == "allLogs") for log in item.get("logs", []))
        for item in diagnostic_items)))
    contracts.extend([diagnostics, workspace])
    components["diagnostics"] = digest(collection_contract(diagnostic_items, "diagnostics"))
    components["workspace"] = digest(workspace)
    return {"profileId": profile, "policies": policies, "workspaceId": workspace["customerId"],
            "checks": checks, "fingerprintVersion": 2,
            "componentFingerprints": components, "fingerprint": digest(components),
            "legacyFingerprint": digest(contracts)}


def backend_checks(config, azure):
    group = config["target"]["resourceGroup"]
    inventory = azure.run(["resource", "list", "--resource-group", group, "--query", "[].{id:id,type:type}"])
    checks = []
    versions = {"microsoft.containerservice/managedclusters": "2024-10-01",
                "microsoft.containerregistry/registries": "2023-07-01",
                "microsoft.keyvault/vaults": "2023-07-01",
                "microsoft.dbforpostgresql/flexibleservers": "2024-08-01",
                "microsoft.cache/redisenterprise": "2025-07-01"}
    counts = {key: 0 for key in versions}
    for item in inventory:
        kind = item["type"].lower()
        if kind not in versions:
            continue
        counts[kind] += 1
        resource = azure.resource(item["id"], versions[kind])
        properties = resource.get("properties", {})
        label = item["id"].rsplit("/", 1)[-1]
        if kind == "microsoft.containerservice/managedclusters":
            okay = (properties.get("apiServerAccessProfile", {}).get("enablePrivateCluster") is True
                and properties.get("disableLocalAccounts") is True
                and properties.get("aadProfile", {}).get("enableAzureRBAC") is True
                and properties.get("oidcIssuerProfile", {}).get("enabled") is True
                and properties.get("securityProfile", {}).get("workloadIdentity", {}).get("enabled") is True)
        elif kind == "microsoft.containerregistry/registries":
            okay = properties.get("publicNetworkAccess") == "Disabled" and properties.get("adminUserEnabled") is False
        elif kind == "microsoft.keyvault/vaults":
            okay = (properties.get("publicNetworkAccess") == "Disabled"
                and properties.get("enableRbacAuthorization") is True and properties.get("enablePurgeProtection") is True)
        elif kind == "microsoft.dbforpostgresql/flexibleservers":
            okay = (properties.get("network", {}).get("publicNetworkAccess") == "Disabled"
                and properties.get("authConfig", {}).get("activeDirectoryAuth") == "Enabled"
                and properties.get("authConfig", {}).get("passwordAuth") == "Disabled")
        else:
            db = azure.resource(item["id"] + "/databases/default", "2025-07-01").get("properties", {})
            # Some Redis ARM versions omit publicNetworkAccess. Do not turn null into a passing proof.
            checks.append({"name": label + "-redis-public-access",
                           "result": "pass" if properties.get("publicNetworkAccess") == "Disabled" else "unverified",
                           "scope": "configuration-only"})
            okay = db.get("accessKeysAuthentication") == "Disabled" and db.get("clientProtocol") == "Encrypted"
        checks.append(check(label + "-security-settings", okay))
    checks.extend(check(kind + "-present", count > 0) for kind, count in counts.items())
    return checks


def cases(admin_url, source):
    result = []
    for name, method, path, headers, rule in [
        ("unsupported-method", "PUT", "/v1/responses", {}, "BlockUnsupportedApiMethods"),
        ("non-responses-get", "GET", "/v1/chat/completions", {}, "BlockNonResponsesGet"),
        ("missing-upgrade", "GET", "/v1/responses", {}, "BlockInvalidWebSocketUpgrade"),
        ("bad-connection", "GET", "/v1/responses", {"Upgrade": "websocket", "Connection": "close"},
         "BlockInvalidWebSocketConnection"),
        ("model-list-post", "POST", "/v1/models", {}, "BlockModelListNonGet"),
    ]:
        result.append({"name": name, "method": method, "path": path, "headers": headers,
                       "expected": [403], "goal": "waf-log-correlation", "plane": "api", "rule": rule})
    for name, value, category in [
        ("managed-xss", "<script>alert('waf-probe')</script>", "XSS"),
        ("managed-sqli", "' OR '1'='1' --", "SQLI"),
    ]:
        result.append({"name": name, "method": "POST", "path": "/v1/responses?security_probe=" + quote(value, safe=""),
            "body": {"model": "security-probe-no-model", "input": "synthetic WAF validation"},
            "syntheticKey": True, "expected": [401, 403], "goal": "waf-log-correlation",
            "plane": "api", "ruleCategory": category})
    result.append({"name": "api-management-isolation", "method": "POST", "path": "/user/info",
        "body": {}, "syntheticKey": True, "expected": [403, 404, 405], "goal": "path-isolation",
        "plane": "api"})
    if admin_url:
        result.extend([
            {"name": "admin-source", "method": "GET", "path": "/fallback/login", "origin": admin_url,
             "expected": [200, 301, 302, 303, 307, 308] if source == "approved" else [403],
             "goal": "admin-source-observation", "plane": "admin",
             **({"rule": "BlockUnapprovedAdminSources"} if source == "unapproved" else {})},
            {"name": "admin-unsafe-method", "method": "TRACE", "path": "/fallback/login", "origin": admin_url,
             "expected": [403], "goal": "waf-log-correlation", "plane": "admin",
             "rule": "BlockUnsafeMethods" if source == "approved" else "BlockUnapprovedAdminSources"},
        ])
        if source == "unapproved":
            result.append({"name": "admin-spoofed-forwarded-source", "method": "GET",
                "path": "/fallback/login", "origin": admin_url,
                "headers": {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
                "expected": [403], "goal": "waf-log-correlation", "plane": "admin",
                "rule": "BlockUnapprovedAdminSources"})
    return result


def waf_query(report):
    observations = report["observations"]
    refs = [item["trackingReference"] for item in observations if item.get("trackingReference")]
    require(refs, "No bounded tracking references to correlate")
    require(all(isinstance(ref, str) and re.fullmatch(r"[A-Za-z0-9_+/=-]{1,200}", ref) for ref in refs),
            "Unsafe tracking reference")
    start = datetime.fromisoformat(report["startedAt"])
    end = datetime.fromisoformat(report["completedAt"])
    require(start.tzinfo and end.tzinfo and timedelta(0) <= end - start <= timedelta(minutes=10),
            "Invalid report time window")
    profile = report["snapshot"]["profileId"]
    require(re.fullmatch(r"/subscriptions/[a-fA-F0-9-]{36}/resourceGroups/[A-Za-z0-9_.()-]+/providers/Microsoft.Cdn/profiles/[A-Za-z0-9-]+", profile),
            "Invalid report profile")
    return f"""AzureDiagnostics
| where TimeGenerated between (datetime({(start - timedelta(minutes=2)).isoformat()}) .. datetime({(end + timedelta(minutes=2)).isoformat()}))
| where Category == "FrontDoorWebApplicationFirewallLog"
| where tolower(_ResourceId) == tolower('{profile}')
| where trackingReference_s in ({','.join(json.dumps(ref) for ref in refs)})
| project reference=tostring(trackingReference_s), rule=tostring(ruleName_s),
          action=tostring(action_s), mode=tostring(policyMode_s), policy=tostring(policy_s),
          host=tostring(host_s)
| take 2000"""


def correlate(report, rows):
    results = []
    for observation in report["observations"]:
        if not observation.get("rule") and not observation.get("ruleCategory"):
            results.append({"name": observation["name"],
                            "result": "http-observation-only" if observation.get("expectedStatusObserved") else "fail"})
            continue
        policy = report["snapshot"]["policies"][observation["plane"]]
        matching = [row for row in rows if row.get("reference") == observation.get("trackingReference")
            and observation.get("trackingReference")
            and str(row.get("policy", "")).lower() in {policy["name"].lower(), policy["id"].lower()}
            and row.get("host", "").lower() == urlsplit(
                report["apiOrigin"] if observation["plane"] == "api" else report["adminOrigin"]).hostname]
        def wanted(row):
            rule = str(row.get("rule", ""))
            if observation.get("rule"):
                return rule == observation["rule"] or rule.endswith("-" + observation["rule"])
            return ("microsoft_defaultruleset" in rule.lower()
                    and observation["ruleCategory"].lower() in rule.lower())
        hit = [row for row in matching if wanted(row)]
        blocked = any(str(row.get("action", "")).lower() == "block"
            and str(row.get("mode", "")).lower() == "prevention"
            for row in (matching if observation.get("ruleCategory") else hit))
        # For managed anomaly scoring the detection and terminal block can be different rules.
        result = ("prevention-block-verified" if hit and blocked and observation.get("status") == 403
                  and policy["mode"] == "Prevention" else
                  "detection-match-only" if hit and policy["mode"] == "Detection"
                  and any(str(row.get("mode", "")).lower() == "detection" for row in hit)
                  else "inconclusive")
        results.append({"name": observation["name"], "result": result,
                        "matchedRules": sorted({str(row["rule"]) for row in hit})})
    return results


def run(args):
    require(args.confirm_authorized_target, "Explicit authorized-target confirmation required")
    require(not args.output_json.exists() and not args.output_json.is_symlink(), "Report exists; use a new path")
    config = config_scope(args.config)
    require(args.api_url == "https://llm-api." + config["baseDomain"], "API origin differs from CFG")
    require(not args.admin_url or args.admin_url == "https://llm-admin." + config["baseDomain"], "Admin origin differs from CFG")
    require(not args.admin_url or args.admin_source in {"approved", "unapproved"}, "Declare actual Admin egress expectation")
    azure = AzureRead(config["azure"]["subscriptionId"])
    live = snapshot(config, azure)
    report = {"schemaVersion": 1, "operation": "waf-security-verification",
              "startedAt": datetime.now(timezone.utc).isoformat(), "apiOrigin": args.api_url,
              "adminOrigin": args.admin_url, "snapshot": live, "observations": [],
              "rateLimitTested": False, "ddosTested": False, "modelCallsRequested": False,
              "credentialLogged": False, "rawResponseLogged": False}
    report["backendChecks"] = backend_checks(config, azure)
    config_failed = any(item["result"] == "fail" for item in live["checks"])
    if config_failed and not args.configuration_only:
        report["stoppedEarly"] = True
        report["stopReason"] = "edge-configuration-failed-no-active-requests"
    if not args.configuration_only and not config_failed:
        opener = build_opener(NoRedirect(), *([ProxyHandler({})] if args.direct else []))
        selected = cases(args.admin_url, args.admin_source)
        report["plannedRequestCount"] = len(selected)
        for case in selected:
            if case["plane"] == "api" and case.get("rule") and live["policies"]["api"]["mode"] == "Detection":
                case = {**case, "expected": [400, 401, 403, 404, 405, 426]}
            if report["observations"]:
                time.sleep(2)
            observation = request_case(opener, args.api_url, case)
            observation.update({key: case[key] for key in ("plane", "rule", "ruleCategory") if key in case})
            report["observations"].append(observation)
            print(json.dumps({"case": observation["name"], "status": observation["status"]}), flush=True)
            if observation["status"] is None or observation["status"] == 429 or observation["status"] >= 500:
                report["stoppedEarly"] = True
                break
    report["completedAt"] = datetime.now(timezone.utc).isoformat()
    report["requestCount"] = len(report["observations"])
    report["pendingWafEvidence"] = bool(report["observations"])
    if report["observations"] and any(item.get("trackingReference") for item in report["observations"]):
        report["wafQuery"] = waf_query(report)
    write_report(args.output_json, report)
    print(json.dumps({"reportSaved": True, "pendingWafEvidence": report["pendingWafEvidence"],
                      "configurationFailures": sum(item["result"] == "fail" for item in live["checks"] + report["backendChecks"])}))
    return 1 if (any(item["result"] == "fail" for item in live["checks"] + report["backendChecks"])
                 or any(not item.get("expectedStatusObserved") for item in report["observations"])
                 or report.get("stoppedEarly")) else 0


def rate_limit(args):
    require(args.confirm_authorized_target and args.confirm_rate_limit_impact,
            "Rate test requires target approval and explicit acceptance of shared-egress throttling impact")
    require(10 <= args.request_budget <= 100, "Rate request budget must be 10..100")
    require(not args.output_json.exists() and not args.output_json.is_symlink(), "Report exists")
    config = config_scope(args.config)
    require(config["environment"] in {"dev", "test"}, "Bounded rate test is limited to dev/test, never production")
    require(args.api_url == "https://llm-api." + config["baseDomain"], "Origin differs from CFG")
    live = snapshot(config, AzureRead(config["azure"]["subscriptionId"]))
    require(all(item["result"] != "fail" for item in live["checks"]), "Edge configuration checks failed")
    threshold = config["parameters"]["edge"].get("rateLimitPerMinute", 600)
    require(type(threshold) is int and threshold + 5 <= args.request_budget,
            "Live reviewed rate threshold exceeds bounded budget; rate behavior NOT TESTED; do not raise budget or alter production policy")
    require(live["policies"]["api"]["mode"] == "Prevention", "Rate block verification requires already-approved Prevention mode")
    report = {"schemaVersion": 1, "operation": "waf-security-verification",
              "startedAt": datetime.now(timezone.utc).isoformat(), "apiOrigin": args.api_url,
              "adminOrigin": None, "snapshot": live, "observations": [], "rateLimitTested": True,
              "ddosTested": False, "modelCallsRequested": False, "credentialLogged": False,
              "rawResponseLogged": False, "requestBudget": args.request_budget, "maximumRequestsPerSecond": 2}
    opener = build_opener(NoRedirect(), *([ProxyHandler({})] if args.direct else []))
    started = time.monotonic()
    try:
        for index in range(args.request_budget):
            if index:
                time.sleep(0.5)
            if time.monotonic() - started >= 55:
                report["stopReason"] = "55-second-time-budget"
                break
            case = {"name": f"rate-{index + 1}", "method": "POST", "path": "/v1/responses",
                    "body": {"model": "security-probe-no-model", "input": "bounded rate test"},
                    "syntheticKey": True, "expected": [401, 403, 429], "goal": "waf-log-correlation"}
            observation = request_case(opener, args.api_url, case)
            observation.update(plane="api", rule="RateLimitApi")
            report["observations"].append(observation)
            if observation["status"] is None or observation["status"] >= 500 or observation["status"] not in {401, 403, 429}:
                report["stopReason"] = "transport-server-or-unexpected-response"
                break
            # A denial is only a stop signal, never proof of the responsible layer.
            if observation["status"] in {403, 429}:
                report["stopReason"] = "denial-observed-check-correlated-rule"
                break
    finally:
        report["completedAt"] = datetime.now(timezone.utc).isoformat()
        report["pendingWafEvidence"] = True
        if any(item.get("trackingReference") for item in report["observations"]):
            report["wafQuery"] = waf_query(report)
        write_report(args.output_json, report)
    print(json.dumps({"requestCount": len(report["observations"]), "pendingWafEvidence": True,
                      "rateLimitVerified": False}))
    return 0


def verify(args):
    require(not args.output_json.exists() and not args.output_json.is_symlink(), "Evidence report exists; choose a new path")
    report = json.loads(args.report.read_text())
    require(report.get("operation") == "waf-security-verification", "Invalid report")
    config = config_scope(args.config)
    require(report["apiOrigin"] == "https://llm-api." + config["baseDomain"], "Report belongs to another CFG")
    require(not report.get("adminOrigin") or report["adminOrigin"] == "https://llm-admin." + config["baseDomain"],
            "Report Admin origin differs from CFG")
    azure = AzureRead(config["azure"]["subscriptionId"])
    azure.require_logs_extension()
    live = snapshot(config, azure)
    check_snapshot(report["snapshot"], live)
    require(live["workspaceId"] == report["snapshot"]["workspaceId"], "LAW differs from report")
    query = waf_query(report)
    rows = azure.run(["monitor", "log-analytics", "query", "--workspace", live["workspaceId"],
                      "--analytics-query", query, "--timespan", "P1D"])
    require(isinstance(rows, list) and len(rows) < 2000, "Unexpected or truncated LAW results")
    results = correlate(report, rows)
    if report.get("rateLimitTested"):
        results = [{"name": "RateLimitApi", "result": "prevention-block-verified" if any(
            item["result"] == "prevention-block-verified" for item in results) else "inconclusive"}]
    evidence = {"operation": "waf-security-evidence", "sourceReportSha256": digest(report),
                "verifiedAt": datetime.now(timezone.utc).isoformat(), "results": results,
                "allWafPreventionVerified": not report.get("stoppedEarly", False)
                    and any(item["result"] == "prevention-block-verified" for item in results)
                    and all(item["result"] in {"prevention-block-verified", "http-observation-only"} for item in results),
                "scope": "observed-cases-only-not-full-security-acceptance"}
    write_report(args.output_json, evidence)
    print(json.dumps(evidence))
    return 0 if not report.get("stoppedEarly") and all(item["result"] in {"prevention-block-verified", "http-observation-only"}
                    for item in results) else 1


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    subs = result.add_subparsers(dest="operation", required=True)
    active = subs.add_parser("run")
    active.add_argument("--config", type=Path, required=True)
    active.add_argument("--api-url", type=base_url, required=True)
    active.add_argument("--admin-url", type=base_url)
    active.add_argument("--admin-source", choices=["approved", "unapproved"])
    active.add_argument("--confirm-authorized-target", action="store_true")
    active.add_argument("--direct", action="store_true")
    active.add_argument("--configuration-only", action="store_true")
    active.add_argument("--output-json", type=Path, required=True)
    rate = subs.add_parser("rate-limit")
    rate.add_argument("--config", type=Path, required=True)
    rate.add_argument("--api-url", type=base_url, required=True)
    rate.add_argument("--confirm-authorized-target", action="store_true")
    rate.add_argument("--confirm-rate-limit-impact", action="store_true")
    rate.add_argument("--request-budget", type=int, required=True)
    rate.add_argument("--direct", action="store_true")
    rate.add_argument("--output-json", type=Path, required=True)
    evidence = subs.add_parser("verify")
    evidence.add_argument("--config", type=Path, required=True)
    evidence.add_argument("--report", type=Path, required=True)
    evidence.add_argument("--output-json", type=Path, required=True)
    return result


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        return {"run": run, "rate-limit": rate_limit, "verify": verify}[args.operation](args)
    except VerificationError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError):
        print("Security verification failed; check approved CFG, report, Azure read permissions and private connectivity. "
              "No raw Azure error, credentials or content printed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
