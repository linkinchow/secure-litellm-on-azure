"""Azure DNS split-plane cutover with pre-write checkpoint and conditional rollback."""

import json
import re

from scripts.customer_migration import fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands, deployment_name, group_id


def dns_settings(config):
    value = config.get("dns")
    require(isinstance(value, dict) and set(value) == {"zoneResourceId", "ttl"}, "dns requires zoneResourceId and ttl")
    match = re.fullmatch(r"/subscriptions/([a-fA-F0-9-]{36})/resourceGroups/([A-Za-z0-9_.()-]+)/providers/Microsoft.Network/dnsZones/([a-z0-9.-]+)", value["zoneResourceId"])
    require(match and match[1].lower() == config["azure"]["subscriptionId"].lower(), "DNS zone must be in the approved subscription")
    hosts = {plane: f"llm-{plane}." + config["baseDomain"] for plane in ("api", "admin")}
    require(all(host.endswith("." + match[3]) for host in hosts.values()), "DNS zone must contain both gateway hostnames; apex CNAME is not supported")
    require(type(value["ttl"]) is int and 60 <= value["ttl"] <= 3600, "DNS TTL must be 60 to 3600 seconds")
    return {**value, "zone": match[3], "group": match[2], "records": {plane: host[:-(len(match[3]) + 1)] for plane, host in hosts.items()}}


class DnsClient:
    def __init__(self, config, directory, azure=None):
        self.config, self.directory = config, directory
        self.azure = azure or AzureCommands(config, directory)
        require(self.azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"]) == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "DNS Azure login scope mismatch")
        self.settings = dns_settings(config)
        self.urls = {plane: "https://management.azure.com" + self.settings["zoneResourceId"] + "/CNAME/" + record + "?api-version=2018-05-01" for plane, record in self.settings["records"].items()}

    def read(self):
        settings = self.settings
        records = self.azure.scoped(["network", "dns", "record-set", "cname", "list", "--resource-group", settings["group"], "--zone-name", settings["zone"]])
        result = {}
        for plane, name in settings["records"].items():
            matches = [item for item in records if item["name"] == name]
            require(len(matches) <= 1, f"Ambiguous {plane} DNS record")
            if not matches:
                result[plane] = None
                continue
            item = matches[0]
            require(item.get("etag"), "DNS record has no concurrency token")
            record = item.get("cnameRecord", item.get("CNAMERecord"))
            require(isinstance(record, dict) and record.get("cname"), "Unexpected DNS record content")
            result[plane] = {"etag": item["etag"], "properties": {"TTL": item.get("ttl", item.get("TTL")), "CNAMERecord": record, "metadata": item.get("metadata") or {}}}
        return result

    def checkpoint(self):
        name = deployment_name(self.config, 9, "dns-checkpoint")
        records = self.azure.scoped(["deployment", "group", "list", "--resource-group", self.config["target"]["resourceGroup"], "--query", f"[?name=='{name}'].properties.outputs.dnsCheckpoint.value"])
        require(len(records) <= 1, "Ambiguous DNS checkpoint")
        return records[0] if records else None

    def save(self, checkpoint):
        path = self.directory / "dns-checkpoint.json"
        private_write(path, json.dumps({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"dnsCheckpoint": {"type": "object", "value": checkpoint}}}))
        result = self.azure.scoped(["deployment", "group", "create", "--resource-group", self.config["target"]["resourceGroup"], "--name", deployment_name(self.config, 9, "dns-checkpoint"), "--mode", "Incremental", "--template-file", str(path)])
        require(result.get("properties", {}).get("provisioningState") == "Succeeded", "DNS checkpoint could not be saved; record was not changed")

    def write(self, plane, desired, previous):
        require(plane in self.urls, "Unexpected DNS plane")
        headers = ["If-Match=" + previous["etag"]] if previous else ["If-None-Match=*"]
        arguments = ["rest", "--method", "put" if desired else "delete", "--url", self.urls[plane], "--headers", *headers]
        if desired:
            path = self.directory / f"dns-record-{plane}.json"
            private_write(path, json.dumps({"properties": desired}))
            arguments += ["--body", "@" + str(path)]
            self.azure.scoped(arguments)
        else:
            self.azure.scoped_allow_empty(arguments)


def change_dns(config, action, operation, revision, directory, approved, *, client=None, edge=None):
    require(action in {"dns-publish", "dns-rollback"} and operation in {"plan", "execute"}, "Invalid DNS operation")
    client = client or DnsClient(config, directory)
    settings = dns_settings(config)
    from scripts.app_gateway import is_app_gateway
    if is_app_gateway(config):
        if edge is None:
            from scripts.app_gateway import deployed_app_gateway, deployment_output
            edge = deployment_output(config, client.azure)
            if action == "dns-publish":
                deployed_app_gateway(config, client.azure)
        pattern = r"[a-z0-9-]+\." + re.escape(config["location"]) + r"\.cloudapp\.azure\.com"
        require(re.fullmatch(pattern, edge.get("publicIpFqdn", "")) is not None, "DNS target must be the Application Gateway public IP DNS name in the target region")
        targets = {"api": edge["publicIpFqdn"], "admin": edge["publicIpFqdn"]}
    elif edge is None:
        output = client.azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge"), "--query", "{state:properties.provisioningState,edge:properties.outputs.edge.value}"])
        require(output.get("state") == "Succeeded", "Deploy and verify the edge before DNS publication")
        edge = output["edge"]
        prefix = group_id(config).lower() + "/providers/microsoft.cdn/profiles/"
        require(all(edge.get(key, "").lower().startswith(prefix) for key in ("routeId", "adminRouteId")), "Re-deploy edge to provide both route resource IDs")
        if action == "dns-publish":
            for key in ("routeId", "adminRouteId"):
                route = client.azure.scoped(["resource", "show", "--ids", edge[key], "--api-version", "2025-04-15"])
                require(route.get("properties", {}).get("enabledState") == "Enabled" and route["properties"].get("linkToDefaultDomain") == "Disabled", "A gateway route is not enabled or exposes the default domain")
    if not is_app_gateway(config):
        endpoint_pattern = r"[a-z0-9-]+\.(?:[a-z0-9-]+\.)?azurefd\.net"
        require(re.fullmatch(endpoint_pattern, edge.get("endpointHost", "")) and re.fullmatch(endpoint_pattern, edge.get("adminEndpointHost", "")) and edge["endpointHost"].lower() != edge["adminEndpointHost"].lower(), "DNS targets must be distinct deployed Front Door endpoints")
        targets = {"api": edge["endpointHost"], "admin": edge["adminEndpointHost"]}
    require(edge.get("apiHost") == "llm-api." + config["baseDomain"] and edge.get("adminHost") == "llm-admin." + config["baseDomain"], "DNS hosts must match the deployed split-plane " + ("Application Gateway" if is_app_gateway(config) else "Front Door"))
    require(edge.get("adminAccessMode") == "SourceIpAllowlistAndNativeLogin", "Admin DNS requires the reviewed source-IP and native-login access mode")
    record_ids = {plane: settings["zoneResourceId"] + "/CNAME/" + record for plane, record in settings["records"].items()}
    scope = {"configSha256": stage_fingerprint(config, 9), "records": record_ids}
    current = client.read()
    require(isinstance(current, dict) and set(current) == {"api", "admin"}, "DNS client must return both gateway records")
    checkpoint = client.checkpoint()
    if checkpoint:
        require(checkpoint.get("scope") == scope, "DNS checkpoint belongs to another configuration; preserve it for separate recovery")
        before, after = checkpoint.get("before", {}), checkpoint.get("after", {})
        require(set(before) == set(after) == {"api", "admin"}, "DNS checkpoint does not cover both gateway records")
        require(all((current[plane]["properties"] if current[plane] else None) in (before[plane], after[plane]) for plane in ("api", "admin")), "Current DNS was modified externally; refuse automatic continuation or rollback")
    if action == "dns-publish":
        require(edge.get("apiTrafficEnabled") is True and edge.get("adminTrafficEnabled") is True, "Approve and enable both edge planes before changing DNS")
        desired = {
            plane: {"TTL": settings["ttl"], "CNAMERecord": {"cname": targets[plane]}, "metadata": {"managedBy": "llmgw-workflow"}}
            for plane in ("api", "admin")
        }
        if checkpoint:
            require(checkpoint.get("after") == desired and checkpoint.get("phase") != "rolled-back", "Existing DNS checkpoint must not be replaced")
        else:
            checkpoint = {"scope": scope, "before": {plane: current[plane]["properties"] if current[plane] else None for plane in ("api", "admin")}, "after": desired, "phase": "prepared", "revision": revision}
    else:
        require(checkpoint, "No approved DNS checkpoint exists")
        desired = checkpoint["before"]
    plan = {"stage": 9, "action": action, "revision": revision, "scope": scope, "before": current, "checkpoint": checkpoint, "desired": desired}
    summary = {"stage": 9, "action": action, "planSha256": fingerprint(plan), "applied": False, "stageAccepted": False, "dnsPropagationVerified": False, "dataRollbackPerformed": False}
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2))
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    if operation == "execute":
        require(approved == summary["planSha256"], "DNS plan changed or was not approved")
        require(client.read() == current, "DNS changed since plan")
        if action == "dns-publish" and client.checkpoint() is None:
            client.save(checkpoint)
        require(client.checkpoint() == checkpoint, "DNS checkpoint changed before update")
        for plane in ("api", "admin"):
            if (current[plane]["properties"] if current[plane] else None) != desired[plane]:
                client.write(plane, desired[plane], current[plane])
                current = client.read()
        observed = client.read()
        require(all((observed[plane]["properties"] if observed[plane] else None) == desired[plane] for plane in ("api", "admin")), "DNS write result is uncertain; replan without overwriting checkpoint")
        client.save({**checkpoint, "phase": "published" if action == "dns-publish" else "rolled-back"})
        summary["applied"] = True
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    print(json.dumps(summary))
    return summary