"""Plan and execute isolated schema migrations against the deployed target database."""

import json
import os
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlunsplit
from uuid import uuid4

from scripts.customer_migration import ROOT, deployment_mode, fingerprint, private_write, require, stage_fingerprint
from scripts.database_roles import database_access
from scripts.migration_deploy import AzureCommands, deployment_name, group_id
from scripts.migration_runtime import run_command
from scripts.runtime_revision import build_runtime_revision
from scripts.source_supply_chain import build_hardened_runtime


def migration_template(host, database):
    require(isinstance(host, str) and re.fullmatch(r"[a-z0-9-]+\.postgres\.database\.azure\.com", host) is not None, "Unexpected target PostgreSQL host")
    require(isinstance(database, str) and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", database) is not None, "Invalid target database name")
    options = urlencode({"sslmode": "require", "sslaccept": "strict", "sslcert": "/etc/ssl/certs/ca-certificates.crt", "schema": "public", "connection_limit": "1", "connect_timeout": "15"})
    return urlunsplit(("postgresql", "llmgw_migrator" + "@" + host + ":5432", "/" + database, options, ""))


def run_schema_container(config, directory, template, token_path, operation, state="", image=""):
    require(isinstance(image, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", image) is not None, "Schema migration requires the immutable hardened runtime image ID")
    container = "llmgw-schema-" + uuid4().hex
    arguments = ["docker", "run", "--rm", "--name", container, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,nosuid,size=512m", "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "LITELLM_LOCAL_MODEL_COST_MAP=True", "--env", "AZURE_DATABASE_URL_TEMPLATE=" + template, "--env", "LLMGW_DATABASE_TOKEN_FILE=/run/db-token", "--mount", f"type=bind,src={token_path},dst=/run/db-token,readonly", "--workdir", "/tmp", "--entrypoint", "/app/.venv/bin/python", image, "-m", "LiteLLM.runtime.schema_migration", "--operation", operation, "--mode", deployment_mode(config)]
    if operation == "execute":
        arguments.extend(["--expected-state", state])
    try:
        result = run_command(arguments, directory, "schema-" + operation)
        return json.loads(result)
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=False, timeout=30)


def migrate_schema(config, operation, revision, directory, approved):
    require(operation in {"plan", "execute"}, "Invalid schema migration operation")
    database_access(config)
    azure = AzureCommands(config, directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure scope mismatch")
    group = config["target"]["resourceGroup"]
    output = azure.scoped(["deployment", "group", "show", "--resource-group", group, "--name", deployment_name(config, 5, "platform"), "--query", "{state:properties.provisioningState,platform:properties.outputs.platform.value}"])
    require(output.get("state") == "Succeeded" and output.get("platform", {}).get("stage5Deployed") is True, "Deploy Stage5 infrastructure before schema migration")
    name = output["platform"]["postgresqlServerName"]
    server = azure.scoped(["postgres", "flexible-server", "show", "--resource-group", group, "--name", name, "--query", "{id:id,host:fullyQualifiedDomainName,auth:authConfig,network:network}"])
    require(server["id"].lower() == (group_id(config) + "/providers/Microsoft.DBforPostgreSQL/flexibleServers/" + name).lower(), "Database server outside target scope")
    require(server["auth"].get("activeDirectoryAuth") == "Enabled" and server["auth"].get("passwordAuth") == "Disabled" and server["network"].get("publicNetworkAccess") == "Disabled", "Schema migration requires private Entra-only PostgreSQL")
    database = config["parameters"]["platform"]["stage5Data"]["postgresqlDatabaseName"]
    template = migration_template(server["host"], database)
    # The migrator is built from this checkout, so its inputs must equal the reviewed backend runtime revision.
    runtime_revision = build_runtime_revision("azure", revision)
    built_image = build_hardened_runtime(revision)
    private_write(directory / "schema-image-build.stderr.txt", built_image["stderr"])
    execution_image_id = built_image["id"]
    runtime_image = {key: value for key, value in built_image.items() if key not in {"stderr", "id"}}
    private_write(directory / "schema-image-observation.json", json.dumps({**runtime_image, "executionImageId": execution_image_id}, indent=2) + "\n")
    token_result = subprocess.run(["az", "account", "get-access-token", "--subscription", config["azure"]["subscriptionId"], "--resource-type", "oss-rdbms", "--output", "json"], capture_output=True, text=True, check=False, timeout=120)
    require(token_result.returncode == 0, "Unable to acquire migration identity token")
    token = json.loads(token_result.stdout)
    require(isinstance(token.get("accessToken"), str) and token["accessToken"] and int(token.get("expires_on", 0)) > time.time() + 2100, "Migration token must remain valid beyond the 30 minute execution limit")
    token_path = directory / "schema-db-token"
    private_write(token_path, token["accessToken"])
    try:
        observed = run_schema_container(config, directory, template, token_path, "inspect", image=execution_image_id)
        plan = {"stage": 5, "action": "schema-migrate", "revision": revision, "runtimeRevision": runtime_revision, "configSha256": stage_fingerprint(config, 5), "image": runtime_image, "serverId": server["id"], "database": database, "migrationRole": "llmgw_migrator", **observed}
        digest = fingerprint(plan)
        summary = {"stage": 5, "action": "schema-migrate", "planSha256": digest, "pendingMigrations": len(observed["pending"]), "stageAccepted": False}
        private_write(directory / "runtime-review.json", json.dumps(plan, indent=2) + "\n")
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
        if operation == "execute":
            require(approved == digest, "Schema plan changed or was not approved")
            require(int(token["expires_on"]) > time.time() + 1900, "Token lifetime is insufficient after schema inspection; replan with a fresh token")
            result = run_schema_container(config, directory, template, token_path, "execute", observed["stateSha256"], image=execution_image_id)
            require(result.get("schemaVerified") is True and result.get("assets") == observed["assets"], "Schema verification did not match the approved release")
            receipt = {"revision": revision, "runtimeRevision": runtime_revision, "configSha256": stage_fingerprint(config, 5), "planSha256": digest, "serverId": server["id"], "database": database, "image": runtime_image, "executionImageId": execution_image_id, "assets": result["assets"], "stateSha256": result["stateSha256"], "verifiedAt": datetime.now(timezone.utc).isoformat()}
            receipt_template = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"databaseSchema": {"type": "object", "value": receipt}}}
            receipt_path = directory / "schema-receipt-template.json"
            private_write(receipt_path, json.dumps(receipt_template))
            saved = azure.scoped(["deployment", "group", "create", "--resource-group", group, "--name", deployment_name(config, 5, "schema-migrate"), "--mode", "Incremental", "--template-file", str(receipt_path)])
            require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "Schema verified but saving the deployment receipt failed")
            summary.update(applied=True, schemaVerified=True, schemaStateSha256=result["stateSha256"])
            private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary))
        return summary
    finally:
        token_path.unlink(missing_ok=True)