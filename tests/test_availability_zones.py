import copy
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.admin_credentials import OIDC_SECRET, SESSION_SECRET
from scripts.audit_manifest import render_audit_documents
from scripts.backend_manifest import render_backend_manifest
from scripts.customer_migration import ROOT, MigrationError, validate_config
from scripts.migration_deploy import deploy_component, group_id, inspect_zone_capacity
from scripts.observability import COLLECTOR_DIGEST, render_observability
from scripts.private_ingress import render_ingress
from scripts.proxy_credentials import binding_contract, credential_bindings
from scripts.proxy_manifest import render_proxy_documents
from scripts.runtime_secrets import BACKEND_SECRETS
from tests.test_audit_manifest import fixture
from tests.test_backend_manifest import backend_customer
from tests.test_customer_migration import customer_config
from tests.test_proxy_config import APPS, proxy_customer

ZONE_KEY = "topology.kubernetes.io/zone"
HOST_KEY = "kubernetes.io/hostname"


def zonal(config, zones=("1", "2", "3"), nodes=3):
    config["parameters"]["platform"]["stage4Aks"].update(
        availabilityZones=list(zones), systemNodeVmSize="Standard_D4s_v5", systemNodeCount=nodes,
        userNodeVmSize="Standard_D4s_v5", userNodeCount=nodes,
    )
    return config


def sku(name="Standard_D4s_v5", zones=("1", "2", "3"), restrictions=()):
    return {"name": name, "locationInfo": [{"location": "westus3", "zones": list(zones)}], "restrictions": list(restrictions)}


class FakeSkus:
    def __init__(self, offers):
        self.offers = offers
        self.calls = []

    def scoped(self, command):
        self.calls.append(command)
        return self.offers


def backend_documents(config):
    platform = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": "33333333-3333-4333-8333-333333333333", "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
    versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32} for name in BACKEND_SECRETS}
    return render_backend_manifest(config, platform, versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24")


def proxy_documents(config):
    config["proxy"]["image"] = "customerregistry.azurecr.io/auth-proxy@sha256:" + "a" * 64
    foundation = {plane: {"identity": {"clientId": APPS[plane]["appId"], "serviceAccountName": f"llm-{plane}-proxy", "kubernetesNamespace": "litellm"}, "vault": {"name": "synthetic-" + plane, "id": group_id(config) + "/providers/Microsoft.KeyVault/vaults/synthetic-" + plane}} for plane in ("api", "admin")}

    def secret(plane, name):
        return {"id": f"https://synthetic-{plane}.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32, "expiresAt": "2099-01-01T00:00:00+00:00" if name == OIDC_SECRET else None}

    applications = {plane: {"id": value["appId"], **value} for plane, value in APPS.items()}
    admin = {"application": applications["admin"], "vaultId": foundation["admin"]["vault"]["id"], "secrets": {name: secret("admin", name) for name in (OIDC_SECRET, SESSION_SECRET)}}
    credentials = {"bindings": []}
    for binding in credential_bindings(config):
        contract = binding_contract(config, binding)
        credentials["bindings"].append({"contract": contract, "secret": {**secret(binding["plane"], contract["secretName"]), "state": "ready"}, "userExists": True, "keyExists": True})
    return render_proxy_documents(config, foundation, applications, admin, credentials)


def pods(documents):
    return {item["metadata"]["name"]: item["spec"]["template"]["spec"] for item in documents if item["kind"] == "Deployment"}


class AvailabilityZoneTests(unittest.TestCase):
    def test_configuration_accepts_regional_or_multi_zone_pools_only(self):
        validate_config(customer_config(), "test")
        validate_config(zonal(customer_config()), "test")
        validate_config(zonal(customer_config(), zones=("1", "3"), nodes=2), "test")
        for zones, nodes in ((["1"], 3), (["1", "1"], 3), (["1", "4"], 3), ([1, 2], 3), ("123", 3), (["1", "2", "3"], 2)):
            config = zonal(customer_config(), nodes=nodes)
            config["parameters"]["platform"]["stage4Aks"]["availabilityZones"] = zones
            with self.subTest(zones=zones, nodes=nodes), self.assertRaises(MigrationError):
                validate_config(config, "test")

    def test_regional_backend_is_unchanged(self):
        documents = backend_documents(backend_customer())
        constraints = pods(documents)["litellm"]["topologySpreadConstraints"]
        self.assertEqual([item["topologyKey"] for item in constraints], [HOST_KEY])
        autoscaler = next(item for item in documents if item["kind"] == "HorizontalPodAutoscaler")
        self.assertEqual(autoscaler["spec"]["minReplicas"], 2)

    def test_zonal_backend_prefers_one_replica_per_zone(self):
        documents = backend_documents(zonal(backend_customer()))
        constraints = pods(documents)["litellm"]["topologySpreadConstraints"]
        self.assertEqual(constraints[0]["topologyKey"], HOST_KEY)
        self.assertEqual(constraints[0]["whenUnsatisfiable"], "DoNotSchedule")
        zone = constraints[1]
        self.assertEqual((zone["topologyKey"], zone["whenUnsatisfiable"], zone["maxSkew"]), (ZONE_KEY, "ScheduleAnyway", 1))
        self.assertEqual(zone["labelSelector"], constraints[0]["labelSelector"])
        autoscaler = next(item for item in documents if item["kind"] == "HorizontalPodAutoscaler")
        self.assertEqual(autoscaler["spec"]["minReplicas"], 3)
        self.assertGreaterEqual(autoscaler["spec"]["maxReplicas"], 3)
        self.assertEqual(backend_documents(zonal(backend_customer(), zones=("1", "2"), nodes=2))[-1]["spec"]["minReplicas"], 2)

    def test_private_ingress_spreads_each_plane_across_zones(self):
        for zones in ((), ("1", "2", "3")):
            config = zonal(customer_config(), zones=zones, nodes=3)
            config["parameters"]["platform"]["stage4Network"].update(ingressSubnetName="snet-ingress", ingressSubnetPrefix="10.30.4.0/24")
            for plane in ("api", "admin"):
                documents = render_ingress(config, plane, "registry.invalid/traefik@sha256:" + "a" * 64, ["10.30.0.0/16"])
                constraints = pods(documents)[f"llm-{plane}-ingress"]["topologySpreadConstraints"]
                with self.subTest(zones=zones, plane=plane):
                    self.assertEqual([item["topologyKey"] for item in constraints], [HOST_KEY, ZONE_KEY] if zones else [HOST_KEY])
                    self.assertEqual(constraints[-1]["labelSelector"]["matchLabels"]["plane"], plane)

    def test_auth_proxies_spread_across_zones(self):
        regional = pods(proxy_documents(proxy_customer()))
        spread = pods(proxy_documents(zonal(proxy_customer())))
        self.assertEqual(set(regional), {"llm-api-proxy", "llm-admin-proxy"})
        for name, pod in spread.items():
            with self.subTest(name=name):
                self.assertEqual([item["topologyKey"] for item in regional[name]["topologySpreadConstraints"]], [HOST_KEY])
                self.assertEqual([item["topologyKey"] for item in pod["topologySpreadConstraints"]], [HOST_KEY, ZONE_KEY])
                self.assertEqual(pod["topologySpreadConstraints"][1]["labelSelector"], pod["topologySpreadConstraints"][0]["labelSelector"])

    def test_telemetry_collector_spreads_across_zones(self):
        receipt = {"identity": {"clientId": "88888888-8888-4888-8888-888888888888", "serviceAccountName": "otel-collector", "kubernetesNamespace": "litellm"}, "connectionString": "InstrumentationKey=55555555-5555-4555-8555-555555555555;IngestionEndpoint=https://westus-0.in.applicationinsights.azure.com/"}
        for zones in ((), ("1", "2", "3")):
            config, source, foundation, storage = fixture()
            zonal(config, zones=zones)
            config["observability"] = {"collectorImage": config["parameters"]["platform"]["containerRegistryName"] + ".azurecr.io/collector@" + COLLECTOR_DIGEST}
            result = render_observability(config, render_audit_documents(config, source, foundation, storage, "10.30.8.0/24"), receipt, "10.30.8.0/24")
            collector = next(item for item in result if item["kind"] == "Deployment" and item["metadata"]["name"] == "otel-collector")
            constraints = collector["spec"]["template"]["spec"].get("topologySpreadConstraints", [])
            with self.subTest(zones=zones):
                self.assertEqual(collector["spec"]["replicas"], 2)
                self.assertEqual([item["topologyKey"] for item in constraints], [ZONE_KEY] if zones else [])
                if zones:
                    self.assertEqual(constraints[0]["labelSelector"], collector["spec"]["selector"])
                    self.assertEqual(constraints[0]["whenUnsatisfiable"], "ScheduleAnyway")

    def test_zone_capacity_requires_every_zone_for_each_node_size(self):
        config = zonal(customer_config())
        azure = FakeSkus([sku("Standard_D4s_v5_Promo"), sku()])
        inspect_zone_capacity(config, azure)
        self.assertEqual(len(azure.calls), 1)
        self.assertIn("--all", azure.calls[0])
        self.assertEqual(azure.calls[0][azure.calls[0].index("--location") + 1], config["location"])
        failures = {
            "is not offered": [sku("Standard_D4s_v5_Promo")],
            "offers no availability zones": [sku(zones=())],
            "zones 3": [sku(zones=("1", "2"))],
            "zones 2": [sku(restrictions=[{"type": "Zone", "restrictionInfo": {"zones": ["2"]}}])],
            "restricted for this subscription": [sku(restrictions=[{"type": "Location", "restrictionInfo": {"locations": ["westus3"]}}])],
        }
        for message, offers in failures.items():
            with self.subTest(message=message), self.assertRaisesRegex(MigrationError, message):
                inspect_zone_capacity(config, FakeSkus(offers))
        mixed = zonal(customer_config())
        mixed["parameters"]["platform"]["stage4Aks"]["systemNodeVmSize"] = "Standard_D2s_v5"
        azure = FakeSkus([sku(), sku("Standard_D2s_v5")])
        inspect_zone_capacity(mixed, azure)
        self.assertEqual([call[call.index("--size") + 1] for call in azure.calls], ["Standard_D2s_v5", "Standard_D4s_v5"])

    def test_platform_plan_checks_zone_capacity_before_compiling(self):
        class Context:
            def __init__(self, config):
                self.config = config

            def run(self, _command):
                return {"tenantId": self.config["azure"]["tenantId"], "id": self.config["azure"]["subscriptionId"]}

        stop = MigrationError("stopped before compile")
        for stage, zones, expected in ((4, ("1", "2", "3"), 1), (4, (), 0), (3, ("1", "2", "3"), 0)):
            config = zonal(customer_config(), zones=zones)
            with self.subTest(stage=stage, zones=zones), tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, \
                    patch("scripts.migration_deploy.inspect_zone_capacity") as inspect, \
                    patch("scripts.migration_deploy.resolve_origin", side_effect=stop):
                with self.assertRaisesRegex(MigrationError, "stopped before compile"):
                    deploy_component(config, stage, "platform", "a" * 40, "plan", [], directory, azure=Context(config))
                self.assertEqual(inspect.call_count, expected)

    def test_node_pool_zones_are_optional_in_the_template(self):
        template = json.loads(subprocess.run(["az", "bicep", "build", "--file", str(ROOT / "infra/modules/private-aks/main.bicep"), "--stdout"], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(template["parameters"]["availabilityZones"]["defaultValue"], [])
        self.assertEqual(template["variables"]["poolZones"], "[if(empty(parameters('availabilityZones')), null(), parameters('availabilityZones'))]")
        self.assertEqual(template["variables"]["minimumNodeCount"], "[max(2, length(parameters('availabilityZones')))]")
        resources = template["resources"].values() if isinstance(template["resources"], dict) else template["resources"]
        pools = [pool for item in resources if item["type"].startswith("Microsoft.ContainerService/managedClusters")
                 for pool in item["properties"].get("agentPoolProfiles", [item["properties"]]) if "minCount" in pool]
        self.assertEqual(len(pools), 2)
        for pool in pools:
            self.assertEqual((pool["availabilityZones"], pool["minCount"]), ("[variables('poolZones')]", "[variables('minimumNodeCount')]"))


if __name__ == "__main__":
    unittest.main()
