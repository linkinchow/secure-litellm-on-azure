"""Optional Stage 9 public entry through Application Gateway WAF_v2 instead of Front Door."""

import ipaddress
import re
from uuid import UUID

from scripts.customer_migration import configured, fingerprint, require

EDGE_TYPES = ("frontDoor", "applicationGateway")
FRONT_DOOR_HEADER = "X-Azure-FDID"
APP_GATEWAY_HEADER = "X-LLMGW-Edge-Id"
APP_GATEWAY_REQUIRED = {"type", "adminAllowedCidrs", "logAnalyticsWorkspaceName", "subnetName", "subnetPrefix"}
APP_GATEWAY_OPTIONAL = {"rateLimitPerMinute", "adminRateLimitPerMinute", "availabilityZones", "minCapacity", "maxCapacity", "requestTimeoutSeconds", "wafExcludePromptFields"}
APP_GATEWAY_ONLY = {"subnetName", "subnetPrefix", "availabilityZones", "minCapacity", "maxCapacity", "requestTimeoutSeconds", "wafExcludePromptFields"}
FRONT_DOOR_ONLY = {"privateOrigin", "adminPrivateOrigin"}
DEFAULTS = {"rateLimitPerMinute": 600, "adminRateLimitPerMinute": 120, "availabilityZones": ["1", "2", "3"], "minCapacity": 2, "maxCapacity": 10, "requestTimeoutSeconds": 900, "wafExcludePromptFields": False}
SSL_POLICY = "AppGwSslPolicy20220101S"
# Public IP names with domainNameLabelScope carry a hash label and, in some regions, a sysgen label.
FQDN_PATTERN = r"[a-z0-9-]+(?:\.[a-z0-9]+)?\.{location}(?:\.sysgen)?\.cloudapp\.azure\.com"


def edge_type(config):
    edge = config.get("parameters", {}).get("edge") or {}
    value = edge.get("type", "frontDoor") if isinstance(edge, dict) else "frontDoor"
    require(value in EDGE_TYPES, "edge.type must be frontDoor or applicationGateway")
    return value


def is_app_gateway(config):
    return edge_type(config) == "applicationGateway"


def require_front_door_edge(config, feature):
    require(not is_app_gateway(config), f"{feature} is not supported for applicationGateway edge yet")


def gateway_vault_name(config):
    environment = config["environment"]
    scope = f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{config['target']['resourceGroup']}".lower()
    return f"kvagw-{environment}-" + fingerprint(scope)[:24 - len(environment) - 7]


def names(config):
    environment = config["environment"]
    return {
        "vault": gateway_vault_name(config),
        "gateway": f"agw-llm-{environment}", "publicIp": f"pip-agw-llm-{environment}",
        "identity": f"id-agw-llm-{environment}", "nsg": f"nsg-agw-llm-{environment}",
        "apiWaf": f"wafagw-llm-api-{environment}", "adminWaf": f"wafagw-llm-admin-{environment}",
        "gateWaf": f"wafagw-llm-gate-{environment}", "diagnostics": "edge-security-and-health",
    }


def certificate_secret_names():
    return {"api": "api-tls-pfx", "admin": "admin-tls-pfx"}


def virtual_network_prefix(config):
    owner = config["parameters"].get("network") or config["parameters"].get("backup") or {}
    value = owner.get("virtualNetworkAddressPrefix")
    return ipaddress.ip_network(value) if isinstance(value, str) and configured(value) else None


def app_gateway_settings(config):
    """Validate the applicationGateway edge; returns the edge settings with defaults applied."""
    edge = config["parameters"].get("edge")
    require(isinstance(edge, dict) and edge.get("type") == "applicationGateway", "Application Gateway edge requires parameters.edge.type=applicationGateway")
    require(not set(edge) - APP_GATEWAY_REQUIRED - APP_GATEWAY_OPTIONAL, "Application Gateway edge contains Front Door or unknown fields")
    require(APP_GATEWAY_REQUIRED.issubset(edge), "Application Gateway edge requires adminAllowedCidrs, logAnalyticsWorkspaceName, subnetName and subnetPrefix")
    require("origin" not in config["parameters"], "Application Gateway edge reaches the private ingress directly; remove parameters.origin (Private Link Services are Front Door only)")
    require(config.get("application", {}).get("authentication", {}).get("mode") == "native", "Application Gateway edge currently supports native gateway authentication only")
    require("privateIngress" in config and "certificate-vault" in config["parameters"], "Application Gateway edge requires privateIngress and the certificate Vault")
    settings = {**DEFAULTS, **edge}
    require(isinstance(settings["subnetName"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,78}[A-Za-z0-9_]", settings["subnetName"]) is not None, "Invalid Application Gateway subnet name")
    try:
        subnet = ipaddress.ip_network(settings["subnetPrefix"], strict=True)
    except (TypeError, ValueError):
        raise ValueError("Application Gateway subnetPrefix must be an IPv4 CIDR") from None
    require(subnet.version == 4 and 16 <= subnet.prefixlen <= 26, "Application Gateway subnet must be IPv4 between /16 and /26 (/24 recommended)")
    parent = virtual_network_prefix(config)
    require(parent is None or subnet.subnet_of(parent), "Application Gateway subnet must be inside the target VNet")
    network = config["parameters"].get("platform", {}).get("stage4Network", {})
    used = [network.get(key) for key in ("firewallSubnetPrefix", "systemSubnetPrefix", "userSubnetPrefix", "ingressSubnetPrefix")]
    owner = config["parameters"].get("network") or config["parameters"].get("backup") or {}
    used.append(owner.get("privateEndpointSubnetPrefix"))
    for value in used:
        if isinstance(value, str) and configured(value):
            require(not subnet.overlaps(ipaddress.ip_network(value)), "Application Gateway subnet overlaps another target subnet")
    for plane in ("api", "admin"):
        sources = [ipaddress.ip_network(value) for value in config["privateIngress"][plane]["allowedCidrs"]]
        require(any(subnet.subnet_of(source) for source in sources), f"privateIngress.{plane}.allowedCidrs must include the Application Gateway subnet")
    zones = settings["availabilityZones"]
    require(isinstance(zones, list) and all(isinstance(zone, str) for zone in zones) and set(zones) <= {"1", "2", "3"} and len(set(zones)) == len(zones), "Application Gateway zones must be unique values from 1, 2 and 3")
    settings["availabilityZones"] = sorted(zones)
    minimum, maximum = settings["minCapacity"], settings["maxCapacity"]
    require(type(minimum) is int and type(maximum) is int and 0 <= minimum <= maximum <= 125 and maximum >= 2, "Application Gateway capacity must satisfy 0 <= min <= max <= 125 and max >= 2")
    require(type(settings["requestTimeoutSeconds"]) is int and 60 <= settings["requestTimeoutSeconds"] <= 86400, "Application Gateway request timeout must be 60 to 86400 seconds")
    require(type(settings["wafExcludePromptFields"]) is bool, "wafExcludePromptFields must be a boolean")
    require(type(settings["rateLimitPerMinute"]) is int and 1 <= settings["rateLimitPerMinute"] <= 100000 and type(settings["adminRateLimitPerMinute"]) is int and 1 <= settings["adminRateLimitPerMinute"] <= 10000, "Invalid edge rate limits")
    return settings


def validate_edge_type(config):
    """Configuration-time checks for both edge types; value checks wait until fields are configured."""
    edge = config["parameters"].get("edge")
    if edge is None:
        return
    kind = edge_type(config)
    if kind == "frontDoor":
        require(not set(edge) & APP_GATEWAY_ONLY, "Application Gateway fields require edge.type=applicationGateway")
        return
    require(not set(edge) & FRONT_DOOR_ONLY, "Application Gateway edge cannot contain Front Door private origins")
    if configured(edge) and "privateIngress" in config:
        app_gateway_settings(config)


def template_parameters(config):
    """Bicep parameters for infra/edge-appgw before deployment-time resolution."""
    settings = app_gateway_settings(config)
    from scripts.customer_migration import admin_source_cidrs
    network = config["parameters"]["platform"]["stage4Network"]
    secrets = certificate_secret_names()
    return {
        "adminAllowedCidrs": admin_source_cidrs(settings["adminAllowedCidrs"]),
        "logAnalyticsWorkspaceName": settings["logAnalyticsWorkspaceName"],
        "rateLimitPerMinute": settings["rateLimitPerMinute"], "adminRateLimitPerMinute": settings["adminRateLimitPerMinute"],
        "virtualNetworkName": network["virtualNetworkName"], "subnetName": settings["subnetName"], "subnetPrefix": settings["subnetPrefix"],
        "availabilityZones": settings["availabilityZones"], "minCapacity": settings["minCapacity"], "maxCapacity": settings["maxCapacity"],
        "requestTimeoutSeconds": settings["requestTimeoutSeconds"], "wafExcludePromptFields": settings["wafExcludePromptFields"],
        "gatewayVaultName": gateway_vault_name(config), "privateEndpointSubnetName": network["privateEndpointSubnetName"],
        "certificateWriterPrincipalId": config["parameters"]["certificate-vault"]["certificateImporterPrincipalId"],
        "certificateWriterPrincipalType": config["parameters"]["certificate-vault"]["certificateImporterPrincipalType"],
        "apiCertificateSecretName": secrets["api"], "adminCertificateSecretName": secrets["admin"],
        "backendAddresses": {"api": "", "admin": ""}, "networkIsolation": False, "deployGateway": False,
    }


def resource_prefix(config):
    return f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{config['target']['resourceGroup']}/providers/"


def resource_ids(config):
    settings = app_gateway_settings(config)
    prefix = resource_prefix(config)
    resource = names(config)
    gateway = prefix + "Microsoft.Network/applicationGateways/" + resource["gateway"]
    network = config["parameters"]["platform"]["stage4Network"]["virtualNetworkName"]
    vault = prefix + "Microsoft.KeyVault/vaults/" + resource["vault"]
    endpoint = prefix + "Microsoft.Network/privateEndpoints/pe-" + resource["vault"] + "-vault"
    return {value.lower() for value in (
        vault, vault + "/providers/Microsoft.Insights/diagnosticSettings/send-key-vault-audit-to-log-analytics",
        endpoint, endpoint + "/privateDnsZoneGroups/default", prefix + "Microsoft.Network/networkInterfaces/nic-pe-" + resource["vault"] + "-vault",
        prefix + f"Microsoft.Network/virtualNetworks/{network}/subnets/{settings['subnetName']}",
        prefix + "Microsoft.Network/networkSecurityGroups/" + resource["nsg"],
        prefix + "Microsoft.Network/publicIPAddresses/" + resource["publicIp"],
        prefix + "Microsoft.ManagedIdentity/userAssignedIdentities/" + resource["identity"],
        *(prefix + "Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies/" + resource[key] for key in ("apiWaf", "adminWaf", "gateWaf")),
        gateway, gateway + "/providers/Microsoft.Insights/diagnosticSettings/" + resource["diagnostics"],
    )}


def change_allowed(config, resource):
    """True when a What-if resource ID belongs to the Application Gateway edge contract."""
    resource = resource.lower()
    if resource in resource_ids(config):
        return True
    assignment = resource_prefix(config).lower() + "microsoft.keyvault/vaults/" + names(config)["vault"] + "/providers/microsoft.authorization/roleassignments/"
    return resource.startswith(assignment) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", resource.removeprefix(assignment)) is not None


def inspect_prerequisites(config, azure):
    """Read-only checks before planning; returns deployment-time template values."""
    settings = app_gateway_settings(config)
    group = config["target"]["resourceGroup"]
    network = config["parameters"]["platform"]["stage4Network"]["virtualNetworkName"]
    feature = azure.scoped(["feature", "show", "--namespace", "Microsoft.Network", "--name", "EnableApplicationGatewayNetworkIsolation", "--query", "properties.state"])
    vnet = azure.scoped(["network", "vnet", "show", "--resource-group", group, "--name", network, "--query", "{prefixes:addressSpace.addressPrefixes,subnets:subnets[].{name:name,prefix:addressPrefix,prefixes:addressPrefixes}}"])
    subnet = ipaddress.ip_network(settings["subnetPrefix"])
    require(any(subnet.subnet_of(ipaddress.ip_network(value)) for value in vnet.get("prefixes") or []), "Application Gateway subnet is outside the deployed VNet address space")
    for item in vnet.get("subnets") or []:
        prefixes = item.get("prefixes") or [item.get("prefix")]
        if item.get("name") == settings["subnetName"]:
            require(prefixes == [settings["subnetPrefix"]], "Existing Application Gateway subnet has a different prefix; subnet size cannot change in place")
            continue
        require(not any(value and subnet.overlaps(ipaddress.ip_network(value)) for value in prefixes), f"Application Gateway subnet overlaps deployed subnet {item.get('name')}")
    vault_name = names(config)["vault"]
    vault = resource_prefix(config) + "Microsoft.KeyVault/vaults/" + vault_name
    existing = azure.scoped(["resource", "list", "--resource-group", group, "--resource-type", "Microsoft.KeyVault/vaults", "--query", f"[?name=='{vault_name}'].name"])
    ready = False
    if existing:
        # The gateway is created only once stage9-edge-certificates has written both listener certificates.
        secrets = azure.scoped(["resource", "list", "--resource-group", group, "--resource-type", "Microsoft.KeyVault/vaults/secrets", "--query", f"[?starts_with(name, '{vault_name}/')].{{name:name,contentType:properties.contentType}}"])
        found = {item["name"].split("/", 1)[1]: item.get("contentType") for item in secrets or []}
        ready = all(found.get(secret) == "application/x-pkcs12" for secret in certificate_secret_names().values())
    links = azure.scoped(["network", "private-dns", "link", "vnet", "list", "--resource-group", group, "--zone-name", "privatelink.vaultcore.azure.net", "--query", "[].virtualNetwork.id"])
    target = resource_prefix(config) + "Microsoft.Network/virtualNetworks/" + network
    require(any(str(value).lower() == target.lower() for value in links or []), "Application Gateway reads certificates over the Vault private endpoint; link the Vault private DNS zone to the target VNet")
    return {"networkIsolation": feature == "Registered", "deployGateway": ready}


def deployment_output(config, azure):
    from scripts.migration_deploy import deployment_name
    output = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge"), "--query", "{state:properties.provisioningState,edge:properties.outputs.edge.value}"])
    edge = output.get("edge") or {}
    require(output.get("state") == "Succeeded" and edge.get("provisioned") is True and edge.get("edgeType") == "applicationGateway", "Provision the matching Application Gateway edge first")
    return edge


def deployed_app_gateway(config, azure):
    """Validate the live gateway against its deployment outputs and the customer configuration."""
    settings = app_gateway_settings(config)
    edge = deployment_output(config, azure)
    resource = names(config)
    prefix = resource_prefix(config)
    hosts = {"api": "llm-api." + config["baseDomain"], "admin": "llm-admin." + config["baseDomain"]}
    gateway_id = prefix + "Microsoft.Network/applicationGateways/" + resource["gateway"]
    wafs = {key: prefix + "Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies/" + resource[key] for key in ("apiWaf", "adminWaf", "gateWaf")}
    require(edge.get("apiHost") == hosts["api"] and edge.get("adminHost") == hosts["admin"], "Application Gateway hosts differ from the customer configuration")
    require(edge.get("adminAccessMode") == "SourceIpAllowlistAndNativeLogin", "Admin listener does not enforce the reviewed source-IP and native-login access mode")
    require(str(edge.get("gatewayId", "")).lower() == gateway_id.lower() and all(str(edge.get(key + "Id", "")).lower() == value.lower() for key, value in wafs.items()), "Application Gateway outputs belong to another target scope")
    identifier = edge.get("edgeId")
    require(isinstance(identifier, str) and UUID(identifier).int != 0 and edge.get("edgeHeader") == APP_GATEWAY_HEADER, "Application Gateway edge identity output is invalid")
    from scripts.customer_migration import admin_source_cidrs
    require(edge.get("adminAllowedCidrs") == admin_source_cidrs(settings["adminAllowedCidrs"]), "Deployed Admin source-IP allowlist differs from the customer configuration")
    require(re.fullmatch(FQDN_PATTERN.format(location=re.escape(config["location"])), edge.get("publicIpFqdn", "")) is not None, "Application Gateway public IP has no reviewed DNS name")
    live = azure.scoped(["resource", "show", "--ids", gateway_id, "--api-version", "2024-05-01"])
    require(live.get("id", "").lower() == gateway_id.lower(), "Azure returned a different Application Gateway")
    properties = live.get("properties", {})
    require(properties.get("provisioningState") == "Succeeded" and properties.get("operationalState") == "Running", "Application Gateway is not provisioned and running")
    require(properties.get("sku", {}).get("tier") == "WAF_v2" and properties.get("enableHttp2") is False and properties.get("globalConfiguration", {}).get("enableResponseBuffering") is False, "Application Gateway SKU, HTTP/2 or response buffering differs from the streaming contract")
    require(properties.get("sslPolicy", {}).get("policyName") == SSL_POLICY, "Application Gateway TLS policy differs from the reviewed contract")
    listeners = {item["name"]: item.get("properties", {}) for item in properties.get("httpListeners", [])}
    require(set(listeners) == {"api", "admin"}, "Application Gateway must expose exactly the API and Admin listeners")
    vault = resource["vault"]
    certificates = {item["name"]: item.get("properties", {}).get("keyVaultSecretId", "") for item in properties.get("sslCertificates", [])}
    for plane in ("api", "admin"):
        listener = listeners[plane]
        expected_policy = wafs["apiWaf" if plane == "api" else "adminWaf"] if edge.get(f"{plane}TrafficEnabled") is True else wafs["gateWaf"]
        require(listener.get("protocol") == "Https" and listener.get("hostName") == hosts[plane] and listener.get("requireServerNameIndication") is True, f"Application Gateway {plane} listener differs from the reviewed host and TLS contract")
        require(listener.get("firewallPolicy", {}).get("id", "").lower() == expected_policy.lower(), f"Application Gateway {plane} listener is not in the reviewed traffic state")
        require(certificates.get(plane) == f"https://{vault}.vault.azure.net/secrets/{certificate_secret_names()[plane]}",f"Application Gateway {plane} certificate is not the versionless reviewed PFX secret")
    pools = {item["name"]: sorted(address.get("ipAddress", "") for address in item.get("properties", {}).get("backendAddresses", [])) for item in properties.get("backendAddressPools", [])}
    backends = edge.get("backendAddresses", {})
    require(pools == {"api": [backends.get("api")], "admin": [backends.get("admin")], "deny": []}, "Application Gateway backend pools differ from the verified private ingress")
    rewrites = [action for rule_set in properties.get("rewriteRuleSets", []) for rule in rule_set.get("properties", {}).get("rewriteRules", []) for action in rule.get("actionSet", {}).get("requestHeaderConfigurations", [])]
    require(rewrites == [{"headerName": APP_GATEWAY_HEADER, "headerValue": identifier}], "Application Gateway edge identity rewrite differs from the deployment output")
    from scripts.private_ingress import NATIVE_API_PATHS
    path_maps = properties.get("urlPathMaps", [])
    require(len(path_maps) == 1 and path_maps[0].get("properties", {}).get("defaultBackendAddressPool", {}).get("id", "").lower().endswith("/backendaddresspools/deny"), "Application Gateway API path map must default to the empty deny pool")
    paths = sorted(path for rule in path_maps[0]["properties"].get("pathRules", []) for path in rule.get("properties", {}).get("paths", []))
    require(paths == sorted(NATIVE_API_PATHS), "Application Gateway API path allowlist differs from the native API contract")
    admin = azure.scoped(["resource", "show", "--ids", wafs["adminWaf"], "--api-version", "2024-05-01"]).get("properties", {})
    rules = {rule.get("name"): rule for rule in admin.get("customRules", [])}
    allowlist = rules.get("BlockUnapprovedAdminSources", {})
    conditions = allowlist.get("matchConditions", [])
    require(admin.get("policySettings", {}).get("mode") == "Prevention" and admin.get("policySettings", {}).get("state") == "Enabled" and allowlist.get("action") == "Block" and allowlist.get("priority") == 5 and len(conditions) == 1, "Admin WAF source-IP gate is not enforced in Prevention mode")
    condition = conditions[0]
    require(condition.get("matchVariables") == [{"variableName": "RemoteAddr"}] and condition.get("operator") == "IPMatch" and condition.get("negationConditon") is True and sorted(condition.get("matchValues", [])) == edge["adminAllowedCidrs"], "Admin WAF source-IP allowlist differs from the customer configuration")
    gate = azure.scoped(["resource", "show", "--ids", wafs["gateWaf"], "--api-version", "2024-05-01"]).get("properties", {})
    require(gate.get("policySettings", {}).get("mode") == "Prevention" and [rule.get("name") for rule in gate.get("customRules", [])] == ["TrafficDisabled"], "Traffic gate WAF must block all requests in Prevention mode")
    return {
        "edgeType": "applicationGateway", "gatewayId": edge["gatewayId"], "edgeHeader": APP_GATEWAY_HEADER, "edgeId": identifier,
        "apiHost": hosts["api"], "adminHost": hosts["admin"], "publicIpId": edge["publicIpId"], "publicIpFqdn": edge["publicIpFqdn"],
        "apiWafId": edge["apiWafId"], "adminWafId": edge["adminWafId"], "gateWafId": edge["gateWafId"], "backendAddresses": backends,
        "adminAccessMode": edge["adminAccessMode"], "adminAllowedCidrs": edge["adminAllowedCidrs"],
        "adminRateLimitPerMinute": edge["adminRateLimitPerMinute"], "subnetPrefix": edge["subnetPrefix"],
    }


RECEIPT_KEYS = ("gatewayId", "edgeHeader", "edgeId", "publicIpId", "publicIpFqdn", "apiWafId", "adminWafId", "gateWafId", "backendAddresses", "adminAllowedCidrs", "adminRateLimitPerMinute", "subnetPrefix")


def require_backend_health(azure, gateway_id):
    health = azure.scoped(["network", "application-gateway", "show-backend-health", "--ids", gateway_id])
    states = {}
    for pool in health.get("backendAddressPools", []):
        name = pool.get("backendAddressPool", {}).get("id", "").rsplit("/", 1)[-1]
        states[name] = [server.get("health") for settings in pool.get("backendHttpSettingsCollection", []) for server in settings.get("servers", [])]
    require(states.get("api") == ["Healthy"] and states.get("admin") == ["Healthy"], "Application Gateway API/Admin backends must both report Healthy before traffic")
