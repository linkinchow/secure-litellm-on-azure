import copy
import json
import subprocess
import unittest

import yaml

from scripts.audit_manifest import render_audit_documents
from scripts.audit_runtime import recovery_documents
from scripts.customer_migration import ROOT, USER_POOL_SELECTOR, MigrationError, validate_config
from scripts.observability import COLLECTOR_DIGEST, render_observability
from scripts.private_ingress import render_ingress
from tests.test_audit_manifest import fixture
from tests.test_availability_zones import backend_documents, proxy_documents, zonal
from tests.test_backend_manifest import backend_customer
from tests.test_customer_migration import customer_config
from tests.test_proxy_config import proxy_customer

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}


def pod_templates(documents):
    """Map kind/name to the pod spec of every workload controller in a rendered document set."""
    result = {}
    for item in documents:
        if not isinstance(item, dict) or item.get("kind") not in WORKLOAD_KINDS:
            continue
        spec = item["spec"]["jobTemplate"]["spec"] if item["kind"] == "CronJob" else item["spec"]
        result[item["kind"] + "/" + item["metadata"]["name"]] = spec["template"]["spec"]
    return result


def compile_template(path):
    return json.loads(subprocess.run(["az", "bicep", "build", "--file", str(ROOT / path), "--stdout"], capture_output=True, text=True, check=True).stdout)


def template_resources(template):
    return template["resources"].values() if isinstance(template["resources"], dict) else template["resources"]


class SystemPoolTaintTests(unittest.TestCase):
    def test_option_is_a_boolean(self):
        for value in (True, False):
            config = customer_config()
            config["parameters"]["platform"]["stage4Aks"]["systemPoolCriticalAddonsOnly"] = value
            validate_config(config, "test")
        for value in ("true", 1, None, ["CriticalAddonsOnly"]):
            config = customer_config()
            config["parameters"]["platform"]["stage4Aks"]["systemPoolCriticalAddonsOnly"] = value
            with self.subTest(value=value), self.assertRaisesRegex(MigrationError, "systemPoolCriticalAddonsOnly"):
                validate_config(config, "test")

    def test_module_taints_the_system_pool_only_when_asked(self):
        template = compile_template("infra/modules/private-aks/main.bicep")
        parameter = template["parameters"]["systemPoolCriticalAddonsOnly"]
        self.assertEqual((parameter["type"], parameter["defaultValue"]), ("bool", False))
        cluster = next(item for item in template_resources(template) if item["type"] == "Microsoft.ContainerService/managedClusters")
        system = next(pool for pool in cluster["properties"]["agentPoolProfiles"] if pool["mode"] == "System")
        # false sends null, which AKS treats like the omitted property the template had before this option.
        self.assertEqual(system["nodeTaints"], "[if(parameters('systemPoolCriticalAddonsOnly'), createArray('CriticalAddonsOnly=true:NoSchedule'), null())]")
        user = next(item for item in template_resources(template) if item["type"] == "Microsoft.ContainerService/managedClusters/agentPools")
        self.assertEqual(user["properties"]["mode"], "User")
        self.assertNotIn("nodeTaints", user["properties"])
        self.assertEqual(user["properties"]["nodeLabels"], USER_POOL_SELECTOR)

    def test_environment_defaults_to_an_untainted_system_pool(self):
        environment = (ROOT / "infra/environments/main.bicep").read_text()
        self.assertIn("  systemPoolCriticalAddonsOnly: bool?\n", environment)
        self.assertIn("systemPoolCriticalAddonsOnly: stage4Aks.?systemPoolCriticalAddonsOnly ?? false", environment)


class UserPoolPlacementTests(unittest.TestCase):
    def assertOnUserPool(self, documents, expected):
        templates = pod_templates(documents)
        self.assertTrue(set(expected).issubset(templates), sorted(templates))
        for name, pod in templates.items():
            with self.subTest(workload=name):
                self.assertEqual(pod.get("nodeSelector"), USER_POOL_SELECTOR)

    def test_every_source_workload_selects_the_user_pool(self):
        found = set()
        for path in sorted((ROOT / "deploy").rglob("*.yaml")):
            for document in yaml.safe_load_all(path.read_text()):
                if not isinstance(document, dict) or document.get("kind") not in WORKLOAD_KINDS:
                    continue
                spec = document.get("spec", {})
                # Strategic-merge patches carry no selector or schedule; their base supplies the pod placement.
                if "selector" not in spec and "schedule" not in spec:
                    continue
                name = f"{path.relative_to(ROOT).as_posix()}:{document['kind']}/{document['metadata']['name']}"
                found.add(name)
                pod = (spec["jobTemplate"]["spec"] if document["kind"] == "CronJob" else spec)["template"]["spec"]
                with self.subTest(workload=name):
                    self.assertEqual(pod.get("nodeSelector"), USER_POOL_SELECTOR)
        self.assertEqual({name.split(":")[1] for name in found}, {"Deployment/litellm", "Deployment/llm-api-proxy", "Deployment/llm-admin-proxy", "Deployment/otel-collector", "CronJob/l3-retention"})

    def test_backend(self):
        for config in (backend_customer(), zonal(backend_customer())):
            self.assertOnUserPool(backend_documents(config), {"Deployment/litellm"})

    def test_auth_proxies(self):
        for config in (proxy_customer(), zonal(proxy_customer())):
            self.assertOnUserPool(proxy_documents(config), {"Deployment/llm-api-proxy", "Deployment/llm-admin-proxy"})

    def test_private_ingress(self):
        config = customer_config()
        config["parameters"]["platform"]["stage4Network"].update(ingressSubnetName="snet-ingress", ingressSubnetPrefix="10.30.4.0/24")
        for plane in ("api", "admin"):
            self.assertOnUserPool(render_ingress(copy.deepcopy(config), plane, "registry.invalid/traefik@sha256:" + "a" * 64, ["10.30.0.0/16"]), {f"Deployment/llm-{plane}-ingress"})

    def test_audit_workloads_and_collector(self):
        config, source, foundation, storage = fixture()
        audit = render_audit_documents(config, source, foundation, storage, "10.30.8.0/24")
        self.assertOnUserPool(audit, {"Deployment/llm-api-proxy", "Deployment/llm-admin-proxy", "CronJob/l3-retention"})
        config["observability"] = {"collectorImage": config["parameters"]["platform"]["containerRegistryName"] + ".azurecr.io/collector@" + COLLECTOR_DIGEST}
        receipt = {"identity": {"clientId": "88888888-8888-4888-8888-888888888888", "serviceAccountName": "otel-collector", "kubernetesNamespace": "litellm"}, "connectionString": "InstrumentationKey=55555555-5555-4555-8555-555555555555;IngestionEndpoint=https://westus-0.in.applicationinsights.azure.com/"}
        self.assertOnUserPool(render_observability(config, audit, receipt, "10.30.8.0/24"), {"Deployment/otel-collector", "CronJob/l3-retention"})

    def test_recovery_job(self):
        documents = recovery_documents({"clientId": "synthetic"}, "synthetic.azurecr.io/proxy@sha256:" + "a" * 64, {}, "l3-recovery-test", "10.30.8.0/24", "10.30.0.10/32")
        self.assertOnUserPool(documents, {"Job/l3-recovery-test"})


if __name__ == "__main__":
    unittest.main()
