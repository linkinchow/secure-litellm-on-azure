"""API-only ACME DNS-01 issuance with durable Key Vault state and exact TXT updates."""

import copy
from datetime import datetime, timedelta, timezone
import json
import re
import time
from urllib.parse import urlsplit
from uuid import UUID

from scripts.customer_migration import fingerprint, private_write, require, stage_fingerprint
from scripts.private_ingress import certificate_material

ACME_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"


def certificate_settings(config):
    value = config.get("certificates")
    require(isinstance(value, dict) and set(value) == {"zoneResourceId", "termsAccepted", "publicApiHostnameAccepted"}, "certificates requires zoneResourceId and explicit CA terms/public hostname acceptance")
    require(value["termsAccepted"] is True and value["publicApiHostnameAccepted"] is True, "Explicit public CA terms and certificate transparency acceptance required")
    match = re.fullmatch(r"/subscriptions/([a-fA-F0-9-]{36})/resourceGroups/([A-Za-z0-9_.()-]+)/providers/Microsoft.Network/dnsZones/([a-z0-9.-]+)", value["zoneResourceId"])
    host = "llm-api." + config["baseDomain"]
    require(match and UUID(match[1]) == UUID(config["azure"]["subscriptionId"]) and host.endswith("." + match[3]), "API certificate DNS zone is outside approved subscription/domain")
    secret = urlsplit(config["privateIngress"]["api"]["tlsSecretId"])
    require(secret.scheme == "https" and re.fullmatch(r"[a-z0-9-]+\.vault\.azure\.net", secret.netloc) and re.fullmatch(r"/secrets/[A-Za-z0-9-]+", secret.path) and not secret.query and not secret.fragment, "Automatic API certificate renewal needs a versionless approved Vault secret")
    require(config["privateIngress"]["admin"]["tlsSecretId"] != config["privateIngress"]["api"]["tlsSecretId"], "API and enterprise admin certificates must remain separate")
    return {"host": host, "zone": match[3], "recordId": value["zoneResourceId"] + "/TXT/_acme-challenge." + host[:-(len(match[3]) + 1)], "vaultUrl": "https://" + secret.netloc, "secretName": secret.path.split("/")[-1]}


class TxtChallenge:
    def __init__(self, request, record_id, wait):
        self.request, self.record_id, self.wait = request, record_id, wait

    def read(self):
        return self.request("GET", self.record_id, None, {})

    def change(self, value, remove=False):
        require(re.fullmatch(r"[A-Za-z0-9_-]{20,200}", value or ""), "Invalid ACME DNS validation token")
        current = self.read()
        properties = copy.deepcopy(current["properties"]) if current else {"TTL": 60, "TXTRecords": []}
        records = properties.get("TXTRecords", [])
        own = {"value": [value]}
        if remove:
            if own not in records:
                return
            records.remove(own)
        elif own not in records:
            records.append(own)
        else:
            return
        properties["TXTRecords"] = records
        headers = {"If-Match": current["etag"]} if current else {"If-None-Match": "*"}
        self.request("PUT", self.record_id, {"properties": properties}, headers)


def new_key_and_csr(host):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])).add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False).sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()
    return pem, csr


def issue_api_certificate(settings, state, save, engine, challenge):
    if state is None:
        key, csr = new_key_and_csr(settings["host"])
        state = {"version": 1, "host": settings["host"], "directory": ACME_DIRECTORY, "key": key, "csr": csr, "phase": "prepared"}
        save(state)
    require(state.get("version") == 1 and state.get("host") == settings["host"] and state.get("directory") == ACME_DIRECTORY, "Pending certificate belongs to another host or CA")
    if state["phase"] == "ordering":
        raise ValueError("Previous order acknowledgement is uncertain; inspect CA account before clearing the isolated pending state")
    if state["phase"] == "prepared":
        state = {**state, "phase": "ordering"}
        save(state)
        order = engine.new_order(state["csr"].encode())
        state = {**state, "phase": "ordered", "order": json.loads(order.json_dumps())}
        save(state)
    require(state["phase"] in {"ordered", "issued"}, "Invalid certificate recovery phase")
    if state["phase"] == "issued":
        if state.get("validation"):
            challenge.change(state["validation"], remove=True)
        return state["pem"]
    order = engine.restore(state["order"])
    require(order.csr_pem == state["csr"].encode(), "Persisted ACME order does not match the approved CSR")
    require(len(order.authorizations) == 1, "API certificate order must have exactly one hostname authorization")
    urls = [order.uri, order.body.finalize, *order.body.authorizations, *(authorization.uri for authorization in order.authorizations)]
    for authorization in order.authorizations:
        urls.extend(item.uri for item in (authorization.body.challenges or ()))
    if order.body.certificate:
        urls.append(order.body.certificate)
    for value in urls:
        parsed = urlsplit(value)
        require(parsed.scheme == "https" and parsed.netloc == "acme-v02.api.letsencrypt.org" and not parsed.query and not parsed.fragment and not parsed.username, "Persisted ACME order left the fixed CA authority")
    try:
        for authorization in order.authorizations:
            require(authorization.body.identifier.value == settings["host"], "ACME authorization crossed hostname scope")
            if authorization.body.status.to_json() == "valid":
                continue
            selected = [item for item in authorization.body.challenges if item.chall.typ == "dns-01"]
            require(len(selected) == 1, "DNS-01 challenge missing or ambiguous")
            item = selected[0]
            validation = item.chall.validation(engine.key)
            state = {**state, "validation": validation}
            save(state)
            challenge.change(validation)
            challenge.wait(settings["host"], validation)
            engine.answer_challenge(item, item.response(engine.key))
        completed = engine.poll_and_finalize(order, deadline=datetime.now() + timedelta(minutes=5))
        pem = completed.fullchain_pem + state["key"]
        certificate_material(pem, settings["host"])
        save({**state, "phase": "issued", "pem": pem})
        return pem
    finally:
        if state.get("validation"):
            challenge.change(state["validation"], remove=True)


class AcmeEngine:
    def __init__(self, account_pem, email):
        from acme import client, errors, messages
        import josepy
        self.key = josepy.JWKRSA.load(account_pem.encode())
        self.net = client.ClientNetwork(self.key, user_agent="llmgw-certificate-workflow", timeout=30)
        self.client = client.ClientV2(messages.Directory.from_json(self.net.get(ACME_DIRECTORY).json()), self.net)
        try:
            self.client.new_account(messages.NewRegistration.from_data(email=email, terms_of_service_agreed=True))
        except errors.ConflictError as existing:
            self.client.query_registration(messages.RegistrationResource(uri=existing.location, body=messages.Registration()))

    def restore(self, value):
        from acme import messages
        return messages.OrderResource.from_json(value)

    def new_order(self, csr): return self.client.new_order(csr)
    def answer_challenge(self, challenge, response): return self.client.answer_challenge(challenge, response)
    def poll_and_finalize(self, order, deadline): return self.client.poll_and_finalize(order, deadline)


def certificate_operation(config, operation, revision, directory, approved):
    from azure.identity import AzureCliCredential
    from azure.keyvault.secrets import SecretClient
    from azure.core.exceptions import ResourceNotFoundError
    import requests
    import dns.resolver

    require(operation in {"plan", "execute"}, "Invalid certificate operation")
    settings = certificate_settings(config)
    credential = AzureCliCredential(tenant_id=config["azure"]["tenantId"])
    vault = SecretClient(settings["vaultUrl"], credential, retry_total=0, logging_enable=False)
    session = requests.Session()
    session.trust_env = False
    try:
        def read(name):
            try:
                secret = vault.get_secret(name)
                require(secret.properties.enabled is not False, "Certificate state secret is disabled")
                return secret
            except ResourceNotFoundError:
                return None
        current = read(settings["secretName"])
        state_name = "llmgw-acme-" + fingerprint(settings)[:20]
        pending = read(state_name)
        keep = False
        material = None
        if current:
            try:
                material = certificate_material(current.value, settings["host"])
                keep = datetime.fromisoformat(material["expiresAt"]) > datetime.now(timezone.utc) + timedelta(days=30)
            except Exception:
                require((current.properties.tags or {}).get("managedBy") == "llmgw-acme", "Unmanaged or invalid API certificate cannot be replaced automatically")
        plan = {"stage": 4, "action": "certificate-renew", "revision": revision, "configSha256": stage_fingerprint(config, 4), "settings": settings, "currentVersion": current.properties.version if current else None, "stateVersion": pending.properties.version if pending else None, "operation": "keep" if keep else "issue", "directory": ACME_DIRECTORY}
        summary = {"stage": 4, "action": "certificate-renew", "planSha256": fingerprint(plan), "applied": False, "stageAccepted": False, "operation": plan["operation"]}
        private_write(directory / "runtime-review.json", json.dumps(plan, indent=2))
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
        if operation == "plan":
            return summary
        require(summary["planSha256"] == approved, "Certificate plan changed or was not approved")
        if keep:
            summary.update(applied=True, certificateSha256=material["sha256"])
        else:
            token_scope = "https://management.azure.com/.default"
            def arm(method, resource, body, headers):
                result = session.request(method, "https://management.azure.com" + resource + "?api-version=2018-05-01", json=body, headers={**headers, "Authorization": "Bearer " + credential.get_token(token_scope).token}, allow_redirects=False, timeout=30)
                if method == "GET" and result.status_code == 404:
                    return None
                require(result.status_code in {200, 201}, "Conditional ACME DNS operation failed; replan")
                return result.json()
            def propagated(host, value):
                resolver = dns.resolver.Resolver()
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    try:
                        answers = resolver.resolve("_acme-challenge." + host, "TXT", lifetime=5)
                        if value in [b"".join(answer.strings).decode() for answer in answers]:
                            return
                    except dns.exception.DNSException:
                        pass
                    time.sleep(2)
                raise ValueError("ACME DNS propagation timed out")
            account_name = "llmgw-acme-account"
            account = read(account_name)
            if not account:
                account_key, _ = new_key_and_csr(settings["host"])
                account = vault.set_secret(account_name, account_key, tags={"managedBy": "llmgw-acme"})
            require((account.properties.tags or {}).get("managedBy") == "llmgw-acme", "ACME account is not managed by this workflow")
            state = json.loads(pending.value) if pending else None
            if state and state.get("phase") == "issued" and current and current.value == state["pem"]:
                state = None
            saved_version = pending.properties.version if pending else None
            def save(value):
                nonlocal saved_version
                before = read(state_name)
                require((before.properties.version if before else None) == saved_version, "Certificate workflow state changed concurrently")
                encoded = json.dumps(value)
                require(len(encoded.encode()) <= 25000, "ACME state exceeds Vault secret size")
                saved = vault.set_secret(state_name, encoded, tags={"managedBy": "llmgw-acme"})
                saved_version = saved.properties.version
            engine = AcmeEngine(account.value, config["ownerEmail"])
            try:
                pem = issue_api_certificate(settings, state, save, engine, TxtChallenge(arm, settings["recordId"], propagated))
            finally:
                engine.net.session.close()
            material = certificate_material(pem, settings["host"])
            before = read(settings["secretName"])
            require((before.properties.version if before else None) == plan["currentVersion"], "API certificate changed during issuance; pending issued version retained")
            saved = vault.set_secret(settings["secretName"], pem, content_type="application/x-pem-file", tags={"managedBy": "llmgw-acme", "hostname": settings["host"], "sha256": material["sha256"]})
            require(vault.get_secret(settings["secretName"], saved.properties.version).value == pem, "Certificate write could not be confirmed")
            from scripts.app_gateway import is_app_gateway
            if is_app_gateway(config):
                # The Application Gateway listener follows the versionless PKCS#12 copy.
                from scripts.certificate_pfx import write_pfx
                write_pfx(vault, settings["secretName"], pem, settings["host"], saved.properties.version)
            summary.update(applied=True, secretId=saved.id, certificateSha256=material["sha256"], expiresAt=material["expiresAt"], ingressUpdated=False)
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
        print(json.dumps(summary))
        return summary
    except Exception:
        raise ValueError("Certificate operation failed; preserve pending Vault state and replan. No private certificate material is logged.") from None
    finally:
        vault.close()
        credential.close()
        session.close()