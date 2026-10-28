"""Dedicated audit recovery Jobs and explicit pause/resume runtime operations."""

import copy
import ipaddress
import json
import os
import re
import socket
import time
from uuid import uuid4

from scripts.audit_window import WINDOW_NAME, assert_paused, execute_window, plan_window
from scripts.backend_manifest import verify_runtime_image
from scripts.customer_migration import USER_POOL_SELECTOR, fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands, deployment_name, group_id
from scripts.migration_runtime import connect_cluster, run_command


class AuditCluster:
    def __init__(self, command, directory):
        self.command = command
        self.directory = directory
        self.counter = 0

    def run(self, arguments):
        self.counter += 1
        return run_command([*self.command, *arguments, "--request-timeout=30s"], self.directory, f"audit-kube-{self.counter}")

    def get(self, kind, name, optional=False):
        output = self.run(["get", kind, name, "-o", "json", *(["--ignore-not-found"] if optional else [])])
        return json.loads(output) if output.strip() else None

    def items(self, kind):
        return json.loads(self.run(["get", kind, "-o", "json"]))["items"]

    def manifest(self, value):
        self.counter += 1
        path = self.directory / f"audit-object-{self.counter}.json"
        private_write(path, json.dumps(value))
        return str(path)

    def create(self, resource):
        self.run(["create", "-f", self.manifest(resource)])

    def patch(self, kind, name, operations):
        self.run(["patch", kind, name, "--type=json", "--patch-file", self.manifest(operations)])

    def delete(self, kind, name, uid):
        require(kind in {"configmap", "job"} and re.fullmatch(r"[a-z0-9-]+", name), "Unexpected recovery cleanup target")
        prefix = "/api/v1" if kind == "configmap" else "/apis/batch/v1"
        path = f"{prefix}/namespaces/litellm/{'configmaps' if kind == 'configmap' else 'jobs'}/{name}"
        options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}, "propagationPolicy": "Foreground"}
        self.run(["delete", "--raw", path, "-f", self.manifest(options)])

    @staticmethod
    def terminal(job):
        return any(condition.get("type") in {"Complete", "Failed"} and condition.get("status") == "True" for condition in job.get("status", {}).get("conditions", []))

    def assert_no_recovery_jobs(self):
        require(not any(job["spec"]["template"]["spec"].get("serviceAccountName") == "l3-recovery" and not self.terminal(job) for job in self.items("jobs")), "Recovery job is still active; keep the window paused")
        require(not any(pod["spec"].get("serviceAccountName") == "l3-recovery" and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"} for pod in self.items("pods")), "Recovery Pod is still active; keep the window paused")

    def quiet(self):
        accounts = {"llm-api-proxy", "l3-retention"}
        pods = self.items("pods")
        jobs = self.items("jobs")
        return not any(pod["spec"].get("serviceAccountName") in accounts and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"} for pod in pods) and not any(job["spec"]["template"]["spec"].get("serviceAccountName") in accounts and not self.terminal(job) for job in jobs)

    def assert_quiet(self):
        require(self.quiet(), "Writer or retention workload remains active")

    def wait_quiet(self):
        deadline = time.monotonic() + 660
        while not self.quiet():
            require(time.monotonic() < deadline, "Audit drain timed out; checkpoint retained, replan or explicitly resume")
            time.sleep(2)

    def wait_ready(self):
        self.run(["rollout", "status", "deployment/llm-api-proxy", "--timeout=660s"])

    def assert_managed_writers(self):
        accounts = {"llm-api-proxy", "l3-retention"}
        for kind in ("deployments", "statefulsets", "daemonsets", "cronjobs"):
            for resource in self.items(kind):
                template = resource["spec"].get("jobTemplate", {}).get("spec", {}).get("template", {}) if kind == "cronjobs" else resource["spec"]["template"]
                if template.get("spec", {}).get("serviceAccountName") in accounts:
                    require((kind, resource["metadata"]["name"]) in {("deployments", "llm-api-proxy"), ("cronjobs", "l3-retention")}, "Unexpected controller can restart an audit writer")
        require(not any(item["spec"]["scaleTargetRef"].get("name") == "llm-api-proxy" for item in self.items("hpa")), "Remove or separately suspend the API autoscaler before audit maintenance")


def recovery_documents(identity, image, input_data, name, subnet, api_cidr):
    require(re.fullmatch(r"[a-z0-9]+\.azurecr\.io/[a-z0-9./_-]+@sha256:[a-f0-9]{64}", image or ""), "Recovery requires a digest-pinned ACR image")
    for cidr in (subnet, api_cidr):
        require(ipaddress.ip_network(cidr).is_private, "Recovery private endpoint routes must be private")
    metadata = lambda resource_name: {"name": resource_name, "namespace": "litellm"}
    security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
    labels = {"app.kubernetes.io/name": "l3-recovery", "azure.workload.identity/use": "true"}
    documents = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {**metadata("l3-recovery"), "annotations": {"azure.workload.identity/client-id": identity["clientId"]}}, "automountServiceAccountToken": False},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": metadata("l3-recovery-check"), "rules": [
            {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": [WINDOW_NAME], "verbs": ["get"]},
            {"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["llm-api-proxy"], "verbs": ["get"]},
            {"apiGroups": ["batch"], "resources": ["cronjobs"], "resourceNames": ["l3-retention"], "verbs": ["get"]},
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
            {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["list"]},
        ]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": metadata("l3-recovery-check"),
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "l3-recovery-check"},
         "subjects": [{"kind": "ServiceAccount", "name": "l3-recovery", "namespace": "litellm"}]},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata("l3-recovery"), "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "l3-recovery"}}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": [
                {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}], "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
                {"to": [{"ipBlock": {"cidr": subnet}}, {"ipBlock": {"cidr": api_cidr}}], "ports": [{"protocol": "TCP", "port": 443}]},
                {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]}}], "ports": [{"protocol": "TCP", "port": 443}]},
            ]}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(name), "immutable": True, "data": {"input.json": json.dumps(input_data)}},
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": {**metadata(name), "labels": labels}, "spec": {
            "backoffLimit": 0, "activeDeadlineSeconds": 1800, "ttlSecondsAfterFinished": 86400,
            "template": {"metadata": {"labels": labels}, "spec": {
                "restartPolicy": "Never", "serviceAccountName": "l3-recovery", "nodeSelector": dict(USER_POOL_SELECTOR), "automountServiceAccountToken": False,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "recovery", "image": image, "command": ["node", "audit-recovery-worker.mjs"], "securityContext": security,
                                "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
                                "volumeMounts": [{"name": "input", "mountPath": "/etc/recovery", "readOnly": True}, {"name": "kube", "mountPath": "/var/run/recovery-kube", "readOnly": True}]}],
                "volumes": [{"name": "input", "configMap": {"name": name}}, {"name": "kube", "projected": {"defaultMode": 288, "sources": [
                    {"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}},
                    {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
                ]}}],
            }},
        }},
    ]
    return documents


def prepare_recovery(config, revision, directory, azure, client):
    cluster_name = config["parameters"]["platform"]["stage4Aks"]["name"]
    scope = {"tenantId": config["azure"]["tenantId"], "clusterId": group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/" + cluster_name,
             "configSha256": stage_fingerprint(config, 8)}
    client.assert_managed_writers()
    client.assert_no_recovery_jobs()
    foundation = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 8, "audit-foundation"), "--query", "{state:properties.provisioningState,foundation:properties.outputs.auditFoundation.value}"])
    require(foundation.get("state") == "Succeeded" and "recovery" in foundation.get("foundation", {}), "Deploy the audit recovery identity first")
    identities = foundation["foundation"]
    identity = identities["recovery"]
    require(identity.get("serviceAccountName") == "l3-recovery" and identity.get("kubernetesNamespace") == "litellm", "Recovery identity federation differs from the fixed Job account")
    require(identity["id"].lower().startswith(group_id(config).lower() + "/providers/microsoft.managedidentity/userassignedidentities/"), "Recovery identity is outside the target group")
    require(len({identities[role]["principalId"].lower() for role in ("writer", "reader", "retention", "recovery")}) == 4, "Audit identities must remain separate")
    live_identity = azure.scoped(["identity", "show", "--ids", identity["id"]])
    require(all(live_identity[key] == identity[key] for key in ("clientId", "principalId")), "Recovery identity was replaced")
    configured_identity = config["parameters"].get("audit", {}).get("recoveryPrincipalId")
    require(configured_identity in {"auto", identity["principalId"]}, "Configure audit.recoveryPrincipalId=auto and deploy the audit permissions first")
    storage_output = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 8, "audit"), "--query", "{state:properties.provisioningState,storageId:properties.outputs.storageResourceId.value,storageUrl:properties.outputs.storageUrl.value}"])
    require(storage_output.get("state") == "Succeeded", "Deploy audit storage before recovery")
    storage_id = storage_output["storageId"]
    require(storage_id.lower().startswith(group_id(config).lower() + "/providers/microsoft.storage/storageaccounts/"), "Audit storage is outside the target group")
    storage = azure.scoped(["storage", "account", "show", "--ids", storage_id])
    require(storage.get("publicNetworkAccess") == "Disabled" and storage.get("allowSharedKeyAccess") is False, "Recovery requires private Entra-only audit storage")
    storage_url = storage_output["storageUrl"].rstrip("/")
    require(storage["primaryEndpoints"]["blob"].rstrip("/") == storage_url, "Audit storage endpoint mismatch")
    for kind, name, template in (("deployment", "llm-api-proxy", ["template"]), ("cronjob", "l3-retention", ["jobTemplate", "spec", "template"])):
        pod = client.get(kind, name)["spec"]
        for field in template:
            pod = pod[field]
        pod = pod["spec"]
        volumes = [volume for volume in pod.get("volumes", []) if volume.get("name") == "stage8" and "configMap" in volume]
        require(len(volumes) == 1, "Stage8 audit configuration is not mounted on the managed workloads")
        settings = json.loads(client.get("configmap", volumes[0]["configMap"]["name"])["data"]["config.json"])["l3"]
        require(settings.get("enabled") is True and settings.get("deliveryMode") == "persist-before-forward" and settings.get("storageUrl", "").rstrip("/") == storage_url, "Audit workload is not bound to the approved durable store")
    image = config.get("proxy", {}).get("image")
    require(image and image.startswith(config["parameters"]["platform"]["containerRegistryName"] + ".azurecr.io/"), "Use the approved proxy image from the target registry")
    verify_runtime_image(config, revision, directory, image, "auth-proxy")
    cluster = azure.scoped(["aks", "show", "--resource-group", config["target"]["resourceGroup"], "--name", cluster_name])
    require(cluster.get("apiServerAccessProfile", {}).get("enablePrivateCluster") is True, "Recovery requires the approved private AKS API")
    federation = azure.scoped(["identity", "federated-credential", "list", "--identity-name", identity["name"], "--resource-group", config["target"]["resourceGroup"]])
    require(len(federation) == 1 and federation[0].get("subject") == "system:serviceaccount:litellm:l3-recovery" and federation[0].get("issuer") == cluster["oidcIssuerProfile"]["issuerUrl"] and federation[0].get("audiences") == ["api://AzureADTokenExchange"], "Recovery federation changed or trusts additional subjects")
    api_address = socket.gethostbyname(cluster["privateFqdn"])
    require(ipaddress.ip_address(api_address).is_private, "Private AKS API did not resolve privately")
    endpoints = azure.scoped(["network", "private-endpoint", "list", "--resource-group", config["target"]["resourceGroup"]])
    matches = []
    for endpoint in endpoints:
        properties = endpoint.get("properties", endpoint)
        for connection in properties.get("privateLinkServiceConnections", []):
            link = connection.get("properties", connection)
            if link.get("privateLinkServiceId", "").lower() == storage_id.lower() and "blob" in link.get("groupIds", []):
                require(link.get("privateLinkServiceConnectionState", {}).get("status") == "Approved", "Audit Blob private endpoint is not approved")
                matches.append(properties["subnet"]["id"])
    require(len(matches) == 1, "Audit storage must have one approved Blob endpoint in the target group")
    subnet_id = matches[0]
    audit = config["parameters"]["audit"]
    require(subnet_id.lower() == (group_id(config) + "/providers/Microsoft.Network/virtualNetworks/" + audit["virtualNetworkName"] + "/subnets/" + audit["privateEndpointSubnetName"]).lower(), "Audit endpoint uses an unapproved subnet")
    subnet = azure.scoped(["network", "vnet", "subnet", "show", "--ids", subnet_id])
    return {"scope": scope, "identity": identity, "image": image, "storageUrl": storage_url,
            "subnet": subnet.get("addressPrefix") or subnet["addressPrefixes"][0], "apiCidr": api_address + "/32"}


def install_recovery_support(client, documents):
    for document in documents:
        existing = client.get(document["kind"].lower(), document["metadata"]["name"], optional=True)
        if existing:
            for key in ("spec", "rules", "roleRef", "subjects", "automountServiceAccountToken"):
                if key in document:
                    actual = copy.deepcopy(existing.get(key))
                    if key == "spec" and document["kind"] == "NetworkPolicy":
                        actual.setdefault("ingress", [])
                    if key == "subjects":
                        for subject in actual:
                            if subject.get("apiGroup") == "":
                                subject.pop("apiGroup")
                    require(actual == document[key], "Existing recovery support differs from the approved contract")
            for key, value in document["metadata"].get("annotations", {}).items():
                require(existing["metadata"].get("annotations", {}).get(key) == value, "Recovery service account identity differs")
        else:
            client.create(document)


def run_recovery_job(client, documents):
    install_recovery_support(client, documents[:4])
    client.create(documents[4])
    client.create(documents[5])
    name = documents[5]["metadata"]["name"]
    deadline = time.monotonic() + 1830
    while True:
        job = client.get("job", name)
        if client.terminal(job):
            require(any(condition.get("type") == "Complete" and condition.get("status") == "True" for condition in job["status"]["conditions"]), "Recovery Job failed; keep window paused and inspect metadata-only Job diagnostics")
            break
        require(time.monotonic() < deadline, "Recovery Job timed out; do not resume while it remains active")
        time.sleep(2)
    output = client.run(["logs", "job/" + name, "--container", "recovery", "--limit-bytes=1048576"])
    result = json.loads(output)
    require(set(result) == {"plan", "summary"} and result["summary"].get("stageAccepted") is False, "Unexpected recovery Job report")
    configmap = client.get("configmap", name)
    client.delete("configmap", name, configmap["metadata"]["uid"])
    return result


def audit_operation(config, action, operation, revision, directory, approved, *, client=None, azure=None, job_runner=run_recovery_job):
    require(action in {"audit-pause", "audit-recover", "audit-resume"} and operation in {"plan", "execute"}, "Invalid audit runtime operation")
    client = client or AuditCluster(connect_cluster(config, directory, False), directory)
    scope = {"tenantId": config["azure"]["tenantId"], "clusterId": group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/" + config["parameters"]["platform"]["stage4Aks"]["name"], "configSha256": stage_fingerprint(config, 8)}
    client.assert_managed_writers()
    if action != "audit-recover":
        plan = {"revision": revision, "scope": scope, "windowPlan": plan_window(client, scope, action)}
        summary = {"stage": 8, "action": action, "revision": revision, "planSha256": fingerprint(plan), "stageAccepted": False, "applied": False}
        private_write(directory / "runtime-review.json", json.dumps(plan, indent=2))
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
        if operation == "execute":
            require(approved == summary["planSha256"], "Audit window plan changed or was not approved")
            summary.update(execute_window(client, scope, action, fingerprint(plan["windowPlan"])), applied=True)
    else:
        context = prepare_recovery(config, revision, directory, azure or AzureCommands(config, directory), client)
        context["window"] = assert_paused(client, scope)
        cursor = os.environ.get("AUDIT_RECOVERY_CURSOR") or None
        require(cursor is None or len(cursor) <= 4096, "Recovery cursor is too large")
        recovery_scope = {"tenantId": scope["tenantId"], "revision": revision, "configSha256": scope["configSha256"], "storageUrl": context["storageUrl"]}
        input_data = {"scope": recovery_scope, "window": context["window"], "operation": operation, "cursor": cursor}
        if operation == "execute":
            reviewed = json.loads(os.environ.get("MIGRATION_AUDIT_PLAN_JSON") or "null")
            require(isinstance(reviewed, dict) and fingerprint(reviewed) == approved, "Download the approved audit plan artifact before execute")
            require(reviewed.get("context") == context and reviewed.get("cursor") == cursor, "Audit target, image or pause checkpoint changed")
            input_data["approved"] = reviewed["recovery"]["summary"]["planSha256"]
        name = "l3-recovery-" + uuid4().hex[:20]
        documents = recovery_documents(context["identity"], context["image"], input_data, name, context["subnet"], context["apiCidr"])
        require(assert_paused(client, scope) == context["window"], "Pause checkpoint changed before Job creation")
        result = job_runner(client, documents)
        require(result["summary"].get("applied") is (operation == "execute"), "Recovery Job operation mismatch")
        plan = {"context": context, "cursor": cursor, "recovery": result}
        summary = {**result["summary"], "revision": revision, "planSha256": fingerprint(plan) if operation == "plan" else approved, "windowId": context["window"]["windowId"], "phase": "paused"}
        if operation == "plan":
            private_write(directory / "runtime-review.json", json.dumps(plan, indent=2))
        else:
            private_write(directory / "runtime-review.json", json.dumps(reviewed, indent=2))
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    print(json.dumps(summary))
    return summary