"""Generate Stage 7 split proxy manifests from verified initialization receipts."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import yaml

from scripts.admin_credentials import OIDC_SECRET, SESSION_SECRET
from scripts.backend_manifest import verify_runtime_image
from scripts.customer_migration import ROOT, availability_zones, fingerprint, require, spread_across_zones, stage_fingerprint
from scripts.migration_deploy import deployment_name, group_id
from scripts.proxy_config import object_id, proxy_policy, proxy_settings
from scripts.proxy_credentials import binding_contract, credential_bindings


def render_proxy_documents(config, foundation, applications, admin_credentials, credentials):
    settings = proxy_settings(config)
    require("image" in settings, "Build and supply the signed proxy image before Stage7 publishing")
    policy = proxy_policy(config, applications)
    policy_json = json.dumps(policy, sort_keys=True, indent=2) + "\n"
    policy_name = "llm-auth-policy-" + hashlib.sha256(policy_json.encode()).hexdigest()[:12]
    documents = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": policy_name, "namespace": "litellm"}, "data": {"policy.json": policy_json}}]
    identities = [object_id(foundation[plane]["identity"]["clientId"]) for plane in ("api", "admin")]
    require(len(set(identities)) == 2 and foundation["api"]["vault"]["name"] != foundation["admin"]["vault"]["name"], "Proxy planes require separate identities and Vaults")
    require(admin_credentials["application"]["id"] == applications["admin"]["id"] and admin_credentials["application"]["appId"] == applications["admin"]["appId"], "OIDC credential belongs to a different application")
    require(admin_credentials["vaultId"].lower() == foundation["admin"]["vault"]["id"].lower(), "OIDC credential belongs to a different Vault")
    expires = datetime.fromisoformat(admin_credentials["secrets"][OIDC_SECRET]["expiresAt"].replace("Z", "+00:00"))
    require(expires.tzinfo is not None and expires > datetime.now(timezone.utc) + timedelta(days=7), "Rotate the admin OIDC credential before publishing; less than seven days remain")
    by_subject = {(item["contract"]["plane"], item["contract"]["oid"]): item for item in credentials["bindings"]}
    require(len(by_subject) == len(credentials["bindings"]), "Duplicate credential receipt bindings")
    expected_bindings = credential_bindings(config)
    require(set(by_subject) == {(item["plane"], object_id(item["oid"])) for item in expected_bindings}, "Proxy credential receipt does not match the active subject set")
    mappings = {"api": {}, "admin": {OIDC_SECRET: {"alias": "oidc-client-secret", **admin_credentials["secrets"][OIDC_SECRET]}, SESSION_SECRET: {"alias": "session-key", **admin_credentials["secrets"][SESSION_SECRET]}}}
    for binding in expected_bindings:
        item = by_subject[(binding["plane"], object_id(binding["oid"]))]
        contract = binding_contract(config, binding)
        require(item["contract"] == contract and item["secret"]["state"] == "ready" and item["userExists"] and item["keyExists"], "Proxy credential receipt is not ready or has different privileges")
        mappings[binding["plane"]][contract["secretName"]] = {"alias": contract["secretName"], **item["secret"]}
    for plane, mapping in mappings.items():
        vault = foundation[plane]["vault"]
        require(foundation[plane]["identity"].get("serviceAccountName") == f"llm-{plane}-proxy" and foundation[plane]["identity"].get("kubernetesNamespace") == "litellm", "Proxy Workload Identity federation does not match its service account")
        require(vault["id"].lower() == (group_id(config) + "/providers/Microsoft.KeyVault/vaults/" + vault["name"]).lower(), "Proxy Vault is outside the target resource group")
        if not mapping:
            continue
        objects = []
        for name, secret in mapping.items():
            require(re.fullmatch(r"[0-9a-f]{32}", secret["version"]) is not None and secret["id"].lower() == f"https://{vault['name']}.vault.azure.net/secrets/{name}/{secret['version']}".lower(), "Proxy CSI secret reference crosses its approved plane or lacks a version")
            objects.append(yaml.safe_dump({"objectName": name, "objectAlias": secret["alias"], "objectType": "secret", "objectVersion": secret["version"], "filePermission": "0444"}, sort_keys=False))
        documents.append({"apiVersion": "secrets-store.csi.x-k8s.io/v1", "kind": "SecretProviderClass", "metadata": {"name": f"llm-{plane}-auth", "namespace": "litellm"}, "spec": {"provider": "azure", "parameters": {"usePodIdentity": "false", "clientID": foundation[plane]["identity"]["clientId"], "keyvaultName": vault["name"], "tenantId": config["azure"]["tenantId"], "objects": yaml.safe_dump({"array": objects}, sort_keys=False)}}})
    for name in ("workloads.yaml", "admin-workload.yaml", "networkpolicy.yaml"):
        for item in yaml.safe_load_all((ROOT / "deploy/components/stage7-identity" / name).read_text()):
            item["metadata"]["namespace"] = "litellm"
            if item["kind"] == "ServiceAccount":
                plane = "api" if item["metadata"]["name"] == "llm-api-proxy" else "admin"
                item["metadata"]["annotations"]["azure.workload.identity/client-id"] = foundation[plane]["identity"]["clientId"]
            if item["kind"] == "Deployment":
                plane = item["spec"]["template"]["metadata"]["labels"]["plane"]
                item["spec"]["template"]["metadata"].setdefault("annotations", {})["llmgw/credential-versions"] = fingerprint(mappings[plane])
                pod = item["spec"]["template"]["spec"]
                pod["containers"][0]["image"] = settings["image"]
                next(volume for volume in pod["volumes"] if volume["name"] == "config")["configMap"]["name"] = policy_name
                spread_across_zones(pod, availability_zones(config))
            documents.append(item)
    return documents


def prepare_proxy_documents(config, revision, directory, azure, image_public_key=None):
    require("privateIngress" in config, "Managed Stage7 generation requires the verified file-provider private ingress")
    output = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 7, "proxy-foundation"), "--query", "{state:properties.provisioningState,foundation:properties.outputs.proxyFoundation.value}"])
    require(output.get("state") == "Succeeded", "Deploy proxy-foundation first")
    receipts = {}
    for action, key in (("entra-apps", "entraApplications"), ("entra-access", "entraAccess"), ("admin-credentials", "adminCredentials"), ("proxy-credentials", "proxyCredentials")):
        result = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 7, action), "--query", "{state:properties.provisioningState,receipt:properties.outputs." + key + ".value}"])
        require(result.get("state") == "Succeeded", "Complete all Stage7 initialization operations before publishing")
        receipt = result["receipt"]
        require(receipt.get("revision") == revision and receipt.get("configSha256") == stage_fingerprint(config, 7), "Stage7 initialization receipt is stale")
        receipts[action] = receipt
    require(receipts["entra-access"].get("directoryVerified") is True and receipts["entra-access"].get("applications") == receipts["entra-apps"]["applications"], "Access grant receipt does not match the verified Entra applications")
    image = proxy_settings(config).get("image")
    require(image, "Signed proxy image is required")
    verify_runtime_image(config, revision, directory, image, "auth-proxy", public_key=image_public_key)
    return render_proxy_documents(config, output["foundation"], receipts["entra-apps"]["applications"], receipts["admin-credentials"], receipts["proxy-credentials"])