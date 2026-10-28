"""Run approved upstream Prisma migrations separately from the application process."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from contextlib import contextmanager, redirect_stdout
from urllib.parse import quote, urlsplit, urlunsplit

from LiteLLM.runtime.application import check_schema
from LiteLLM.runtime.azure_postgresql import DatabaseAuthError, database_url_template


SCHEMA_PATH = Path("/app/litellm-proxy-extras/litellm_proxy_extras/schema.prisma")
SCHEMA_SHA256 = "dbb02f20858ed397ea83b0dfabe2d3cfe59ef658976fdcde373c5baad33df387"
VIEWS_SHA256 = "6118f5eba52c8e3a56accc8973b80b44d53e87f90e7d578ca14be0e8b39c66ad"
# SHA256 of ordered, active {name, sha256} rows from reviewed source databases.
SUPPORTED_SOURCE_HISTORIES = {
    "0a730273fd745521b36008b01816142d92294792667a6a5c046147e0821e0dbb": 141,
    "3263a9bd2b458c8e78fa35af9378a21500bb11b7fdfdff74970a5a2bd3973958": 141,
    # limx legacy (litellm-mi-aks, 2026-10-28 read-only export): the same 141 rows as upstream v1.95.0, but upgraded in
    # three steps, so 20260630120000_add_token_exchange_to_mcp_servers was applied after 20260630190000.
    "f88b79a6d4fc17bc7f199d691c629744c43811e43538de39926fa4ba67a89871": 141,
}
OPERATOR_MANAGED_SPEND_LOG_INDEXES = {
    "LiteLLM_SpendLogs_litellm_call_id_idx": ("litellm_call_id",),
    "LiteLLM_SpendLogs_api_key_startTime_idx": ("api_key", "startTime"),
}
CREATE_SPEND_LOG_INDEX = re.compile(
    r'^CREATE\s+INDEX\s+"(?P<index>[^"]+)"\s+'
    r'ON\s+(?:"public"\.)?"LiteLLM_SpendLogs"\s*\((?P<columns>[^)]*)\)\s*$',
)


@contextmanager
def engine_diagnostics():
    sys.stdout.flush()
    original = os.dup(1)
    try:
        os.dup2(2, 1)
        with redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stderr.flush()
        os.dup2(original, 1)
        os.close(original)


def migration_assets():
    from litellm.proxy.db import create_views

    if hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest() != SCHEMA_SHA256 or hashlib.sha256(Path(create_views.__file__).read_bytes()).hexdigest() != VIEWS_SHA256:
        raise DatabaseAuthError("Schema or view code differs from the approved LiteLLM image")
    files = sorted(SCHEMA_PATH.parent.glob("migrations/*/migration.sql"))
    if not files:
        raise DatabaseAuthError("Approved migration files are missing")
    runtime = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(Path(__file__).parent.glob("*.py"))}
    return {"schemaSha256": SCHEMA_SHA256, "viewsSha256": VIEWS_SHA256, "runtimeCode": runtime, "migrations": [{"name": path.parent.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in files]}


def check_history(assets, history, tables, mode):
    expected = {item["name"]: item["sha256"] for item in assets["migrations"]}
    active = []
    for item in history:
        if item.get("rolledBack"):
            continue
        if not item.get("finished"):
            raise DatabaseAuthError("Unfinished migration requires investigation; automatic resolve/reset is forbidden")
        active.append(item)
    ordered = [item["name"] for item in assets["migrations"]]
    if mode == "migration":
        for source_hash, count in SUPPORTED_SOURCE_HISTORIES.items():
            if len(active) < count:
                continue
            source = [{"name": item["name"], "sha256": item["checksum"]} for item in active[:count]]
            digest = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if digest == source_hash:
                inherited = [item["name"] for item in active[:count]]
                if len(set(inherited)) != len(inherited) or any(name not in expected for name in inherited):
                    raise DatabaseAuthError("Approved source migration history contains unsupported names")
                remaining = [name for name in ordered if name not in inherited]
                tail = active[count:]
                for index, item in enumerate(tail):
                    if index >= len(remaining) or item["name"] != remaining[index] or item["checksum"] != expected.get(item["name"]):
                        raise DatabaseAuthError("Database migrations after the approved source history differ from the target image")
                pending = remaining[len(tail):]
                break
        else:
            pending = None
    else:
        pending = None
    if pending is None:
        finished = []
        for item in active:
            if item["name"] not in expected or item["checksum"] != expected[item["name"]] or item["name"] in finished:
                raise DatabaseAuthError("Database migration history differs from the approved image")
            finished.append(item["name"])
        if finished != ordered[:len(finished)]:
            raise DatabaseAuthError("Migration history must be an ordered prefix of the approved release or a verified source upgrade")
        pending = ordered[len(finished):]
    if tables and not active:
        raise DatabaseAuthError("Existing database has no compatible migration history; automatic baseline is forbidden")
    if mode == "migration" and not tables:
        raise DatabaseAuthError("Restore and verify the legacy database before migrating its schema")
    if mode not in {"migration", "greenfield"}:
        raise DatabaseAuthError("Unknown deployment mode")
    return pending


async def database_state(client):
    tables = await client.query_raw("SELECT tablename AS name FROM pg_catalog.pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    names = [row["name"] for row in tables]
    history = []
    if "_prisma_migrations" in names:
        history = await client.query_raw('SELECT migration_name AS name, checksum, finished_at IS NOT NULL AS finished, rolled_back_at IS NOT NULL AS "rolledBack" FROM public._prisma_migrations ORDER BY started_at, migration_name')
    return {"tables": [name for name in names if name != "_prisma_migrations"], "history": history}


def state_fingerprint(assets, state):
    return hashlib.sha256(json.dumps({"assets": assets, "state": state}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def unapproved_schema_diff(diff_sql):
    remaining = []
    for statement in diff_sql.split(";"):
        bare = "\n".join(line for line in statement.splitlines() if line.strip() and not line.strip().startswith("--")).strip()
        if not bare:
            continue
        match = CREATE_SPEND_LOG_INDEX.match(bare)
        columns = tuple(column.strip().strip('"') for column in match["columns"].split(",")) if match else ()
        if match and OPERATOR_MANAGED_SPEND_LOG_INDEXES.get(match["index"]) == columns:
            continue
        remaining.append(bare)
    return ";\n".join(remaining)


async def verify_database_schema():
    process = await asyncio.create_subprocess_exec("/app/.venv/bin/prisma", "migrate", "diff", "--from-schema-datasource", str(SCHEMA_PATH), "--to-schema-datamodel", str(SCHEMA_PATH), "--script", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, errors = await asyncio.wait_for(process.communicate(), timeout=120)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0:
        raise DatabaseAuthError("Read-only database schema verification failed")
    if unapproved_schema_diff(output.decode(errors="replace")):
        raise DatabaseAuthError("Database schema differs from the approved datamodel after migration; no automatic db push or data-loss repair is permitted")


async def execute_schema(operation, expected_state, mode):
    from prisma import Prisma
    from litellm.proxy.db.create_views import create_missing_views

    assets = migration_assets()
    client = Prisma(datasource={"url": os.environ["DATABASE_URL"]})
    try:
        await client.connect()
        await client.query_raw("SELECT pg_advisory_lock(193701, 6)::text")
        state = await database_state(client)
        pending = check_history(assets, state["history"], state["tables"], mode)
        digest = state_fingerprint(assets, state)
        if operation == "inspect":
            return {"assets": assets, "state": state, "pending": pending, "stateSha256": digest}
        if expected_state != digest:
            raise DatabaseAuthError("Database schema state changed after plan approval")
        process = await asyncio.create_subprocess_exec("/app/.venv/bin/prisma", "migrate", "deploy", "--schema", str(SCHEMA_PATH), cwd=str(SCHEMA_PATH.parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, errors = await asyncio.wait_for(process.communicate(), timeout=1800)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode != 0:
            codes = sorted(set(re.findall(r"\bP[0-9]{4}\b", (output + errors).decode(errors="replace"))))
            raise DatabaseAuthError("Prisma migrate deploy failed " + ",".join(codes) + "; database was not reset or automatically resolved")
        await create_missing_views(client)
        await client.execute_raw("REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE public._prisma_migrations FROM llmgw_app")
        await verify_database_schema()
        await check_schema(client)
        after = await database_state(client)
        if check_history(assets, after["history"], after["tables"], mode):
            raise DatabaseAuthError("Migration verification found pending migrations")
        return {"schemaVerified": True, "assets": assets, "stateSha256": state_fingerprint(assets, after), "stageAccepted": False}
    finally:
        if client.is_connected():
            await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("assets", "inspect", "execute"), required=True)
    parser.add_argument("--expected-state", default="")
    parser.add_argument("--mode", choices=("migration", "greenfield"), default="greenfield")
    args = parser.parse_args()
    try:
        with engine_diagnostics():
            if args.operation == "assets":
                result = migration_assets()
            else:
                parsed, query = database_url_template(os.environ["AZURE_DATABASE_URL_TEMPLATE"])
                if parsed.username != "llmgw_migrator":
                    raise DatabaseAuthError("Schema operations require the dedicated migration role")
                token = Path(os.environ["LLMGW_DATABASE_TOKEN_FILE"]).read_text().strip()
                if not token:
                    raise DatabaseAuthError("Database token is empty")
                authority = f"{parsed.username}:{quote(token, safe='')}@{parsed.hostname}:5432"
                os.environ["DATABASE_URL"] = urlunsplit(("postgresql", authority, parsed.path, query, ""))
                os.environ.pop("DIRECT_URL", None)
                result = asyncio.run(execute_schema(args.operation, args.expected_state, args.mode))
        print(json.dumps(result, sort_keys=True))
    except DatabaseAuthError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("Schema operation failed. Review migration history, target permissions and private connectivity; no reset or automatic baseline was performed.") from None


if __name__ == "__main__":
    main()