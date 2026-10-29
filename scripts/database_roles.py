"""Provision bounded Entra database roles on the deployed target PostgreSQL server."""

import ipaddress
import json
import os
import re
import socket
import subprocess
from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from scripts.customer_migration import MigrationError, fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands, deployment_name, group_id


POSTGRES_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


@contextmanager
def isolated_postgres_environment():
    inherited = {key: os.environ.pop(key) for key in tuple(os.environ) if key.startswith("PG")}
    try:
        yield
    finally:
        os.environ.update(inherited)


def postgres_ca_bundle(path=POSTGRES_CA_BUNDLE):
    require(os.path.isfile(path), "PostgreSQL system CA bundle is missing from the Runner")
    return path


def database_access(config):
    access = config.get("databaseAccess", {})
    require(isinstance(access, dict) and set(access) == {"migrationPrincipalId"}, "databaseAccess requires the migration service principal Object ID")
    require(isinstance(access["migrationPrincipalId"], str) and UUID(access["migrationPrincipalId"]).int != 0, "Invalid migration service principal Object ID")
    return access


def require_private_postgres_dns(host, resolve=socket.getaddrinfo):
    try:
        addresses = {item[4][0] for item in resolve(host, 5432, type=socket.SOCK_STREAM)}
    except OSError:
        raise MigrationError("PostgreSQL private hostname does not resolve from this Runner; deploy the Stage 5 Runner private DNS links") from None
    require(addresses and all(ipaddress.ip_address(address).version == 4 and ipaddress.ip_address(address).is_private for address in addresses), "PostgreSQL hostname must resolve only to private endpoint IPv4 addresses from this Runner; deploy the Stage 5 Runner private DNS links")
    return sorted(addresses)


def role_contract(config, application):
    migration_id = str(UUID(database_access(config)["migrationPrincipalId"]))
    application_id = str(UUID(application["principalId"]))
    require(UUID(application_id).int != 0, "Application identity Object ID must be nonzero")
    require(migration_id != application_id, "Database migration and application identities must be separate")
    return [
        {"name": "llmgw_migrator", "objectId": migration_id, "tenantId": config["azure"]["tenantId"], "kind": "service"},
        {"name": "llmgw_app", "objectId": application_id, "tenantId": config["azure"]["tenantId"], "kind": "service"},
    ]


def inspect_roles(connection, roles):
    result = []
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolname, principaltype, objectid, tenantid, isadmin FROM pg_catalog.pgaadauth_list_principals(false)")
        normalized = [{key.lower(): value for key, value in row.items()} for row in cursor.fetchall()]
        mapped = {row["rolname"]: row for row in normalized}
        for role in roles:
            cursor.execute("SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolcanlogin FROM pg_catalog.pg_roles WHERE rolname = %s", (role["name"],))
            existing = cursor.fetchone()
            mapping = mapped.get(role["name"])
            if existing:
                require(mapping is not None, "Refusing to adopt an existing password or unmapped database role")
                require(str(mapping["objectid"]).lower() == role["objectId"].lower() and str(mapping["tenantid"]).lower() == role["tenantId"].lower() and mapping["principaltype"] == "service" and mapping["isadmin"] == 0, "Existing Entra database role mapping differs from the approved identity")
                require(not any(existing[key] for key in ("rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication", "rolbypassrls")) and existing["rolcanlogin"], "Existing database role has unexpected privileges")
                cursor.execute("SELECT parent.rolname FROM pg_catalog.pg_auth_members membership JOIN pg_catalog.pg_roles parent ON parent.oid = membership.roleid JOIN pg_catalog.pg_roles member ON member.oid = membership.member WHERE member.rolname = %s ORDER BY parent.rolname", (role["name"],))
                require(not cursor.fetchall(), "Managed database roles must not inherit other roles")
            result.append({"name": role["name"], "existing": existing, "mapping": mapping})
    return result


def inspect_schema(connection, administrator=None):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_userbyid(namespace.nspowner) AS owner, pg_get_userbyid(database.datdba) AS database_owner, namespace.nspacl::text AS acl FROM pg_catalog.pg_namespace namespace JOIN pg_catalog.pg_database database ON database.datname = current_database() WHERE namespace.nspname = 'public'")
        schema = cursor.fetchone()
        # ARM creates the database as azure_pg_admin before an Entra administrator exists; a database recreated
        # afterwards (final cutover onto an emptied target) is owned by that configured administrator instead.
        initial_owners = {"azure_pg_admin", administrator} if administrator else {"azure_pg_admin"}
        approved_owner = schema is not None and (schema["owner"] in {"pg_database_owner", "llmgw_migrator"} or (schema["owner"] == "azure_pg_admin" and schema["database_owner"] in initial_owners))
        require(approved_owner, "Public schema must be owned by the Azure database owner or the managed migration role")
        cursor.execute("SELECT pg_get_userbyid(relowner) AS owner, count(*) AS count FROM pg_catalog.pg_class relation JOIN pg_catalog.pg_namespace namespace ON namespace.oid = relation.relnamespace WHERE namespace.nspname = 'public' AND relation.relkind IN ('r','p','v','m','S','f') GROUP BY relowner ORDER BY owner")
        owners = cursor.fetchall()
        require(all(row["owner"] == "llmgw_migrator" for row in owners), "Existing public objects are not owned by the migration role; refusing automatic ownership takeover")
        cursor.execute("SELECT datacl::text AS acl FROM pg_catalog.pg_database WHERE datname = current_database()")
        database_acl = cursor.fetchone()
        cursor.execute("SELECT pg_get_userbyid(defaclrole) AS owner, defaclobjtype AS kind, defaclacl::text AS acl FROM pg_catalog.pg_default_acl WHERE defaclnamespace = 'public'::regnamespace ORDER BY owner, kind")
        defaults = cursor.fetchall()
    return {"schema": schema, "owners": owners, "databaseAcl": database_acl, "defaultAcl": defaults}


def create_roles(connection, roles, before):
    with connection.cursor() as cursor:
        for role, observed in zip(roles, before):
            if not observed["existing"]:
                cursor.execute("SELECT * FROM pg_catalog.pgaadauth_create_principal_with_oid(%s, %s, 'service', false, false)", (role["name"], role["objectId"]))


def grant_roles(connection, roles, database):
    with connection.cursor() as cursor:
        migrator, application = (sql.Identifier(role["name"]) for role in roles)
        cursor.execute(sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(sql.Identifier(database), migrator))
        cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), application))
        cursor.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(migrator))
        cursor.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(migrator))
        cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cursor.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(migrator))
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(application))
        cursor.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(application))
        cursor.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(application))
        cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(migrator, application))
        cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {}").format(migrator, application))
        cursor.execute("SELECT to_regclass('public._prisma_migrations') AS migration_table")
        if cursor.fetchone()["migration_table"] is not None:
            cursor.execute(sql.SQL("REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE public._prisma_migrations FROM {}").format(application))


def verify_grants(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT has_database_privilege('llmgw_migrator', current_database(), 'CONNECT, CREATE') AS migration_database, has_schema_privilege('llmgw_migrator', 'public', 'USAGE, CREATE') AS migration_schema, has_database_privilege('llmgw_app', current_database(), 'CONNECT') AS application_connect, has_schema_privilege('llmgw_app', 'public', 'USAGE') AS application_schema, has_database_privilege('llmgw_app', current_database(), 'CREATE') AS application_create_database, has_schema_privilege('llmgw_app', 'public', 'CREATE') AS application_create_schema")
        privileges = cursor.fetchone()
        require(all(privileges[key] for key in ("migration_database", "migration_schema", "application_connect", "application_schema")) and not privileges["application_create_database"] and not privileges["application_create_schema"], "Database role privileges did not match the DDL/DML separation contract")


def provision_database_roles(config, operation, revision, directory, approved):
    require(operation in {"plan", "execute"}, "Invalid database role operation")
    database_access(config)
    azure = AzureCommands(config, directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope mismatch")
    group = config["target"]["resourceGroup"]
    output = azure.scoped(["deployment", "group", "show", "--resource-group", group, "--name", deployment_name(config, 5, "platform"), "--query", "{state:properties.provisioningState,platform:properties.outputs.platform.value}"])
    require(output.get("state") == "Succeeded" and output.get("platform", {}).get("stage5Deployed") is True, "Deploy Stage 5 before provisioning database roles")
    platform = output["platform"]
    server = azure.scoped(["postgres", "flexible-server", "show", "--resource-group", group, "--name", platform["postgresqlServerName"], "--query", "{id:id,host:fullyQualifiedDomainName,auth:authConfig,network:network}"])
    require(server["id"].lower() == (group_id(config) + "/providers/Microsoft.DBforPostgreSQL/flexibleServers/" + platform["postgresqlServerName"]).lower(), "Database server outside target scope")
    require(server["auth"].get("activeDirectoryAuth") == "Enabled" and server["auth"].get("passwordAuth") == "Disabled" and server["network"].get("publicNetworkAccess") == "Disabled", "Database role setup requires private Entra-only PostgreSQL")
    application = azure.scoped(["identity", "show", "--resource-group", group, "--name", platform["workloadIdentityName"], "--query", "{id:id,principalId:principalId,clientId:clientId}"])
    require(application["id"].lower().startswith(group_id(config).lower() + "/providers/microsoft.managedidentity/userassignedidentities/"), "Application identity outside target scope")
    roles = role_contract(config, application)
    data = config["parameters"]["platform"]["stage5Data"]
    require(str(data["postgresqlEntraAdministratorObjectId"]).lower() not in {role["objectId"] for role in roles}, "Do not use the database administrator as migration or application identity")
    database = data["postgresqlDatabaseName"]
    require(re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", database) is not None, "Invalid target database name")
    require_private_postgres_dns(server["host"])
    token = subprocess.run(["az", "account", "get-access-token", "--subscription", config["azure"]["subscriptionId"], "--resource-type", "oss-rdbms", "--query", "accessToken", "--output", "tsv"], capture_output=True, text=True, check=False, timeout=120)
    require(token.returncode == 0 and token.stdout.strip(), "Unable to obtain database administrator token")
    try:
        connection_options = {"host": server["host"], "port": 5432, "user": data["postgresqlEntraAdministratorPrincipalName"], "password": token.stdout.strip(), "sslmode": "verify-full", "sslrootcert": postgres_ca_bundle(), "connect_timeout": 15, "options": "-c statement_timeout=30000 -c lock_timeout=5000", "row_factory": dict_row}
        with isolated_postgres_environment(), psycopg.connect(dbname="postgres", **connection_options) as control, psycopg.connect(dbname=database, **connection_options) as connection:
            with control.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(193701, 5)")
            before = {"roles": inspect_roles(control, roles), **inspect_schema(connection, data["postgresqlEntraAdministratorPrincipalName"])}
            plan = {"stage": 5, "action": "database-roles", "revision": revision, "configSha256": stage_fingerprint(config, 5), "serverId": server["id"], "database": database, "roles": roles, "before": before, "grants": {"llmgw_migrator": "CONNECT, CREATE database; own public schema with USAGE, CREATE; administrator receives migration-role membership", "llmgw_app": "CONNECT; public schema USAGE; table DML; sequence USAGE, SELECT; future migrator objects", "public": "transfer owner to llmgw_migrator and revoke CREATE from PUBLIC"}}
            digest = fingerprint(plan)
            summary = {"stage": 5, "action": "database-roles", "planSha256": digest, "stageAccepted": False}
            private_write(directory / "runtime-review.json", json.dumps(plan, indent=2) + "\n")
            private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
            if operation == "execute":
                require(digest == approved, "Database role plan changed or was not approved")
                create_roles(control, roles, before["roles"])
                control.commit()
                grant_roles(connection, roles, database)
                inspect_roles(control, roles)
                inspect_schema(connection, data["postgresqlEntraAdministratorPrincipalName"])
                verify_grants(connection)
            else:
                connection.rollback()
                control.rollback()
        if operation == "execute":
            summary["applied"] = True
            private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary))
        return summary
    except psycopg.OperationalError:
        raise MigrationError("PostgreSQL connection failed after private DNS validation; verify Runner TCP/5432, TLS trust, and the configured Entra administrator identity") from None
    except psycopg.Error:
        if operation == "plan":
            raise MigrationError("PostgreSQL role plan could not inspect the target database; no database changes were made") from None
        raise MigrationError("Database role execution failed; mapped roles may remain if target grants failed. Replan before retrying; no acceptance issued.") from None