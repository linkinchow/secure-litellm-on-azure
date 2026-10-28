import base64
import copy
import inspect
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from local_execution.runner import (
    IMAGE_STEPS,
    INFRASTRUCTURE_STEPS,
    RUNTIME_STEPS,
    STEPS,
    authentication_profile,
    authenticate_azure,
    check_configuration,
    enforce_local_policy,
    execute_infrastructure_plan,
    load_config,
    main,
    remove_sensitive_runtime_files,
    run_backup_restore,
    run_connectivity_check,
    run_infrastructure,
    run_runtime,
    run_target_connectivity_check,
    require_legacy_cluster_running,
)
from local_execution.merge_config import changed_paths, main as merge_config_main, merge_stage, operation_directory, read_json
from scripts.customer_migration import ROOT, parameters_for, validate_config
from scripts.migration_deploy import deploy_component
from tests.test_customer_migration import customer_config


class LocalExecutionTests(unittest.TestCase):
    def configuration(self):
        config = customer_config()
        config["localExecution"] = {
            "postgresRestoreImage": "postgres@sha256:" + "a" * 64,
            "executionHost": {
                "virtualNetworkId": f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/rg-execution/providers/Microsoft.Network/virtualNetworks/execution-vnet",
                "managePeering": True,
                "manageBlobDnsLink": True,
            },
        }
        config["parameters"].update({
            "backup": {
                "logAnalyticsWorkspaceName": "target-logs",
                "backupOwnerPrincipalId": "33333333-3333-4333-8333-333333333333",
                "virtualNetworkName": "target-vnet",
                "virtualNetworkAddressPrefix": "10.30.0.0/16",
                "privateEndpointSubnetName": "snet-private-endpoints",
                "privateEndpointSubnetPrefix": "10.30.8.0/24",
            },
            "legacy-logging": {"workspaceMode": "create"},
        })
        return config

    def prepared(self):
        source = self.configuration()
        settings = source.pop("localExecution")
        host = settings["executionHost"]
        source["parameters"]["runner-connectivity"] = {
            "runnerVirtualNetworkId": host["virtualNetworkId"],
            "managePeering": host["managePeering"],
            "manageBlobDnsLink": host["manageBlobDnsLink"],
        }
        return validate_config(source, "test"), settings

    def greenfield_configuration(self):
        config = self.configuration()
        config["deploymentMode"] = "greenfield"
        config.pop("legacy")
        config["parameters"]["network"] = {
            key: config["parameters"]["backup"][key]
            for key in ("virtualNetworkName", "virtualNetworkAddressPrefix", "privateEndpointSubnetName", "privateEndpointSubnetPrefix")
        }
        for component in ("backup", "legacy-logging", "monitoring"):
            config["parameters"].pop(component, None)
        config["localExecution"]["executionHost"]["manageBlobDnsLink"] = False
        return config

    def test_local_greenfield_uses_network_peering_and_blocks_legacy_steps(self):
        from scripts.runner_connectivity import connectivity_settings

        source = self.greenfield_configuration()
        source["localExecution"].pop("postgresRestoreImage")
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            path = Path(folder) / "customer.json"
            path.write_text(json.dumps(source))
            config, settings = load_config(path)
        self.assertEqual(check_configuration(config), {"status": "valid"})
        self.assertEqual(INFRASTRUCTURE_STEPS["network"], (0, "network"))
        self.assertIn("network", STEPS)
        self.assertEqual(config["parameters"]["runner-connectivity"]["manageBlobDnsLink"], False)
        self.assertTrue(connectivity_settings(config)["connectionName"].startswith("llmgw-target-"))
        parameters = parameters_for(config, 0, "runner-connectivity")[1]["parameters"]
        self.assertEqual(parameters["backupVirtualNetworkName"]["value"], config["parameters"]["network"]["virtualNetworkName"])
        enforce_local_policy(settings, "network", config)
        for step in ("backup", "connectivity-check", "backup-restore", "legacy-hardening", "stage5-restore-target"):
            with self.subTest(step=step), self.assertRaisesRegex(ValueError, "Greenfield"):
                enforce_local_policy(settings, step, config)

        invalid = self.greenfield_configuration()
        invalid["localExecution"].pop("postgresRestoreImage")
        invalid["localExecution"]["executionHost"]["manageBlobDnsLink"] = True
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            path = Path(folder) / "customer.json"
            path.write_text(json.dumps(invalid))
            with self.assertRaisesRegex(ValueError, "Blob DNS"):
                load_config(path)

        invalid = self.greenfield_configuration()
        invalid["localExecution"].pop("postgresRestoreImage")
        invalid["localExecution"]["runtimeInputs"] = {"backupBlob": "pre-change/" + "1" * 32 + ".dump"}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            path = Path(folder) / "customer.json"
            path.write_text(json.dumps(invalid))
            with self.assertRaisesRegex(ValueError, "migration-only runtime inputs"):
                load_config(path)

    def test_greenfield_stage5_merge_omits_migration_inputs(self):
        source = self.greenfield_configuration()
        source["localExecution"].pop("postgresRestoreImage")
        catalog = json.loads((ROOT / "local_execution/customer.stage2-9.fragments.example.json").read_text())
        values = {
            "REPLACE_SUPPORTED_PG_SKU": "Standard_D2s_v3",
            "REPLACE_PG_ADMIN_GROUP_OBJECT_ID": "66666666-6666-4666-8666-666666666666",
            "REPLACE_PG_ADMIN_GROUP_NAME": "database-bootstrap-group",
            "REPLACE_LOCAL_OPERATOR_UAMI_PRINCIPAL_ID": "44444444-4444-4444-8444-444444444444",
        }

        merged, missing = merge_stage(source, catalog, 5, values=values)

        self.assertEqual(missing, [])
        self.assertFalse({"backupBlob", "backupSha256", "postgresMigrationUser", "legacySaltSource"} & merged["localExecution"]["runtimeInputs"].keys())
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            path = Path(folder) / "customer.json"
            path.write_text(json.dumps(merged))
            load_config(path)
        with self.assertRaisesRegex(ValueError, "greenfield"):
            merge_stage(source, catalog, 5, options=("legacy-master-key-salt",), values=values)

    def test_greenfield_stage3_merge_derives_bootstrap_network(self):
        source = self.greenfield_configuration()
        source["localExecution"].pop("postgresRestoreImage")
        source["parameters"].pop("platform")
        source["parameters"]["bootstrap"] = {
            "workspaceMode": "create",
            "logRetentionDays": 30,
            "logAnalyticsWorkspaceName": "target-logs",
        }
        catalog = json.loads((ROOT / "local_execution/customer.stage2-9.fragments.example.json").read_text())
        values = {
            "REPLACE_GLOBALLY_UNIQUE_ACR_NAME": "syntheticregistry",
            "REPLACE_NEW_PRIVATE_AKS_NAME": "target-aks",
            "REPLACE_NEW_AKS_DNS_PREFIX": "target-aks",
            "REPLACE_SUPPORTED_K8S_VERSION": "1.30",
            "REPLACE_SUPPORTED_VM_SKU": "Standard_D4s_v4",
        }

        merged, missing = merge_stage(source, catalog, 3, values=values)

        self.assertEqual(missing, [])
        self.assertEqual(merged["parameters"]["platform"]["stage4Network"]["virtualNetworkName"], source["parameters"]["network"]["virtualNetworkName"])
        self.assertEqual(merged["parameters"]["platform"]["logAnalyticsWorkspaceName"], source["parameters"]["bootstrap"]["logAnalyticsWorkspaceName"])

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            path = Path(folder) / "customer.json"
            path.write_text(json.dumps(source))
            config, _settings = load_config(path)
        self.assertEqual(check_configuration(config), {"status": "valid"})
        bootstrap = parameters_for(config, 0, "bootstrap")[1]["parameters"]
        self.assertEqual(bootstrap["logAnalyticsWorkspaceName"]["value"], "target-logs")

    def test_standalone_configuration_is_translated_before_shared_validation(self):
        source = self.configuration()
        with self.assertRaisesRegex(ValueError, "Unexpected or missing"):
            validate_config(source, "test")
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            path = Path(folder) / "customer.json"
            path.write_text(json.dumps(source))
            config, settings = load_config(path)
        self.assertNotIn("localExecution", config)
        self.assertEqual(settings["postgresRestoreImage"], source["localExecution"]["postgresRestoreImage"])
        self.assertEqual(settings["executionHost"], source["localExecution"]["executionHost"])
        self.assertEqual(settings["authentication"], {"default": {"method": "existing"}})
        for profile in ("deploy", "runtime", "database", "certificate", "entraBootstrap", "entraAccess"):
            self.assertEqual(authentication_profile(settings, profile), {"method": "existing"})
        self.assertEqual(settings["features"], {"entraMode": "deferred", "allowTrafficRelease": False})
        self.assertEqual(config["parameters"]["runner-connectivity"]["runnerVirtualNetworkId"], settings["executionHost"]["virtualNetworkId"])

    def test_local_configuration_rejects_symlinks(self):
        source = self.configuration()
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            real = Path(folder) / "real.json"
            link = Path(folder) / "customer.json"
            real.write_text(json.dumps(source))
            link.symlink_to(real)
            with self.assertRaisesRegex(ValueError, "cannot be a symlink"):
                load_config(link)

    def test_local_settings_require_pinned_image_and_execution_host(self):
        for update, message in (
            ({"postgresRestoreImage": "postgres:16"}, "full image digest"),
            ({"executionHost": {}}, "unexpected or missing"),
            ({"unexpected": True}, "unexpected or missing"),
        ):
            source = self.configuration()
            source["localExecution"].update(update)
            if "unexpected" in update:
                source["localExecution"].pop("executionHost")
            with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
                path = Path(folder) / "customer.json"
                path.write_text(json.dumps(source))
                with self.subTest(update=update), self.assertRaisesRegex(ValueError, message):
                    load_config(path)

    def test_shared_deployment_controller_has_no_local_bypass(self):
        self.assertNotIn("enforce_evidence", inspect.signature(deploy_component).parameters)

    def test_infrastructure_uses_shared_plan_and_local_execution(self):
        config, _settings = self.prepared()
        plan = {"planSha256": "b" * 64, "revision": "c" * 40, "configSha256": "d" * 64}
        receipt = {"provisioningState": "Succeeded"}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.deploy_component", return_value=plan) as preview, patch("local_execution.runner.execute_infrastructure_plan", return_value=receipt) as execute:
            result = run_infrastructure(config, "c" * 40, 1, "monitoring", Path(folder))
        self.assertEqual(result, {"planSha256": plan["planSha256"], "receipt": receipt})
        self.assertEqual(preview.call_args.args[4], "plan")
        execute.assert_called_once()

    def test_direct_infrastructure_execution_uses_generated_template(self):
        config, _settings = self.prepared()
        plan = {"planSha256": "b" * 64, "revision": "c" * 40, "configSha256": "d" * 64}
        calls = []

        class FakeAzure:
            def run(self, arguments):
                calls.append(arguments)
                return {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}

            def scoped(self, arguments):
                calls.append(arguments)
                return {"id": "/synthetic/deployment", "properties": {"provisioningState": "Succeeded", "outputs": {}}}

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.AzureCommands", return_value=FakeAzure()):
            base = Path(folder)
            plan_directory = base / "plan"
            execute_directory = base / "execute"
            plan_directory.mkdir()
            (plan_directory / "template.json").write_text("{}")
            (plan_directory / "parameters.json").write_text("{}")
            receipt = execute_infrastructure_plan(config, 0, "bootstrap", plan, plan_directory, execute_directory)
        self.assertEqual(receipt["provisioningState"], "Succeeded")
        self.assertEqual(calls[1][:3], ["deployment", "sub", "create"])
        self.assertIn(str(plan_directory / "template.json"), calls[1])

    def test_target_connectivity_deployment_is_reverified(self):
        config, _settings = self.prepared()
        plan = {"planSha256": "b" * 64, "revision": "c" * 40, "configSha256": "d" * 64}
        context = {"apiHostname": "target.private", "privateEndpointIps": ["10.0.0.4"]}

        class FakeAzure:
            def run(self, _arguments):
                return {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}

            def scoped(self, _arguments):
                return {"id": "/synthetic/deployment", "properties": {"provisioningState": "Succeeded", "outputs": {}}}

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.AzureCommands", return_value=FakeAzure()), patch("scripts.runner_target_connectivity.inspect_target_connectivity", return_value=context) as inspect:
            base = Path(folder)
            plan_directory = base / "plan"
            execute_directory = base / "execute"
            plan_directory.mkdir()
            (plan_directory / "template.json").write_text("{}")
            (plan_directory / "parameters.json").write_text("{}")
            (plan_directory / "connectivity-review.json").write_text(json.dumps(context))
            execute_infrastructure_plan(config, 4, "runner-target-connectivity", plan, plan_directory, execute_directory)
        inspect.assert_called_once()
        self.assertTrue(inspect.call_args.kwargs["require_link"])

    def test_config_check_cli_reads_only_standalone_customer_file(self):
        source = self.configuration()
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.reviewed_revision", return_value="a" * 40), patch("sys.stdout", new_callable=io.StringIO) as output:
            base = Path(folder)
            config_path = base / "customer.json"
            config_path.write_text(json.dumps(source))
            with patch("sys.argv", ["local-execution", "--config", str(config_path), "--step", "config-check", "--output-root", str(base / "runs")]):
                main()
            summary = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(summary["result"], {"status": "valid"})

    def test_failed_step_records_status_and_removes_sensitive_files(self):
        source = self.configuration()
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.reviewed_revision", return_value="a" * 40):
            base = Path(folder)
            config_path = base / "customer.json"
            config_path.write_text(json.dumps(source))

            def fail(_config, _settings, _revision, _step, destination, _operation, _approved_plan):
                (destination / "database.dump").write_text("sensitive")
                raise RuntimeError("synthetic failure")

            with patch("local_execution.runner.execute_step", side_effect=fail), patch("sys.argv", ["local-execution", "--config", str(config_path), "--step", "config-check", "--output-root", str(base / "runs")]), self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                main()
            outputs = list((base / "runs").glob("*/local-execution.json"))
            status = json.loads(outputs[0].read_text())
        self.assertEqual(status["result"], {"status": "failed", "errorType": "RuntimeError"})
        self.assertFalse((outputs[0].parent / "database.dump").exists())

    def test_sensitive_cleanup_preserves_reports(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            directory = Path(folder)
            for name in ("kubeconfig", "aks-ca.crt", "database.dump", "downloaded.dump", "runtime-summary.json"):
                (directory / name).write_text("synthetic")
            remove_sensitive_runtime_files(directory)
            self.assertFalse(any((directory / name).exists() for name in ("kubeconfig", "aks-ca.crt", "database.dump", "downloaded.dump")))
            self.assertTrue((directory / "runtime-summary.json").exists())

    def test_connectivity_check_is_local_and_does_not_use_actions_readiness(self):
        config, _settings = self.prepared()
        target = {"blobHost": "backup.blob.core.windows.net", "privateEndpointIps": ["10.30.8.4"], "storageAccountName": "backup", "containerName": "litellm-postgresql"}

        def run(arguments, **_kwargs):
            if arguments[0] == "docker":
                output = "27.0.0"
            elif arguments[:3] == ["az", "account", "show"]:
                output = json.dumps({"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]})
            elif arguments[0] == "getent":
                output = "10.30.8.4 STREAM backup"
            elif arguments[0] == "curl":
                output = "10.30.8.4 403"
            elif arguments[:3] == ["az", "storage", "blob"]:
                output = "0"
            else:
                output = "deployment/postgres"
            return subprocess.CompletedProcess(arguments, 0, output, "")

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.shutil.which", return_value="installed"), patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.runner_connectivity.backup_target", return_value=target), patch("local_execution.runner.require_legacy_cluster_running", return_value={"powerState": "Running"}), patch("local_execution.runner.AzureCommands"):
            directory = Path(folder)
            result = run_connectivity_check(config, "a" * 40, directory, run=run)
            report = json.loads((directory / "local-readiness.json").read_text())
        self.assertEqual(result, {"status": "passed"})
        self.assertEqual(report["status"], "passed")
        self.assertIn("local-tools", {check["name"] for check in report["checks"]})

    def test_stopped_legacy_cluster_is_rejected_before_kubernetes_access(self):
        config, _settings = self.prepared()

        class FakeAzure:
            def scoped(self, _arguments):
                return {"provisioningState": "Succeeded", "powerState": "Stopped", "fqdn": "legacy.invalid", "privateFqdn": None}

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, self.assertRaisesRegex(ValueError, "not Running"):
            require_legacy_cluster_running(config, Path(folder), FakeAzure())

    def test_runtime_preview_hash_is_used_immediately(self):
        config, _settings = self.prepared()
        plan_sha = "d" * 64
        calls = []

        def monitoring(_config, stage, operation, revision, directory, approved):
            calls.append((stage, operation, revision, approved))
            directory.mkdir(parents=True, exist_ok=True)
            if operation == "plan":
                (directory / "runtime-summary.json").write_text(json.dumps({"planSha256": plan_sha}))

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("scripts.migration_runtime.monitoring_onboard", side_effect=monitoring):
            result = run_runtime(config, "e" * 40, 1, "monitoring-onboard", Path(folder))
        self.assertEqual(result, {"planSha256": plan_sha})
        self.assertEqual(calls[-1][-1], plan_sha)

    def test_later_runtime_actions_use_immediate_plan_hash(self):
        config, _settings = self.prepared()
        plan_sha = "9" * 64
        calls = []

        def invoke(_config, _revision, stage, action, operation, directory, approved, image_public_key=None):
            calls.append((stage, action, operation, approved, image_public_key))
            if operation == "plan":
                (directory / "runtime-summary.json").write_text(json.dumps({"planSha256": plan_sha}))

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.invoke_runtime_action", side_effect=invoke):
            result = run_runtime(config, "e" * 40, 5, "schema-migrate", Path(folder))
        self.assertEqual(result, {"planSha256": plan_sha})
        self.assertEqual(calls, [(5, "schema-migrate", "plan", "", None), (5, "schema-migrate", "execute", plan_sha, None)])

    def test_later_runtime_defaults_to_plan_and_requires_matching_approval(self):
        from local_execution.runner import local_operation

        config, _settings = self.prepared()
        plan_sha = "8" * 64
        calls = []

        def invoke(_config, _revision, stage, action, operation, directory, approved, image_public_key=None):
            calls.append((stage, action, operation, approved))
            if operation == "plan":
                (directory / "runtime-summary.json").write_text(json.dumps({"planSha256": plan_sha}))

        self.assertEqual(local_operation("stage5-schema-migrate", "auto"), "plan")
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.invoke_runtime_action", side_effect=invoke):
            planned = run_runtime(config, "e" * 40, 5, "schema-migrate", Path(folder), operation="plan")
        self.assertEqual(planned, {"planSha256": plan_sha, "executionPerformed": False})
        self.assertEqual(calls, [(5, "schema-migrate", "plan", "")])
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("local_execution.runner.invoke_runtime_action", side_effect=invoke), self.assertRaisesRegex(ValueError, "changed since approval"):
            run_runtime(config, "e" * 40, 5, "schema-migrate", Path(folder), operation="execute", approved_plan="7" * 64)

    def test_managed_identity_authentication_selects_client_and_scope(self):
        config, settings = self.prepared()
        client_id = "33333333-3333-4333-8333-333333333333"
        settings["authentication"] = {"runtime": {"method": "managed-identity", "clientId": client_id}}
        calls = []

        def run(arguments, **_kwargs):
            calls.append(arguments)
            if arguments[1:3] == ["account", "show"]:
                output = json.dumps({"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]})
            elif arguments[1:3] == ["account", "get-access-token"]:
                payload = json.dumps({"appid": client_id}).encode()
                output = "header." + __import__("base64").urlsafe_b64encode(payload).decode().rstrip("=") + ".signature"
            else:
                output = ""
            return subprocess.CompletedProcess(arguments, 0, output, "")

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            profile = authenticate_azure(config, settings, "runtime", Path(folder), run=run)
            summary = json.loads((Path(folder) / "azure-auth.json").read_text())
        self.assertEqual(profile["clientId"], client_id)
        self.assertEqual(calls[0][:6], ["az", "login", "--identity", "--client-id", client_id, "--output"])
        self.assertEqual(summary["method"], "managed-identity")
        self.assertTrue(summary["clientIdentityVerified"])

    def test_stage2_to_9_cannot_use_immediate_apply(self):
        from local_execution.runner import local_operation

        with self.assertRaisesRegex(ValueError, "limited to the validated Stage0-1"):
            local_operation("stage9-edge-release", "apply")
        self.assertEqual(local_operation("monitoring", "apply"), "apply")

    def test_entra_deferral_blocks_identity_and_traffic_not_preparation(self):
        config, settings = self.prepared()
        settings["features"] = {"entraMode": "deferred", "allowTrafficRelease": False}
        for step in ("stage7-entra-apps", "stage8-application", "stage9-edge-release", "stage9-dns-publish"):
            with self.subTest(step=step), self.assertRaisesRegex(ValueError, "Entra is deferred"):
                enforce_local_policy(settings, step)
        enforce_local_policy(settings, "stage9-edge-prepare")
        enforce_local_policy(settings, "stage7-promote-proxy-image")
        enforce_local_policy(settings, "stage9-dns-rollback")
        config["parameters"]["platform"]["azureOpenAIConnections"] = [{"alias": "primary", "accountName": "synthetic-model"}]
        config["parameters"]["platform"]["stage5Data"] = {"postgresqlDatabaseName": "litellm"}
        config["application"] = {"backendImage": "synthetic.azurecr.io/litellm@sha256:" + "a" * 64, "models": [{"modelGroup": "coding", "connectionAlias": "primary", "deploymentName": "model", "id": "primary-coding", "apiVersion": "v1"}], "authentication": {"mode": "native", "adminUsername": "gateway-admin"}}
        for step in ("stage7-promote-proxy-image", "stage7-entra-apps", "stage7-application"):
            with self.subTest(native_step=step), self.assertRaisesRegex(ValueError, "Native gateway authentication"):
                enforce_local_policy(settings, step, config)
        for step in ("stage8-application", "stage9-edge-bind", "stage9-edge-release", "stage9-dns-publish"):
            if step in {"stage9-edge-release", "stage9-dns-publish"}:
                settings["features"]["allowTrafficRelease"] = True
            enforce_local_policy(settings, step, config)
        settings["features"]["entraMode"] = "enabled"
        with self.assertRaisesRegex(ValueError, "requires localExecution.features.entraMode=deferred"):
            enforce_local_policy(settings, "stage8-application", config)

    def test_local_operation_sanitizes_ambient_manifest_and_ingress_overrides(self):
        from local_execution.runner import local_operation_environment

        config, settings = self.prepared()
        ambient = {
            "MIGRATION_MANIFEST_YAML": "unreviewed",
            "MIGRATION_LEGACY_SALT_SOURCE": "master-key",
            "PRIVATE_API_INGRESS_CLASS": "ambient-api",
            "PRIVATE_ADMIN_INGRESS_CLASS": "ambient-admin",
        }
        with patch.dict(os.environ, ambient, clear=False):
            with local_operation_environment(config, settings, "stage6-application", {"method": "existing"}):
                self.assertEqual(os.environ["MIGRATION_MANIFEST_YAML"], "")
                self.assertEqual(os.environ["MIGRATION_LEGACY_SALT_SOURCE"], "")
                self.assertEqual(os.environ["PRIVATE_API_INGRESS_CLASS"], "")
                self.assertEqual(os.environ["PRIVATE_ADMIN_INGRESS_CLASS"], "")
            self.assertEqual({name: os.environ[name] for name in ambient}, ambient)

    def test_backend_secret_salt_source_comes_from_local_config(self):
        from local_execution.runner import local_operation_environment

        config, settings = self.prepared()
        with local_operation_environment(config, settings, "stage5-backend-secrets", {"method": "existing"}):
            self.assertEqual(os.environ["MIGRATION_LEGACY_SALT_SOURCE"], "secret")
        settings.setdefault("runtimeInputs", {})["legacySaltSource"] = "master-key"
        with patch.dict(os.environ, {"MIGRATION_LEGACY_SALT_SOURCE": "secret"}, clear=False):
            with local_operation_environment(config, settings, "stage5-backend-secrets", {"method": "existing"}):
                self.assertEqual(os.environ["MIGRATION_LEGACY_SALT_SOURCE"], "master-key")
            self.assertEqual(os.environ["MIGRATION_LEGACY_SALT_SOURCE"], "secret")

    def test_image_promotion_requires_explicit_execute(self):
        from local_execution.runner import local_operation

        with self.assertRaisesRegex(ValueError, "requires explicit --operation execute"):
            local_operation("stage4-promote-backend-image", "auto")
        self.assertEqual(local_operation("stage4-promote-backend-image", "execute"), "execute")

    def test_target_connectivity_check_binds_dns_tls_and_cluster_read(self):
        config, _settings = self.prepared()
        target = {
            "apiHostname": "target.privatelink.westus.azmk8s.io",
            "privateEndpointIps": ["10.30.1.4"],
            "acrLoginServer": "customerregistry.azurecr.io",
            "acrPrivateEndpointIps": ["10.30.8.4"],
        }

        calls = []

        def run(arguments, **_kwargs):
            calls.append(arguments)
            if arguments[0] == "getent":
                address = "10.30.1.4" if arguments[-1] == target["apiHostname"] else "10.30.8.4"
                output = address + " STREAM " + arguments[-1]
            elif arguments[0] == "curl":
                address = "10.30.1.4" if target["apiHostname"] in arguments[-1] else "10.30.8.4"
                output = address + " 401"
            else:
                output = "deployment/litellm"
            return subprocess.CompletedProcess(arguments, 0, output, "")

        def connect_cluster(_config, runtime_directory, legacy):
            self.assertFalse(legacy)
            certificate = "-----BEGIN CERTIFICATE-----\nSYNTHETIC\n-----END CERTIFICATE-----\n"
            kubeconfig = {
                "current-context": "target",
                "contexts": [{"name": "target", "context": {"cluster": "target-cluster"}}],
                "clusters": [{"name": "target-cluster", "cluster": {"certificate-authority-data": base64.b64encode(certificate.encode()).decode()}}],
            }
            path = runtime_directory / "kubeconfig"
            path.write_text(yaml.safe_dump(kubeconfig))
            return ["kubectl", "--kubeconfig", str(path), "--namespace", "litellm"]

        config, settings = self.prepared()
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch("scripts.runner_target_connectivity.inspect_target_connectivity", return_value=target), patch("scripts.migration_runtime.connect_cluster", side_effect=connect_cluster), patch("local_execution.runner.AzureCommands"), patch("local_execution.runner.authenticate_azure") as authenticate:
            directory = Path(folder)
            result = run_target_connectivity_check(config, settings, "a" * 40, directory, run=run)
            report = json.loads((directory / "target-readiness.json").read_text())
            curl_calls = [arguments for arguments in calls if arguments[0] == "curl"]
            self.assertIn("--cacert", curl_calls[0])
            self.assertNotIn("--cacert", curl_calls[1])
            self.assertTrue((directory / "runtime/aks-ca.crt").is_file())
        self.assertEqual(result, {"status": "passed"})
        self.assertEqual([item["name"] for item in report["checks"]], ["target-aks-private", "target-acr-private", "target-cluster-read"])
        self.assertEqual([call.args[2] for call in authenticate.call_args_list], ["deploy", "runtime"])

    def test_local_image_promotion_signs_and_removes_registry_token(self):
        from local_execution.image_supply_chain import promote_image

        config, settings = self.prepared()
        config["parameters"]["platform"] = {"containerRegistryName": "customerregistry"}
        calls = []
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            directory = Path(folder)
            private_key = directory / "cosign.key"
            public_key = directory / "cosign.pub"
            private_key.write_text("private")
            private_key.chmod(0o600)
            public_key.write_text("public")
            settings["imageSigning"] = {
                "privateKeyPath": str(private_key),
                "publicKeyPath": str(public_key),
                "backendTargetTag": "litellm-azure:test",
            }

            def run(arguments, **kwargs):
                calls.append((arguments, kwargs.get("env")))
                output = ""
                if arguments[:3] == ["az", "acr", "login"]:
                    output = json.dumps({"loginServer": "customerregistry.azurecr.io", "accessToken": "private-token"})
                elif arguments[:4] == ["az", "acr", "manifest", "list-metadata"]:
                    output = "sha256:" + "a" * 64 + "\n"
                elif arguments[0] == "syft":
                    Path(arguments[arguments.index("--output") + 1].split("=", 1)[1]).write_text(json.dumps({"spdxVersion": "SPDX-2.3"}))
                elif arguments[0] == "trivy":
                    Path(arguments[arguments.index("--output") + 1]).write_text(json.dumps({"Results": []}))
                elif arguments[:2] == ["cosign", "verify"]:
                    output = json.dumps([{"critical": {"identity": {"docker-reference": "customerregistry.azurecr.io/litellm-azure"}}}])
                return subprocess.CompletedProcess(arguments, 0, output, "")

            with patch.dict(os.environ, {"COSIGN_PASSWORD": "not-recorded"}), patch("local_execution.image_supply_chain.build_runtime_revision", return_value="c" * 64) as runtime:
                summary = promote_image(config, settings, "f" * 40, "backend", directory, run=run)
            self.assertTrue(summary["signatureVerified"])
            self.assertEqual(summary["runtimeRevision"], "c" * 64)
            self.assertEqual([call.args for call in runtime.call_args_list], [("azure", "f" * 40)] * 2)
            signing = next(arguments for arguments, _ in calls if arguments[:2] == ["cosign", "sign"])
            self.assertIn("llmgw.revision=" + "f" * 40, signing)
            self.assertIn("llmgw.runtimeRevision=" + "c" * 64, signing)
            self.assertFalse((directory / "docker-config/config.json").exists())
            self.assertNotIn("private-token", str(calls))
            self.assertNotIn("not-recorded", str(calls[:-2]))
            self.assertEqual(calls[-1][1]["COSIGN_PASSWORD"], "not-recorded")
            with patch.dict(os.environ, {"COSIGN_PASSWORD": "not-recorded"}), patch("local_execution.image_supply_chain.build_runtime_revision", side_effect=["c" * 64, "e" * 64]), self.assertRaisesRegex(ValueError, "changed during promotion"):
                promote_image(config, settings, "f" * 40, "backend", directory, run=run)

    def test_backup_restore_reads_image_from_local_settings(self):
        config, settings = self.prepared()
        observed = []

        def restore(_config, revision, _directory):
            observed.append((revision, os.environ.get("POSTGRES_RESTORE_IMAGE")))

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch.dict(os.environ, {"POSTGRES_RESTORE_IMAGE": "previous"}), patch("local_execution.runner.require_legacy_cluster_running"), patch("scripts.migration_runtime.backup_restore", side_effect=restore):
            result = run_backup_restore(config, settings, "f" * 40, Path(folder))
            self.assertEqual(os.environ["POSTGRES_RESTORE_IMAGE"], "previous")
        self.assertEqual(result, {"status": "completed"})
        self.assertEqual(observed, [("f" * 40, settings["postgresRestoreImage"])])

    def test_guide_and_example_match_steps(self):
        guide = (ROOT / "local_execution/README_ZH.md").read_text()
        later_guide = (ROOT / "local_execution/stage2-9-guide-zh.md").read_text()
        all_guides = guide + later_guide
        requirements = (ROOT / "local_execution/requirements.txt").read_text()
        feishu = (ROOT / "local_execution/feishu-alert-notification-zh.md").read_text()
        example = json.loads((ROOT / "local_execution/customer.example.json").read_text())
        staged = json.loads((ROOT / "local_execution/customer.stage2-9.fragments.example.json").read_text())
        self.assertEqual(set(example["localExecution"]), {"postgresRestoreImage", "executionHost", "authentication", "features", "runtimeInputs"})
        self.assertEqual(example["localExecution"]["authentication"], {"default": {"method": "existing"}})
        self.assertEqual(example["localExecution"]["features"], {"entraMode": "deferred", "allowTrafficRelease": False})
        self.assertEqual(staged["catalogVersion"], 1)
        self.assertEqual(set(staged["stages"]), set("23456789"))
        self.assertIn("customer.stage2-9.fragments.example.json", guide)
        self.assertIn("customer.stage2-9.fragments.example.json", later_guide)
        self.assertIn("merge_config.py", guide)
        self.assertIn("python -m local_execution.merge_config", later_guide)
        for stage in range(2, 10):
            self.assertIn(f"--stage {stage} --operation plan", later_guide)
            self.assertIn(f"--stage {stage} --operation apply", later_guide)
        self.assertEqual(set(staged["stages"]["2"]["customerConfig"]), {"governance", "contentAudit"})
        self.assertIn("platform", staged["stages"]["3"]["customerConfig"]["parameters"])
        stage4 = staged["stages"]["4"]
        self.assertEqual(set(stage4["customerConfig"]["parameters"]), {"platform", "certificate-vault", "runner-target-connectivity"})
        self.assertIn("privateIngress", stage4["customerConfig"])
        self.assertIn("imageSigning", stage4["localExecutionMerge"])
        stage5 = staged["stages"]["5"]
        self.assertIn("stage5Data", stage5["customerConfig"]["parameters"]["platform"])
        self.assertIn("databaseAccess", stage5["customerConfig"])
        self.assertEqual(set(stage5["localExecutionMerge"]["runtimeInputs"]), {"backupBlob", "backupSha256", "postgresMigrationUser", "legacySaltSource"})
        self.assertEqual(stage5["localExecutionMerge"]["runtimeInputs"]["legacySaltSource"], "secret")
        self.assertIn("legacyMasterKeySaltCompatibility", stage5)
        self.assertIn("separateDatabaseAdminIdentity", stage5)
        self.assertIn("application", staged["stages"]["6"]["customerConfig"])
        self.assertEqual(staged["stages"]["6"]["customerConfig"]["application"]["models"][0]["baseModel"], "azure/REPLACE_EXISTING_MODEL_BASE_MODEL")
        self.assertEqual(staged["stages"]["6"]["nativeAuthentication"]["customerConfig"]["application"]["authentication"]["mode"], "native")
        self.assertEqual(set(staged["stages"]["7"]["customerConfig"]), {"entra", "proxy"})
        self.assertEqual(staged["stages"]["7"]["localExecutionMerge"]["features"]["entraMode"], "enabled")
        self.assertEqual(set(staged["stages"]["7"]["localExecutionMerge"]["authentication"]), {"entraBootstrap", "entraAccess"})
        self.assertIn("nativeAuditOptionalObservability", staged["stages"]["8"])
        self.assertIn("enhancedL3Alternative", staged["stages"]["8"])
        self.assertEqual(staged["stages"]["8"]["enhancedL3Alternative"]["removeCustomerConfigKeysBeforeMerge"], ["contentAudit"])
        self.assertEqual(set(staged["stages"]["9"]["customerConfig"]["parameters"]), {"origin", "edge"})
        self.assertEqual(staged["stages"]["9"]["localExecutionForPrepare"]["features"], {"allowTrafficRelease": False})
        self.assertTrue(staged["stages"]["9"]["localExecutionForApprovedRelease"]["features"]["allowTrafficRelease"])
        for dependency in ("-r ../requirements.txt", "acme==5.8.0", "dnspython==2.8.0", "josepy==2.2.0"):
            self.assertIn(dependency, requirements)
        self.assertIn("local_execution/requirements.txt", guide)
        for value in ("BACKEND_IMAGE_SUMMARY", "BACKEND_IMAGE_PREFIX", "BACKEND_DIGEST", ".signatureVerified == true", "CURRENT_REVISION=\"$(git rev-parse HEAD)\"", "${BACKEND_IMAGE##*@sha256:}", "REPLACE_BUILT_64_HEX_DIGEST"):
            self.assertIn(value, later_guide)
        for value in ("--option native-auth", "仅新增`litellm-ui-password=generate`", "Master Key继续不向任何人工用户", "--resolve \"llm-api.${BASE_DOMAIN}:443:${API_PRIVATE_IP}\"", "native_virtual_key_acl", "私有Admin LB仅作Front Door回源"):
            self.assertIn(value, later_guide)
        self.assertNotIn("runner-connectivity", example["parameters"])
        self.assertIn("在线Runner VM绝不能挂载高权限UAMI", guide)
        self.assertIn("可以复用客户现有的deploy/runtime UAMI", guide)
        self.assertIn("不要使用数据库管理员UAMI", guide)
        self.assertIn("Client ID用于VM内`az login --identity`", guide)
        for value in ("llmgw-manual-stage01-test", "S0-L02之前", "S0-L03之前", "17d1049b-9a84-46fb-8f53-869881c3d3ab", "b7e6dc6d-f1e8-4753-8033-0f276bb0955b", "ba92f5b4-2d11-453d-a403-e96b0029c9fe", "不是创建UAMI时预先手工分配的角色", "不属于Stage0–1手工执行UAMI的最低要求"):
            self.assertIn(value, guide)
        for value in ("旧AKS必须处于`Running`", "power=Stopped", "az aks start", "getent ahostsv4 \"$AKS_FQDN\"", "不是Runner DNS配置或数据库错误"):
            self.assertIn(value, guide)
        for value in ("database system is shutting down", "容器PID 1已经切换为`postgres`", "restore-container-state.json", "restore-container.log", "本次失败不会生成正式`backupBlob`"):
            self.assertIn(value, guide)
        self.assertIn("feishu-alert-notification-zh.md", guide)
        for value in ("ag-litellm-stage1-owner", "Common alert schema", "Send_to_Feishu", "Secure Inputs", "FeishuRejected", "code=0", "19024", "重跑monitoring后飞书Action消失"):
            self.assertIn(value, feishu)
        for value in ("没有`legacy-monitoring`字段", "workspaceMode", "只能是`create`或`existing`", "REPLACE_EXISTING_LEGACY_WORKSPACE_NAME", "REPLACE_NEW_LEGACY_WORKSPACE_NAME", "不能用`create`试探资源是否存在", "az monitor log-analytics workspace list"):
            self.assertIn(value, guide)
        for step in STEPS:
            self.assertIn(f"--step {step}", all_guides)
        config_check = guide.index("--step config-check")
        bootstrap = guide.index("--step bootstrap")
        network = guide.index("--step network")
        connectivity_positions = [match.start() for match in re.finditer(r"--step execution-host-connectivity", guide)]
        backup = guide.index("--step backup")
        connectivity_check = guide.index("--step connectivity-check")
        backup_restore = guide.index("--step backup-restore")
        self.assertTrue(config_check < bootstrap < network < connectivity_positions[0])
        migration_connectivity = next(position for position in connectivity_positions if position > backup)
        migration_ordered = [config_check, bootstrap, backup, migration_connectivity, connectivity_check, backup_restore]
        migration_ordered.extend(guide.index(f"--step {step}") for step in ("legacy-logging", "monitoring-onboard", "monitoring", "legacy-hardening", "legacy-access-restrict"))
        self.assertEqual(migration_ordered, sorted(migration_ordered))

    def test_stage2_to_9_fragments_merge_into_validator_compatible_config(self):
        catalog = json.loads((ROOT / "local_execution/customer.stage2-9.fragments.example.json").read_text())["stages"]
        source = self.configuration()
        runner_vnet = source["localExecution"]["executionHost"]["virtualNetworkId"]
        replacements = {
            "REPLACE_APPROVED_OPERATOR_USER_OBJECT_ID": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "REPLACE_APPROVED_GITHUB_LOGIN": "approved-user",
            "REPLACE_LOCAL_OPERATOR_UAMI_CLIENT_ID": "10000000-0000-4000-8000-000000000001",
            "REPLACE_ENTRA_BOOTSTRAP_UAMI_CLIENT_ID": "10000000-0000-4000-8000-000000000005",
            "REPLACE_ENTRA_ACCESS_UAMI_CLIENT_ID": "10000000-0000-4000-8000-000000000006",
            "REPLACE_GLOBALLY_UNIQUE_ACR_NAME": "syntheticregistry",
            "REPLACE_TARGET_LOG_WORKSPACE": "target-logs",
            "REPLACE_TARGET_VNET": "target-vnet",
            "REPLACE_NEW_PRIVATE_AKS_NAME": "target-aks",
            "REPLACE_NEW_AKS_DNS_PREFIX": "target-aks",
            "REPLACE_SUPPORTED_K8S_VERSION": "1.30",
            "REPLACE_SUPPORTED_VM_SKU": "Standard_D4s_v5",
            "REPLACE_MODEL_SUBSCRIPTION_ID": source["azure"]["subscriptionId"],
            "REPLACE_MODEL_RESOURCE_GROUP": "rg-model",
            "REPLACE_MODEL_ACCOUNT_NAME": "synthetic-model",
            "REPLACE_LOCAL_OPERATOR_UAMI_PRINCIPAL_ID": "44444444-4444-4444-8444-444444444444",
            "REPLACE_APPROVED_CERTIFICATE_IMPORTER_OBJECT_ID": "55555555-5555-4555-8555-555555555555",
            "REPLACE_RUNNER_VNET_RESOURCE_ID": runner_vnet,
            "REPLACE_GLOBALLY_UNIQUE_CERTIFICATE_VAULT_NAME": "synthetic-cert-vault",
            "REPLACE_CERTIFICATE_VAULT_NAME": "synthetic-cert-vault",
            "REPLACE_SUBSCRIPTION_ID": source["azure"]["subscriptionId"],
            "REPLACE_DNS_RESOURCE_GROUP": "rg-dns",
            "REPLACE_CUSTOMER_BASE_DOMAIN": source["baseDomain"],
            "REPLACE_SUPPORTED_PG_SKU": "Standard_D2s_v3",
            "REPLACE_PG_ADMIN_GROUP_OBJECT_ID": "66666666-6666-4666-8666-666666666666",
            "REPLACE_PG_ADMIN_GROUP_NAME": "database-bootstrap-group",
            "REPLACE_DATABASE_ADMIN_UAMI_CLIENT_ID": "10000000-0000-4000-8000-000000000007",
            "REPLACE_DATABASE_ADMIN_UAMI_PRINCIPAL_ID": "67676767-6767-4676-8676-676767676767",
            "REPLACE_DATABASE_ADMIN_UAMI_NAME": "database-bootstrap-identity",
            "REPLACE_32_HEX": "1" * 32,
            "REPLACE_64_HEX_SHA256": "2" * 64,
            "REPLACE_BUILT_64_HEX_DIGEST": "3" * 64,
            "REPLACE_EXISTING_MODEL_DEPLOYMENT_NAME": "gpt-deployment",
            "REPLACE_EXISTING_MODEL_BASE_MODEL": "gpt-6-luna",
            "REPLACE_ENTRA_BOOTSTRAP_SERVICE_PRINCIPAL_OBJECT_ID": "77777777-7777-4777-8777-777777777777",
            "REPLACE_ENTRA_ACCESS_SERVICE_PRINCIPAL_OBJECT_ID": "88888888-8888-4888-8888-888888888888",
            "REPLACE_CALLING_APPLICATION_CLIENT_ID": "99999999-9999-4999-8999-999999999999",
            "REPLACE_API_USER_OBJECT_ID": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "REPLACE_ADMIN_USER_OBJECT_ID": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
            "REPLACE_AUDIT_READER_USER_OBJECT_ID": "cccccccc-dddd-4eee-8fff-000000000001",
            "REPLACE_APPROVED_AUDIT_TEAM_ID": "approved-team",
            "REPLACE_AUDIT_CMK_VAULT": "synthetic-audit-vault",
            "REPLACE_AUDIT_CMK_KEY": "audit-key",
            "REPLACE_ADMIN_ALLOWED_PUBLIC_CIDRS": ["20.30.40.50/32"],
            "REPLACE_FRONT_DOOR_PRIVATE_LINK_REGION": "westus3",
            "REPLACE_AZURE_REGION": source["location"],
        }

        def replace(value):
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, str):
                for old, new in replacements.items():
                    if value == old and isinstance(new, list):
                        return copy.deepcopy(new)
                    if old in value:
                        value = value.replace(old, new)
                self.assertNotIn("REPLACE_", value)
            return value

        def merge(target, addition):
            for key, value in addition.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value)
                else:
                    target[key] = copy.deepcopy(value)

        def validated(document):
            with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
                path = Path(folder) / "customer.json"
                path.write_text(json.dumps(document))
                return load_config(path)[0]

        merge(source, replace(catalog["2"]["customerConfig"]))
        merge(source["localExecution"], replace(catalog["2"]["singleValidationIdentityAlternative"]))
        validated(source)

        merge(source, replace(catalog["3"]["customerConfig"]))
        config = validated(source)
        parameters_for(config, 3, "platform")

        merge(source, replace(catalog["4"]["customerConfig"]))
        merge(source["localExecution"], replace(catalog["4"]["localExecutionMerge"]))
        config = validated(source)
        for component in ("platform", "certificate-vault", "runner-target-connectivity"):
            parameters_for(config, 4, component)
        automatic_certificate = copy.deepcopy(source)
        merge(automatic_certificate, replace(catalog["4"]["optionalAutomaticApiCertificate"]["customerConfig"]))
        validated(automatic_certificate)

        stage4_source = copy.deepcopy(source)
        merge(source, replace(catalog["5"]["customerConfig"]))
        merge(source["localExecution"], replace(catalog["5"]["localExecutionMerge"]))
        config = validated(source)
        parameters_for(config, 5, "platform")

        repeated_group, missing = merge_stage(source, {"catalogVersion": 1, "stages": catalog}, 5)
        self.assertEqual(missing, [])
        self.assertEqual(changed_paths(source, repeated_group), [])

        database_identity = stage4_source
        merge(database_identity, replace(catalog["5"]["customerConfig"]))
        merge(database_identity["localExecution"], replace(catalog["5"]["localExecutionMerge"]))
        merge(database_identity, replace(catalog["5"]["separateDatabaseAdminIdentity"]["customerConfig"]))
        merge(database_identity["localExecution"], replace(catalog["5"]["separateDatabaseAdminIdentity"]["localExecutionMerge"]))
        database_identity_config = validated(database_identity)
        parameters_for(database_identity_config, 5, "platform")
        self.assertEqual(database_identity_config["parameters"]["platform"]["stage5Data"]["postgresqlEntraAdministratorPrincipalType"], "ServicePrincipal")
        self.assertEqual(database_identity["localExecution"]["authentication"]["database"]["method"], "managed-identity")

        master_key_salt, missing = merge_stage(stage4_source, {"catalogVersion": 1, "stages": catalog}, 5, ("database-admin-identity", "legacy-master-key-salt"), replacements)
        self.assertEqual(missing, [])
        self.assertEqual(master_key_salt["localExecution"]["runtimeInputs"]["legacySaltSource"], "master-key")

        repeated, missing = merge_stage(database_identity, {"catalogVersion": 1, "stages": catalog}, 5, ("database-admin-identity", "legacy-master-key-salt"))
        self.assertEqual(missing, [])
        self.assertEqual(changed_paths(database_identity, repeated), ["localExecution.runtimeInputs.legacySaltSource"])
        self.assertEqual(repeated["databaseAccess"], database_identity["databaseAccess"])
        self.assertEqual(repeated["parameters"]["platform"]["stage5Data"], database_identity["parameters"]["platform"]["stage5Data"])
        self.assertEqual(repeated["localExecution"]["authentication"]["database"], database_identity["localExecution"]["authentication"]["database"])
        self.assertEqual(repeated["localExecution"]["runtimeInputs"]["backupBlob"], database_identity["localExecution"]["runtimeInputs"]["backupBlob"])
        self.assertEqual(repeated["localExecution"]["runtimeInputs"]["backupSha256"], database_identity["localExecution"]["runtimeInputs"]["backupSha256"])

        merge(source, replace(catalog["6"]["customerConfig"]))
        validated(source)

        merge(source, replace(catalog["7"]["customerConfig"]))
        merge(source["localExecution"], replace(catalog["7"]["localExecutionMerge"]))
        config = validated(source)
        parameters_for(config, 7, "proxy-foundation")

        stage7_source = copy.deepcopy(source)
        merge(source, replace(catalog["8"]["nativeAuditOptionalObservability"]["customerConfig"]))
        config = validated(source)
        parameters_for(config, 8, "observability")

        merge(source, replace(catalog["9"]["customerConfig"]))
        merge(source, replace(catalog["9"]["optionalAzureDns"]["customerConfig"]))
        merge(source["localExecution"], replace(catalog["9"]["localExecutionForApprovedRelease"]))
        config = validated(source)
        parameters_for(config, 9, "origin")
        parameters_for(config, 9, "edge")

        stage7_source.pop("contentAudit")
        merge(stage7_source, replace(catalog["8"]["enhancedL3Alternative"]["customerConfig"]))
        config = validated(stage7_source)
        parameters_for(config, 8, "audit-foundation")
        parameters_for(config, 8, "audit")

    def test_stage2_to_9_runbook_has_complete_execution_and_identity_steps(self):
        guide = (ROOT / "local_execution/stage2-9-guide-zh.md").read_text()
        paired = {
            step for step in (*INFRASTRUCTURE_STEPS, *RUNTIME_STEPS)
            if step.startswith("stage")
        } | {"stage9-edge-release"}
        for step in paired:
            with self.subTest(step=step):
                self.assertIn(f"--step {step} --operation plan", guide)
                self.assertIn(f"--step {step} --operation execute", guide)
        for step in IMAGE_STEPS:
            with self.subTest(image_step=step):
                self.assertIn(f"--step {step} --operation execute", guide)
        for value in (
            "az identity create",
            "az vm identity assign",
            "az vm identity remove",
            "Application.ReadWrite.OwnedBy",
            "AppRoleAssignment.ReadWrite.All",
            "LLMGW Model Deployment Operator",
            "LLMGW Resource Lock Writer",
            "LLMGW Runtime Receipt Writer",
            "LLMGW AKS Namespace Bootstrapper",
            "az aks stop",
            "az aks start",
        ):
            self.assertIn(value, guide)

    def test_config_merge_recurses_preserves_existing_platform_and_reports_placeholders(self):
        source = self.configuration()
        before = copy.deepcopy(source)
        catalog = {
            "catalogVersion": 1,
            "stages": {
                "3": {
                    "customerConfig": {
                        "parameters": {
                            "platform": {
                                "containerRegistryName": "REPLACE_ACR_NAME",
                                "stage4Network": {"privateEndpointSubnetName": "snet-private-endpoints"},
                            }
                        }
                    }
                }
            },
        }
        merged, missing = merge_stage(source, catalog, 3)
        self.assertEqual(source, before)
        self.assertEqual(missing, ["REPLACE_ACR_NAME"])
        self.assertEqual(merged["parameters"]["platform"]["stage4Aks"], source["parameters"]["platform"]["stage4Aks"])
        self.assertEqual(merged["parameters"]["platform"]["stage4Network"]["virtualNetworkName"], source["parameters"]["platform"]["stage4Network"]["virtualNetworkName"])
        merged, missing = merge_stage(source, catalog, 3, values={"REPLACE_ACR_NAME": "mergedregistry"})
        self.assertFalse(missing)
        self.assertEqual(merged["parameters"]["platform"]["containerRegistryName"], "mergedregistry")
        array_catalog = {"catalogVersion": 1, "stages": {"9": {"customerConfig": {"parameters": {"edge": {"adminAllowedCidrs": "REPLACE_ADMIN_ALLOWED_PUBLIC_CIDRS"}}}, "localExecutionForPrepare": {"features": {"allowTrafficRelease": False}}}}}
        merged, missing = merge_stage(source, array_catalog, 9, values={"REPLACE_ADMIN_ALLOWED_PUBLIC_CIDRS": ["20.30.40.50/32", "21.31.41.51/32"]})
        self.assertFalse(missing)
        self.assertEqual(merged["parameters"]["edge"]["adminAllowedCidrs"], ["20.30.40.50/32", "21.31.41.51/32"])

    def test_config_merge_options_are_explicit_and_enhanced_l3_removes_native_audit(self):
        source = self.configuration()
        source["contentAudit"] = {"mode": "native", "retentionDays": 7, "contentPolicyAccepted": True}
        source["localExecution"]["features"] = {"entraMode": "deferred", "allowTrafficRelease": False}
        catalog = {
            "catalogVersion": 1,
            "stages": {
                "5": {
                    "customerConfig": {},
                    "separateDatabaseAdminIdentity": {
                        "localExecutionMerge": {"authentication": {"database": {"method": "managed-identity", "clientId": "10000000-0000-4000-8000-000000000007"}}},
                    },
                },
                "6": {
                    "customerConfig": {},
                    "nativeAuthentication": {"customerConfig": {"application": {"authentication": {"mode": "native", "adminUsername": "REPLACE_NATIVE_ADMIN_USERNAME"}}}},
                },
                "8": {
                    "enhancedL3Alternative": {
                        "removeCustomerConfigKeysBeforeMerge": ["contentAudit"],
                        "customerConfig": {"auditRuntime": {"retentionDays": 7, "captureEnabled": True, "retentionEnabled": True, "deliveryPolicyAccepted": True}},
                    },
                    "nativeAuditOptionalObservability": {"customerConfig": {"observability": {"collectorImage": "synthetic"}}},
                },
                "9": {
                    "customerConfig": {},
                    "localExecutionForPrepare": {"features": {"allowTrafficRelease": False}},
                    "localExecutionForApprovedRelease": {"features": {"allowTrafficRelease": True}, "releaseReportPath": "temp/release.json"},
                },
            },
        }
        merged, missing = merge_stage(source, catalog, 8, options=("enhanced-l3",))
        self.assertFalse(missing)
        self.assertNotIn("contentAudit", merged)
        self.assertIn("auditRuntime", merged)
        database_identity, missing = merge_stage(source, catalog, 5, options=("database-admin-identity",))
        self.assertFalse(missing)
        self.assertEqual(database_identity["localExecution"]["authentication"]["database"]["clientId"], "10000000-0000-4000-8000-000000000007")
        native, missing = merge_stage(source, catalog, 6, options=("native-auth",), values={"REPLACE_NATIVE_ADMIN_USERNAME": "gateway-admin"})
        self.assertFalse(missing)
        self.assertEqual(native["application"]["authentication"], {"mode": "native", "adminUsername": "gateway-admin"})
        with self.assertRaisesRegex(ValueError, "not both"):
            merge_stage(source, catalog, 8, options=("enhanced-l3", "observability"))
        prepared, _ = merge_stage(source, catalog, 9)
        enabled_source = copy.deepcopy(source)
        enabled_source["localExecution"]["features"]["entraMode"] = "enabled"
        enabled_prepared, _ = merge_stage(enabled_source, catalog, 9)
        released, _ = merge_stage(enabled_source, catalog, 9, options=("approved-release",))
        self.assertEqual(prepared["localExecution"]["features"]["entraMode"], "deferred")
        self.assertFalse(prepared["localExecution"]["features"]["allowTrafficRelease"])
        self.assertEqual(enabled_prepared["localExecution"]["features"]["entraMode"], "enabled")
        self.assertFalse(enabled_prepared["localExecution"]["features"]["allowTrafficRelease"])
        self.assertEqual(released["localExecution"]["features"]["entraMode"], "enabled")
        self.assertTrue(released["localExecution"]["features"]["allowTrafficRelease"])

    def test_config_merge_plan_does_not_write_and_apply_backs_up_atomically(self):
        source = self.configuration()
        catalog = {
            "catalogVersion": 1,
            "stages": {
                "2": {
                    "customerConfig": {"contentAudit": {"mode": "native", "retentionDays": 7, "contentPolicyAccepted": True}},
                    "localExecutionMerge": {"features": {"entraMode": "deferred", "allowTrafficRelease": False}},
                }
            },
        }
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            directory = Path(folder)
            config_path = directory / "customer.json"
            catalog_path = directory / "catalog.json"
            output_root = directory / "runs"
            config_path.write_text(json.dumps(source))
            catalog_path.write_text(json.dumps(catalog))
            with patch("sys.argv", ["merge-config", "--config", str(config_path), "--catalog", str(catalog_path), "--stage", "2", "--operation", "plan", "--output-root", str(output_root)]), patch("sys.stdout", new_callable=io.StringIO) as output:
                merge_config_main()
                planned = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(json.loads(config_path.read_text()), source)
            self.assertFalse(planned["applied"])
            self.assertTrue((ROOT / planned["preview"]).is_file())
            with patch("sys.argv", ["merge-config", "--config", str(config_path), "--catalog", str(catalog_path), "--stage", "2", "--operation", "apply", "--output-root", str(output_root)]), patch("sys.stdout", new_callable=io.StringIO) as output:
                merge_config_main()
                applied = json.loads(output.getvalue().splitlines()[-1])
            document = json.loads(config_path.read_text())
            self.assertTrue(applied["applied"])
            self.assertEqual(document["contentAudit"]["mode"], "native")
            self.assertEqual(json.loads((ROOT / applied["backup"]).read_text()), source)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    def test_config_merge_rejects_unfilled_values_file(self):
        source = self.configuration()
        catalog = {"catalogVersion": 1, "stages": {"3": {"customerConfig": {"parameters": {"platform": {"containerRegistryName": "REPLACE_ACR_NAME"}}}}}}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            directory = Path(folder)
            config_path = directory / "customer.json"
            catalog_path = directory / "catalog.json"
            values_path = directory / "values.local.json"
            config_path.write_text(json.dumps(source))
            catalog_path.write_text(json.dumps(catalog))
            values_path.write_text(json.dumps({"REPLACE_ACR_NAME": ""}))
            with patch("sys.argv", ["merge-config", "--config", str(config_path), "--catalog", str(catalog_path), "--values", str(values_path), "--stage", "3"]), self.assertRaisesRegex(ValueError, "nonempty strings"):
                merge_config_main()

    def test_config_merge_rejects_symlinked_parent_paths(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            base = Path(folder)
            real = base / "real"
            real.mkdir()
            config = real / "customer.json"
            config.write_text("{}")
            linked = base / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink path components"):
                read_json(linked / "customer.json", "local customer configuration")
            output = base / "output-real"
            output.mkdir()
            output_link = base / "output-link"
            output_link.symlink_to(output, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink path components"):
                operation_directory(output_link, 3)


if __name__ == "__main__":
    unittest.main()