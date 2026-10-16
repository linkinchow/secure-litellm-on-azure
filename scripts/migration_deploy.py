"""Explicit, plan-bound ARM deployments. Never backs up data, cuts DNS, or certifies acceptance."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from scripts.customer_migration import (
    COMPONENTS, ROOT, MigrationError, active_stages, approval_policy, configured, fingerprint, prepare,
    private_write, require, stage_fingerprint, validate_config, validate_evidence,
)
from scripts.workflow_diagnostics import command_failure_summary, command_name, diagnostic_exit


def deployment_name(config, stage, component):
    base = f"llmgw-{config['environment']}-s{stage}-{component}"
    if stage == 0 and component == "bootstrap":
        scope = fingerprint({"location": config["location"], "targetResourceGroup": config["target"]["resourceGroup"].lower()})[:8]
        return f"{base}-{scope}"
    return base


def group_id(config, legacy=False):
    return f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{config['legacy' if legacy else 'target']['resourceGroup']}"


def assert_change_scope(config, component, changes, connectivity=None):
    allowed_types = {"Create", "Modify", "NoChange", "Ignore"}
    allowed_group = group_id(config, component in {"monitoring", "legacy-logging"}).lower()
    if component == "aks-ingress-role":
        actionable = [change for change in changes if change.get("changeType") not in {"NoChange", "Ignore"}]
        require(len(actionable) <= 1, "AKS ingress role plan must contain at most one change")
    external_accounts = {
        connection["accountResourceId"].rstrip("/").lower()
        for connection in config["parameters"].get("platform", {}).get("azureOpenAIConnections", [])
    }
    for change in changes:
        require(change.get("changeType") in allowed_types, "Delete, Unsupported or unknown changes require separate remediation; deployment blocked")
        if change["changeType"] in {"NoChange", "Ignore"}:
            continue
        resource = change.get("resourceId", "").lower()
        if component == "runner-target-connectivity":
            from scripts.runner_target_connectivity import target_connectivity_resource_ids
            require(resource in target_connectivity_resource_ids(config, connectivity), "Target connectivity plan attempts to modify an unapproved resource")
            continue
        if component == "certificate-vault":
            from scripts.certificate_vault import certificate_resource_ids
            require(resource in certificate_resource_ids(config), "Certificate plan attempts to modify an unapproved resource")
            continue
        if component == "runner-connectivity":
            from scripts.runner_connectivity import connectivity_resource_ids, connectivity_settings
            require(connectivity is not None, "Connectivity changes require resolved network resources")
            settings = connectivity_settings(config)
            dns_zone = connectivity.get("privateDnsZoneName", "")
            require(not settings["manageBlobDnsLink"] or configured(dns_zone), "Managed Blob DNS connectivity requires a resolved Private DNS zone")
            require(resource in connectivity_resource_ids(config, dns_zone), "Connectivity plan attempts to modify an unapproved resource")
            continue
        if component == "aks-ingress-role":
            network = config["parameters"]["platform"]["stage4Network"]
            prefix = (
                allowed_group + "/providers/microsoft.network/virtualnetworks/" + network["virtualNetworkName"].lower()
                + "/providers/microsoft.authorization/roleassignments/"
            )
            require(resource.startswith(prefix) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", resource.removeprefix(prefix)), "AKS network role plan attempts to modify an unapproved resource")
            continue
        local = resource.startswith(allowed_group + "/") or (component == "bootstrap" and resource == allowed_group)
        external_role = component == "platform" and any(
            resource.startswith(account + "/providers/microsoft.authorization/roleassignments/")
            for account in external_accounts
        )
        require(local or external_role, "Plan attempts to modify a resource outside the approved component scope")
        if component in {"monitoring", "legacy-logging"}:
            kinds = ("microsoft.operationalinsights/workspaces", "microsoft.resources/deployments") if component == "legacy-logging" else (
                "microsoft.insights/actiongroups", "microsoft.insights/scheduledqueryrules",
                "microsoft.insights/activitylogalerts", "microsoft.resources/deployments",
            )
            require(any(resource.startswith(allowed_group + "/providers/" + kind + "/") for kind in kinds), "Monitoring deployment cannot replace legacy application or data resources")


class AzureCommands:
    def __init__(self, config, directory):
        self.config = config
        self.directory = directory
        self.counter = 0

    def _run(self, arguments, allow_empty=False):
        self.counter += 1
        result = subprocess.run(["az", *arguments, "--only-show-errors", "--output", "json"], capture_output=True, text=True, check=False)
        private_write(self.directory / f"command-{self.counter}.stdout.json", result.stdout)
        private_write(self.directory / f"command-{self.counter}.stderr.txt", result.stderr)
        operation = command_name(["az", *arguments])
        if result.returncode:
            raise MigrationError(f"Azure command failed ({operation}): {command_failure_summary(result.stdout, result.stderr, result.returncode)}")
        if not result.stdout.strip():
            if allow_empty:
                return None
            raise MigrationError(f"{operation} returned no JSON output")
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise MigrationError(f"{operation} returned invalid JSON") from None

    def run(self, arguments):
        return self._run(arguments)

    def run_allow_empty(self, arguments):
        return self._run(arguments, allow_empty=True)

    def scoped(self, arguments):
        return self.run([*arguments, "--subscription", self.config["azure"]["subscriptionId"]])

    def scoped_allow_empty(self, arguments):
        return self.run_allow_empty([*arguments, "--subscription", self.config["azure"]["subscriptionId"]])


def resolve_origin(config, component, azure):
    resolved = copy.deepcopy(config)
    if component == "audit":
        audit = resolved["parameters"]["audit"]
        roles = ("writer", "reader", "retention")
        if "recoveryPrincipalId" in audit:
            roles += ("recovery",)
        if any(audit.get(role + "PrincipalId") == "auto" or not configured(audit.get(role + "PrincipalId", "")) for role in roles):
            output = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 8, "audit-foundation"), "--query", "{state:properties.provisioningState,foundation:properties.outputs.auditFoundation.value}"])
            require(output.get("state") == "Succeeded", "Deploy audit-foundation before resolving audit identities")
            foundation = output["foundation"]
            require(all(audit[key] == foundation[key] for key in ("cmkVaultName", "cmkKeyName")), "Audit foundation Vault/key differs from configured names")
            for role in roles:
                key = role + "PrincipalId"
                if audit.get(key) == "auto" or not configured(audit.get(key, "")):
                    require(isinstance(foundation.get(role), dict), "Redeploy audit-foundation to initialize the requested identity")
                    audit[key] = foundation[role]["principalId"]
        require(len({audit[role + "PrincipalId"].lower() for role in roles}) == len(roles), "Audit roles must be distinct service principals")
    elif component == "edge":
        origins = {
            "api": resolved["parameters"]["edge"]["privateOrigin"],
            "admin": resolved["parameters"]["edge"]["adminPrivateOrigin"],
        }
        if any(not configured(origin["privateLinkServiceId"]) or origin["privateLinkServiceId"] == "auto" for origin in origins.values()):
            output = azure.scoped([
                "deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"],
                "--name", deployment_name(config, 9, "origin"),
                "--query", "{state:properties.provisioningState,api:properties.outputs.privateOrigin.value,admin:properties.outputs.adminPrivateOrigin.value}",
            ])
            require(output.get("state") == "Succeeded", "Deploy the origin component before resolving edge")
            for plane, origin in origins.items():
                if not configured(origin["privateLinkServiceId"]) or origin["privateLinkServiceId"] == "auto":
                    require(isinstance(output.get(plane), dict), f"Origin deployment lacks the {plane} Private Link Service output")
                    deployed_origin = output[plane]
                    require(deployed_origin.get("privateLinkLocation") == config["location"], f"Deployed {plane} Private Link Service location differs from the target region")
                    origin["privateLinkServiceId"] = deployed_origin.get("privateLinkServiceId", "")
        prefix = group_id(config).lower() + "/providers/microsoft.network/privatelinkservices/"
        require(all(origin["privateLinkServiceId"].lower().startswith(prefix) for origin in origins.values()), "PLS resources must belong to the approved target resource group")
        require(origins["api"]["privateLinkServiceId"].lower() != origins["admin"]["privateLinkServiceId"].lower(), "API and Admin must use separate Private Link Services")
    elif component == "origin":
        cluster_name = config["parameters"]["platform"]["stage4Aks"]["name"]
        node_group = azure.scoped(["aks", "show", "--resource-group", config["target"]["resourceGroup"], "--name", cluster_name, "--query", "nodeResourceGroup"])
        load_balancers = {plane: resolved["parameters"]["origin"][plane + "LoadBalancer"] for plane in ("api", "admin")}
        managed_addresses = {}
        if "privateIngress" in config:
            output = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 4, "private-ingress"), "--query", "{state:properties.provisioningState,ingress:properties.outputs.privateIngress.value}"])
            require(output.get("state") == "Succeeded", "Deploy and verify private-ingress before creating the origin")
            ingress = output["ingress"]
            require(ingress.get("configSha256") == stage_fingerprint(config, 4), "Private ingress outputs differ from the approved Stage 4 configuration")
            from scripts.backend_manifest import application_authentication
            expected_authentication = application_authentication(config)["mode"] if "application" in config else "entra"
            require(ingress.get("authenticationMode", "entra") == expected_authentication, "Private ingress authentication mode differs from the application; replan and execute private-ingress")
            for plane, load_balancer in load_balancers.items():
                managed = ingress[plane]
                managed_addresses[plane] = managed["privateIpAddress"]
                for key in ("resourceGroupName", "name", "frontendName"):
                    require(load_balancer[key] == "auto" or not configured(load_balancer[key]) or load_balancer[key] == managed[key], f"Configured origin differs from the verified {plane} ingress")
                    load_balancer[key] = managed[key]
            require(managed_addresses["api"] != managed_addresses["admin"], "API/Admin private frontends must have separate addresses")
        candidates = azure.scoped(["network", "lb", "list", "--resource-group", node_group])
        selected = {}
        expected_subnet = group_id(config) + "/providers/Microsoft.Network/virtualNetworks/" + resolved["parameters"]["origin"]["virtualNetworkName"] + "/subnets/" + resolved["parameters"]["origin"]["ingressSubnetName"]
        for plane, load_balancer in load_balancers.items():
            supplied_group = load_balancer["resourceGroupName"]
            require(supplied_group == "auto" or not configured(supplied_group) or supplied_group.lower() == node_group.lower(), "Configured load balancer group differs from actual AKS node resource group")
            matches = []
            for candidate in candidates:
                if candidate.get("sku", {}).get("name") != "Standard":
                    continue
                if configured(load_balancer["name"]) and load_balancer["name"] != "auto" and candidate["name"] != load_balancer["name"]:
                    continue
                for frontend in candidate.get("frontendIPConfigurations", []):
                    if frontend.get("publicIPAddress") or not frontend.get("privateIPAddress"):
                        continue
                    if managed_addresses.get(plane) and frontend["privateIPAddress"] != managed_addresses[plane]:
                        continue
                    if configured(load_balancer["frontendName"]) and load_balancer["frontendName"] != "auto" and frontend["name"] != load_balancer["frontendName"]:
                        continue
                    if frontend.get("subnet", {}).get("id", "").lower() == expected_subnet.lower():
                        matches.append({"resourceGroupName": node_group, "name": candidate["name"], "frontendName": frontend["name"]})
            require(len(matches) == 1, f"Expected exactly one reviewed private {plane} frontend; specify actual LB/frontend names to resolve ambiguity")
            selected[plane] = matches[0]
            resolved["parameters"]["origin"][plane + "LoadBalancer"] = matches[0]
        require(selected["api"] != selected["admin"], "API and Admin origins must not share one load balancer frontend")
    return resolved


def build_plan(config, stage, component, revision, template_hash, parameters, changes, connectivity=None):
    assert_change_scope(config, component, changes, connectivity)
    relevant = sorted((change for change in changes if change["changeType"] != "Ignore"), key=lambda change: change.get("resourceId", ""))
    contract = {
        "stage": stage, "component": component, "environment": config["environment"],
        "revision": revision, "configSha256": stage_fingerprint(config, stage),
        "templateSha256": template_hash, "parametersSha256": fingerprint(parameters),
        "changes": relevant,
    }
    return {
        "planSha256": fingerprint(contract), "stage": stage, "component": component,
        "environment": config["environment"], "revision": revision,
        "configSha256": contract["configSha256"], "templateSha256": template_hash,
        "parametersSha256": contract["parametersSha256"],
        "changes": [{"resourceId": change.get("resourceId"), "changeType": change["changeType"],
                     "changedProperties": [delta.get("path") for delta in (change.get("delta") or [])]}
                    for change in relevant],
    }


def what_if_leaf_deltas(deltas, parent=""):
    require(isinstance(deltas, list) and deltas, "What-if requires explicit property changes")
    for delta in deltas:
        segment = delta.get("path", "")
        require(isinstance(segment, str) and segment, "What-if property path is missing")
        path = parent + f"[{segment}]" if segment.isdigit() else ".".join(filter(None, (parent, segment)))
        children = delta.get("children")
        if children:
            yield from what_if_leaf_deltas(children, path)
        else:
            yield path, delta


def assert_allowlist_changes(changes, edge):
    require(edge.get("apiTrafficEnabled") is True and edge.get("adminTrafficEnabled") is True,
            "Allowlist updates require both existing traffic gates to be enabled")
    for change in changes:
        if change.get("changeType") in {"NoChange", "Ignore"}:
            continue
        require(change.get("changeType") == "Modify"
                and change.get("resourceId", "").lower() == edge["adminWafId"].lower(),
                "Allowlist update cannot change endpoints, routes or other resources")
        deltas = change.get("delta")
        require(isinstance(deltas, list) and deltas,
                "Allowlist update requires explicit WAF property changes")
        require(all(re.fullmatch(r"properties\.customRules\.rules\[0\]\.matchConditions\[0\]\.matchValue(?:\[\d+\])?",
                                 path) for path, _delta in what_if_leaf_deltas(deltas)),
                "Allowlist update requires granular source-IP matchValue changes only")


def prepare_allowlist_template(config, edge, azure, directory, parameters):
    identifier = edge["adminWafId"]
    prefix = group_id(config) + "/providers/Microsoft.Network/frontDoorWebApplicationFirewallPolicies/"
    require(identifier.lower().startswith(prefix.lower()) and "/" not in identifier[len(prefix):],
            "Admin WAF must belong to the approved resource group")
    live = azure.scoped(["resource", "show", "--ids", identifier, "--api-version", "2024-02-01"])
    require(live.get("id", "").lower() == identifier.lower(), "Azure returned a different Admin WAF")
    properties = live["properties"]
    require(set(properties).issubset({"policySettings", "customRules", "managedRules",
                                     "frontendEndpointLinks", "securityPolicyLinks", "routingRuleLinks",
                                     "resourceState", "provisioningState"}),
            "Admin WAF has unknown properties; review before updating")
    desired = {key: copy.deepcopy(properties[key]) for key in ("policySettings", "customRules", "managedRules")}
    require(properties.get("provisioningState") == "Succeeded"
            and desired["policySettings"].get("mode") == "Prevention",
            "Admin WAF must be provisioned in Prevention mode")
    rules = desired["customRules"]["rules"]
    require(rules and rules[0].get("name") == "BlockUnapprovedAdminSources"
            and len(rules[0].get("matchConditions", [])) == 1,
            "Admin source-IP rule must retain its reviewed position and condition")
    require(rules[0]["matchConditions"][0].get("matchValue") == edge["adminAllowedCidrs"],
            "Live Admin WAF differs from the reviewed baseline")
    rules[0]["matchConditions"][0]["matchValue"] = config["parameters"]["edge"]["adminAllowedCidrs"]
    resource = {
        "type": "Microsoft.Network/frontDoorWebApplicationFirewallPolicies",
        "apiVersion": "2024-02-01", "name": identifier[len(prefix):],
        "location": live["location"], "sku": {"name": live["sku"]["name"]},
        "properties": desired,
    }
    if live.get("tags") is not None:
        resource["tags"] = live["tags"]
    updated_edge = copy.deepcopy(edge)
    updated_edge["adminAllowedCidrs"] = config["parameters"]["edge"]["adminAllowedCidrs"]
    declarations = {}
    for key, parameter in parameters["parameters"].items():
        value = parameter["value"]
        kind = {str: "string", bool: "bool", int: "int", list: "array", dict: "object"}.get(type(value))
        require(kind is not None, "Unsupported edge parameter type")
        declarations[key] = {"type": kind}
    template = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0", "parameters": declarations,
        "resources": [resource], "outputs": {"edge": {"type": "object", "value": updated_edge}},
    }
    private_write(directory / "template.json", json.dumps(template, indent=2) + "\n")
    private_write(directory / "admin-waf-before.json", json.dumps(live, indent=2) + "\n")
    return directory / "template.json"


def deploy_component(config, stage, component, revision, operation, previous, directory, approved_plan="", azure=None, release=None, allowlist_baseline=None):
    require(operation in {"plan", "deploy"}, "Invalid deployment operation")
    require(component in COMPONENTS, "Unknown deployment component")
    require(re.fullmatch(r"[0-9a-f]{40}", revision or "") is not None, "Full reviewed Git revision required")
    if allowlist_baseline is not None:
        require(operation == "plan" and stage == 9 and component == "edge" and release is not None,
                "Allowlist baseline is only supported by the reviewed Stage9 edge plan")
        expected = copy.deepcopy(allowlist_baseline)
        before = expected["parameters"]["edge"]["adminAllowedCidrs"]
        after = config["parameters"]["edge"]["adminAllowedCidrs"]
        expected["parameters"]["edge"]["adminAllowedCidrs"] = after
        require(expected == config and set(before) < set(after),
                "Allowlist update must only add Admin CIDRs")
        require(config["environment"] in {"dev", "test"} and release.get("phase") == "canary"
                and release.get("authenticationMode") == "native" and release.get("auditMode") == "native",
                "Allowlist update is limited to native dev/test canary")
    validate_config(config, config["environment"])
    require(stage in active_stages(config), "Stage does not apply to the selected deployment mode")
    completed = set()
    if operation == "deploy":
        completed = validate_evidence(previous, stage, config, revision)
        require(re.fullmatch(r"[0-9a-f]{64}", approved_plan or "") is not None, "Deploy requires an approved plan SHA256")
        require(not (component in {"backup", "network"} and any(value >= 4 for value in completed)), "Do not reapply the bootstrap VNet after Stage 4")
        require(not (component == "platform" and any(value > stage for value in completed)), "Do not apply an earlier platform stage over a later accepted stage")
    directory = Path(directory).resolve()
    require(directory.is_relative_to((ROOT / "temp").resolve()) and directory != (ROOT / "temp").resolve(), "Deployment output must stay under temp/")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    azure = azure or AzureCommands(config, directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope does not match customer configuration")
    connectivity = None
    if component == "certificate-vault":
        from scripts.certificate_vault import inspect_certificate_infrastructure
        inspect_certificate_infrastructure(config, azure)
    if component == "runner-connectivity":
        from scripts.runner_connectivity import inspect_connectivity
        connectivity = inspect_connectivity(config, azure)
    if component == "runner-target-connectivity":
        from scripts.runner_target_connectivity import inspect_target_connectivity
        connectivity = inspect_target_connectivity(config, azure)
    resolved = resolve_origin(config, component, azure)
    template, path = prepare(resolved, stage, component, directory)
    if component == "runner-target-connectivity":
        document = json.loads(path.read_text())
        for key in ("createDnsLink", "dnsResourceGroupName", "privateDnsZoneName", "linkName", "aksResourceId", "apiHostname", "createAcrDnsLink", "acrDnsResourceGroupName", "acrPrivateDnsZoneName", "acrLinkName", "acrResourceId", "acrLoginServer"):
            document["parameters"][key] = {"value": connectivity[key]}
        private_write(path, json.dumps(document, indent=2) + "\n")
        private_write(directory / "connectivity-review.json", json.dumps(connectivity, indent=2) + "\n")
    if release is not None:
        from scripts.stage9_release import validate_release
        require(stage == 9 and component == "edge", "Release operation only applies to Stage 9 edge")
        expected_audit = "native" if "contentAudit" in config else "l3"
        require(release.get("optionalL3Enabled", False) is ("l3Audit" in config),
                "Release optional L3 decision differs from customer configuration")
        require(release.get("auditMode", "l3") == expected_audit, "Release audit mode differs from customer configuration")
        from scripts.backend_manifest import application_authentication
        expected_authentication = application_authentication(config)["mode"] if "application" in config else "entra"
        require(release.get("authenticationMode", "entra") == expected_authentication, "Release authentication mode differs from customer configuration")
        if expected_audit == "native":
            require(release.get("telemetryEnabled") is ("observability" in config), "Release telemetry decision differs from customer configuration")
        mode, eligible = approval_policy(config)
        validate_release(release, required_approvers=1 if mode == "single-operator" else 2, eligible_approvers=eligible)
        require(len(release["approvedBy"]) == (1 if mode == "single-operator" else 2), "Release approval count differs from configured policy")
        require(release.get("revision") == revision and release.get("configSha256") == stage_fingerprint(config, 9), "Release must bind the reviewed code and stage configuration")
        require(release["environmentName"] == config["environment"] and release["baseDomain"] == config["baseDomain"], "Release environment/domain mismatch")
        require(release["privateOrigin"] == resolved["parameters"]["edge"]["privateOrigin"], "Release PLS differs from deployed origin")
        require(release["adminPrivateOrigin"] == resolved["parameters"]["edge"]["adminPrivateOrigin"], "Release Admin PLS differs from deployed origin")
        require(release["adminAllowedCidrs"] == resolved["parameters"]["edge"]["adminAllowedCidrs"], "Release Admin source-IP allowlist differs from the reviewed edge")
        require(release["logAnalyticsWorkspaceName"] == resolved["parameters"]["edge"]["logAnalyticsWorkspaceName"] and release["rateLimitPerMinute"] == resolved["parameters"]["edge"].get("rateLimitPerMinute", 600) and release["adminRateLimitPerMinute"] == resolved["parameters"]["edge"].get("adminRateLimitPerMinute", 120), "Release logging/rate configuration mismatch")
        previous_edge = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge"), "--query", "{state:properties.provisioningState,edge:properties.outputs.edge.value}"])
        require(previous_edge.get("state") == "Succeeded", "Provision the disabled edge before releasing traffic")
        require(release.get("frontDoorId") == previous_edge.get("edge", {}).get("profileId"), "Release Front Door identity differs from the provisioned edge")
        if release["phase"] != "prepare" and "application" in config:
            from scripts.edge_binding import require_edge_binding
            binding_client = None
            from scripts.audit_runtime import AuditCluster
            from scripts.migration_runtime import connect_cluster
            kube = connect_cluster(config, directory, legacy=False)
            if expected_authentication == "native":
                if kube[-2:] == ["--namespace", "litellm"]:
                    kube = kube[:-2]
                binding_client = {plane: AuditCluster([*kube, "--namespace", f"llm-{plane}-ingress"], directory) for plane in ("api", "admin")}
            else:
                binding_client = AuditCluster(kube, directory)
            require_edge_binding(allowlist_baseline or config, revision, release["frontDoorId"], azure, binding_client)
            if expected_authentication == "native":
                from scripts.private_ingress_runtime import require_private_ingress_backends
                require_private_ingress_backends(allowlist_baseline or config, azure, front_door_id=release["frontDoorId"])
        document = json.loads(path.read_text())
        document["parameters"]["enableApiTraffic"] = {"value": release["phase"] != "prepare"}
        document["parameters"]["enableAdminTraffic"] = {"value": release["phase"] != "prepare"}
        document["parameters"]["wafMode"] = {"value": release["wafMode"]}
        private_write(path, json.dumps(document, indent=2) + "\n")
    compiled = directory / "template.json"
    if allowlist_baseline is not None:
        compiled = prepare_allowlist_template(config, previous_edge["edge"], azure, directory,
                                              json.loads(path.read_text()))
    else:
        result = subprocess.run(["az", "bicep", "build", "--file", str(template), "--outfile", str(compiled)], capture_output=True, text=True, check=False)
        private_write(directory / "bicep-diagnostics.txt", result.stderr)
        if result.returncode:
            raise MigrationError(f"Bicep compilation failed: {command_failure_summary(result.stdout, result.stderr, result.returncode)}")
    template_hash = hashlib.sha256(compiled.read_bytes()).hexdigest()
    scope = "sub" if component == "bootstrap" else "group"
    scope_args = ["--location", config["location"]] if scope == "sub" else ["--resource-group", config["legacy" if component in {"monitoring", "legacy-logging"} else "target"]["resourceGroup"]]
    common = [*scope_args, "--name", deployment_name(config, stage, component), "--template-file", str(compiled), "--parameters", f"@{path}"]
    response = azure.scoped(["deployment", scope, "what-if", *common, "--result-format", "FullResourcePayloads", "--no-pretty-print"])
    require(response.get("status") == "Succeeded" and isinstance(response.get("changes"), list), "What-if did not produce a successful change list")
    if allowlist_baseline is not None:
        assert_allowlist_changes(response["changes"], previous_edge["edge"])
    reviewed_parameters = {"parameters": json.loads(path.read_text()), "release": release}
    if allowlist_baseline is not None:
        reviewed_parameters["allowlistBaselineSha256"] = stage_fingerprint(allowlist_baseline, 9)
    if connectivity is not None:
        reviewed_parameters["connectivity"] = connectivity
    plan = build_plan(config, stage, component, revision, template_hash, reviewed_parameters, response["changes"], connectivity)
    private_write(directory / "reviewed-plan.json", json.dumps({"planSha256": plan["planSha256"], "revision": revision, "changes": [change for change in response["changes"] if change["changeType"] != "Ignore"]}, indent=2) + "\n")
    private_write(directory / "plan-summary.json", json.dumps(plan, indent=2) + "\n")
    print(json.dumps({"planSha256": plan["planSha256"], "stage": stage, "component": component, "deploymentPerformed": False}))
    if operation == "plan":
        return plan
    require(plan["planSha256"] == approved_plan, "Plan changed since approval; review the new plan before deploying")
    create_args = ["deployment", scope, "create", *common]
    if scope == "group":
        create_args.extend(["--mode", "Incremental"])
    deployed = azure.scoped(create_args)
    require(deployed.get("properties", {}).get("provisioningState") == "Succeeded", "Deployment did not succeed; no acceptance evidence created")
    if component == "runner-target-connectivity":
        verified = inspect_target_connectivity(config, azure, require_link=True)
        require(verified == connectivity, "AKS DNS context changed during deployment; inspect connectivity before proceeding")
    receipt = {key: plan[key] for key in ("stage", "component", "environment", "revision", "configSha256", "planSha256")}
    receipt.update(deploymentId=deployed.get("id"), provisioningState="Succeeded", stageAccepted=False)
    private_write(directory / "deployment-outputs.json", json.dumps(deployed.get("properties", {}).get("outputs", {}), indent=2) + "\n")
    private_write(directory / "deployment-receipt.json", json.dumps(receipt, indent=2) + "\n")
    print("ARM deployment succeeded. Complete runtime checks and record acceptance separately.")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("plan", "deploy"), required=True)
    parser.add_argument("--stage", type=int, choices=range(10), required=True)
    parser.add_argument("--component", choices=tuple(COMPONENTS), required=True)
    parser.add_argument("--environment", choices=("dev", "test", "prod"), required=True)
    parser.add_argument("--release", action="store_true", help="Use separately approved Stage 9 release attestations")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "temp/migration-deploy")
    args = parser.parse_args()
    config = validate_config(json.loads(os.environ["CUSTOMER_CONFIG_JSON"]), args.environment)
    require(os.environ.get("AZURE_TENANT_ID") == config["azure"]["tenantId"] and os.environ.get("AZURE_SUBSCRIPTION_ID") == config["azure"]["subscriptionId"], "Configured Azure scope mismatch")
    if args.operation == "deploy":
        require(os.environ.get("MIGRATION_CONFIRM_ENVIRONMENT") == args.environment, "Explicit environment confirmation required")
    from scripts.workflow_artifacts import approved_operation, load_evidence, write_operation_receipt
    revision = os.environ.get("GITHUB_SHA", os.environ.get("MIGRATION_REVISION", ""))
    approved = os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", "")
    previous = json.loads(os.environ.get("MIGRATION_EVIDENCE_JSON") or "[]")
    if args.operation == "deploy":
        if os.environ.get("MIGRATION_APPROVED_RUN_ID"):
            approved = approved_operation(config, revision, args.stage, args.component, os.environ["MIGRATION_APPROVED_RUN_ID"], "infrastructure")
        if os.environ.get("MIGRATION_AUTO_EVIDENCE") == "true":
            previous = load_evidence(config, args.stage, revision)
    deploy_component(config, args.stage, args.component,
                     revision, args.operation, previous, args.output_dir, approved,
                     release=json.loads(os.environ["MIGRATION_RELEASE_JSON"]) if args.release else None)
    write_operation_receipt(args.output_dir, config, revision, args.stage, args.component, args.operation, "infrastructure")


if __name__ == "__main__":
    try:
        main()
    except MigrationError as error:
        raise SystemExit(diagnostic_exit(error, "migration-deploy")) from None
    except Exception as error:
        raise SystemExit(diagnostic_exit(error, "migration-deploy", "Deployment controller failed")) from None