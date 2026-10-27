import base64
import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from scripts.certificate_pfx import MANAGED_BY, pfx_from_pem, pfx_leaf_sha256, sync_edge_certificates
from scripts.customer_migration import ROOT, MigrationError
from tests.test_app_gateway import app_gateway_customer


def certificate_pem(host):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
                   .not_valid_after(now + datetime.timedelta(days=60))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .sign(key, hashes.SHA256()))
    return (certificate.public_bytes(serialization.Encoding.PEM)
            + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())).decode()


class FakeVault:
    def __init__(self, secrets):
        self.secrets = {name: [value] for name, value in secrets.items()}
        self.writes = []

    def _secret(self, name, index):
        value, tags, content_type = self.secrets[name][index]
        return SimpleNamespace(value=value, id=f"https://vault/secrets/{name}/{index}", properties=SimpleNamespace(version=str(index), enabled=True, tags=tags, content_type=content_type))

    def get_secret(self, name, version=None):
        from azure.core.exceptions import ResourceNotFoundError
        if name not in self.secrets:
            raise ResourceNotFoundError("missing")
        return self._secret(name, len(self.secrets[name]) - 1 if version is None else int(version))

    def set_secret(self, name, value, content_type=None, tags=None):
        self.secrets.setdefault(name, []).append((value, tags, content_type))
        self.writes.append(name)
        return self._secret(name, len(self.secrets[name]) - 1)

    def close(self):
        pass


class CertificatePfxTests(unittest.TestCase):
    def test_pfx_round_trip_keeps_leaf_and_needs_no_password(self):
        pem = certificate_pem("llm-api.example.com")
        value, sha256 = pfx_from_pem(pem, "llm-api.example.com")
        key, certificate, _chain = pkcs12.load_key_and_certificates(base64.b64decode(value), None)
        self.assertIsNotNone(key)
        self.assertEqual(pfx_leaf_sha256(value), sha256)
        self.assertEqual(certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value, "llm-api.example.com")
        with self.assertRaises(Exception):
            pfx_from_pem(pem, "llm-admin.example.com")

    def test_sync_writes_then_keeps_and_refuses_unmanaged_or_changed_secrets(self):
        config = app_gateway_customer()
        hosts = {plane: f"llm-{plane}.{config['baseDomain']}" for plane in ("api", "admin")}
        vault = FakeVault({f"{plane}-tls": (certificate_pem(host), {}, "application/x-pem-file") for plane, host in hosts.items()})
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder:
            directory = Path(folder)
            plan = sync_edge_certificates(config, "plan", "a" * 40, directory, "", vault_factory=lambda: vault)
            self.assertEqual(plan["operations"], {"api": "write", "admin": "write"})
            self.assertEqual(vault.writes, [])
            with self.assertRaisesRegex(MigrationError, "plan changed"):
                sync_edge_certificates(config, "execute", "a" * 40, directory, "0" * 64, vault_factory=lambda: vault)
            result = sync_edge_certificates(config, "execute", "a" * 40, directory, plan["planSha256"], vault_factory=lambda: vault)
            self.assertTrue(result["applied"])
            self.assertEqual(sorted(vault.writes), ["admin-tls-pfx", "api-tls-pfx"])
            stored = vault.get_secret("api-tls-pfx")
            self.assertEqual(stored.properties.content_type, "application/x-pkcs12")
            self.assertEqual(stored.properties.tags["managedBy"], MANAGED_BY)
            self.assertEqual(pfx_leaf_sha256(stored.value), result["certificates"]["api"]["sha256"])
            again = sync_edge_certificates(config, "plan", "a" * 40, directory, "", vault_factory=lambda: vault)
            self.assertEqual(again["operations"], {"api": "keep", "admin": "keep"})
            vault.set_secret("api-tls", certificate_pem(hosts["api"]), "application/x-pem-file", {})
            renewed = sync_edge_certificates(config, "plan", "a" * 40, directory, "", vault_factory=lambda: vault)
            self.assertEqual(renewed["operations"], {"api": "write", "admin": "keep"})
            vault.set_secret("api-tls", certificate_pem(hosts["api"]), "application/x-pem-file", {})
            with self.assertRaisesRegex(MigrationError, "plan changed|changed since the plan"):
                sync_edge_certificates(config, "execute", "a" * 40, directory, renewed["planSha256"], vault_factory=lambda: vault)
            vault.set_secret("admin-tls-pfx", "unmanaged", "application/x-pkcs12", {"managedBy": "someone-else"})
            with self.assertRaisesRegex(MigrationError, "not managed by this workflow"):
                sync_edge_certificates(config, "plan", "a" * 40, directory, "", vault_factory=lambda: vault)


if __name__ == "__main__":
    unittest.main()
