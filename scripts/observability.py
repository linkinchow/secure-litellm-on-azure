"""Generate private, identity-authenticated OpenTelemetry delivery manifests."""

import copy
import json
import re
from urllib.parse import urlsplit
from uuid import UUID

import yaml

from scripts.customer_migration import ROOT, availability_zones, fingerprint, require, spread_across_zones
from scripts.migration_deploy import deployment_name, group_id

COLLECTOR_DIGEST = "sha256:8164eab2e6bca9c9b0837a8d2f118a6618489008a839db7f9d6510e66be3923c"
COLLECTOR_SOURCE = "otel/opentelemetry-collector-contrib@" + COLLECTOR_DIGEST


def telemetry_settings(config):
    settings = config.get("observability")
    require(isinstance(settings, dict) and set(settings) == {"collectorImage"}, "observability requires collectorImage")
    image = settings["collectorImage"]
    registry = config["parameters"]["platform"]["containerRegistryName"]
    require(re.fullmatch(re.escape(registry) + r"\.azurecr\.io/[a-z0-9./_-]+@" + COLLECTOR_DIGEST, image or ""), "Import the validated immutable collector version into the target ACR")
    return settings


def connection_fields(value):
    require(isinstance(value, str) and len(value) < 2048, "Invalid Application Insights connection metadata")
    pairs = [entry.split("=", 1) for entry in value.rstrip(";").split(";")]
    require(all(len(pair) == 2 for pair in pairs) and len({pair[0] for pair in pairs}) == len(pairs), "Invalid connection metadata fields")
    fields = dict(pairs)
    require(not set(fields) - {"InstrumentationKey", "IngestionEndpoint", "LiveEndpoint", "ApplicationId"}, "Unexpected connection metadata fields")
    require(UUID(fields["InstrumentationKey"]).int != 0, "Invalid Application Insights identifier")
    for name in ("IngestionEndpoint", "LiveEndpoint"):
        if name in fields:
            endpoint = urlsplit(fields[name])
            require(endpoint.scheme == "https" and not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment and endpoint.port in {None, 443} and endpoint.path in {"", "/"}, "Unsafe telemetry endpoint")
            require(endpoint.hostname.endswith(".applicationinsights.azure.com") or endpoint.hostname.endswith(".applicationinsights.microsoft.com"), "Telemetry endpoint is outside Azure Monitor")
    require("IngestionEndpoint" in fields, "An explicit telemetry ingestion endpoint is required")
    return fields


def collector_config():
    config = yaml.safe_load((ROOT / "deploy/components/stage8-audit/collector-config.yaml").read_text())
    statements = config["processors"]["transform/allowlist"]["trace_statements"]
    statements.append({"context": "scope", "statements": ['keep_keys(attributes, [])', 'set(name, "llm-gateway")', 'set(version, "")']})
    statements.append({"context": "span", "statements": ['set(name, "gateway.request")', 'set(status.message, "")', 'set(trace_state, "")', 'set(events, events)', 'set(links, links)', 'set(resource.schema_url, "")', 'set(scope.schema_url, "")']})
    config["exporters"]["azuremonitor"]["sending_queue"] = {"enabled": True, "queue_size": 256, "num_consumers": 2}
    config["service"]["telemetry"] = {"logs": {"level": "error"}}
    return config


def render_observability(config, source, receipt, subnet):
    image = telemetry_settings(config)["collectorImage"]
    connection_fields(receipt["connectionString"])
    identity = receipt["identity"]
    require(identity.get("serviceAccountName") == "otel-collector" and identity.get("kubernetesNamespace") == "litellm" and UUID(identity["clientId"]).int, "Collector identity federation mismatch")
    result = copy.deepcopy(source)
    for document in result:
        if document["kind"] == "ServiceAccount":
            require(document.get("metadata", {}).get("annotations", {}).get("azure.workload.identity/client-id") != identity["clientId"], "Collector identity must not be shared with another workload")
        if document["kind"] == "ConfigMap" and document["metadata"]["name"].startswith(("stage8-api-", "stage8-admin-")):
            settings = json.loads(document["data"]["config.json"])
            settings["telemetry"] = {"enabled": True, "endpoint": "http://otel-collector.litellm.svc.cluster.local:4318/v1/traces"}
            document["data"]["config.json"] = json.dumps(settings, sort_keys=True)
            old = document["metadata"]["name"]
            new = old.rsplit("-", 1)[0] + "-" + fingerprint(settings)[:12]
            document["metadata"]["name"] = new
            for workload in result:
                if workload["kind"] == "Deployment":
                    for volume in workload["spec"]["template"]["spec"].get("volumes", []):
                        if volume.get("configMap", {}).get("name") == old:
                            volume["configMap"]["name"] = new
    content = yaml.safe_dump(collector_config(), sort_keys=False)
    config_name = "otel-config-" + fingerprint({"config": content, "connection": receipt["connectionString"]})[:12]
    result.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": config_name, "namespace": "litellm"}, "data": {"collector-config.yaml": content, "connection-string": receipt["connectionString"]}})
    for document in yaml.safe_load_all((ROOT / "deploy/components/stage8-audit/collector.yaml").read_text()):
        document["metadata"]["namespace"] = "litellm"
        if document["kind"] == "ServiceAccount":
            document["metadata"]["annotations"]["azure.workload.identity/client-id"] = identity["clientId"]
        if document["kind"] == "Deployment":
            document["spec"]["replicas"] = 2
            pod = document["spec"]["template"]["spec"]
            container = pod["containers"][0]
            container["image"] = image
            container["env"][0] = {"name": "APPLICATIONINSIGHTS_CONNECTION_STRING", "valueFrom": {"configMapKeyRef": {"name": config_name, "key": "connection-string"}}}
            container["startupProbe"] = {"httpGet": {"path": "/", "port": 13133}, "failureThreshold": 30, "periodSeconds": 5}
            pod["volumes"][0]["configMap"]["name"] = config_name
            pod["securityContext"]["fsGroup"] = 10001
            spread_across_zones(pod, availability_zones(config), document["spec"]["selector"])
        result.append(document)
    result.append({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": "otel-collector", "namespace": "litellm"}, "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": "otel-collector"}}}})
    policy = next(document for document in yaml.safe_load_all((ROOT / "deploy/components/stage8-audit/networkpolicy.yaml").read_text()) if document["metadata"]["name"] == "stage8-collector")
    policy["metadata"]["namespace"] = "litellm"
    policy["spec"]["egress"].append({"to": [{"ipBlock": {"cidr": subnet}}], "ports": [{"protocol": "TCP", "port": 443}]})
    result.append(policy)
    next(document for document in result if document["kind"] == "NetworkPolicy" and document["metadata"]["name"] == "stage8-proxy-egress")["spec"]["egress"].append({"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "otel-collector"}}}], "ports": [{"protocol": "TCP", "port": 4318}]})
    return result


def prepare_observability(config, documents, azure, subnet, image_public_key=None, revision=None, directory=None):
    output = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 8, "observability"), "--query", "{state:properties.provisioningState,receipt:properties.outputs.observability.value}"])
    require(output.get("state") == "Succeeded", "Deploy the private observability component first")
    receipt = output["receipt"]
    base = group_id(config).lower()
    require(receipt["applicationId"].lower().startswith(base + "/providers/microsoft.insights/components/"), "Telemetry application is outside the target group")
    actual = azure.scoped(["resource", "show", "--ids", receipt["applicationId"], "--api-version", "2020-02-02"])["properties"]
    require(actual.get("DisableLocalAuth") is True and actual.get("publicNetworkAccessForIngestion") == "Disabled" and actual.get("publicNetworkAccessForQuery") == "Disabled", "Telemetry requires private Entra-only ingestion and queries")
    require(actual.get("ConnectionString") == receipt["connectionString"], "Telemetry connection metadata changed")
    identity = receipt["identity"]
    require(identity["id"].lower().startswith(base + "/providers/microsoft.managedidentity/userassignedidentities/"), "Collector identity is outside target group")
    live = azure.scoped(["identity", "show", "--ids", identity["id"]])
    require(all(live.get(key) == identity[key] for key in ("clientId", "principalId")), "Collector identity was replaced")
    cluster = azure.scoped(["aks", "show", "--resource-group", config["target"]["resourceGroup"], "--name", config["parameters"]["platform"]["stage4Aks"]["name"]])
    federation = azure.scoped(["identity", "federated-credential", "list", "--resource-group", config["target"]["resourceGroup"], "--identity-name", identity["name"]])
    require(len(federation) == 1 and federation[0].get("issuer") == cluster["oidcIssuerProfile"]["issuerUrl"] and federation[0].get("subject") == "system:serviceaccount:litellm:otel-collector" and federation[0].get("audiences") == ["api://AzureADTokenExchange"], "Collector federation mismatch")
    if image_public_key is not None:
        from scripts.backend_manifest import verify_runtime_image
        require(revision is not None and directory is not None, "Local collector verification requires the reviewed revision and operation directory")
        verify_runtime_image(config, revision, directory, telemetry_settings(config)["collectorImage"], "collector", public_key=image_public_key)
    return render_observability(config, documents, receipt, subnet)