"""Private-runner operations for legacy baseline, hardening, and reviewed target manifests."""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

import yaml

from scripts.customer_migration import ROOT, USER_POOL_SELECTOR, MigrationError, active_stages, configured, deployment_mode, fingerprint, private_write, require, stage_fingerprint, validate_config, validate_evidence
from scripts.migration_deploy import AzureCommands, deployment_name, group_id
from scripts.migration_evidence import draft_report
from scripts.workflow_diagnostics import command_failure_summary, diagnostic_exit


POSTGRES_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(arguments, directory, label, allowed=(0,), environment=None):
    result = subprocess.run(arguments, capture_output=True, text=True, check=False, env=environment)
    private_write(directory / f"{label}.stdout.txt", result.stdout)
    private_write(directory / f"{label}.stderr.txt", result.stderr)
    require(result.returncode in allowed, f"{label} failed: {command_failure_summary(result.stdout, result.stderr, result.returncode)}")
    return result.stdout


def capture_restore_container_diagnostics(container_name, directory):
    try:
        state = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", container_name],
            capture_output=True, text=True, check=False, timeout=30,
        )
        document = json.loads(state.stdout) if state.returncode == 0 and state.stdout.strip() else {"inspectExitCode": state.returncode}
        private_write(directory / "restore-container-state.json", json.dumps(document, indent=2) + "\n")
        logs = subprocess.run(
            ["docker", "logs", "--tail", "200", container_name],
            capture_output=True, text=True, check=False, timeout=30,
        )
        private_write(directory / "restore-container.log", logs.stdout + logs.stderr)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


def connect_cluster(config, directory, legacy):
    require(not legacy or deployment_mode(config) == "migration", "Greenfield cannot access a legacy cluster")
    azure = AzureCommands(config, directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope mismatch")
    group = config["legacy" if legacy else "target"]["resourceGroup"]
    name = config["legacy"]["aksClusterName"] if legacy else config["parameters"]["platform"]["stage4Aks"]["name"]
    kubeconfig = directory / "kubeconfig"
    run_command(["az", "aks", "get-credentials", "--subscription", config["azure"]["subscriptionId"], "--resource-group", group, "--name", name, "--file", str(kubeconfig), "--overwrite-existing"], directory, "cluster-credentials")
    kubeconfig.chmod(0o600)
    run_command(["kubelogin", "convert-kubeconfig", "--kubeconfig", str(kubeconfig), "-l", "azurecli"], directory, "cluster-login")
    namespace = config["legacy"]["namespace"] if legacy else "litellm"
    return ["kubectl", "--kubeconfig", str(kubeconfig), "--namespace", namespace]


def hardened_spec(deployment):
    document = copy.deepcopy(deployment)
    spec = document["spec"]
    postgres = document["metadata"]["name"] == "postgres"
    if postgres:
        spec["strategy"] = {"type": "Recreate"}
    containers = spec["template"]["spec"]["containers"]
    expected = "postgres" if postgres else "litellm"
    selected = [container for container in containers if container["name"] == expected]
    require(len(selected) == 1, "Legacy container differs from the reviewed deployment contract")
    container = selected[0]
    for probe, threshold, delay in (("startupProbe", 60, 0), ("readinessProbe", 3, 5), ("livenessProbe", 6, 10)):
        if probe not in container:
            action = {"exec": {"command": ["sh", "-c", 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"']}} if postgres else {"httpGet": {"path": "/health/readiness" if probe == "readinessProbe" else "/health/liveliness", "port": 4000}}
            container[probe] = {**action, "periodSeconds": 10, "timeoutSeconds": 5, "failureThreshold": threshold, "initialDelaySeconds": delay}
    return spec


def check_application(documents, stage, config):
    require(stage in {6, 7, 8}, "Application publishing supports stages 6, 7 and 8")
    require(isinstance(documents, list) and documents, "Reviewed rendered manifests are required")
    autoscaled = {}
    for document in documents:
        if isinstance(document, dict) and document.get("kind") == "HorizontalPodAutoscaler":
            spec = document.get("spec", {})
            target = spec.get("scaleTargetRef", {})
            name = target.get("name")
            require(target.get("apiVersion") == "apps/v1" and target.get("kind") == "Deployment" and isinstance(name, str), "HPA must target an explicit apps/v1 Deployment")
            require(name not in autoscaled and type(spec.get("minReplicas")) is int and spec["minReplicas"] >= 2 and type(spec.get("maxReplicas")) is int and spec["maxReplicas"] >= spec["minReplicas"], "HPA must preserve at least two available deployment replicas")
            autoscaled[name] = spec
    allowed = {"Namespace", "Deployment", "Service", "ServiceAccount", "ConfigMap", "SecretProviderClass", "NetworkPolicy", "PodDisruptionBudget", "HorizontalPodAutoscaler", "Ingress", "CronJob"}
    kinds = set()
    identities = set()
    deployments = set()
    for document in documents:
        require(isinstance(document, dict), "Invalid manifest document")
        kind = document.get("kind")
        kinds.add(kind)
        require(kind in allowed, "Manifest kind is not allowed; Secrets, RBAC and persistent volumes require separate provisioning")
        metadata = document.get("metadata", {})
        require(isinstance(metadata.get("name"), str) and re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", metadata["name"]) is not None, "Manifest object must have a plain Kubernetes name")
        if kind == "Namespace":
            require(metadata["name"] == "litellm", "Only target litellm namespace is allowed")
        else:
            require(metadata.get("namespace") == "litellm", "Target objects must explicitly select litellm namespace")
        identity = (kind, metadata["name"])
        require(identity not in identities, "Duplicate manifest object")
        identities.add(identity)
        reviewed_document = copy.deepcopy(document)
        if kind == "Deployment" and "application" in config:
            from LiteLLM.runtime.azure_postgresql import database_url_template
            for container in reviewed_document.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
                for environment in container.get("env", []):
                    if environment["name"] == "AZURE_DATABASE_URL_TEMPLATE":
                        parsed, _query = database_url_template(environment.get("value"))
                        require(parsed.username == "llmgw_app" and container.get("image") == config["application"]["backendImage"], "Database template requires the approved Azure application image and role")
                        environment["value"] = "validated-passwordless-template"
        serialized = json.dumps(reviewed_document)
        forbidden = r"REPLACE_|example\.(com|net|org)|<[^>]+>|postgresql://|rediss?://|sk-[A-Za-z0-9]"
        if deployment_mode(config) == "migration":
            forbidden += r"|postgres\." + re.escape(config["legacy"]["namespace"]) + r"\.svc"
        require(not re.search(forbidden, serialized, re.I), "Manifest contains placeholders, credential literals or legacy database references")
        if kind == "Service":
            require(document.get("spec", {}).get("type", "ClusterIP") == "ClusterIP" and not document["spec"].get("externalIPs"), "Application Services must remain private ClusterIP")
        if kind == "SecretProviderClass":
            require(metadata["name"] != "llm-api-auth", "API admission no longer uses an internal credential provider")
        if kind == "Ingress":
            require(stage >= 7, "Stage 6 cannot publish an ingress")
            spec = document["spec"]
            require(spec.get("ingressClassName") in {os.environ.get("PRIVATE_API_INGRESS_CLASS") or "llm-api-private", os.environ.get("PRIVATE_ADMIN_INGRESS_CLASS") or "llm-admin-private"}, "Ingress must use a reviewed private controller")
            hosts = {rule["host"] for rule in spec.get("rules", [])}
            require(hosts and hosts.issubset({f"llm-api.{config['baseDomain']}", f"llm-admin.{config['baseDomain']}"}), "Ingress host outside approved gateway domains")
        if kind not in {"Deployment", "CronJob"}:
            continue
        spec = document["spec"]
        if kind == "Deployment":
            deployments.add(metadata["name"])
            if metadata["name"] in autoscaled:
                require("replicas" not in spec, "Autoscaled deployments must leave replicas under HPA ownership")
            else:
                require(type(spec.get("replicas")) is int and spec["replicas"] >= 2, "Target deployments require at least two replicas")
            pod = spec["template"]["spec"]
        else:
            pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        require(not any(pod.get(option) for option in ("hostNetwork", "hostPID", "hostIPC")), "Host access is forbidden")
        require(pod.get("automountServiceAccountToken") is False, "Disable automatic Kubernetes API credentials")
        require(pod.get("securityContext", {}).get("runAsNonRoot") is True, "Non-root pod security is required")
        require(not any("hostPath" in volume for volume in pod.get("volumes", [])), "hostPath is forbidden")
        require(all(pod.get("nodeSelector", {}).get(key) == value for key, value in USER_POOL_SELECTOR.items()), "Workloads must select the user node pool (nodeSelector workload=litellm)")
        if kind == "Deployment" and metadata["name"] == "llm-api-proxy":
            require(not any("csi" in volume or "secret" in volume for volume in pod.get("volumes", [])), "API admission must not mount internal credentials")
        for container in pod.get("containers", []) + pod.get("initContainers", []):
            require(re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", container.get("image", "")) is not None, "All images must be digest pinned")
            security = container.get("securityContext", {})
            require(security.get("allowPrivilegeEscalation") is False and security.get("readOnlyRootFilesystem") is True and not security.get("privileged") and "ALL" in security.get("capabilities", {}).get("drop", []), "Container security hardening is incomplete")
            require(not container.get("envFrom"), "Use individually scoped Secret references")
            require(container.get("resources", {}).get("requests") and container.get("resources", {}).get("limits"), "Container resource requests and limits are required")
            for environment in container.get("env", []):
                if "application" in config and container["image"] == config["application"]["backendImage"]:
                    if environment["name"] == "AZURE_DATABASE_URL_TEMPLATE":
                        continue
                    if environment["name"] == "LLMGW_BACKEND_SECRETS_DIR" and environment.get("value") == "/mnt/backend-secrets":
                        continue
                if re.search(r"PASSWORD|SECRET|TOKEN|MASTER_KEY|SALT_KEY|DATABASE_URL|API_KEY", environment["name"], re.I):
                    require("value" not in environment, "Sensitive environment values must use Secret references")
            if kind == "Deployment" and container in pod.get("containers", []):
                require(all(probe in container for probe in ("startupProbe", "readinessProbe", "livenessProbe")), "Deployment container health probes are required")
    require({"Deployment", "NetworkPolicy", "PodDisruptionBudget", "SecretProviderClass"}.issubset(kinds), "Required isolation, availability and secret provider resources are missing")
    from scripts.backend_manifest import application_authentication
    native_gateway = "application" in config and application_authentication(config)["mode"] == "native"
    expected = {"litellm"} if stage == 6 or native_gateway else {"litellm", "llm-api-proxy", "llm-admin-proxy"}
    require(expected.issubset(deployments), "Stage workload set is incomplete")


def publish(config, stage, action, operation, revision, directory, approved, image_public_key=None):
    validate_action(config, stage, action)
    kube = connect_cluster(config, directory, legacy=action == "legacy-hardening")
    if action == "cluster-bootstrap":
        documents = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}} for name in ("litellm", "llm-api-ingress", "llm-admin-ingress")]
        path = directory / "workload.yaml"
        private_write(path, yaml.safe_dump_all(documents))
        run_command([*kube, "apply", "--server-side", "--field-manager=llmgw-migration", "--dry-run=server", "-f", str(path)], directory, "namespace-dry-run")
        plan_hash = fingerprint({"action": action, "revision": revision, "config": stage_fingerprint(config, stage), "documents": documents})
    elif action == "legacy-hardening":
        require(stage == 1, "Legacy hardening belongs to stage 1")
        snapshots = [json.loads(run_command([*kube, "get", "deployment", name, "-o", "json"], directory, f"before-{name}")) for name in ("postgres", "litellm-mi-proxy")]
        desired = [{"name": item["metadata"]["name"], "uid": item["metadata"]["uid"], "before": item["spec"], "after": hardened_spec(item)} for item in snapshots]
        plan_hash = fingerprint({"revision": revision, "config": stage_fingerprint(config, stage), "action": action, "changes": desired})
        private_write(directory / "runtime-review.json", json.dumps({"planSha256": plan_hash, "changes": [{"name": change["name"], "strategy": change["after"].get("strategy"), "probes": [{"container": container["name"], **{key: container.get(key) for key in ("startupProbe", "readinessProbe", "livenessProbe")}} for container in change["after"]["template"]["spec"]["containers"]]} for change in desired]}, indent=2) + "\n")
    else:
        require(stage in {6, 7, 8}, "Application publishing belongs to stages 6-8")
        window = run_command([*kube, "get", "configmap", "llmgw-audit-recovery-window", "--ignore-not-found", "-o", "name"], directory, "audit-window-check")
        require(not window.strip(), "Complete the approved audit-resume workflow before application publishing")
        manifest = os.environ.get("MIGRATION_MANIFEST_YAML", "")
        if "application" in config:
            require(not manifest, "Managed application generation cannot be mixed with manual manifests")
            from scripts.backend_manifest import application_authentication, prepare_backend_documents
            native_gateway = application_authentication(config)["mode"] == "native"
            require(stage == 6 or (not native_gateway and stage == 7 and "proxy" in config) or (stage == 8 and ((native_gateway and "contentAudit" in config) or ("proxy" in config and ("auditRuntime" in config or "contentAudit" in config)))), "Managed application generation requires its approved authentication and audit configuration")
            if native_gateway and stage == 6:
                for proxy in ("llm-api-proxy", "llm-admin-proxy"):
                    existing = run_command([*kube, "get", "deployment", proxy, "--ignore-not-found", "-o", "json"], directory, "existing-entra-proxy-" + proxy)
                    require(not existing.strip(), "Remove or explicitly migrate existing Entra proxy workloads before native Stage 6 publication")
            documents = prepare_backend_documents(config, revision, directory, AzureCommands(config, directory), image_public_key=image_public_key)
            if stage >= 7 and not native_gateway:
                from scripts.proxy_manifest import prepare_proxy_documents
                documents = [*documents, *prepare_proxy_documents(config, revision, directory, AzureCommands(config, directory), image_public_key=image_public_key)]
            if stage == 8:
                if "contentAudit" in config:
                    from scripts.native_audit import prepare_native_audit
                    documents = prepare_native_audit(config, documents, directory, AzureCommands(config, directory), kube, image_public_key=image_public_key, revision=revision)
                else:
                    from scripts.audit_manifest import prepare_audit_documents
                    documents = prepare_audit_documents(config, revision, directory, AzureCommands(config, directory), documents, kube, image_public_key=image_public_key)
        else:
            documents = [document for document in yaml.safe_load_all(manifest) if document]
        if stage >= 7 and not ("application" in config and application_authentication(config)["mode"] == "native"):
            from scripts.edge_binding import preserve_binding
            existing_bindings = {plane: json.loads(run_command([*kube, "get", "deployment", f"llm-{plane}-proxy", "--ignore-not-found", "-o", "json"], directory, f"existing-{plane}-edge-binding") or "null") for plane in ("api", "admin")}
            documents = preserve_binding(documents, existing_bindings)
        check_application(documents, stage, config)
        path = directory / "workload.yaml"
        private_write(path, yaml.safe_dump_all(documents, sort_keys=False))
        dry = run_command([*kube, "apply", "--server-side", "--field-manager=llmgw-migration", "--dry-run=server", "-f", str(path), "-o", "json"], directory, "server-dry-run")
        require(bool(dry.strip()), "Server dry-run returned no objects")
        live = []
        for document in documents:
            live.append(json.loads(run_command([*kube, "get", document["kind"], document["metadata"]["name"], "--ignore-not-found", "-o", "json"], directory, "live-" + document["kind"] + "-" + document["metadata"]["name"]) or "null"))
        plan_contract = {"revision": revision, "config": stage_fingerprint(config, stage), "action": action, "documents": documents, "live": [{"uid": item["metadata"]["uid"], "spec": item.get("spec"), "data": item.get("data"), "annotations": item["metadata"].get("annotations")} if item else None for item in live]}
        if image_public_key is not None:
            plan_contract["imageTrust"] = {"cosignPublicKeySha256": file_sha256(Path(image_public_key))}
        plan_hash = fingerprint(plan_contract)
    summary = {"stage": stage, "action": action, "planSha256": plan_hash, "revision": revision, "stageAccepted": False}
    if action != "legacy-hardening":
        review = {"planSha256": plan_hash, "desiredObjects": documents}
        if action == "application" and image_public_key is not None:
            review["imageTrust"] = plan_contract["imageTrust"]
        private_write(directory / "runtime-review.json", json.dumps(review, indent=2) + "\n")
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    if operation == "plan":
        return
    require(plan_hash == approved, "Runtime plan changed or was not approved")
    if action == "legacy-hardening":
        for snapshot, change in zip(snapshots, desired):
            patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": snapshot["metadata"]["resourceVersion"]}, {"op": "replace", "path": "/spec", "value": change["after"]}]
            patch_path = directory / "workload-patch.json"
            private_write(patch_path, json.dumps(patch))
            run_command([*kube, "patch", "deployment", change["name"], "--type=json", "--patch-file", str(patch_path)], directory, "patch-" + change["name"])
            run_command([*kube, "rollout", "status", "deployment/" + change["name"], "--timeout=15m"], directory, "ready-" + change["name"])
    else:
        run_command([*kube, "apply", "--server-side", "--field-manager=llmgw-migration", "-f", str(path)], directory, "apply")
        for document in documents:
            if document["kind"] == "Deployment":
                run_command([*kube, "rollout", "status", "deployment/" + document["metadata"]["name"], "--timeout=15m"], directory, "ready-" + document["metadata"]["name"])
        if action == "application" and stage == 6 and "application" in config:
            from scripts.backend_manifest import application_authentication
            if application_authentication(config)["mode"] == "native":
                from scripts.private_ingress_runtime import verify_private_ingress_backends
                verification = verify_private_ingress_backends(config, revision, directory)
                summary["backendRoutesVerified"] = verification["backendRoutesVerified"]
    summary["applied"] = True
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")


def monitoring_onboard(config, stage, operation, revision, directory, approved):
    validate_action(config, stage, "monitoring-onboard")
    azure = AzureCommands(config, directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope mismatch")
    legacy = stage == 1
    group = config["legacy" if legacy else "target"]["resourceGroup"]
    cluster = config["legacy"]["aksClusterName"] if legacy else config["parameters"]["platform"]["stage4Aks"]["name"]
    workspace = config["parameters"]["monitoring" if legacy else "platform"]["logAnalyticsWorkspaceName"]
    workspace_id = azure.scoped(["monitor", "log-analytics", "workspace", "show", "--resource-group", group, "--workspace-name", workspace, "--query", "id"])
    profile_result = azure.scoped(["aks", "show", "--resource-group", group, "--name", cluster, "--query", "{profile:addonProfiles.omsagent}"])
    profile = profile_result.get("profile") or {}
    existing = profile.get("config", {}).get("logAnalyticsWorkspaceResourceID", "")
    require(not existing or existing.lower() == workspace_id.lower(), "Existing Container Insights destination differs; do not silently redirect telemetry")
    plan = {"action": "monitoring-onboard", "stage": stage, "revision": revision, "configSha256": stage_fingerprint(config, stage), "workspaceId": workspace_id, "before": profile}
    plan_hash = fingerprint(plan)
    summary = {"stage": stage, "action": "monitoring-onboard", "planSha256": plan_hash, "stageAccepted": False}
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2) + "\n")
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    if operation == "execute":
        require(approved == plan_hash, "Monitoring plan changed or was not approved")
        if not profile.get("enabled"):
            azure.scoped(["aks", "enable-addons", "--addons", "monitoring", "--resource-group", group, "--name", cluster, "--workspace-resource-id", workspace_id])
        summary["applied"] = True
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
        print("Container Insights onboarding completed; verify agent health, actual logs and received alerts before accepting.")


def target_database_restore(config, operation, revision, directory, approved):
    validate_action(config, 5, "restore-target")
    azure = AzureCommands(config, directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope mismatch")
    group = config["target"]["resourceGroup"]
    deployed = azure.scoped(["deployment", "group", "show", "--resource-group", group, "--name", deployment_name(config, 5, "platform"), "--query", "{state:properties.provisioningState,platform:properties.outputs.platform.value}"])
    require(deployed.get("state") == "Succeeded" and deployed.get("platform", {}).get("stage5Deployed") is True, "Deploy Stage 5 data infrastructure before restoring")
    server = azure.scoped(["postgres", "flexible-server", "show", "--resource-group", group, "--name", deployed["platform"]["postgresqlServerName"], "--query", "{id:id,host:fullyQualifiedDomainName,auth:authConfig,network:network}"])
    expected_server = group_id(config) + "/providers/Microsoft.DBforPostgreSQL/flexibleServers/" + deployed["platform"]["postgresqlServerName"]
    require(server.get("id", "").lower() == expected_server.lower() and re.fullmatch(r"[a-z0-9-]+\.postgres\.database\.azure\.com", server.get("host", "")), "Restore target differs from the approved PostgreSQL server")
    require(server.get("auth", {}).get("activeDirectoryAuth") == "Enabled" and server.get("auth", {}).get("passwordAuth") == "Disabled" and server.get("network", {}).get("publicNetworkAccess") == "Disabled", "Restore requires private Entra-only PostgreSQL")
    database = config["parameters"]["platform"]["stage5Data"]["postgresqlDatabaseName"]
    require(isinstance(database, str) and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", database) is not None, "Target database must be a plain PostgreSQL database name, never a connection string")
    blob = os.environ.get("MIGRATION_RESTORE_BLOB", "")
    digest = os.environ.get("MIGRATION_BACKUP_SHA256", "")
    username = os.environ.get("POSTGRES_MIGRATION_USER", "")
    if "databaseAccess" in config:
        require(not username or username == "llmgw_migrator", "Managed database roles require the llmgw_migrator restore role")
        username = "llmgw_migrator"
    require(re.fullmatch(r"pre-change/[0-9a-f]{32}\.dump", blob) is not None, "Select the reviewed stage0 pre-change backup blob")
    require(re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "Reviewed backup SHA256 required")
    require(username and configured(username), "POSTGRES_MIGRATION_USER must be the Entra database role granted to the runtime identity")
    plan = {"stage": 5, "action": "restore-target", "revision": revision, "config": stage_fingerprint(config, 5), "server": server, "database": database, "username": username, "blob": blob, "backupSha256": digest}
    plan_hash = fingerprint(plan)
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2) + "\n")
    summary = {"stage": 5, "action": "restore-target", "planSha256": plan_hash, "backupSha256": digest, "stageAccepted": False}
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    if operation == "plan":
        return
    require(approved == plan_hash, "Restore plan changed or was not approved")
    storage = azure.scoped(["deployment", "group", "show", "--resource-group", group, "--name", deployment_name(config, 0, "backup"), "--query", "properties.outputs.backupStorage.value"])
    backup = directory / "database.dump"
    azure.scoped(["storage", "blob", "download", "--auth-mode", "login", "--account-name", storage["storageAccountName"], "--container-name", storage["containerName"], "--name", blob, "--file", str(backup), "--overwrite", "false"])
    backup.chmod(0o600)
    require(file_sha256(backup) == digest, "Downloaded backup SHA256 mismatch")
    token_result = subprocess.run(["az", "account", "get-access-token", "--subscription", config["azure"]["subscriptionId"], "--resource-type", "oss-rdbms", "--query", "accessToken", "--output", "tsv"], capture_output=True, text=True, check=False)
    require(token_result.returncode == 0 and bool(token_result.stdout.strip()), "Unable to acquire PostgreSQL Entra token")
    require(Path(POSTGRES_CA_BUNDLE).is_file(), "PostgreSQL system CA bundle is missing from the Runner")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment.update(PGHOST=server["host"], PGPORT="5432", PGDATABASE=database, PGUSER=username, PGPASSWORD=token_result.stdout.strip(), PGSSLMODE="verify-full", PGSSLROOTCERT=POSTGRES_CA_BUNDLE)
    empty_query = "SELECT (SELECT count(*) FROM pg_catalog.pg_class JOIN pg_catalog.pg_namespace ON pg_namespace.oid = pg_class.relnamespace WHERE nspname = 'public') + (SELECT count(*) FROM pg_catalog.pg_proc JOIN pg_catalog.pg_namespace ON pg_namespace.oid = pg_proc.pronamespace WHERE nspname = 'public') + (SELECT count(*) FROM pg_catalog.pg_type JOIN pg_catalog.pg_namespace ON pg_namespace.oid = pg_type.typnamespace WHERE nspname = 'public') + (SELECT count(*) FROM pg_catalog.pg_namespace WHERE nspname NOT IN ('public', 'information_schema') AND nspname NOT LIKE 'pg_%');"
    tables = run_command(["psql", "--no-psqlrc", "--no-password", "--set", "ON_ERROR_STOP=1", "-At", "-c", empty_query], directory, "target-empty-check", environment=environment).strip()
    require(tables == "0", "Target database is not empty; refusing to overwrite or clean it")
    run_command(["pg_restore", "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl", "--no-password", "--dbname", database, str(backup)], directory, "target-restore", environment=environment)
    summary["restored"] = True
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    print("Restore into the new database completed; grant application roles and verify migration, encryption and token renewal before acceptance.")


def backup_restore(config, revision, directory):
    validate_action(config, 0, "backup-restore")
    image = os.environ.get("POSTGRES_RESTORE_IMAGE", "")
    require(re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image) is not None, "POSTGRES_RESTORE_IMAGE must pin the approved matching PostgreSQL restore image digest")
    kube = connect_cluster(config, directory, legacy=True)
    inventory = json.loads(run_command([*kube, "get", "deployment", "postgres", "litellm-mi-proxy", "-o", "json"], directory, "legacy-inventory"))
    postgres = next(item for item in inventory["items"] if item["metadata"]["name"] == "postgres")
    require(any(volume.get("persistentVolumeClaim", {}).get("claimName") == config["legacy"]["postgresPvc"] for volume in postgres["spec"]["template"]["spec"].get("volumes", [])), "Postgres does not mount the configured legacy PVC")
    identifier = uuid4().hex
    remote = f"/tmp/llmgw-{identifier}.dump"
    backup = directory / "database.dump"
    report = draft_report(config, 0, revision)
    report["observedAt"] = datetime.now(timezone.utc).isoformat()
    report["observations"] = {"legacyWorkloads": [{"name": item["metadata"]["name"], "images": [container["image"] for container in item["spec"]["template"]["spec"]["containers"]]} for item in inventory["items"]]}
    pod_list = json.loads(run_command([*kube, "get", "pods", "-l", "app=postgres", "-o", "json"], directory, "postgres-pods"))["items"]
    pods = [pod for pod in pod_list if pod.get("status", {}).get("phase") == "Running" and not pod["metadata"].get("deletionTimestamp")]
    require(len(pods) == 1, "Exactly one running legacy Postgres pod is required")
    pod = pods[0]["metadata"]["name"]
    container_name = "llmgw-restore-" + identifier
    started = False
    try:
        run_command([*kube, "exec", pod, "-c", "postgres", "--", "sh", "-c", 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$1"', "sh", remote], directory, "pg-dump")
        run_command([*kube, "cp", f"{pod}:{remote}", str(backup), "-c", "postgres"], directory, "copy-backup")
        backup.chmod(0o600)
        require(backup.stat().st_size > 0, "Database dump is empty")
        digest = file_sha256(backup)
        run_command(["docker", "pull", image], directory, "restore-image")
        run_command(["docker", "run", "--detach", "--name", container_name, "--network", "none", "--memory", "4g", "--env", "POSTGRES_HOST_AUTH_METHOD=trust", image], directory, "restore-start")
        started = True
        run_command(["docker", "exec", container_name, "sh", "-c", "for attempt in $(seq 1 120); do if [ \"$(cat /proc/1/comm 2>/dev/null)\" = postgres ] && pg_isready -U postgres >/dev/null 2>&1; then exit 0; fi; sleep 1; done; exit 1"], directory, "restore-ready")
        run_command(["docker", "cp", str(backup), f"{container_name}:/tmp/database.dump"], directory, "restore-copy")
        started_at = time.monotonic()
        run_command(["docker", "exec", container_name, "pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "-U", "postgres", "-d", "postgres", "/tmp/database.dump"], directory, "pg-restore")
        tables = run_command(["docker", "exec", container_name, "psql", "-U", "postgres", "-d", "postgres", "-At", "-c", "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';"], directory, "restored-tables").strip()
        require(tables.isdigit() and int(tables) > 0, "No public tables found after restore")
        azure = AzureCommands(config, directory)
        storage = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 0, "backup"), "--query", "properties.outputs.backupStorage.value"])
        blob = f"pre-change/{identifier}.dump"
        azure.scoped(["storage", "blob", "upload", "--auth-mode", "login", "--account-name", storage["storageAccountName"], "--container-name", storage["containerName"], "--name", blob, "--file", str(backup), "--overwrite", "false"])
        downloaded = directory / "downloaded.dump"
        azure.scoped(["storage", "blob", "download", "--auth-mode", "login", "--account-name", storage["storageAccountName"], "--container-name", storage["containerName"], "--name", blob, "--file", str(downloaded), "--overwrite", "false"])
        downloaded.chmod(0o600)
        require(file_sha256(downloaded) == digest, "Stored backup download did not match the original dump")
        report["observations"].update(backupSha256=digest, backupBytes=backup.stat().st_size, backupAccount=storage["storageAccountName"], backupContainer=storage["containerName"], backupBlob=blob, fullRestoreSucceeded=True, publicTableCount=int(tables), restoreSeconds=round(time.monotonic() - started_at, 2))
        report["checks"]["backup_restore"]["evidence"] = "Automated full restore and private upload completed; review row counts, roles/ACLs, encryption and application behavior before accepting."
        private_write(directory / "acceptance-report.json", json.dumps(report, indent=2) + "\n")
        print("Backup uploaded and restored in an isolated container. Acceptance report remains pending until all checks are reviewed.")
    except Exception:
        if started:
            capture_restore_container_diagnostics(container_name, directory)
        raise
    finally:
        subprocess.run([*kube, "exec", pod, "-c", "postgres", "--", "rm", "-f", remote], capture_output=True, check=False)
        if started:
            subprocess.run(["docker", "rm", "--force", "--volumes", container_name], capture_output=True, check=False)


def validate_action(config, stage, action):
    allowed_stages = {"backup-restore": {0}, "legacy-hardening": {1}, "cluster-bootstrap": {4}, "private-ingress": {4}, "monitoring-onboard": {1, 4}, "database-roles": {5}, "backend-secrets": {5}, "restore-target": {5}, "schema-migrate": {5}, "entra-apps": {7}, "entra-access": {7}, "admin-credentials": {7}, "admin-credentials-rotate": {7}, "admin-credentials-recover": {7}, "proxy-credentials": {7}, "application": {6, 7, 8}}
    allowed_stages.update({action: {8} for action in ("audit-pause", "audit-recover", "audit-resume")})
    allowed_stages["entra-revoke"] = {7}
    allowed_stages.update({action: {7} for action in ("admin-credentials-session-rotate", "admin-credentials-retire-expired")})
    allowed_stages["certificate-renew"] = {4}
    allowed_stages["edge-bind"] = {9}
    allowed_stages["edge-certificates"] = {9}
    allowed_stages.update({action: {9} for action in ("dns-publish", "dns-rollback")})
    allowed_stages.update({action: {1} for action in ("legacy-access-restrict", "legacy-access-restore")})
    require(stage in active_stages(config), "Stage does not apply to the selected deployment mode")
    require(stage in allowed_stages.get(action, set()), "Action does not belong to the selected stage")
    require(deployment_mode(config) != "greenfield" or action not in {"backup-restore", "legacy-hardening", "legacy-access-restrict", "legacy-access-restore", "restore-target"}, "Legacy operations do not apply to greenfield")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, choices=range(10), required=True)
    parser.add_argument("--environment", choices=("dev", "test", "prod"), required=True)
    parser.add_argument("--action", choices=("backup-restore", "legacy-hardening", "legacy-access-restrict", "legacy-access-restore", "cluster-bootstrap", "private-ingress", "certificate-renew", "monitoring-onboard", "database-roles", "backend-secrets", "restore-target", "schema-migrate", "entra-apps", "entra-access", "entra-revoke", "admin-credentials", "admin-credentials-rotate", "admin-credentials-recover", "admin-credentials-session-rotate", "admin-credentials-retire-expired", "proxy-credentials", "application", "audit-pause", "audit-recover", "audit-resume", "dns-publish", "dns-rollback", "edge-bind"), required=True)
    parser.add_argument("--operation", choices=("plan", "execute"), required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "temp/migration-runtime")
    args = parser.parse_args()
    config = validate_config(json.loads(os.environ["CUSTOMER_CONFIG_JSON"]), args.environment)
    revision = os.environ.get("GITHUB_SHA", os.environ.get("MIGRATION_REVISION", ""))
    require(re.fullmatch(r"[0-9a-f]{40}", revision or "") is not None, "Reviewed Git revision required")
    validate_action(config, args.stage, args.action)
    directory = args.output_dir.resolve()
    require(directory.is_relative_to((ROOT / "temp").resolve()) and directory != (ROOT / "temp").resolve(), "Runtime output must stay under temp/")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    from scripts.workflow_artifacts import approved_operation, load_evidence, write_operation_receipt
    for filename in ("operation-receipt.json", "runtime-summary.json", "runtime-review.json"):
        (directory / filename).unlink(missing_ok=True)
    if args.operation == "execute":
        require(os.environ.get("MIGRATION_CONFIRM_ENVIRONMENT") == args.environment, "Explicit environment confirmation required")
        previous = load_evidence(config, args.stage, revision) if os.environ.get("MIGRATION_AUTO_EVIDENCE") == "true" else json.loads(os.environ.get("MIGRATION_EVIDENCE_JSON") or "[]")
        validate_evidence(previous, args.stage, config, revision)
        if args.action not in {"audit-pause", "audit-recover", "audit-resume", "backup-restore"} and os.environ.get("MIGRATION_APPROVED_RUN_ID"):
            os.environ["MIGRATION_APPROVED_PLAN_SHA256"] = approved_operation(config, revision, args.stage, args.action, os.environ["MIGRATION_APPROVED_RUN_ID"], "runtime")
    if args.action == "backup-restore":
        require(args.operation == "execute", "Backup/restore is an explicit operation, not a What-if; use the acceptance draft to review its scope")
        backup_restore(config, revision, directory)
    elif args.action in {"audit-pause", "audit-recover", "audit-resume"}:
        from scripts.audit_plan import approved_audit_plan
        from scripts.audit_runtime import audit_operation
        approved = ""
        if args.operation == "execute":
            reviewed, approved = approved_audit_plan(config, args.action, revision, os.environ.get("MIGRATION_APPROVED_RUN_ID", ""))
            os.environ["MIGRATION_AUDIT_PLAN_JSON"] = json.dumps(reviewed)
            if args.action == "audit-recover":
                os.environ["AUDIT_RECOVERY_CURSOR"] = reviewed.get("cursor") or ""
        elif args.action == "audit-recover":
            os.environ["AUDIT_RECOVERY_CURSOR"] = ""
            if os.environ.get("AUDIT_CONTINUE_RUN_ID"):
                previous, _ = approved_audit_plan(config, args.action, revision, os.environ["AUDIT_CONTINUE_RUN_ID"])
                require(previous["context"]["scope"]["configSha256"] == stage_fingerprint(config, 8), "Previous recovery page belongs to another configuration")
                cursor = previous["recovery"]["summary"].get("nextCursor")
                require(isinstance(cursor, str) and cursor, "Previous recovery page has no continuation")
                os.environ["AUDIT_RECOVERY_CURSOR"] = cursor
        audit_operation(config, args.action, args.operation, revision, directory, approved)
    elif args.action == "monitoring-onboard":
        monitoring_onboard(config, args.stage, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action in {"legacy-access-restrict", "legacy-access-restore"}:
        from scripts.legacy_access import access_operation
        access_operation(config, args.action, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action == "restore-target":
        target_database_restore(config, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action == "private-ingress":
        from scripts.private_ingress_runtime import deploy_private_ingress
        deploy_private_ingress(config, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action == "certificate-renew":
        from scripts.certificate_runtime import certificate_operation
        certificate_operation(config, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action == "edge-bind":
        from scripts.edge_binding import bind_edge
        bind_edge(config, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action == "database-roles":
        from scripts.database_roles import provision_database_roles
        provision_database_roles(config, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action == "schema-migrate":
        from scripts.schema_runtime import migrate_schema
        migrate_schema(config, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action == "backend-secrets":
        from scripts.runtime_secrets import initialize_backend_secrets
        initialize_backend_secrets(config, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action == "entra-apps":
        from scripts.entra_apps import initialize_entra_apps
        initialize_entra_apps(config, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action == "entra-access":
        from scripts.entra_access import initialize_entra_access
        initialize_entra_access(config, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action == "entra-revoke":
        from scripts.entra_revoke import initialize_entra_revoke
        initialize_entra_revoke(config, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action in {"dns-publish", "dns-rollback"}:
        from scripts.dns_runtime import change_dns
        change_dns(config, args.action, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    elif args.action in {"admin-credentials", "admin-credentials-rotate", "admin-credentials-recover", "admin-credentials-session-rotate", "admin-credentials-retire-expired"}:
        from scripts.admin_credentials import initialize_admin_credentials
        initialize_admin_credentials(config, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""), action=args.action)
    elif args.action == "proxy-credentials":
        from scripts.proxy_credentials_runtime import initialize_proxy_credentials
        initialize_proxy_credentials(config, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    else:
        publish(config, args.stage, args.action, args.operation, revision, directory, os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", ""))
    if args.action != "backup-restore":
        write_operation_receipt(directory, config, revision, args.stage, args.action, args.operation, "runtime")


if __name__ == "__main__":
    try:
        main()
    except MigrationError as error:
        raise SystemExit(diagnostic_exit(error, "migration-runtime")) from None
    except Exception as error:
        raise SystemExit(diagnostic_exit(error, "migration-runtime", "Runtime operation failed")) from None