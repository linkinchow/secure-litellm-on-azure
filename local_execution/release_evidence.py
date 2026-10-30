"""Collect Stage9 release evidence for a native-auth Application Gateway environment.

Writes one <check>.json file per release check into an ignored temp/ directory for release_report --evidence-dir.
Live checks run from the Runner against the private ingress (with the gateway edge header) and the deployed Azure
resources. Probe keys, the native UI password and session tokens are never written or printed; evidence files hold
status codes and configuration facts only. Decision records (telemetry, pilots, rollback, database reconciliation)
come from an operator-written decisions file and are recorded as such, not as test results.
"""

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import socket
import ssl
import subprocess

import time
from urllib.parse import urlsplit, urlunsplit

from scripts.customer_migration import ROOT, require
from scripts.migration_deploy import deployment_name

DECISIONS = ("telemetry_disabled", "approved_pilot_clients", "rollback_plan", "database_restore", "l3_alerts_disabled")
ADMIN_ONLY_PREFIXES = ("/ui", "/key", "/spend", "/login", "/fallback", "/user", "/team")

def az(config, *arguments):
    result = subprocess.run(["az", *arguments, "--subscription", config["azure"]["subscriptionId"], "--output", "json", "--only-show-errors"],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout) if result.stdout.strip() else None

def deployment_outputs(config, stage, component):
    return az(config, "deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"],
              "--name", deployment_name(config, stage, component), "--query", "properties.outputs")

class Plane:
    """HTTPS to a private IP while keeping the public host name for SNI, certificate checks and Host."""

    def __init__(self, ip, host, headers):
        import requests
        from requests.adapters import HTTPAdapter

        outer = self

        class Adapter(HTTPAdapter):
            def send(self, request, **kwargs):
                parts = urlsplit(request.url)
                request.headers["Host"] = parts.hostname
                request.url = urlunsplit(parts._replace(netloc=outer.ip))
                self.poolmanager.connection_pool_kw.update({"server_hostname": parts.hostname, "assert_hostname": parts.hostname})
                return super().send(request, **kwargs)

        self.ip, self.host, self.headers = ip, host, headers
        self.session = requests.Session()
        self.session.mount("https://", Adapter())

    def request(self, method, path, headers=None, **kwargs):
        merged = {**self.headers, **(headers or {})}
        return self.session.request(method, f"https://{self.host}{path}", headers=merged, timeout=kwargs.pop("timeout", 120), **kwargs)

class Evidence:
    def __init__(self, directory):
        self.directory = directory
        self.results = {}

    def record(self, name, passed, facts):
        path = self.directory / f"{name}.json"
        payload = {"check": name, "passed": bool(passed), "observedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "facts": facts}
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        self.results[name] = bool(passed)
        print(json.dumps({"check": name, "passed": bool(passed), "facts": facts})[:400])

def responses_body(model, **extra):
    return {"model": model, "input": "Reply with the single word: pong", "max_output_tokens": 64, **extra}

def websocket_turn(ip, host, edge, key, model):
    import websocket

    facts = {}
    try:
        tls = ssl.create_default_context().wrap_socket(socket.create_connection((ip, 443), timeout=60), server_hostname=host)
        ws = websocket.WebSocket()
        ws.connect(f"wss://{host}/v1/responses", socket=tls, header=[f"{edge[0]}: {edge[1]}", f"Authorization: Bearer {key}"], timeout=90)
        facts["handshake"] = ws.getstatus()
        ws.send(json.dumps({"type": "response.create", **responses_body(model)}))
        events = []
        while len(events) <= 200:
            events.append(json.loads(ws.recv()).get("type"))
            if events[-1] in ("response.completed", "response.failed", "error"):
                break
        ws.close()
        facts.update({"events": len(events), "last": events[-1], "completed": events[-1] == "response.completed"})
    except Exception as error:
        facts.update({"completed": False, "error": type(error).__name__})
    return facts

def admin_session_key(admin, password, username):
    login = admin.request("POST", "/login", data={"username": username, "password": password}, allow_redirects=False, timeout=30)
    token = login.cookies.get("token")
    if not token:
        return login.status_code, None
    payload = token.split(".")[1]
    return login.status_code, json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["key"]

def spend_log_count(admin, session_key):
    response = admin.request("GET", "/spend/logs", headers={"Authorization": f"Bearer {session_key}"}, timeout=120)
    return response.status_code, (len(response.json()) if response.ok and isinstance(response.json(), list) else None)

def live_checks(config, evidence, keys):
    ingress = deployment_outputs(config, 4, "private-ingress")["privateIngress"]["value"]
    edge_output = deployment_outputs(config, 9, "edge")["edge"]["value"]
    require(edge_output.get("edgeId"), "Release evidence currently supports the Application Gateway edge")
    edge = ("X-LLMGW-Edge-Id", edge_output["edgeId"])
    api_host, admin_host = edge_output["apiHost"], edge_output["adminHost"]
    api = Plane(ingress["api"]["privateIpAddress"], api_host, {edge[0]: edge[1]})
    raw_api = Plane(ingress["api"]["privateIpAddress"], api_host, {})
    admin = Plane(ingress["admin"]["privateIpAddress"], admin_host, {edge[0]: edge[1]})
    raw_admin = Plane(ingress["admin"]["privateIpAddress"], admin_host, {})
    full, limited = keys["full"], keys["limited"]
    bearer = lambda key: {"Authorization": f"Bearer {key}"}
    model = full["model"]

    codes = {
        "api_no_header": raw_api.request("POST", "/v1/responses", headers=bearer(full["key"]), json=responses_body(model)).status_code,
        "api_wrong_header": raw_api.request("POST", "/v1/responses", headers={**bearer(full["key"]), edge[0]: "00000000-0000-0000-0000-000000000000"}, json=responses_body(model)).status_code,
        "api_header_no_key": api.request("POST", "/v1/responses", json=responses_body(model)).status_code,
        "admin_no_header": raw_admin.request("GET", "/fallback/login", timeout=30).status_code,
        "admin_header": admin.request("GET", "/fallback/login", timeout=30).status_code,
    }
    evidence.record("origin_bypass_denied", codes["api_no_header"] == 404 and codes["api_wrong_header"] == 404 and codes["api_header_no_key"] in (401, 403)
                    and codes["admin_no_header"] == 404 and codes["admin_header"] == 200, codes)

    matrix = {"responses": api.request("POST", "/v1/responses", headers=bearer(full["key"]), json=responses_body(model)).status_code,
              "chat": api.request("POST", "/v1/chat/completions", headers=bearer(full["key"]),
                                  json={"model": model, "messages": [{"role": "user", "content": "Reply with the single word: pong"}]}).status_code}
    for name, path, body in (("responses_sse", "/v1/responses", responses_body(model, stream=True)),
                             ("chat_sse", "/v1/chat/completions", {"model": model, "stream": True, "messages": [{"role": "user", "content": "Count from 1 to 5."}]})):
        response = api.request("POST", path, headers=bearer(full["key"]), json=body, stream=True)
        with response:
            events = sum(1 for line in response.iter_lines() if line.startswith(b"data:"))
            matrix[name] = {"status": response.status_code, "events": events, "contentType": response.headers.get("content-type", "").split(";")[0]}
    listing = api.request("GET", "/v1/models", headers=bearer(full["key"]), timeout=30)
    matrix["models_get"] = {"status": listing.status_code, "ids": sorted(item["id"] for item in listing.json().get("data", [])) if listing.ok else []}
    matrix["models_get_unversioned"] = api.request("GET", "/models", headers=bearer(full["key"]), timeout=30).status_code
    matrix["models_post"] = api.request("POST", "/v1/models", headers=bearer(full["key"]), json={}, timeout=30).status_code
    matrix["models_by_id"] = api.request("GET", f"/v1/models/{model}", headers=bearer(full["key"]), timeout=30).status_code
    matrix["admin_path_on_api"] = api.request("GET", "/key/info", headers=bearer(full["key"]), timeout=30).status_code
    matrix["ui_on_api"] = api.request("GET", "/ui/", timeout=30).status_code
    matrix["websocket"] = websocket_turn(ingress["api"]["privateIpAddress"], api_host, edge, full["key"], model)
    evidence.record("required_protocol_matrix", matrix["responses"] == 200 and matrix["chat"] == 200
                    and all(matrix[name]["status"] == 200 and matrix[name]["events"] > 1 for name in ("responses_sse", "chat_sse"))
                    and matrix["models_get"]["status"] == 200 and matrix["models_get_unversioned"] == 200
                    and all(matrix[name] == 404 for name in ("models_post", "models_by_id", "admin_path_on_api", "ui_on_api"))
                    and matrix["websocket"].get("completed") is True, matrix)

    acl = {"allowed": api.request("POST", "/v1/responses", headers=bearer(limited["key"]), json=responses_body(limited["allowedModel"])).status_code,
           "denied": api.request("POST", "/v1/responses", headers=bearer(limited["key"]), json=responses_body(limited["deniedModel"])).status_code,
           "invalid_key": api.request("POST", "/v1/responses", headers=bearer("sk-invalid-release-evidence"), json=responses_body(model)).status_code}
    evidence.record("native_virtual_key_acl", acl["allowed"] == 200 and acl["denied"] == 403 and acl["invalid_key"] in (401, 403), acl)

    vault = next(name for name in az(config, "keyvault", "list", "--resource-group", config["target"]["resourceGroup"], "--query", "[].name") if name.startswith("kv-lt-"))
    password = az(config, "keyvault", "secret", "show", "--vault-name", vault, "--name", "litellm-ui-password", "--query", "value")
    username = config["application"]["authentication"]["adminUsername"]
    wrong = admin.request("POST", "/login", data={"username": username, "password": "wrong-password-release-evidence"}, allow_redirects=False, timeout=30).status_code
    status, session_key = admin_session_key(admin, password, username)
    del password
    evidence.record("native_admin_password_login", wrong in (400, 401, 403) and status in (200, 302, 303) and session_key is not None,
                    {"wrongPassword": wrong, "correctPassword": status, "sessionIssued": session_key is not None})

    audit = {"apiSpendLogsWithKey": api.request("GET", "/spend/logs", headers=bearer(full["key"]), timeout=30).status_code,
             "adminPlaneWithoutHeader": raw_admin.request("GET", "/spend/logs", timeout=30).status_code}
    spend = {}
    if session_key:
        audit["adminSpendLogs"], before = spend_log_count(admin, session_key)
        api.request("POST", "/v1/responses", headers=bearer(full["key"]), json=responses_body(model))
        time.sleep(75)  # LiteLLM writes Spend Logs in batches.
        _, after = spend_log_count(admin, session_key)
        spend = {"rowsBefore": before, "rowsAfter": after}
        del session_key
    evidence.record("native_audit_access", audit.get("adminSpendLogs") == 200 and audit["apiSpendLogsWithKey"] == 404 and audit["adminPlaneWithoutHeader"] == 404, audit)
    evidence.record("native_spend_logs", isinstance(spend.get("rowsBefore"), int) and isinstance(spend.get("rowsAfter"), int) and spend["rowsAfter"] > spend["rowsBefore"], spend)
    return ingress, edge_output

def configuration_checks(config, evidence, ingress, edge_output):
    group = config["target"]["resourceGroup"]
    tls = {}
    for plane, host in (("api", edge_output["apiHost"]), ("admin", edge_output["adminHost"])):
        with socket.create_connection((ingress[plane]["privateIpAddress"], 443), timeout=20) as raw, \
                ssl.create_default_context().wrap_socket(raw, server_hostname=host) as connection:
            certificate = connection.getpeercert()
            tls[plane] = {"host": host, "issuer": dict(item[0] for item in certificate["issuer"]).get("organizationName"), "notAfter": certificate["notAfter"]}
    evidence.record("private_origin_tls", True, tls)

    gateway_id = edge_output["gatewayId"]
    gateway = az(config, "network", "application-gateway", "show", "--ids", gateway_id)
    policies = {policy["name"]: policy for policy in az(config, "network", "application-gateway", "waf-policy", "list", "--resource-group", group)}
    by_id = {policy["id"].lower(): policy for policy in policies.values()}
    admin_policy, api_policy = by_id[edge_output["adminWafId"].lower()], by_id[edge_output["apiWafId"].lower()]
    allowed = sorted(config["parameters"]["edge"]["adminAllowedCidrs"])
    rules = [{"name": rule["name"], "action": rule["action"], "negated": any(condition.get("negationConditon") for condition in rule["matchConditions"]),
              "values": sorted(value for condition in rule["matchConditions"] for value in condition.get("matchValues", []))}
             for rule in admin_policy.get("customRules", [])
             if any(variable["variableName"] == "RemoteAddr" for condition in rule["matchConditions"] for variable in condition["matchVariables"])]
    evidence.record("admin_source_ip_allowlist", admin_policy["policySettings"]["mode"] == "Prevention"
                    and any(rule["values"] == allowed and rule["negated"] and rule["action"] == "Block" for rule in rules),
                    {"mode": admin_policy["policySettings"]["mode"], "allowedCidrs": allowed, "rules": rules})

    settings = az(config, "monitor", "diagnostic-settings", "list", "--resource", gateway_id) or []
    workspace = config["parameters"]["edge"]["logAnalyticsWorkspaceName"]
    evidence.record("waf_diagnostics_privacy", any(item.get("workspaceId", "").lower().endswith("/" + workspace.lower()) for item in settings),
                    {"diagnosticSettings": [item["name"] for item in settings], "workspace": workspace,
                     "logScrubbing": {name: policy["policySettings"].get("logScrubbing", {}).get("state") for name, policy in policies.items()}})

    health = az(config, "network", "application-gateway", "show-backend-health", "--ids", gateway_id)
    pools = {pool["backendAddressPool"]["id"].rsplit("/", 1)[-1]: [server["health"] for setting in pool.get("backendHttpSettingsCollection", []) for server in setting.get("servers", [])]
             for pool in health["backendAddressPools"]}
    evidence.record("app_gateway_backend_health", pools.get("api") and all(state == "Healthy" for state in pools["api"])
                    and pools.get("admin") and all(state == "Healthy" for state in pools["admin"]), pools)

    maps = [{"default": item["defaultBackendAddressPool"]["id"].rsplit("/", 1)[-1], "paths": sorted(path for rule in item["pathRules"] for path in rule["paths"]),
             "targets": sorted({rule["backendAddressPool"]["id"].rsplit("/", 1)[-1] for rule in item["pathRules"]})} for item in gateway.get("urlPathMaps", [])]
    isolated = len(maps) == 1 and maps[0]["targets"] == ["api"] and maps[0]["default"] == "deny" and not any(path.startswith(ADMIN_ONLY_PREFIXES) for path in maps[0]["paths"])
    evidence.record("admin_listener_isolation", isolated, {"pathMaps": maps})

    evidence.record("waf_detection_review", api_policy["policySettings"]["mode"] == "Detection",
                    {"mode": api_policy["policySettings"]["mode"], "managedRules": [f'{item["ruleSetType"]} {item["ruleSetVersion"]}' for item in api_policy["managedRules"]["managedRuleSets"]],
                     "customRules": [f'{rule["priority"]} {rule["name"]} {rule["action"]}' for rule in api_policy.get("customRules", [])]})

    backup = az(config, "postgres", "flexible-server", "list", "--resource-group", group, "--query", "[0].backup.backupRetentionDays")
    configured = config["contentAudit"]["retentionDays"]
    evidence.record("native_retention_recovery", isinstance(backup, int) and backup >= 7,
                    {"spendLogsRetentionDays": configured, "cleanupInterval": "1d", "postgresBackupRetentionDays": backup})

def decision_checks(evidence, decisions):
    for name in DECISIONS:
        record = decisions.get(name)
        if record is None:
            continue
        require(isinstance(record, dict) and record.get("approvedBy") and record.get("summary"), f"Decision {name} needs approvedBy and summary")
        evidence.record(name, True, {"decision": record})

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--pilot-keys", type=Path, required=True,
                        help='JSON {"full": {"key", "model"}, "limited": {"key", "allowedModel", "deniedModel"}}; kept 0600, never echoed')
    parser.add_argument("--decisions", type=Path, help="JSON of operator decision records for " + ", ".join(DECISIONS))
    parser.add_argument("--output", type=Path, default=Path("temp/customer-private/evidence"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    require(config.get("application", {}).get("authentication", {}).get("mode") == "native", "Release evidence supports native authentication")
    output = (args.output if args.output.is_absolute() else ROOT / args.output).resolve()
    require(output.is_relative_to((ROOT / "temp").resolve()), "Evidence must stay under ignored temp/")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    keys = json.loads(args.pilot_keys.read_text(encoding="utf-8"))
    evidence = Evidence(output)
    ingress, edge_output = live_checks(config, evidence, keys)
    configuration_checks(config, evidence, ingress, edge_output)
    if args.decisions:
        decision_checks(evidence, json.loads(args.decisions.read_text(encoding="utf-8")))
    failed = sorted(name for name, passed in evidence.results.items() if not passed)
    print(json.dumps({"evidenceDirectory": str(output.relative_to(ROOT)), "checks": sorted(evidence.results), "failed": failed}))
    raise SystemExit(1 if failed else 0)

if __name__ == "__main__":
    main()
