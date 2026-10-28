"""Initialize admin backend credentials over an approved private AKS tunnel."""

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import json
from queue import Empty, Queue
import re
import subprocess
import threading

import requests

from scripts.customer_migration import MigrationError, fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands, deployment_name, group_id
from scripts.migration_runtime import connect_cluster, run_command
from scripts.proxy_config import object_id
from scripts.proxy_credentials import credential_bindings, inspect_binding, provision_binding
from scripts.runtime_revision import receipt_current, runtime_revision


@contextmanager
def backend_tunnel(kube):
    process = subprocess.Popen([*kube, "port-forward", "--address=127.0.0.1", "service/litellm", ":4000"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    ports = Queue(maxsize=1)

    def collect():
        for line in process.stdout:
            match = re.fullmatch(r"Forwarding from 127\.0\.0\.1:([0-9]+) -> 4000\s*", line)
            if match and ports.empty():
                ports.put(int(match[1]))

    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    try:
        try:
            port = ports.get(timeout=30)
        except Empty:
            raise MigrationError("Target backend tunnel did not become ready; no credentials were published") from None
        require(process.poll() is None and 0 < port < 65536, "Backend tunnel failed")
        yield f"http://127.0.0.1:{port}"
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        thread.join(timeout=2)
        process.stdout.close()


class BackendAPI:
    def __init__(self, endpoint, master_key):
        require(re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", endpoint) is not None, "Bootstrap backend must use a loopback-only AKS tunnel")
        self.endpoint = endpoint
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers["Authorization"] = "Bearer " + master_key

    def request(self, method, path, data=None, params=None):
        require((method, path) in {("GET", "/user/info"), ("GET", "/key/info"), ("POST", "/user/new"), ("POST", "/key/generate")}, "Backend bootstrap route is not allowed")
        try:
            response = self.session.request(method, self.endpoint + path, json=data, params=params, timeout=30, allow_redirects=False)
            if method == "GET" and response.status_code == 404:
                return None
            require(response.status_code == 200, "Backend credential API rejected the operation; pending state retained for review")
            return response.json()
        except (requests.RequestException, requests.exceptions.JSONDecodeError):
            raise MigrationError("Backend credential API failed; replan to inspect possible partial results") from None

    def user_info(self, user_id):
        result = self.request("GET", "/user/info", params={"user_id": user_id})
        return result["user_info"] if result else None

    def key_info(self, digest):
        require(re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "Key lookup must use a digest, never a raw credential")
        result = self.request("GET", "/key/info", params={"key": digest})
        return result["info"] if result else None

    def create_user(self, payload):
        self.request("POST", "/user/new", data=payload)

    def create_key(self, payload):
        self.request("POST", "/key/generate", data=payload)

    def close(self):
        self.session.headers.pop("Authorization", None)
        self.session.close()


def binding_plan(config, operation, revision, directory, approved, vaults, backend, scope=None):
    require(operation in {"plan", "execute"}, "Invalid proxy credential operation")
    bindings = credential_bindings(config)
    before = [inspect_binding(config, binding, vaults[binding["plane"]], backend)[2] for binding in bindings]
    plan = {"stage": 7, "action": "proxy-credentials", "revision": revision, "configSha256": stage_fingerprint(config, 7), "before": before, "scope": scope}
    digest = fingerprint(plan)
    summary = {"stage": 7, "action": "proxy-credentials", "planSha256": digest, "stageAccepted": False}
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2) + "\n")
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    if operation == "execute":
        require(digest == approved, "Proxy credential plan changed or was not approved")
        summaries = []
        for binding, observed in zip(bindings, before):
            require(inspect_binding(config, binding, vaults[binding["plane"]], backend)[2] == observed, "Proxy credential state changed during initialization")
            summaries.append(provision_binding(config, binding, vaults[binding["plane"]], backend))
        summary.update(initialized=True, bindings=summaries)
    print(json.dumps({key: value for key, value in summary.items() if key != "bindings"}))
    return summary


def initialize_proxy_credentials(config, operation, revision, directory, approved):
    from azure.identity import AzureCliCredential
    from azure.keyvault.secrets import SecretClient
    from azure.core.exceptions import AzureError

    require(operation in {"plan", "execute"}, "Invalid proxy credential operation")
    credential_bindings(config)
    kube = connect_cluster(config, directory, legacy=False)
    azure = AzureCommands(config, directory)
    group = config["target"]["resourceGroup"]
    foundation_output = azure.scoped(["deployment", "group", "show", "--resource-group", group, "--name", deployment_name(config, 7, "proxy-foundation"), "--query", "{state:properties.provisioningState,foundation:properties.outputs.proxyFoundation.value}"])
    require(foundation_output.get("state") == "Succeeded", "Deploy proxy-foundation before proxy credentials")
    foundation = foundation_output["foundation"]
    secret_output = azure.scoped(["deployment", "group", "show", "--resource-group", group, "--name", deployment_name(config, 5, "backend-secrets"), "--query", "{state:properties.provisioningState,receipt:properties.outputs.backendSecrets.value}"])
    require(secret_output.get("state") == "Succeeded", "Initialize backend secrets first")
    receipt = secret_output["receipt"]
    require(receipt_current(receipt, revision, runtime_revision("azure", revision)) and receipt.get("configSha256") == stage_fingerprint(config, 5), "Backend secret receipt is stale")
    require(receipt["vaultId"].lower() == (group_id(config) + "/providers/Microsoft.KeyVault/vaults/" + receipt["vaultName"]).lower(), "Backend Vault scope mismatch")
    deployment = json.loads(run_command([*kube, "get", "deployment", "litellm", "-o", "json"], directory, "backend-bootstrap-deployment"))
    containers = deployment["spec"]["template"]["spec"]["containers"]
    require(len(containers) == 1 and containers[0]["name"] == "litellm" and containers[0]["image"] == config["application"]["backendImage"] and deployment.get("status", {}).get("availableReplicas", 0) >= 2, "Backend bootstrap requires the approved ready Stage6 deployment")
    service = json.loads(run_command([*kube, "get", "service", "litellm", "-o", "json"], directory, "backend-bootstrap-service"))
    require(service["spec"].get("type", "ClusterIP") == "ClusterIP" and service["spec"].get("selector") == deployment["spec"]["selector"]["matchLabels"], "Backend Service must select only the approved Deployment labels")
    identities = {object_id(foundation[plane]["identity"]["principalId"]) for plane in ("api", "admin")}
    require(len(identities) == 2, "API/admin runtime identities must remain distinct")
    vault_names = {foundation[plane]["vault"]["name"] for plane in ("api", "admin")}
    require(len(vault_names) == 2 and receipt["vaultName"] not in vault_names, "Proxy and backend Vaults must be distinct")
    credential = AzureCliCredential(tenant_id=config["azure"]["tenantId"])
    try:
        with ExitStack() as stack:
            vaults = {}
            for plane in ("admin",):
                name = foundation[plane]["vault"]["name"]
                vault = azure.scoped(["keyvault", "show", "--resource-group", group, "--name", name, "--query", "{id:id,uri:properties.vaultUri,public:properties.publicNetworkAccess,rbac:properties.enableRbacAuthorization,purge:properties.enablePurgeProtection}"])
                require(vault["id"].lower() == (group_id(config) + "/providers/Microsoft.KeyVault/vaults/" + name).lower() and vault["id"].lower() == foundation[plane]["vault"]["id"].lower() and vault["uri"].rstrip("/").lower() == f"https://{name}.vault.azure.net".lower(), "Proxy Vault is outside the target scope")
                require(vault["public"] == "Disabled" and vault["rbac"] is True and vault["purge"] is True, "Proxy Vault must retain private RBAC protection")
                vaults[plane] = stack.enter_context(SecretClient(vault_url=vault["uri"], credential=credential))
            backend_vault = stack.enter_context(SecretClient(vault_url=f"https://{receipt['vaultName']}.vault.azure.net", credential=credential))
            master_meta = receipt["secrets"]["litellm-master-key"]
            master = backend_vault.get_secret("litellm-master-key", master_meta["version"])
            require(master.properties.enabled is True and master.properties.id == master_meta["id"], "Backend Master Key version changed or is disabled")
            endpoint = stack.enter_context(backend_tunnel(kube))
            backend = BackendAPI(endpoint, master.value)
            stack.callback(backend.close)
            scope = {"foundation": foundation, "backendDeploymentUid": deployment["metadata"]["uid"], "backendServiceUid": service["metadata"]["uid"], "backendMasterVersion": master_meta}
            summary = binding_plan(config, operation, revision, directory, approved, vaults, backend, scope)
            if operation == "execute":
                metadata = {"revision": revision, "configSha256": stage_fingerprint(config, 7), "bindings": summary["bindings"], "verifiedAt": datetime.now(timezone.utc).isoformat()}
                template = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"proxyCredentials": {"type": "object", "value": metadata}}}
                path = directory / "proxy-credential-receipt.json"
                private_write(path, json.dumps(template))
                saved = azure.scoped(["deployment", "group", "create", "--resource-group", group, "--name", deployment_name(config, 7, "proxy-credentials"), "--mode", "Incremental", "--template-file", str(path)])
                require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "Credentials initialized but receipt failed; replan without rotating existing keys")
                private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
            return summary
    except AzureError:
        raise MigrationError("Proxy credential Vault operation failed; pending initialization is retained for a reviewed retry") from None
    finally:
        credential.close()