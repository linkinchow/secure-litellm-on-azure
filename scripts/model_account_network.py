"""Restrict Azure OpenAI accounts reached without private endpoints (plan B) to the gateway egress IPs.

For each account the change is: publicNetworkAccess=Enabled, networkAcls.defaultAction=Deny with ipRules equal to
the supplied egress IPs (the new environment's Firewall public IP), and disableLocalAuth=true. The plan reads the
current state, writes a private rollback snapshot and prints planSha256; --execute re-reads the state and applies
only when the recomputed plan hash equals --approved-plan-sha256. --rollback-snapshot restores a snapshot with the
same plan/execute discipline.

Example:
    python3 -m scripts.model_account_network --accounts-file temp/limx-import/model-fragments.json \
        --egress-ip 20.0.0.10 --output-dir temp/limx-network
    python3 -m scripts.model_account_network --accounts-file temp/limx-import/model-fragments.json \
        --egress-ip 20.0.0.10 --output-dir temp/limx-network --execute --approved-plan-sha256 <planSha256>
"""

import argparse
import copy
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from scripts.customer_migration import MigrationError, fingerprint, private_write, require


API_VERSION = "2024-10-01"
ACCOUNT_ID = re.compile(r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[A-Za-z0-9._()-]{1,90}"
                        r"/providers/Microsoft\.CognitiveServices/accounts/[A-Za-z0-9][A-Za-z0-9-]{1,62}")
SNAPSHOT = "rollback-snapshot.json"
MAX_IP_RULES = 64


class AzureCli:
    """Minimal az runner returning parsed JSON (None for an empty body)."""

    def run(self, arguments):
        result = subprocess.run(["az", *arguments, "--only-show-errors", "--output", "json"],
                                capture_output=True, text=True, check=False, timeout=300)
        if result.returncode:
            message = (result.stderr or "").strip().splitlines()
            raise MigrationError("az " + " ".join(arguments[:2]) + " failed: " + (message[-1] if message else "no detail"))
        return json.loads(result.stdout) if result.stdout.strip() else None


def account_ids(values):
    result, seen = [], set()
    for value in values:
        require(isinstance(value, str) and ACCOUNT_ID.fullmatch(value.strip().rstrip("/")) is not None,
                "Account must be a Microsoft.CognitiveServices/accounts resource ID")
        value = value.strip().rstrip("/")
        require(value.lower() not in seen, "Duplicate account " + value)
        seen.add(value.lower())
        result.append(value)
    require(result, "Supply at least one account")
    return result


def read_accounts_file(path):
    """JSON list of IDs, a legacy_model_import fragment / customer.json (privateEndpoint=false connections), or lines."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except ValueError:
        return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if isinstance(document, list):
        return document
    require(isinstance(document, dict), "Accounts file must be a JSON array or object")
    connections = (document.get("parameters", {}).get("platform", {}).get("azureOpenAIConnections")
                   or document.get("azureOpenAIConnections"))
    require(isinstance(connections, list), "Accounts file has no azureOpenAIConnections")
    return [item["accountResourceId"] for item in connections if item.get("privateEndpoint", True) is False]


def egress_rules(values):
    rules = []
    for value in values:
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError:
            raise MigrationError("Invalid egress IP/CIDR " + str(value)) from None
        require(network.version == 4 and network.is_global and network.prefixlen >= 24,
                "Egress rules must be public IPv4 addresses or CIDRs no broader than /24")
        rules.append(str(network.network_address) if network.prefixlen == 32 else str(network))
    require(rules and len(set(rules)) == len(rules) and len(rules) <= MAX_IP_RULES,
            f"Supply one to {MAX_IP_RULES} unique egress IPs")
    return sorted(rules)


def normalize_rule(value):
    return value[:-3] if isinstance(value, str) and value.endswith("/32") else value


def network_state(resource):
    """The rollback-relevant subset of an account, normalized for comparison."""
    properties = resource.get("properties") or {}
    acls = properties.get("networkAcls")
    state = {"publicNetworkAccess": properties.get("publicNetworkAccess"),
             "disableLocalAuth": properties.get("disableLocalAuth"),
             "networkAcls": copy.deepcopy(acls) if acls is not None else None}
    return state


def effective(state):
    """Semantics used to compare states: missing ACLs allow all, missing disableLocalAuth means keys enabled."""
    acls = state.get("networkAcls") or {}
    result = {"publicNetworkAccess": state.get("publicNetworkAccess") or "Enabled",
              "disableLocalAuth": bool(state.get("disableLocalAuth")),
              "defaultAction": acls.get("defaultAction") or "Allow",
              "ipRules": sorted(normalize_rule(rule.get("value")) for rule in acls.get("ipRules") or []),
              "virtualNetworkRules": sorted((rule.get("id") or "").lower() for rule in acls.get("virtualNetworkRules") or [])}
    if "bypass" in acls:
        result["bypass"] = acls["bypass"]
    return result


def read_account(azure, identity):
    resource = azure.run(["resource", "show", "--ids", identity, "--api-version", API_VERSION])
    require(isinstance(resource, dict) and resource.get("id", "").lower() == identity.lower(),
            "Azure returned a different resource for " + identity)
    properties = resource.get("properties") or {}
    require(resource.get("kind") in {"OpenAI", "AIServices"} and properties.get("provisioningState") == "Succeeded",
            identity + " must be a Succeeded Azure OpenAI/AI Services account")
    return resource


def desired_acls(current, rules, clear_virtual_network_rules):
    acls = {"defaultAction": "Deny", "ipRules": [{"value": rule} for rule in rules],
            "virtualNetworkRules": [] if clear_virtual_network_rules else copy.deepcopy((current or {}).get("virtualNetworkRules") or [])}
    if current and "bypass" in current:
        acls["bypass"] = current["bypass"]
    return acls


def plan_apply(azure, identities, rules, *, allow_private_endpoints=False, clear_virtual_network_rules=False,
               keep_local_auth=False, remove_ip_rules=False, enable_public_access=False):
    accounts, refusals = [], []
    for identity in identities:
        resource = read_account(azure, identity)
        properties = resource.get("properties") or {}
        current = network_state(resource)
        connections = [item.get("id") for item in properties.get("privateEndpointConnections") or []]
        vnet_rules = (current["networkAcls"] or {}).get("virtualNetworkRules") or []
        stale_rules = sorted(set(effective(current)["ipRules"]) - set(rules)) if effective(current)["defaultAction"] == "Deny" else []
        if connections and not allow_private_endpoints:
            refusals.append(f"{identity} has {len(connections)} private endpoint connection(s); confirm they stay valid and pass --allow-private-endpoints")
        if vnet_rules and not clear_virtual_network_rules:
            refusals.append(f"{identity} has {len(vnet_rules)} virtualNetworkRules that Deny-with-ipRules would orphan; pass --clear-virtual-network-rules after review")
        if current["publicNetworkAccess"] == "Disabled" and not enable_public_access:
            refusals.append(f"{identity} has publicNetworkAccess Disabled; opening it to the egress IPs needs --enable-public-access")
        if stale_rules and not remove_ip_rules:
            refusals.append(f"{identity} allows other IPs {stale_rules}; add them with --egress-ip or pass --remove-ip-rules")
        desired = {"publicNetworkAccess": "Enabled",
                   "disableLocalAuth": bool(current["disableLocalAuth"]) if keep_local_auth else True,
                   "networkAcls": desired_acls(current["networkAcls"], rules, clear_virtual_network_rules)}
        if keep_local_auth and current["disableLocalAuth"] is None:
            desired.pop("disableLocalAuth")
        changed = effective(current) != effective({**current, **desired})
        accounts.append({"accountResourceId": identity, "kind": resource["kind"], "location": resource.get("location"),
                         "privateEndpointConnections": len(connections), "current": current, "desired": desired,
                         "operation": "update" if changed else "keep"})
    return accounts, refusals


def plan_rollback(azure, snapshot):
    require(isinstance(snapshot, dict) and snapshot.get("kind") == "model-account-network-rollback"
            and isinstance(snapshot.get("accounts"), list) and snapshot["accounts"], "Not a model account network rollback snapshot")
    identities = account_ids([item.get("accountResourceId") for item in snapshot["accounts"]])
    accounts = []
    for identity, saved in zip(identities, snapshot["accounts"]):
        current = network_state(read_account(azure, identity))
        previous = saved["previous"]
        desired = {}
        if previous.get("publicNetworkAccess") is not None:
            desired["publicNetworkAccess"] = previous["publicNetworkAccess"]
        desired["disableLocalAuth"] = bool(previous.get("disableLocalAuth"))
        desired["networkAcls"] = copy.deepcopy(previous["networkAcls"]) if previous.get("networkAcls") is not None else {
            "defaultAction": "Allow", "ipRules": [], "virtualNetworkRules": []}
        changed = effective(current) != effective({**current, **desired})
        accounts.append({"accountResourceId": identity, "current": current, "desired": desired,
                         "operation": "update" if changed else "keep"})
    return accounts


def apply_account(azure, account, *, attempts, sleep):
    url = "https://management.azure.com" + account["accountResourceId"] + "?api-version=" + API_VERSION
    azure.run(["rest", "--method", "patch", "--url", url, "--body", json.dumps({"properties": account["desired"]})])
    target = effective({**account["current"], **account["desired"]})
    for _ in range(attempts):
        resource = azure.run(["resource", "show", "--ids", account["accountResourceId"], "--api-version", API_VERSION])
        state = (resource.get("properties") or {}).get("provisioningState")
        if state == "Succeeded" and effective(network_state(resource)) == target:
            return network_state(resource)
        require(state not in {"Failed", "Canceled"}, account["accountResourceId"] + " update " + str(state))
        sleep(10)
    raise MigrationError(account["accountResourceId"] + " did not reach the planned network state; inspect it before retrying")


def execute(azure, plan, directory, approved, *, attempts=30, sleep=time.sleep, receipt="execute-receipt.json"):
    planned = fingerprint(plan)
    require(isinstance(approved, str) and approved == planned,
            "Plan changed since review or was not approved; rerun the plan and approve its planSha256")
    applied = []
    try:
        for account in plan["accounts"]:
            if account["operation"] == "keep":
                continue
            applied.append({"accountResourceId": account["accountResourceId"],
                            "result": apply_account(azure, account, attempts=attempts, sleep=sleep)})
    finally:
        private_write(directory / receipt, json.dumps({"planSha256": planned, "applied": applied,
                                                       "complete": len(applied) == sum(a["operation"] == "update" for a in plan["accounts"])}, indent=2))
    return applied


def write_once(path, content):
    if path.exists():
        require(not path.is_symlink() and path.read_text(encoding="utf-8") == content,
                f"{path} already holds a different rollback snapshot; use a new --output-dir and keep the old snapshot")
        return
    private_write(path, content)


def run(azure, directory, *, identities=None, egress=None, snapshot_path=None, execute_plan=False, approved="",
        allow_private_endpoints=False, clear_virtual_network_rules=False, keep_local_auth=False, remove_ip_rules=False,
        enable_public_access=False, attempts=30, sleep=time.sleep):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if snapshot_path is not None:
        require(identities is None and egress is None, "Rollback takes only --rollback-snapshot")
        snapshot_text = Path(snapshot_path).read_text(encoding="utf-8")
        snapshot = json.loads(snapshot_text)
        plan = {"action": "model-account-network-rollback", "apiVersion": API_VERSION,
                "snapshotSha256": fingerprint(snapshot), "accounts": plan_rollback(azure, snapshot)}
        refusals, prefix = [], "rollback-"
    else:
        identities, rules = account_ids(identities or []), egress_rules(egress or [])
        accounts, refusals = plan_apply(azure, identities, rules, allow_private_endpoints=allow_private_endpoints,
                                        clear_virtual_network_rules=clear_virtual_network_rules,
                                        keep_local_auth=keep_local_auth, remove_ip_rules=remove_ip_rules,
                                        enable_public_access=enable_public_access)
        plan = {"action": "model-account-network", "apiVersion": API_VERSION, "egressIpRules": rules,
                "options": {"allowPrivateEndpoints": allow_private_endpoints, "clearVirtualNetworkRules": clear_virtual_network_rules,
                            "keepLocalAuth": keep_local_auth, "removeIpRules": remove_ip_rules,
                            "enablePublicAccess": enable_public_access},
                "accounts": accounts}
        prefix = ""
    summary = {"action": plan["action"], "planSha256": fingerprint(plan), "applied": False, "refusals": refusals,
               "updates": sum(item["operation"] == "update" for item in plan["accounts"]),
               "operations": {item["accountResourceId"]: item["operation"] for item in plan["accounts"]}}
    if execute_plan:
        # Nothing is written before the recomputed plan matches the approved one.
        require(not refusals, "Plan has refusals; resolve them before executing")
        require(isinstance(approved, str) and approved == summary["planSha256"],
                "Plan changed since review or was not approved; rerun the plan and approve its planSha256")
        if snapshot_path is None and summary["updates"]:
            saved = json.loads((directory / SNAPSHOT).read_text(encoding="utf-8")) if (directory / SNAPSHOT).is_file() else {}
            require(saved.get("planSha256") == summary["planSha256"],
                    "Execute in the plan's --output-dir so its rollback snapshot matches the approved plan")
        summary["result"] = execute(azure, plan, directory, approved, attempts=attempts, sleep=sleep,
                                    receipt=prefix + "execute-receipt.json")
        summary["applied"] = True
        private_write(directory / (prefix + "execute-summary.json"), json.dumps(summary, indent=2))
        return summary
    private_write(directory / (prefix + "plan.json"), json.dumps(plan, indent=2))
    if snapshot_path is None and not refusals and summary["updates"]:
        snapshot = {"kind": "model-account-network-rollback", "planSha256": summary["planSha256"], "apiVersion": API_VERSION,
                    "accounts": [{"accountResourceId": item["accountResourceId"], "previous": item["current"]}
                                 for item in plan["accounts"] if item["operation"] == "update"]}
        write_once(directory / SNAPSHOT, json.dumps(snapshot, indent=2))
        summary["rollbackSnapshot"] = str(directory / SNAPSHOT)
    private_write(directory / (prefix + "summary.json"), json.dumps(summary, indent=2))
    return summary

def main(argv=None, azure=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account-id", action="append", default=[], help="Microsoft.CognitiveServices/accounts resource ID (repeatable)")
    parser.add_argument("--accounts-file", type=Path, help="JSON list, model fragment/customer.json (privateEndpoint=false connections) or one ID per line")
    parser.add_argument("--egress-ip", action="append", default=[], help="Gateway Firewall public IP or CIDR (repeatable)")
    parser.add_argument("--rollback-snapshot", type=Path, help="Restore the state recorded by an earlier plan")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Apply; requires --approved-plan-sha256 from the plan")
    parser.add_argument("--approved-plan-sha256", default="")
    parser.add_argument("--allow-private-endpoints", action="store_true", help="Proceed although accounts have private endpoint connections (they are left untouched)")
    parser.add_argument("--clear-virtual-network-rules", action="store_true", help="Remove existing virtualNetworkRules (saved in the snapshot)")
    parser.add_argument("--remove-ip-rules", action="store_true", help="Drop existing ipRules that are not in --egress-ip (saved in the snapshot)")
    parser.add_argument("--enable-public-access", action="store_true", help="Allow switching publicNetworkAccess from Disabled to Enabled")
    parser.add_argument("--keep-local-auth", action="store_true", help="Leave disableLocalAuth unchanged (stage the key cut-off separately)")
    args = parser.parse_args(argv)
    try:
        if args.rollback_snapshot is not None:
            require(not args.account_id and args.accounts_file is None and not args.egress_ip,
                    "--rollback-snapshot cannot be combined with accounts or egress IPs")
            identities = egress = None
        else:
            identities = [*args.account_id, *(read_accounts_file(args.accounts_file) if args.accounts_file else [])]
            egress = args.egress_ip
        require(not args.execute or args.approved_plan_sha256, "--execute requires --approved-plan-sha256")
        summary = run(azure or AzureCli(), args.output_dir, identities=identities, egress=egress,
                      snapshot_path=args.rollback_snapshot, execute_plan=args.execute, approved=args.approved_plan_sha256,
                      allow_private_endpoints=args.allow_private_endpoints,
                      clear_virtual_network_rules=args.clear_virtual_network_rules,
                      keep_local_auth=args.keep_local_auth, remove_ip_rules=args.remove_ip_rules,
                      enable_public_access=args.enable_public_access)
    except (MigrationError, ValueError, OSError) as error:
        print("Model account network change failed: " + str(error), file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return 1 if summary["refusals"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
