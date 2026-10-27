"""Run migration Stage 0 through Stage 9 from a local customer configuration."""

import argparse
import base64
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from uuid import uuid4

import yaml

from scripts.customer_migration import (
    ROOT,
    MigrationError,
    deployment_mode,
    fingerprint,
    parameters_for,
    private_write,
    require,
    validate_config,
)
from scripts.migration_deploy import AzureCommands, deploy_component, deployment_name
from scripts.workflow_diagnostics import command_failure_summary, diagnostic_exit, exception_diagnostic


INFRASTRUCTURE_STEPS = {
    "bootstrap": (0, "bootstrap"),
    "network": (0, "network"),
    "backup": (0, "backup"),
    "execution-host-connectivity": (0, "runner-connectivity"),
    "legacy-logging": (1, "legacy-logging"),
    "monitoring": (1, "monitoring"),
    "stage3-platform": (3, "platform"),
    "stage4-platform": (4, "platform"),
    "stage4-certificate-vault": (4, "certificate-vault"),
    "stage4-target-connectivity": (4, "runner-target-connectivity"),
    "stage4-aks-ingress-role": (4, "aks-ingress-role"),
    "stage5-platform": (5, "platform"),
    "stage7-proxy-foundation": (7, "proxy-foundation"),
    "stage8-audit-foundation": (8, "audit-foundation"),
    "stage8-audit-storage": (8, "audit"),
    "stage8-observability": (8, "observability"),
    "stage9-origin": (9, "origin"),
    "stage9-edge-prepare": (9, "edge"),
}
RUNTIME_STEPS = {
    "monitoring-onboard": (1, "monitoring-onboard"),
    "legacy-hardening": (1, "legacy-hardening"),
    "legacy-access-restrict": (1, "legacy-access-restrict"),
    "legacy-access-restore": (1, "legacy-access-restore"),
    "stage4-cluster-bootstrap": (4, "cluster-bootstrap"),
    "stage4-monitoring-onboard": (4, "monitoring-onboard"),
    "stage4-certificate-renew": (4, "certificate-renew"),
    "stage4-private-ingress": (4, "private-ingress"),
    "stage5-database-roles": (5, "database-roles"),
    "stage5-backend-secrets": (5, "backend-secrets"),
    "stage5-restore-target": (5, "restore-target"),
    "stage5-schema-migrate": (5, "schema-migrate"),
    "stage6-application": (6, "application"),
    "stage7-entra-apps": (7, "entra-apps"),
    "stage7-entra-access": (7, "entra-access"),
    "stage7-entra-revoke": (7, "entra-revoke"),
    "stage7-admin-credentials": (7, "admin-credentials"),
    "stage7-admin-credentials-rotate": (7, "admin-credentials-rotate"),
    "stage7-admin-credentials-recover": (7, "admin-credentials-recover"),
    "stage7-admin-session-rotate": (7, "admin-credentials-session-rotate"),
    "stage7-admin-credentials-retire": (7, "admin-credentials-retire-expired"),
    "stage7-proxy-credentials": (7, "proxy-credentials"),
    "stage7-application": (7, "application"),
    "stage8-application": (8, "application"),
    "stage8-audit-pause": (8, "audit-pause"),
    "stage8-audit-recover": (8, "audit-recover"),
    "stage8-audit-resume": (8, "audit-resume"),
    "stage9-edge-certificates": (9, "edge-certificates"),
    "stage9-edge-bind": (9, "edge-bind"),
    "stage9-dns-publish": (9, "dns-publish"),
    "stage9-dns-rollback": (9, "dns-rollback"),
}
IMAGE_STEPS = {
    "stage4-promote-backend-image": "backend",
    "stage7-promote-proxy-image": "proxy",
    "stage8-promote-collector-image": "collector",
}
STEPS = (
    "config-check",
    "bootstrap",
    "network",
    "backup",
    "execution-host-connectivity",
    "connectivity-check",
    "backup-restore",
    "legacy-logging",
    "monitoring-onboard",
    "monitoring",
    "legacy-hardening",
    "legacy-access-restrict",
    "legacy-access-restore",
    "stage2-decisions",
    "stage3-source-check",
    "stage4-target-check",
    *tuple(IMAGE_STEPS),
    *tuple(key for key in INFRASTRUCTURE_STEPS if key.startswith("stage")),
    *tuple(key for key in RUNTIME_STEPS if key.startswith("stage")),
    "stage9-edge-release",
)
LOCAL_TOOLS = ("az", "kubectl", "kubelogin", "docker", "curl", "getent")
AUTHENTICATION_PROFILES = {"default", "deploy", "runtime", "database", "certificate", "entraBootstrap", "entraAccess"}
LOCAL_SETTING_FIELDS = {"postgresRestoreImage", "executionHost", "authentication", "features", "runtimeInputs", "releaseReportPath", "imageSigning"}


def reviewed_revision(run=subprocess.run):
    result = run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False)
    revision = result.stdout.strip()
    require(result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "Run local Stage0-9 from a Git checkout with a full revision")
    return revision


def local_settings(document):
    settings = document.get("localExecution")
    required = {"executionHost"} | ({"postgresRestoreImage"} if deployment_mode(document) == "migration" else set())
    require(isinstance(settings, dict) and required.issubset(settings) and not set(settings) - LOCAL_SETTING_FIELDS, "localExecution has unexpected or missing fields")
    settings = copy.deepcopy(settings)
    image = settings.get("postgresRestoreImage")
    require(image is None or isinstance(image, str) and re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image) is not None, "localExecution.postgresRestoreImage must pin a full image digest")
    host = settings["executionHost"]
    require(isinstance(host, dict) and set(host) == {"virtualNetworkId", "managePeering", "manageBlobDnsLink"}, "localExecution.executionHost has unexpected or missing fields")
    require(isinstance(host["virtualNetworkId"], str) and host["virtualNetworkId"], "Configure the execution host VNet resource ID")
    require(type(host["managePeering"]) is bool and type(host["manageBlobDnsLink"]) is bool, "Execution host connectivity switches must be booleans")
    authentication = settings.setdefault("authentication", {"default": {"method": "existing"}})
    require(isinstance(authentication, dict) and authentication and not set(authentication) - AUTHENTICATION_PROFILES, "localExecution.authentication contains an unknown identity profile")
    for name, profile in authentication.items():
        require(isinstance(profile, dict) and set(profile).issubset({"method", "clientId"}) and "method" in profile, f"Invalid local Azure authentication profile: {name}")
        require(profile["method"] in {"existing", "managed-identity"}, f"Unsupported local Azure authentication method: {name}")
        client_id = profile.get("clientId")
        require(client_id is None or re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", client_id) is not None, f"Invalid Azure client ID in local authentication profile: {name}")
        require(profile["method"] != "managed-identity" or client_id is not None, f"Managed identity profile requires clientId: {name}")
    features = settings.setdefault("features", {"entraMode": "deferred", "allowTrafficRelease": False})
    require(isinstance(features, dict) and set(features) == {"entraMode", "allowTrafficRelease"}, "localExecution.features requires entraMode and allowTrafficRelease")
    require(features["entraMode"] in {"enabled", "deferred"} and type(features["allowTrafficRelease"]) is bool, "Invalid local feature decision")
    runtime_inputs = settings.setdefault("runtimeInputs", {})
    require(isinstance(runtime_inputs, dict) and not set(runtime_inputs) - {"backupBlob", "backupSha256", "postgresMigrationUser", "legacySaltSource", "privateApiIngressClass", "privateAdminIngressClass", "auditRecoveryCursor"}, "localExecution.runtimeInputs contains unknown fields")
    require("legacySaltSource" not in runtime_inputs or runtime_inputs["legacySaltSource"] in {"secret", "master-key"}, "localExecution.runtimeInputs.legacySaltSource must be secret or master-key")
    if deployment_mode(document) == "greenfield":
        migration_inputs = {"backupBlob", "backupSha256", "postgresMigrationUser", "legacySaltSource"}
        require(not migration_inputs & runtime_inputs.keys(), "Greenfield local execution cannot contain migration-only runtime inputs")
    for field in ("releaseReportPath", "imageSigning"):
        require(field not in settings or settings[field] is not None, f"localExecution.{field} cannot be null")
    if "releaseReportPath" in settings:
        require(isinstance(settings["releaseReportPath"], str) and settings["releaseReportPath"], "localExecution.releaseReportPath must be a file path")
    if "imageSigning" in settings:
        signing = settings["imageSigning"]
        allowed_signing = {"privateKeyPath", "publicKeyPath", "backendTargetTag", "proxyTargetTag", "collectorTargetTag"}
        require(isinstance(signing, dict) and not set(signing) - allowed_signing and all(isinstance(value, str) and value for value in signing.values()), "localExecution.imageSigning contains invalid fields")
    return settings


def authentication_profile(settings, name):
    profiles = settings["authentication"]
    profile = profiles.get(name, profiles.get("default"))
    require(profile is not None, f"Configure local Azure authentication profile: {name}")
    return profile


def authentication_profile_for_step(step):
    if step in INFRASTRUCTURE_STEPS or step in IMAGE_STEPS or step == "stage9-edge-release":
        return "deploy"
    if step in {"config-check", "stage2-decisions", "stage3-source-check"}:
        return None
    if step in RUNTIME_STEPS:
        action = RUNTIME_STEPS[step][1]
        if action == "database-roles":
            return "database"
        if action in {"certificate-renew", "edge-certificates"}:
            return "certificate"
        if action in {"entra-apps", "admin-credentials", "admin-credentials-rotate", "admin-credentials-recover", "admin-credentials-session-rotate", "admin-credentials-retire-expired"}:
            return "entraBootstrap"
        if action in {"entra-access", "entra-revoke"}:
            return "entraAccess"
    return "runtime"


def authenticate_azure(config, settings, profile_name, destination, run=subprocess.run):
    profile = authentication_profile(settings, profile_name)

    def execute(arguments, label, *, sensitive_stdout=False):
        try:
            completed = run(arguments, capture_output=True, text=True, check=False, timeout=120)
        except OSError:
            raise MigrationError(f"Unable to run local Azure authentication command: {label}") from None
        private_write(destination / f"azure-auth-{label}.stderr.txt", completed.stderr)
        safe_stdout = "" if sensitive_stdout else completed.stdout
        require(completed.returncode == 0, f"Local Azure authentication failed ({label}): {command_failure_summary(safe_stdout, completed.stderr, completed.returncode)}")
        return completed.stdout

    if profile["method"] == "managed-identity":
        execute(["az", "login", "--identity", "--client-id", profile["clientId"], "--output", "none", "--only-show-errors"], "login")
    execute(["az", "account", "set", "--subscription", config["azure"]["subscriptionId"], "--only-show-errors"], "subscription")
    output = execute([
        "az", "account", "show", "--query", "{tenantId:tenantId,id:id}",
        "--output", "json", "--only-show-errors",
    ], "scope")
    try:
        account = json.loads(output)
    except ValueError:
        raise MigrationError("Local Azure account check returned invalid JSON") from None
    expected = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}
    require(account == expected, "Local Azure login scope does not match customer configuration")
    summary = {"profile": profile_name, "method": profile["method"], **expected}
    if profile.get("clientId"):
        token = execute([
            "az", "account", "get-access-token", "--subscription", config["azure"]["subscriptionId"],
            "--resource", "https://management.azure.com/", "--query", "accessToken",
            "--output", "tsv", "--only-show-errors",
        ], "identity", sensitive_stdout=True).strip()
        try:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (IndexError, ValueError, json.JSONDecodeError):
            raise MigrationError("Unable to verify the Azure client identity token") from None
        actual_client = claims.get("appid") or claims.get("azp")
        require(isinstance(actual_client, str) and actual_client.lower() == profile["clientId"].lower(), "Current Azure CLI token was not issued to the configured client ID")
        summary["clientId"] = profile["clientId"]
        summary["clientIdentityVerified"] = True
    private_write(destination / "azure-auth.json", json.dumps(summary, indent=2) + "\n")
    return profile


def load_config(path):
    require(not path.is_symlink(), "Local customer config cannot be a symlink")
    source = path.resolve()
    require(source.is_file(), "Local customer config must be a regular JSON file")
    try:
        document = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError):
        raise MigrationError("Unable to read the local customer JSON configuration") from None
    require(isinstance(document, dict), "Local customer configuration must be a JSON object")
    settings = local_settings(document)
    customer = copy.deepcopy(document)
    customer.pop("localExecution")
    config = validate_config(customer, customer.get("environment"))
    require("runner-connectivity" not in config["parameters"], "Move execution host connectivity from parameters into localExecution.executionHost")
    host = settings["executionHost"]
    if deployment_mode(config) == "greenfield":
        require(host["manageBlobDnsLink"] is False, "Greenfield execution host connectivity must not manage migration-only Blob DNS")
    config["parameters"]["runner-connectivity"] = {
        "runnerVirtualNetworkId": host["virtualNetworkId"],
        "managePeering": host["managePeering"],
        "manageBlobDnsLink": host["manageBlobDnsLink"],
    }
    validate_config(config, config["environment"])
    return config, settings


def operation_root(base, step):
    base = base.resolve()
    require(base.is_relative_to((ROOT / "temp").resolve()) and base != (ROOT / "temp").resolve(), "Local execution output must stay under temp/")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = base / f"{stamp}-{step}-{uuid4().hex[:8]}"
    destination.mkdir(parents=True, mode=0o700)
    destination.chmod(0o700)
    return destination


def execute_infrastructure_plan(config, stage, component, plan, plan_directory, execute_directory):
    execute_directory.mkdir(mode=0o700)
    azure = AzureCommands(config, execute_directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope does not match customer configuration")
    scope = "sub" if component == "bootstrap" else "group"
    if scope == "sub":
        scope_arguments = ["--location", config["location"]]
    else:
        group = config["legacy" if component in {"monitoring", "legacy-logging"} else "target"]["resourceGroup"]
        scope_arguments = ["--resource-group", group]
    common = [
        *scope_arguments,
        "--name", deployment_name(config, stage, component),
        "--template-file", str(plan_directory / "template.json"),
        "--parameters", f"@{plan_directory / 'parameters.json'}",
    ]
    command = ["deployment", scope, "create", *common]
    if scope == "group":
        command.extend(["--mode", "Incremental"])
    deployed = azure.scoped(command)
    require(deployed.get("properties", {}).get("provisioningState") == "Succeeded", "Local infrastructure deployment did not succeed")
    if component == "runner-target-connectivity":
        from scripts.runner_target_connectivity import inspect_target_connectivity

        reviewed = json.loads((plan_directory / "connectivity-review.json").read_text())
        verified = inspect_target_connectivity(config, azure, require_link=True)
        require(verified == reviewed, "AKS or ACR DNS context changed during local deployment")
    receipt = {
        "stage": stage,
        "component": component,
        "environment": config["environment"],
        "revision": plan["revision"],
        "configSha256": plan["configSha256"],
        "planSha256": plan["planSha256"],
        "deploymentId": deployed.get("id"),
        "provisioningState": "Succeeded",
    }
    private_write(execute_directory / "deployment-outputs.json", json.dumps(deployed.get("properties", {}).get("outputs", {}), indent=2) + "\n")
    private_write(execute_directory / "deployment-receipt.json", json.dumps(receipt, indent=2) + "\n")
    return receipt


def run_infrastructure(config, revision, stage, component, destination, release=None, operation="apply", approved_plan=""):
    require(operation in {"plan", "execute", "apply"}, "Invalid local infrastructure operation")
    plan_directory = destination / "plan"
    execute_directory = destination / "execute"
    plan = deploy_component(config, stage, component, revision, "plan", [], plan_directory, release=release)
    if operation == "plan":
        return {"planSha256": plan["planSha256"], "deploymentPerformed": False}
    if operation == "execute":
        require(re.fullmatch(r"[0-9a-f]{64}", approved_plan or "") is not None, "Local infrastructure execute requires --approved-plan-sha256")
        require(plan["planSha256"] == approved_plan, "Local infrastructure plan changed since approval")
    receipt = execute_infrastructure_plan(config, stage, component, plan, plan_directory, execute_directory)
    return {"planSha256": plan["planSha256"], "receipt": receipt}


def runtime_plan_sha(directory):
    summary = json.loads((directory / "runtime-summary.json").read_text())
    value = summary.get("planSha256", "")
    require(re.fullmatch(r"[0-9a-f]{64}", value) is not None, "Local runtime preview did not produce a plan hash")
    return value


def invoke_runtime_action(config, revision, stage, action, operation, directory, approved, image_public_key=None):
    if action in {"cluster-bootstrap", "legacy-hardening", "application"}:
        from scripts.migration_runtime import publish
        return publish(config, stage, action, operation, revision, directory, approved, image_public_key=image_public_key)
    if action == "monitoring-onboard":
        from scripts.migration_runtime import monitoring_onboard
        return monitoring_onboard(config, stage, operation, revision, directory, approved)
    if action in {"legacy-access-restrict", "legacy-access-restore"}:
        from scripts.legacy_access import access_operation
        return access_operation(config, action, operation, revision, directory, approved)
    if action == "restore-target":
        from scripts.migration_runtime import target_database_restore
        return target_database_restore(config, operation, revision, directory, approved)
    if action == "private-ingress":
        from scripts.private_ingress_runtime import deploy_private_ingress
        return deploy_private_ingress(config, operation, revision, directory, approved)
    if action == "certificate-renew":
        from scripts.certificate_runtime import certificate_operation
        return certificate_operation(config, operation, revision, directory, approved)
    if action == "edge-certificates":
        from scripts.certificate_pfx import sync_edge_certificates
        return sync_edge_certificates(config, operation, revision, directory, approved)
    if action == "edge-bind":
        from scripts.edge_binding import bind_edge
        return bind_edge(config, operation, revision, directory, approved)
    if action == "database-roles":
        from scripts.database_roles import provision_database_roles
        return provision_database_roles(config, operation, revision, directory, approved)
    if action == "schema-migrate":
        from scripts.schema_runtime import migrate_schema
        return migrate_schema(config, operation, revision, directory, approved)
    if action == "backend-secrets":
        from scripts.runtime_secrets import initialize_backend_secrets
        return initialize_backend_secrets(config, operation, revision, directory, approved)
    if action == "entra-apps":
        from scripts.entra_apps import initialize_entra_apps
        return initialize_entra_apps(config, operation, revision, directory, approved)
    if action == "entra-access":
        from scripts.entra_access import initialize_entra_access
        return initialize_entra_access(config, operation, revision, directory, approved)
    if action == "entra-revoke":
        from scripts.entra_revoke import initialize_entra_revoke
        return initialize_entra_revoke(config, operation, revision, directory, approved)
    if action in {"admin-credentials", "admin-credentials-rotate", "admin-credentials-recover", "admin-credentials-session-rotate", "admin-credentials-retire-expired"}:
        from scripts.admin_credentials import initialize_admin_credentials
        return initialize_admin_credentials(config, operation, revision, directory, approved, action=action)
    if action == "proxy-credentials":
        from scripts.proxy_credentials_runtime import initialize_proxy_credentials
        return initialize_proxy_credentials(config, operation, revision, directory, approved)
    if action in {"audit-pause", "audit-recover", "audit-resume"}:
        from scripts.audit_runtime import audit_operation
        return audit_operation(config, action, operation, revision, directory, approved)
    if action in {"dns-publish", "dns-rollback"}:
        from scripts.dns_runtime import change_dns
        return change_dns(config, action, operation, revision, directory, approved)
    raise MigrationError(f"Unsupported local runtime action: {action}")


def run_runtime(config, revision, stage, action, destination, image_public_key=None, operation="apply", approved_plan=""):
    require(operation in {"plan", "execute", "apply"}, "Invalid local runtime operation")

    plan_directory = destination / "plan"
    execute_directory = destination / "execute"
    plan_directory.mkdir(mode=0o700)
    execute_directory.mkdir(mode=0o700)
    invoke_runtime_action(config, revision, stage, action, "plan", plan_directory, "", image_public_key=image_public_key)
    plan_sha = runtime_plan_sha(plan_directory)
    if operation == "plan":
        return {"planSha256": plan_sha, "executionPerformed": False}
    if operation == "execute":
        require(re.fullmatch(r"[0-9a-f]{64}", approved_plan or "") is not None, "Local runtime execute requires --approved-plan-sha256")
        require(plan_sha == approved_plan, "Local runtime plan changed since approval")
    previous_audit_plan = os.environ.get("MIGRATION_AUDIT_PLAN_JSON")
    if action == "audit-recover":
        reviewed = json.loads((plan_directory / "runtime-review.json").read_text())
        os.environ["MIGRATION_AUDIT_PLAN_JSON"] = json.dumps(reviewed)
    try:
        invoke_runtime_action(config, revision, stage, action, "execute", execute_directory, plan_sha, image_public_key=image_public_key)
    finally:
        if action == "audit-recover":
            if previous_audit_plan is None:
                os.environ.pop("MIGRATION_AUDIT_PLAN_JSON", None)
            else:
                os.environ["MIGRATION_AUDIT_PLAN_JSON"] = previous_audit_plan
    return {"planSha256": plan_sha}


def local_input_path(value, field):
    require(isinstance(value, str) and value, f"Configure localExecution.{field}")
    supplied = Path(value).expanduser()
    supplied = supplied if supplied.is_absolute() else ROOT / supplied
    require(not supplied.is_symlink(), f"localExecution.{field} cannot be a symlink")
    path = supplied.resolve()
    require(path.is_file(), f"localExecution.{field} must reference a regular file")
    return path


@contextmanager
def local_operation_environment(config, settings, step, profile):
    updates = {
        "AZURE_TENANT_ID": config["azure"]["tenantId"],
        "AZURE_SUBSCRIPTION_ID": config["azure"]["subscriptionId"],
        "MIGRATION_MANIFEST_YAML": "",
        "MIGRATION_LEGACY_SALT_SOURCE": "",
        "PRIVATE_API_INGRESS_CLASS": "",
        "PRIVATE_ADMIN_INGRESS_CLASS": "",
    }
    inputs = settings.get("runtimeInputs", {})
    action = RUNTIME_STEPS.get(step, (None, None))[1]
    if action == "backend-secrets":
        updates["MIGRATION_LEGACY_SALT_SOURCE"] = inputs.get("legacySaltSource", "secret")
    if action == "restore-target":
        updates["POSTGRES_MIGRATION_USER"] = ""
        for setting, environment in (("backupBlob", "MIGRATION_RESTORE_BLOB"), ("backupSha256", "MIGRATION_BACKUP_SHA256")):
            require(isinstance(inputs.get(setting), str) and inputs[setting], f"Configure localExecution.runtimeInputs.{setting}")
            updates[environment] = inputs[setting]
        if inputs.get("postgresMigrationUser"):
            updates["POSTGRES_MIGRATION_USER"] = inputs["postgresMigrationUser"]
    if inputs.get("privateApiIngressClass"):
        updates["PRIVATE_API_INGRESS_CLASS"] = inputs["privateApiIngressClass"]
    if inputs.get("privateAdminIngressClass"):
        updates["PRIVATE_ADMIN_INGRESS_CLASS"] = inputs["privateAdminIngressClass"]
    if action == "audit-recover" and inputs.get("auditRecoveryCursor"):
        updates["AUDIT_RECOVERY_CURSOR"] = inputs["auditRecoveryCursor"]
    elif action == "audit-recover":
        updates["AUDIT_RECOVERY_CURSOR"] = ""
    if profile and (step.startswith("stage7-entra") or action and action.startswith("admin-credentials")):
        if action in {"entra-access", "entra-revoke"}:
            require(profile.get("clientId"), "The Entra access authentication profile requires clientId")
            updates["AZURE_ENTRA_ACCESS_CLIENT_ID"] = profile["clientId"]
        elif action in {"entra-apps", "admin-credentials", "admin-credentials-rotate", "admin-credentials-recover", "admin-credentials-session-rotate", "admin-credentials-retire-expired"}:
            require(profile.get("clientId"), "The Entra bootstrap authentication profile requires clientId")
            updates["AZURE_ENTRA_CLIENT_ID"] = profile["clientId"]
    previous = {name: os.environ.get(name) for name in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def enforce_local_policy(settings, step, config=None):
    features = settings["features"]
    native = False
    if config and deployment_mode(config) == "greenfield":
        migration_only = {"backup", "connectivity-check", "backup-restore", "legacy-logging", "monitoring", "monitoring-onboard", "legacy-hardening", "legacy-access-restrict", "legacy-access-restore", "stage5-restore-target"}
        require(step not in migration_only, "Greenfield local execution does not run legacy backup, restore, monitoring or access operations")
    if config and "application" in config:
        from scripts.backend_manifest import application_authentication
        native = application_authentication(config)["mode"] == "native"
    if native:
        require(features["entraMode"] == "deferred", "Native gateway authentication requires localExecution.features.entraMode=deferred")
        require(not step.startswith("stage7-"), "Native gateway authentication omits all Stage7 Entra and proxy operations")
    elif features["entraMode"] == "deferred":
        blocked = (step.startswith("stage7-") and step != "stage7-promote-proxy-image") or step == "stage8-application" or step in {
            "stage9-edge-bind", "stage9-edge-release", "stage9-dns-publish",
        }
        require(not blocked, "Entra is deferred: stop before Stage7 identity/proxy deployment and do not publish Stage8 application or Stage9 traffic")
    if step in {"stage9-edge-release", "stage9-dns-publish"}:
        require(features["allowTrafficRelease"] is True, "Stage9 traffic changes require localExecution.features.allowTrafficRelease=true and separate approval")


def run_stage2_decisions(config, revision, destination):
    from scripts.migration_evidence import draft_report

    report = draft_report(config, 2, revision)
    private_write(destination / "stage2-decision-checklist.json", json.dumps(report, indent=2) + "\n")
    return {"status": "review-required", "checks": list(report["checks"])}


def run_source_check(revision, destination):
    from scripts.source_supply_chain import check_source

    report = check_source(destination, revision)
    failures = [f"{item['name']}: {item.get('reason', 'failed')}" for item in report["results"] if item["status"] != "passed"]
    require(report["status"] == "passed", "Stage3 source image checks failed: " + "; ".join(failures))
    return {"status": "passed", "sourceImage": report["sourceImage"]}


def cluster_ca_from_kubeconfig(kube, directory):
    require("--kubeconfig" in kube, "Target cluster command is missing its kubeconfig")
    index = kube.index("--kubeconfig")
    require(index + 1 < len(kube), "Target cluster command has an invalid kubeconfig argument")
    kubeconfig = Path(kube[index + 1]).resolve()
    require(kubeconfig == (directory / "kubeconfig").resolve() and kubeconfig.is_file(), "Target kubeconfig is outside the runtime directory")
    try:
        document = yaml.safe_load(kubeconfig.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise MigrationError("Unable to read the target kubeconfig CA") from None
    require(isinstance(document, dict), "Target kubeconfig must contain an object")
    current = document.get("current-context")
    contexts = [item.get("context") for item in document.get("contexts", []) if isinstance(item, dict) and item.get("name") == current]
    require(len(contexts) == 1 and isinstance(contexts[0], dict), "Target kubeconfig current context is invalid")
    cluster_name = contexts[0].get("cluster")
    clusters = [item.get("cluster") for item in document.get("clusters", []) if isinstance(item, dict) and item.get("name") == cluster_name]
    require(len(clusters) == 1 and isinstance(clusters[0], dict), "Target kubeconfig cluster is invalid")
    encoded = clusters[0].get("certificate-authority-data")
    require(isinstance(encoded, str) and encoded, "Target kubeconfig requires inline certificate authority data")
    try:
        certificate = base64.b64decode(encoded, validate=True).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        raise MigrationError("Target kubeconfig CA is not valid base64 PEM") from None
    require(certificate.startswith("-----BEGIN CERTIFICATE-----\n") and certificate.rstrip().endswith("-----END CERTIFICATE-----") and "\x00" not in certificate, "Target kubeconfig CA is not a PEM certificate")
    path = directory / "aks-ca.crt"
    private_write(path, certificate)
    return path


def run_target_connectivity_check(config, settings, revision, destination, run=subprocess.run):
    from scripts.migration_runtime import connect_cluster
    from scripts.runner_target_connectivity import inspect_target_connectivity

    management_directory = destination / "management"
    runtime_directory = destination / "runtime"
    management_directory.mkdir(mode=0o700)
    runtime_directory.mkdir(mode=0o700)
    authenticate_azure(config, settings, "deploy", management_directory, run=run)
    azure = AzureCommands(config, management_directory)
    target = inspect_target_connectivity(config, azure, require_link=True)
    authenticate_azure(config, settings, "runtime", runtime_directory, run=run)
    kube = connect_cluster(config, runtime_directory, legacy=False)
    cluster_ca = cluster_ca_from_kubeconfig(kube, runtime_directory)
    checks = []

    def execute(arguments):
        completed = run(arguments, capture_output=True, text=True, check=False, timeout=120)
        require(completed.returncode == 0, f"Target connectivity command failed: {command_failure_summary(completed.stdout, completed.stderr, completed.returncode)}")
        return completed.stdout

    def private_endpoint(name, host, expected, ca_path=None):
        output = execute(["getent", "ahostsv4", host])
        addresses = {line.split()[0] for line in output.splitlines() if line.split()}
        require(addresses and addresses.issubset(set(expected)), f"{name} DNS does not resolve exclusively to its approved private endpoint")
        arguments = [
            "curl", "--noproxy", "*", "--connect-timeout", "5", "--max-time", "15",
            "--silent", "--show-error", "--output", "/dev/null", "--write-out", "%{remote_ip} %{http_code}",
        ]
        if ca_path is not None:
            arguments.extend(["--cacert", str(ca_path)])
        response = execute([*arguments, "https://" + host + "/"]).split()
        require(len(response) == 2 and response[0] in expected and re.fullmatch(r"[1-5][0-9]{2}", response[1]), f"{name} HTTPS did not reach its approved private endpoint")
        checks.append({"name": name, "status": "passed", "hostname": host, "privateEndpointIps": expected})

    private_endpoint("target-aks-private", target["apiHostname"], target["privateEndpointIps"], cluster_ca)
    private_endpoint("target-acr-private", target["acrLoginServer"], target["acrPrivateEndpointIps"])
    execute([*kube, "get", "deployments", "-o", "name"])
    checks.append({"name": "target-cluster-read", "status": "passed"})
    report = {
        "version": 1,
        "environment": config["environment"],
        "revision": revision,
        "configSha256": fingerprint(config),
        "observedAt": datetime.now(timezone.utc).isoformat(),
        "status": "passed",
        "checks": checks,
        "notCovered": ["Kubernetes write permissions", "ACR image pull authorization", "Vault/PostgreSQL/Redis access", "application readiness"],
    }
    private_write(destination / "target-readiness.json", json.dumps(report, indent=2) + "\n")
    return {"status": "passed"}


def run_image_promotion(config, settings, revision, step, destination):
    from local_execution.image_supply_chain import promote_image

    return promote_image(config, settings, revision, IMAGE_STEPS[step], destination)


def release_report(settings):
    path = local_input_path(settings.get("releaseReportPath"), "releaseReportPath")
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise MigrationError("Unable to read local Stage9 release report JSON") from None
    require(isinstance(report, dict), "Local Stage9 release report must be a JSON object")
    return report


def require_legacy_cluster_running(config, destination, azure=None):
    azure = azure or AzureCommands(config, destination)
    cluster = azure.scoped([
        "aks", "show",
        "--resource-group", config["legacy"]["resourceGroup"],
        "--name", config["legacy"]["aksClusterName"],
        "--query", "{provisioningState:provisioningState,powerState:powerState.code,fqdn:fqdn,privateFqdn:privateFqdn}",
    ])
    require(cluster.get("provisioningState") == "Succeeded", "Legacy AKS provisioning state must be Succeeded")
    require(cluster.get("powerState") == "Running", "Legacy AKS is not Running; start it and wait for its API hostname to resolve before continuing")
    require(isinstance(cluster.get("privateFqdn") or cluster.get("fqdn"), str), "Legacy AKS did not expose an API hostname")
    return cluster


def run_connectivity_check(config, revision, destination, run=subprocess.run):
    from scripts.migration_runtime import connect_cluster
    from scripts.runner_connectivity import backup_target

    checks = []

    def execute(arguments):
        result = run(arguments, capture_output=True, text=True, check=False, timeout=120)
        require(result.returncode == 0, f"Local connectivity command failed: {command_failure_summary(result.stdout, result.stderr, result.returncode)}")
        return result.stdout

    def probe(name, operation):
        try:
            details = operation()
            checks.append({"name": name, "status": "passed", "details": details})
            return details
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            checks.append({"name": name, "status": "failed", "details": "Check the local tool, current Azure identity, and private network path", "diagnostic": exception_diagnostic(error, "local-connectivity")})
            return None

    missing = [tool for tool in LOCAL_TOOLS if shutil.which(tool) is None]
    checks.append({"name": "local-tools", "status": "failed" if missing else "passed", "missing": missing})
    checks.append({"name": "python-version", "status": "passed" if sys.version_info >= (3, 10) else "failed", "version": sys.version.split()[0]})
    probe("docker-daemon", lambda: execute(["docker", "version", "--format", "{{.Server.Version}}"] ).strip())
    account = probe("azure-scope", lambda: json.loads(execute(["az", "account", "show", "--query", "{tenantId:tenantId,id:id}", "--output", "json", "--only-show-errors"])))
    expected = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}
    if account is not None and account != expected:
        checks[-1] = {"name": "azure-scope", "status": "failed", "details": "Current Azure tenant or subscription differs from customer configuration"}
        account = None
    target = None
    if account is not None:
        azure = AzureCommands(config, destination)
        cluster = probe("legacy-cluster-state", lambda: require_legacy_cluster_running(config, destination, azure))

        def cluster_read():
            kube = connect_cluster(config, destination, legacy=True)
            execute([*kube, "get", "deployments", "-o", "name"])
            return "Old cluster deployment listing succeeded"

        if cluster is not None:
            probe("legacy-cluster-read", cluster_read)
        target = probe("backup-resources", lambda: backup_target(config, azure))
    if target is not None:
        def private_dns():
            output = execute(["getent", "ahostsv4", target["blobHost"]])
            addresses = {line.split()[0] for line in output.splitlines() if line.split()}
            require(addresses and addresses.issubset(set(target["privateEndpointIps"])), "Blob DNS does not resolve exclusively to the approved private endpoint")
            return "Execution host DNS matches the backup private endpoint"

        def private_tls():
            output = execute(["curl", "--noproxy", "*", "--connect-timeout", "5", "--max-time", "15", "--silent", "--show-error", "--output", "/dev/null", "--write-out", "%{remote_ip} %{http_code}", "https://" + target["blobHost"] + "/"])
            response = output.split()
            require(len(response) == 2 and response[0] in target["privateEndpointIps"] and re.fullmatch(r"[1-5][0-9]{2}", response[1]), "Blob TLS did not reach the approved private endpoint")
            return "TLS reached the backup private endpoint"

        def blob_read():
            output = execute(["az", "storage", "blob", "list", "--subscription", config["azure"]["subscriptionId"], "--account-name", target["storageAccountName"], "--container-name", target["containerName"], "--auth-mode", "login", "--num-results", "1", "--query", "length(@)", "--output", "json", "--only-show-errors"])
            require(output.strip() in {"0", "1"}, "Unexpected Blob listing result")
            return "Current Azure identity can list the backup container"

        probe("backup-private-dns", private_dns)
        probe("backup-private-tls", private_tls)
        probe("backup-blob-read", blob_read)
    status = "failed" if any(check["status"] == "failed" for check in checks) else "passed"
    report = {
        "version": 1,
        "environment": config["environment"],
        "revision": revision,
        "configSha256": fingerprint(config),
        "observedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "checks": checks,
        "notCovered": ["Pod exec/cp", "Blob upload and download", "backup contents", "actual restore"],
    }
    private_write(destination / "local-readiness.json", json.dumps(report, indent=2) + "\n")
    require(status == "passed", "Local execution host checks failed")
    return {"status": status}


def run_backup_restore(config, settings, revision, destination):
    from scripts.migration_runtime import backup_restore

    require_legacy_cluster_running(config, destination)
    image = settings["postgresRestoreImage"]
    previous = os.environ.get("POSTGRES_RESTORE_IMAGE")
    os.environ["POSTGRES_RESTORE_IMAGE"] = image
    try:
        backup_restore(config, revision, destination)
    finally:
        if previous is None:
            os.environ.pop("POSTGRES_RESTORE_IMAGE", None)
        else:
            os.environ["POSTGRES_RESTORE_IMAGE"] = previous
    return {"status": "completed"}


def check_configuration(config):
    components = ((0, "bootstrap"), (0, "network"), (0, "runner-connectivity")) if deployment_mode(config) == "greenfield" else ((0, "bootstrap"), (0, "backup"), (0, "runner-connectivity"), (1, "legacy-logging"), (1, "monitoring"))
    for stage, component in components:
        parameters_for(config, stage, component)
    return {"status": "valid"}


def remove_sensitive_runtime_files(destination):
    for path in destination.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.name in {"kubeconfig", "aks-ca.crt", "database.dump", "downloaded.dump"}:
            path.unlink()


def local_operation(step, requested):
    require(requested in {"auto", "plan", "execute", "apply"}, "Invalid local operation")
    if requested != "auto":
        if requested == "apply":
            legacy_direct = (step in INFRASTRUCTURE_STEPS and not step.startswith("stage")) or (step in RUNTIME_STEPS and not step.startswith("stage")) or step in {"config-check", "connectivity-check", "backup-restore"}
            require(legacy_direct, "Immediate apply is limited to the validated Stage0-1 local path")
        return requested
    if step in IMAGE_STEPS:
        raise MigrationError("Image promotion requires explicit --operation execute")
    if step in INFRASTRUCTURE_STEPS and not step.startswith("stage"):
        return "apply"
    if step in RUNTIME_STEPS and not step.startswith("stage"):
        return "apply"
    if step in INFRASTRUCTURE_STEPS or step in RUNTIME_STEPS or step == "stage9-edge-release":
        return "plan"
    return "execute"


def execute_step(config, settings, revision, step, destination, operation="auto", approved_plan=""):
    enforce_local_policy(settings, step, config)
    operation = local_operation(step, operation)
    if step == "config-check":
        require(operation in {"execute", "apply"}, "Configuration check does not use plan/execute")
        return check_configuration(config)
    if step == "stage2-decisions":
        require(operation in {"execute", "apply"}, "Stage2 decision checklist does not use plan/execute")
        return run_stage2_decisions(config, revision, destination)
    if step == "stage3-source-check":
        require(operation in {"execute", "apply"}, "Source check does not use plan/execute")
        return run_source_check(revision, destination)
    if step == "stage4-target-check":
        require(operation in {"execute", "apply"}, "Target connectivity check does not use plan/execute")
        return run_target_connectivity_check(config, settings, revision, destination)
    profile_name = authentication_profile_for_step(step)
    profile = authenticate_azure(config, settings, profile_name, destination) if profile_name else None
    with local_operation_environment(config, settings, step, profile):
        if step in INFRASTRUCTURE_STEPS:
            stage, component = INFRASTRUCTURE_STEPS[step]
            return run_infrastructure(config, revision, stage, component, destination, operation=operation, approved_plan=approved_plan)
        if step in IMAGE_STEPS:
            require(operation in {"execute", "apply"}, "Image promotion is an explicit execute operation and has no ARM plan")
            return run_image_promotion(config, settings, revision, step, destination)
        if step == "stage9-edge-release":
            return run_infrastructure(config, revision, 9, "edge", destination, release=release_report(settings), operation=operation, approved_plan=approved_plan)
        if step in RUNTIME_STEPS:
            stage, action = RUNTIME_STEPS[step]
            require(action not in {"legacy-access-restrict", "legacy-access-restore"} or "legacyAccess" in config, "Configure legacyAccess before changing legacy source restrictions")
            if action == "application":
                from local_execution.image_supply_chain import public_key_path
                image_public_key = public_key_path(settings)
            else:
                image_public_key = None
            return run_runtime(config, revision, stage, action, destination, image_public_key=image_public_key, operation=operation, approved_plan=approved_plan)
        if step == "connectivity-check":
            require(operation in {"execute", "apply"}, "Connectivity check does not use plan/execute")
            return run_connectivity_check(config, revision, destination)
        if step == "backup-restore":
            require(operation in {"execute", "apply"}, "Backup/restore is an explicit execute operation")
            return run_backup_restore(config, settings, revision, destination)
    raise MigrationError(f"Unknown local execution step: {step}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Path to the standalone local customer JSON file")
    parser.add_argument("--step", choices=STEPS, required=True)
    parser.add_argument("--operation", choices=("auto", "plan", "execute", "apply"), default="auto", help="Stage2-9 defaults to plan; execute requires an approved plan hash. apply preserves the Stage0-1 direct mode.")
    parser.add_argument("--approved-plan-sha256", default="", help="Reviewed plan hash required by Stage2-9 execute")
    parser.add_argument("--output-root", type=Path, default=ROOT / "temp/local-stage09")
    args = parser.parse_args()
    config, settings = load_config(args.config)
    revision = reviewed_revision()
    destination = operation_root(args.output_root, args.step)
    effective_operation = local_operation(args.step, args.operation)
    config_sha = fingerprint({"customer": config, "localExecution": settings})
    try:
        result = execute_step(config, settings, revision, args.step, destination, effective_operation, args.approved_plan_sha256)
    except Exception as error:
        failure = {
            "version": 1,
            "mode": "local-direct",
            "environment": config["environment"],
            "step": args.step,
            "operation": effective_operation,
            "revision": revision,
            "configSha256": config_sha,
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "outputDirectory": str(destination.relative_to(ROOT)),
            "result": {"status": "failed", "errorType": type(error).__name__},
        }
        private_write(destination / "local-execution.json", json.dumps(failure, indent=2) + "\n")
        raise
    finally:
        remove_sensitive_runtime_files(destination)
    summary = {
        "version": 1,
        "mode": "local-direct",
        "environment": config["environment"],
        "step": args.step,
        "operation": effective_operation,
        "revision": revision,
        "configSha256": config_sha,
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "outputDirectory": str(destination.relative_to(ROOT)),
        "result": result,
    }
    private_write(destination / "local-execution.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


def cli():
    try:
        main()
        return 0
    except MigrationError as error:
        raise SystemExit(diagnostic_exit(error, "local-stage09")) from None
    except Exception as error:
        raise SystemExit(diagnostic_exit(error, "local-stage09", "Local Stage0-9 execution failed")) from None