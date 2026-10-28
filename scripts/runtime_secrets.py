"""Initialize runtime secrets without rotating existing encryption material."""

import hmac
import base64
import json
import os
import secrets
import subprocess
from datetime import datetime, timezone
from uuid import UUID

from scripts.customer_migration import deployment_mode, fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands, deployment_name, group_id
from scripts.runtime_revision import runtime_revision


BACKEND_SECRETS = {"litellm-master-key": "LITELLM_MASTER_KEY", "litellm-salt-key": "LITELLM_SALT_KEY"}
NATIVE_UI_SECRET = {"litellm-ui-password": "UI_PASSWORD"}


def backend_secrets(config=None):
    result = dict(BACKEND_SECRETS)
    if config is not None and config.get("application", {}).get("authentication", {}).get("mode") == "native":
        result.update(NATIVE_UI_SECRET)
    return result


def ensure_key_creation_is_safe(mode, existing, has_tables, required=BACKEND_SECRETS):
    missing_encryption = set(BACKEND_SECRETS) - set(existing)
    require(mode != "greenfield" or not missing_encryption or not has_tables, "Database already has tables but backend encryption keys are missing; recover original keys instead of generating replacements")


def secret_value(value):
    require(isinstance(value, str) and bool(value.strip()) and len(value.encode()) <= 25000, "Secret is empty or exceeds the Key Vault limit")
    return value


def backend_secret_values(mode, existing, legacy=None, required=BACKEND_SECRETS):
    require(mode in {"migration", "greenfield"}, "Unknown secret initialization mode")
    require(isinstance(required, dict) and set(BACKEND_SECRETS).issubset(required) and not set(required) - (set(BACKEND_SECRETS) | set(NATIVE_UI_SECRET)), "Unknown required backend secret")
    require(isinstance(existing, dict) and not set(existing) - required.keys(), "Unknown backend secret")
    for value in existing.values():
        secret_value(value)
    if mode == "migration":
        require(isinstance(legacy, dict) and set(legacy) == set(BACKEND_SECRETS.values()), "Read both existing Master Key and Salt before migrating; absent Salt requires an explicitly validated compatibility strategy")
        desired = {name: secret_value(legacy[environment]) for name, environment in BACKEND_SECRETS.items()}
        for name, value in existing.items():
            if name in BACKEND_SECRETS:
                require(hmac.compare_digest(value.encode(), desired[name].encode()), "Existing target encryption material differs from the legacy source; automatic overwrite is forbidden")
            else:
                desired[name] = value
    else:
        require(legacy is None, "Greenfield secret initialization cannot import legacy credentials")
        desired = dict(existing)
        for name in required:
            if name not in desired:
                desired[name] = ("sk-" if name == "litellm-master-key" else "") + secrets.token_urlsafe(48)
    if mode == "migration":
        for name in required:
            if name not in desired:
                desired[name] = secrets.token_urlsafe(48)
    return {name: value for name, value in desired.items() if name not in existing}


def read_legacy_keys(config, directory, salt_source="secret"):
    from scripts.migration_runtime import connect_cluster

    require(deployment_mode(config) == "migration", "Greenfield cannot read legacy keys")
    require(salt_source in {"secret", "master-key"}, "Legacy Salt source must be secret or master-key")
    kube = connect_cluster(config, directory, legacy=True)
    response = subprocess.run([*kube, "get", "secret", "litellm-env", "-o", "json"], capture_output=True, text=True, check=False, timeout=60)
    require(response.returncode == 0, "Unable to read the approved legacy litellm-env Secret")
    document = json.loads(response.stdout)
    data = document.get("data", {})
    encoded_master = data.get("LITELLM_MASTER_KEY")
    require(isinstance(encoded_master, str), "Legacy Master Key is absent")
    master = secret_value(base64.b64decode(encoded_master, validate=True).decode("utf-8"))
    encoded_salt = data.get("LITELLM_SALT_KEY")
    if salt_source == "secret":
        require(isinstance(encoded_salt, str), "Legacy Salt is absent; select master-key only after approving and validating that compatibility strategy")
        salt = secret_value(base64.b64decode(encoded_salt, validate=True).decode("utf-8"))
    else:
        require(encoded_salt is None, "Legacy Secret already contains an explicit Salt; select the secret source")
        salt = master
    values = {"LITELLM_MASTER_KEY": master, "LITELLM_SALT_KEY": salt}
    metadata = document["metadata"]
    require(metadata["name"] == "litellm-env" and metadata["namespace"] == config["legacy"]["namespace"], "Legacy secret scope mismatch")
    return values, {"name": metadata["name"], "uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"], "saltSource": salt_source}


def inspect_backend_secrets(client, mode, required=BACKEND_SECRETS):
    from azure.core.exceptions import ResourceNotFoundError

    existing, metadata = {}, {}
    for name in required:
        try:
            item = client.get_secret(name)
        except ResourceNotFoundError:
            metadata[name] = None
            continue
        properties = item.properties
        tags = properties.tags or {}
        require(properties.enabled is True and properties.expires_on is None, "Backend encryption secrets must remain enabled and have no implicit expiry")
        require(tags.get("llmgw-bootstrap") == "backend" and tags.get("llmgw-mode") == mode, "Refusing to adopt unmanaged or differently initialized backend secrets")
        existing[name] = secret_value(item.value)
        metadata[name] = {"id": properties.id, "version": properties.version}
    return existing, metadata


def apply_backend_secrets(client, mode, existing, metadata, legacy, required=BACKEND_SECRETS):
    values = backend_secret_values(mode, existing, legacy, required)
    for name, value in values.items():
        _current, refreshed = inspect_backend_secrets(client, mode, required)
        require(refreshed == metadata, "Vault secret versions changed during initialization; no overwrite allowed")
        created = client.set_secret(name, value, enabled=True, tags={"llmgw-bootstrap": "backend", "llmgw-mode": mode}, content_type="text/plain")
        metadata[name] = {"id": created.properties.id, "version": created.properties.version}
    complete, verified = inspect_backend_secrets(client, mode, required)
    require(set(complete) == set(required), "Backend secret initialization is incomplete")
    require(verified == metadata, "Backend secret versions changed during final verification")
    for name, value in {**existing, **values}.items():
        require(hmac.compare_digest(complete[name].encode(), value.encode()), "Backend secret values did not match initialization results")
    require(not backend_secret_values(mode, complete, legacy, required), "Backend secret verification failed")
    return verified


def initialize_backend_secrets(config, operation, revision, directory, approved):
    from azure.identity import AzureCliCredential
    from azure.keyvault.secrets import SecretClient
    from azure.core.exceptions import AzureError
    import psycopg
    from scripts.database_roles import database_access, isolated_postgres_environment, postgres_ca_bundle
    from scripts.schema_runtime import migration_template

    require(operation in {"plan", "execute"}, "Invalid secret initialization operation")
    database_access(config)
    azure = AzureCommands(config, directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope mismatch")
    group = config["target"]["resourceGroup"]
    output = azure.scoped(["deployment", "group", "show", "--resource-group", group, "--name", deployment_name(config, 5, "platform"), "--query", "{state:properties.provisioningState,platform:properties.outputs.platform.value}"])
    require(output.get("state") == "Succeeded" and output.get("platform", {}).get("stage5Deployed") is True, "Deploy Stage5 data infrastructure before initializing secrets")
    platform = output["platform"]
    require(UUID(platform["workloadIdentityClientId"]).int != 0, "Redeploy Stage5 to expose the backend Workload Identity client ID")
    require(platform["workloadIdentityPrincipalId"].lower() != config["databaseAccess"]["migrationPrincipalId"].lower(), "Backend pod identity must not be the privileged bootstrap identity")
    vault = azure.scoped(["keyvault", "show", "--resource-group", group, "--name", platform["keyVaultName"], "--query", "{id:id,uri:properties.vaultUri,rbac:properties.enableRbacAuthorization,public:properties.publicNetworkAccess,purge:properties.enablePurgeProtection}"])
    expected_vault = group_id(config) + "/providers/Microsoft.KeyVault/vaults/" + platform["keyVaultName"]
    require(vault["id"].lower() == expected_vault.lower() and vault["uri"].rstrip("/").lower() == f"https://{platform['keyVaultName']}.vault.azure.net".lower(), "Secret Vault must be the deployed backend Vault")
    require(vault.get("rbac") is True and vault.get("public") == "Disabled" and vault.get("purge") is True, "Backend Vault requires private RBAC and purge protection")
    server = azure.scoped(["postgres", "flexible-server", "show", "--resource-group", group, "--name", platform["postgresqlServerName"], "--query", "{host:fullyQualifiedDomainName,auth:authConfig,network:network}"])
    require(server["auth"].get("activeDirectoryAuth") == "Enabled" and server["auth"].get("passwordAuth") == "Disabled" and server["network"].get("publicNetworkAccess") == "Disabled", "Secret initialization lock requires the private Entra-only target database")
    database = config["parameters"]["platform"]["stage5Data"]["postgresqlDatabaseName"]
    migration_template(server["host"], database)
    token = subprocess.run(["az", "account", "get-access-token", "--subscription", config["azure"]["subscriptionId"], "--resource-type", "oss-rdbms", "--query", "accessToken", "--output", "tsv"], capture_output=True, text=True, check=False, timeout=120)
    require(token.returncode == 0 and token.stdout.strip(), "Unable to acquire the migration identity database token")
    mode = deployment_mode(config)
    required_secrets = backend_secrets(config)
    legacy_salt_source = os.environ.get("MIGRATION_LEGACY_SALT_SOURCE", "secret")
    credential = AzureCliCredential(tenant_id=config["azure"]["tenantId"])
    try:
        with isolated_postgres_environment(), psycopg.connect(host=server["host"], port=5432, dbname=database, user="llmgw_migrator", password=token.stdout.strip(), sslmode="verify-full", sslrootcert=postgres_ca_bundle(), connect_timeout=15, options="-c statement_timeout=30000") as lock, SecretClient(vault_url=vault["uri"], credential=credential) as client:
            with lock.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(hashtext(%s), 7)", (vault["id"].lower(),))
            legacy, source = read_legacy_keys(config, directory, legacy_salt_source) if mode == "migration" else (None, None)
            existing, metadata = inspect_backend_secrets(client, mode, required_secrets)
            with lock.cursor() as cursor:
                cursor.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema') AND table_type = 'BASE TABLE')")
                has_tables = cursor.fetchone()[0]
            ensure_key_creation_is_safe(mode, existing, has_tables, required_secrets)
            if mode == "migration":
                backend_secret_values(mode, existing, legacy, required_secrets)
            plan = {"stage": 5, "action": "backend-secrets", "revision": revision, "runtimeRevision": runtime_revision("azure", revision), "configSha256": stage_fingerprint(config, 5), "vaultId": vault["id"], "source": source, "before": metadata, "databaseHasTables": has_tables, "actions": {name: "keep" if name in existing else ("import" if mode == "migration" and name in BACKEND_SECRETS else "generate") for name in required_secrets}}
            digest = fingerprint(plan)
            summary = {"stage": 5, "action": "backend-secrets", "planSha256": digest, "stageAccepted": False}
            private_write(directory / "runtime-review.json", json.dumps(plan, indent=2) + "\n")
            private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
            if operation == "execute":
                require(approved == digest, "Backend secret plan changed or was not approved")
                versions = apply_backend_secrets(client, mode, existing, metadata, legacy, required_secrets)
                from scripts.backend_access import render_backend_access

                access = render_backend_access(config, platform, versions)
                private_write(directory / "backend-access.json", json.dumps(access, indent=2) + "\n")
                receipt = {"revision": revision, "runtimeRevision": plan["runtimeRevision"], "configSha256": stage_fingerprint(config, 5), "vaultId": vault["id"], "vaultName": platform["keyVaultName"], "secrets": versions, "verifiedAt": datetime.now(timezone.utc).isoformat()}
                document = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"backendSecrets": {"type": "object", "value": receipt}}}
                path = directory / "backend-secret-receipt.json"
                private_write(path, json.dumps(document))
                saved = azure.scoped(["deployment", "group", "create", "--resource-group", group, "--name", deployment_name(config, 5, "backend-secrets"), "--mode", "Incremental", "--template-file", str(path)])
                require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "Secrets initialized but saving the receipt failed; replan without rotating values")
                summary.update(initialized=True, versions=versions)
                private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
            print(json.dumps(summary))
            return summary
    except (AzureError, psycopg.Error):
        raise ValueError("Backend secret operation failed; verify private access and explicit bootstrap permissions. Existing secrets were not intentionally overwritten; replan before retrying.") from None
    finally:
        credential.close()