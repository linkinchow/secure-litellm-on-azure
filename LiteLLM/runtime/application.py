"""ASGI entry point for the reviewed Azure single-writer LiteLLM deployment."""

import argparse
from contextlib import asynccontextmanager
import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import re

import yaml

from LiteLLM.runtime.azure_postgresql import DatabaseAuthError, database_url_template, install_prisma_adapter, workload_identity_tokens


UTILS_SHA256 = "da34d8122f16023f935fa2389c51007aca7238dd48c77b271ca2947e576c4be0"
SERVER_SHA256 = "926644751f749b40eb579124b9efb1d783c1a674f382baab668b90bff027ecc6"
REQUIRED_VIEWS = ("LiteLLM_VerificationTokenView", "MonthlyGlobalSpend", "Last30dKeysBySpend", "Last30dModelsBySpend", "MonthlyGlobalSpendPerKey", "MonthlyGlobalSpendPerUserPerKey", "Last30dTopEndUsersSpend", "DailyTagSpend")
REQUIRED_TABLES = ("LiteLLM_VerificationToken", "LiteLLM_TeamTable", "LiteLLM_UserTable", "LiteLLM_SpendLogs", "_prisma_migrations")


def trusted_proxy_cidrs(value):
    if not isinstance(value, str) or not value.strip():
        raise DatabaseAuthError("Trusted ingress proxy CIDR is missing")
    private_ranges = tuple(ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    try:
        networks = [ipaddress.ip_network(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError:
        raise DatabaseAuthError("Trusted ingress proxy CIDR is invalid") from None
    if not networks or any(network.version != 4 or not any(network.subnet_of(parent) for parent in private_ranges) for network in networks):
        raise DatabaseAuthError("Trusted ingress proxies must use explicit RFC1918 IPv4 CIDRs")
    return [str(network) for network in networks]


def proxy_server_settings(value):
    return {"proxy_headers": True, "forwarded_allow_ips": trusted_proxy_cidrs(value)}


def load_backend_keys(directory):
    values = {}
    names = ["LITELLM_MASTER_KEY", "LITELLM_SALT_KEY"]
    if os.environ.get("LLMGW_GATEWAY_AUTH_MODE") == "native":
        names.append("UI_PASSWORD")
    for name in names:
        value = (Path(directory) / name).read_bytes().decode("utf-8")
        if not value.strip() or "\x00" in value or len(value.encode()) > 25000:
            raise DatabaseAuthError("Backend CSI key is missing or invalid")
        if name in os.environ and not hmac.compare_digest(os.environ[name].encode(), value.encode()):
            raise DatabaseAuthError("Mounted backend key conflicts with an existing environment value")
        values[name] = value
    os.environ.update(values)
    return values


def configure_gateway_authentication(values):
    mode = os.environ.get("LLMGW_GATEWAY_AUTH_MODE", "entra")
    if mode == "entra":
        if any(os.environ.get(name) for name in ("LLMGW_NATIVE_ADMIN_USERNAME", "UI_USERNAME", "UI_PASSWORD")):
            raise DatabaseAuthError("Native UI credentials conflict with Entra gateway authentication")
        return
    if mode != "native":
        raise DatabaseAuthError("Unsupported gateway authentication mode")
    username = os.environ.get("LLMGW_NATIVE_ADMIN_USERNAME", "")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{2,127}", username) is None:
        raise DatabaseAuthError("Native admin username is missing or invalid")
    password = values.get("UI_PASSWORD")
    if not password:
        raise DatabaseAuthError("Native admin login requires the dedicated mounted UI password")
    if os.environ.get("UI_USERNAME") not in {None, username} or os.environ.get("UI_PASSWORD") not in {None, password}:
        raise DatabaseAuthError("Native UI credentials conflict with the reviewed gateway configuration")
    os.environ.update(UI_USERNAME=username, UI_PASSWORD=password)


def application_config(path):
    value = yaml.safe_load(Path(path).read_text())
    if not isinstance(value, dict) or "environment_variables" in value:
        raise DatabaseAuthError("Application config must not override the protected runtime environment")
    settings = value.get("general_settings", {})
    if not isinstance(settings, dict) or settings.get("disable_prisma_schema_update") is not True:
        raise DatabaseAuthError("Application config must explicitly disable Prisma schema updates")
    if any(key in settings for key in ("database_url", "database_url_read_replica", "direct_url")):
        raise DatabaseAuthError("Database targets are controlled by the Azure runtime, not application YAML")
    return value


async def check_schema(client):
    tables = await client.query_raw("SELECT tablename AS name FROM pg_catalog.pg_tables WHERE schemaname = 'public'")
    views = await client.query_raw("SELECT viewname AS name FROM pg_catalog.pg_views WHERE schemaname = 'public'")
    if not set(REQUIRED_TABLES).issubset({row["name"] for row in tables}) or not set(REQUIRED_VIEWS).issubset({row["name"] for row in views}):
        raise DatabaseAuthError("Required database schema or views are missing; run the approved schema migration before starting the application")
    failed = await client.query_raw("SELECT count(*) AS count FROM public._prisma_migrations WHERE finished_at IS NULL AND rolled_back_at IS NULL")
    if len(failed) != 1 or failed[0]["count"] != 0:
        raise DatabaseAuthError("Database has unfinished migrations; application startup is blocked")


async def check_application_role(client):
    rights = await client.query_raw("SELECT current_user AS name, has_schema_privilege(current_user, 'public', 'CREATE') AS ddl, has_table_privilege(current_user, 'public._prisma_migrations', 'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') AS migration_write")
    if len(rights) != 1 or rights[0]["name"] != "llmgw_app" or rights[0]["ddl"] or rights[0]["migration_write"]:
        raise DatabaseAuthError("Application role must have neither schema DDL nor migration-history write privileges")


def create_application(config_path, template, tokens=None):
    application_config(config_path)
    values = {}
    if os.environ.get("LLMGW_BACKEND_SECRETS_DIR"):
        values = load_backend_keys(os.environ["LLMGW_BACKEND_SECRETS_DIR"])
    configure_gateway_authentication(values)
    parsed, _query = database_url_template(template)
    if parsed.username != "llmgw_app":
        raise DatabaseAuthError("Application process must use the DML-only llmgw_app role")
    provider = tokens or workload_identity_tokens(template)
    install_prisma_adapter(provider)
    os.environ.update(DISABLE_SCHEMA_UPDATE="true", DATABASE_SCHEMA="public", CONFIG_FILE_PATH=str(Path(config_path).resolve()), WORKER_CONFIG=str(Path(config_path).resolve()))
    from litellm.proxy import utils

    if hashlib.sha256(Path(utils.__file__).read_bytes()).hexdigest() != UTILS_SHA256:
        raise DatabaseAuthError("Proxy database utilities differ from the reviewed image")

    async def read_only_views(self):
        await check_schema(self.db)

    utils.PrismaClient.check_view_exists = read_only_views
    from litellm.proxy import proxy_server

    if hashlib.sha256(Path(proxy_server.__file__).read_bytes()).hexdigest() != SERVER_SHA256:
        raise DatabaseAuthError("Proxy lifecycle differs from the reviewed image")
    app = proxy_server.app
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        from prisma import Prisma

        probe = Prisma(datasource={"url": os.environ["DATABASE_URL"]})
        try:
            await probe.connect()
            await check_schema(probe)
            await check_application_role(probe)
        finally:
            if probe.is_connected():
                await probe.disconnect()
        async with original_lifespan(application):
            if proxy_server.prisma_client is None:
                raise DatabaseAuthError("Proxy did not establish its Azure database connection")
            yield

    app.router.lifespan_context = lifespan
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--port", type=int, choices=(4000,), default=4000)
    args = parser.parse_args()
    try:
        app = create_application(args.config, os.environ["AZURE_DATABASE_URL_TEMPLATE"])
        proxy_settings = proxy_server_settings(os.environ.get("LLMGW_TRUSTED_PROXY_CIDRS"))
    except Exception:
        raise SystemExit("Azure application initialization failed; check approved configuration, identity and schema. Sensitive values are suppressed.") from None
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=4000, workers=1, access_log=False, **proxy_settings)


if __name__ == "__main__":
    main()