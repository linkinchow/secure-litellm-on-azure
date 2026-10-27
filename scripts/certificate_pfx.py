"""PKCS#12 copies of the ingress certificates for the optional Application Gateway listeners."""

import base64
import hashlib
import json

from scripts.customer_migration import fingerprint, private_write, require, stage_fingerprint
from scripts.private_ingress import certificate_material

MANAGED_BY = "llmgw-edge-certificates"


def pfx_from_pem(pem, host):
    """Base64 PKCS#12 without a password, the format Application Gateway reads from Key Vault."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    material = certificate_material(pem, host)
    certificates = x509.load_pem_x509_certificates(material["certificate"].encode())
    key = serialization.load_pem_private_key(material["key"].encode(), password=None)
    data = pkcs12.serialize_key_and_certificates(host.encode(), key, certificates[0], certificates[1:] or None, serialization.NoEncryption())
    return base64.b64encode(data).decode(), material["sha256"]


def pfx_leaf_sha256(value):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    _key, certificate, _chain = pkcs12.load_key_and_certificates(base64.b64decode(value), None)
    require(certificate is not None, "PKCS#12 secret has no leaf certificate")
    return hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()


def pfx_secret_name(pem_secret_name):
    return pem_secret_name + "-pfx"


def write_pfx(vault, pem_secret_name, pem, host, source_version):
    """Store the PKCS#12 copy next to its PEM secret and confirm the stored leaf certificate."""
    value, sha256 = pfx_from_pem(pem, host)
    name = pfx_secret_name(pem_secret_name)
    saved = vault.set_secret(name, value, content_type="application/x-pkcs12", tags={"managedBy": MANAGED_BY, "hostname": host, "sha256": sha256, "sourceVersion": source_version})
    require(pfx_leaf_sha256(vault.get_secret(name, saved.properties.version).value) == sha256, "PKCS#12 certificate write could not be confirmed")
    return saved


def sync_edge_certificates(config, operation, revision, directory, approved, vault_factory=None):
    from scripts.app_gateway import app_gateway_settings, certificate_secret_names

    require(operation in {"plan", "execute"}, "Invalid edge certificate operation")
    app_gateway_settings(config)
    vault_name = config["parameters"]["certificate-vault"]["vaultName"]
    hosts = {"api": "llm-api." + config["baseDomain"], "admin": "llm-admin." + config["baseDomain"]}
    if vault_factory is None:
        from azure.identity import AzureCliCredential
        from azure.keyvault.secrets import SecretClient
        credential = AzureCliCredential(tenant_id=config["azure"]["tenantId"])
        vault = SecretClient(f"https://{vault_name}.vault.azure.net", credential, retry_total=0, logging_enable=False)
    else:
        credential, vault = None, vault_factory()
    try:
        from azure.core.exceptions import ResourceNotFoundError

        def read(name):
            try:
                return vault.get_secret(name)
            except ResourceNotFoundError:
                return None

        planes, sources = {}, {}
        for plane, target in certificate_secret_names().items():
            source_name = target.removesuffix("-pfx")
            source = read(source_name)
            require(source is not None and source.properties.enabled is not False, f"{plane} PEM certificate secret is missing or disabled")
            material = certificate_material(source.value, hosts[plane])
            current = read(target)
            tags = (current.properties.tags or {}) if current else {}
            keep = current is not None and current.properties.enabled is not False and tags.get("sha256") == material["sha256"] and tags.get("sourceVersion") == source.properties.version
            require(current is None or tags.get("managedBy") == MANAGED_BY, f"{plane} PKCS#12 secret is not managed by this workflow")
            planes[plane] = {"pemSecret": source_name, "pemVersion": source.properties.version, "sha256": material["sha256"], "expiresAt": material["expiresAt"], "pfxSecret": target, "pfxVersion": current.properties.version if current else None, "operation": "keep" if keep else "write"}
            sources[plane] = source
        plan = {"stage": 9, "action": "edge-certificates", "revision": revision, "configSha256": stage_fingerprint(config, 9), "vault": vault_name, "planes": planes}
        summary = {"stage": 9, "action": "edge-certificates", "planSha256": fingerprint(plan), "applied": False, "stageAccepted": False, "operations": {plane: item["operation"] for plane, item in planes.items()}}
        private_write(directory / "runtime-review.json", json.dumps(plan, indent=2))
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
        if operation == "plan":
            print(json.dumps(summary))
            return summary
        require(summary["planSha256"] == approved, "Edge certificate plan changed or was not approved")
        for plane, item in planes.items():
            if item["operation"] == "keep":
                continue
            current = read(item["pfxSecret"])
            require((current.properties.version if current else None) == item["pfxVersion"] and read(item["pemSecret"]).properties.version == item["pemVersion"], f"{plane} certificate secrets changed since the plan")
            saved = write_pfx(vault, item["pemSecret"], sources[plane].value, hosts[plane], item["pemVersion"])
            item["pfxVersion"] = saved.properties.version
        summary.update(applied=True, certificates={plane: {"sha256": item["sha256"], "expiresAt": item["expiresAt"], "pfxVersion": item["pfxVersion"]} for plane, item in planes.items()})
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
        print(json.dumps(summary))
        return summary
    finally:
        vault.close()
        if credential is not None:
            credential.close()
