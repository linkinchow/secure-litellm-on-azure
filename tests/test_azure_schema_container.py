"""Real Prisma migrations over TLS on isolated PostgreSQL; no Azure authentication claim."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from scripts.customer_migration import ROOT, private_write
from scripts.database_roles import grant_roles
from tests.test_azure_postgresql import TEMPLATE


LITELLM_IMAGE = "docker.litellm.ai/berriai/litellm@sha256:6cc08e60be3e4ebf0cfb41ef23aed97e9ba4f61702d1e95e533f47ee6d67865f"
LEGACY_LITELLM_IMAGE = "docker.litellm.ai/berriai/litellm@sha256:af806882b7a6ced41658db5b6a7e98ed7b9b51d03b935e0417bf1c8552d688af"
POSTGRES_IMAGE = "postgres@sha256:e17e86066e5ef83e0952a9347f5c792b7ece00972e2aa787a6986f471b3dd3d5"


@unittest.skipUnless(os.environ.get("RUN_AZURE_SCHEMA_CONTAINER_TESTS") == "1", "Explicit isolated Prisma/PostgreSQL integration opt-in required")
class AzureSchemaContainerTests(unittest.TestCase):
    def docker(self, *arguments):
        result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=240)
        if result.returncode:
            self.fail("Container test failed: " + result.stderr[-3000:] + result.stdout[-1000:])
        return result.stdout.strip()

    def test_new_database_migration_then_application_read_only_startup(self):
        self.run_database_scenario()

    def test_legacy_195_restore_then_1104_upgrade_preserves_budget_and_source(self):
        self.run_database_scenario(legacy=True)

    def run_database_scenario(self, legacy=False):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            root = Path(directory)
            root.chmod(0o755)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.postgres.database.azure.com")])
            now = datetime.now(timezone.utc)
            cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=2)).add_extension(x509.SubjectAlternativeName([x509.DNSName("synthetic.postgres.database.azure.com")]), critical=False).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256())
            (root / "tls.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            (root / "tls.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            (root / "token").write_text("synthetic-migration-password")
            (root / "config.yaml").write_text("model_list:\n  - model_name: coding\n    litellm_params:\n      model: openai/gpt-4o-mini\n      api_base: http://127.0.0.1:9\n      api_key: synthetic-model-key\n      mock_response: SYNTHETIC_NATIVE_RESPONSE\ngeneral_settings:\n  disable_prisma_schema_update: true\n  disable_spend_logs: false\n  store_prompts_in_spend_logs: true\n  maximum_spend_logs_retention_period: 7d\n  maximum_spend_logs_retention_interval: 1d\n")
            (root / "backend").mkdir()
            (root / "backend/LITELLM_MASTER_KEY").write_text("synthetic-application-master-key")
            (root / "backend/LITELLM_SALT_KEY").write_text("synthetic-application-salt-key")
            (root / "backend/UI_PASSWORD").write_text("synthetic-ui-password")
            network = "llmgw-schema-" + uuid4().hex
            database_container = "llmgw-schema-" + uuid4().hex
            self.docker("network", "create", "--internal", network)
            try:
                startup = "cp /input/tls.crt /tmp/tls.crt && cp /input/tls.key /tmp/tls.key && chown postgres:postgres /tmp/tls.crt /tmp/tls.key && chmod 600 /tmp/tls.key && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/tls.crt -c ssl_key_file=/tmp/tls.key"
                self.docker("run", "--detach", "--name", database_container, "--network", network, "--network-alias", "synthetic.postgres.database.azure.com", "--env", "POSTGRES_PASSWORD=synthetic-admin-password", "--mount", f"type=bind,src={root},dst=/input,readonly", "--entrypoint", "sh", POSTGRES_IMAGE, "-c", startup)
                try:
                    inspect = json.loads(self.docker("inspect", database_container))[0]
                    self.assertFalse(inspect["HostConfig"]["PortBindings"])
                    address = inspect["NetworkSettings"]["Networks"][network]["IPAddress"]
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            admin = psycopg.connect(host=address, dbname="postgres", user="postgres", password="synthetic-admin-password", sslmode="require", connect_timeout=1, autocommit=True)
                            break
                        except psycopg.OperationalError:
                            if time.monotonic() > deadline:
                                self.fail("Isolated PostgreSQL startup failed")
                    with admin:
                        admin.execute("CREATE DATABASE litellm")
                        admin.execute("CREATE ROLE llmgw_migrator LOGIN PASSWORD 'synthetic-migration-password'")
                        admin.execute("CREATE ROLE llmgw_app LOGIN PASSWORD 'synthetic-application-password'")
                    with psycopg.connect(host=address, dbname="litellm", user="postgres", password="synthetic-admin-password", sslmode="require", row_factory=dict_row) as admin:
                        grant_roles(admin, [{"name": "llmgw_migrator"}, {"name": "llmgw_app"}], "litellm")
                    common = ["run", "--rm", "--network", network, "--read-only", "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,nosuid,size=256m", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "LITELLM_LOCAL_MODEL_COST_MAP=True", "--env", "HOME=/tmp", "--env", "AZURE_DATABASE_URL_TEMPLATE=" + TEMPLATE.replace("llmgw_app", "llmgw_migrator"), "--env", "LLMGW_DATABASE_TOKEN_FILE=/input/token", "--mount", f"type=bind,src={ROOT},dst=/workspace,readonly", "--mount", f"type=bind,src={root},dst=/input,readonly", "--mount", f"type=bind,src={root / 'tls.crt'},dst=/etc/ssl/certs/ca-certificates.crt,readonly", "--workdir", "/workspace", "--entrypoint", "/app/.venv/bin/python", LITELLM_IMAGE]
                    migration_started = time.monotonic()
                    source_history = None
                    if legacy:
                        with psycopg.connect(host=address, dbname="postgres", user="postgres", password="synthetic-admin-password", sslmode="require", autocommit=True) as admin:
                            admin.execute("CREATE DATABASE legacy")
                        with psycopg.connect(host=address, dbname="legacy", user="postgres", password="synthetic-admin-password", sslmode="require", row_factory=dict_row) as admin:
                            grant_roles(admin, [{"name": "llmgw_migrator"}, {"name": "llmgw_app"}], "legacy")
                        old_url = TEMPLATE.replace("llmgw_app", "llmgw_migrator:synthetic-migration-password").replace("/litellm?", "/legacy?")
                        old = [*common[:-1], "--env", "DATABASE_URL=" + old_url, LEGACY_LITELLM_IMAGE]
                        self.docker(*old, "-m", "prisma", "migrate", "deploy", "--schema", "/app/litellm-proxy-extras/litellm_proxy_extras/schema.prisma")
                        encryption_probe = "import base64; from litellm.proxy.common_utils.encrypt_decrypt_utils import encrypt_value; print(base64.b64encode(encrypt_value('SYNTHETIC_LEGACY_ENCRYPTED_VALUE', 'synthetic-application-salt-key')).decode())"
                        encrypted = self.docker(*old, "-c", encryption_probe)
                        with psycopg.connect(host=address, dbname="legacy", user="postgres", password="synthetic-admin-password", sslmode="require", row_factory=dict_row) as admin:
                            admin.execute('INSERT INTO "LiteLLM_UserTable" (user_id, user_role, max_budget, spend, models, metadata) VALUES (%s, %s, %s, %s, %s, %s)', ("synthetic-migration-user", "internal_user", 100.0, 12.5, ["coding"], Jsonb({"syntheticEncryptedValue": encrypted})))
                            source_history = admin.execute("SELECT count(*) AS total FROM _prisma_migrations").fetchone()["total"]
                        self.docker("exec", database_container, "pg_dump", "-U", "postgres", "-d", "legacy", "-Fc", "-f", "/tmp/legacy.dump")
                        self.docker("exec", database_container, "pg_restore", "-U", "postgres", "--role", "llmgw_migrator", "--no-owner", "--no-acl", "--exit-on-error", "--single-transaction", "-d", "litellm", "/tmp/legacy.dump")
                    mode = "migration" if legacy else "greenfield"
                    report = json.loads(self.docker(*common, "-m", "LiteLLM.runtime.schema_migration", "--mode", mode, "--operation", "inspect"))
                    self.assertTrue(report["pending"])
                    outcome = json.loads(self.docker(*common, "-m", "LiteLLM.runtime.schema_migration", "--mode", mode, "--operation", "execute", "--expected-state", report["stateSha256"]))
                    self.assertTrue(outcome["schemaVerified"])
                    self.assertFalse(outcome["stageAccepted"])
                    after = json.loads(self.docker(*common, "-m", "LiteLLM.runtime.schema_migration", "--mode", mode, "--operation", "inspect"))
                    self.assertFalse(after["pending"])
                    if legacy:
                        for database in ("legacy", "litellm"):
                            with psycopg.connect(host=address, dbname=database, user="postgres", password="synthetic-admin-password", sslmode="require", row_factory=dict_row) as admin:
                                record = admin.execute('SELECT user_role, max_budget, spend, models FROM "LiteLLM_UserTable" WHERE user_id = %s', ("synthetic-migration-user",)).fetchone()
                                self.assertEqual(record, {"user_role": "internal_user", "max_budget": 100.0, "spend": 12.5, "models": ["coding"]})
                                metadata = admin.execute('SELECT metadata FROM "LiteLLM_UserTable" WHERE user_id = %s', ("synthetic-migration-user",)).fetchone()["metadata"]
                                self.assertEqual(metadata["syntheticEncryptedValue"], encrypted)
                                history = admin.execute("SELECT count(*) AS total FROM _prisma_migrations").fetchone()["total"]
                                if database == "legacy":
                                    self.assertEqual(history, source_history)
                                else:
                                    self.assertGreater(history, source_history)
                        decryption_probe = "import base64,sys; from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value; assert decrypt_value(base64.b64decode(sys.argv[1]), 'synthetic-application-salt-key') == 'SYNTHETIC_LEGACY_ENCRYPTED_VALUE'; print('Synthetic restored ciphertext verified')"
                        self.docker(*common, "-c", decryption_probe, encrypted)
                        output = ROOT / "temp/legacy-upgrade"
                        output.mkdir(mode=0o700, exist_ok=True)
                        private_write(output / "report.json", json.dumps({"sourceImage": LEGACY_LITELLM_IMAGE, "targetImage": LITELLM_IMAGE, "databaseImage": POSTGRES_IMAGE, "sourceMigrations": source_history, "targetMigrations": len(after["assets"]["migrations"]), "syntheticBudgetPreserved": True, "syntheticCiphertextRecovered": True, "sourceDatabaseUnchanged": True, "durationSeconds": round(time.monotonic() - migration_started, 2), "customerRtoVerified": False, "stageAccepted": False}, indent=2))
                    self.docker(*common, "-m", "tests.azure_application_probe", "--config", "/input/config.yaml")
                    if os.environ.get("RUN_NATIVE_UI_BROWSER_TESTS") == "1":
                        application_name = "llmgw-ui-" + uuid4().hex
                        self.docker("run", "--detach", "--name", application_name, *common[1:], "-m", "tests.azure_application_probe", "--config", "/input/config.yaml", "--serve")
                        try:
                            application_ip = json.loads(self.docker("inspect", application_name))[0]["NetworkSettings"]["Networks"][network]["IPAddress"]
                            target = "http://" + application_ip + ":4000"
                            deadline = time.monotonic() + 45
                            while True:
                                try:
                                    with urlopen(target + "/health/liveliness", timeout=2) as response:
                                        self.assertEqual(response.status, 200)
                                    break
                                except (URLError, TimeoutError):
                                    self.assertLess(time.monotonic(), deadline, "Native UI application startup failed")
                                    time.sleep(0.2)
                            browser = subprocess.run(["node", "auth-proxy/test/native-ui-browser.mjs", target, str(root / "tls.crt"), str(root / "tls.key"), str(ROOT / "temp/native-ui-browser")], cwd=ROOT, capture_output=True, text=True, timeout=120)
                            self.assertEqual(browser.returncode, 0, browser.stderr[-3000:] + browser.stdout[-2000:])
                        finally:
                            self.docker("rm", "--force", application_name)
                finally:
                    self.docker("rm", "--force", "--volumes", database_container)
            finally:
                self.docker("network", "rm", network)