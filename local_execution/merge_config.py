"""Safely merge one Stage 2-9 fragment into a local customer configuration."""

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from local_execution.runner import load_config
from scripts.customer_migration import ROOT, MigrationError, deployment_mode, private_write, require
from scripts.workflow_diagnostics import diagnostic_exit


DEFAULT_CATALOG = ROOT / "local_execution/customer.stage2-9.fragments.example.json"
DEFAULT_OUTPUT_ROOT = ROOT / "temp/local-config-merge"
PLACEHOLDER = re.compile(r"REPLACE_[A-Z0-9_]+")
OPTION_SPECS = {
    "single-validation-identity": (2, "singleValidationIdentityAlternative", "local"),
    "automatic-api-certificate": (4, "optionalAutomaticApiCertificate", "section"),
    "database-admin-identity": (5, "separateDatabaseAdminIdentity", "section"),
    "legacy-master-key-salt": (5, "legacyMasterKeySaltCompatibility", "section"),
    "native-auth": (6, "nativeAuthentication", "section"),
    "observability": (8, "nativeAuditOptionalObservability", "section"),
    "enhanced-l3": (8, "enhancedL3Alternative", "section"),
    "azure-dns": (9, "optionalAzureDns", "section"),
    "app-gateway": (9, "applicationGatewayEdge", "section"),
    "approved-release": (9, "localExecutionForApprovedRelease", "local"),
}


def require_no_symlink_components(path, label):
    absolute = Path(path).expanduser().absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        require(not current.is_symlink(), f"{label} cannot contain symlink path components")
    return absolute


def read_json(path, label):
    source = require_no_symlink_components(path, label).resolve()
    require(source.is_file(), f"{label} must be a regular JSON file")
    try:
        document = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError):
        raise MigrationError(f"Unable to read {label}") from None
    require(isinstance(document, dict), f"{label} must contain a JSON object")
    return document


def deep_merge(target, addition):
    require(isinstance(target, dict) and isinstance(addition, dict), "Configuration fragments must be JSON objects")
    for key, value in addition.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def remove_keys(target, keys):
    require(isinstance(keys, list) and all(isinstance(key, str) and key for key in keys), "Fragment removal keys must be strings")
    for key in keys:
        target.pop(key, None)


def remove_paths(target, paths):
    require(isinstance(paths, list) and all(isinstance(path, str) and path for path in paths), "Fragment removal paths must be strings")
    for path in paths:
        *parents, leaf = path.split(".")
        current = target
        for key in parents:
            current = current.get(key) if isinstance(current, dict) else None
        if isinstance(current, dict):
            current.pop(leaf, None)


def derived_values(config):
    platform = config.get("parameters", {}).get("platform", {})
    stage5 = platform.get("stage5Data", {})
    backup = config.get("parameters", {}).get("backup", {})
    network = config.get("parameters", {}).get("network", {})
    bootstrap = config.get("parameters", {}).get("bootstrap", {})
    certificate = config.get("parameters", {}).get("certificate-vault", {})
    local = config.get("localExecution", {})
    host = local.get("executionHost", {})
    runtime_inputs = local.get("runtimeInputs", {})
    database_profile = local.get("authentication", {}).get("database", {})
    backup_blob = runtime_inputs.get("backupBlob", "")
    backup_sha256 = runtime_inputs.get("backupSha256", "")
    backup_match = re.fullmatch(r"pre-change/([0-9a-f]{32})\.dump", backup_blob) if isinstance(backup_blob, str) else None
    values = {
        "REPLACE_SUBSCRIPTION_ID": config.get("azure", {}).get("subscriptionId"),
        "REPLACE_AZURE_REGION": config.get("location"),
        "REPLACE_CUSTOMER_BASE_DOMAIN": config.get("baseDomain"),
        "REPLACE_TARGET_LOG_WORKSPACE": platform.get("logAnalyticsWorkspaceName") or backup.get("logAnalyticsWorkspaceName") or bootstrap.get("logAnalyticsWorkspaceName"),
        "REPLACE_TARGET_VNET": platform.get("stage4Network", {}).get("virtualNetworkName") or backup.get("virtualNetworkName") or network.get("virtualNetworkName"),
        "REPLACE_RUNNER_VNET_RESOURCE_ID": host.get("virtualNetworkId"),
        "REPLACE_GLOBALLY_UNIQUE_ACR_NAME": platform.get("containerRegistryName"),
        "REPLACE_CERTIFICATE_VAULT_NAME": certificate.get("vaultName"),
        "REPLACE_GLOBALLY_UNIQUE_CERTIFICATE_VAULT_NAME": certificate.get("vaultName"),
        "REPLACE_SUPPORTED_PG_SKU": stage5.get("postgresqlSkuName"),
        "REPLACE_LOCAL_OPERATOR_UAMI_PRINCIPAL_ID": config.get("databaseAccess", {}).get("migrationPrincipalId"),
        "REPLACE_32_HEX": backup_match.group(1) if backup_match else None,
        "REPLACE_64_HEX_SHA256": backup_sha256 if isinstance(backup_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", backup_sha256) else None,
    }
    if stage5.get("postgresqlEntraAdministratorPrincipalType") == "Group":
        values.update({
            "REPLACE_PG_ADMIN_GROUP_OBJECT_ID": stage5.get("postgresqlEntraAdministratorObjectId"),
            "REPLACE_PG_ADMIN_GROUP_NAME": stage5.get("postgresqlEntraAdministratorPrincipalName"),
        })
    if stage5.get("postgresqlEntraAdministratorPrincipalType") == "ServicePrincipal":
        values.update({
            "REPLACE_DATABASE_ADMIN_UAMI_CLIENT_ID": database_profile.get("clientId"),
            "REPLACE_DATABASE_ADMIN_UAMI_PRINCIPAL_ID": stage5.get("postgresqlEntraAdministratorObjectId"),
            "REPLACE_DATABASE_ADMIN_UAMI_NAME": stage5.get("postgresqlEntraAdministratorPrincipalName"),
        })
    return {key: value for key, value in values.items() if isinstance(value, str) and value and "REPLACE_" not in value}


def replace_placeholders(value, values):
    if isinstance(value, dict):
        return {key: replace_placeholders(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_placeholders(item, values) for item in value]
    if not isinstance(value, str):
        return value
    for placeholder, replacement in values.items():
        require(isinstance(placeholder, str) and PLACEHOLDER.fullmatch(placeholder), "Values keys must be complete REPLACE_* placeholders")
        if value == placeholder and isinstance(replacement, list):
            require(bool(replacement) and all(isinstance(item, str) for item in replacement), "Array placeholder replacements must be nonempty string arrays")
            return copy.deepcopy(replacement)
        if placeholder in value:
            require(isinstance(replacement, str), "Embedded placeholder replacement values must be strings")
            value = value.replace(placeholder, replacement)
    return value


def unresolved_placeholders(value):
    found = set()
    if isinstance(value, dict):
        for item in value.values():
            found.update(unresolved_placeholders(item))
    elif isinstance(value, list):
        for item in value:
            found.update(unresolved_placeholders(item))
    elif isinstance(value, str):
        found.update(PLACEHOLDER.findall(value))
    return found


def apply_section(config, section):
    require(isinstance(section, dict), "Selected catalog section must be an object")
    if "removeCustomerConfigKeysBeforeMerge" in section:
        remove_keys(config, section["removeCustomerConfigKeysBeforeMerge"])
    if "customerConfig" in section:
        deep_merge(config, section["customerConfig"])
    if "removeCustomerConfigPathsAfterMerge" in section:
        remove_paths(config, section["removeCustomerConfigPathsAfterMerge"])
    if "localExecutionMerge" in section:
        require(isinstance(config.get("localExecution"), dict), "Base configuration requires localExecution")
        deep_merge(config["localExecution"], section["localExecutionMerge"])


def validate_options(stage, options):
    require(len(options) == len(set(options)), "Duplicate merge option")
    for option in options:
        require(option in OPTION_SPECS, f"Unknown merge option: {option}")
        required_stage = OPTION_SPECS[option][0]
        require(stage == required_stage, f"Option {option} applies only to Stage {required_stage}")
    require(not ({"enhanced-l3", "observability"} <= set(options)), "Choose enhanced-l3 or observability in one merge operation, not both")


def merge_stage(config, catalog, stage, options=(), values=None):
    require(type(stage) is int and 2 <= stage <= 9, "Merge stage must be 2 through 9")
    require(catalog.get("catalogVersion") == 1 and isinstance(catalog.get("stages"), dict), "Unsupported Stage fragment catalog")
    selected = catalog["stages"].get(str(stage))
    require(isinstance(selected, dict), f"Stage {stage} is missing from the fragment catalog")
    validate_options(stage, tuple(options))
    mode = deployment_mode(config)
    require(mode != "greenfield" or "legacy-master-key-salt" not in options, "legacy-master-key-salt does not apply to greenfield")
    merged = copy.deepcopy(config)
    require(isinstance(merged.get("localExecution"), dict), "Base configuration requires localExecution")
    apply_section(merged, selected)
    if stage == 9 and "approved-release" not in options:
        deep_merge(merged["localExecution"], selected["localExecutionForPrepare"])
    for option in options:
        _required_stage, key, destination = OPTION_SPECS[option]
        section = selected[key]
        if destination == "section":
            apply_section(merged, section)
        else:
            deep_merge(merged["localExecution"], section)
    if stage == 5 and mode == "greenfield":
        runtime_inputs = merged["localExecution"].setdefault("runtimeInputs", {})
        remove_keys(runtime_inputs, ["backupBlob", "backupSha256", "postgresMigrationUser", "legacySaltSource"])
    replacements = {**derived_values(merged), **derived_values(config), **(values or {})}
    merged = replace_placeholders(merged, replacements)
    return merged, sorted(unresolved_placeholders(merged))


def changed_paths(before, after, prefix=""):
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in before or key not in after:
                changes.append(path)
            else:
                changes.extend(changed_paths(before[key], after[key], path))
        return changes
    return [] if before == after else [prefix]


def operation_directory(root, stage):
    root = require_no_symlink_components(root, "Merge output path").resolve()
    require(root.is_relative_to((ROOT / "temp").resolve()) and root != (ROOT / "temp").resolve(), "Merge output must stay under temp/")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = root / f"stage{stage}-{stamp}-{uuid4().hex[:8]}"
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def atomic_write(path, document):
    path = require_no_symlink_components(path, "Local customer configuration").resolve()
    descriptor, temporary = tempfile.mkstemp(prefix=".customer-merge-", suffix=".json", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(document, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stage", type=int, choices=range(2, 10), required=True)
    parser.add_argument("--operation", choices=("plan", "apply"), default="plan")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--values", type=Path)
    parser.add_argument("--option", action="append", default=[], choices=tuple(OPTION_SPECS))
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args()

    source = read_json(args.config, "local customer configuration")
    catalog = read_json(args.catalog, "Stage fragment catalog")
    supplied_values = read_json(args.values, "placeholder values") if args.values else {}
    def valid_value(value):
        values = value if isinstance(value, list) else [value]
        return bool(values) and all(isinstance(item, str) and item.strip() and not PLACEHOLDER.search(item) for item in values)
    require(all(valid_value(value) for value in supplied_values.values()), "Placeholder values must be nonempty strings or string arrays without REPLACE_* tokens")
    merged, missing = merge_stage(source, catalog, args.stage, args.option, supplied_values)
    directory = operation_directory(args.output_root, args.stage)
    preview = directory / "customer.merged.json"
    private_write(preview, json.dumps(merged, indent=2) + "\n")
    required = directory / "values.required.json"
    private_write(required, json.dumps({key: "" for key in missing}, indent=2) + "\n")
    report = {
        "stage": args.stage,
        "operation": args.operation,
        "options": args.option,
        "config": str(args.config.resolve().relative_to(ROOT)),
        "preview": str(preview.relative_to(ROOT)),
        "requiredValues": str(required.relative_to(ROOT)),
        "missingPlaceholders": missing,
        "changedPaths": changed_paths(source, merged),
        "applied": False,
    }
    if args.operation == "apply":
        require(not missing, "Replace every listed placeholder before apply")
        load_config(preview)
        backup = directory / "customer.before.json"
        private_write(backup, json.dumps(source, indent=2) + "\n")
        atomic_write(args.config, merged)
        report.update(applied=True, backup=str(backup.relative_to(ROOT)))
    private_write(directory / "merge-report.json", json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as error:
        raise SystemExit(diagnostic_exit(error, "local-config-merge")) from None
    except Exception as error:
        raise SystemExit(diagnostic_exit(error, "local-config-merge", "Local configuration merge failed")) from None