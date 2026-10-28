"""Convert a legacy LiteLLM config.yaml model_list into azureOpenAIConnections/application.models fragments.

Offline only: the tool never calls Azure. The operator supplies an account map produced by a read-only
command, for example (Resource Graph, all subscriptions the operator can read):

    az graph query --first 1000 --output json -q "resources
      | where type =~ 'microsoft.cognitiveservices/accounts'
      | project id, name, resourceGroup, subscriptionId, location, kind,
                customSubDomainName = tostring(properties.customSubDomainName)" > accounts.json

or per subscription, concatenated into one JSON array:

    az cognitiveservices account list --subscription "$SUB" --output json \
      --query "[].{id:id,name:name,resourceGroup:resourceGroup,location:location,kind:kind,customSubDomainName:properties.customSubDomainName}"

A plain {"<accountName>": {"subscriptionId": "...", "resourceGroupName": "..."}} object is accepted too.

Credential values are never copied: literal or environment-referenced keys/tokens/secrets make the import fail,
non-allowlisted fields are reported by name (never by value) and left out of the fragments.
"""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys

import yaml

from scripts.customer_migration import MigrationError, private_write, require
from scripts.model_configuration import (
    COST_FIELDS, INFO_FIELDS, PARAM_FIELDS, azure_base_model, identifier, normalize_openai_endpoint, rendered_models, validate_options,
)
from scripts.model_sync_catalog import account_id, plain, uuid


MODEL_PARAMS = PARAM_FIELDS - {"api_version"}
MODEL_INFO = INFO_FIELDS - {"base_model"}
# Fields consumed structurally rather than copied.
STRUCTURAL_PARAMS = {"model", "api_base", "api_version", "base_model"} | COST_FIELDS
STRUCTURAL_INFO = {"id", "base_model"}
# Identity selectors are replaced by the platform's workload identity; they may only name these variables.
IDENTITY_FIELDS = {"tenant_id", "client_id", "azure_client_id", "azure_tenant_id", "azure_authority_host",
                   "azure_scope", "use_azure_ad", "azure_ad_token_refresh"}
ALLOWED_ENV_REFERENCES = {"AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_AUTHORITY_HOST"}
CREDENTIAL_NAME = re.compile(r"(^|[_-])(api[_-]?key|key|keys|secret|password|passwd|pwd|token|credentials?|sas|signature|"
                             r"connection[_-]string|private|authorization|bearer|cookie)($|[_-])", re.I)
CONFIGMAP_LIMIT = 1048576
ALIAS_PREFIX = "aoai-"
ID_PREFIX = "legacy-"


def load_document(path):
    text = Path(path).read_text(encoding="utf-8")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        raise MigrationError("Legacy configuration is not valid YAML") from None
    if isinstance(document, dict) and document.get("kind") == "ConfigMap":
        data = document.get("data") or {}
        require(isinstance(data.get("config.yaml"), str), "Legacy ConfigMap must carry data.config.yaml")
        try:
            document = yaml.safe_load(data["config.yaml"])
        except yaml.YAMLError:
            raise MigrationError("Legacy ConfigMap config.yaml is not valid YAML") from None
    require(isinstance(document, dict) and isinstance(document.get("model_list"), list) and document["model_list"],
            "Legacy configuration requires a nonempty model_list")
    return document


def parse_account_id(value):
    match = isinstance(value, str) and re.fullmatch(
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/Microsoft\.CognitiveServices/accounts/([^/]+)", value, re.I)
    require(bool(match), "Account map id must be a Microsoft.CognitiveServices/accounts resource ID")
    return match.groups()


def load_accounts(path):
    """Normalize az graph query / az cognitiveservices account list output or a plain name map."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(document, dict) and "data" in document:
        require(not document.get("skip_token") and not document.get("skipToken"),
                "Resource Graph output is paginated; rerun with a larger --first or merge every page")
        document = document["data"]
    entries = []
    if isinstance(document, dict):
        for name, value in document.items():
            require(isinstance(value, dict), "Account map values must be objects")
            entries.append({**value, "name": value.get("name", name)})
    else:
        require(isinstance(document, list), "Account map must be a JSON array or object")
        entries = document
    accounts, hosts = {}, {}
    for entry in entries:
        require(isinstance(entry, dict), "Account map entries must be objects")
        subscription, group, name = entry.get("subscriptionId"), entry.get("resourceGroupName", entry.get("resourceGroup")), entry.get("name")
        if "id" in entry:
            parsed = parse_account_id(entry["id"])
            subscription, group = subscription or parsed[0], group or parsed[1]
            require(parsed[2].lower() == str(name).lower() and parsed[0].lower() == str(subscription).lower()
                    and parsed[1].lower() == str(group).lower(), "Account map id disagrees with its name/subscription/resource group")
        name = plain(name, r"[A-Za-z0-9][A-Za-z0-9-]{1,62}").lower()
        group = plain(group, r"[A-Za-z0-9][A-Za-z0-9._()-]{0,89}")
        subscription = uuid(subscription)
        host = (entry.get("customSubDomainName") or name)
        host = plain(host, r"[A-Za-z0-9][A-Za-z0-9-]{1,62}").lower()
        identity = account_id(subscription, group, name)
        require(identity.lower() not in {item["accountResourceId"].lower() for item in accounts.values()},
                "Account map lists " + name + " twice")
        require(host not in hosts, "Account map has two accounts serving " + host + ".openai.azure.com")
        account = {"accountResourceId": identity, "subscriptionId": subscription, "resourceGroupName": group,
                   "accountName": name, "host": host}
        for key in ("location", "kind"):
            if isinstance(entry.get(key), str):
                account[key] = entry[key]
        hosts[host] = account
        accounts[identity.lower()] = account
    require(hosts, "Account map is empty")
    return hosts


def env_reference(value):
    return isinstance(value, str) and value.strip().lower().startswith("os.environ/")


def walk(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk(item, path + "." + str(key) if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, f"{path}[{index}]")
    else:
        yield path, value


def screen(fields, section, allowed, structural, label, errors, dropped):
    """Classify fields; credential-like names or env references are errors, values are never echoed."""
    carried = {}
    for field, value in fields.items():
        name = str(field)
        where = f"{label} {section}.{name}"
        if name in allowed:
            if any(env_reference(item) for _, item in walk(value)):
                errors.append(where + " references os.environ; only literal values can be imported")
            else:
                carried[name] = copy.deepcopy(value)
            continue
        if name in structural:
            continue
        if name in IDENTITY_FIELDS:
            references = [item for _, item in walk(value) if env_reference(item)]
            if any(item.strip()[len("os.environ/"):] not in ALLOWED_ENV_REFERENCES for item in references):
                errors.append(where + " references an environment variable outside " + ", ".join(sorted(ALLOWED_ENV_REFERENCES)))
            dropped.setdefault(section + "." + name, []).append(label)
            continue
        if CREDENTIAL_NAME.search(name):
            if any(env_reference(item) for _, item in walk(value)):
                errors.append(where + " references a credential from the environment; key/token authentication is in use "
                              "(the new gateway uses managed identity only; remove it after confirming)")
            else:
                errors.append(where + " holds a credential value (not shown); remove and rotate it before importing")
            continue
        for path, item in walk(value):
            leaf = re.split(r"[.\[]", path)[-1] if path else ""
            if leaf and CREDENTIAL_NAME.search(leaf):
                errors.append(f"{where}.{path} holds a credential value (not shown); remove and rotate it before importing")
            elif env_reference(item):
                errors.append(f"{where}{'.' + path if path else ''} references os.environ; not importable")
        dropped.setdefault(section + "." + name, []).append(label)
    return carried


def parse_overrides(values):
    result = {}
    for value in values or []:
        require(isinstance(value, str) and value.count("=") == 1, "--base-model expects GROUP=MODEL")
        group, model = value.split("=")
        result[identifier(group)] = azure_base_model(model)
    return result


def load_deployments(path):
    """Optional read-only `az cognitiveservices account deployment list` output, merged into one array."""
    if path is None:
        return {}
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(document, dict) and "value" in document:
        document = document["value"]
    require(isinstance(document, list), "Deployment list must be a JSON array")
    result = {}
    for item in document:
        require(isinstance(item, dict) and isinstance(item.get("id"), str), "Deployment entries need their ARM id")
        result[item["id"].lower()] = item
    return result


def import_models(document, hosts, *, private_endpoint=False, base_models=None, deployments=None,
                  preserve_legacy_ids=True, default_api_version=None):
    base_models = base_models or {}
    deployments = deployments or {}
    errors, warnings, dropped, moved = [], [], {}, {}
    connections, models, ids, occurrences = {}, [], set(), {}
    legacy_ids = {}
    for index, entry in enumerate(document["model_list"]):
        label = f"model_list[{index}]"
        if not isinstance(entry, dict):
            errors.append(label + " is not an object")
            continue
        for key in sorted(set(entry) - {"model_name", "litellm_params", "model_info"}):
            if CREDENTIAL_NAME.search(str(key)):
                errors.append(f"{label}.{key} holds a credential value (not shown); remove and rotate it before importing")
            else:
                dropped.setdefault(str(key), []).append(label)
        group = entry.get("model_name")
        params, info = entry.get("litellm_params"), entry.get("model_info", {}) or {}
        if not isinstance(group, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", group) is None:
            errors.append(label + ".model_name must be a plain identifier")
            continue
        label = f"{label} ({group})"
        if not isinstance(params, dict) or not isinstance(info, dict):
            errors.append(label + " litellm_params/model_info must be objects")
            continue
        carried_params = screen(params, "litellm_params", MODEL_PARAMS, STRUCTURAL_PARAMS, label, errors, dropped)
        carried_info = screen(info, "model_info", MODEL_INFO, STRUCTURAL_INFO, label, errors, dropped)
        # LiteLLM also prices from litellm_params; the platform keeps prices in model_info only.
        for field in sorted(COST_FIELDS & params.keys()):
            if env_reference(params[field]):
                errors.append(f"{label} litellm_params.{field} references os.environ; only literal values can be imported")
            elif field in carried_info and carried_info[field] != params[field]:
                errors.append(f"{label} litellm_params.{field} and model_info.{field} disagree")
            else:
                carried_info[field] = copy.deepcopy(params[field])
                moved.setdefault("litellm_params." + field + " -> model_info", []).append(label)
        for field in ("model", "api_base", "api_version", "base_model"):
            if env_reference(params.get(field)):
                errors.append(f"{label} litellm_params.{field} must be a literal, not an os.environ reference")
        model = params.get("model")
        if not isinstance(model, str) or not model.startswith("azure/"):
            errors.append(label + " litellm_params.model must be azure/<deployment>; other providers are not imported")
            continue
        deployment = model.removeprefix("azure/")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", deployment) is None:
            errors.append(label + " deployment name must be a plain identifier")
            continue
        try:
            endpoint = normalize_openai_endpoint(str(params.get("api_base", "")).rstrip("/") or None)
        except MigrationError as error:
            errors.append(label + " api_base: " + str(error))
            continue
        host = endpoint.removeprefix("https://").removesuffix(".openai.azure.com")
        account = hosts.get(host)
        if account is None:
            errors.append(f"{label} api_base host {host}.openai.azure.com is not in the account map")
            continue
        if not private_endpoint and host != account["accountName"]:
            errors.append(f"{label} {host}.openai.azure.com is served by account {account['accountName']}; "
                          "connections without a private endpoint must use https://<accountName>.openai.azure.com")
            continue
        version = params.get("api_version", default_api_version)
        if not isinstance(version, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", version) is None:
            errors.append(label + " requires a plain litellm_params.api_version (or --default-api-version)")
            continue
        try:
            validate_options(carried_params, carried_info)
        except MigrationError as error:
            errors.append(f"{label} allowlisted option failed validation: {error}")
            continue
        base = None
        for source in (info.get("base_model"), params.get("base_model")):
            if source is not None:
                try:
                    candidate = azure_base_model(source)
                except MigrationError:
                    errors.append(label + " base_model must be a plain Azure model identifier")
                    candidate = None
                if base is not None and candidate != base:
                    errors.append(label + " model_info.base_model and litellm_params.base_model disagree")
                base = base or candidate
        identity_key = account["accountResourceId"].lower() + "/deployments/" + deployment.lower()
        live = deployments.get(identity_key)
        if deployments and live is None:
            errors.append(f"{label} deployment {deployment} is missing from the supplied deployment list for {account['accountName']}")
        live_base = None
        if live is not None:
            properties = live.get("properties", {})
            if properties.get("provisioningState") not in {None, "Succeeded"}:
                errors.append(f"{label} deployment {deployment} is not Succeeded")
            name = properties.get("model", {}).get("name")
            try:
                live_base = azure_base_model(name) if isinstance(name, str) else None
            except MigrationError:
                warnings.append(f"{label} deployed model name of {deployment} is not a plain identifier; not used as baseModel")
        base = base or base_models.get(group) or live_base
        if base is not None and live_base is not None and base.lower() != live_base.lower():
            warnings.append(f"{label} base_model {base} differs from the deployed model {live_base}")
        triple = (group, account["accountResourceId"].lower(), deployment)
        ordinal = occurrences.get(triple, 0)
        occurrences[triple] = ordinal + 1
        if ordinal:
            warnings.append(f"{label} repeats {group}/{account['accountName']}/{deployment}; kept as a separate weighted entry")
        legacy_id = info.get("id")
        if legacy_id is not None and preserve_legacy_ids:
            if not isinstance(legacy_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", legacy_id) is None:
                errors.append(label + " model_info.id is not a plain identifier; rerun with --regenerate-ids")
                continue
            if legacy_id in legacy_ids:
                errors.append(f"{label} model_info.id duplicates {legacy_ids[legacy_id]}")
                continue
            legacy_ids[legacy_id] = label
            model_id = legacy_id
        else:
            seed = "|".join((group, triple[1], deployment) + ((str(ordinal),) if ordinal else ()))
            model_id = ID_PREFIX + hashlib.sha256(seed.encode()).hexdigest()[:24]
        if model_id in ids:
            errors.append(label + " produces a duplicate model id")
            continue
        ids.add(model_id)
        alias = ALIAS_PREFIX + hashlib.sha256(account["accountResourceId"].lower().encode()).hexdigest()[:16]
        connection = connections.setdefault(alias, {
            "alias": alias, "accountResourceId": account["accountResourceId"], "subscriptionId": account["subscriptionId"],
            "resourceGroupName": account["resourceGroupName"], "accountName": account["accountName"],
            **({"endpoint": endpoint} if host != account["accountName"] else {}),
            "privateEndpoint": bool(private_endpoint)})
        mapping = {"modelGroup": group, "connectionAlias": connection["alias"], "deploymentName": deployment,
                   "id": model_id, "apiVersion": version}
        if base is not None:
            mapping["baseModel"] = base
        if carried_params:
            mapping["litellmParams"] = carried_params
        if carried_info:
            mapping["modelInfo"] = carried_info
        models.append((mapping, account))
    groups = {}
    for mapping, _ in models:
        groups.setdefault(mapping["modelGroup"], set()).add(mapping.get("baseModel", "").lower())
    for group, bases in sorted(groups.items()):
        if "" in bases:
            warnings.append(f"Model group {group} has deployments without baseModel; LiteLLM cost tracking falls back to "
                            "the deployment name (add model_info.base_model, --base-model or --deployments)")
        if len(bases - {""}) > 1:
            errors.append(f"Model group {group} mixes base models {sorted(bases - {''})}; load-balanced deployments must share one family")
    return {"connections": list(connections.values()), "models": models, "errors": errors, "warnings": warnings,
            "dropped": dropped, "moved": moved}


def rendered_size(connections, models):
    config = {"parameters": {"platform": {"azureOpenAIConnections": connections}}, "application": {"models": models}}
    return len(yaml.safe_dump(rendered_models(config), sort_keys=False).encode())


def summarize(document, result, *, private_endpoint):
    models = result["models"]
    groups, accounts = {}, {}
    for mapping, account in models:
        group = groups.setdefault(mapping["modelGroup"], {"deployments": 0, "accounts": set(), "apiVersions": set(), "baseModels": set()})
        group["deployments"] += 1
        group["accounts"].add(account["accountName"])
        group["apiVersions"].add(mapping["apiVersion"])
        group["baseModels"].add(mapping.get("baseModel", "(none)"))
        record = accounts.setdefault(account["accountName"], {"accountResourceId": account["accountResourceId"],
                                                               "location": account.get("location"), "deployments": 0, "modelGroups": set()})
        record["deployments"] += 1
        record["modelGroups"].add(mapping["modelGroup"])
    settings = document.get("litellm_settings") or {}
    summary = {
        "legacyModelEntries": len(document["model_list"]),
        "importedModels": len(models),
        "connections": len(result["connections"]),
        "privateEndpoint": bool(private_endpoint),
        "subscriptions": sorted({account["subscriptionId"] for _, account in models}),
        "locations": sorted({account.get("location") or "unknown" for _, account in models}),
        "modelGroups": {name: {"deployments": item["deployments"], "accounts": len(item["accounts"]),
                               "apiVersions": sorted(item["apiVersions"]), "baseModels": sorted(item["baseModels"])}
                        for name, item in sorted(groups.items())},
        "accounts": {name: {**{key: value for key, value in item.items() if key != "modelGroups"},
                            "modelGroups": sorted(item["modelGroups"])} for name, item in sorted(accounts.items())},
        "droppedFields": {field: {"count": len(labels), "entries": labels[:10]} for field, labels in sorted(result["dropped"].items())},
        "movedFields": {field: len(labels) for field, labels in sorted(result["moved"].items())},
        "notImportedSections": sorted(str(key) for key in document if key != "model_list"),
        "legacyAzureAdTokenRefresh": settings.get("enable_azure_ad_token_refresh") is True if isinstance(settings, dict) else False,
        "warnings": result["warnings"],
        "errors": result["errors"],
    }
    if models and not result["errors"]:
        size = rendered_size(result["connections"], [mapping for mapping, _ in models])
        summary["renderedModelListBytes"] = size
        summary["configMapLimitBytes"] = CONFIGMAP_LIMIT
        summary["configMapHeadroomBytes"] = CONFIGMAP_LIMIT - size
    return summary


def run(legacy, accounts, output, *, private_endpoint=False, base_models=(), deployments=None,
        preserve_legacy_ids=True, default_api_version=None):
    document = load_document(legacy)
    hosts = load_accounts(accounts)
    if default_api_version is not None:
        identifier(default_api_version)
    result = import_models(document, hosts, private_endpoint=private_endpoint, base_models=parse_overrides(base_models),
                           deployments=load_deployments(deployments), preserve_legacy_ids=preserve_legacy_ids,
                           default_api_version=default_api_version)
    summary = summarize(document, result, private_endpoint=private_endpoint)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # Never leave fragments from an earlier run beside a failed summary.
    (output / "model-fragments.json").unlink(missing_ok=True)
    private_write(output / "import-summary.json", json.dumps(summary, indent=2))
    if result["errors"]:
        return summary
    fragment = {"parameters": {"platform": {"azureOpenAIConnections": result["connections"]}},
                "application": {"models": [mapping for mapping, _ in result["models"]]}}
    private_write(output / "model-fragments.json", json.dumps(fragment, indent=2))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--legacy-config", required=True, type=Path, help="Legacy config.yaml or the litellm-config ConfigMap YAML")
    parser.add_argument("--accounts", required=True, type=Path, help="Account map JSON (az graph query / az cognitiveservices account list / name map)")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--private-endpoint", action="store_true", help="Emit privateEndpoint=true (default false: plan B public endpoint)")
    parser.add_argument("--base-model", action="append", default=[], metavar="GROUP=MODEL", help="Base model for a group lacking model_info.base_model")
    parser.add_argument("--deployments", type=Path, help="Optional merged az cognitiveservices account deployment list output")
    parser.add_argument("--regenerate-ids", action="store_true", help="Ignore legacy model_info.id values and derive new stable ids")
    parser.add_argument("--default-api-version", help="Fallback when an entry lacks litellm_params.api_version")
    args = parser.parse_args(argv)
    try:
        summary = run(args.legacy_config, args.accounts, args.output_dir, private_endpoint=args.private_endpoint,
                      base_models=args.base_model, deployments=args.deployments,
                      preserve_legacy_ids=not args.regenerate_ids, default_api_version=args.default_api_version)
    except (MigrationError, ValueError, OSError) as error:
        print("Legacy model import failed: " + str(error), file=sys.stderr)
        return 2
    brief = {key: summary[key] for key in ("legacyModelEntries", "importedModels", "connections", "privateEndpoint")}
    brief.update(modelGroups={name: item["deployments"] for name, item in summary["modelGroups"].items()},
                 droppedFields={name: item["count"] for name, item in summary["droppedFields"].items()},
                 warnings=len(summary["warnings"]), errors=summary["errors"],
                 renderedModelListBytes=summary.get("renderedModelListBytes"))
    print(json.dumps(brief, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
