import json
import copy
from pathlib import Path
import subprocess
import unittest

from scripts.customer_migration import COMPONENTS, ROOT, parameters_for, validate_config
from tests.test_customer_migration import certificate_config


def example_customer():
    config = json.loads((ROOT / "config/customer.example.json").read_text())

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if value == "REPLACE_FRONT_DOOR_PRIVATE_LINK_REGION":
            return "westus3"
        if isinstance(value, str) and value.startswith("REPLACE_"):
            return "synthetic-value"
        return value

    config = replace(config)
    config.update(location="westus", baseDomain="customer.invalid", ownerEmail="owner@customer.invalid")
    config["azure"] = {"tenantId": "11111111-1111-4111-8111-111111111111", "subscriptionId": "22222222-2222-4222-8222-222222222222"}
    config["legacy"]["resourceGroup"] = "rg-legacy"
    config["target"]["resourceGroup"] = "rg-target"
    config["parameters"]["platform"]["containerRegistryName"] = "syntheticregistry"
    config["parameters"]["platform"]["stage4Aks"]["name"] = "synthetic-new-aks"
    config["parameters"]["certificate-vault"] = certificate_config()["parameters"]["certificate-vault"]
    config["parameters"]["runner-connectivity"]["runnerVirtualNetworkId"] = config["parameters"]["certificate-vault"]["runnerVirtualNetworkId"]
    config["parameters"]["runner-target-connectivity"]["runnerVirtualNetworkId"] = config["parameters"]["certificate-vault"]["runnerVirtualNetworkId"]
    config["parameters"]["edge"]["adminAllowedCidrs"] = ["20.30.40.50/32"]
    return validate_config(config, "test")


class CustomerTemplateTests(unittest.TestCase):
    def test_target_connectivity_only_manages_the_resolved_dns_link(self):
        result = subprocess.run(["az", "bicep", "build", "--file", str(ROOT / "infra/runner-target-connectivity/main.bicep"), "--stdout"], capture_output=True, text=True, check=True)
        compiled = json.loads(result.stdout)
        self.assertEqual(len(compiled["resources"]), 2)
        modules = {module["resourceGroup"]: module for module in compiled["resources"]}
        for group, create in (("[parameters('dnsResourceGroupName')]", "createDnsLink"), ("[parameters('acrDnsResourceGroupName')]", "createAcrDnsLink")):
            module = modules[group]
            self.assertEqual(module["type"], "Microsoft.Resources/deployments")
            self.assertEqual(module["condition"], f"[and(parameters('manageDnsLink'), parameters('{create}'))]")
            self.assertEqual(module["properties"]["mode"], "Incremental")
            resources = module["properties"]["template"]["resources"]
            self.assertEqual(len(resources), 1)
            self.assertEqual(resources[0]["type"], "Microsoft.Network/privateDnsZones/virtualNetworkLinks")
            self.assertFalse(resources[0]["properties"]["registrationEnabled"])
            self.assertEqual(resources[0]["properties"]["resolutionPolicy"], "Default")
            self.assertEqual(resources[0]["properties"]["virtualNetwork"]["id"], "[parameters('runnerVirtualNetworkId')]" )

    def test_aks_subnet_writes_are_serialized_without_replacing_vnet(self):
        result = subprocess.run(["az", "bicep", "build", "--file", str(ROOT / "infra/modules/aks-network/main.bicep"), "--stdout"], capture_output=True, text=True, check=True)
        compiled = json.loads(result.stdout)
        resources = compiled["resources"]
        self.assertIsInstance(resources, list)
        self.assertFalse(any(resource["type"] == "Microsoft.Network/virtualNetworks" for resource in resources))
        subnets = {resource["name"]: resource for resource in resources if resource["type"] == "Microsoft.Network/virtualNetworks/subnets"}
        self.assertEqual(len(subnets), 3)
        for parameter, previous in (("systemSubnetName", None), ("userSubnetName", "systemSubnetName"), ("ingressSubnetName", "userSubnetName")):
            with self.subTest(subnet=parameter):
                resource = subnets[f"[format('{{0}}/{{1}}', parameters('virtualNetworkName'), parameters('{parameter}'))]"]
                dependencies = resource["dependsOn"]
                expected = [] if previous is None else [f"[resourceId('Microsoft.Network/virtualNetworks/subnets', parameters('virtualNetworkName'), parameters('{previous}'))]"]
                self.assertEqual([dependency for dependency in dependencies if "Microsoft.Network/virtualNetworks/subnets" in dependency], expected)
                properties = resource["properties"]
                self.assertEqual(properties["routeTable"]["id"], "[resourceId('Microsoft.Network/routeTables', 'rt-litellm-aks-egress')]")
                self.assertEqual(properties["privateEndpointNetworkPolicies"], "Enabled")
                self.assertEqual(properties["privateLinkServiceNetworkPolicies"], "Disabled" if parameter == "ingressSubnetName" else "Enabled")

    def test_aks_ingress_role_parameters_are_derived_from_stage4_platform(self):
        config = example_customer()
        template, document = parameters_for(config, 4, "aks-ingress-role")
        self.assertEqual(template, ROOT / "infra/aks-ingress-role/main.bicep")
        parameters = {name: item["value"] for name, item in document["parameters"].items()}
        platform = config["parameters"]["platform"]
        self.assertEqual(parameters, {
            "aksClusterName": platform["stage4Aks"]["name"],
            "virtualNetworkName": platform["stage4Network"]["virtualNetworkName"],
        })
        for stage in (0, 3, 5):
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                parameters_for(config, stage, "aks-ingress-role")

    def test_generated_parameters_match_compiled_bicep_contracts(self):
        config = example_customer()
        config["databaseAccess"] = {"migrationPrincipalId": "33333333-3333-4333-8333-333333333333"}
        for component, (first_stage, directory, _allowed) in COMPONENTS.items():
            selected = copy.deepcopy(config)
            if component == "network":
                selected["parameters"]["network"] = {key: selected["parameters"]["backup"][key] for key in COMPONENTS["network"][2]}
                selected["parameters"].pop("backup")
                validate_config(selected, "test")
            if component == "hub":
                selected["parameters"]["hub"] = {"resourceGroupName": "rg-hub", "virtualNetworkName": "hub-vnet", "virtualNetworkAddressPrefix": "10.40.0.0/24", "firewallSubnetPrefix": "10.40.0.0/26", "logAnalyticsWorkspaceName": "target-logs"}
                validate_config(selected, "test")
            result = subprocess.run(["az", "bicep", "build", "--file", str(ROOT / f"infra/{directory}/main.bicep"), "--stdout"], capture_output=True, text=True, check=True)
            compiled = json.loads(result.stdout)
            if component == "certificate-vault":
                self.assert_certificate_vault_contract(compiled)
            if component == "platform":
                self.assert_stage4_role_naming_contract(compiled)
            declarations = compiled["parameters"]
            for stage in ((3, 4, 5) if component == "platform" else (first_stage,)):
                with self.subTest(component=component, stage=stage):
                    _template, document = parameters_for(selected, stage, component)
                    parameters = document["parameters"]
                    self.assertFalse(set(parameters) - set(declarations))
                    self.assertFalse({name for name, declaration in declarations.items() if "defaultValue" not in declaration and not declaration.get("nullable")} - set(parameters))
                    for name, item in parameters.items():
                        declaration = declarations[name]
                        if "$ref" in declaration:
                            declaration = compiled["definitions"][declaration["$ref"].removeprefix("#/definitions/")]
                        expected = declaration["type"]
                        types = {"string": str, "object": dict, "array": list, "bool": bool, "int": int}
                        self.assertIsInstance(item["value"], types[expected], name)

    def assert_stage4_role_naming_contract(self, compiled):
        mode = compiled["parameters"]["stage4RoleAssignmentNaming"]
        self.assertEqual(mode["defaultValue"], "principal-id")
        self.assertEqual(set(mode["allowedValues"]), {"principal-id", "resource-id"})

        network_module = compiled["resources"]["aksNetworkRole"]
        self.assertEqual(network_module["properties"]["parameters"]["aksClusterName"]["value"], "[parameters('stage4Aks').name]")
        network_principal = network_module["properties"]["parameters"]["controlPlanePrincipalId"]["value"]
        self.assertIn("reference('privateAks')", network_principal)
        self.assertIn("controlPlanePrincipalId", network_principal)
        network_template = network_module["properties"]["template"]
        network_roles = [resource for resource in network_template["resources"] if resource["type"] == "Microsoft.Authorization/roleAssignments"]
        self.assertEqual(len(network_roles), 1)
        network_assignment = network_roles[0]
        network_scope = "resourceId('Microsoft.Network/virtualNetworks', parameters('virtualNetworkName'))"
        self.assertEqual(network_assignment["scope"], f"[{network_scope}]")
        self.assertEqual(network_assignment["name"], f"[guid({network_scope}, resourceId('Microsoft.ContainerService/managedClusters', parameters('aksClusterName')), variables('networkContributorRoleId'))]")
        network_role_principal = network_assignment["properties"]["principalId"]
        self.assertIn("reference(resourceId('Microsoft.ContainerService/managedClusters'", network_role_principal)
        self.assertIn("parameters('controlPlanePrincipalId')", network_role_principal)
        self.assertEqual(network_assignment["properties"]["principalType"], "ServicePrincipal")
        self.assertIn("4d97b98b-1d4f-4787-a291-c67834d212e7", network_template["variables"]["networkContributorRoleId"])

        contracts = (
            ("acrPullRole", "Microsoft.ContainerService/managedClusters", "kubeletPrincipalId", "kubeletObjectId", "Microsoft.ContainerRegistry/registries", "registryName", "acrPullRoleId", "7f951dda-4ed3-4680-a7ca-43fe172d538d"),
            ("azureOpenAIDataPlaneRoles", "Microsoft.ManagedIdentity/userAssignedIdentities", "principalId", "principalId", "Microsoft.CognitiveServices/accounts", "accountName", "cognitiveServicesOpenAIUserRoleId", "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"),
        )
        for module_name, source_type, principal_parameter, principal_output, scope_type, scope_parameter, role_variable, role_id in contracts:
            with self.subTest(module=module_name):
                module = compiled["resources"][module_name]
                source = json.dumps(module["properties"]["parameters"]["principalSourceResourceId"])
                self.assertIn("parameters('stage4RoleAssignmentNaming')", source)
                self.assertIn("resourceId('" + source_type + "'", source)
                self.assertNotIn("reference(", source)
                self.assertNotIn("principalId", source)
                actual_principal = module["properties"]["parameters"][principal_parameter]["value"]
                self.assertIn("reference(", actual_principal)
                self.assertIn(principal_output, actual_principal)
                nested = module["properties"]["template"]
                self.assertEqual(nested["parameters"]["principalSourceResourceId"]["defaultValue"], "")
                roles = [resource for resource in nested["resources"] if resource["type"] == "Microsoft.Authorization/roleAssignments"]
                self.assertEqual(len(roles), 1)
                assignment = roles[0]
                self.assertEqual(assignment["name"], f"[guid(resourceId('{scope_type}', parameters('{scope_parameter}')), if(empty(parameters('principalSourceResourceId')), parameters('{principal_parameter}'), parameters('principalSourceResourceId')), variables('{role_variable}'))]")
                self.assertEqual(assignment["scope"], f"[resourceId('{scope_type}', parameters('{scope_parameter}'))]")
                self.assertEqual(assignment["properties"]["principalId"], f"[parameters('{principal_parameter}')]")
                self.assertEqual(assignment["properties"]["principalType"], "ServicePrincipal")
                self.assertIn(role_id, nested["variables"][role_variable])

        for module_name in ("keyVault", "managedRedis"):
            with self.subTest(module=module_name):
                module = compiled["resources"][module_name]
                source = json.dumps(module["properties"]["parameters"]["principalSourceResourceId"])
                self.assertIn("parameters('stage4RoleAssignmentNaming')", source)
                self.assertIn("resourceId('Microsoft.ManagedIdentity/userAssignedIdentities'", source)
                self.assertNotIn("reference(", source)
                self.assertNotIn("principalId", source)
                nested = module["properties"]["template"]
                self.assertEqual(nested["parameters"]["principalSourceResourceId"]["defaultValue"], "")

        key_vault = compiled["resources"]["keyVault"]["properties"]["template"]
        secrets_user = next(resource for resource in key_vault["resources"] if resource["type"] == "Microsoft.Authorization/roleAssignments" and "keyVaultSecretsUserRoleId" in resource["properties"]["roleDefinitionId"])
        self.assertEqual(secrets_user["name"], "[guid(resourceId('Microsoft.KeyVault/vaults', parameters('name')), if(empty(parameters('principalSourceResourceId')), parameters('workloadPrincipalId'), parameters('principalSourceResourceId')), variables('keyVaultSecretsUserRoleId'))]")
        self.assertEqual(secrets_user["properties"]["principalId"], "[parameters('workloadPrincipalId')]")

        managed_redis = compiled["resources"]["managedRedis"]["properties"]["template"]
        redis_assignment = next(resource for resource in managed_redis["resources"] if resource["type"] == "Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments")
        self.assertIn("if(empty(parameters('principalSourceResourceId')), parameters('workloadPrincipalId'), uniqueString(", redis_assignment["name"])
        self.assertEqual(redis_assignment["properties"]["user"]["objectId"], "[parameters('workloadPrincipalId')]")

    def assert_certificate_vault_contract(self, compiled):
        resources = compiled["resources"]
        resources = list(resources.values()) if isinstance(resources, dict) else resources
        vaults = [item for item in resources if item["type"] == "Microsoft.KeyVault/vaults"]
        self.assertEqual(len(vaults), 1)
        properties = vaults[0]["properties"]
        self.assertEqual(properties["publicNetworkAccess"], "Disabled")
        self.assertEqual(properties["networkAcls"]["bypass"], "None")
        self.assertEqual(properties["networkAcls"]["defaultAction"], "Deny")
        self.assertTrue(properties["enableRbacAuthorization"])
        self.assertTrue(properties["enablePurgeProtection"])
        self.assertTrue(properties["enableSoftDelete"])
        self.assertEqual(properties["softDeleteRetentionInDays"], 90)
        self.assertEqual(properties["sku"]["name"], "standard")
        roles = [item for item in resources if item["type"] == "Microsoft.Authorization/roleAssignments"]
        self.assertEqual(len(roles), 2)
        for assignment, principal, role in zip(roles, ("ingressReaderPrincipalId", "certificateImporterPrincipalId"), ("4633458b-17de-408a-b874-0445c86b69e6", "b86a8fe4-44ce-4948-aee5-eccb2c155cd7")):
            self.assertEqual(assignment["properties"]["principalId"], f"[parameters('{principal}')]")
            self.assertIn(role, assignment["properties"]["roleDefinitionId"])
            self.assertIn("Microsoft.KeyVault/vaults", assignment["scope"])
        self.assertFalse(any(item["type"] == "Microsoft.KeyVault/vaults/secrets" for item in resources))
        outputs = compiled["outputs"]["certificateVault"]["value"]
        self.assertFalse(outputs["certificateMaterialsImported"])
        self.assertIn("secrets/api-tls", outputs["apiTlsSecretId"])
        self.assertIn("secrets/admin-tls", outputs["adminTlsSecretId"])