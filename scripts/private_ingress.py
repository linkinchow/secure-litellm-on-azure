"""Render isolated Traefik file-provider gateways; never watch application Secrets."""

import copy
import ipaddress
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import yaml

from scripts.customer_migration import ROOT, availability_zones, require, spread_across_zones
from scripts.render_stage7_domain import domain_hosts


NATIVE_API_PATHS = ("/chat/completions", "/v1/chat/completions", "/responses", "/v1/responses", "/embeddings", "/v1/embeddings")
NATIVE_WSS_PATHS = ("/responses", "/v1/responses")
FRONT_DOOR_HEADER = "X-Azure-FDID"
APP_GATEWAY_HEADER = "X-LLMGW-Edge-Id"
# Header the private ingress requires from its public edge, and the pod annotation recording it.
EDGE_HEADERS = {FRONT_DOOR_HEADER: "llmgw/front-door-id", APP_GATEWAY_HEADER: "llmgw/edge-id"}
EDGE_BINDING = re.compile(r"HeaderRegexp\(`(X-Azure-FDID|X-LLMGW-Edge-Id)`,\s*`\(\?i\)\^([a-fA-F0-9-]+)\$`\)")


def edge_header_bindings(rule):
    return EDGE_BINDING.findall(rule or "")


def native_api_rule(host, front_door_id=None, header=FRONT_DOOR_HEADER):
    path_rule = " || ".join(f"Path(`{path}`)" for path in NATIVE_API_PATHS)
    websocket_paths = " || ".join(f"Path(`{path}`)" for path in NATIVE_WSS_PATHS)
    websocket = (
        f"Method(`GET`) && ({websocket_paths})"
        " && HeaderRegexp(`Upgrade`, `(?i)^websocket$`)"
        " && HeaderRegexp(`Connection`, `(?i)(^|[, \\t])upgrade([, \\t]|$)`)"
    )
    rule = f"Host(`{host}`) && ((Method(`POST`) && ({path_rule})) || ({websocket}))"
    if front_door_id is not None:
        require(header in EDGE_HEADERS, "Unknown edge identity header")
        rule += f" && HeaderRegexp(`{header}`, `(?i)^{front_door_id}$`)"
    return rule


def native_admin_rule(host, front_door_id=None, header=FRONT_DOOR_HEADER):
    rule = f"Host(`{host}`)"
    if front_door_id is not None:
        require(header in EDGE_HEADERS, "Unknown edge identity header")
        rule += f" && HeaderRegexp(`{header}`, `(?i)^{front_door_id}$`)"
    return rule


def native_health_rule(host):
    return f"Host(`{host}`) && Method(`GET`) && Path(`/readyz`)"


def ingress_settings(config):
    settings = config.get("privateIngress", {})
    require(isinstance(settings, dict) and set(settings) == {"api", "admin"}, "privateIngress requires api and admin configuration")
    for plane in ("api", "admin"):
        item = settings[plane]
        require(isinstance(item, dict) and set(item) == {"tlsSecretId", "allowedCidrs"}, "Each ingress plane needs tlsSecretId and allowedCidrs")
        secret = urlsplit(item["tlsSecretId"])
        require(secret.scheme == "https" and bool(re.fullmatch(r"[a-z0-9-]+\.vault\.azure\.net", secret.netloc)) and re.fullmatch(r"/secrets/[A-Za-z0-9-]+(?:/[0-9a-f]{32})?", secret.path) is not None and not secret.query and not secret.fragment, "TLS must reference an Azure public-cloud Key Vault PEM secret, without credentials or query")
        source_ranges(item["allowedCidrs"])
    return settings


def ingress_image(config):
    lock = json.loads((ROOT / "deploy/private-ingress-image.json").read_text())
    registry = config["parameters"]["platform"]["containerRegistryName"]
    require(re.fullmatch(r"[a-zA-Z0-9]{5,50}", registry) is not None, "Invalid ingress ACR name")
    return f"{registry}.azurecr.io/{lock['repository']}@{lock['digest']}", lock


def certificate_material(pem, host, now=None):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import ExtendedKeyUsageOID
    from service_identity.cryptography import verify_certificate_hostname

    require(isinstance(pem, str) and len(pem) <= 131072, "Invalid TLS PEM size")
    encoded = pem.encode()
    certificates = x509.load_pem_x509_certificates(encoded)
    require(bool(certificates), "TLS PEM must include a leaf certificate and intermediate chain")
    leaf = certificates[0]
    key = serialization.load_pem_private_key(encoded, password=None)
    public = lambda value: value.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    require(public(leaf.public_key()) == public(key.public_key()), "TLS certificate/private key mismatch")
    now = now or datetime.now(timezone.utc)
    require(leaf.not_valid_before_utc <= now and leaf.not_valid_after_utc >= now + timedelta(days=7), "TLS certificate is not valid or expires within seven days")
    leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    verify_certificate_hostname(leaf, host)
    try:
        usage = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        require(ExtendedKeyUsageOID.SERVER_AUTH in usage, "TLS certificate is not valid for server authentication")
    except x509.ExtensionNotFound:
        pass
    chain = b"".join(certificate.public_bytes(serialization.Encoding.PEM) for certificate in certificates)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return {"certificate": chain.decode(), "key": private.decode(), "sha256": hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest(), "expiresAt": leaf.not_valid_after_utc.isoformat()}


def source_ranges(values):
    require(isinstance(values, list) and bool(values), "Explicit private ingress source CIDRs are required")
    networks = []
    private = [ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    for value in values:
        network = ipaddress.ip_network(value)
        require(network.version == 4 and any(network.subnet_of(parent) for parent in private), "Ingress sources must use explicit RFC1918 IPv4 ranges")
        networks.append(str(network))
    return sorted(set(networks))


def preserve_native_edge_binding(config, documents, current_config_map):
    result = copy.deepcopy(documents)
    if not current_config_map:
        return result
    config_map = next(item for item in result if item["kind"] == "ConfigMap")
    match = re.fullmatch(r"llm-(api|admin)-ingress", config_map["metadata"]["name"])
    require(match is not None, "Cannot preserve a Front Door binding for an unknown ingress plane")
    plane = match[1]
    current = yaml.safe_load(current_config_map.get("data", {}).get("routes.yaml", ""))
    current_rule = (current or {}).get("http", {}).get("routers", {}).get(plane, {}).get("rule", "")
    matches = edge_header_bindings(current_rule)
    require(len(matches) <= 1, "Existing native API ingress has ambiguous Front Door bindings")
    if not matches:
        return result
    from uuid import UUID
    header, identifier = matches[0]
    require(UUID(identifier).int != 0, "Existing native API ingress has an invalid Front Door binding")
    deployment = next(item for item in result if item["kind"] == "Deployment")
    dynamic = yaml.safe_load(config_map["data"]["routes.yaml"])
    require(not edge_header_bindings(dynamic["http"]["routers"][plane]["rule"]), f"Rendered native {plane} ingress unexpectedly contains a Front Door binding")
    rule = native_api_rule if plane == "api" else native_admin_rule
    dynamic["http"]["routers"][plane]["rule"] = rule(domain_hosts(config["baseDomain"])[plane], identifier, header)
    config_map["data"]["routes.yaml"] = yaml.safe_dump(dynamic, sort_keys=False)
    annotations = deployment["spec"]["template"]["metadata"].setdefault("annotations", {})
    annotations[EDGE_HEADERS[header]] = identifier
    annotations["llmgw/routes-sha256"] = hashlib.sha256(json.dumps(dynamic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


preserve_native_front_door_binding = preserve_native_edge_binding


def render_ingress(config, plane, image, ranges):
    require(plane in {"api", "admin"}, "Unknown ingress plane")
    require(re.fullmatch(r"[a-z0-9./_-]+@sha256:[0-9a-f]{64}", image or "") is not None, "Ingress image must be digest pinned")
    hosts = domain_hosts(config["baseDomain"])
    from scripts.backend_manifest import application_authentication
    authentication = application_authentication(config)
    native = authentication["mode"] == "native"
    name = f"llm-{plane}-ingress"
    labels = {"app.kubernetes.io/name": "llmgw-ingress", "app.kubernetes.io/component": "controller", "app.kubernetes.io/managed-by": "llmgw-workflow", "plane": plane}
    network = config["parameters"]["platform"]["stage4Network"]
    allowed = source_ranges([*ranges, network["ingressSubnetPrefix"]])
    metadata = {"name": name, "namespace": name, "labels": labels}
    from scripts.optional_l3 import enabled, upstream as native_upstream
    audit_proxy = native and plane == "api" and enabled(config)
    upstream = native_upstream(config, plane) if native else f"http://llm-{plane}-proxy.litellm.svc.cluster.local:8080"
    business_rule = native_api_rule(hosts[plane]) if native and plane == "api" else native_admin_rule(hosts[plane]) if native else f"Host(`{hosts[plane]}`)"
    routers = {plane: {"rule": business_rule, "entryPoints": ["websecure"], "service": plane, "tls": {}}}
    middlewares = {}
    if native:
        health = f"{plane}-health"
        health_path = f"{plane}-health-path"
        routers[health] = {"rule": native_health_rule(hosts[plane]), "entryPoints": ["websecure"], "service": plane, "middlewares": [health_path], "tls": {}}
        middlewares[health_path] = {"replacePath": {"path": "/health/readiness"}}
    dynamic = {
        "http": {
            "routers": routers,
            "services": {plane: {"loadBalancer": {"servers": [{"url": upstream}], "passHostHeader": True}}},
        },
        "tls": {
            "certificates": [{"certFile": "/certs/tls.crt", "keyFile": "/certs/tls.key"}],
            "options": {"default": {"minVersion": "VersionTLS12", "sniStrict": True}},
        },
    }
    if middlewares:
        dynamic["http"]["middlewares"] = middlewares
    arguments = [
        "--entrypoints.websecure.address=:8443", "--entrypoints.websecure.http.tls=true",
        "--entrypoints.websecure.transport.respondingtimeouts.readtimeout=600s",
        "--entrypoints.websecure.transport.respondingtimeouts.writetimeout=0s",
        "--entrypoints.websecure.transport.respondingtimeouts.idletimeout=600s",
        "--entrypoints.health.address=:8082", "--ping=true", "--ping.entrypoint=health",
        "--providers.file.directory=/dynamic", "--providers.file.watch=true",
        "--api.dashboard=false", "--log.level=ERROR", "--accesslog=false",
        "--global.checknewversion=false", "--global.sendanonymoususage=false",
    ]
    container = {
        "name": "traefik", "image": image, "args": arguments,
        "ports": [{"name": "https", "containerPort": 8443}, {"name": "health", "containerPort": 8082}],
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
        "volumeMounts": [{"name": "dynamic", "mountPath": "/dynamic", "readOnly": True}, {"name": "tls", "mountPath": "/certs", "readOnly": True}, {"name": "tmp", "mountPath": "/tmp"}],
        **{probe: {"httpGet": {"path": "/ping", "port": "health"}, "periodSeconds": 10, "timeoutSeconds": 3, "failureThreshold": 30 if probe == "startupProbe" else 3} for probe in ("startupProbe", "readinessProbe", "livenessProbe")},
    }
    pod = {
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532, "fsGroup": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
        "terminationGracePeriodSeconds": 60, "containers": [container],
        "volumes": [{"name": "dynamic", "configMap": {"name": name}}, {"name": "tls", "secret": {"secretName": name + "-tls", "defaultMode": 288}}, {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}],
        "topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": "kubernetes.io/hostname", "whenUnsatisfiable": "DoNotSchedule", "labelSelector": {"matchLabels": labels}}],
    }
    spread_across_zones(pod, availability_zones(config))
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata, "data": {"routes.yaml": yaml.safe_dump(dynamic, sort_keys=False)}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata, "spec": {"replicas": 2, "selector": {"matchLabels": labels}, "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}}, "template": {"metadata": {"labels": labels}, "spec": pod}}},
        {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": metadata, "spec": {"minAvailable": 1, "selector": {"matchLabels": labels}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {**metadata, "annotations": {"service.beta.kubernetes.io/azure-load-balancer-internal": "true", "service.beta.kubernetes.io/azure-load-balancer-internal-subnet": network["ingressSubnetName"]}}, "spec": {"type": "LoadBalancer", "externalTrafficPolicy": "Local", "loadBalancerSourceRanges": allowed, "selector": labels, "ports": [{"name": "https", "port": 443, "targetPort": "https", "protocol": "TCP"}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata, "spec": {
            "podSelector": {"matchLabels": labels}, "policyTypes": ["Ingress", "Egress"],
            "ingress": [{"from": [{"ipBlock": {"cidr": cidr}} for cidr in allowed], "ports": [{"protocol": "TCP", "port": 8443}]}],
            "egress": [
                {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}], "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
                {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "litellm"}}, "podSelector": {"matchLabels": {"app.kubernetes.io/name": "l3-audit"} if audit_proxy else {"app.kubernetes.io/name": "litellm", "app.kubernetes.io/component": "gateway"} if native else {"app.kubernetes.io/name": "entra-auth-proxy", "plane": plane}}}], "ports": [{"protocol": "TCP", "port": 8080 if audit_proxy or not native else 4000}]},
            ],
        }},
    ]