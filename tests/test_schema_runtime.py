import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.customer_migration import ROOT
from scripts.migration_deploy import group_id
from scripts.schema_runtime import migrate_schema, migration_template, run_schema_container
from scripts.migration_runtime import validate_action
from tests.test_customer_migration import customer_config


class SchemaRuntimeTests(unittest.TestCase):
    def test_schema_container_uses_only_image_embedded_runtime_code(self):
        config = customer_config()
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.schema_runtime.run_command", return_value=json.dumps({"ok": True})) as command, patch("scripts.schema_runtime.subprocess.run"):
            token = Path(directory) / "token"
            token.write_text("synthetic")
            result = run_schema_container(config, Path(directory), "synthetic-template", token, "inspect", image="sha256:" + "a" * 64)
        arguments = command.call_args.args[0]
        self.assertEqual(result, {"ok": True})
        self.assertNotIn("PYTHONPATH=/code", arguments)
        self.assertFalse(any("dst=/code" in argument for argument in arguments))
        self.assertIn("LiteLLM.runtime.schema_migration", arguments)

    def test_schema_action_is_only_stage5_for_both_paths(self):
        config = customer_config()
        for mode in ("migration", "greenfield"):
            config["deploymentMode"] = mode
            validate_action(config, 5, "schema-migrate")
            with self.assertRaises(ValueError):
                validate_action(config, 6, "schema-migrate")

    def test_template_rejects_arbitrary_hosts_and_connection_strings(self):
        migration_template("synthetic.postgres.database.azure.com", "litellm")
        for host, database in (("localhost", "litellm"), ("synthetic.postgres.database.azure.com", "postgresql://other")):
            with self.assertRaises(ValueError):
                migration_template(host, database)

    def test_plan_execute_approval_and_token_cleanup(self):
        config = customer_config()
        config["databaseAccess"] = {"migrationPrincipalId": "33333333-3333-4333-8333-333333333333"}
        config["parameters"]["platform"]["stage5Data"] = {"postgresqlDatabaseName": "litellm"}
        azure = Mock()
        azure.run.return_value = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}

        def cloud(arguments):
            if arguments[:3] == ["deployment", "group", "create"]:
                return {"properties": {"provisioningState": "Succeeded"}}
            if arguments[0] == "deployment":
                return {"state": "Succeeded", "platform": {"stage5Deployed": True, "postgresqlServerName": "target"}}
            return {"id": group_id(config) + "/providers/Microsoft.DBforPostgreSQL/flexibleServers/target", "host": "synthetic.postgres.database.azure.com", "auth": {"activeDirectoryAuth": "Enabled", "passwordAuth": "Disabled"}, "network": {"publicNetworkAccess": "Disabled"}}

        observed = {"assets": {"schemaSha256": "a" * 64, "runtimeCode": {"schema_migration.py": "b" * 64}}, "state": {}, "pending": ["first"], "stateSha256": "b" * 64}
        calls = []

        image_ids = iter(("sha256:" + character * 64 for character in "def"))
        runtime_image = {"reference": "litellm-azure-source-check:" + "a" * 12, "contentSha256": "c" * 64, "buildInputsSha256": "e" * 64, "source": "synthetic"}

        def container(config, directory, template, token_path, operation, state="", image=""):
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            self.assertRegex(image, r"^sha256:[0-9a-f]{64}$")
            calls.append((operation, image))
            return observed if operation == "inspect" else {"schemaVerified": True, "assets": observed["assets"], "stateSha256": "c" * 64}

        azure.scoped.side_effect = cloud
        def build(_revision):
            return {**runtime_image, "id": next(image_ids), "stderr": ""}

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.schema_runtime.AzureCommands", return_value=azure), patch("scripts.schema_runtime.build_hardened_runtime", side_effect=build), patch("scripts.schema_runtime.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"accessToken": "synthetic-sensitive-token", "expires_on": int(time.time()) + 3600}))), patch("scripts.schema_runtime.run_schema_container", side_effect=container), patch("scripts.schema_runtime.build_runtime_revision", return_value="9" * 64) as runtime:
            path = Path(directory)
            plan = migrate_schema(config, "plan", "a" * 40, path, "")
            runtime.assert_called_once_with("azure", "a" * 40)
            self.assertEqual([operation for operation, _image in calls], ["inspect"])
            with self.assertRaisesRegex(ValueError, "plan changed"):
                migrate_schema(config, "execute", "a" * 40, path, "f" * 64)
            self.assertNotIn("execute", [operation for operation, _image in calls])
            result = migrate_schema(config, "execute", "a" * 40, path, plan["planSha256"])
            self.assertEqual([image for operation, image in calls if operation == "inspect"], ["sha256:" + character * 64 for character in "def"])
            self.assertTrue(result["schemaVerified"])
            self.assertFalse(result["stageAccepted"])
            self.assertFalse((path / "schema-db-token").exists())
            receipt = json.loads((path / "schema-receipt-template.json").read_text())
            self.assertEqual(receipt["resources"], [])
            self.assertEqual(receipt["outputs"]["databaseSchema"]["value"]["database"], "litellm")
            self.assertEqual(receipt["outputs"]["databaseSchema"]["value"]["image"], runtime_image)
            self.assertEqual(receipt["outputs"]["databaseSchema"]["value"]["executionImageId"], "sha256:" + "f" * 64)
            self.assertEqual(receipt["outputs"]["databaseSchema"]["value"]["planSha256"], plan["planSha256"])
            self.assertEqual({key: receipt["outputs"]["databaseSchema"]["value"][key] for key in ("revision", "runtimeRevision")}, {"revision": "a" * 40, "runtimeRevision": "9" * 64})
            for name in ("runtime-summary.json", "runtime-review.json"):
                self.assertNotIn("synthetic-sensitive-token", (path / name).read_text())