#!/usr/bin/env python3
"""Static safety checks for Stage 5 data platform assets."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "infra/modules"
STAGE5 = ROOT / "deploy/components/stage5-data"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def validate_infrastructure() -> None:
    key_vault = read(MODULES / "key-vault/main.bicep")
    assert "enablePurgeProtection: true" in key_vault
    assert "enableRbacAuthorization: true" in key_vault
    assert "publicNetworkAccess: 'Disabled'" in key_vault
    assert "softDeleteRetentionInDays: 90" in key_vault
    assert "4633458b-17de-408a-b874-0445c86b69e6" in key_vault
    assert "Microsoft.KeyVault/vaults/secrets" not in key_vault

    postgresql = read(MODULES / "postgresql-flexible-server/main.bicep")
    assert "version: '16'" in postgresql
    assert "passwordAuth: 'Disabled'" in postgresql
    assert "activeDirectoryAuth: 'Enabled'" in postgresql
    assert "publicNetworkAccess: 'Disabled'" in postgresql
    assert "backupRetentionDays: 14" in postgresql
    assert "autoGrow: 'Enabled'" in postgresql

    redis = read(MODULES / "managed-redis/main.bicep")
    assert "Balanced_B0" in redis
    assert "minimumTlsVersion: '1.2'" in redis
    # HA stays the default; Disabled is only an explicit, customer-approved cost trade-off.
    assert "param highAvailability string = 'Enabled'" in redis and "highAvailability: highAvailability" in redis
    assert "highAvailability: stage5Data.?redisHighAvailability ?? 'Enabled'" in read(ROOT / "infra/environments/main.bicep")
    assert "accessKeysAuthentication: 'Disabled'" in redis
    assert "clusteringPolicy: 'NoCluster'" in redis
    assert "accessPolicyAssignments@2025-07-01" in redis
    assert "category: 'AllMetrics'" in redis
    assert "category: 'ConnectionEvents'" in redis
    assert "categoryGroup: 'allLogs'" not in redis

    private_dns = read(MODULES / "private-dns/main.bicep")
    assert "resource postgresqlRunnerLink" in private_dns
    assert "resource managedRedisRunnerLink" in private_dns
    assert "registrationEnabled: false" in private_dns
    assert "id: runnerVirtualNetworkId" in private_dns

    expected_private_link = {
        "key-vault-private-endpoint/main.bicep": ("'vault'", "privatelink.vaultcore.azure.net"),
        "postgresql-private-endpoint/main.bicep": (
            "'postgresqlServer'",
            "privatelink.postgres.database.azure.com",
        ),
        "redis-private-endpoint/main.bicep": (
            "'redisEnterprise'",
            "privatelink.redis.azure.net",
        ),
    }
    for relative_path, expected in expected_private_link.items():
        content = read(MODULES / relative_path)
        for value in expected:
            assert value in content, f"{value} missing from {relative_path}"


def validate_secret_delivery() -> None:
    documents = [
        item
        for item in yaml.safe_load_all(read(STAGE5 / "secretproviderclass.yaml"))
        if item
    ]
    provider = documents[0]
    assert provider["kind"] == "SecretProviderClass"
    parameters = provider["spec"]["parameters"]
    assert parameters["clientID"] == "REPLACE_FROM_PROTECTED_DEPLOYMENT_OUTPUT"
    assert parameters["keyvaultName"] == "REPLACE_FROM_PROTECTED_DEPLOYMENT_OUTPUT"
    assert parameters["tenantId"] == "REPLACE_FROM_PROTECTED_DEPLOYMENT_OUTPUT"
    secret_data = provider["spec"]["secretObjects"][0]["data"]
    assert {entry["key"] for entry in secret_data} == {
        "LITELLM_MASTER_KEY",
        "LITELLM_SALT_KEY",
        "DATABASE_URL",
    }

    deployment = yaml.safe_load(read(STAGE5 / "deployment-patch.yaml"))
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["REDIS_PORT"] == "10000"
    assert env["REDIS_USERNAME"] == "REPLACE_WITH_WORKLOAD_IDENTITY_OBJECT_ID"
    assert container["volumeMounts"][0]["readOnly"] is True

    config = yaml.safe_load(read(STAGE5 / "config-patch.yaml"))
    router = yaml.safe_load(config["data"]["config.yaml"])["router_settings"]
    assert router["cache_kwargs"]["azure_redis_ad_token"] is True
    assert router["cache_kwargs"]["ssl"] is True
    assert router["cache_kwargs"]["ssl_check_hostname"] is True


def validate_no_committed_secrets() -> None:
    text = "\n".join(read(path) for path in STAGE5.rglob("*.yaml"))
    forbidden = ("postgresql://", "redis://", "rediss://", "sk-")
    for marker in forbidden:
        assert marker not in text, f"committed credential marker found: {marker}"


if __name__ == "__main__":
    validate_infrastructure()
    validate_secret_delivery()
    validate_no_committed_secrets()
    print("Stage 5 static safety checks passed.")
