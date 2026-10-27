"""Plan, deploy and verify the managed private gateways on a private runner."""

import base64
import copy
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID

import certifi
import yaml

from scripts.customer_migration import MigrationError, fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands, deployment_name, group_id
from scripts.migration_runtime import connect_cluster, run_command
from scripts.private_ingress import certificate_material, ingress_image, ingress_settings, preserve_native_edge_binding, render_ingress
from scripts.render_stage7_domain import domain_hosts
from scripts.workflow_diagnostics import command_failure_summary


def ingress_ownership_handoff(document, current, native):
    if not native or not current or document["kind"] not in {"ConfigMap", "Deployment"}:
        return False
    root = "f:data" if document["kind"] == "ConfigMap" else "f:spec"
    managers = current["metadata"].get("managedFields")
    require(isinstance(managers, list) and managers,
            "Native ingress ownership metadata is missing; read objects with --show-managed-fields")
    legacy = any(entry.get("manager") == "kubectl-patch" and root in entry.get("fieldsV1", {})
                 for entry in managers)
    if not legacy:
        return False
    require(current["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") == "llmgw-workflow",
            "Ownership handoff requires an existing workflow-managed ingress")
    for entry in managers:
        fields = entry.get("fieldsV1", {})
        labels = fields.get("f:metadata", {}).get("f:labels", {})
        touched = root in fields or any("f:" + key in labels for key in document["metadata"].get("labels", {}))
        require(not touched or entry.get("manager") in {"llmgw-ingress", "kubectl-patch"},
                "Ingress ownership handoff would override another manager; review its fields separately")
    require(current["metadata"].get("uid") and current["metadata"].get("resourceVersion"),
            "Ownership handoff requires live UID and resourceVersion")
    return True


def read_certificate(config, secret_id, host):
    result = subprocess.run(["az", "keyvault", "secret", "show", "--id", secret_id, "--subscription", config["azure"]["subscriptionId"], "--only-show-errors", "--output", "json"], capture_output=True, text=True, check=False, timeout=120)
    require(result.returncode == 0, "Cannot read the configured TLS secret; check private access and Key Vault authorization")
    secret = json.loads(result.stdout)
    require(secret.get("attributes", {}).get("enabled") is True, "TLS secret is disabled")
    requested, returned = urlsplit(secret_id), urlsplit(secret["id"])
    require(returned.scheme == requested.scheme and returned.netloc == requested.netloc and not returned.query and not returned.fragment, "TLS secret returned from an unexpected Vault")
    expected_path = requested.path.split("/")
    actual_path = returned.path.split("/")
    require(len(actual_path) == 4 and actual_path[:3] == expected_path[:3] and len(actual_path[3]) == 32 and all(character in "0123456789abcdef" for character in actual_path[3]), "TLS secret must resolve to the configured secret and a concrete version")
    require(len(expected_path) == 3 or actual_path == expected_path, "TLS secret version differs from the requested version")
    try:
        material = certificate_material(secret["value"], host)
    except Exception:
        raise MigrationError("TLS PEM failed certificate, key, hostname or validity checks") from None
    material["secretId"] = secret["id"]
    return material


def scan_image(source, directory, run=subprocess.run):
    result = run(["trivy", "image", "--quiet", "--exit-code", "1", "--severity", "CRITICAL", "--platform", "linux/amd64", "--format", "json", source], capture_output=True, text=True, check=False, timeout=1200)
    private_write(directory / "ingress-image-scan.stderr.txt", result.stderr)
    require(len(result.stdout) <= 8 * 1024 * 1024, "Ingress image scan report exceeded its size limit")
    try:
        report = json.loads(result.stdout)
    except ValueError:
        if result.returncode:
            raise MigrationError("Ingress image scan failed: " + command_failure_summary(result.stdout, result.stderr, result.returncode)) from None
        raise MigrationError("Ingress image scan returned invalid JSON") from None
    require(isinstance(report, dict) and isinstance(report.get("Results"), list), "Ingress image scan returned an unexpected report")
    private_write(directory / "ingress-image-scan.json", json.dumps(report, sort_keys=True))
    findings = []
    def safe(value):
        return isinstance(value, str) and 1 <= len(value) <= 200 and "\n" not in value and "\r" not in value
    for target in report["Results"]:
        require(isinstance(target, dict), "Ingress image scan returned an unexpected result")
        vulnerabilities = target.get("Vulnerabilities") or []
        require(isinstance(vulnerabilities, list), "Ingress image scan returned invalid vulnerabilities")
        for vulnerability in vulnerabilities:
            require(isinstance(vulnerability, dict), "Ingress image scan returned an invalid vulnerability")
            identifier = vulnerability.get("VulnerabilityID", "unknown")
            package = vulnerability.get("PkgName", "unknown")
            installed = vulnerability.get("InstalledVersion", "unknown")
            fixed = vulnerability.get("FixedVersion") or "unavailable"
            values = (identifier, package, installed, fixed)
            require(all(safe(value) for value in values), "Ingress image scan returned unsafe vulnerability metadata")
            findings.append(f"vulnerability={identifier} package={package} installed={installed} fixed={fixed}")
        for field, kind, keys in (("Secrets", "secret", ("RuleID", "Category")), ("Misconfigurations", "misconfiguration", ("ID", "Type"))):
            entries = target.get(field) or []
            require(isinstance(entries, list), f"Ingress image scan returned invalid {field.lower()}")
            for entry in entries:
                require(isinstance(entry, dict), f"Ingress image scan returned an invalid {kind}")
                values = [entry.get(key) or "unknown" for key in keys]
                require(all(safe(value) for value in values), f"Ingress image scan returned unsafe {kind} metadata")
                findings.append(kind + "=" + values[0] + " category=" + values[1])
    if findings:
        suffix = f"; and {len(findings) - 20} more" if len(findings) > 20 else ""
        raise MigrationError("Ingress image scan found CRITICAL findings: " + "; ".join(findings[:20]) + suffix)
    if result.returncode:
        raise MigrationError("Ingress image scan failed: " + command_failure_summary(result.stdout, result.stderr, result.returncode))


def require_private_registry_dns(registry, resolve=socket.getaddrinfo):
    host = registry + ".azurecr.io"
    try:
        addresses = {item[4][0] for item in resolve(host, 443, type=socket.SOCK_STREAM)}
    except OSError:
        raise MigrationError("Unable to resolve the ACR login server from the private runner") from None
    private_ranges = [ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    require(addresses and all(any(ipaddress.ip_address(address) in network for network in private_ranges) for address in addresses), "ACR login server must resolve only to private endpoint addresses; deploy runner-target-connectivity before private-ingress")


def promote_image(config, directory, azure, lock):
    source = f"{lock['source']}@{lock['digest']}"
    registry = config["parameters"]["platform"]["containerRegistryName"]
    target = f"{registry}.azurecr.io/{lock['repository']}"
    require_private_registry_dns(registry)
    scan_image(source, directory)
    run_command(["syft", source, "--platform", "linux/amd64", "--output", f"spdx-json={directory / 'ingress-sbom.spdx.json'}"], directory, "ingress-image-sbom")
    token = subprocess.run(["az", "acr", "login", "--name", registry, "--expose-token", "--subscription", config["azure"]["subscriptionId"], "--only-show-errors", "--output", "json"], capture_output=True, text=True, check=False, timeout=120)
    if token.returncode:
        raise MigrationError("Unable to obtain scoped ACR login token: " + command_failure_summary("", token.stderr, token.returncode))
    credentials = json.loads(token.stdout)
    require(credentials["loginServer"].lower() == f"{registry}.azurecr.io".lower(), "Unexpected ACR login server")
    authfile = directory / "registry-auth.json"
    encoded = base64.b64encode(("00000000-0000-0000-0000-000000000000:" + credentials["accessToken"]).encode()).decode()
    private_write(authfile, json.dumps({"auths": {credentials["loginServer"]: {"auth": encoded}}}))
    try:
        run_command(["skopeo", "copy", "--all", "--preserve-digests", "--authfile", str(authfile), "docker://" + source, "docker://" + target + ":sha256-" + lock["digest"].split(":")[1]], directory, "ingress-image-copy")
        digest = azure.scoped(["acr", "repository", "show", "--name", registry, "--image", lock["repository"] + "@" + lock["digest"], "--query", "digest"])
        require(digest == lock["digest"], "Promoted ingress image digest mismatch")
    finally:
        authfile.unlink(missing_ok=True)


def verify_endpoint(address, host, other_host, expected_sha256, *, native=False, plane=None, front_door_id=None, edge_binding=None):
    from scripts.private_ingress import EDGE_HEADERS, FRONT_DOOR_HEADER
    require(front_door_id is None or edge_binding is None, "Pass either a Front Door ID or an edge binding")
    if front_door_id is not None:
        edge_binding = (FRONT_DOOR_HEADER, front_door_id)
    edge_header, front_door_id = edge_binding if edge_binding is not None else (FRONT_DOOR_HEADER, None)
    require(front_door_id is None or native, "Front Door probes require native ingress" if edge_header == FRONT_DOOR_HEADER else "Edge identity probes require native ingress")
    if front_door_id is not None:
        require(edge_header in EDGE_HEADERS and isinstance(front_door_id, str)
                and re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", front_door_id)
                and UUID(front_door_id).int != 0, "Invalid Front Door identity for ingress verification" if edge_header == FRONT_DOOR_HEADER else "Invalid edge identity for ingress verification")
    context = ssl.create_default_context(cafile=certifi.where())

    def probe(method, path, request_host, statuses, identifier=None, body=b"", label="route"):
        header = f"{edge_header}: {identifier}\r\n" if identifier else ""
        request = (f"{method} {path} HTTP/1.1\r\nHost: {request_host}\r\n{header}"
                   f"Connection: close\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n")
        with socket.create_connection((address, 443), timeout=15) as connection:
            with context.wrap_socket(connection, server_hostname=host) as secured:
                require(hashlib.sha256(secured.getpeercert(binary_form=True)).hexdigest() == expected_sha256,
                        "Ingress served an unexpected TLS certificate")
                secured.sendall(request.encode("ascii") + body)
                response = http.client.HTTPResponse(secured)
                response.begin()
                scope = f"Native {plane}" if native else "Private"
                require(response.status in statuses,
                        f"{scope} ingress {label} verification failed for {path}: "
                        f"HTTP {response.status}; expected {sorted(statuses)}")

    probe("GET", "/", other_host, {404, 421}, front_door_id, label="other-host rejection")
    if native:
        require(plane in {"api", "admin"}, "Native ingress verification requires an explicit plane")
        if plane == "api":
            probe("GET", "/readyz", host, {200})
            probe("GET", "/fallback/login", host, {404}, front_door_id)
        if front_door_id is not None:
            method, path, body = ("POST", "/v1/responses", b"{}") if plane == "api" else ("GET", "/fallback/login", b"")
            wrong_id = "00000000-0000-0000-0000-000000000001"
            if UUID(front_door_id) == UUID(wrong_id):
                wrong_id = "00000000-0000-0000-0000-000000000002"
            short = "FDID" if edge_header == FRONT_DOOR_HEADER else "edge-ID"
            probe(method, path, host, {404}, body=body, label=f"missing-{short} rejection")
            probe(method, path, host, {404}, wrong_id, body, label=f"wrong-{short} rejection")
            probe(method, path, host, {401, 403} if plane == "api" else {200},
                  front_door_id, body, label="bound route")
        elif plane == "admin":
            probe("GET", "/fallback/login", host, {200})


def frontend_for(config, azure, node_group, address):
    network = config["parameters"]["platform"]["stage4Network"]
    subnet_id = group_id(config) + "/providers/Microsoft.Network/virtualNetworks/" + network["virtualNetworkName"] + "/subnets/" + network["ingressSubnetName"]
    matches = []
    for candidate in azure.scoped(["network", "lb", "list", "--resource-group", node_group]):
        if candidate.get("sku", {}).get("name") != "Standard":
            continue
        for frontend in candidate.get("frontendIPConfigurations", []):
            if frontend.get("privateIPAddress") == address and not frontend.get("publicIPAddress") and frontend.get("subnet", {}).get("id", "").lower() == subnet_id.lower():
                matches.append({"resourceGroupName": node_group, "name": candidate["name"], "frontendName": frontend["name"], "privateIpAddress": address})
    require(len(matches) == 1, "Private ingress IP must map to exactly one Standard LB frontend in the approved ingress subnet")
    return matches[0]


def ingress_observation(scoped, namespace, service, directory):
    slices = json.loads(run_command([*scoped, "get", "endpointslices.discovery.k8s.io", "--selector", "kubernetes.io/service-name=" + namespace, "-o", "json"], directory, "endpoint-slices-" + namespace))
    events = json.loads(run_command([*scoped, "get", "events", "--field-selector", "involvedObject.kind=Service,involvedObject.name=" + namespace, "-o", "json"], directory, "service-events-" + namespace))
    require(isinstance(slices, dict) and isinstance(slices.get("items"), list), "EndpointSlice observation returned an unexpected response")
    require(isinstance(events, dict) and isinstance(events.get("items"), list), "Service event observation returned an unexpected response")
    endpoint_slices = []
    ready_endpoint_count = 0
    for item in slices["items"][:20]:
        endpoints = item.get("endpoints", [])
        require(isinstance(endpoints, list), "EndpointSlice observation returned invalid endpoints")
        observed_endpoints = []
        for endpoint in endpoints[:50]:
            conditions = endpoint.get("conditions", {})
            if conditions.get("ready") is not False:
                ready_endpoint_count += 1
            observed_endpoints.append({"nodeName": endpoint.get("nodeName"), "conditions": conditions})
        endpoint_slices.append({"name": item.get("metadata", {}).get("name"), "addressType": item.get("addressType"), "ports": item.get("ports", []), "endpoints": observed_endpoints})
    observed_events = []
    for item in events["items"][-20:]:
        message = item.get("message")
        observed_events.append({
            "type": item.get("type"),
            "reason": item.get("reason"),
            "count": item.get("count"),
            "firstTimestamp": item.get("firstTimestamp"),
            "lastTimestamp": item.get("lastTimestamp"),
            "eventTime": item.get("eventTime"),
            "sourceComponent": item.get("source", {}).get("component"),
            "message": message[:2000] if isinstance(message, str) else None,
        })
    ingress = (service or {}).get("status", {}).get("loadBalancer", {}).get("ingress", [])
    return {
        "serviceExists": service is not None,
        "serviceUid": (service or {}).get("metadata", {}).get("uid"),
        "serviceResourceVersion": (service or {}).get("metadata", {}).get("resourceVersion"),
        "loadBalancerStatus": (service or {}).get("status", {}).get("loadBalancer", {}),
        "loadBalancerIngressCount": len(ingress) if isinstance(ingress, list) else 0,
        "readyEndpointCount": ready_endpoint_count,
        "endpointSlices": endpoint_slices,
        "events": observed_events,
    }


def deployed_private_ingress(config, azure):
    result = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 4, "private-ingress"), "--query", "{state:properties.provisioningState,ingress:properties.outputs.privateIngress.value}"])
    ingress = result.get("ingress", {})
    require(result.get("state") == "Succeeded" and ingress.get("configSha256") == stage_fingerprint(config, 4), "Deploy the current Stage 4 private ingress before verifying backend routes")
    return ingress


def verify_native_ingress_routes(config, ingress, *, front_door_id=None, edge_binding=None):
    require(ingress.get("authenticationMode") == "native" and ingress.get("backendRoutesVerified") is False, "Backend route verification applies only to the initial native private ingress")
    hosts = domain_hosts(config["baseDomain"])
    network = ipaddress.ip_network(config["parameters"]["platform"]["stage4Network"]["ingressSubnetPrefix"])
    addresses = {}
    for plane in ("api", "admin"):
        address = ingress.get(plane, {}).get("privateIpAddress")
        certificate_sha256 = ingress.get("certificates", {}).get(plane, {}).get("sha256")
        require(isinstance(address, str) and ipaddress.ip_address(address) in network and re.fullmatch(r"[a-f0-9]{64}", certificate_sha256 or ""), "Stage 4 private ingress receipt is incomplete")
        addresses[plane] = address
        verify_endpoint(address, hosts[plane], hosts["admin" if plane == "api" else "api"], certificate_sha256,
                        native=True, plane=plane, front_door_id=front_door_id, edge_binding=edge_binding)
    require(addresses["api"] != addresses["admin"], "API/admin must not share an ingress frontend")
    return ingress


def current_edge_binding(config, azure):
    """Edge header the ingress already requires after Stage 9, so re-running Stage 6 on a published gateway still verifies."""
    name = deployment_name(config, 9, "edge-bind")
    receipts = azure.scoped(["deployment", "group", "list", "--resource-group", config["target"]["resourceGroup"], "--query", f"[?name=='{name}'].properties.outputs.edgeBinding.value"])
    require(isinstance(receipts, list) and len(receipts) <= 1, "Ambiguous Stage 9 edge binding receipt")
    if not receipts:
        return None
    from scripts.edge_binding import bound_edge
    bindings = {plane: bound_edge((receipts[0].get("planes") or {}).get(plane) or {}) for plane in ("api", "admin")}
    require(bindings["api"] == bindings["admin"], "API and Admin ingresses are bound to different edges")
    return bindings["api"]


def verify_private_ingress_backends(config, revision, directory, azure=None):
    azure = azure or AzureCommands(config, directory)
    ingress = deployed_private_ingress(config, azure)
    ingress = verify_native_ingress_routes(config, ingress, edge_binding=current_edge_binding(config, azure))
    verification = {
        "revision": revision,
        "configSha256": stage_fingerprint(config, 6),
        "privateIngressSha256": fingerprint(ingress),
        "authenticationMode": "native",
        "backendRoutesVerified": True,
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
    }
    receipt = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"privateIngressBackend": {"type": "object", "value": verification}}}
    receipt_path = directory / "ingress-backend-receipt-template.json"
    private_write(receipt_path, json.dumps(receipt))
    saved = azure.scoped(["deployment", "group", "create", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 6, "private-ingress-backend"), "--mode", "Incremental", "--template-file", str(receipt_path)])
    require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "Private ingress backend routes passed, but saving verification evidence failed")
    return verification


def require_private_ingress_backends(config, azure, *, front_door_id=None, edge_binding=None):
    ingress = deployed_private_ingress(config, azure)
    result = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 6, "private-ingress-backend"), "--query", "{state:properties.provisioningState,verification:properties.outputs.privateIngressBackend.value}"])
    verification = result.get("verification", {})
    expected = {
        "configSha256": stage_fingerprint(config, 6),
        "authenticationMode": "native",
        "backendRoutesVerified": True,
    }
    require(result.get("state") == "Succeeded" and re.fullmatch(r"[0-9a-f]{40}", verification.get("revision", "")) is not None and all(verification.get(key) == value for key, value in expected.items()), "Verify the current native private ingress against the Stage 6 backend before enabling traffic")
    verify_native_ingress_routes(config, ingress, front_door_id=front_door_id, edge_binding=edge_binding)


def deploy_private_ingress(config, operation, revision, directory, approved):
    require(operation in {"plan", "execute"}, "Invalid private ingress operation")
    settings = ingress_settings(config)
    from scripts.backend_manifest import application_authentication
    authentication_mode = application_authentication(config)["mode"] if "application" in config else "entra"
    image, lock = ingress_image(config)
    hosts = domain_hosts(config["baseDomain"])
    kube = connect_cluster(config, directory, legacy=False)
    azure = AzureCommands(config, directory)
    cluster = azure.scoped(["aks", "show", "--resource-group", config["target"]["resourceGroup"], "--name", config["parameters"]["platform"]["stage4Aks"]["name"], "--query", "{id:id,nodeResourceGroup:nodeResourceGroup,private:apiServerAccessProfile.enablePrivateCluster}"])
    require(cluster.get("private") is True, "Managed ingress requires the target Private AKS")
    network = config["parameters"]["platform"]["stage4Network"]
    subnet = azure.scoped(["network", "vnet", "subnet", "show", "--resource-group", config["target"]["resourceGroup"], "--vnet-name", network["virtualNetworkName"], "--name", network["ingressSubnetName"], "--query", "{prefix:addressPrefix,plsPolicy:privateLinkServiceNetworkPolicies}"])
    require(subnet.get("plsPolicy") == "Disabled", "Ingress subnet must permit Private Link Service before deployment")
    require(subnet["prefix"] == network["ingressSubnetPrefix"], "Deployed ingress subnet differs from configuration")
    materials, manifests, live, observations, handoffs = {}, {}, [], {}, {}
    for plane in ("api", "admin"):
        namespace = f"llm-{plane}-ingress"
        scoped = [*kube, "--namespace", namespace]
        run_command([*scoped, "get", "namespace", namespace, "-o", "name"], directory, "namespace-" + plane)
        material = read_certificate(config, settings[plane]["tlsSecretId"], hosts[plane])
        materials[plane] = material
        certificate_path = directory / f"{plane}-chain.pem"
        private_write(certificate_path, material["certificate"])
        run_command(["openssl", "verify", "-CAfile", certifi.where(), "-purpose", "sslserver", "-verify_hostname", hosts[plane], "-untrusted", str(certificate_path), str(certificate_path)], directory, "certificate-trust-" + plane)
        documents = [document for document in render_ingress(config, plane, image, settings[plane]["allowedCidrs"]) if document["kind"] != "Namespace"]
        preserved_config_map = None
        if authentication_mode == "native":
            preserved_config_map = json.loads(run_command([*scoped, "get", "ConfigMap", namespace, "--ignore-not-found", "--show-managed-fields", "-o", "json"], directory, f"before-binding-{plane}-configmap") or "null")
            documents = preserve_native_edge_binding(config, documents, preserved_config_map)
        current_service = None
        ordinary, handoffs[plane] = [], []
        for document in documents:
            if document["kind"] == "Deployment":
                pod = document["spec"]["template"]
                pod["metadata"].setdefault("annotations", {})["llmgw/certificate-sha256"] = material["sha256"]
                pod["spec"]["volumes"][1]["secret"]["secretName"] = namespace + "-tls-" + material["sha256"][:16]
            current = preserved_config_map if document["kind"] == "ConfigMap" and preserved_config_map is not None else json.loads(run_command([*scoped, "get", document["kind"], document["metadata"]["name"], "--ignore-not-found", "--show-managed-fields", "-o", "json"], directory, f"before-{plane}-{document['kind']}") or "null")
            if document["kind"] == "Service":
                current_service = current
            if current:
                require(current["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") == "llmgw-workflow", "Refusing to adopt an existing unmanaged ingress object")
                live.append({"uid": current["metadata"]["uid"], "spec": current.get("spec"), "data": current.get("data"), "labels": current["metadata"].get("labels"), "annotations": current["metadata"].get("annotations"),
                             "managedFields": [{key: value for key, value in entry.items() if key != "time"}
                                               for entry in current["metadata"].get("managedFields", [])]})
            else:
                live.append(None)
            if ingress_ownership_handoff(document, current, authentication_mode == "native"):
                payload = copy.deepcopy(document)
                payload["metadata"].update(uid=current["metadata"]["uid"], resourceVersion=current["metadata"]["resourceVersion"])
                handoff_path = directory / f"ownership-{plane}-{document['kind']}.yaml"
                private_write(handoff_path, yaml.safe_dump(payload, sort_keys=False))
                handoffs[plane].append(handoff_path)
                run_command([*scoped, "apply", "--server-side", "--field-manager=llmgw-ingress",
                             "--force-conflicts", "--dry-run=server", "-f", str(handoff_path)],
                            directory, f"dry-run-ownership-{plane}-{document['kind']}")
            else:
                ordinary.append(document)
        manifests[plane] = documents
        path = directory / f"ingress-{plane}.yaml"
        private_write(path, yaml.safe_dump_all(ordinary, sort_keys=False))
        run_command([*scoped, "apply", "--server-side", "--field-manager=llmgw-ingress", "--dry-run=server", "-f", str(path)], directory, "dry-run-" + plane)
        observations[plane] = ingress_observation(scoped, namespace, current_service, directory)
    public_certificates = {plane: {key: material[key] for key in ("sha256", "expiresAt", "secretId")} for plane, material in materials.items()}
    ownership_handoffs = {plane: [path.name for path in paths] for plane, paths in handoffs.items()}
    plan = {"stage": 4, "action": "private-ingress", "revision": revision, "configSha256": stage_fingerprint(config, 4), "cluster": cluster, "subnet": subnet, "documents": manifests, "certificates": public_certificates, "before": live,
            "legacyOwnershipHandoffs": ownership_handoffs}
    plan_hash = fingerprint(plan)
    public_observations = {plane: {"serviceExists": observation["serviceExists"], "loadBalancerIngressCount": observation["loadBalancerIngressCount"], "readyEndpointCount": observation["readyEndpointCount"], "eventReasons": sorted({event["reason"] for event in observation["events"] if isinstance(event.get("reason"), str)})} for plane, observation in observations.items()}
    summary = {"stage": 4, "action": "private-ingress", "planSha256": plan_hash, "stageAccepted": False, "ingressObservation": public_observations,
               "legacyOwnershipHandoffs": ownership_handoffs}
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    private_write(directory / "runtime-review.json", json.dumps({"planSha256": plan_hash, **plan, "currentIngressObservationNotPlanBound": observations}, indent=2) + "\n")
    print(json.dumps(summary))
    if operation == "plan":
        return summary
    require(approved == plan_hash, "Private ingress plan changed or was not approved")
    promote_image(config, directory, azure, lock)
    endpoints = {}
    for plane in ("api", "admin"):
        namespace = f"llm-{plane}-ingress"
        scoped = [*kube, "--namespace", namespace]
        material = materials[plane]
        secret = {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/tls", "immutable": True, "metadata": {"name": namespace + "-tls-" + material["sha256"][:16], "namespace": namespace, "labels": {"app.kubernetes.io/managed-by": "llmgw-workflow"}}, "data": {key: base64.b64encode(value.encode()).decode() for key, value in {"tls.crt": material["certificate"], "tls.key": material["key"]}.items()}}
        secret_path = directory / "ingress-tls.json"
        private_write(secret_path, json.dumps(secret))
        try:
            run_command([*scoped, "apply", "--server-side", "--field-manager=llmgw-ingress", "-f", str(secret_path), "-o", "name"], directory, "tls-apply-" + plane)
        finally:
            secret_path.unlink(missing_ok=True)
        for handoff_path in handoffs[plane]:
            run_command([*scoped, "apply", "--server-side", "--field-manager=llmgw-ingress",
                         "--force-conflicts", "-f", str(handoff_path)],
                        directory, "ownership-apply-" + handoff_path.stem)
        run_command([*scoped, "apply", "--server-side", "--field-manager=llmgw-ingress", "-f", str(directory / f"ingress-{plane}.yaml")], directory, "apply-" + plane)
        run_command([*scoped, "rollout", "status", "deployment/" + namespace, "--timeout=15m"], directory, "ready-" + plane)
        run_command([*scoped, "wait", "--for=jsonpath={.status.loadBalancer.ingress[0].ip}", "service/" + namespace, "--timeout=10m"], directory, "lb-ready-" + plane)
        service = json.loads(run_command([*scoped, "get", "service", namespace, "-o", "json"], directory, "service-" + plane))
        addresses = service.get("status", {}).get("loadBalancer", {}).get("ingress", [])
        require(len(addresses) == 1 and "ip" in addresses[0], "Expected one IPv4 private ingress address")
        address = addresses[0]["ip"]
        require(ipaddress.ip_address(address) in ipaddress.ip_network(subnet["prefix"]), "Ingress address is outside the approved subnet")
        endpoints[plane] = frontend_for(config, azure, cluster["nodeResourceGroup"], address)
        verify_endpoint(address, hosts[plane], hosts["admin" if plane == "api" else "api"], material["sha256"])
    require(endpoints["api"]["privateIpAddress"] != endpoints["admin"]["privateIpAddress"], "API/admin must not share an ingress frontend")
    output = {"revision": revision, "configSha256": stage_fingerprint(config, 4), "authenticationMode": authentication_mode, "backendRoutesVerified": False, "verifiedAt": datetime.now(timezone.utc).isoformat(), "image": image, "certificates": public_certificates, **endpoints}
    receipt = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"privateIngress": {"type": "object", "value": output}}}
    receipt_path = directory / "ingress-receipt-template.json"
    private_write(receipt_path, json.dumps(receipt))
    saved = azure.scoped(["deployment", "group", "create", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 4, "private-ingress"), "--mode", "Incremental", "--template-file", str(receipt_path)])
    require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "Private ingress verified, but saving deployment outputs failed")
    summary.update(applied=True, verified=True, backendRoutesVerified=False, endpoints=endpoints)
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    print("Private ingress TLS and cross-plane host rejection verified; backend authentication and full stage acceptance remain separate.")
    return summary