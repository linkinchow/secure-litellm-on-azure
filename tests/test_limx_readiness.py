import copy
import unittest

from scripts.customer_migration import ROOT, MigrationError, validate_config
from tests.test_runtime_config import native_customer


def platform_customer():
    config = native_customer()
    platform = config["parameters"]["platform"]
    subscription = config["azure"]["subscriptionId"]
    platform["azureOpenAIConnections"] = [{
        "alias": "api1", "accountName": "api1-resource", "subscriptionId": subscription, "resourceGroupName": "rg-models",
        "accountResourceId": f"/subscriptions/{subscription}/resourceGroups/rg-models/providers/Microsoft.CognitiveServices/accounts/api1-resource",
    }]
    return config


class PlatformOptionTests(unittest.TestCase):
    def test_upgrade_channels_redis_ha_and_public_model_access_are_validated(self):
        config = platform_customer()
        platform = config["parameters"]["platform"]
        platform["stage4Aks"].update(upgradeChannel="none", nodeOSUpgradeChannel="None")
        platform.setdefault("stage5Data", {})["redisHighAvailability"] = "Disabled"
        platform["azureOpenAIConnections"][0]["privateEndpoint"] = False
        validate_config(copy.deepcopy(config))
        for path, value in ((("stage4Aks", "upgradeChannel"), "weekly"), (("stage4Aks", "nodeOSUpgradeChannel"), "none"),
                            (("stage5Data", "redisHighAvailability"), "Off")):
            broken = copy.deepcopy(config)
            broken["parameters"]["platform"][path[0]][path[1]] = value
            with self.subTest(path=path), self.assertRaises(MigrationError):
                validate_config(broken)
        for connection in ({"privateEndpoint": "no"}, {"privateEndpoint": False, "endpoint": "https://gateway.example.invalid"}):
            broken = copy.deepcopy(config)
            broken["parameters"]["platform"]["azureOpenAIConnections"][0].update(connection)
            with self.subTest(connection=connection), self.assertRaises(MigrationError):
                validate_config(broken)

    def test_templates_keep_safe_defaults_and_derive_public_model_access(self):
        aks = (ROOT / "infra/modules/private-aks/main.bicep").read_text()
        environment = (ROOT / "infra/environments/main.bicep").read_text()
        dns = (ROOT / "infra/modules/private-dns/main.bicep").read_text()
        self.assertIn("param upgradeChannel string = 'patch'", aks)
        self.assertIn("param nodeOSUpgradeChannel string = 'NodeImage'", aks)
        self.assertIn("upgradeChannel: stage4Aks.?upgradeChannel ?? 'patch'", environment)
        self.assertIn("if (deployStage4 && (connection.?privateEndpoint ?? true))", environment)
        self.assertIn("union(approvedHttpsFqdns, azureEnvironmentHttpsFqdns, publicModelHosts)", environment)
        self.assertIn("azureOpenAIFallbackToInternet: !empty(publicModelHosts)", environment)
        self.assertIn("resolutionPolicy: azureOpenAIFallbackToInternet ? 'NxDomainRedirect' : 'Default'", dns)
        # Data-plane role assignments stay per account whatever the network path.
        self.assertIn("module azureOpenAIDataPlaneRoles '../modules/model-access-role/main.bicep' = [for connection in azureOpenAIConnections: if (deployStage4) {", environment)


if __name__ == "__main__":
    unittest.main()
