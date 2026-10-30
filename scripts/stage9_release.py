#!/usr/bin/env python3
"""Validate release attestations and render preview-only Stage 9 artifacts."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re

import yaml

try:
    from scripts.render_stage7_domain import ROOT, domain_hosts, render
    from scripts.customer_migration import admin_source_cidrs, front_door_private_link_location
    from scripts.api_protocol import MODEL_LIST_RATE_LIMIT
except ModuleNotFoundError:
    from render_stage7_domain import ROOT, domain_hosts, render
    from customer_migration import admin_source_cidrs, front_door_private_link_location
    from api_protocol import MODEL_LIST_RATE_LIMIT

PREPARE_CHECKS = {
    "private_origin_tls", "origin_bypass_denied", "admin_source_ip_allowlist",
    "admin_private_origin_isolation",
    "waf_diagnostics_privacy", "private_link_approval", "rollback_plan",
}
CANARY_CHECKS = PREPARE_CHECKS | {
    "entra_backend_acl", "stage8_capture_architecture", "l3_governance_and_recovery",
    "required_protocol_matrix", "database_restore", "telemetry_alerts",
    "waf_detection_review", "approved_pilot_clients",
}
PRODUCTION_CHECKS = CANARY_CHECKS | {"canary_slo", "waf_prevention_review", "dns_cutover_and_rollback"}
LIVE_GATED_CANARY_ENVIRONMENTS = {"dev", "test"}
PLS_ID = re.compile(r"^/subscriptions/[a-fA-F0-9-]{36}/resourceGroups/[A-Za-z0-9_.()-]+/providers/Microsoft.Network/privateLinkServices/[A-Za-z0-9_.-]+$")
UUID = re.compile(r"^[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$")


def release_checks(config):
    phase = config.get("phase")
    if phase not in {"prepare", "canary", "production"}:
        raise ValueError("phase must be prepare, canary or production")
    mode = config.get("auditMode", "l3")
    if "optionalL3Enabled" in config and type(config["optionalL3Enabled"]) is not bool:
        raise ValueError("optionalL3Enabled must be a boolean")
    if "optionalL3AlertsEnabled" in config and type(config["optionalL3AlertsEnabled"]) is not bool:
        raise ValueError("optionalL3AlertsEnabled must be a boolean")
    if mode not in {"l3", "native"}:
        raise ValueError("Release auditMode must be native or l3")
    authentication = config.get("authenticationMode", "entra")
    if authentication not in {"entra", "native"}:
        raise ValueError("Release authenticationMode must be entra or native")
    if mode == "native" and type(config.get("telemetryEnabled")) is not bool:
        raise ValueError("Native release requires an explicit telemetry decision")
    if phase == "canary" and config.get("environmentName") in LIVE_GATED_CANARY_ENVIRONMENTS and authentication == "native" and mode == "native":
        return set()
    required = set({"prepare": PREPARE_CHECKS, "canary": CANARY_CHECKS, "production": PRODUCTION_CHECKS}[phase])
    if phase != "prepare" and authentication == "native":
        required.remove("entra_backend_acl")
        required.update({"native_virtual_key_acl", "native_admin_password_login"})
    if mode == "native":
        if phase != "prepare":
            required.difference_update({"stage8_capture_architecture", "l3_governance_and_recovery"})
            required.update({"native_spend_logs", "native_audit_access", "native_retention_recovery"})
            if not config["telemetryEnabled"]:
                required.remove("telemetry_alerts")
                required.add("telemetry_disabled")
    if config.get("optionalL3Enabled") is True and phase != "prepare":
        required.update({"l3_protocol_capture", "l3_delivery_recovery", "l3_query_hold"})
        # Like telemetry: deployed alerts need tested notification evidence; an approved opt-out needs a decision record.
        required.add("l3_alerts" if config.get("optionalL3AlertsEnabled", True) else "l3_alerts_disabled")
    if config.get("edgeType", "frontDoor") == "applicationGateway":
        required.difference_update({"private_link_approval", "admin_private_origin_isolation"})
        required.update({"app_gateway_backend_health", "admin_listener_isolation"})
    return required


def validate_release(config: dict, now: datetime | None = None, required_approvers: int = 2, eligible_approvers=None) -> None:
    now = now or datetime.now(timezone.utc)
    phase = config.get("phase")
    if phase not in {"prepare", "canary", "production"}:
        raise ValueError("phase must be prepare, canary or production")
    domain_hosts(config["baseDomain"])
    if config.get("authenticationMode", "entra") not in {"entra", "native"}:
        raise ValueError("Release authenticationMode must be entra or native")
    if config.get("environmentName") not in {"dev", "test", "prod"}:
        raise ValueError("Invalid environment")
    edge_type = config.get("edgeType", "frontDoor")
    if edge_type not in {"frontDoor", "applicationGateway"}:
        raise ValueError("Release edgeType must be frontDoor or applicationGateway")
    if edge_type == "applicationGateway":
        if {"privateOrigin", "adminPrivateOrigin", "frontDoorId"} & set(config):
            raise ValueError("Application Gateway releases have no Private Link origins or Front Door ID")
    else:
        origins = {"API": config.get("privateOrigin", {}), "Admin": config.get("adminPrivateOrigin", {})}
        for plane, origin in origins.items():
            if not PLS_ID.fullmatch(origin.get("privateLinkServiceId", "")):
                raise ValueError(f"An explicit {plane} Private Link Service resource ID is required")
            front_door_private_link_location(origin.get("privateLinkLocation"))
        if origins["API"]["privateLinkServiceId"].lower() == origins["Admin"]["privateLinkServiceId"].lower():
            raise ValueError("API and Admin releases require separate Private Link Services")
    admin_source_cidrs(config.get("adminAllowedCidrs"))
    if not config.get("logAnalyticsWorkspaceName") or "REPLACE" in config["logAnalyticsWorkspaceName"]:
        raise ValueError("An explicit diagnostics workspace is required")
    if type(config.get("rateLimitPerMinute")) is not int or not 1 <= config["rateLimitPerMinute"] <= 100000:
        raise ValueError("Invalid rate limit")
    if type(config.get("adminRateLimitPerMinute")) is not int or not 1 <= config["adminRateLimitPerMinute"] <= 10000:
        raise ValueError("Invalid Admin rate limit")
    model_list = config.get("modelListRateLimitPerMinute", MODEL_LIST_RATE_LIMIT)
    if type(model_list) is not int or not 1 <= model_list <= 10000:
        raise ValueError("Invalid model-list rate limit")
    if config.get("wafMode") != ("Prevention" if phase == "production" else "Detection"):
        raise ValueError("Prepare/canary use Detection; production requires reviewed Prevention")
    identity = "edgeId" if edge_type == "applicationGateway" else "frontDoorId"
    if phase != "prepare" and not UUID.fullmatch(config.get(identity, "")):
        raise ValueError("Traffic requires the actual Application Gateway edge ID" if identity == "edgeId" else "Traffic requires the actual Front Door ID")
    if not config.get("changeTicket") or "REPLACE" in config["changeTicket"]:
        raise ValueError("Approved change ticket required")
    owners = config.get("approvedBy", [])
    if len(owners) < required_approvers or len(set(owners)) != len(owners) or any(not UUID.fullmatch(owner) for owner in owners):
        raise ValueError("Distinct approval owners matching the release policy are required")
    if eligible_approvers and not {owner.lower() for owner in owners}.issubset({str(owner).lower() for owner in eligible_approvers}):
        raise ValueError("Release contains an ineligible approver")
    required = release_checks(config)
    for name in sorted(required):
        evidence = config.get("checks", {}).get(name, {})
        if evidence.get("passed") is not True or not evidence.get("report") or "REPLACE" in evidence["report"]:
            raise ValueError(f"Missing passing evidence: {name}")
        observed = datetime.fromisoformat(evidence.get("observedAt", "").replace("Z", "+00:00"))
        if observed.tzinfo is None or not now - timedelta(days=7) <= observed <= now:
            raise ValueError(f"Expired or invalid evidence timestamp: {name}")


def validate_origin_snapshot(load_balancer: dict, frontend_id: str, subnet: dict) -> None:
    if load_balancer.get("sku", {}).get("name") != "Standard":
        raise ValueError("Private Link requires a Standard load balancer")
    frontends = load_balancer.get("properties", {}).get("frontendIPConfigurations", [])
    selected = next((item for item in frontends if item.get("id", "").lower() == frontend_id.lower()), None)
    if selected is None:
        raise ValueError("Selected Private Link frontend is missing from the load balancer snapshot")
    properties = selected.get("properties", {})
    try:
        address = ipaddress.ip_address(properties.get("privateIPAddress", ""))
    except ValueError:
        raise ValueError("Selected Private Link frontend must have a valid private IP address") from None
    if properties.get("publicIPAddress") or not address.is_private or address.is_loopback or address.is_link_local or not properties.get("subnet", {}).get("id"):
        raise ValueError("Private Link frontend must have a private address and subnet, with no public IP")
    if subnet.get("properties", {}).get("privateLinkServiceNetworkPolicies") != "Disabled":
        raise ValueError("PLS NAT subnet requires privateLinkServiceNetworkPolicies=Disabled")


def validate_what_if(result: dict) -> dict[str, int]:
    if result.get("status") != "Succeeded" or not isinstance(result.get("changes"), list):
        raise ValueError("What-if did not complete successfully")
    counts = {}
    for change in result["changes"]:
        kind = change.get("changeType")
        counts[kind] = counts.get(kind, 0) + 1
        if kind not in {"Create", "Ignore", "NoChange"}:
            raise ValueError(f"What-if requires review: {kind}; Modify/Delete/unsupported analysis cannot pass automatically")
    return counts


def generate(config: dict, output_dir: Path) -> None:
    if config.get("edgeType", "frontDoor") != "frontDoor":
        raise ValueError("Stage9 preview rendering is not supported for applicationGateway edge yet")
    validate_release(config)
    if config.get("auditMode", "l3") != "l3" or config.get("authenticationMode", "entra") != "entra":
        raise ValueError("Native release uses managed customer-runtime manifests, not the legacy Stage9 overlay")
    render(config["baseDomain"], output_dir, stage=9)
    overlay_path = output_dir / "kustomization.yaml"
    overlay = yaml.safe_load(overlay_path.read_text())
    if UUID.fullmatch(config.get("frontDoorId", "")):
        for plane in ("api", "admin"):
            overlay["patches"].append({"patch": yaml.safe_dump({
                "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": f"llm-{plane}-proxy"},
                "spec": {"template": {"spec": {"containers": [{"name": "auth-proxy", "env": [{"name": "FRONT_DOOR_ID", "value": config["frontDoorId"]}]}]}}},
            })})
    overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")
    parameters = {name: {"value": config[name]} for name in ("environmentName", "privateOrigin", "adminPrivateOrigin", "adminAllowedCidrs", "logAnalyticsWorkspaceName", "wafMode", "rateLimitPerMinute", "adminRateLimitPerMinute")}
    parameters["modelListRateLimitPerMinute"] = {"value": config.get("modelListRateLimitPerMinute", MODEL_LIST_RATE_LIMIT)}
    parameters.update({"baseDomain": {"value": domain_hosts(config["baseDomain"])["api"].removeprefix("llm-api.")}, "deployEdge": {"value": True}, "enableApiTraffic": {"value": config["phase"] != "prepare"}, "enableAdminTraffic": {"value": config["phase"] != "prepare"}})
    (output_dir / "edge.parameters.json").write_text(json.dumps({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#", "contentVersion": "1.0.0.0", "parameters": parameters}, indent=2) + "\n", encoding="utf-8")
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    (output_dir / "release-summary.json").write_text(json.dumps({"phase": config["phase"], "configurationSha256": fingerprint, "attestationsChecked": True, "deploymentPerformed": False}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    release = commands.add_parser("render")
    release.add_argument("--config", type=Path, required=True)
    release.add_argument("--output-dir", type=Path, default=ROOT / "temp/stage9-release")
    what_if = commands.add_parser("check-what-if")
    what_if.add_argument("result", type=Path)
    origin = commands.add_parser("check-origin")
    origin.add_argument("--load-balancer", type=Path, required=True)
    origin.add_argument("--frontend-id", required=True)
    origin.add_argument("--subnet", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "render":
        generate(json.loads(args.config.read_text()), args.output_dir)
        print("Stage 9 preview artifacts generated. Attestations are not independent proof; no deployment was performed.")
    elif args.command == "check-origin":
        validate_origin_snapshot(json.loads(args.load_balancer.read_text()), args.frontend_id, json.loads(args.subnet.read_text()))
        print("Origin ARM snapshots meet private frontend requirements; controller ownership still requires review.")
    else:
        print(json.dumps(validate_what_if(json.loads(args.result.read_text()))))