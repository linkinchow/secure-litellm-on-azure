import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_execution.merge_config import merge_stage
from local_execution.runner import INFRASTRUCTURE_STEPS, STEPS, execute_infrastructure_plan, local_operation
from scripts.customer_migration import COMPONENTS, ROOT, MigrationError, parameters_for, stage_fingerprint, validate_config
from scripts.hub_network import egress_mode, hub_egress_resource_ids, hub_settings, resolve_hub, spoke_egress
from scripts.migration_deploy import assert_change_scope, deploy_component, deployment_name, group_id
from tests.test_customer_migration import customer_config

HUB = {
    "resourceGroupName": "rg-hub", "virtualNetworkName": "hub-westus",
    "virtualNetworkAddressPrefix": "10.40.0.0/24", "firewallSubnetPrefix": "10.40.0.0/26",
    "reservedPrefixes": {"AzureFirewallManagementSubnet": "10.40.0.64/26", "AzureBastionSubnet": "10.40.0.128/26", "GatewaySubnet": "10.40.0.192/27"},
    "logAnalyticsWorkspaceName": "target-logs",
}
NETWORK = {
    "virtualNetworkName": "target-vnet", "privateEndpointSubnetName": "snet-private-endpoints",
    "systemSubnetName": "snet-aks-system", "systemSubnetPrefix": "10.30.1.0/24",
    "userSubnetName": "snet-aks-user", "userSubnetPrefix": "10.30.2.0/23",
    "ingressSubnetName": "snet-private-ingress", "ingressSubnetPrefix": "10.30.4.0/24",
    "podCidr": "10.244.0.0/16", "serviceCidr": "10.31.0.0/16", "dnsServiceIp": "10.31.0.10",
}


def dedicated_config():
    config = customer_config()
    config["parameters"]["platform"]["stage4Network"] = {**NETWORK, "firewallSubnetPrefix": "10.30.0.0/26"}
    config["parameters"]["backup"] = {
        "logAnalyticsWorkspaceName": "target-logs", "backupOwnerPrincipalId": "33333333-3333-4333-8333-333333333333",
        "virtualNetworkName": "target-vnet", "virtualNetworkAddressPrefix": "10.30.0.0/16",
        "privateEndpointSubnetName": "snet-private-endpoints", "privateEndpointSubnetPrefix": "10.30.8.0/24",
    }
    return validate_config(config, "test")


def hub_config(**egress):
    config = dedicated_config()
    config["parameters"]["hub"] = copy.deepcopy(HUB)
    config["parameters"]["platform"]["stage4Network"] = {**NETWORK, "egress": {"mode": "hub", **egress}}
    return validate_config(config, "test")


def compiled(path):
    result = subprocess.run(["az", "bicep", "build", "--file", str(ROOT / path), "--stdout"], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def receipt(config, **changes):
    egress = spoke_egress(config)
    hub = {
        "resourceGroupName": "rg-hub", "location": config["location"],
        "virtualNetworkId": egress["virtualNetworkId"], "virtualNetworkName": "hub-westus",
        "virtualNetworkAddressPrefix": "10.40.0.0/24", "firewallSubnetPrefix": "10.40.0.0/26",
        "firewallName": "afw-hub-westus", "firewallPolicyId": egress["firewallPolicyId"], "firewallPolicyName": "afwp-hub-westus",
        "firewallPrivateIpAddress": "10.40.0.4", "firewallPublicIpAddress": "20.1.2.3",
    }
    hub.update(changes)
    return {"state": "Succeeded", "hub": hub}


class FakeHubAzure:
    def __init__(self, config, deployment, groups=(), changes=(), runner_prefixes=("10.60.0.0/24",)):
        self.config = config
        self.deployment = deployment
        self.groups = list(groups)
        self.changes = list(changes)
        self.runner_prefixes = list(runner_prefixes)
        self.calls = []

    def run(self, command):
        self.calls.append(command)
        if command[:2] == ["cloud", "show"]:
            return "https://management.azure.com/"
        return {"tenantId": self.config["azure"]["tenantId"], "id": self.config["azure"]["subscriptionId"]}

    def scoped(self, command):
        self.calls.append(command)
        if command[:3] == ["deployment", "sub", "show"]:
            return self.deployment
        if command[0] == "rest":
            return self.groups
        if command[:3] == ["network", "vnet", "show"]:
            return self.runner_prefixes
        if "what-if" in command:
            return {"status": "Succeeded", "changes": self.changes}
        return {"id": "/synthetic/deployment", "properties": {"provisioningState": "Succeeded"}}


def compile_stub(command, **_kwargs):
    Path(command[command.index("--outfile") + 1]).write_text('{"resources": []}')
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class HubTemplateTests(unittest.TestCase):
    def test_platform_defaults_keep_the_dedicated_firewall(self):
        template = compiled("infra/environments/main.bicep")
        self.assertEqual(template["variables"]["hubEgress"], "[equals(coalesce(tryGet(tryGet(parameters('stage4Network'), 'egress'), 'mode'), 'dedicated'), 'hub')]")
        self.assertNotIn("egress", template["parameters"]["stage4Network"]["defaultValue"])
        self.assertEqual(template["parameters"]["stage4Network"]["defaultValue"]["firewallSubnetPrefix"], "10.30.0.0/26")
        self.assertTrue(template["parameters"]["stage4Hub"]["nullable"])
        resources = template["resources"]
        for name in ("networkFoundation", "firewall"):
            self.assertEqual(resources[name]["condition"], "[and(parameters('deployStage4'), not(variables('hubEgress')))]")
        for name in ("hubEgressRules", "spokeToHubPeering"):
            self.assertEqual(resources[name]["condition"], "[and(parameters('deployStage4'), variables('hubEgress'))]")
        route = resources["aksNetwork"]["properties"]["parameters"]["firewallPrivateIpAddress"]
        self.assertIn("reference('firewall').outputs.firewall.value.privateIpAddress", route)
        self.assertTrue(route.startswith("[if(variables('hubEgress'), "))
        self.assertEqual(resources["aksNetwork"]["dependsOn"], ["firewall", "spokeToHubPeering"])
        peering = resources["spokeToHubPeering"]["properties"]
        self.assertEqual((peering["allowForwardedTraffic"], peering["useRemoteGateways"], peering["allowGatewayTransit"]), (True, False, False))
        rules = resources["hubEgressRules"]
        self.assertEqual(rules["resourceGroup"], "[coalesce(tryGet(parameters('stage4Hub'), 'resourceGroupName'), resourceGroup().name)]")
        for parameter in ("tags", "location"):
            self.assertNotIn(parameter, rules["properties"]["parameters"])
        self.assertEqual(rules["properties"]["parameters"]["approvedHttpsFqdns"], template["resources"]["firewall"]["properties"]["parameters"]["approvedHttpsFqdns"])
        self.assertEqual(rules["properties"]["parameters"]["sourceAddressPrefixes"], template["resources"]["firewall"]["properties"]["parameters"]["sourceAddressPrefixes"])

    def test_hub_rules_match_the_dedicated_firewall_rules(self):
        dedicated = next(item for item in compiled("infra/modules/firewall-egress/main.bicep")["resources"] if item["type"] == "Microsoft.Network/firewallPolicies/ruleCollectionGroups")
        hub = compiled("infra/modules/hub-egress/main.bicep")
        group = next(item for item in hub["resources"] if item["type"] == "Microsoft.Network/firewallPolicies/ruleCollectionGroups")
        self.assertEqual(group["properties"]["ruleCollections"], dedicated["properties"]["ruleCollections"])
        self.assertEqual(group["properties"]["priority"], "[parameters('ruleCollectionGroupPriority')]")
        peering = next(item for item in hub["resources"] if item["type"] == "Microsoft.Network/virtualNetworks/virtualNetworkPeerings")
        self.assertFalse(peering["properties"]["useRemoteGateways"])
        self.assertEqual({item["type"] for item in hub["resources"]}, {"Microsoft.Network/virtualNetworks/virtualNetworkPeerings", "Microsoft.Network/firewallPolicies/ruleCollectionGroups"})

    def test_hub_template_owns_only_its_group_firewall_and_subnet(self):
        template = compiled("infra/hub/main.bicep")
        self.assertIn("subscriptionDeploymentTemplate", template["$schema"])
        groups = [item for item in template["resources"] if item["type"] == "Microsoft.Resources/resourceGroups"]
        self.assertEqual(len(groups), 1)
        module = next(item for item in template["resources"] if item["type"] == "Microsoft.Resources/deployments")
        self.assertEqual(module["name"], "llmgw-hub-network")
        resources = {item["type"]: item for item in module["properties"]["template"]["resources"]}
        self.assertEqual(set(resources), {"Microsoft.Network/virtualNetworks", "Microsoft.Network/publicIPAddresses", "Microsoft.Network/firewallPolicies", "Microsoft.Network/azureFirewalls", "Microsoft.Insights/diagnosticSettings"})
        self.assertEqual([subnet["name"] for subnet in resources["Microsoft.Network/virtualNetworks"]["properties"]["subnets"]], ["AzureFirewallSubnet"])
        policy = resources["Microsoft.Network/firewallPolicies"]["properties"]
        self.assertEqual((policy["sku"]["tier"], policy["dnsSettings"]["enableProxy"], policy["intrusionDetection"]["mode"]), ("Premium", True, "Alert"))
        firewall = resources["Microsoft.Network/azureFirewalls"]
        self.assertNotIn("zones", firewall)
        self.assertEqual(firewall["properties"]["sku"], {"name": "AZFW_VNet", "tier": "Premium"})
        self.assertEqual(resources["Microsoft.Network/publicIPAddresses"]["sku"]["name"], "Standard")
        self.assertNotIn("zones", resources["Microsoft.Network/publicIPAddresses"])
        self.assertEqual(set(template["outputs"]["hub"]["value"]) >= {"resourceGroupName", "virtualNetworkId", "virtualNetworkName", "firewallName", "firewallPolicyId", "firewallPolicyName", "firewallPrivateIpAddress", "firewallPublicIpAddress"}, True)

    def test_hub_parameters_match_the_compiled_template(self):
        config = hub_config()
        template, document = parameters_for(config, 0, "hub")
        self.assertEqual(template, ROOT / "infra/hub/main.bicep")
        declarations = compiled("infra/hub/main.bicep")["parameters"]
        parameters = document["parameters"]
        self.assertFalse(set(parameters) - set(declarations))
        self.assertFalse({name for name, declaration in declarations.items() if "defaultValue" not in declaration} - set(parameters))


class HubConfigurationTests(unittest.TestCase):
    def test_dedicated_defaults_render_without_hub_values(self):
        config = dedicated_config()
        self.assertEqual(egress_mode(config), "dedicated")
        for stage in (3, 4, 5) if "stage5Data" in config["parameters"]["platform"] else (3, 4):
            parameters = parameters_for(config, stage, "platform")[1]["parameters"]
            self.assertNotIn("stage4Hub", parameters)
            self.assertNotIn("egress", parameters["stage4Network"]["value"])
            self.assertEqual(parameters["stage4Network"]["value"]["firewallSubnetPrefix"], "10.30.0.0/26")
        self.assertEqual(COMPONENTS["hub"][:2], (0, "hub"))
        with self.assertRaisesRegex(ValueError, "stage0-hub"):
            parameters_for(dict(config, parameters={**config["parameters"], "platform": {**config["parameters"]["platform"], "stage4Network": {**NETWORK, "egress": {"mode": "hub"}}}, "hub": HUB}), 4, "platform")
        injected = copy.deepcopy(config)
        injected["parameters"]["platform"]["stage4Hub"] = {"firewallPrivateIpAddress": "10.40.0.4"}
        with self.assertRaisesRegex(ValueError, "Only hub egress"):
            parameters_for(injected, 4, "platform")

    def test_hub_parameters_derive_names_and_default_workspace_group(self):
        config = hub_config()
        parameters = {key: item["value"] for key, item in parameters_for(config, 0, "hub")[1]["parameters"].items()}
        self.assertEqual(parameters["firewallName"], "afw-hub-westus")
        self.assertEqual(parameters["firewallPolicyName"], "afwp-hub-westus")
        self.assertEqual(parameters["publicIpName"], "pip-afw-hub-westus")
        self.assertEqual(parameters["logAnalyticsWorkspaceResourceGroupName"], "rg-secure")
        self.assertEqual(parameters["location"], "westus")
        self.assertNotIn("reservedPrefixes", parameters)
        config["parameters"]["hub"]["logAnalyticsWorkspaceResourceGroupName"] = "rg-shared-logs"
        self.assertEqual(hub_settings(config)["logAnalyticsWorkspaceResourceGroupName"], "rg-shared-logs")
        for stage in (3, 4):
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                parameters_for(config, stage, "hub")

    def test_hub_mode_platform_takes_only_the_resolved_receipt(self):
        config = hub_config()
        stage3 = parameters_for(config, 3, "platform")[1]["parameters"]
        self.assertNotIn("stage4Hub", stage3)
        self.assertEqual(stage3["stage4Network"]["value"]["egress"], {"mode": "hub"})
        with self.assertRaisesRegex(ValueError, "stage0-hub deployment receipt"):
            parameters_for(config, 4, "platform")
        azure = FakeHubAzure(config, receipt(config))
        resolved = copy.deepcopy(config)
        resolved["parameters"]["platform"]["stage4Hub"] = resolve_hub(config, azure)
        hub = parameters_for(resolved, 4, "platform")[1]["parameters"]["stage4Hub"]["value"]
        egress = spoke_egress(config)
        self.assertEqual(hub["firewallPrivateIpAddress"], "10.40.0.4")
        self.assertEqual(hub["ruleCollectionGroupName"], egress["ruleCollectionGroupName"])
        self.assertRegex(hub["ruleCollectionGroupName"], r"^litellm-test-[0-9a-f]{8}$")
        self.assertTrue(1000 <= hub["ruleCollectionGroupPriority"] <= 9999)
        self.assertNotEqual(hub["hubPeeringName"], hub["spokePeeringName"])
        for key, value in (("firewallPrivateIpAddress", "10.40.0.200"), ("ruleCollectionGroupName", "litellm-other"), ("resourceGroupName", "rg-other")):
            tampered = copy.deepcopy(resolved)
            tampered["parameters"]["platform"]["stage4Hub"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                parameters_for(tampered, 4, "platform")

    def test_rule_collection_group_names_are_unique_per_spoke_and_priority_is_overridable(self):
        config = hub_config()
        other = copy.deepcopy(config)
        other["target"]["resourceGroup"] = "rg-secure-other"
        self.assertNotEqual(spoke_egress(config)["ruleCollectionGroupName"], spoke_egress(other)["ruleCollectionGroupName"])
        self.assertEqual(spoke_egress(config), spoke_egress(copy.deepcopy(config)))
        self.assertEqual(spoke_egress(hub_config(ruleCollectionGroupPriority=4200))["ruleCollectionGroupPriority"], 4200)

    def test_invalid_hub_and_egress_configuration_is_rejected(self):
        cases = {
            "hub mode without hub": lambda config: config["parameters"].pop("hub"),
            "spoke firewall subnet in hub mode": lambda config: config["parameters"]["platform"]["stage4Network"].update(firewallSubnetPrefix="10.30.0.0/26"),
            "unknown mode": lambda config: config["parameters"]["platform"]["stage4Network"].update(egress={"mode": "nva"}),
            "priority range": lambda config: config["parameters"]["platform"]["stage4Network"]["egress"].update(ruleCollectionGroupPriority=50),
            "unknown egress field": lambda config: config["parameters"]["platform"]["stage4Network"]["egress"].update(firewallPrivateIpAddress="10.40.0.4"),
            "firewall subnet /25": lambda config: config["parameters"]["hub"].update(firewallSubnetPrefix="10.40.0.0/25"),
            "firewall subnet outside hub": lambda config: config["parameters"]["hub"].update(firewallSubnetPrefix="10.41.0.0/26"),
            "public hub": lambda config: config["parameters"]["hub"].update(virtualNetworkAddressPrefix="20.40.0.0/24", firewallSubnetPrefix="20.40.0.0/26", reservedPrefixes={}),
            "host bits": lambda config: config["parameters"]["hub"].update(virtualNetworkAddressPrefix="10.40.0.1/24"),
            "reserved overlap": lambda config: config["parameters"]["hub"]["reservedPrefixes"].update(AzureBastionSubnet="10.40.0.0/26"),
            "reserved size": lambda config: config["parameters"]["hub"]["reservedPrefixes"].update(GatewaySubnet="10.40.0.192/28"),
            "reserved management size": lambda config: config["parameters"]["hub"]["reservedPrefixes"].update(AzureFirewallManagementSubnet="10.40.0.64/27"),
            "reserved unknown": lambda config: config["parameters"]["hub"]["reservedPrefixes"].update(AzureRouteServerSubnet="10.40.0.224/27"),
            "spoke VNet overlap": lambda config: config["parameters"]["hub"].update(virtualNetworkAddressPrefix="10.30.255.0/24", firewallSubnetPrefix="10.30.255.0/26", reservedPrefixes={}),
            "service CIDR overlap": lambda config: config["parameters"]["hub"].update(virtualNetworkAddressPrefix="10.31.255.0/24", firewallSubnetPrefix="10.31.255.0/26", reservedPrefixes={}),
            "target group": lambda config: config["parameters"]["hub"].update(resourceGroupName="RG-SECURE"),
            "legacy group": lambda config: config["parameters"]["hub"].update(resourceGroupName="rg-legacy"),
            "long VNet name": lambda config: config["parameters"]["hub"].update(virtualNetworkName="h" * 53),
            "unknown hub field": lambda config: config["parameters"]["hub"].update(firewallName="afw-custom"),
            "missing hub field": lambda config: config["parameters"]["hub"].pop("firewallSubnetPrefix"),
        }
        for name, change in cases.items():
            config = hub_config()
            change(config)
            with self.subTest(case=name), self.assertRaises(ValueError):
                validate_config(config, "test")
                parameters_for(config, 0, "hub")
        placeholder = dedicated_config()
        placeholder["parameters"]["hub"] = {**HUB, "resourceGroupName": "REPLACE_HUB_RESOURCE_GROUP"}
        validate_config(placeholder, "test")
        with self.assertRaises(ValueError):
            parameters_for(placeholder, 0, "hub")

    def test_hub_is_bound_into_every_stage_fingerprint_only_when_configured(self):
        config = dedicated_config()
        hub = copy.deepcopy(config)
        hub["parameters"]["hub"] = copy.deepcopy(HUB)
        validate_config(hub, "test")
        for stage in range(10):
            self.assertNotEqual(stage_fingerprint(config, stage), stage_fingerprint(hub, stage))


class HubReceiptTests(unittest.TestCase):
    def test_receipt_resolution_reads_the_stage0_hub_deployment(self):
        config = hub_config()
        azure = FakeHubAzure(config, receipt(config), groups=[{"name": "platform-deny", "priority": 100}, {"name": spoke_egress(config)["ruleCollectionGroupName"], "priority": spoke_egress(config)["ruleCollectionGroupPriority"]}])
        resolved = resolve_hub(config, azure)
        self.assertEqual(resolved["firewallPrivateIpAddress"], "10.40.0.4")
        show = next(call for call in azure.calls if call[:3] == ["deployment", "sub", "show"])
        self.assertEqual(show[show.index("--name") + 1], deployment_name(config, 0, "hub"))
        listing = next(call for call in azure.calls if call[0] == "rest")
        self.assertEqual(listing[listing.index("--method") + 1], "get")
        self.assertEqual(listing[listing.index("--url") + 1], "https://management.azure.com" + spoke_egress(config)["firewallPolicyId"] + "/ruleCollectionGroups?api-version=2024-07-01")

    def test_receipt_resolution_rejects_drift_and_priority_collisions(self):
        config = hub_config()
        priority = spoke_egress(config)["ruleCollectionGroupPriority"]
        cases = {
            "not deployed": ({"state": "Failed", "hub": receipt(config)["hub"]}, []),
            "no outputs": ({"state": "Succeeded"}, []),
            "other group": (receipt(config, resourceGroupName="rg-other"), []),
            "other policy": (receipt(config, firewallPolicyId=spoke_egress(config)["firewallPolicyId"] + "-old"), []),
            "other region": (receipt(config, location="eastus"), []),
            "other address plan": (receipt(config, virtualNetworkAddressPrefix="10.40.0.0/23"), []),
            "ip outside subnet": (receipt(config, firewallPrivateIpAddress="10.40.0.100"), []),
            "invalid ip": (receipt(config, firewallPrivateIpAddress="hub-firewall"), []),
            "priority taken": (receipt(config), [{"name": "other-spoke", "priority": priority}]),
        }
        for name, (deployment, groups) in cases.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                resolve_hub(config, FakeHubAzure(config, deployment, groups))

    def test_hub_deployment_name_is_stable_and_region_scoped(self):
        config = hub_config()
        name = deployment_name(config, 0, "hub")
        self.assertRegex(name, r"^llmgw-test-s0-hub-[0-9a-f]{8}$")
        regional = copy.deepcopy(config)
        regional["location"] = "westus3"
        self.assertNotEqual(name, deployment_name(regional, 0, "hub"))
        regrouped = copy.deepcopy(config)
        regrouped["parameters"]["hub"]["resourceGroupName"] = "rg-hub-2"
        self.assertNotEqual(name, deployment_name(regrouped, 0, "hub"))

    def test_hub_plan_is_subscription_scoped_and_checks_the_execution_host(self):
        config = hub_config()
        config["parameters"]["runner-connectivity"] = {"runnerVirtualNetworkId": group_id(config).replace("rg-secure", "rg-runner") + "/providers/Microsoft.Network/virtualNetworks/runner-vnet", "managePeering": True, "manageBlobDnsLink": True}
        hub_group = f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/rg-hub"
        changes = [{"resourceId": hub_group, "changeType": "Create"}, {"resourceId": hub_group + "/providers/Microsoft.Network/azureFirewalls/afw-hub-westus", "changeType": "Create"}]
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_deploy.subprocess.run", side_effect=compile_stub):
            azure = FakeHubAzure(config, None, changes=changes)
            plan = deploy_component(config, 0, "hub", "a" * 40, "plan", [], directory, azure=azure)
            what_if = next(call for call in azure.calls if "what-if" in call)
            self.assertEqual(what_if[:3], ["deployment", "sub", "what-if"])
            self.assertEqual(what_if[what_if.index("--location") + 1], "westus")
            self.assertEqual(what_if[what_if.index("--name") + 1], deployment_name(config, 0, "hub"))
            self.assertEqual(len(plan["changes"]), 2)
            with self.assertRaisesRegex(ValueError, "execution host"):
                deploy_component(config, 0, "hub", "a" * 40, "plan", [], directory, azure=FakeHubAzure(config, None, changes=changes, runner_prefixes=["10.40.0.0/16"]))

    def test_hub_platform_plan_renders_the_receipt_and_scopes_changes(self):
        config = hub_config()
        egress = spoke_egress(config)
        changes = [{"resourceId": identifier, "changeType": "Create"} for identifier in sorted(hub_egress_resource_ids(config))]
        changes.append({"resourceId": group_id(config) + "/providers/Microsoft.Network/routeTables/rt-litellm-aks-egress", "changeType": "Create"})
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_deploy.subprocess.run", side_effect=compile_stub):
            azure = FakeHubAzure(config, receipt(config), changes=changes)
            deploy_component(config, 4, "platform", "a" * 40, "plan", [], directory, azure=azure)
            parameters = json.loads((Path(directory) / "parameters.json").read_text())["parameters"]
        self.assertEqual(parameters["stage4Hub"]["value"]["firewallPrivateIpAddress"], "10.40.0.4")
        self.assertEqual(parameters["stage4Hub"]["value"]["deploymentName"], egress["deploymentName"])
        what_if = next(call for call in azure.calls if "what-if" in call)
        self.assertEqual(what_if[:3], ["deployment", "group", "what-if"])


class HubChangeScopeTests(unittest.TestCase):
    def test_hub_component_stays_inside_the_hub_group(self):
        config = hub_config()
        hub_group = f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/rg-hub"
        allowed = (hub_group, hub_group + "/providers/Microsoft.Network/virtualNetworks/hub-westus", hub_group + "/providers/Microsoft.Network/azureFirewalls/afw-hub-westus/providers/Microsoft.Insights/diagnosticSettings/send-firewall-logs-to-log-analytics")
        assert_change_scope(config, "hub", [{"resourceId": identifier, "changeType": "Create"} for identifier in allowed])
        for identifier in (group_id(config), group_id(config) + "/providers/Microsoft.Network/virtualNetworks/target-vnet", hub_group + "-other/providers/Microsoft.Network/virtualNetworks/hub"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                assert_change_scope(config, "hub", [{"resourceId": identifier, "changeType": "Create"}])
        with self.assertRaises(ValueError):
            assert_change_scope(config, "hub", [{"resourceId": hub_group + "/providers/Microsoft.Network/virtualNetworks/hub-westus", "changeType": "Delete"}])

    def test_hub_platform_touches_only_its_peering_and_rules_outside_the_target(self):
        config = hub_config()
        egress = spoke_egress(config)
        hub_group = f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/rg-hub"
        allowed = [
            egress["virtualNetworkId"] + "/virtualNetworkPeerings/" + egress["hubPeeringName"],
            egress["firewallPolicyId"] + "/ruleCollectionGroups/" + egress["ruleCollectionGroupName"],
            hub_group + "/providers/Microsoft.Resources/deployments/" + egress["deploymentName"],
            egress["spokeVirtualNetworkId"] + "/virtualNetworkPeerings/" + egress["spokePeeringName"],
            group_id(config) + "/providers/Microsoft.Network/routeTables/rt-litellm-aks-egress",
        ]
        assert_change_scope(config, "platform", [{"resourceId": identifier, "changeType": "Modify"} for identifier in allowed])
        denied = (
            egress["virtualNetworkId"],
            egress["firewallPolicyId"],
            egress["firewallPolicyId"] + "/ruleCollectionGroups/litellm-prod-00000000",
            egress["virtualNetworkId"] + "/virtualNetworkPeerings/other-spoke",
            hub_group + "/providers/Microsoft.Network/azureFirewalls/afw-hub-westus",
            hub_group + "/providers/Microsoft.Resources/deployments/other",
            group_id(config) + "/providers/Microsoft.Network/azureFirewalls/afw-litellm-test",
            group_id(config) + "/providers/Microsoft.Network/firewallPolicies/afwp-litellm-test/ruleCollectionGroups/litellm-egress-rules",
            egress["spokeVirtualNetworkId"] + "/subnets/AzureFirewallSubnet",
        )
        for identifier in denied:
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                assert_change_scope(config, "platform", [{"resourceId": identifier, "changeType": "Create"}])

    def test_dedicated_platform_never_reaches_the_hub(self):
        config = dedicated_config()
        hub = hub_config()
        identifiers = sorted(hub_egress_resource_ids(hub))
        assert_change_scope(config, "platform", [{"resourceId": group_id(config) + "/providers/Microsoft.Network/azureFirewalls/afw-litellm-test", "changeType": "Create"}])
        for identifier in identifiers:
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                assert_change_scope(config, "platform", [{"resourceId": identifier, "changeType": "Create"}])


class HubLocalExecutionTests(unittest.TestCase):
    def test_stage0_hub_step_is_planned_and_subscription_scoped(self):
        self.assertEqual(INFRASTRUCTURE_STEPS["stage0-hub"], (0, "hub"))
        self.assertLess(STEPS.index("execution-host-connectivity"), STEPS.index("stage0-hub"))
        self.assertLess(STEPS.index("stage0-hub"), STEPS.index("stage3-platform"))
        self.assertEqual(STEPS.count("stage0-hub"), 1)
        self.assertEqual(local_operation("stage0-hub", "auto"), "plan")
        with self.assertRaisesRegex(MigrationError, "Immediate apply"):
            local_operation("stage0-hub", "apply")
        config = hub_config()
        calls = []

        class Azure:
            def run(self, arguments):
                calls.append(arguments)
                return {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}

            def scoped(self, arguments):
                calls.append(arguments)
                return {"id": "/synthetic/deployment", "properties": {"provisioningState": "Succeeded", "outputs": {}}}

        plan = {"planSha256": "b" * 64, "revision": "c" * 40, "configSha256": "d" * 64}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.AzureCommands", return_value=Azure()):
            plan_directory = Path(folder) / "plan"
            plan_directory.mkdir()
            (plan_directory / "template.json").write_text("{}")
            (plan_directory / "parameters.json").write_text("{}")
            execute_infrastructure_plan(config, 0, "hub", plan, plan_directory, Path(folder) / "execute")
        self.assertEqual(calls[1][:3], ["deployment", "sub", "create"])
        self.assertEqual(calls[1][calls[1].index("--name") + 1], deployment_name(config, 0, "hub"))
        self.assertNotIn("--mode", calls[1])

    def test_hub_egress_option_merges_hub_and_drops_the_spoke_firewall_subnet(self):
        catalog = json.loads((ROOT / "local_execution/customer.stage2-9.fragments.example.json").read_text())
        source = dedicated_config()
        source["parameters"]["platform"] = {"containerRegistryName": "customerregistry", "logAnalyticsWorkspaceName": "target-logs", "stage4Network": {"virtualNetworkName": "target-vnet"}, "stage4Aks": {"name": "new-aks"}}
        source["localExecution"] = {"executionHost": {"virtualNetworkId": "/synthetic", "managePeering": True, "manageBlobDnsLink": True}}
        values = {
            "REPLACE_NEW_PRIVATE_AKS_NAME": "new-aks", "REPLACE_NEW_AKS_DNS_PREFIX": "new-aks",
            "REPLACE_SUPPORTED_K8S_VERSION": "1.35", "REPLACE_SUPPORTED_VM_SKU": "Standard_D4s_v5",
            "REPLACE_HUB_RESOURCE_GROUP": "rg-hub", "REPLACE_HUB_VNET": "hub-westus",
        }
        default, missing = merge_stage(source, catalog, 3, (), values)
        self.assertEqual(missing, [])
        self.assertNotIn("hub", default["parameters"])
        self.assertEqual(default["parameters"]["platform"]["stage4Network"]["firewallSubnetPrefix"], "10.30.0.0/26")
        self.assertNotIn("egress", default["parameters"]["platform"]["stage4Network"])
        merged, missing = merge_stage(source, catalog, 3, ("hub-egress",), values)
        self.assertEqual(missing, [])
        self.assertNotIn("firewallSubnetPrefix", merged["parameters"]["platform"]["stage4Network"])
        self.assertEqual(merged["parameters"]["platform"]["stage4Network"]["egress"], {"mode": "hub"})
        self.assertEqual(merged["parameters"]["hub"]["logAnalyticsWorkspaceName"], "target-logs")
        merged.pop("localExecution")
        config = validate_config(merged, "test")
        self.assertEqual(egress_mode(config), "hub")
        parameters_for(config, 0, "hub")
        parameters_for(config, 3, "platform")
        for stage in (2, 4):
            with self.subTest(stage=stage), self.assertRaisesRegex(ValueError, "applies only to Stage 3"):
                merge_stage(source, catalog, stage, ("hub-egress",), values)


if __name__ == "__main__":
    unittest.main()
