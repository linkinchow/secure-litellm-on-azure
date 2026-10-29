import ast
import copy
import hashlib
import json
import re
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from scripts.backend_manifest import BACKEND_CALLBACKS, application_authentication, application_settings, prepare_backend_documents, render_backend_manifest, verify_runtime_image
from scripts.customer_migration import ROOT, stage_fingerprint, validate_config
from scripts.migration_deploy import group_id
from scripts.runtime_secrets import BACKEND_SECRETS, backend_secrets
from scripts.native_audit import native_audit_settings, render_native_audit
from LiteLLM.runtime.application import application_config
from scripts.migration_runtime import check_application, publish
from tests.test_customer_migration import customer_config


def backend_customer():
    config = customer_config()
    config["parameters"]["platform"]["stage4Network"]["podCidr"] = "10.244.0.0/16"
    config["parameters"]["platform"]["azureOpenAIConnections"] = [{"alias": "primary", "accountName": "synthetic-model"}]
    config["parameters"]["platform"]["stage5Data"] = {"postgresqlDatabaseName": "litellm"}
    config["application"] = {"backendImage": "customerregistry.azurecr.io/litellm-azure@sha256:" + "a" * 64, "models": [{"modelGroup": "coding", "connectionAlias": "primary", "deploymentName": "gpt-deployment", "baseModel": "azure/gpt-6-luna", "id": "primary-coding", "apiVersion": "v1"}]}
    return config


def cosign_payload(image, runtime="azure", environment="test", **annotations):
    return json.dumps([{"critical": {"image": {"docker-manifest-digest": image.rsplit("@", 1)[1]}}, "optional": {"llmgw.runtime": runtime, "llmgw.environment": environment, **annotations}}])


class BackendManifestTests(unittest.TestCase):

    def test_l3_activation_accepts_exactly_the_backend_callbacks(self):
        source = (Path(__file__).resolve().parents[1] / "l3-audit/src/activation.ts").read_text(encoding="utf-8")
        declared = re.search(r"REVIEWED_CALLBACKS: readonly string\[\] = \[([^\]]*)\]", source)
        self.assertIsNotNone(declared)
        self.assertEqual(tuple(re.findall(r"'([^']+)'", declared.group(1))), BACKEND_CALLBACKS)
    def test_native_stage8_publishes_backend_only_with_spend_logs(self):
        config = backend_customer()
        config["application"]["authentication"] = {"mode": "native", "adminUsername": "gateway-admin"}
        config["contentAudit"] = {"mode": "native", "retentionDays": 7, "contentPolicyAccepted": True}
        platform = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": "33333333-3333-4333-8333-333333333333", "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32} for name in backend_secrets(config)}
        source = render_backend_manifest(config, platform, versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24")

        def command(arguments, _directory, label, **_kwargs):
            if label == "server-dry-run":
                return "{}"
            if label.startswith("live-") or label.startswith("audit-kube-") or label == "audit-window-check":
                return ""
            return ""

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl", "--namespace", "litellm"]), patch("scripts.backend_manifest.prepare_backend_documents", return_value=source), patch("scripts.migration_runtime.run_command", side_effect=command), patch("scripts.audit_runtime.AuditCluster.get", return_value=None):
            path = Path(directory)
            publish(config, 8, "application", "plan", "a" * 40, path, "")
            review = json.loads((path / "runtime-review.json").read_text())
            deployments = [item["metadata"]["name"] for item in review["desiredObjects"] if item["kind"] == "Deployment"]
            self.assertEqual(deployments, ["litellm"])
            runtime = yaml.safe_load(next(item for item in review["desiredObjects"] if item["kind"] == "ConfigMap" and "config.yaml" in item.get("data", {}))["data"]["config.yaml"])
            self.assertTrue(runtime["general_settings"]["store_prompts_in_spend_logs"])

    def test_native_authentication_routes_only_managed_private_ingress_to_backend(self):
        config = backend_customer()
        config["application"]["authentication"] = {"mode": "native", "adminUsername": "gateway-admin"}
        platform = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": "33333333-3333-4333-8333-333333333333", "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32} for name in backend_secrets(config)}
        documents = render_backend_manifest(config, platform, versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24")
        deployment = next(item for item in documents if item["kind"] == "Deployment")
        autoscaler = next(item for item in documents if item["kind"] == "HorizontalPodAutoscaler")
        self.assertNotIn("replicas", deployment["spec"])
        self.assertEqual(autoscaler["spec"]["minReplicas"], 2)
        environment = {item["name"]: item["value"] for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        runtime = yaml.safe_load(next(item for item in documents if item["kind"] == "ConfigMap" and "config.yaml" in item.get("data", {}))["data"]["config.yaml"])
        self.assertEqual(runtime["model_list"][0]["litellm_params"]["base_model"], "azure/gpt-6-luna")
        self.assertEqual(runtime["model_list"][0]["model_info"]["base_model"], "azure/gpt-6-luna")
        self.assertEqual(application_authentication(config), {"mode": "native", "adminUsername": "gateway-admin"})
        self.assertEqual(environment["LLMGW_GATEWAY_AUTH_MODE"], "native")
        self.assertEqual(environment["LLMGW_NATIVE_ADMIN_USERNAME"], "gateway-admin")
        self.assertEqual(environment["LLMGW_TRUSTED_PROXY_CIDRS"], config["parameters"]["platform"]["stage4Network"]["podCidr"])
        policy = next(item for item in documents if item["kind"] == "NetworkPolicy" and item["metadata"]["name"] == "allow-litellm-required-traffic")
        peers = policy["spec"]["ingress"][0]["from"]
        self.assertEqual({peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] for peer in peers}, {"llm-api-ingress", "llm-admin-ingress"})
        self.assertTrue(all(peer["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "llmgw-ingress" for peer in peers))
        conflicting = copy.deepcopy(documents)
        next(item for item in conflicting if item["kind"] == "Deployment")["spec"]["replicas"] = 2
        with self.assertRaisesRegex(ValueError, "HPA ownership"):
            check_application(conflicting, 6, config)
        for authentication in ({"mode": "native"}, {"mode": "native", "adminUsername": "bad name"}, {"mode": "other"}):
            invalid = copy.deepcopy(config)
            invalid["application"]["authentication"] = authentication
            with self.assertRaises(ValueError):
                application_settings(invalid)

    def test_local_runtime_signature_uses_explicit_customer_public_key(self):
        config = backend_customer()
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.backend_manifest.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"loginServer": "customerregistry.azurecr.io", "accessToken": "synthetic-acr-token"}))), patch("scripts.backend_manifest.runtime_revision", return_value="c" * 64), patch("scripts.migration_runtime.run_command", return_value=cosign_payload(config["application"]["backendImage"], **{"llmgw.runtimeRevision": "c" * 64})) as command:
            path = Path(directory)
            public_key = path / "cosign.pub"
            public_key.write_text("public")
            verify_runtime_image(config, "a" * 40, path, config["application"]["backendImage"], "azure", public_key=public_key)
            arguments = command.call_args.args[0]
        self.assertIn("--key", arguments)
        self.assertIn(str(public_key), arguments)
        self.assertNotIn("--certificate-identity", arguments)

    def test_local_application_plan_binds_cosign_public_key_contents(self):
        config = backend_customer()
        documents = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "litellm"}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "litellm", "namespace": "litellm", "uid": "synthetic"}, "spec": {"replicas": 2}},
        ]
        def command(_arguments, _directory, label, **_kwargs):
            return "{}" if label == "server-dry-run" else ""

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.backend_manifest.prepare_backend_documents", return_value=documents), patch("scripts.migration_runtime.check_application"), patch("scripts.migration_runtime.run_command", side_effect=command):
            path = Path(directory)
            public_key = path / "cosign.pub"
            public_key.write_text("public-one")
            publish(config, 6, "application", "plan", "a" * 40, path, "", image_public_key=public_key)
            first = json.loads((path / "runtime-summary.json").read_text())["planSha256"]
            public_key.write_text("public-two")
            publish(config, 6, "application", "plan", "a" * 40, path, "", image_public_key=public_key)
            second = json.loads((path / "runtime-summary.json").read_text())["planSha256"]
        self.assertNotEqual(first, second)

    def test_native_logging_is_explicit_retained_and_does_not_mutate_stage6(self):
        config = backend_customer()
        config["application"]["authentication"] = {"mode": "native", "adminUsername": "gateway-admin"}
        config["contentAudit"] = {"mode": "native", "retentionDays": 14, "contentPolicyAccepted": True}
        source = [
            {"kind": "ConfigMap", "metadata": {"name": "litellm-config-old"}, "data": {"config.yaml": yaml.safe_dump({"general_settings": {"store_prompts_in_spend_logs": False}})}},
            {"kind": "Deployment", "metadata": {"name": "litellm"}, "spec": {"template": {"spec": {"volumes": [{"configMap": {"name": "litellm-config-old"}}]}}}},
        ]
        result = render_native_audit(config, source)
        settings = yaml.safe_load(result[0]["data"]["config.yaml"])["general_settings"]
        self.assertTrue(settings["store_prompts_in_spend_logs"])
        self.assertFalse(settings["disable_spend_logs"])
        self.assertEqual(settings["maximum_spend_logs_retention_period"], "14d")
        self.assertEqual(result[1]["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"], result[0]["metadata"]["name"])
        self.assertFalse(yaml.safe_load(source[0]["data"]["config.yaml"])["general_settings"]["store_prompts_in_spend_logs"])
        for updates in ({"contentPolicyAccepted": False}, {"retentionDays": True}, {"retentionDays": 0}, {"mode": "disabled"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                native_audit_settings({**config, "contentAudit": {**config["contentAudit"], **updates}})
        with self.assertRaisesRegex(ValueError, "L3"):
            native_audit_settings({**config, "auditRuntime": {}})
        with self.assertRaisesRegex(ValueError, "L3 bindings"):
            native_audit_settings({**config, "proxy": {"bindings": [{"auditTeamId": "preserve-existing"}]}})

    def test_native_logging_accepts_only_reviewed_request_callbacks(self):
        config = backend_customer()
        config["application"]["authentication"] = {"mode": "native", "adminUsername": "gateway-admin"}
        config["contentAudit"] = {"mode": "native", "retentionDays": 14, "contentPolicyAccepted": True}

        def source(settings):
            runtime = {"litellm_settings": settings, "general_settings": {"store_prompts_in_spend_logs": False}}
            return [{"kind": "ConfigMap", "metadata": {"name": "litellm-config-old"}, "data": {"config.yaml": yaml.safe_dump(runtime)}}]

        result = render_native_audit(config, source({"callbacks": list(BACKEND_CALLBACKS)}))
        self.assertEqual(yaml.safe_load(result[0]["data"]["config.yaml"])["litellm_settings"]["callbacks"], list(BACKEND_CALLBACKS))
        render_native_audit(config, source({"callbacks": []}))
        for settings in ({"callbacks": [*BACKEND_CALLBACKS, "custom_logger.instance"]}, {"callbacks": "custom_logger.instance"},
                         {"callbacks": [{"name": "custom"}]}, {"callbacks": list(BACKEND_CALLBACKS), "success_callback": ["langfuse"]}):
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, "content callbacks"):
                render_native_audit(config, source(settings))

    def test_backend_callbacks_are_shipped_in_the_runtime_image(self):
        self.assertEqual(BACKEND_CALLBACKS, ("LiteLLM.runtime.callbacks.codex_passthrough",))
        dockerfile = (ROOT / "LiteLLM/runtime/Dockerfile").read_text()
        self.assertIn("COPY --chown=10001:10001 LiteLLM/runtime /opt/llmgw/LiteLLM/runtime", dockerfile)
        self.assertIn("ENV PYTHONPATH=/opt/llmgw", dockerfile)
        self.assertIn("!LiteLLM/runtime/*.py", (ROOT / "LiteLLM/runtime/Dockerfile.dockerignore").read_text().splitlines())
        for name in BACKEND_CALLBACKS:
            module, attribute = name.rsplit(".", 1)
            source = ROOT / (module.replace(".", "/") + ".py")
            self.assertEqual(source.parent, ROOT / "LiteLLM/runtime")
            assigned = {target.id for node in ast.parse(source.read_text()).body if isinstance(node, ast.Assign)
                        for target in node.targets if isinstance(target, ast.Name)}
            self.assertIn(attribute, assigned)
        stage6 = yaml.safe_load(yaml.safe_load((ROOT / "deploy/components/stage6-ha/config-patch.yaml").read_text())["data"]["config.yaml"])
        self.assertNotIn("callbacks", stage6["litellm_settings"], "The public-image Kustomize baseline cannot import runtime-image callbacks")

    def test_application_config_only_changes_stage6_fingerprint(self):
        config = backend_customer()
        plain = copy.deepcopy(config)
        plain.pop("application")
        validate_config(config, "test")
        self.assertEqual(stage_fingerprint(config, 5), stage_fingerprint(plain, 5))
        self.assertNotEqual(stage_fingerprint(config, 6), stage_fingerprint(plain, 6))
        native = copy.deepcopy(config)
        native["application"]["authentication"] = {"mode": "native", "adminUsername": "gateway-admin"}
        self.assertEqual(stage_fingerprint(config, 3), stage_fingerprint(native, 3))
        self.assertNotEqual(stage_fingerprint(config, 4), stage_fingerprint(native, 4))
        self.assertNotEqual(stage_fingerprint(config, 5), stage_fingerprint(native, 5))
        renamed = copy.deepcopy(native)
        renamed["application"]["authentication"]["adminUsername"] = "another-admin"
        self.assertEqual(stage_fingerprint(native, 4), stage_fingerprint(renamed, 4))
        self.assertEqual(stage_fingerprint(native, 5), stage_fingerprint(renamed, 5))

    def test_publisher_uses_generated_documents_and_rejects_manual_override(self):
        config = backend_customer()
        platform = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": "33333333-3333-4333-8333-333333333333", "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32} for name in BACKEND_SECRETS}
        documents = render_backend_manifest(config, platform, versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24")

        def command(arguments, directory, label, **kwargs):
            return "{}" if label == "server-dry-run" else ""

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch.dict("os.environ", {}, clear=True), patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.backend_manifest.prepare_backend_documents", return_value=documents) as render, patch("scripts.migration_runtime.run_command", side_effect=command) as execute:
            path = Path(directory)
            publish(config, 6, "application", "plan", "a" * 40, path, "")
            render.assert_called_once()
            digest = json.loads((path / "runtime-summary.json").read_text())["planSha256"]
            self.assertTrue(all("--dry-run=server" in call.args[0] for call in execute.call_args_list if "apply" in call.args[0]))
            publish(config, 6, "application", "execute", "a" * 40, path, digest)
            self.assertTrue(json.loads((path / "runtime-summary.json").read_text())["applied"])
            with patch.dict("os.environ", {"MIGRATION_MANIFEST_YAML": "manual"}), self.assertRaisesRegex(ValueError, "cannot be mixed"):
                publish(config, 6, "application", "plan", "a" * 40, path, "")
            with self.assertRaisesRegex(ValueError, "approved authentication and audit configuration"):
                publish(config, 7, "application", "plan", "a" * 40, path, "")

    def test_native_stage6_verifies_private_ingress_after_backend_rollout(self):
        config = backend_customer()
        config["application"]["authentication"] = {"mode": "native", "adminUsername": "gateway-admin"}
        platform = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": "33333333-3333-4333-8333-333333333333", "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32} for name in backend_secrets(config)}
        documents = render_backend_manifest(config, platform, versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24")
        existing_proxy = {"value": False}
        def command(arguments, _directory, label, **_kwargs):
            if label == "existing-entra-proxy-llm-api-proxy" and existing_proxy["value"]:
                return "{}"
            return "{}" if label == "server-dry-run" else ""
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl"]), patch("scripts.backend_manifest.prepare_backend_documents", return_value=documents), patch("scripts.migration_runtime.run_command", side_effect=command) as execute, patch("scripts.private_ingress_runtime.verify_private_ingress_backends", return_value={"backendRoutesVerified": True}) as verify:
            path = Path(directory)
            publish(config, 6, "application", "plan", "a" * 40, path, "")
            verify.assert_not_called()
            digest = json.loads((path / "runtime-summary.json").read_text())["planSha256"]
            publish(config, 6, "application", "execute", "a" * 40, path, digest)
            verify.assert_called_once()
            rollout = next(index for index, call in enumerate(execute.call_args_list) if "rollout" in call.args[0])
            self.assertLess(rollout, len(execute.call_args_list))
            self.assertTrue(json.loads((path / "runtime-summary.json").read_text())["backendRoutesVerified"])
            existing_proxy["value"] = True
            with self.assertRaisesRegex(ValueError, "explicitly migrate existing Entra proxy"):
                publish(config, 6, "application", "plan", "a" * 40, path, "")
    def test_receipts_and_image_signature_are_bound_before_rendering(self):
        config = backend_customer()
        config["parameters"]["platform"]["stage4Network"]["privateEndpointSubnetName"] = "snet-private-endpoints"
        platform = {"stage5Deployed": True, "postgresqlServerName": "target", "keyVaultName": "synthetic-backend", "workloadIdentityClientId": "33333333-3333-4333-8333-333333333333", "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        revision = "a" * 40
        common = {"revision": revision, "runtimeRevision": "c" * 64, "configSha256": stage_fingerprint(config, 5)}
        schema = {**common, "serverId": group_id(config) + "/providers/Microsoft.DBforPostgreSQL/flexibleServers/target", "database": "litellm"}
        versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32} for name in BACKEND_SECRETS}
        secrets_receipt = {**common, "vaultId": group_id(config) + "/providers/Microsoft.KeyVault/vaults/synthetic-backend", "secrets": versions}

        def cloud(arguments):
            if arguments[0] == "deployment":
                name = arguments[arguments.index("--name") + 1]
                if name.endswith("platform"):
                    return {"state": "Succeeded", "platform": platform}
                return {"state": "Succeeded", "receipt": schema if name.endswith("schema-migrate") else secrets_receipt}
            if arguments[0] == "postgres":
                return {"host": "synthetic.postgres.database.azure.com", "auth": {"activeDirectoryAuth": "Enabled", "passwordAuth": "Disabled"}, "network": {"publicNetworkAccess": "Disabled"}}
            return "10.30.8.0/24"

        azure = Mock()
        azure.scoped.side_effect = cloud
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch.dict("os.environ", {"GITHUB_REPOSITORY": "customer/gateway", "GITHUB_REF": "refs/heads/main"}), patch("scripts.backend_manifest.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"loginServer": "customerregistry.azurecr.io", "accessToken": "synthetic-acr-token"}))), patch("scripts.backend_manifest.runtime_revision", return_value="c" * 64) as current, patch("scripts.migration_runtime.run_command", return_value=cosign_payload(config["application"]["backendImage"], **{"llmgw.revision": "d" * 40, "llmgw.runtimeRevision": "c" * 64})) as command:
            path = Path(directory)
            result = prepare_backend_documents(config, revision, path, azure)
            check_application(result, 6, config)
            self.assertEqual({call.args for call in current.call_args_list}, {("azure", revision)})
            arguments = command.call_args.args[0]
            self.assertIn("llmgw.runtime=azure", arguments)
            self.assertNotIn("llmgw.revision=" + revision, arguments)
            self.assertIn("llmgw.environment=test", arguments)
            self.assertIn("https://github.com/customer/gateway/.github/workflows/promote-litellm-image.yml@refs/heads/main", arguments)
            self.assertNotIn("synthetic-acr-token", str(command.call_args))
            self.assertFalse((path / "backend-acr-auth/config.json").exists())
            # A later commit with the same runtime inputs keeps the receipts; a different runtime revision does not.
            schema["revision"] = "b" * 40
            prepare_backend_documents(config, revision, path, azure)
            schema["runtimeRevision"] = "e" * 64
            with self.assertRaisesRegex(ValueError, "stale"):
                prepare_backend_documents(config, revision, path, azure)
            # Receipts written before runtime revisions existed still require the exact commit.
            del schema["runtimeRevision"]
            with self.assertRaisesRegex(ValueError, "stale"):
                prepare_backend_documents(config, revision, path, azure)
            schema["revision"] = revision
            prepare_backend_documents(config, revision, path, azure)
            command.return_value = cosign_payload(config["application"]["backendImage"], **{"llmgw.revision": revision, "llmgw.runtimeRevision": "e" * 64})
            with self.assertRaisesRegex(ValueError, "rebuild and re-sign"):
                prepare_backend_documents(config, revision, path, azure)

    def test_rendered_backend_uses_real_outputs_without_secret_sync_or_legacy_refs(self):
        config = backend_customer()
        platform = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": "33333333-3333-4333-8333-333333333333", "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32, "version": "a" * 32} for name in BACKEND_SECRETS}
        documents = render_backend_manifest(config, platform, versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24")
        check_application(documents, 6, config)
        unsafe = copy.deepcopy(documents)
        bad_deployment = next(item for item in unsafe if item["kind"] == "Deployment")
        bad_container = bad_deployment["spec"]["template"]["spec"]["containers"][0]
        next(item for item in bad_container["env"] if item["name"] == "AZURE_DATABASE_URL_TEMPLATE")["value"] = "postgresql://stored-secret"
        with self.assertRaises(ValueError):
            check_application(unsafe, 6, config)
        serialized = json.dumps(documents)
        self.assertNotIn("REPLACE", serialized)
        self.assertNotIn("litellm-runtime-secrets", serialized)
        self.assertNotIn("secretObjects", serialized)
        deployment = next(item for item in documents if item["kind"] == "Deployment")
        provider = next(item for item in documents if item["kind"] == "SecretProviderClass")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        environment = {item["name"]: item["value"] for item in container["env"]}
        self.assertNotIn("DATABASE_URL", environment)
        self.assertEqual(environment["LLMGW_BACKEND_SECRETS_DIR"], "/mnt/backend-secrets")
        self.assertEqual(container["image"], config["application"]["backendImage"])
        self.assertEqual(
            deployment["spec"]["template"]["metadata"]["annotations"]["llmgw/backend-secret-mount"],
            hashlib.sha256(provider["spec"]["parameters"]["objects"].encode()).hexdigest(),
        )
        runtime = yaml.safe_load(next(item for item in documents if item["kind"] == "ConfigMap")["data"]["config.yaml"])
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            config_file = Path(directory) / "generated.yaml"
            config_file.write_text(yaml.safe_dump(runtime))
            application_config(config_file)
        self.assertTrue(runtime["general_settings"]["disable_prisma_schema_update"])
        self.assertTrue(runtime["router_settings"]["cache_kwargs"]["azure_redis_ad_token"])
        self.assertTrue(runtime["router_settings"]["cache_kwargs"]["ssl_check_hostname"])
        self.assertEqual(runtime["litellm_settings"], {"enable_azure_ad_token_refresh": True, "callbacks": ["LiteLLM.runtime.callbacks.codex_passthrough"]})
        self.assertEqual(runtime["model_list"][0]["litellm_params"]["api_base"], "https://synthetic-model.openai.azure.com")
        traffic = next(item for item in documents if item["kind"] == "NetworkPolicy" and item["metadata"]["name"] == "allow-litellm-required-traffic")
        self.assertNotIn("namespaceSelector", json.dumps(traffic["spec"]["ingress"]))
        self.assertEqual(traffic["spec"]["egress"][1]["to"][0]["ipBlock"]["cidr"], "10.30.8.0/24")

    def test_unapproved_images_accounts_and_duplicate_ids_are_rejected(self):
        config = backend_customer()
        for change in (lambda value: value["application"].update(backendImage="other.azurecr.io/litellm:latest"), lambda value: value["application"]["models"][0].update(connectionAlias="other"), lambda value: value["application"]["models"][0].update(baseModel="gpt-6-luna"), lambda value: value["application"]["models"].append(copy.deepcopy(value["application"]["models"][0])), lambda value: value["parameters"]["platform"]["azureOpenAIConnections"].append(copy.deepcopy(value["parameters"]["platform"]["azureOpenAIConnections"][0]))):
            modified = copy.deepcopy(config)
            change(modified)
            with self.assertRaises(ValueError):
                application_settings(modified)

    def test_existing_model_mapping_without_base_model_remains_compatible(self):
        config = backend_customer()
        config["application"]["models"][0].pop("baseModel")
        self.assertNotIn("baseModel", application_settings(config)["models"][0])