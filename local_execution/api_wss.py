"""Plan-bound API WAF method update; preserves traffic, Admin and policy mode."""

import argparse
import copy
import json
from pathlib import Path

from local_execution.runner import authenticate_azure, load_config, operation_root, reviewed_revision
from local_execution.release_report import generate_report
from scripts.api_protocol import api_method_rules
from scripts.backend_manifest import application_authentication
from scripts.customer_migration import ROOT, MigrationError, fingerprint, private_write, require
from scripts.edge_binding import deployed_edge, deployed_resource
from scripts.migration_deploy import AzureCommands, build_plan, group_id, what_if_leaf_deltas
from scripts.workflow_diagnostics import diagnostic_exit


def normalized_rule(rule):
    value = copy.deepcopy(rule)
    if value.get("ruleType") == "MatchRule":
        for field in ("rateLimitDurationInMinutes", "rateLimitThreshold"):
            if field in value and value[field] is None:
                value.pop(field)
        if value.get("groupBy") == []:
            value.pop("groupBy")
    for condition in value.get("matchConditions", []):
        if condition.get("negateCondition") is False:
            condition.pop("negateCondition")
        if condition.get("transforms") == []:
            condition.pop("transforms")
        if "selector" in condition and condition["selector"] in (None, ""):
            condition.pop("selector")
    return value


def desired_waf(live, identifier):
    require(live.get("id", "").lower() == identifier.lower(), "API WAF scope mismatch")
    properties = live.get("properties", {})
    allowed = {"policySettings", "customRules", "managedRules", "frontendEndpointLinks",
               "securityPolicyLinks", "routingRuleLinks", "resourceState", "provisioningState"}
    require(set(properties).issubset(allowed), "Unknown API WAF properties; review before updating")
    require(properties.get("provisioningState") == "Succeeded", "API WAF is not in a stable state")
    policy = properties.get("policySettings", {})
    require(policy.get("enabledState") == "Enabled" and policy.get("mode") in {"Detection", "Prevention"},
            "API WAF must be enabled with a reviewed mode")
    rules = properties.get("customRules", {}).get("rules", [])
    require(isinstance(rules, list) and rules, "API WAF custom rules are missing")
    rate = [rule for rule in rules if rule.get("name") == "RateLimitApi"]
    require(len(rate) == 1 and rate[0].get("priority") == 20
            and rate[0].get("action") == "Block" and rate[0].get("enabledState") == "Enabled"
            and rate[0].get("ruleType") == "RateLimitRule"
            and rate[0].get("rateLimitDurationInMinutes") == 1,
            "API rate-limit rule is missing, disabled or reordered")
    methods = [normalized_rule(rule) for rule in rules if rule.get("name") != "RateLimitApi"]
    legacy = [{"name": "BlockNonPost", "priority": 10, "enabledState": "Enabled",
               "ruleType": "MatchRule", "action": "Block", "matchConditions": [
                   {"matchVariable": "RequestMethod", "operator": "Equal",
                    "negateCondition": True, "matchValue": ["POST"]},
               ]}]
    require(methods in (legacy, api_method_rules()), "API method rules differ from the reviewed baseline")
    desired = {key: copy.deepcopy(properties[key]) for key in ("policySettings", "customRules", "managedRules")}
    desired["customRules"]["rules"] = [*api_method_rules(), copy.deepcopy(rate[0])]
    resource = {"type": "Microsoft.Network/frontDoorWebApplicationFirewallPolicies",
                "apiVersion": "2024-02-01", "name": identifier.rsplit("/", 1)[1],
                "location": live["location"], "sku": copy.deepcopy(live["sku"]), "properties": desired}
    if "tags" in live:
        resource["tags"] = copy.deepcopy(live["tags"])
    return resource


def assert_protocol_changes(changes, identifier):
    for change in changes:
        if change.get("changeType") in {"Ignore", "NoChange"}:
            continue
        require(change.get("changeType") == "Modify"
                and change.get("resourceId", "").lower() == identifier.lower(),
                "WSS update may modify only the existing API WAF")
        leaves = list(what_if_leaf_deltas(change.get("delta")))
        require(leaves and all(path == "properties.customRules.rules" or path.startswith("properties.customRules.rules[")
                              for path, _delta in leaves),
                "WSS update may change only API custom method rules")


def update(config_path, operation, approved, change_ticket, approvers):
    require(operation in {"plan", "execute"}, "Unknown WSS update operation")
    require(not config_path.is_symlink(), "Customer configuration cannot be a symlink")
    config, settings = load_config(config_path)
    require(application_authentication(config)["mode"] == "native",
            "This WSS contract is for native gateways; Entra proxy protocols require separate validation")
    require(settings["features"]["allowTrafficRelease"] is True, "Published-edge update requires traffic approval")
    revision = reviewed_revision()
    directory = operation_root(ROOT / "temp/local-stage09", "stage9-api-wss")
    authenticate_azure(config, settings, "deploy", directory)
    azure = AzureCommands(config, directory)
    approval = generate_report(config, settings, revision, change_ticket, approvers, azure)
    private_write(directory / "approval-report.json", json.dumps(approval, indent=2) + "\n")
    edge = deployed_edge(config, azure)
    association_id = edge["profileResourceId"] + "/securityPolicies/api-waf"
    association = deployed_resource(azure, association_id, "2025-04-15")
    identifier = association.get("properties", {}).get("parameters", {}).get("wafPolicy", {}).get("id", "")
    prefix = group_id(config) + "/providers/Microsoft.Network/frontDoorWebApplicationFirewallPolicies/"
    require(identifier.lower().startswith(prefix.lower()) and "/" not in identifier[len(prefix):]
            and identifier.lower() != edge["adminWafId"].lower(), "API WAF must be separate and in the target group")
    live = deployed_resource(azure, identifier, "2024-02-01")
    desired = desired_waf(live, identifier)
    require(desired["properties"]["policySettings"]["mode"] == approval["wafMode"],
            "Live API WAF mode differs from the approved canary report")
    require(desired["properties"]["customRules"]["rules"][-1].get("rateLimitThreshold")
            == config["parameters"]["edge"].get("rateLimitPerMinute", 600),
            "Live API rate limit differs from the approved configuration")
    template = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
                "contentVersion": "1.0.0.0", "resources": [desired]}
    template_path = directory / "api-wss-template.json"
    private_write(template_path, json.dumps(template, indent=2) + "\n")
    private_write(directory / "api-waf-before.json", json.dumps(live, indent=2) + "\n")
    common = ["--resource-group", config["target"]["resourceGroup"],
              "--name", f"llmgw-{config['environment']}-s9-api-wss", "--template-file", str(template_path)]
    what_if = azure.scoped(["deployment", "group", "what-if", *common,
                           "--result-format", "FullResourcePayloads", "--no-pretty-print"])
    require(what_if.get("status") == "Succeeded" and isinstance(what_if.get("changes"), list),
            "API WSS What-if failed")
    assert_protocol_changes(what_if["changes"], identifier)
    plan = build_plan(config, 9, "edge", revision, fingerprint(template),
                      {"apiWafBefore": live, "desired": desired, "edge": edge,
                       "association": association, "approval": approval},
                      what_if["changes"])
    private_write(directory / "reviewed-plan.json", json.dumps(plan, indent=2) + "\n")
    summary = {"planSha256": plan["planSha256"], "outputDirectory": str(directory.relative_to(ROOT)),
               "apiWafId": identifier, "wafMode": desired["properties"]["policySettings"]["mode"],
               "deployed": False, "deploymentAttempted": False, "status": "planned", "protocolAccepted": False}
    print(json.dumps(summary), flush=True)
    if operation == "plan":
        return summary
    require(approved == plan["planSha256"], "WSS plan changed or was not approved; replan")
    require(reviewed_revision() == revision and load_config(config_path) == (config, settings),
            "Code or configuration changed; replan")
    require(deployed_resource(azure, identifier, "2024-02-01") == live
            and deployed_resource(azure, association_id, "2025-04-15") == association
            and deployed_edge(config, azure) == edge, "Live WAF or edge changed; replan")
    summary["status"] = "executing"
    try:
        summary["deploymentAttempted"] = True
        result = azure.scoped(["deployment", "group", "create", *common, "--mode", "Incremental"])
        require(result.get("properties", {}).get("provisioningState") == "Succeeded", "WSS WAF deployment failed")
        summary["deployed"] = True
        after = deployed_resource(azure, identifier, "2024-02-01")
        observed = desired_waf(after, identifier)
        require(observed == desired
                and [normalized_rule(rule) for rule in after["properties"]["customRules"]["rules"]]
                == [normalized_rule(rule) for rule in desired["properties"]["customRules"]["rules"]],
                "API WAF does not match the approved WSS rules")
        require(deployed_resource(azure, association_id, "2025-04-15") == association
                and deployed_edge(config, azure) == edge,
                "API policy association, edge identity or Admin state changed during WSS update")
        summary["status"] = "completed"
    except Exception:
        summary["status"] = "failed"
        raise
    finally:
        private_write(directory / "wss-summary.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--operation", choices=("plan", "execute"), required=True)
    parser.add_argument("--approved-plan-sha256", default="")
    parser.add_argument("--change-ticket", required=True, help="Actual approved change reference")
    parser.add_argument("--approved-by", action="append", required=True, help="Actual approver object ID; repeat if required")
    args = parser.parse_args()
    try:
        update(args.config, args.operation, args.approved_plan_sha256, args.change_ticket, args.approved_by)
    except MigrationError as error:
        raise SystemExit(diagnostic_exit(error, "local-api-wss")) from None
    except Exception as error:
        raise SystemExit(diagnostic_exit(error, "local-api-wss", "API WSS update failed; inspect its output directory")) from None


if __name__ == "__main__":
    main()
