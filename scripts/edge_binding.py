"""Bind managed API and Admin planes to their deployed Front Door before release."""

import copy
import http.client
import ipaddress
import json
import re
import socket
import ssl
from uuid import UUID

import certifi
import yaml

from scripts.customer_migration import fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands, deployment_name, group_id
from scripts.private_ingress import APP_GATEWAY_HEADER, EDGE_HEADERS, FRONT_DOOR_HEADER, NATIVE_API_PATHS, edge_header_bindings, native_admin_rule, native_api_rule, native_health_rule, source_ranges


def front_door_id(deployment):
    if not deployment:
        return None
    values = [item for container in deployment["spec"]["template"]["spec"]["containers"] for item in container.get("env", []) if item.get("name") == "FRONT_DOOR_ID"]
    require(len(values) <= 1, "Duplicate Front Door identity configuration")
    if not values:
        return None
    value = values[0].get("value")
    require(set(values[0]) == {"name", "value"} and isinstance(value, str) and UUID(value).int != 0, "Front Door ID must be an explicit nonzero UUID")
    return value


def bind_deployment(deployment, identifier):
    require(isinstance(identifier, str) and UUID(identifier).int != 0, "Invalid deployed Front Door identity")
    require(deployment["metadata"]["name"] in {"llm-api-proxy", "llm-admin-proxy"}, "Only managed API/Admin proxies can bind Front Door")
    existing = front_door_id(deployment)
    require(existing is None or existing.lower() == identifier.lower(), "Existing proxy trusts another Front Door; review that change separately")
    desired = copy.deepcopy(deployment)
    containers = desired["spec"]["template"]["spec"]["containers"]
    require(len(containers) == 1 and containers[0]["name"] == "auth-proxy", "Unexpected managed API proxy container set")
    if existing is None:
        containers[0].setdefault("env", []).append({"name": "FRONT_DOOR_ID", "value": identifier})
    return desired


def preserve_binding(documents, live):
    live_items = live.values() if isinstance(live, dict) and "metadata" not in live else [live]
    existing = {item["metadata"]["name"]: item for item in live_items if item}
    result = copy.deepcopy(documents)
    for index, item in enumerate(result):
        if item["kind"] != "Deployment":
            continue
        name = item["metadata"]["name"]
        if name in {"llm-api-proxy", "llm-admin-proxy"} and name in existing:
            identifier = front_door_id(existing[name])
            if identifier:
                result[index] = bind_deployment(item, identifier)
    return result


def deployed_resource(azure, resource_id, api_version):
    value = azure.scoped(["resource", "show", "--ids", resource_id, "--api-version", api_version])
    require(value.get("id", "").lower() == resource_id.lower(), "Azure returned a resource outside the reviewed edge scope")
    return value


def probe_enabled_route(host, endpoint_host, plane):
    require(plane in {"api", "admin"}, "Unknown Front Door plane")
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(endpoint_host, 443, type=socket.SOCK_STREAM) if ipaddress.ip_address(item[4][0]).version == 4})
    require(addresses and len(addresses) <= 16, f"Front Door {plane} endpoint has no bounded public IPv4 address set")
    method, path, body, statuses = ("POST", "/v1/responses", b"{}", {400, 401, 403}) if plane == "api" else ("GET", "/fallback/login", b"", {200, 302, 303, 307, 308, 401, 403})
    context = ssl.create_default_context(cafile=certifi.where())
    for address in addresses:
        with socket.create_connection((address, 443), timeout=15) as connection:
            with context.wrap_socket(connection, server_hostname=host) as secured:
                request = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode("ascii") + body
                secured.sendall(request)
                response = http.client.HTTPResponse(secured)
                response.begin()
                headers = {key.lower(): value for key, value in response.getheaders()}
                require(response.status in statuses and bool(headers.get("x-azure-ref")), f"Front Door {plane} route did not reach its reviewed public data plane")


def validate_private_edge_resources(config, azure, profile_resource_id, deployed_origins, edge_output, route_probe=probe_enabled_route):
    edge = config["parameters"]["edge"]
    hosts = {"api": "llm-api." + config["baseDomain"], "admin": "llm-admin." + config["baseDomain"]}
    api_endpoint_id = edge_output["routeId"].rsplit("/routes/", 1)[0]
    admin_endpoint_id = edge_output["adminRouteId"].rsplit("/routes/", 1)[0]
    api_endpoint = deployed_resource(azure, api_endpoint_id, "2025-04-15").get("properties", {})
    admin_endpoint = deployed_resource(azure, admin_endpoint_id, "2025-04-15").get("properties", {})
    require(api_endpoint.get("provisioningState") == "Succeeded" and api_endpoint.get("enforceMtls") in {None, "Disabled"}, "Front Door API endpoint unexpectedly enforces mTLS")
    require(admin_endpoint.get("provisioningState") == "Succeeded" and admin_endpoint.get("enforceMtls") in {None, "Disabled"}, "Front Door Admin endpoint has an unexpected mTLS requirement")
    for plane, route_key, traffic_key in (("api", "routeId", "apiTrafficEnabled"), ("admin", "adminRouteId", "adminTrafficEnabled")):
        route = deployed_resource(azure, edge_output[route_key], "2025-04-15").get("properties", {})
        expected_state = "Enabled" if edge_output.get(traffic_key) is True else "Disabled"
        expected_patterns = sorted(NATIVE_API_PATHS) if plane == "api" else ["/*"]
        expected_domain = profile_resource_id + f"/customDomains/llm-{plane}"
        expected_group = profile_resource_id + f"/originGroups/private-{plane}"
        deployment_status = route.get("deploymentStatus")
        require(route.get("provisioningState") == "Succeeded" and route.get("enabledState") == expected_state, f"Front Door {plane} route is not deployed in the reviewed traffic state")
        if expected_state == "Enabled" and deployment_status == "NotStarted":
            route_probe(hosts[plane], edge_output["endpointHost" if plane == "api" else "adminEndpointHost"], plane)
        else:
            expected_deployment = "Succeeded" if expected_state == "Enabled" else "NotStarted"
            require(deployment_status == expected_deployment, f"Front Door {plane} route is not deployed in the reviewed traffic state")
        require(route.get("supportedProtocols") == ["Https"] and route.get("forwardingProtocol") == "HttpsOnly" and route.get("httpsRedirect") == "Enabled" and route.get("linkToDefaultDomain") == "Disabled", f"Front Door {plane} route exposes an unreviewed protocol or default endpoint")
        require(sorted(route.get("patternsToMatch", [])) == expected_patterns and route.get("ruleSets", []) == [] and route.get("cacheConfiguration") in (None, {}), f"Front Door {plane} route patterns, rules or caching differ from the reviewed contract")
        domains = route.get("customDomains", [])
        require(len(domains) == 1 and domains[0].get("id", "").lower() == expected_domain.lower() and route.get("originGroup", {}).get("id", "").lower() == expected_group.lower(), f"Front Door {plane} route targets an unexpected domain or origin group")
    for plane in ("api", "admin"):
        origin_id = profile_resource_id + f"/originGroups/private-{plane}/origins/private-{plane}"
        origin = deployed_resource(azure, origin_id, "2025-04-15")
        properties = origin.get("properties", {})
        shared = properties.get("sharedPrivateLinkResource", {})
        require(properties.get("provisioningState") == "Succeeded" and properties.get("enabledState") == "Enabled", f"Front Door {plane} private origin is not provisioned and enabled")
        require(properties.get("hostName") == hosts[plane] and properties.get("originHostHeader") == hosts[plane] and properties.get("enforceCertificateNameCheck") is True, f"Front Door {plane} origin TLS identity differs from the reviewed contract")
        traffic_enabled = edge_output.get(f"{plane}TrafficEnabled") is True
        allowed_shared_statuses = {"", "approved"}
        require(shared.get("privateLink", {}).get("id", "").lower() == deployed_origins[plane]["privateLinkServiceId"].lower() and shared.get("privateLinkLocation") == deployed_origins[plane]["privateLinkLocation"] and str(shared.get("status") or "").lower() in allowed_shared_statuses, f"Front Door {plane} private connection is not ready for the reviewed PLS and traffic state")
        service = deployed_resource(azure, deployed_origins[plane]["privateLinkServiceId"], "2024-07-01")
        service_properties = service.get("properties", {})
        active = []
        for connection in service_properties.get("privateEndpointConnections", []):
            status = str(connection.get("properties", {}).get("privateLinkServiceConnectionState", {}).get("status", "")).lower()
            if status in {"approved", "pending"}:
                active.append((status, connection.get("properties", {}).get("privateEndpoint", {}).get("id")))
        require(len(active) == 1 and active[0][0] == "approved" and isinstance(active[0][1], str) and bool(active[0][1]), f"{plane} PLS must have exactly one approved connection and no unexpected pending connection")
        require(service_properties.get("autoApproval", {}).get("subscriptions") == [], f"{plane} PLS must not auto-approve consumers")
        if traffic_enabled and not shared.get("status"):
            route_probe(hosts[plane], edge_output["endpointHost" if plane == "api" else "adminEndpointHost"], plane)
    for plane in ("api", "admin"):
        domain = deployed_resource(azure, profile_resource_id + f"/customDomains/llm-{plane}", "2025-04-15")
        properties = domain.get("properties", {})
        require(properties.get("provisioningState") == "Succeeded" and properties.get("deploymentStatus") == "Succeeded" and properties.get("domainValidationState") == "Approved", f"Front Door {plane} custom domain ownership or edge certificate is not ready")
        require(properties.get("hostName") == hosts[plane] and properties.get("tlsSettings", {}).get("certificateType") == "ManagedCertificate" and properties.get("tlsSettings", {}).get("minimumTlsVersion") == "TLS12" and "mtlsSettings" not in properties, f"Front Door {plane} custom domain TLS differs from the reviewed contract")
    waf = deployed_resource(azure, edge_output["adminWafId"], "2024-02-01").get("properties", {})
    policy = waf.get("policySettings", {})
    require(waf.get("provisioningState") == "Succeeded" and policy.get("enabledState") == "Enabled" and policy.get("mode") == "Prevention" and policy.get("requestBodyCheck") == "Enabled" and policy.get("logScrubbing", {}).get("state") == "Enabled", "Admin WAF source-IP gate, body checks or log scrubbing are not enabled in Prevention mode")
    managed = {(item.get("ruleSetType"), item.get("ruleSetVersion"), item.get("ruleSetAction")) for item in waf.get("managedRules", {}).get("managedRuleSets", [])}
    require(managed == {("Microsoft_DefaultRuleSet", "2.1", "Block"), ("Microsoft_BotManagerRuleSet", "1.1", "Block")}, "Admin WAF managed rule sets or actions differ from the reviewed contract")
    rules = {rule.get("name"): rule for rule in waf.get("customRules", {}).get("rules", [])}
    require(set(rules) == {"BlockUnapprovedAdminSources", "BlockUnsafeMethods", "RateLimitAdmin"}, "Admin WAF contains an unexpected custom rule set")
    allowlist = rules["BlockUnapprovedAdminSources"]
    conditions = allowlist.get("matchConditions", [])
    require(allowlist.get("enabledState") == "Enabled" and allowlist.get("action") == "Block" and allowlist.get("priority") == 5 and len(conditions) == 1, "Admin WAF source-IP rule is disabled or reordered")
    condition = conditions[0]
    require(condition.get("matchVariable") == "SocketAddr" and condition.get("operator") == "IPMatch" and condition.get("negateCondition") is True and sorted(condition.get("matchValue", [])) == sorted(edge["adminAllowedCidrs"]), "Admin WAF source-IP allowlist differs from the customer configuration")
    unsafe = rules["BlockUnsafeMethods"]
    unsafe_conditions = unsafe.get("matchConditions", [])
    require(unsafe.get("enabledState") == "Enabled" and unsafe.get("ruleType") == "MatchRule" and unsafe.get("action") == "Block" and unsafe.get("priority") == 10 and len(unsafe_conditions) == 1, "Admin WAF unsafe-method rule is disabled or reordered")
    unsafe_condition = unsafe_conditions[0]
    require(unsafe_condition.get("matchVariable") == "RequestMethod" and unsafe_condition.get("operator") == "Equal" and unsafe_condition.get("negateCondition", False) is False and set(unsafe_condition.get("matchValue", [])) == {"TRACE", "TRACK"}, "Admin WAF unsafe-method contract changed")
    rate = rules["RateLimitAdmin"]
    rate_conditions = rate.get("matchConditions", [])
    require(rate.get("enabledState") == "Enabled" and rate.get("ruleType") == "RateLimitRule" and rate.get("action") == "Block" and rate.get("priority") == 20 and rate.get("rateLimitDurationInMinutes") == 1 and rate.get("rateLimitThreshold") == edge.get("adminRateLimitPerMinute", 120) and len(rate_conditions) == 1, "Admin WAF rate-limit rule differs from the reviewed contract")
    rate_condition = rate_conditions[0]
    require(rate_condition.get("matchVariable") == "RequestUri" and rate_condition.get("operator") == "BeginsWith" and rate_condition.get("negateCondition", False) is False and rate_condition.get("matchValue") == ["/"], "Admin WAF rate-limit scope changed")


def deployed_edge(config, azure):
    output = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge"), "--query", "{state:properties.provisioningState,edge:properties.outputs.edge.value}"])
    edge = output.get("edge", {})
    require(output.get("state") == "Succeeded" and edge.get("provisioned") is True, "Provision the matching disabled Front Door before binding")
    require(edge.get("apiHost") == "llm-api." + config["baseDomain"] and edge.get("adminHost") == "llm-admin." + config["baseDomain"], "Front Door domains differ from the customer configuration")
    require(edge.get("adminAccessMode") == "SourceIpAllowlistAndNativeLogin", "Admin Front Door does not enforce the reviewed source-IP and native-login access mode")
    prefix = group_id(config) + "/providers/Microsoft.Cdn/profiles/"
    matches = []
    for key in ("routeId", "adminRouteId"):
        match = re.fullmatch(re.escape(prefix) + r"([A-Za-z0-9-]+)/afdEndpoints/([A-Za-z0-9-]+)/routes/[A-Za-z0-9-]+", edge.get(key, ""), re.I)
        require(match is not None, f"Front Door {key} belongs to another target scope")
        matches.append((match[1], match[2]))
    require(matches[0][0].lower() == matches[1][0].lower(), "API and Admin routes must belong to the same reviewed Front Door profile")
    require(matches[0][1].lower() != matches[1][1].lower(), "API and Admin routes must use separate Front Door endpoints")
    endpoint_pattern = r"[a-z0-9-]+\.(?:[a-z0-9-]+\.)?azurefd\.net"
    require(re.fullmatch(endpoint_pattern, edge.get("endpointHost", "")) and re.fullmatch(endpoint_pattern, edge.get("adminEndpointHost", "")) and edge["endpointHost"].lower() != edge["adminEndpointHost"].lower(), "API and Admin must use distinct deployed Front Door endpoint hosts")
    resource_id = prefix + matches[0][0]
    profile = deployed_resource(azure, resource_id, "2025-04-15")
    identifier = edge.get("profileId")
    require(isinstance(identifier, str) and UUID(identifier).int != 0 and profile.get("properties", {}).get("frontDoorId") == identifier, "Actual Front Door identity differs from deployment outputs")
    configured_edge = config["parameters"]["edge"]
    deployed_origins = {"api": edge.get("privateOrigin"), "admin": edge.get("adminPrivateOrigin")}
    prefix_pls = group_id(config).lower() + "/providers/microsoft.network/privatelinkservices/"
    for plane, parameter in (("api", "privateOrigin"), ("admin", "adminPrivateOrigin")):
        origin = deployed_origins[plane]
        configured_origin = configured_edge[parameter]
        require(isinstance(origin, dict) and origin.get("privateLinkServiceId", "").lower().startswith(prefix_pls) and origin.get("privateLinkLocation") == configured_origin["privateLinkLocation"], f"Deployed {plane} private origin differs from the approved target scope")
        require(configured_origin["privateLinkServiceId"] == "auto" or origin == configured_origin, f"Deployed {plane} private origin differs from the explicit customer configuration")
    require(deployed_origins["api"]["privateLinkServiceId"].lower() != deployed_origins["admin"]["privateLinkServiceId"].lower(), "Deployed API and Admin origins reuse one Private Link Service")
    require(edge.get("adminAllowedCidrs") == configured_edge["adminAllowedCidrs"], "Deployed Admin source-IP allowlist differs from the customer configuration")
    validate_private_edge_resources(config, azure, resource_id, deployed_origins, edge)
    return {"profileResourceId": resource_id, "frontDoorId": identifier, "apiHost": edge["apiHost"], "adminHost": edge["adminHost"], "endpointHost": edge["endpointHost"], "adminEndpointHost": edge["adminEndpointHost"], "routeId": edge["routeId"], "adminRouteId": edge["adminRouteId"], "adminWafId": edge["adminWafId"], "adminAccessMode": edge["adminAccessMode"], "privateOrigin": deployed_origins["api"], "adminPrivateOrigin": deployed_origins["admin"], "adminAllowedCidrs": edge["adminAllowedCidrs"], "adminRateLimitPerMinute": edge["adminRateLimitPerMinute"]}


def deployed_public_edge(config, azure):
    """Live-validated public edge, Front Door or Application Gateway, with its identity header."""
    from scripts.app_gateway import deployed_app_gateway, is_app_gateway
    if is_app_gateway(config):
        edge = deployed_app_gateway(config, azure)
        return edge, edge["edgeId"], APP_GATEWAY_HEADER
    edge = deployed_edge(config, azure)
    return edge, edge["frontDoorId"], FRONT_DOOR_HEADER


def edge_label(header):
    return "Front Door" if header == FRONT_DOOR_HEADER else "Application Gateway edge"


def bound_edge(state):
    if state.get("frontDoorHeaderBound"):
        return FRONT_DOOR_HEADER, state["frontDoorHeaderBound"]
    if state.get("edgeHeaderBound"):
        return APP_GATEWAY_HEADER, state["edgeHeaderBound"]
    return None


def native_ingress_state(config, client, identifier=None, plane="api", header=FRONT_DOOR_HEADER):
    require(plane in {"api", "admin"}, "Unknown native ingress plane")
    name = f"llm-{plane}-ingress"
    config_map = client.get("configmap", name)
    deployment = client.get("deployment", name)
    service = client.get("service", name)
    return native_ingress_document_state(config, config_map, deployment, service, identifier, plane, header)


def native_ingress_document_state(config, config_map, deployment, service, identifier=None, plane="api", header=FRONT_DOOR_HEADER):
    require(plane in {"api", "admin"}, "Unknown native ingress plane")
    for item in (config_map, deployment, service):
        require(item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/managed-by") == "llmgw-workflow", f"Native {plane} ingress must remain workflow managed")
    dynamic = yaml.safe_load(config_map.get("data", {}).get("routes.yaml", ""))
    require(isinstance(dynamic, dict), "Native API ingress route configuration is missing")
    http = dynamic.get("http", {})
    routers = http.get("routers", {})
    health_name = f"{plane}-health"
    health_path = f"{plane}-health-path"
    require(set(routers) == {plane, health_name}, f"Native {plane} ingress contains an unexpected router")
    business = routers[plane]
    health = routers[health_name]
    rule = business.get("rule", "")
    bound = edge_header_bindings(rule)
    require(len(bound) <= 1 and (not bound or UUID(bound[0][1]).int != 0), f"Native {plane} ingress has an invalid {edge_label(bound[0][0]) if bound else 'Front Door'} header binding")
    host = f"llm-{plane}." + config["baseDomain"]
    if identifier is not None:
        require(bound and bound[0][0] == header and bound[0][1].lower() == identifier.lower(), f"Native {plane} ingress trusts another or no {edge_label(header)}")
    renderer = native_api_rule if plane == "api" else native_admin_rule
    expected_rule = renderer(host, bound[0][1], bound[0][0]) if bound else renderer(host)
    require(business == {"rule": expected_rule, "entryPoints": ["websecure"], "service": plane, "tls": {}}, f"Native {plane} ingress business route differs from the reviewed contract")
    require(health == {"rule": native_health_rule(host), "entryPoints": ["websecure"], "service": plane, "middlewares": [health_path], "tls": {}}, f"Native {plane} ingress health router differs from the reviewed contract")
    require(http.get("middlewares") == {health_path: {"replacePath": {"path": "/health/readiness"}}}, f"Native {plane} ingress middleware differs from the reviewed contract")
    from scripts.optional_l3 import upstream
    require(http.get("services") == {plane: {"loadBalancer": {"servers": [{"url": upstream(config, plane)}], "passHostHeader": True}}}, f"Native {plane} ingress targets an unexpected service")
    service_spec = service.get("spec", {})
    expected_sources = source_ranges([*config["privateIngress"][plane]["allowedCidrs"], config["parameters"]["platform"]["stage4Network"]["ingressSubnetPrefix"]])
    expected_labels = {"app.kubernetes.io/name": "llmgw-ingress", "app.kubernetes.io/component": "controller", "app.kubernetes.io/managed-by": "llmgw-workflow", "plane": plane}
    annotations = service.get("metadata", {}).get("annotations", {})
    ports = service_spec.get("ports", [])
    port_contract = len(ports) == 1 and all(ports[0].get(key) == value for key, value in {"name": "https", "port": 443, "targetPort": "https", "protocol": "TCP"}.items())
    service_contract = (
        service_spec.get("type") == "LoadBalancer"
        and service_spec.get("selector") == expected_labels
        and service_spec.get("externalTrafficPolicy") == "Local"
        and service_spec.get("loadBalancerSourceRanges") == expected_sources
        and not service_spec.get("externalIPs")
        and not service_spec.get("loadBalancerIP")
        and not service_spec.get("loadBalancerClass")
        and port_contract
        and annotations.get("service.beta.kubernetes.io/azure-load-balancer-internal") == "true"
        and annotations.get("service.beta.kubernetes.io/azure-load-balancer-internal-subnet") == config["parameters"]["platform"]["stage4Network"]["ingressSubnetName"]
    )
    require(service_contract, f"Native {plane} ingress Service differs from the reviewed private routing contract")
    labels = deployment.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
    require(labels == expected_labels, "Native API ingress deployment labels changed")
    pod = deployment["spec"]["template"]["spec"]
    dynamic_volumes = [item for item in pod.get("volumes", []) if item.get("name") == "dynamic"]
    dynamic_volume = dynamic_volumes[0] if len(dynamic_volumes) == 1 else {}
    config_map_source = dynamic_volume.get("configMap", {})
    volume_contract = (
        set(dynamic_volume) == {"name", "configMap"}
        and isinstance(config_map_source, dict)
        and set(config_map_source).issubset({"name", "defaultMode", "optional"})
        and config_map_source.get("name") == f"llm-{plane}-ingress"
        and config_map_source.get("defaultMode", 420) == 420
        and config_map_source.get("optional", False) is False
    )
    require(volume_contract, f"Native {plane} ingress does not mount the reviewed route ConfigMap")
    containers = pod.get("containers", [])
    require(len(containers) == 1 and containers[0].get("name") == "traefik" and {"name": "dynamic", "mountPath": "/dynamic", "readOnly": True} in containers[0].get("volumeMounts", []), "Native API ingress controller does not read the reviewed route ConfigMap")
    state = {
        "bindingMode": "native-private-ingress",
        "configMapUid": config_map["metadata"]["uid"],
        "deploymentUid": deployment["metadata"]["uid"],
        "serviceUid": service["metadata"]["uid"],
        "serviceSpecSha256": fingerprint(service["spec"]),
        "podTemplateSha256": fingerprint(deployment["spec"]["template"]),
        "routeConfigSha256": fingerprint(dynamic),
        "frontDoorHeaderBound": bound[0][1] if bound and bound[0][0] == FRONT_DOOR_HEADER else None,
    }
    if bound and bound[0][0] == APP_GATEWAY_HEADER:
        state["edgeHeaderBound"] = bound[0][1]
    return state


def bind_native_ingress(config, client, identifier, plane="api", header=FRONT_DOOR_HEADER):
    require(plane in {"api", "admin"} and header in EDGE_HEADERS, "Unknown native ingress plane or edge header")
    require(isinstance(identifier, str) and UUID(identifier).int != 0, f"Invalid deployed {edge_label(header)} identity")
    name = f"llm-{plane}-ingress"
    config_map = client.get("configmap", name)
    deployment = client.get("deployment", name)
    service = client.get("service", name)
    current = native_ingress_document_state(config, config_map, deployment, service, plane=plane)
    existing = bound_edge(current)
    # Switching edge type replaces the other type's binding; a different identity of the same type is refused.
    require(existing is None or existing[0] != header or existing[1].lower() == identifier.lower(), f"Existing native {plane} ingress trusts another {edge_label(header)}")
    desired_config_map = copy.deepcopy(config_map)
    desired_deployment = copy.deepcopy(deployment)
    dynamic = yaml.safe_load(desired_config_map["data"]["routes.yaml"])
    if existing is None or existing[0] != header:
        renderer = native_api_rule if plane == "api" else native_admin_rule
        dynamic["http"]["routers"][plane]["rule"] = renderer(f"llm-{plane}." + config["baseDomain"], identifier, header)
    desired_config_map["data"]["routes.yaml"] = yaml.safe_dump(dynamic, sort_keys=False)
    annotations = desired_deployment["spec"]["template"]["metadata"].setdefault("annotations", {})
    for other in set(EDGE_HEADERS.values()) - {EDGE_HEADERS[header]}:
        annotations.pop(other, None)
    annotations[EDGE_HEADERS[header]] = identifier
    annotations["llmgw/routes-sha256"] = fingerprint(dynamic)
    desired = native_ingress_document_state(config, desired_config_map, desired_deployment, service, identifier, plane, header)
    return config_map, deployment, desired_config_map, desired_deployment, current, desired


def bind_edge(config, operation, revision, directory, approved, azure=None, client=None):
    from scripts.audit_runtime import AuditCluster
    from scripts.migration_runtime import connect_cluster, validate_action

    require(operation in {"plan", "execute"}, "Invalid edge binding operation")
    validate_action(config, 9, "edge-bind")
    azure = azure or AzureCommands(config, directory)
    edge, identifier, header = deployed_public_edge(config, azure)
    from scripts.backend_manifest import application_authentication
    native = application_authentication(config)["mode"] == "native"
    if not native:
        from scripts.app_gateway import require_front_door_edge
        require_front_door_edge(config, "Entra proxy edge binding")
    if client is None:
        kube = connect_cluster(config, directory, legacy=False)
        if native and kube[-2:] == ["--namespace", "litellm"]:
            kube = kube[:-2]
        client = {plane: AuditCluster([*kube, "--namespace", f"llm-{plane}-ingress"], directory) for plane in ("api", "admin")} if native else AuditCluster(kube, directory)
    if native:
        require(isinstance(client, dict) and set(client) == {"api", "admin"}, "Native edge binding requires isolated API and Admin clients")
        scope = {"stage": 9, "action": "edge-bind", "revision": revision, "configSha256": stage_fingerprint(config, 9),
                 "clusterResourceId": group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/" + config["parameters"]["platform"]["stage4Aks"]["name"], **edge}
        changes = {}
        for plane in ("api", "admin"):
            current_config_map, current_deployment, desired_config_map, desired_deployment, current_state, desired_state = bind_native_ingress(config, client[plane], identifier, plane, header)
            changes[plane] = {
                "currentConfigMap": current_config_map,
                "currentDeployment": current_deployment,
                "desiredConfigMap": desired_config_map,
                "desiredDeployment": desired_deployment,
                "plan": {"configMapUid": current_config_map["metadata"]["uid"], "deploymentUid": current_deployment["metadata"]["uid"], "serviceUid": desired_state["serviceUid"], "currentRouteConfigSha256": current_state["routeConfigSha256"], "currentPodTemplateSha256": current_state["podTemplateSha256"], "desiredRouteConfigSha256": desired_state["routeConfigSha256"], "desiredPodTemplateSha256": desired_state["podTemplateSha256"]},
            }
            replaced = bound_edge(current_state)
            if replaced and replaced[0] != header:
                changes[plane]["plan"]["replacesEdgeHeader"] = replaced[0]
        plan = {**scope, "bindingMode": "native-private-ingress", "planes": {plane: changes[plane]["plan"] for plane in ("api", "admin")}}
        digest = fingerprint(plan)
        summary = {**scope, "bindingMode": "native-private-ingress", "planSha256": digest, "applied": False, "stageAccepted": False, "trafficVerified": False}
        private_write(directory / "runtime-review.json", json.dumps(plan, indent=2))
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
        if operation == "plan":
            return summary
        require(digest == approved, "Edge binding plan changed or was not approved")
        for plane in ("api", "admin"):
            name = f"llm-{plane}-ingress"
            change = changes[plane]
            plane_plan = plan["planes"][plane]
            observed_config_map = client[plane].get("configmap", name)
            observed_deployment = client[plane].get("deployment", name)
            require(observed_config_map["metadata"]["uid"] == plane_plan["configMapUid"] and observed_config_map["data"] == change["currentConfigMap"]["data"] and observed_deployment["metadata"]["uid"] == plane_plan["deploymentUid"] and observed_deployment["spec"] == change["currentDeployment"]["spec"], f"Native {plane} ingress changed during approval")
            if observed_config_map["data"] != change["desiredConfigMap"]["data"]:
                client[plane].patch("configmap", name, [{"op": "test", "path": "/metadata/uid", "value": plane_plan["configMapUid"]}, {"op": "test", "path": "/data", "value": observed_config_map["data"]}, {"op": "replace", "path": "/data/routes.yaml", "value": change["desiredConfigMap"]["data"]["routes.yaml"]}])
            if observed_deployment["spec"] != change["desiredDeployment"]["spec"]:
                operations = [{"op": "test", "path": "/metadata/uid", "value": plane_plan["deploymentUid"]},
                              {"op": "test", "path": "/spec", "value": observed_deployment["spec"]}]
                observed_annotations = observed_deployment["spec"]["template"]["metadata"].get("annotations")
                if observed_annotations is None:
                    operations.append({"op": "add", "path": "/spec/template/metadata/annotations", "value": {}})
                annotations = change["desiredDeployment"]["spec"]["template"]["metadata"]["annotations"]
                for key in sorted(set(EDGE_HEADERS.values()) - set(annotations)):
                    if key in (observed_annotations or {}):
                        operations.append({"op": "remove", "path": "/spec/template/metadata/annotations/" + key.replace("/", "~1")})
                for key in (EDGE_HEADERS[header], "llmgw/routes-sha256"):
                    operations.append({"op": "add", "path": "/spec/template/metadata/annotations/" + key.replace("/", "~1"),
                                       "value": annotations[key]})
                client[plane].patch("deployment", name, operations)
            client[plane].run(["rollout", "status", f"deployment/{name}", "--timeout=15m"])
        after = {plane: native_ingress_state(config, client[plane], identifier, plane, header) for plane in ("api", "admin")}
        for plane, state in after.items():
            plane_plan = plan["planes"][plane]
            require(state["deploymentUid"] == plane_plan["deploymentUid"] and state["serviceUid"] == plane_plan["serviceUid"] and state["routeConfigSha256"] == plane_plan["desiredRouteConfigSha256"] and state["podTemplateSha256"] == plane_plan["desiredPodTemplateSha256"], f"Native {plane} ingress changed during rollout verification")
        receipt = {**scope, "bindingMode": "native-private-ingress", "planes": after, "rolloutVerified": True}
        template = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"edgeBinding": {"type": "object", "value": receipt}}}
        path = directory / "edge-binding-receipt.json"
        private_write(path, json.dumps(template))
        saved = azure.scoped(["deployment", "group", "create", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge-bind"), "--mode", "Incremental", "--template-file", str(path)])
        require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "Native ingresses verified but saving the binding receipt failed")
        summary.update(applied=True, rolloutVerified=True)
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
        return summary
    workloads = {}
    for plane in ("api", "admin"):
        name = f"llm-{plane}-proxy"
        current = client.get("deployment", name)
        desired = bind_deployment(current, identifier)
        pod = desired["spec"]["template"]["spec"]
        require(pod.get("serviceAccountName") == name and pod["containers"][0].get("image") == config.get("proxy", {}).get("image"), f"Bind only the approved managed {plane} workload and image")
        workloads[plane] = {"current": current, "desired": desired}
    scope = {"stage": 9, "action": "edge-bind", "revision": revision, "configSha256": stage_fingerprint(config, 9),
             "clusterResourceId": group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/" + config["parameters"]["platform"]["stage4Aks"]["name"], **edge}
    plan = {**scope, "planes": {plane: {"deploymentUid": workloads[plane]["current"]["metadata"]["uid"], "currentSpecSha256": fingerprint(workloads[plane]["current"]["spec"]), "desiredSpecSha256": fingerprint(workloads[plane]["desired"]["spec"])} for plane in ("api", "admin")}}
    digest = fingerprint(plan)
    summary = {**scope, "planSha256": digest, "applied": False, "stageAccepted": False, "trafficVerified": False}
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2))
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    if operation == "plan":
        return summary
    require(digest == approved, "Edge binding plan changed or was not approved")
    for plane in ("api", "admin"):
        name = f"llm-{plane}-proxy"
        current = workloads[plane]["current"]
        desired = workloads[plane]["desired"]
        if current["spec"] != desired["spec"]:
            client.patch("deployment", name, [{"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]}, {"op": "test", "path": "/spec", "value": current["spec"]}, {"op": "replace", "path": "/spec", "value": desired["spec"]}])
        client.run(["rollout", "status", f"deployment/{name}", "--timeout=15m"])
    after = {plane: client.get("deployment", f"llm-{plane}-proxy") for plane in ("api", "admin")}
    for plane, deployment in after.items():
        require(deployment["metadata"]["uid"] == plan["planes"][plane]["deploymentUid"] and deployment["spec"] == workloads[plane]["desired"]["spec"] and front_door_id(deployment) == edge["frontDoorId"], f"{plane} binding changed during rollout; do not release traffic")
    receipt = {**scope, "planes": {plane: {"deploymentUid": after[plane]["metadata"]["uid"], "podTemplateSha256": fingerprint(after[plane]["spec"]["template"])} for plane in ("api", "admin")}, "rolloutVerified": True}
    template = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"edgeBinding": {"type": "object", "value": receipt}}}
    path = directory / "edge-binding-receipt.json"
    private_write(path, json.dumps(template))
    saved = azure.scoped(["deployment", "group", "create", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge-bind"), "--mode", "Incremental", "--template-file", str(path)])
    require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "Proxy bindings applied but receipt storage failed; replan before release")
    summary.update(applied=True, rolloutVerified=True)
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    return summary


def require_edge_binding(config, revision, identifier, azure, client=None):
    result = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge-bind"), "--query", "{state:properties.provisioningState,binding:properties.outputs.edgeBinding.value}"])
    receipt = result.get("binding", {})
    require(result.get("state") == "Succeeded" and receipt.get("rolloutVerified") is True, "Run edge-bind and verify its rollout before enabling traffic")
    current_edge, current_identifier, header = deployed_public_edge(config, azure)
    require(current_identifier == identifier, f"Current {edge_label(header)} identity differs from the release")
    from scripts.app_gateway import RECEIPT_KEYS
    identity_key, edge_keys, state_keys = ("frontDoorId", ("profileResourceId", "endpointHost", "adminEndpointHost", "routeId", "adminRouteId", "adminWafId", "privateOrigin", "adminPrivateOrigin", "adminAllowedCidrs", "adminRateLimitPerMinute"), ("frontDoorHeaderBound",)) if header == FRONT_DOOR_HEADER else ("edgeId", RECEIPT_KEYS, ("frontDoorHeaderBound", "edgeHeaderBound"))
    expected = {"revision": revision, "configSha256": stage_fingerprint(config, 9), identity_key: identifier,
                "apiHost": "llm-api." + config["baseDomain"], "adminHost": "llm-admin." + config["baseDomain"],
                "adminAccessMode": "SourceIpAllowlistAndNativeLogin",
                "clusterResourceId": group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/" + config["parameters"]["platform"]["stage4Aks"]["name"]}
    require(all(receipt.get(key) == value for key, value in expected.items()), "Edge binding receipt is stale or belongs to a different environment")
    for key in edge_keys:
        require(receipt.get(key) == current_edge.get(key), f"Edge binding receipt differs from the current {edge_label(header)} and private-origin configuration")
    from scripts.backend_manifest import application_authentication
    if application_authentication(config)["mode"] == "native":
        planes = receipt.get("planes", {})
        require(receipt.get("bindingMode") == "native-private-ingress" and isinstance(planes, dict) and set(planes) == {"api", "admin"}, "Native edge binding receipt lacks both verified private ingresses")
        require(isinstance(client, dict) and set(client) == {"api", "admin"}, "Native traffic release requires live access to both private ingresses")
        for plane in ("api", "admin"):
            state = planes[plane]
            require(isinstance(state.get("serviceUid"), str) and re.fullmatch(r"[a-f0-9]{64}", state.get("routeConfigSha256", "")) and re.fullmatch(r"[a-f0-9]{64}", state.get("serviceSpecSha256", "")), f"Native edge binding receipt lacks the verified private {plane} ingress")
            live = native_ingress_state(config, client[plane], identifier, plane, header)
            for key in ("configMapUid", "deploymentUid", "serviceUid", "serviceSpecSha256", "podTemplateSha256", "routeConfigSha256", *state_keys):
                require(state.get(key) == live.get(key), f"Native {plane} ingress differs from the verified edge binding receipt")
    else:
        planes = receipt.get("planes", {})
        require(isinstance(planes, dict) and set(planes) == {"api", "admin"}, "Edge binding receipt lacks both verified proxy workloads")
        require(client is not None, "Proxy traffic release requires live access to both managed proxy workloads")
        for plane in ("api", "admin"):
            state = planes[plane]
            require(isinstance(state.get("deploymentUid"), str) and bool(state["deploymentUid"]) and re.fullmatch(r"[a-f0-9]{64}", state.get("podTemplateSha256", "")), f"Edge binding receipt lacks the verified {plane} workload")
            name = f"llm-{plane}-proxy"
            live = client.get("deployment", name)
            pod = live.get("spec", {}).get("template", {}).get("spec", {})
            containers = pod.get("containers", [])
            require(live.get("metadata", {}).get("uid") == state["deploymentUid"] and fingerprint(live["spec"]["template"]) == state["podTemplateSha256"], f"Managed {plane} proxy differs from the verified edge binding receipt")
            require(pod.get("serviceAccountName") == name and len(containers) == 1 and containers[0].get("name") == "auth-proxy" and containers[0].get("image") == config.get("proxy", {}).get("image") and front_door_id(live) == identifier, f"Managed {plane} proxy no longer enforces the reviewed Front Door binding")