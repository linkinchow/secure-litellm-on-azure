"""Explicit native Spend Logs policy for the managed Stage8 publication."""

import copy
import hashlib
import json

import yaml

from scripts.customer_migration import fingerprint, require


def native_audit_settings(config):
    settings = config.get("contentAudit")
    require(isinstance(settings, dict) and set(settings) == {"mode", "retentionDays", "contentPolicyAccepted"}, "contentAudit requires mode, retentionDays and contentPolicyAccepted")
    require(settings["mode"] == "native", "Only native contentAudit mode is currently supported; use auditRuntime for enhanced L3")
    require(type(settings["retentionDays"]) is int and 1 <= settings["retentionDays"] <= 30, "Native audit retention must be 1 to 30 days")
    require(settings["contentPolicyAccepted"] is True, "Approve native content, retention, backup and delivery limits before publishing")
    require("auditRuntime" not in config and "auditGovernance" not in config, "Native audit cannot be combined with enhanced L3 configuration")
    require(not any(binding.get("auditTeamId") or binding.get("role") == "audit_reader" for binding in config.get("proxy", {}).get("bindings", [])), "Existing L3 bindings require a separately approved migration; do not silently disable them")
    return settings


def render_native_audit(config, source):
    settings = native_audit_settings(config)
    from scripts.backend_manifest import application_authentication
    native_gateway = application_authentication(config)["mode"] == "native"
    require("application" in config and (native_gateway or "proxy" in config), "Native publishing requires a managed backend and an approved authentication path")
    documents = copy.deepcopy(source)
    candidates = [item for item in documents if item["kind"] == "ConfigMap" and "config.yaml" in item.get("data", {})]
    require(len(candidates) == 1, "Native audit requires exactly one managed backend configuration")
    config_map = candidates[0]
    runtime = yaml.safe_load(config_map["data"]["config.yaml"])
    require(not runtime.get("litellm_settings", {}).get("callbacks") and not runtime.get("litellm_settings", {}).get("success_callback"), "Review content callbacks before enabling native audit")
    from scripts.optional_l3 import enabled
    runtime["general_settings"].update(disable_spend_logs=False, store_prompts_in_spend_logs=not enabled(config),
                                       maximum_spend_logs_retention_period=f"{settings['retentionDays']}d",
                                       maximum_spend_logs_retention_interval="1d")
    content = yaml.safe_dump(runtime, sort_keys=False)
    previous_name = config_map["metadata"]["name"]
    name = "litellm-config-" + hashlib.sha256(content.encode()).hexdigest()[:12]
    config_map["metadata"]["name"] = name
    config_map["data"]["config.yaml"] = content
    for item in documents:
        if item["kind"] == "Deployment" and item["metadata"]["name"] == "litellm":
            for volume in item["spec"]["template"]["spec"].get("volumes", []):
                if volume.get("configMap", {}).get("name") == previous_name:
                    volume["configMap"]["name"] = name
    return documents


def render_native_services(config, source):
    native_audit_settings(config)
    documents = copy.deepcopy(source)
    for plane in ("api", "admin"):
        settings = {"l3": {"enabled": False}, "telemetry": {"enabled": False}, "guardrail": {"enabled": False}}
        name = "stage8-" + plane + "-" + fingerprint(settings)[:12]
        documents.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": "litellm"}, "data": {"config.json": json.dumps(settings, sort_keys=True)}})
        matches = [item for item in documents if item["kind"] == "Deployment" and item["metadata"]["name"] == "llm-" + plane + "-proxy"]
        require(len(matches) == 1, "Native audit requires both managed proxy deployments")
        pod = matches[0]["spec"]["template"]["spec"]
        require(not any(item["name"] == "stage8" for item in pod.get("volumes", [])), "Native input already contains Stage8 configuration")
        container = next(item for item in pod["containers"] if item["name"] == "auth-proxy")
        pod.setdefault("volumes", []).append({"name": "stage8", "configMap": {"name": name}})
        container.setdefault("env", []).append({"name": "STAGE8_CONFIG", "value": "/etc/stage8/config.json"})
        container.setdefault("volumeMounts", []).append({"name": "stage8", "mountPath": "/etc/stage8", "readOnly": True})
    documents.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "stage8-proxy-egress", "namespace": "litellm"},
                      "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "entra-auth-proxy"}}, "policyTypes": ["Egress"], "egress": []}})
    return documents


def prepare_native_audit(config, source, directory, azure, kube, image_public_key=None, revision=None):
    from scripts.audit_runtime import AuditCluster

    native_audit_settings(config)
    client = AuditCluster(kube, directory)
    from scripts.backend_manifest import application_authentication
    if application_authentication(config)["mode"] == "native":
        require("observability" not in config, "Native gateway authentication does not yet support the proxy-based observability collector")
        require(client.get("deployment", "llm-api-proxy", optional=True) is None and client.get("deployment", "llm-admin-proxy", optional=True) is None, "Remove or explicitly migrate existing Entra proxy workloads before native gateway publication")
        return render_native_audit(config, source)
    for plane in ("api", "admin"):
        existing = client.get("deployment", "llm-" + plane + "-proxy", optional=True)
        if existing:
            for volume in existing["spec"]["template"]["spec"].get("volumes", []):
                if volume.get("name") == "stage8" and "configMap" in volume:
                    current = json.loads(client.get("configmap", volume["configMap"]["name"])["data"]["config.json"])
                    require(current.get("l3", {}).get("enabled") is not True, "Existing L3 requires an approved migration before native publishing")
                    require(current.get("guardrail", {}).get("enabled") is not True, "Native publishing cannot disable existing guardrails")
                    require(current.get("telemetry", {}).get("enabled") is not True or "observability" in config, "Native publishing cannot disable existing telemetry")
    documents = render_native_services(config, render_native_audit(config, source))
    if "observability" in config:
        from scripts.observability import prepare_observability
        network = config["parameters"]["platform"]["stage4Network"]
        subnet = azure.scoped(["network", "vnet", "subnet", "show", "--resource-group", config["target"]["resourceGroup"], "--vnet-name", network["virtualNetworkName"], "--name", network["privateEndpointSubnetName"], "--query", "addressPrefix"])
        documents = prepare_observability(config, documents, azure, subnet, image_public_key=image_public_key, revision=revision, directory=directory)
    return documents