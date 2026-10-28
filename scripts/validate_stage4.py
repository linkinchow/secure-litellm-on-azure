#!/usr/bin/env python3
"""Static safety checks for Stage 4 network and identity assets."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
VNET = ipaddress.ip_network("10.30.0.0/16")
SUBNETS = {
    "AzureFirewallSubnet": ipaddress.ip_network("10.30.0.0/26"),
    "snet-aks-system": ipaddress.ip_network("10.30.1.0/24"),
    "snet-aks-user": ipaddress.ip_network("10.30.2.0/23"),
    "snet-private-ingress": ipaddress.ip_network("10.30.4.0/24"),
    "snet-private-endpoints": ipaddress.ip_network("10.30.8.0/24"),
}
POD_CIDR = ipaddress.ip_network("10.244.0.0/16")
SERVICE_CIDR = ipaddress.ip_network("10.31.0.0/16")
# Optional hub egress reference plan: a /24 hub with the firewall subnet and reserved later subnets.
HUB_VNET = ipaddress.ip_network("10.40.0.0/24")
HUB_SUBNETS = {
    "AzureFirewallSubnet": ipaddress.ip_network("10.40.0.0/26"),
    "AzureFirewallManagementSubnet": ipaddress.ip_network("10.40.0.64/26"),
    "AzureBastionSubnet": ipaddress.ip_network("10.40.0.128/26"),
    "GatewaySubnet": ipaddress.ip_network("10.40.0.192/27"),
}


def validate_cidrs() -> None:
    for name, subnet in SUBNETS.items():
        assert subnet.subnet_of(VNET), f"{name} is outside {VNET}"
    values = list(SUBNETS.items())
    for index, (left_name, left) in enumerate(values):
        for right_name, right in values[index + 1 :]:
            assert not left.overlaps(right), f"{left_name} overlaps {right_name}"
    assert not VNET.overlaps(POD_CIDR)
    assert not VNET.overlaps(SERVICE_CIDR)
    assert not POD_CIDR.overlaps(SERVICE_CIDR)
    assert ipaddress.ip_address("10.31.0.10") in SERVICE_CIDR


def validate_hub_cidrs() -> None:
    for name, subnet in HUB_SUBNETS.items():
        assert subnet.subnet_of(HUB_VNET), f"{name} is outside {HUB_VNET}"
    values = list(HUB_SUBNETS.items())
    for index, (left_name, left) in enumerate(values):
        for right_name, right in values[index + 1 :]:
            assert not left.overlaps(right), f"Hub {left_name} overlaps {right_name}"
    for spoke in (VNET, POD_CIDR, SERVICE_CIDR):
        assert not HUB_VNET.overlaps(spoke), f"Hub overlaps {spoke}"
    catalog = json.loads((ROOT / "local_execution/customer.stage2-9.fragments.example.json").read_text())
    fragment = catalog["stages"]["3"]["hubEgress"]
    hub = fragment["customerConfig"]["parameters"]["hub"]
    assert ipaddress.ip_network(hub["virtualNetworkAddressPrefix"]) == HUB_VNET
    assert ipaddress.ip_network(hub["firewallSubnetPrefix"]) == HUB_SUBNETS["AzureFirewallSubnet"]
    assert {name: ipaddress.ip_network(value) for name, value in hub["reservedPrefixes"].items()} == {name: value for name, value in HUB_SUBNETS.items() if name != "AzureFirewallSubnet"}
    assert fragment["customerConfig"]["parameters"]["platform"]["stage4Network"]["egress"] == {"mode": "hub"}
    assert fragment["removeCustomerConfigPathsAfterMerge"] == ["parameters.platform.stage4Network.firewallSubnetPrefix"]


def rule_collections(path: Path) -> str:
    text = path.read_text()
    start = text.index("    ruleCollections: [\n")
    return text[start : text.index("\n    ]\n", start)]


def validate_hub_egress_rules() -> None:
    dedicated = rule_collections(ROOT / "infra/modules/firewall-egress/main.bicep")
    hub = rule_collections(ROOT / "infra/modules/hub-egress/main.bicep")
    assert dedicated == hub, "Hub egress rules must match the dedicated firewall rules"
    hub_network = (ROOT / "infra/modules/hub-network/main.bicep").read_text()
    assert "ruleCollectionGroups" not in hub_network, "The hub policy carries no workload rules"
    for value in ("name: 'AzureFirewallManagementSubnet'", "name: 'AzureBastionSubnet'", "name: 'GatewaySubnet'", "zones:"):
        assert value not in hub_network, f"The hub template must not declare {value}"


def validate_network_policies() -> None:
    path = ROOT / "deploy/components/stage4-network/networkpolicy.yaml"
    documents = [item for item in yaml.safe_load_all(path.read_text()) if item]
    policies = {item["metadata"]["name"]: item for item in documents}
    assert "default-deny" in policies
    assert set(policies["default-deny"]["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    required = policies["allow-litellm-required-traffic"]
    serialized = yaml.safe_dump(required)
    assert "169.254.169.254/32" in serialized, "IMDS must be excluded"
    assert "10.30.8.0/24" in serialized, "Private Endpoint subnet must be allowed"
    assert "kube-dns" in serialized, "DNS egress must be explicit"


def validate_workload_identity_patch() -> None:
    path = ROOT / "deploy/components/stage4-network/workload-identity-patch.yaml"
    documents = [item for item in yaml.safe_load_all(path.read_text()) if item]
    service_account = next(item for item in documents if item["kind"] == "ServiceAccount")
    deployment = next(item for item in documents if item["kind"] == "Deployment")
    annotation = service_account["metadata"]["annotations"]["azure.workload.identity/client-id"]
    assert annotation == "REPLACE_FROM_PROTECTED_DEPLOYMENT_OUTPUT"
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert labels["azure.workload.identity/use"] == "true"


if __name__ == "__main__":
    validate_cidrs()
    validate_hub_cidrs()
    validate_hub_egress_rules()
    validate_network_policies()
    validate_workload_identity_patch()
    print("Stage 4 static safety checks passed.")
