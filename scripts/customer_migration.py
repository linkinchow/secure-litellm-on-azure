#!/usr/bin/env python3
"""Customer migration guidance and read-only previews; never deploy or cut over."""

import argparse
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import UUID

from scripts.render_stage7_domain import domain_hosts, render
from scripts.workflow_diagnostics import command_failure_summary, diagnostic_exit

ROOT = Path(__file__).resolve().parents[1]
FRONT_DOOR_PRIVATE_LINK_LOCATIONS = frozenset({
    "australiaeast", "brazilsouth", "canadacentral", "centralindia", "centralus",
    "chinaeast3", "chinanorth3", "eastasia", "eastus", "eastus2", "francecentral",
    "germanywestcentral", "japaneast", "koreacentral", "northeurope", "norwayeast",
    "southafricanorth", "southcentralus", "southeastasia", "swedencentral", "uaenorth",
    "uksouth", "usgovarizona", "usgovtexas", "usgovvirginia", "usnateast", "usnatwest",
    "usseceast", "ussecwest", "westeurope", "westus2", "westus3",
})
STAGES = (
    ("Baseline and recoverable backup", ("inventory", "backup_restore", "key_salt_recovery", "protocol_baseline")),
    ("Legacy minimum hardening", ("legacy_health", "alerts_received", "rollback_snapshot")),
    ("Customer architecture decisions", ("network_capacity", "identity_owners", "pg_auth_ha", "l3_policy", "protocol_scope")),
    ("Source supply chain and isolated target", ("oidc_scope", "source_image_sbom_scan", "target_isolation")),
    ("Private network and AKS", ("private_dns_egress", "private_runner", "target_image_signature_sbom", "workload_identity", "private_ingress")),
    ("Data platform and restore rehearsal", ("pg_migration_restore", "key_salt_decryption", "redis_entra", "csi_rotation")),
    ("HA and routing", ("replica_failure", "load_affinity", "capacity_limits", "no_legacy_db_writes")),
    ("Entra and split domains", ("tenant_negative_tests", "object_ownership", "admin_private", "required_protocols")),
    ("L3 and observability", ("durable_audit_recovery", "audit_governance", "telemetry_received", "guardrail_scope")),
    ("Approved pilot and cutover", ("enabled_what_if", "origin_tls_private_link", "pilot_regression", "rollback_rehearsal", "dual_owner_release")),
)
COMPONENTS = {
    "bootstrap": (0, "bootstrap", {"workspaceMode", "logRetentionDays", "logAnalyticsWorkspaceName"}),
    "network": (0, "network-bootstrap", {"virtualNetworkName", "virtualNetworkAddressPrefix", "privateEndpointSubnetName", "privateEndpointSubnetPrefix"}),
    "backup": (0, "backup-storage", {"storageAccountName", "virtualNetworkName", "virtualNetworkAddressPrefix", "privateEndpointSubnetName", "privateEndpointSubnetPrefix", "logAnalyticsWorkspaceName", "backupOwnerPrincipalId", "backupAutomationPrincipalId", "storageSku", "softDeleteRetentionDays"}),
    "runner-connectivity": (0, "runner-connectivity", {"runnerVirtualNetworkId", "managePeering", "manageBlobDnsLink"}),
    "hub": (0, "hub", {"resourceGroupName", "virtualNetworkName", "virtualNetworkAddressPrefix", "firewallSubnetPrefix", "logAnalyticsWorkspaceName", "logAnalyticsWorkspaceResourceGroupName", "reservedPrefixes"}),
    "legacy-logging": (1, "legacy-logging", {"workspaceMode"}),
    "monitoring": (1, "monitoring", {"logAnalyticsWorkspaceName"}),
    "platform": (3, "environments", {"containerRegistryName", "logAnalyticsWorkspaceName", "stage4Network", "stage4Aks", "stage4RoleAssignmentNaming", "stage5Data", "approvedHttpsFqdns", "azureOpenAIConnections", "createStage5KeyVaultPrivateDnsZone", "createStage5PostgresqlPrivateDnsZone", "createStage5ManagedRedisPrivateDnsZone"}),
    "aks-ingress-role": (4, "aks-ingress-role", set()),
    "certificate-vault": (4, "certificate-vault", {"vaultName", "ingressReaderPrincipalId", "certificateImporterPrincipalId", "certificateImporterPrincipalType", "runnerVirtualNetworkId", "createPrivateDnsZone", "manageRunnerDnsLink", "manageTargetDnsLink"}),
    "runner-target-connectivity": (4, "runner-target-connectivity", {"runnerVirtualNetworkId", "manageDnsLink"}),
    "audit-foundation": (8, "audit-foundation", set()),
    "observability": (8, "observability", set()),
    "proxy-foundation": (7, "proxy-foundation", set()),
    "audit": (8, "audit-storage", {"storageAccountName", "virtualNetworkName", "privateEndpointSubnetName", "logAnalyticsWorkspaceName", "cmkVaultName", "cmkKeyName", "writerPrincipalId", "readerPrincipalId", "retentionPrincipalId", "recoveryPrincipalId"}),
    "origin": (9, "edge-origin", {"privateLinkServiceName", "adminPrivateLinkServiceName", "virtualNetworkName", "ingressSubnetName", "apiLoadBalancer", "adminLoadBalancer"}),
    "edge": (9, "edge", {"type", "privateOrigin", "adminPrivateOrigin", "adminAllowedCidrs", "logAnalyticsWorkspaceName", "rateLimitPerMinute", "adminRateLimitPerMinute", "subnetName", "subnetPrefix", "availabilityZones", "minCapacity", "maxCapacity", "requestTimeoutSeconds", "wafExcludePromptFields"}),
}
REQUIRED = {
    "bootstrap": set(),
    "network": {"virtualNetworkName", "virtualNetworkAddressPrefix", "privateEndpointSubnetName", "privateEndpointSubnetPrefix"},
    "legacy-logging": set(),
    "backup": {"logAnalyticsWorkspaceName", "backupOwnerPrincipalId", "virtualNetworkName", "virtualNetworkAddressPrefix", "privateEndpointSubnetName", "privateEndpointSubnetPrefix"},
    "runner-connectivity": {"runnerVirtualNetworkId"},
    "hub": {"resourceGroupName", "virtualNetworkName", "virtualNetworkAddressPrefix", "firewallSubnetPrefix", "logAnalyticsWorkspaceName"},
    "monitoring": {"logAnalyticsWorkspaceName"},
    "platform": {"containerRegistryName", "logAnalyticsWorkspaceName", "stage4Network", "stage4Aks"},
    "aks-ingress-role": set(),
    "certificate-vault": {"vaultName", "ingressReaderPrincipalId", "certificateImporterPrincipalId", "certificateImporterPrincipalType", "runnerVirtualNetworkId", "createPrivateDnsZone", "manageRunnerDnsLink"},
    "runner-target-connectivity": {"runnerVirtualNetworkId"},
    "audit-foundation": set(),
    "observability": set(),
    "proxy-foundation": set(),
    "audit": {"virtualNetworkName", "privateEndpointSubnetName", "logAnalyticsWorkspaceName", "cmkVaultName", "cmkKeyName", "writerPrincipalId", "readerPrincipalId", "retentionPrincipalId"},
    "origin": {"virtualNetworkName", "ingressSubnetName", "apiLoadBalancer", "adminLoadBalancer"},
    "edge": {"privateOrigin", "adminPrivateOrigin", "adminAllowedCidrs", "logAnalyticsWorkspaceName"},
}
# Subscription-scope templates that create and own their resource group.
SUBSCRIPTION_COMPONENTS = frozenset({"bootstrap", "hub"})


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def approval_policy(config):
    policy = config.get("governance", {})
    require(isinstance(policy, dict) and not set(policy) - {"approvalMode", "approverObjectIds", "singleOperatorRiskAccepted", "githubLogin"}, "Invalid governance fields")
    mode = policy.get("approvalMode", "dual")
    require(mode in {"dual", "single-operator"}, "Invalid approvalMode")
    allowed = policy.get("approverObjectIds", [])
    require(isinstance(allowed, list), "approverObjectIds must be an array")
    identities = {UUID(value) for value in allowed}
    require(len(identities) == len(allowed) and all(identity.int for identity in identities), "Invalid or duplicate approved operator IDs")
    if mode == "single-operator":
        require(policy.get("singleOperatorRiskAccepted") is True and len(identities) == 1, "Single-operator mode requires one named operator and explicit risk acceptance")
    elif allowed:
        require(len(identities) >= 2, "Dual approval requires at least two eligible approvers")
    if "githubLogin" in policy:
        require(mode == "single-operator" and isinstance(policy["githubLogin"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", policy["githubLogin"]), "githubLogin requires an explicitly approved single operator")
    return mode, identities


def deployment_mode(config):
    mode = config.get("deploymentMode", "migration")
    require(mode in {"migration", "greenfield"}, "deploymentMode must be migration or greenfield")
    return mode


def active_stages(config):
    native = config.get("application", {}).get("authentication", {}).get("mode") == "native"
    return tuple(stage for stage in range(10) if (deployment_mode(config) != "greenfield" or stage != 1) and (not native or stage != 7))


def stage_title(stage, config):
    require(stage in active_stages(config), "Stage does not apply to the selected deployment mode")
    if stage == 8 and "contentAudit" in config:
        if "l3Audit" in config:
            return "Optional TypeScript L3 and native spend metadata"
        return "Native Spend Logs and observability"
    if deployment_mode(config) == "greenfield":
        return {0: "New environment prerequisites", 5: "Data platform and database initialization", 9: "Approved pilot and first release"}.get(stage, STAGES[stage][0])
    return STAGES[stage][0]


def stage_checks(stage, config):
    require(stage in active_stages(config), "Stage does not apply to the selected deployment mode")
    checks = list(STAGES[stage][1])
    if stage == 1 and "legacyAccess" in config:
        checks.append("legacy_source_access")
    if "contentAudit" in config:
        if stage == 2:
            checks[checks.index("l3_policy")] = "content_audit_policy"
        elif stage == 8:
            checks = ["native_spend_logs", "native_audit_access", "native_retention_recovery", "telemetry_received" if "observability" in config else "telemetry_disabled", "guardrail_scope"]
            if config.get("application", {}).get("authentication", {}).get("mode") == "native":
                checks[-1] = "native_api_route_isolation"
    if deployment_mode(config) == "greenfield":
        replacements = {
            0: ("subscription_scope", "deployment_permissions", "private_runner", "domain_ownership"),
            5: ("database_schema", "application_secret_recovery", "redis_entra", "csi_rotation"),
            6: ("replica_failure", "load_affinity", "capacity_limits", "target_database_scope"),
        }
        checks = list(replacements.get(stage, checks))
    if stage == 8 and "l3Audit" in config:
        checks = ["native_spend_metadata", "l3_protocol_capture", "l3_delivery_recovery",
                  "l3_query_hold", "l3_alerts", "native_api_route_isolation"]
    if stage == 9 and config.get("parameters", {}).get("edge", {}).get("type") == "applicationGateway":
        checks[checks.index("origin_tls_private_link")] = "origin_tls_private_backend"
    if stage == 9 and approval_policy(config)[0] == "single-operator":
        checks[-1] = "single_operator_release"
    return checks


def stage_fingerprint(config, stage):
    scoped = {key: value for key, value in config.items() if key != "parameters"}
    if stage != 4 and stage < 8:
        scoped.pop("l3Audit", None)
    if 4 <= stage < 6:
        scoped["applicationAuthenticationMode"] = config.get("application", {}).get("authentication", {}).get("mode", "entra")
    if stage < 1:
        scoped.pop("legacyAccess", None)
    if stage < 2:
        scoped.pop("contentAudit", None)
    if stage < 9:
        scoped.pop("dns", None)
    if stage < 8:
        scoped.pop("auditRuntime", None)
        scoped.pop("observability", None)
        scoped.pop("auditGovernance", None)
    if stage < 7:
        scoped.pop("proxy", None)
        scoped.pop("entra", None)
    if stage < 6:
        scoped.pop("application", None)
    if stage < 5:
        scoped.pop("databaseAccess", None)
    if stage < 4:
        scoped.pop("privateIngress", None)
        scoped.pop("certificates", None)
    scoped["bootstrapWorkspaceName"] = config["parameters"].get("bootstrap", {}).get("logAnalyticsWorkspaceName") or config["parameters"].get("backup", config["parameters"].get("platform", {})).get("logAnalyticsWorkspaceName")
    scoped["parameters"] = {}
    for component, values in config["parameters"].items():
        if COMPONENTS[component][0] > stage:
            continue
        selected = dict(values)
        if component == "platform" and stage < 5:
            selected.pop("stage5Data", None)
        if component == "platform" and stage < 4:
            selected.pop("azureOpenAIConnections", None)
            selected.pop("stage4RoleAssignmentNaming", None)
        scoped["parameters"][component] = selected
    return fingerprint(scoped)


class MigrationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise MigrationError(message)


def configured(value):
    if isinstance(value, dict):
        return all(configured(item) for item in value.values())
    if isinstance(value, list):
        return all(configured(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip()) and not re.search(r"REPLACE|example\.(com|net|org)|example-workspace|stage3check|<|>", value, re.I)
    return value is not None


def admin_source_cidrs(value):
    require(isinstance(value, list) and 1 <= len(value) <= 32, "Admin access requires one to 32 explicit public source CIDRs")
    networks = []
    for item in value:
        require(isinstance(item, str) and "/" in item, "Admin source entries must use explicit CIDR notation")
        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError:
            raise MigrationError("Invalid Admin source CIDR") from None
        minimum_prefix = 24 if network.version == 4 else 64
        require(network.is_global and network.prefixlen >= minimum_prefix, "Admin source CIDRs must be public and no broader than /24 IPv4 or /64 IPv6")
        networks.append(str(network))
    require(len(set(networks)) == len(networks), "Admin source CIDRs must be unique")
    return sorted(networks)


def availability_zones(config):
    aks = config["parameters"].get("platform", {}).get("stage4Aks", {})
    zones = aks.get("availabilityZones", [])
    require(isinstance(zones, list) and all(isinstance(zone, str) for zone in zones) and set(zones) <= {"1", "2", "3"} and len(set(zones)) == len(zones) and len(zones) != 1, "AKS availability zones must be empty or two to three unique zones from 1, 2 and 3")
    if zones:
        require(all(isinstance(aks.get(key), int) and aks[key] >= len(zones) for key in ("systemNodeCount", "userNodeCount")), "Zonal AKS pools need at least one initial node per availability zone")
    return sorted(zones)


def platform_maintenance(config):
    platform = config["parameters"].get("platform", {})
    aks, data = platform.get("stage4Aks", {}), platform.get("stage5Data", {})
    require(aks.get("upgradeChannel", "patch") in {"none", "patch", "stable", "rapid", "node-image"}, "stage4Aks.upgradeChannel must be none, patch, stable, rapid or node-image")
    require(aks.get("nodeOSUpgradeChannel", "NodeImage") in {"None", "Unmanaged", "NodeImage", "SecurityPatch"}, "stage4Aks.nodeOSUpgradeChannel must be None, Unmanaged, NodeImage or SecurityPatch")
    require(type(aks.get("systemPoolCriticalAddonsOnly", False)) is bool, "stage4Aks.systemPoolCriticalAddonsOnly must be true or false")
    require(data.get("redisHighAvailability", "Enabled") in {"Enabled", "Disabled"}, "stage5Data.redisHighAvailability must be Enabled or Disabled")
    for connection in platform.get("azureOpenAIConnections", []):
        require(connection.get("privateEndpoint", True) in {True, False}, "azureOpenAIConnections privateEndpoint must be true or false")
        if connection.get("privateEndpoint", True) is False:
            # The Firewall allowlist and DNS fallback are derived from the default account host.
            require(connection.get("endpoint", "https://" + connection["accountName"] + ".openai.azure.com").rstrip("/") == "https://" + connection["accountName"] + ".openai.azure.com",
                    "A model connection without a private endpoint must use the default https://<account>.openai.azure.com endpoint")


# The private-aks module labels the user pool workload=litellm; every platform pod template selects it so
# nothing this repo deploys lands on the system pool, which may be tainted CriticalAddonsOnly.
USER_POOL_SELECTOR = {"workload": "litellm"}


def on_user_pool(pod):
    pod["nodeSelector"] = {**pod.get("nodeSelector", {}), **USER_POOL_SELECTOR}
    return pod


def spread_across_zones(pod, zones, selector=None):
    if not zones:
        return pod
    if selector is None:
        selector = next(item for item in pod["topologySpreadConstraints"] if item["topologyKey"] == "kubernetes.io/hostname")["labelSelector"]
    # Soft on purpose: a hard zone rule keeps replacement pods Pending while a failed zone's nodes are still registered.
    pod.setdefault("topologySpreadConstraints", []).append({"maxSkew": 1, "topologyKey": "topology.kubernetes.io/zone", "whenUnsatisfiable": "ScheduleAnyway", "labelSelector": copy.deepcopy(selector)})
    return pod


def front_door_private_link_location(value):
    require(
        isinstance(value, str) and value in FRONT_DOOR_PRIVATE_LINK_LOCATIONS,
        "Azure Front Door Private Link location is unsupported; select a supported managed Private Link region independently from the origin region",
    )
    return value


def validate_config(config, environment):
    require(isinstance(config, dict) and config.get("schemaVersion") == 1, "Unsupported customer configuration")
    mode = deployment_mode(config)
    required_fields = {"schemaVersion", "environment", "azure", "location", "baseDomain", "ownerEmail", "target", "parameters"}
    if mode == "migration":
        required_fields.add("legacy")
    require(set(config) - {"governance", "deploymentMode", "privateIngress", "databaseAccess", "application", "proxy", "entra", "auditRuntime", "contentAudit", "observability", "auditGovernance", "dns", "certificates", "legacyAccess", "l3Audit"} == required_fields, "Unexpected or missing customer configuration fields; greenfield must omit legacy")
    if "l3Audit" in config:
        from scripts.optional_l3 import enabled
        enabled(config)
    native_gateway = config.get("application", {}).get("authentication", {}).get("mode") == "native"
    if native_gateway:
        require("contentAudit" in config, "Native gateway authentication requires the explicit native Spend Logs policy")
        require(not any(key in config for key in ("entra", "proxy", "auditRuntime", "auditGovernance", "observability")), "Native gateway authentication cannot retain Entra proxy, enhanced L3 or proxy-based observability configuration")
    if "legacyAccess" in config:
        from scripts.legacy_access import access_settings
        access_settings(config)
    if "contentAudit" in config:
        from scripts.native_audit import native_audit_settings
        native_audit_settings(config)
    if "certificates" in config:
        from scripts.certificate_runtime import certificate_settings
        certificate_settings(config)
    if "dns" in config:
        from scripts.dns_runtime import dns_settings
        dns_settings(config)
    if "auditGovernance" in config:
        from scripts.audit_governance import governance_settings
        governance_settings(config)
    if "observability" in config:
        from scripts.observability import telemetry_settings
        require("auditRuntime" in config or "contentAudit" in config, "Managed observability requires an explicit audit mode")
        telemetry_settings(config)
    if "auditRuntime" in config:
        from scripts.audit_manifest import audit_settings
        audit_settings(config)
    if "proxy" in config:
        from scripts.proxy_config import proxy_settings
        proxy_settings(config)
    if "entra" in config:
        from scripts.entra_apps import entra_settings
        entra_settings(config)
    if "application" in config:
        from scripts.backend_manifest import application_authentication, application_settings
        application_settings(config)
        application_authentication(config)
    if "databaseAccess" in config:
        access = config["databaseAccess"]
        require(isinstance(access, dict) and set(access) == {"migrationPrincipalId"} and isinstance(access["migrationPrincipalId"], str) and UUID(access["migrationPrincipalId"]).int != 0, "databaseAccess requires a nonzero migration service principal Object ID")
    if "privateIngress" in config:
        from scripts.private_ingress import ingress_settings
        ingress_settings(config)
    approval_policy(config)
    require(environment in {"dev", "test", "prod"} and config["environment"] == environment, "Environment mismatch")
    require(set(config["azure"]) == {"tenantId", "subscriptionId"}, "Invalid Azure scope fields")
    require(all(UUID(value).int for value in config["azure"].values()), "Nonzero Azure scope identifiers are required")
    require(configured({key: config[key] for key in ("location", "baseDomain", "ownerEmail", "target")}), "Replace customer configuration placeholders")
    domain_hosts(config["baseDomain"])
    require(re.fullmatch(r"[a-z0-9]+", config["location"]) is not None, "Invalid location")
    require(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", config["ownerEmail"]) is not None, "Invalid ownerEmail")
    require(set(config["target"]) == {"resourceGroup"}, "Invalid target fields")
    groups = [config["target"]["resourceGroup"]]
    if mode == "migration":
        require(isinstance(config["legacy"], dict) and set(config["legacy"]) == {"resourceGroup", "aksClusterName", "namespace", "postgresPvc"}, "Invalid legacy inventory fields")
        require(configured(config["legacy"]), "Replace legacy inventory placeholders")
        require(config["legacy"]["resourceGroup"].lower() != config["target"]["resourceGroup"].lower(), "Target must use a separate resource group; in-place migration is not supported")
        groups.append(config["legacy"]["resourceGroup"])
    for group in groups:
        require(re.fullmatch(r"[a-zA-Z0-9_().-]{1,90}", group) is not None and not group.endswith("."), "Invalid resource group name")
    require(isinstance(config["parameters"], dict) and not set(config["parameters"]) - COMPONENTS.keys(), "Unknown parameter component")
    require(not {"network", "backup"}.issubset(config["parameters"]), "Choose one bootstrap network owner: network or backup, never both")
    for component, parameters in config["parameters"].items():
        require(isinstance(parameters, dict) and not set(parameters) - COMPONENTS[component][2], "Unknown or reserved component parameter")
        require(mode != "greenfield" or component not in {"monitoring", "legacy-logging"}, "Legacy monitoring components do not apply to greenfield")
    availability_zones(config)
    platform_maintenance(config)
    from scripts.hub_network import validate_hub
    validate_hub(config)
    edge = config["parameters"].get("edge")
    if edge is not None and "adminAllowedCidrs" in edge and configured(edge["adminAllowedCidrs"]):
        admin_source_cidrs(edge["adminAllowedCidrs"])
    if edge is not None:
        from scripts.app_gateway import validate_edge_type
        validate_edge_type(config)
    if edge is not None and edge.get("type", "frontDoor") == "frontDoor":
        for origin_name in ("privateOrigin", "adminPrivateOrigin"):
            origin = edge.get(origin_name)
            if isinstance(origin, dict) and configured(origin.get("privateLinkLocation")):
                front_door_private_link_location(origin["privateLinkLocation"])
    certificate_vault = config["parameters"].get("certificate-vault")
    if certificate_vault is not None and (configured(certificate_vault) or "privateIngress" in config):
        from scripts.certificate_vault import certificate_vault_parameters
        certificate_vault_parameters(config)
    target_connectivity = config["parameters"].get("runner-target-connectivity")
    if target_connectivity is not None and configured(target_connectivity):
        from scripts.runner_target_connectivity import target_connectivity_settings
        target_connectivity_settings(config)
    network = config["parameters"].get("network")
    if network is not None:
        require(REQUIRED["network"].issubset(network), "Network bootstrap configuration is incomplete")
        if configured(network):
            address = ipaddress.ip_network(network["virtualNetworkAddressPrefix"])
            endpoint = ipaddress.ip_network(network["privateEndpointSubnetPrefix"])
            require(address.version == endpoint.version == 4 and endpoint.subnet_of(address), "Private Endpoint subnet must be inside the IPv4 target VNet")
        platform_network = config["parameters"].get("platform", {}).get("stage4Network", {})
        for key in ("virtualNetworkName", "privateEndpointSubnetName"):
            require(key not in platform_network or platform_network[key] == network[key], "Platform network names must match network bootstrap")
    return config


def validate_evidence(evidence, stage, config, revision, now=None):
    require(stage == 10 or stage in active_stages(config), "Stage does not apply to the selected deployment mode")
    require(re.fullmatch(r"[0-9a-f]{40}", revision or "") is not None, "A full reviewed Git revision is required")
    require(isinstance(evidence, list), "Evidence must be an array")
    now = now or datetime.now(timezone.utc)
    mode, eligible = approval_policy(config)
    required_approvers = 1 if mode == "single-operator" else 2
    completed = set()
    for item in evidence:
        require(isinstance(item, dict), "Invalid evidence record")
        previous = item.get("stage")
        require(type(previous) is int and 0 <= previous <= 9 and previous not in completed, "Invalid or duplicate evidence stage")
        binding = item.get("binding", "full-config")
        require(binding in {"full-config", "stage-config"}, "Invalid evidence binding")
        expected_hash = stage_fingerprint(config, previous) if binding == "stage-config" else fingerprint(config)
        evidence_revision = item.get("revision", "")
        revision_matches = re.fullmatch(r"[0-9a-f]{40}", evidence_revision) is not None and (mode == "single-operator" or evidence_revision == revision)
        require(item.get("environment") == config["environment"] and item.get("configSha256") == expected_hash and revision_matches, "Evidence configuration or revision mismatch")
        require(item.get("approvalMode", "dual") == mode, "Evidence approval policy mismatch")
        require(item.get("status") == "passed" and set(item.get("checks", [])) == set(stage_checks(previous, config)), "Stage checks are incomplete")
        approvers = item.get("approvedBy", [])
        require(isinstance(approvers, list) and len(approvers) == required_approvers, "Approval count does not match configured policy")
        try:
            identities = {UUID(value) for value in approvers}
            observed = datetime.fromisoformat(item["observedAt"].replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError):
            raise ValueError("Invalid evidence approval or timestamp") from None
        require(len(identities) == required_approvers and all(identity.int for identity in identities), "Distinct nonzero approver object IDs are required")
        require(not eligible or identities.issubset(eligible), "Evidence contains an ineligible approver")
        require(observed.tzinfo is not None and now - timedelta(days=7) <= observed <= now, "Evidence must be from the last seven days")
        report = urlsplit(item.get("reportUrl", ""))
        require(report.scheme == "https" and bool(report.hostname) and not report.username and not report.password and not report.query, "Report must be a private HTTPS reference without embedded credentials or query tokens")
        require(re.fullmatch(r"[0-9a-f]{64}", item.get("reportSha256", "")) is not None, "Report hash is required")
        completed.add(previous)
    require({previous for previous in active_stages(config) if previous < stage}.issubset(completed), "Prior stage evidence is missing; do not skip migration gates")
    return completed


def parameters_for(config, stage, component):
    require(stage in active_stages(config), "Stage does not apply to the selected deployment mode")
    require(component in COMPONENTS, "Select an IaC component")
    first_stage, template, _allowed = COMPONENTS[component]
    require(stage in ({3, 4, 5} if component == "platform" else {first_stage}), "Component does not belong to the selected stage")
    parameters = dict(config["parameters"].get(component, {}))
    if component == "platform" and stage < 5:
        parameters.pop("stage5Data", None)
    if component == "platform" and stage < 4:
        parameters.pop("azureOpenAIConnections", None)
        parameters.pop("stage4RoleAssignmentNaming", None)
    app_gateway = component == "edge" and parameters.get("type") == "applicationGateway"
    if app_gateway:
        from scripts.app_gateway import APP_GATEWAY_REQUIRED
    required = APP_GATEWAY_REQUIRED if app_gateway else REQUIRED[component]
    require(required.issubset(parameters) and configured(parameters), "Component configuration is missing or contains placeholders")
    if component == "bootstrap":
        logging = config["parameters"].get("backup", config["parameters"].get("platform", {}))
        workspace_name = parameters.get("logAnalyticsWorkspaceName") or logging.get("logAnalyticsWorkspaceName", "")
        require(configured(workspace_name), "Target Log Analytics workspace name is required for bootstrap")
        parameters.update(resourceGroupName=config["target"]["resourceGroup"], logAnalyticsWorkspaceName=workspace_name)
    elif component == "legacy-logging":
        workspace_name = config["parameters"].get("monitoring", {}).get("logAnalyticsWorkspaceName", "")
        require(configured(workspace_name), "Legacy monitoring workspace name is required")
        parameters["logAnalyticsWorkspaceName"] = workspace_name
    elif component == "platform":
        require(parameters.get("stage4RoleAssignmentNaming", "principal-id") in {"principal-id", "resource-id"}, "Invalid Stage 4 role assignment naming mode")
        from scripts.hub_network import egress_mode, platform_hub_parameters
        # Deployment-time value added by migration_deploy from the stage0-hub receipt, not a customer setting.
        resolved_hub = parameters.pop("stage4Hub", None)
        hub_egress = egress_mode(config) == "hub"
        require(resolved_hub is None or hub_egress, "Only hub egress takes resolved hub parameters")
        if stage >= 4 and hub_egress:
            require(resolved_hub is not None, "Hub egress reads the hub firewall from the stage0-hub deployment receipt; render it through the platform plan")
            parameters["stage4Hub"] = platform_hub_parameters(config, resolved_hub)
        if stage >= 5:
            require("stage5Data" in parameters, "Stage 5 database configuration is required")
            runner_dns = config["parameters"].get("runner-target-connectivity", {})
            runner_vnet = runner_dns.get("runnerVirtualNetworkId", "") if isinstance(runner_dns, dict) else ""
            manage_runner_dns = runner_dns.get("manageDnsLink", True) if isinstance(runner_dns, dict) and runner_vnet else False
            parameters.update(stage5RunnerVirtualNetworkId=runner_vnet, manageStage5RunnerDnsLinks=manage_runner_dns)
            if "certificate-vault" in config["parameters"]:
                require(not parameters.get("createStage5KeyVaultPrivateDnsZone", False), "certificate-vault owns Key Vault DNS; set createStage5KeyVaultPrivateDnsZone=false")
                parameters.update(createStage5KeyVaultPrivateDnsZone=False, configureStage5KeyVaultDnsLink=False)
            if "databaseAccess" in config:
                parameters["bootstrapPrincipalId"] = config["databaseAccess"]["migrationPrincipalId"]
        if deployment_mode(config) == "migration":
            require(parameters["stage4Aks"]["name"] != config["legacy"]["aksClusterName"], "New AKS must not reuse the legacy name")
        parameters.update(environmentName=config["environment"], deployContainerRegistry=True, deployStage4=stage >= 4, deployStage5=stage >= 5, containerRegistryPublicNetworkAccess="Disabled")
    elif component == "backup":
        parameters["backupOwnerUpn"] = config["ownerEmail"]
    elif component == "hub":
        from scripts.hub_network import hub_settings
        parameters = hub_settings(config)
    elif component == "aks-ingress-role":
        platform = config["parameters"]["platform"]
        network = platform["stage4Network"]
        parameters.update(
            aksClusterName=platform["stage4Aks"]["name"],
            virtualNetworkName=network["virtualNetworkName"],
        )
    elif component == "runner-connectivity":
        from scripts.runner_connectivity import connectivity_parameters
        parameters = connectivity_parameters(config)
    elif component == "certificate-vault":
        from scripts.certificate_vault import certificate_vault_parameters
        parameters = certificate_vault_parameters(config)
    elif component == "runner-target-connectivity":
        from scripts.runner_target_connectivity import target_connectivity_settings
        settings = target_connectivity_settings(config)
        parameters = {key: settings[key] for key in ("runnerVirtualNetworkId", "manageDnsLink")}
    elif component == "monitoring":
        require(config["legacy"]["namespace"] == "litellm" and config["legacy"]["postgresPvc"] == "pg-data", "Monitoring queries currently require litellm namespace and pg-data PVC; adapt and test queries first")
        parameters.update(aksClusterName=config["legacy"]["aksClusterName"], ownerEmail=config["ownerEmail"])
    elif component == "audit-foundation":
        audit = config["parameters"].get("audit", {})
        keys = ("cmkVaultName", "cmkKeyName", "virtualNetworkName", "privateEndpointSubnetName")
        require(all(configured(audit.get(key, "")) for key in keys), "Audit Vault/key and network names must be configured")
        parameters.update({key: audit[key] for key in keys})
        parameters["aksClusterName"] = config["parameters"]["platform"]["stage4Aks"]["name"]
    elif component == "observability":
        platform = config["parameters"]["platform"]
        network = platform["stage4Network"]
        parameters.update(aksClusterName=platform["stage4Aks"]["name"], logAnalyticsWorkspaceName=platform["logAnalyticsWorkspaceName"], virtualNetworkName=network["virtualNetworkName"], privateEndpointSubnetName=network["privateEndpointSubnetName"])
    elif component == "proxy-foundation":
        platform = config["parameters"]["platform"]
        require("databaseAccess" in config, "Proxy foundation requires an explicitly approved bootstrap identity")
        network = platform["stage4Network"]
        parameters.update(environmentName=config["environment"], aksClusterName=platform["stage4Aks"]["name"], virtualNetworkName=network["virtualNetworkName"], privateEndpointSubnetName=network["privateEndpointSubnetName"], logAnalyticsWorkspaceName=platform["logAnalyticsWorkspaceName"], bootstrapPrincipalId=config["databaseAccess"]["migrationPrincipalId"])
        if "entra" in config:
            parameters["entraBootstrapPrincipalId"] = config["entra"]["bootstrapPrincipalId"]
    elif component == "audit":
        parameters["deployAuditStorage"] = True
    elif component == "origin":
        parameters["deployPrivateOrigin"] = True
    elif app_gateway:
        from scripts.app_gateway import template_parameters
        # Deployment-time values added by migration_deploy.resolve_origin, not customer settings.
        resolved = {key: parameters[key] for key in ("backendAddresses", "networkIsolation", "deployGateway") if key in parameters}
        unresolved = copy.deepcopy(config)
        for key in resolved:
            unresolved["parameters"]["edge"].pop(key)
        parameters = {**template_parameters(unresolved), **resolved}
        template = "edge-appgw"
        parameters.update(deployEdge=True, enableApiTraffic=False, enableAdminTraffic=False, environmentName=config["environment"], baseDomain=config["baseDomain"], wafMode="Detection", location=config["location"])
    elif component == "edge":
        parameters.pop("type", None)
        parameters["adminAllowedCidrs"] = admin_source_cidrs(parameters["adminAllowedCidrs"])
        for origin_name in ("privateOrigin", "adminPrivateOrigin"):
            parameters[origin_name]["privateLinkLocation"] = front_door_private_link_location(parameters[origin_name]["privateLinkLocation"])
        parameters.update(deployEdge=True, enableApiTraffic=False, enableAdminTraffic=False, environmentName=config["environment"], baseDomain=config["baseDomain"], wafMode="Detection")
    if component not in {"edge", "aks-ingress-role"}:
        parameters["location"] = config["location"]
    if component != "aks-ingress-role":
        parameters["tags"] = {"owner": config["ownerEmail"], "environment": config["environment"], "workload": "litellm", "managedBy": "bicep"}
    return ROOT / f"infra/{template}/main.bicep", {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {key: {"value": value} for key, value in parameters.items()},
    }


def private_write(path, content):
    require(not path.is_symlink(), "Refusing symlink output")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def prepare(config, stage, component, destination):
    destination = destination.resolve()
    require(destination.is_relative_to((ROOT / "temp").resolve()) and destination != (ROOT / "temp").resolve(), "Output must stay under ignored temp/")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    template, parameters = parameters_for(config, stage, component)
    path = destination / "parameters.json"
    private_write(path, json.dumps(parameters, indent=2) + "\n")
    return template, path


def preview(config, template, path, component):
    require(component != "runner-target-connectivity", "Use Customer infrastructure deployment plan to discover and review existing AKS DNS resources")
    group = config["legacy" if component in {"monitoring", "legacy-logging"} else "target"]["resourceGroup"]
    scope = ["sub", "what-if", "--location", config["location"]] if component in SUBSCRIPTION_COMPONENTS else ["group", "what-if", "--resource-group", group]
    result = subprocess.run(["az", "deployment", *scope, "--subscription", config["azure"]["subscriptionId"], "--template-file", str(template), "--parameters", f"@{path}", "--result-format", "FullResourcePayloads", "--no-pretty-print", "--output", "json"], capture_output=True, text=True, check=False)
    private_write(path.parent / "what-if.json", result.stdout)
    private_write(path.parent / "diagnostics.txt", result.stderr)
    require(result.returncode == 0, f"Azure what-if failed: {command_failure_summary(result.stdout, result.stderr, result.returncode)}")
    result = json.loads(result.stdout)
    require(result.get("status") == "Succeeded" and isinstance(result.get("changes"), list), "Unrecognized What-if response")
    counts = {}
    for change in result["changes"]:
        kind = change.get("changeType", "Unknown")
        counts[kind] = counts.get(kind, 0) + 1
    print(json.dumps({"changeCounts": counts, "deploymentPerformed": False}))
    require(not set(counts) - {"Create", "NoChange", "Ignore"}, "Review required: Modify/Delete/Unsupported/unknown changes block this gate; no deployment performed")
    require(counts.get("Create", 0) > 0, "No creation detected; a disabled or already-deployed preview is not a new-stage deployment approval")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, choices=range(10), required=True)
    parser.add_argument("--mode", choices=("guide", "config-check", "preflight", "what-if"), default="guide")
    parser.add_argument("--component", choices=tuple(COMPONENTS) + ("none",), default="none")
    parser.add_argument("--environment", choices=("dev", "test", "prod"), required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "temp/customer-migration")
    args = parser.parse_args()
    supplied = os.environ.get("CUSTOMER_CONFIG_JSON", "")
    config = json.loads(supplied) if supplied else {}
    title, checks = stage_title(args.stage, config), stage_checks(args.stage, config)
    print(f"Stage {args.stage}: {title}\nRequired exit evidence: {', '.join(checks)}")
    print(f"Deployment mode: {deployment_mode(config)}; applicable stages: {', '.join(map(str, active_stages(config)))}")
    print("Runbook: docs/customer-migration-guide-zh.md; no automatic deployment, DNS change or retirement.")
    if args.mode == "guide":
        return
    config = validate_config(config, args.environment)
    require(os.environ.get("AZURE_TENANT_ID") == config["azure"]["tenantId"] and os.environ.get("AZURE_SUBSCRIPTION_ID") == config["azure"]["subscriptionId"], "OIDC scope does not match customer configuration")
    print(json.dumps({"configSha256": fingerprint(config), "stageConfigSha256": stage_fingerprint(config, args.stage), "stage": args.stage, "environment": args.environment}))
    if args.mode == "config-check":
        if args.component != "none":
            parameters_for(config, args.stage, args.component)
        print("Configuration syntax checked only; evidence, cloud state and deployment readiness were not checked.")
        return
    revision = os.environ.get("GITHUB_SHA", os.environ.get("MIGRATION_REVISION", ""))
    if os.environ.get("MIGRATION_AUTO_EVIDENCE") == "true":
        from scripts.workflow_artifacts import load_evidence
        previous = load_evidence(config, args.stage, revision)
    else:
        previous = json.loads(os.environ.get("MIGRATION_EVIDENCE_JSON") or "[]")
    completed = validate_evidence(previous, args.stage, config, revision)
    require(not (args.component in {"backup", "network"} and any(stage >= 4 for stage in completed)), "Do not reapply the bootstrap VNet after Stage 4")
    if args.component != "none":
        template, path = prepare(config, args.stage, args.component, args.output_dir)
        if args.mode == "what-if":
            preview(config, template, path, args.component)
    else:
        require(args.mode != "what-if", "What-if needs an IaC component")
        if args.stage in (7, 8, 9):
            render(config["baseDomain"], args.output_dir, args.stage)
            print("Domain overlay generated; identity, image and secret placeholders still require integration. Not deployable as-is.")
    print("Readiness metadata checked, not an independent audit of evidence or cloud acceptance.")


if __name__ == "__main__":
    try:
        main()
    except MigrationError as error:
        raise SystemExit(diagnostic_exit(error, "customer-migration")) from None
    except Exception as error:
        raise SystemExit(diagnostic_exit(error, "customer-migration", "Migration check failed")) from None