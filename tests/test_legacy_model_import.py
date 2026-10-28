import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import yaml

from scripts import legacy_model_import as importer
from scripts.backend_manifest import application_settings, render_backend_manifest
from scripts.customer_migration import ROOT, MigrationError, validate_config
from scripts.runtime_secrets import backend_secrets
from tests.test_runtime_config import native_customer


GROUPS = ["gpt-6-astra", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5", "DeepSeek-V4-Flash"]
SUBSCRIPTIONS = ["7a1b2c3d-0000-4000-8000-00000000000%d" % index for index in range(1, 4)]
# 41 of api1..api50, as in the customer estate.
NUMBERS = [number for number in range(1, 51) if number not in {4, 9, 13, 18, 22, 27, 31, 36, 44}]
SECRET = "sk-legacy-super-secret-value-123"


def account_name(number):
    return f"api{number}-resource"


def limx_accounts(**changes):
    data = []
    for number in NUMBERS:
        subscription = SUBSCRIPTIONS[number % 3]
        group = f"rg-aoai-{number}"
        data.append({"id": f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.CognitiveServices/accounts/{account_name(number)}",
                     "name": account_name(number), "resourceGroup": group, "subscriptionId": subscription,
                     "location": "eastus", "kind": "OpenAI", "customSubDomainName": account_name(number)})
    return {"count": len(data), "data": data, "skip_token": None, **changes}


def limx_legacy():
    models = []
    for group in GROUPS:
        for number in NUMBERS:
            params = {"model": "azure/" + group, "api_base": f"https://{account_name(number)}.openai.azure.com/",
                      "api_version": "2025-04-01-preview", "tenant_id": "os.environ/AZURE_TENANT_ID",
                      "client_id": "os.environ/AZURE_CLIENT_ID", "rpm": 600, "tpm": 600000}
            info = {"mode": "chat"}
            if group == "gpt-5.5":
                info.update(base_model="azure/gpt-5.5", id=f"gpt55-{account_name(number)}",
                            input_cost_per_token=0.00000125, output_cost_per_token=0.00001)
            if group == "DeepSeek-V4-Flash":
                params.update(weight=2, input_cost_per_token=0.0000002)
            models.append({"model_name": group, "litellm_params": params, "model_info": info})
    return {"model_list": models,
            "litellm_settings": {"enable_azure_ad_token_refresh": True, "drop_params": True},
            "router_settings": {"routing_strategy": "simple-shuffle"},
            "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"}}


BASE_MODELS = ["gpt-6-astra=gpt-6-astra", "gpt-5.6-terra=gpt-5.6-terra", "gpt-5.6-sol=gpt-5.6-sol",
               "gpt-5.6-luna=gpt-5.6-luna", "DeepSeek-V4-Flash=DeepSeek-V4-Flash"]


class LegacyImportCase(unittest.TestCase):
    def setUp(self):
        (ROOT / "temp").mkdir(exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(dir=ROOT / "temp"))

    def tearDown(self):
        for path in sorted(self.directory.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        self.directory.rmdir()

    def files(self, legacy, accounts, raw=False):
        legacy_path, accounts_path = self.directory / "legacy.yaml", self.directory / "accounts.json"
        legacy_path.write_text(legacy if raw else yaml.safe_dump(legacy, sort_keys=False))
        accounts_path.write_text(json.dumps(accounts))
        return legacy_path, accounts_path

    def run_import(self, legacy=None, accounts=None, raw=False, **options):
        legacy_path, accounts_path = self.files(limx_legacy() if legacy is None else legacy,
                                                limx_accounts() if accounts is None else accounts, raw)
        options.setdefault("base_models", BASE_MODELS)
        output = self.directory / "out"
        summary = importer.run(legacy_path, accounts_path, output, **options)
        fragment = json.loads((output / "model-fragments.json").read_text()) if (output / "model-fragments.json").exists() else None
        return summary, fragment, output


class LimxShapeTests(LegacyImportCase):
    def test_246_deployments_import_validate_and_render_within_configmap_limit(self):
        summary, fragment, output = self.run_import()
        self.assertEqual(summary["errors"], [])
        self.assertEqual((summary["legacyModelEntries"], summary["importedModels"], summary["connections"]), (246, 246, 41))
        self.assertEqual({name: item["deployments"] for name, item in summary["modelGroups"].items()}, {group: 41 for group in GROUPS})
        self.assertTrue(all(item["accounts"] == 41 and item["apiVersions"] == ["2025-04-01-preview"] for item in summary["modelGroups"].values()))
        self.assertEqual({item["deployments"] for item in summary["accounts"].values()}, {6})
        self.assertEqual(summary["locations"], ["eastus"])
        self.assertEqual(len(summary["subscriptions"]), 3)
        self.assertEqual({name: item["count"] for name, item in summary["droppedFields"].items()},
                         {"litellm_params.client_id": 246, "litellm_params.tenant_id": 246, "litellm_params.weight": 41})
        self.assertEqual(summary["movedFields"], {"litellm_params.input_cost_per_token -> model_info": 41})
        self.assertEqual(summary["notImportedSections"], ["general_settings", "litellm_settings", "router_settings"])
        self.assertTrue(summary["legacyAzureAdTokenRefresh"])
        connections, models = fragment["parameters"]["platform"]["azureOpenAIConnections"], fragment["application"]["models"]
        self.assertTrue(all(item["privateEndpoint"] is False and "endpoint" not in item for item in connections))
        self.assertEqual(len({item["id"] for item in models}), 246)
        self.assertEqual({item["id"] for item in models if item["modelGroup"] == "gpt-5.5"},
                         {f"gpt55-{account_name(number)}" for number in NUMBERS})
        self.assertTrue(all(item["baseModel"] == "azure/" + item["modelGroup"] for item in models))
        self.assertEqual(next(item for item in models if item["modelGroup"] == "DeepSeek-V4-Flash")["modelInfo"],
                         {"mode": "chat", "input_cost_per_token": 0.0000002})
        self.assertEqual(models[0]["litellmParams"], {"rpm": 600, "tpm": 600000})
        if os.name == "posix":
            self.assertEqual((output / "model-fragments.json").stat().st_mode & 0o777, 0o600)

        # The fragments must pass the platform's own validators and renderer.
        config = native_customer()
        config["parameters"]["platform"]["azureOpenAIConnections"] = connections
        config["application"]["models"] = models
        validate_config(copy.deepcopy(config), "test")
        application_settings(config)
        platform = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": "33333333-3333-4333-8333-333333333333",
                    "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "managedRedisHostName": "synthetic.eastus.redis.azure.net"}
        versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32}
                    for name in backend_secrets(config)}
        documents = render_backend_manifest(config, platform, versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24")
        config_map = next(item for item in documents if item["kind"] == "ConfigMap" and "config.yaml" in item.get("data", {}))
        runtime = yaml.safe_load(config_map["data"]["config.yaml"])
        self.assertEqual(len(runtime["model_list"]), 246)
        first = runtime["model_list"][0]["litellm_params"]
        self.assertEqual((first["model"], first["api_base"], first["api_version"]),
                         ("azure/gpt-6-astra", "https://api1-resource.openai.azure.com", "2025-04-01-preview"))
        self.assertEqual(set(runtime["router_settings"]["model_group_affinity_config"]), set(GROUPS))
        size = len(yaml.safe_dump(config_map, sort_keys=False).encode())
        self.assertLess(size, importer.CONFIGMAP_LIMIT // 4)
        self.assertLess(summary["renderedModelListBytes"], size)
        self.assertGreater(summary["configMapHeadroomBytes"], importer.CONFIGMAP_LIMIT * 3 // 4)
        print(f"\nlimx 246-model ConfigMap: {size} bytes; model_list {summary['renderedModelListBytes']} bytes "
              f"(limit {importer.CONFIGMAP_LIMIT})")

    def test_output_is_deterministic_and_regenerated_ids_are_stable(self):
        _, first, _ = self.run_import(preserve_legacy_ids=False)
        self.tearDown()
        self.setUp()
        _, second, _ = self.run_import(preserve_legacy_ids=False)
        self.assertEqual(first, second)
        self.assertTrue(all(item["id"].startswith("legacy-") for item in first["application"]["models"]))

    def test_configmap_wrapper_name_map_and_private_endpoint_flag(self):
        wrapped = yaml.safe_dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "litellm-config"},
                                  "data": {"config.yaml": yaml.safe_dump(limx_legacy())}})
        names = {account_name(number): {"subscriptionId": SUBSCRIPTIONS[number % 3], "resourceGroupName": f"rg-aoai-{number}"}
                 for number in NUMBERS}
        summary, fragment, _ = self.run_import(wrapped, names, raw=True, private_endpoint=True)
        self.assertEqual(summary["importedModels"], 246)
        self.assertTrue(all(item["privateEndpoint"] is True for item in fragment["parameters"]["platform"]["azureOpenAIConnections"]))

    def test_paginated_resource_graph_output_is_refused(self):
        with self.assertRaisesRegex(MigrationError, "paginated"):
            self.run_import(accounts=limx_accounts(skip_token="abc"))


class RefusalTests(LegacyImportCase):
    def legacy_with(self, params=None, info=None, entry=None, index=0):
        legacy = limx_legacy()
        legacy["model_list"][index]["litellm_params"].update(params or {})
        legacy["model_list"][index]["model_info"].update(info or {})
        legacy["model_list"][index].update(entry or {})
        return legacy

    def assert_refused(self, legacy, pattern, accounts=None, **options):
        summary, fragment, output = self.run_import(legacy, accounts, **options)
        self.assertIsNone(fragment)
        self.assertTrue(any(__import__("re").search(pattern, error) for error in summary["errors"]), summary["errors"])
        self.assertNotIn(SECRET, (output / "import-summary.json").read_text())
        return summary

    def test_credentials_are_refused_and_never_echoed(self):
        cases = [({"api_key": SECRET}, "credential value"),
                 ({"api_key": "os.environ/AZURE_API_KEY"}, "key/token authentication"),
                 ({"azure_ad_token": SECRET}, "credential value"),
                 ({"client_secret": "os.environ/AZURE_CLIENT_SECRET"}, "key/token authentication"),
                 ({"extra_headers": {"api-key": SECRET}}, "credential value"),
                 ({"headers": {"Authorization": "Bearer " + SECRET}}, "credential value"),
                 ({"tenant_id": "os.environ/OTHER_TENANT"}, "outside AZURE_AUTHORITY_HOST"),
                 ({"organization": "os.environ/OPENAI_ORG"}, "references os.environ"),
                 ({"temperature": "os.environ/TEMP"}, "references os.environ"),
                 ({"api_base": "os.environ/AZURE_API_BASE"}, "must be a literal"),
                 ({"litellm_credential_name": "aoai"}, "credential value")]
        for params, pattern in cases:
            with self.subTest(params=list(params)):
                self.assert_refused(self.legacy_with(params), pattern)
        with self.subTest("model_info secret"):
            self.assert_refused(self.legacy_with(info={"access_token": SECRET}), "credential value")
        with self.subTest("entry-level secret"):
            self.assert_refused(self.legacy_with(entry={"api_key": SECRET}), "credential value")

    def test_unsupported_shapes_are_reported(self):
        self.assert_refused(self.legacy_with({"model": "openai/gpt-4o"}), "other providers")
        self.assert_refused(self.legacy_with({"api_base": "https://api99-resource.openai.azure.com"}), "not in the account map")
        self.assert_refused(self.legacy_with({"api_base": "https://gateway.example.net"}), "api_base")
        self.assert_refused(self.legacy_with({"temperature": 7}), "allowlisted option failed")
        self.assert_refused(self.legacy_with(info={"base_model": "azure/gpt-4o"}, index=41), "mixes base models")
        self.assert_refused(self.legacy_with(info={"id": "gpt55-api2-resource"}, index=4 * 41), "duplicates")
        legacy = limx_legacy()
        del legacy["model_list"][0]["litellm_params"]["api_version"]
        self.assert_refused(legacy, "api_version")
        summary, fragment, _ = self.run_import(legacy, default_api_version="2025-04-01-preview")
        self.assertEqual(summary["errors"], [])
        self.assertEqual(fragment["application"]["models"][0]["apiVersion"], "2025-04-01-preview")

    def test_custom_subdomain_needs_private_endpoint(self):
        accounts = limx_accounts()
        accounts["data"][0]["customSubDomainName"] = "astra-east"
        legacy = limx_legacy()
        for entry in legacy["model_list"]:
            entry["litellm_params"]["api_base"] = entry["litellm_params"]["api_base"].replace("api1-resource", "astra-east")
        self.assert_refused(legacy, "must use https://<accountName>", accounts)
        summary, fragment, _ = self.run_import(legacy, accounts, private_endpoint=True)
        self.assertEqual(summary["errors"], [])
        connection = next(item for item in fragment["parameters"]["platform"]["azureOpenAIConnections"] if item["accountName"] == "api1-resource")
        self.assertEqual(connection["endpoint"], "https://astra-east.openai.azure.com")

    def test_deployment_list_supplies_base_models(self):
        deployments = []
        accounts = limx_accounts()["data"]
        for group in GROUPS:
            for account in accounts:
                deployments.append({"id": account["id"] + "/deployments/" + group,
                                    "properties": {"provisioningState": "Succeeded", "model": {"name": group.lower(), "version": "1"}}})
        path = self.directory / "deployments.json"
        path.write_text(json.dumps(deployments))
        summary, fragment, _ = self.run_import(base_models=[], deployments=path)
        self.assertEqual(summary["errors"], [])
        self.assertEqual(next(item for item in fragment["application"]["models"] if item["modelGroup"] == "DeepSeek-V4-Flash")["baseModel"],
                         "azure/deepseek-v4-flash")
        path.write_text(json.dumps(deployments[1:]))
        self.assert_refused(limx_legacy(), "missing from the supplied deployment list", base_models=[], deployments=path)

    def test_missing_base_model_is_a_warning(self):
        summary, fragment, _ = self.run_import(base_models=[])
        self.assertEqual(summary["errors"], [])
        self.assertTrue(any("gpt-6-astra has deployments without baseModel" in item for item in summary["warnings"]))
        self.assertNotIn("baseModel", fragment["application"]["models"][0])

    def test_cli_exit_codes(self):
        legacy_path, accounts_path = self.files(limx_legacy(), limx_accounts())
        arguments = ["--legacy-config", str(legacy_path), "--accounts", str(accounts_path), "--output-dir", str(self.directory / "cli")]
        for value in BASE_MODELS:
            arguments += ["--base-model", value]
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(importer.main(arguments), 0)
        self.assertEqual(json.loads(stdout.getvalue())["importedModels"], 246)
        legacy_path.write_text(yaml.safe_dump(self.legacy_with({"api_key": SECRET})))
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(importer.main(arguments), 1)
        self.assertNotIn(SECRET, stdout.getvalue())
        legacy_path.write_text("model_list: []\n")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(importer.main(arguments), 2)


if __name__ == "__main__":
    unittest.main()
