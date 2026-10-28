"""Optional regional hub: the Stage 0 hub firewall and the platform's hub egress mode."""

import hashlib
import ipaddress
import re

from scripts.customer_migration import configured, require

HUB_REQUIRED = {"resourceGroupName", "virtualNetworkName", "virtualNetworkAddressPrefix", "firewallSubnetPrefix", "logAnalyticsWorkspaceName"}
HUB_OPTIONAL = {"logAnalyticsWorkspaceResourceGroupName", "reservedPrefixes"}
# Planned later hub subnets as (largest, smallest) prefix length; validated for overlap only, never created.
RESERVED_SUBNETS = {"AzureFirewallManagementSubnet": (26, 26), "AzureBastionSubnet": (16, 26), "GatewaySubnet": (16, 27)}
EGRESS_MODES = ("dedicated", "hub")
EGRESS_FIELDS = {"mode", "ruleCollectionGroupPriority"}
# Derived priorities stay in 1000-9999 so hub owners keep 100-999 for their own collections.
DERIVED_PRIORITY = (1000, 9000)
RESOURCE_GROUP = r"[a-zA-Z0-9_().-]{1,90}"
# Azure Firewall names allow 56 characters; afw- plus this keeps every derived name valid.
HUB_VNET = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,50}[A-Za-z0-9_])?"


def stage4_network(config):
    network = config.get("parameters", {}).get("platform", {}).get("stage4Network", {})
    return network if isinstance(network, dict) else {}


def egress_settings(config):
    egress = stage4_network(config).get("egress", {"mode": "dedicated"})
    require(isinstance(egress, dict) and not set(egress) - EGRESS_FIELDS and egress.get("mode") in EGRESS_MODES, "stage4Network.egress requires mode dedicated or hub")
    if "ruleCollectionGroupPriority" in egress:
        priority = egress["ruleCollectionGroupPriority"]
        require(egress["mode"] == "hub" and type(priority) is int and 100 <= priority <= 65000, "stage4Network.egress.ruleCollectionGroupPriority applies to hub mode and must be 100 to 65000")
    return egress


def egress_mode(config):
    return egress_settings(config)["mode"]


def network(value, label):
    try:
        parsed = ipaddress.ip_network(value, strict=True)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be an IPv4 CIDR") from None
    require(parsed.version == 4, f"{label} must be an IPv4 CIDR")
    return parsed


def spoke_ranges(config):
    """Configured target-side ranges a peered hub must never overlap."""
    parameters = config["parameters"]
    owner = parameters.get("network") or parameters.get("backup") or {}
    platform = stage4_network(config)
    edge = parameters.get("edge") or {}
    candidates = {
        "target VNet": owner.get("virtualNetworkAddressPrefix"),
        "Private Endpoint subnet": owner.get("privateEndpointSubnetPrefix"),
        **{key: platform.get(key) for key in ("firewallSubnetPrefix", "systemSubnetPrefix", "userSubnetPrefix", "ingressSubnetPrefix", "podCidr", "serviceCidr")},
        "Application Gateway subnet": edge.get("subnetPrefix") if isinstance(edge, dict) else None,
    }
    return {label: ipaddress.ip_network(value, strict=False) for label, value in candidates.items() if isinstance(value, str) and configured(value)}


def hub_settings(config):
    """Validate parameters.hub; returns the template parameters with derived resource names."""
    hub = config["parameters"].get("hub")
    require(isinstance(hub, dict) and HUB_REQUIRED.issubset(hub) and not set(hub) - HUB_REQUIRED - HUB_OPTIONAL, "parameters.hub requires resourceGroupName, virtualNetworkName, virtualNetworkAddressPrefix, firewallSubnetPrefix and logAnalyticsWorkspaceName")
    require(configured(hub), "Replace hub configuration placeholders")
    group = hub["resourceGroupName"]
    workspace_group = hub.get("logAnalyticsWorkspaceResourceGroupName", config["target"]["resourceGroup"])
    for value in (group, workspace_group):
        require(isinstance(value, str) and re.fullmatch(RESOURCE_GROUP, value) is not None and not value.endswith("."), "Invalid hub resource group name")
    others = {config["target"]["resourceGroup"].lower()} | ({config["legacy"]["resourceGroup"].lower()} if "legacy" in config else set())
    require(group.lower() not in others, "The hub needs its own resource group, separate from the target and legacy groups")
    name = hub["virtualNetworkName"]
    require(isinstance(name, str) and re.fullmatch(HUB_VNET, name) is not None, "Hub virtualNetworkName must be 1-52 VNet name characters")
    require(isinstance(hub["logAnalyticsWorkspaceName"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{2,61}[A-Za-z0-9]", hub["logAnalyticsWorkspaceName"]) is not None, "Invalid hub Log Analytics workspace name")
    address = network(hub["virtualNetworkAddressPrefix"], "Hub virtualNetworkAddressPrefix")
    require(address.is_private and address.prefixlen <= 26, "Hub VNet must be a private IPv4 range of /26 or larger")
    subnets = {"AzureFirewallSubnet": network(hub["firewallSubnetPrefix"], "Hub firewallSubnetPrefix")}
    require(subnets["AzureFirewallSubnet"].prefixlen == 26, "AzureFirewallSubnet must be a /26")
    reserved = hub.get("reservedPrefixes", {})
    require(isinstance(reserved, dict) and not set(reserved) - RESERVED_SUBNETS.keys(), "hub.reservedPrefixes accepts AzureFirewallManagementSubnet, AzureBastionSubnet and GatewaySubnet")
    for subnet, value in reserved.items():
        subnets[subnet] = network(value, f"hub.reservedPrefixes.{subnet}")
        largest, smallest = RESERVED_SUBNETS[subnet]
        require(largest <= subnets[subnet].prefixlen <= smallest, f"{subnet} must be /{smallest}" + ("" if largest == smallest else " or larger"))
    for subnet, value in subnets.items():
        require(value.subnet_of(address), f"{subnet} must be inside the hub VNet")
    values = list(subnets.items())
    for index, (left_name, left) in enumerate(values):
        for right_name, right in values[index + 1:]:
            require(not left.overlaps(right), f"Hub {left_name} overlaps {right_name}")
    for label, value in spoke_ranges(config).items():
        require(not address.overlaps(value), f"Hub VNet overlaps the {label}")
    return {
        "resourceGroupName": group, "virtualNetworkName": name,
        "virtualNetworkAddressPrefix": str(address), "firewallSubnetPrefix": str(subnets["AzureFirewallSubnet"]),
        "firewallName": f"afw-{name}", "firewallPolicyName": f"afwp-{name}", "publicIpName": f"pip-afw-{name}",
        "logAnalyticsWorkspaceName": hub["logAnalyticsWorkspaceName"], "logAnalyticsWorkspaceResourceGroupName": workspace_group,
    }


def validate_hub(config):
    """Configuration-time checks; hub values are checked once configured, the egress decision always."""
    mode = egress_mode(config)
    hub = config["parameters"].get("hub")
    if mode == "hub":
        require(hub is not None, "stage4Network.egress.mode=hub requires parameters.hub and its stage0-hub deployment")
        require("firewallSubnetPrefix" not in stage4_network(config), "Hub egress has no spoke AzureFirewallSubnet; remove stage4Network.firewallSubnetPrefix")
    if hub is not None and configured(hub):
        hub_settings(config)


def hub_group_id(config):
    return f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{config['parameters']['hub']['resourceGroupName']}"


def spoke_egress(config):
    """Names the platform owns inside the hub; derived from configuration so plans and change scope agree."""
    settings = hub_settings(config)
    egress = egress_settings(config)
    require(egress["mode"] == "hub", "Hub egress names apply only to stage4Network.egress.mode=hub")
    environment = config["environment"]
    hub_group = hub_group_id(config)
    hub_id = f"{hub_group}/providers/Microsoft.Network/virtualNetworks/{settings['virtualNetworkName']}"
    spoke_id = f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{config['target']['resourceGroup']}/providers/Microsoft.Network/virtualNetworks/{stage4_network(config)['virtualNetworkName']}"
    suffix = hashlib.sha256((spoke_id.lower() + "|" + hub_id.lower()).encode()).hexdigest()[:16]
    start, span = DERIVED_PRIORITY
    return {
        "resourceGroupName": settings["resourceGroupName"], "virtualNetworkName": settings["virtualNetworkName"],
        "virtualNetworkId": hub_id, "spokeVirtualNetworkId": spoke_id,
        "firewallPolicyName": settings["firewallPolicyName"],
        "firewallPolicyId": f"{hub_group}/providers/Microsoft.Network/firewallPolicies/{settings['firewallPolicyName']}",
        "firewallSubnetPrefix": settings["firewallSubnetPrefix"],
        "deploymentName": f"llmgw-{environment}-hub-egress-{suffix}",
        "hubPeeringName": f"llmgw-{environment}-spoke-{suffix}",
        "spokePeeringName": f"llmgw-{environment}-hub-{suffix}",
        "ruleCollectionGroupName": f"litellm-{environment}-{suffix[:8]}",
        "ruleCollectionGroupPriority": egress.get("ruleCollectionGroupPriority", start + int(suffix[:8], 16) % span),
    }


TEMPLATE_KEYS = ("resourceGroupName", "virtualNetworkName", "firewallPolicyName", "deploymentName", "hubPeeringName", "spokePeeringName", "ruleCollectionGroupName", "ruleCollectionGroupPriority")


def platform_hub_parameters(config, resolved):
    """The stage4Hub template value: configuration-derived names plus the receipt's firewall address."""
    egress = spoke_egress(config)
    require(isinstance(resolved, dict) and set(resolved) == {*TEMPLATE_KEYS, "firewallPrivateIpAddress"} and all(resolved[key] == egress[key] for key in TEMPLATE_KEYS), "Hub egress parameters must come from the stage0-hub receipt resolved during plan")
    address = ipaddress.ip_address(resolved["firewallPrivateIpAddress"])
    require(address.version == 4 and address in ipaddress.ip_network(egress["firewallSubnetPrefix"]), "Hub firewall private IP is outside the hub AzureFirewallSubnet")
    return dict(resolved)


def hub_egress_resource_ids(config):
    """The only resources a hub-mode platform plan may change outside the target group."""
    egress = spoke_egress(config)
    return {identifier.lower() for identifier in (
        egress["virtualNetworkId"] + "/virtualNetworkPeerings/" + egress["hubPeeringName"],
        egress["firewallPolicyId"] + "/ruleCollectionGroups/" + egress["ruleCollectionGroupName"],
        hub_group_id(config) + "/providers/Microsoft.Resources/deployments/" + egress["deploymentName"],
    )}


SPOKE_FIREWALL_KINDS = ("microsoft.network/azurefirewalls/", "microsoft.network/firewallpolicies/")


def platform_change_allowed(config, resource, target_group):
    """Hub mode: never a spoke firewall; outside the target group only the peering, rules and their deployment."""
    if resource.startswith(target_group + "/"):
        local = resource.removeprefix(target_group + "/providers/")
        return not local.startswith(SPOKE_FIREWALL_KINDS) and not resource.endswith("/subnets/azurefirewallsubnet")
    return resource in hub_egress_resource_ids(config)


def hub_change_allowed(config, resource):
    group = hub_group_id(config).lower()
    return resource == group or resource.startswith(group + "/")


def inspect_hub(config, azure):
    """Read-only plan check: the hub must not overlap the execution host VNet."""
    settings = hub_settings(config)
    runner = config["parameters"].get("runner-connectivity", {}).get("runnerVirtualNetworkId", "")
    if not (isinstance(runner, str) and configured(runner)):
        return []
    prefixes = azure.scoped(["network", "vnet", "show", "--ids", runner, "--query", "addressSpace.addressPrefixes"])
    require(isinstance(prefixes, list) and all(isinstance(value, str) for value in prefixes), "Unable to read the execution host VNet address space")
    address = ipaddress.ip_network(settings["virtualNetworkAddressPrefix"])
    require(not any(address.overlaps(ipaddress.ip_network(value, strict=False)) for value in prefixes), "Hub VNet overlaps the execution host VNet")
    return prefixes


def resolve_hub(config, azure):
    """Resolve the hub firewall from the stage0-hub receipt and check the rule collection group priority is free."""
    from scripts.migration_deploy import deployment_name
    egress = spoke_egress(config)
    settings = hub_settings(config)
    output = azure.scoped(["deployment", "sub", "show", "--name", deployment_name(config, 0, "hub"), "--query", "{state:properties.provisioningState,hub:properties.outputs.hub.value}"])
    hub = output.get("hub") if isinstance(output, dict) else None
    require(isinstance(hub, dict) and output.get("state") == "Succeeded", "Deploy the Stage 0 hub (stage0-hub) before planning hub egress")
    require(hub.get("resourceGroupName", "").lower() == settings["resourceGroupName"].lower() and hub.get("virtualNetworkName") == settings["virtualNetworkName"]
            and hub.get("virtualNetworkId", "").lower() == egress["virtualNetworkId"].lower()
            and hub.get("firewallPolicyId", "").lower() == egress["firewallPolicyId"].lower() and hub.get("firewallPolicyName") == settings["firewallPolicyName"],
            "Hub receipt differs from parameters.hub; plan and execute stage0-hub first")
    require(hub.get("location") == config["location"], "The hub must be in the target region; deploy one hub per region")
    require(hub.get("virtualNetworkAddressPrefix") == settings["virtualNetworkAddressPrefix"] and hub.get("firewallSubnetPrefix") == settings["firewallSubnetPrefix"], "Hub receipt address plan differs from parameters.hub; plan and execute stage0-hub first")
    try:
        address = ipaddress.ip_address(hub.get("firewallPrivateIpAddress"))
    except ValueError:
        address = None
    require(address is not None and address.version == 4 and address in ipaddress.ip_network(settings["firewallSubnetPrefix"]), "Hub receipt lacks a firewall private IP in AzureFirewallSubnet")
    endpoint = azure.run(["cloud", "show", "--query", "endpoints.resourceManager"])
    require(isinstance(endpoint, str) and re.fullmatch(r"https://[a-z0-9.-]+/?", endpoint) is not None, "Invalid Azure Resource Manager endpoint")
    groups = azure.scoped(["rest", "--method", "get", "--url", endpoint.rstrip("/") + egress["firewallPolicyId"] + "/ruleCollectionGroups?api-version=2024-07-01", "--query", "value[].{name:name,priority:properties.priority}"])
    require(isinstance(groups, list) and all(isinstance(group, dict) for group in groups), "Unable to list the hub policy rule collection groups")
    taken = sorted(group.get("name", "") for group in groups if group.get("priority") == egress["ruleCollectionGroupPriority"] and str(group.get("name", "")).lower() != egress["ruleCollectionGroupName"].lower())
    require(not taken, f"Hub policy priority {egress['ruleCollectionGroupPriority']} is already used by {', '.join(taken)}; set stage4Network.egress.ruleCollectionGroupPriority")
    return {**{key: egress[key] for key in TEMPLATE_KEYS}, "firewallPrivateIpAddress": str(address)}
