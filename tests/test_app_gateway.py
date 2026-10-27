import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import yaml

from scripts.app_gateway import APP_GATEWAY_HEADER, change_allowed, deployed_app_gateway, edge_type, names, require_front_door_edge, resource_ids
from scripts.customer_migration import ROOT, MigrationError, parameters_for, stage_checks, stage_fingerprint, validate_config
from scripts.migration_deploy import assert_change_scope, deploy_component, deployment_name, group_id, resolve_origin
from scripts.private_ingress import FRONT_DOOR_HEADER, edge_header_bindings, preserve_native_edge_binding, render_ingress
from scripts.stage9_release import release_checks, validate_release
from tests.test_runtime_config import native_customer

SUBNET = "10.30.5.0/24"
VAULT = "kvcert-synthetic"
EDGE_ID = "33333333-3333-4333-8333-333333333333"
FRONT_DOOR_ID = "11111111-1111-4111-8111-111111111111"
IMAGE = "registry.invalid/traefik@sha256:" + "a" * 64


def app_gateway_customer():
    config = native_customer()
    platform = config["parameters"]["platform"]
    platform.setdefault("logAnalyticsWorkspaceName", "target-logs")
    platform["stage4Network"].update(firewallSubnetPrefix="10.30.0.0/26", systemSubnetPrefix="10.30.1.0/24", userSubnetPrefix="10.30.2.0/23", ingressSubnetName="snet-ingress", ingressSubnetPrefix="10.30.4.0/24")
    config["privateIngress"] = {plane: {"tlsSecretId": f"https://{VAULT}.vault.azure.net/secrets/{plane}-tls", "allowedCidrs": ["10.50.0.0/24", SUBNET]} for plane in ("api", "admin")}
    subscription = config["azure"]["subscriptionId"]
    config["parameters"]["certificate-vault"] = {
        "vaultName": VAULT, "ingressReaderPrincipalId": "55555555-5555-4555-8555-555555555555",
        "certificateImporterPrincipalId": "66666666-6666-4666-8666-666666666666", "certificateImporterPrincipalType": "User",
        "runnerVirtualNetworkId": f"/subscriptions/{subscription}/resourceGroups/rg-runner/providers/Microsoft.Network/virtualNetworks/runner-vnet",
        "createPrivateDnsZone": True, "manageRunnerDnsLink": True,
    }
    config["parameters"]["edge"] = {"type": "applicationGateway", "adminAllowedCidrs": ["20.30.40.50/32"], "logAnalyticsWorkspaceName": "target-logs", "subnetName": "snet-appgw", "subnetPrefix": SUBNET}
    return config


def gateway_output(config, **changes):
    resource = names(config)
    prefix = group_id(config) + "/providers/"
    edge = {
        "edgeType": "applicationGateway", "provisioned": True, "trafficEnabled": False, "apiTrafficEnabled": False, "adminTrafficEnabled": False,
        "apiHost": "llm-api." + config["baseDomain"], "adminHost": "llm-admin." + config["baseDomain"],
        "gatewayId": prefix + "Microsoft.Network/applicationGateways/" + resource["gateway"],
        "publicIpId": prefix + "Microsoft.Network/publicIPAddresses/" + resource["publicIp"], "publicIpAddress": "20.1.2.3",
        "publicIpFqdn": f"llm-test-gw-abc123.{config['location']}.cloudapp.azure.com", "edgeHeader": APP_GATEWAY_HEADER, "edgeId": EDGE_ID,
        **{key + "Id": prefix + "Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies/" + resource[key] for key in ("apiWaf", "adminWaf", "gateWaf")},
        "adminAccessMode": "SourceIpAllowlistAndNativeLogin", "adminAllowedCidrs": ["20.30.40.50/32"], "adminRateLimitPerMinute": 120,
        "rateLimitPerMinute": 600, "backendAddresses": {"api": "10.30.4.10", "admin": "10.30.4.11"}, "subnetPrefix": SUBNET,
    }
    edge.update(changes)
    return edge


def live_gateway(config, edge):
    hosts = {"api": edge["apiHost"], "admin": edge["adminHost"]}
    policy = {plane: edge[("apiWafId" if plane == "api" else "adminWafId") if edge[f"{plane}TrafficEnabled"] else "gateWafId"] for plane in hosts}
    gateway = edge["gatewayId"]
    return {"id": gateway, "properties": {
        "provisioningState": "Succeeded", "operationalState": "Running", "sku": {"tier": "WAF_v2"}, "enableHttp2": False,
        "globalConfiguration": {"enableRequestBuffering": True, "enableResponseBuffering": False}, "sslPolicy": {"policyName": "AppGwSslPolicy20220101S"},
        "httpListeners": [{"name": plane, "properties": {"protocol": "Https", "hostName": hosts[plane], "requireServerNameIndication": True, "firewallPolicy": {"id": policy[plane]}}} for plane in hosts],
        "sslCertificates": [{"name": plane, "properties": {"keyVaultSecretId": f"https://{VAULT}.vault.azure.net/secrets/{plane}-tls-pfx"}} for plane in hosts],
        "backendAddressPools": [{"name": "api", "properties": {"backendAddresses": [{"ipAddress": "10.30.4.10"}]}}, {"name": "admin", "properties": {"backendAddresses": [{"ipAddress": "10.30.4.11"}]}}, {"name": "deny", "properties": {"backendAddresses": []}}],
        "rewriteRuleSets": [{"name": "edge-identity", "properties": {"rewriteRules": [{"actionSet": {"requestHeaderConfigurations": [{"headerName": APP_GATEWAY_HEADER, "headerValue": edge["edgeId"]}]}}]}}],
        "urlPathMaps": [{"name": "api-paths", "properties": {"defaultBackendAddressPool": {"id": gateway + "/backendAddressPools/deny"}, "pathRules": [{"properties": {"paths": ["/chat/completions", "/v1/chat/completions", "/responses", "/v1/responses", "/embeddings", "/v1/embeddings"]}}]}}],
    }}


def admin_policy(cidrs):
    return {"properties": {"policySettings": {"mode": "Prevention", "state": "Enabled"}, "customRules": [{"name": "BlockUnapprovedAdminSources", "priority": 5, "action": "Block", "matchConditions": [{"matchVariables": [{"variableName": "RemoteAddr"}], "operator": "IPMatch", "negationConditon": True, "matchValues": cidrs}]}]}}


class FakeAzure:
    def __init__(self, config, edge, gateway=None):
        self.config, self.edge = config, edge
        self.gateway = gateway or live_gateway(config, edge)
        self.calls = []

    def scoped(self, arguments):
        self.calls.append(arguments)
        if arguments[:3] == ["deployment", "group", "show"]:
            if arguments[arguments.index("--name") + 1].endswith("s4-private-ingress"):
                return {"state": "Succeeded", "ingress": {"configSha256": stage_fingerprint(self.config, 4), "api": {"privateIpAddress": "10.30.4.10"}, "admin": {"privateIpAddress": "10.30.4.11"}}}
            return {"state": "Succeeded", "edge": copy.deepcopy(self.edge)}
        if arguments[:2] == ["feature", "show"]:
            return "Registered"
        if arguments[:3] == ["network", "vnet", "show"]:
            return {"prefixes": ["10.30.0.0/16"], "subnets": [{"name": "snet-ingress", "prefix": "10.30.4.0/24"}, {"name": "snet-pe", "prefix": "10.30.8.0/24"}]}
        if arguments[:3] == ["network", "private-dns", "link"]:
            return [group_id(self.config) + "/providers/Microsoft.Network/virtualNetworks/target-vnet"]
        if arguments[:2] == ["resource", "show"]:
            identifier = arguments[arguments.index("--ids") + 1]
            if "/secrets/" in identifier:
                return {"contentType": "application/x-pkcs12", "enabled": True}
            if identifier.lower() == self.edge["gatewayId"].lower():
                return copy.deepcopy(self.gateway)
            if identifier == self.edge["adminWafId"]:
                return admin_policy(self.edge["adminAllowedCidrs"])
            if identifier == self.edge["gateWafId"]:
                return {"properties": {"policySettings": {"mode": "Prevention"}, "customRules": [{"name": "TrafficDisabled"}]}}
        raise AssertionError(f"Unexpected Azure call: {arguments}")


class AppGatewayConfigurationTests(unittest.TestCase):
    def test_front_door_remains_the_default_edge(self):
        config = native_customer()
        validate_config(config, "test")
        self.assertEqual(edge_type(config), "frontDoor")
        template, document = parameters_for(config, 9, "edge")
        self.assertEqual(template, ROOT / "infra/edge/main.bicep")
        self.assertNotIn("type", document["parameters"])
        self.assertEqual(deployment_name(config, 9, "edge"), "llmgw-test-s9-edge")
        self.assertIn("origin_tls_private_link", stage_checks(9, config))
        explicit = copy.deepcopy(config)
        explicit["parameters"]["edge"]["type"] = "frontDoor"
        validate_config(explicit, "test")
        self.assertEqual(parameters_for(explicit, 9, "edge")[1], document)

    def test_application_gateway_selects_its_template_with_traffic_closed(self):
        config = app_gateway_customer()
        validate_config(config, "test")
        template, document = parameters_for(config, 9, "edge")
        self.assertEqual(template, ROOT / "infra/edge-appgw/main.bicep")
        values = {key: item["value"] for key, item in document["parameters"].items()}
        self.assertTrue(values["deployEdge"])
        self.assertFalse(values["enableApiTraffic"] or values["enableAdminTraffic"])
        self.assertEqual(values["wafMode"], "Detection")
        self.assertEqual((values["certificateVaultName"], values["apiCertificateSecretName"], values["adminCertificateSecretName"]), (VAULT, "api-tls-pfx", "admin-tls-pfx"))
        self.assertEqual((values["subnetPrefix"], values["requestTimeoutSeconds"], values["availabilityZones"]), (SUBNET, 900, ["1", "2", "3"]))
        compiled = json.loads(subprocess.run(["az", "bicep", "build", "--file", str(template), "--stdout"], capture_output=True, text=True, check=True).stdout)
        declarations = compiled["parameters"]
        self.assertFalse(set(values) - set(declarations))
        self.assertFalse({name for name, item in declarations.items() if "defaultValue" not in item} - set(values))
        self.assertEqual(deployment_name(config, 9, "edge"), "llmgw-test-s9-edge-appgw")
        self.assertEqual(deployment_name(config, 9, "dns-checkpoint"), "llmgw-test-s9-dns-checkpoint-appgw")
        self.assertEqual(deployment_name(config, 9, "edge-bind"), "llmgw-test-s9-edge-bind")
        checks = stage_checks(9, config)
        self.assertIn("origin_tls_private_backend", checks)
        self.assertNotIn("origin_tls_private_link", checks)

    def test_invalid_application_gateway_configurations_are_rejected(self):
        cases = {
            "Front Door private origins": lambda config: config["parameters"]["edge"].update(privateOrigin={}),
            "origin component": lambda config: config["parameters"].update(origin={"virtualNetworkName": "target-vnet"}),
            "unknown edge type": lambda config: config["parameters"]["edge"].update(type="cdn"),
            "subnet outside ingress sources": lambda config: config["privateIngress"]["admin"].update(allowedCidrs=["10.50.0.0/24"]),
            "overlapping subnet": lambda config: config["parameters"]["edge"].update(subnetPrefix="10.30.4.0/25"),
            "too small subnet": lambda config: config["parameters"]["edge"].update(subnetPrefix="10.30.5.0/27"),
            "bad capacity": lambda config: config["parameters"]["edge"].update(minCapacity=5, maxCapacity=3),
            "bad timeout": lambda config: config["parameters"]["edge"].update(requestTimeoutSeconds=30),
            "bad zones": lambda config: config["parameters"]["edge"].update(availabilityZones=["4"]),
            "prompt exclusion type": lambda config: config["parameters"]["edge"].update(wafExcludePromptFields="yes"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                config = app_gateway_customer()
                change(config)
                with self.assertRaises(ValueError):
                    validate_config(config, "test")
                    parameters_for(config, 9, "edge")
        front_door = native_customer()
        front_door["parameters"]["edge"]["subnetPrefix"] = SUBNET
        with self.assertRaisesRegex(ValueError, "require edge.type=applicationGateway"):
            validate_config(front_door, "test")

    def test_change_scope_allows_only_the_gateway_contract(self):
        config = app_gateway_customer()
        allowed = sorted(resource_ids(config))
        assignment = group_id(config) + f"/providers/Microsoft.KeyVault/vaults/{VAULT}/secrets/api-tls-pfx/providers/Microsoft.Authorization/roleAssignments/" + EDGE_ID
        assert_change_scope(config, "edge", [{"changeType": "Create", "resourceId": item} for item in allowed] + [{"changeType": "Create", "resourceId": assignment}])
        self.assertTrue(change_allowed(config, assignment))
        for resource in (group_id(config) + "/providers/Microsoft.Network/virtualNetworks/target-vnet",
                         group_id(config) + f"/providers/Microsoft.KeyVault/vaults/{VAULT}/secrets/api-tls/providers/Microsoft.Authorization/roleAssignments/" + EDGE_ID,
                         group_id(config) + "/providers/Microsoft.Cdn/profiles/afd"):
            with self.subTest(resource=resource), self.assertRaisesRegex(MigrationError, "unapproved"):
                assert_change_scope(config, "edge", [{"changeType": "Modify", "resourceId": resource}])

    def test_origin_and_allowlist_tools_refuse_application_gateway(self):
        config = app_gateway_customer()
        with self.assertRaisesRegex(MigrationError, "applies only to edge.type=frontDoor"):
            deploy_component(config, 9, "origin", "a" * 40, "plan", [], ROOT / "temp/appgw-origin-refusal")
        with self.assertRaisesRegex(MigrationError, "not supported for applicationGateway"):
            deploy_component(config, 9, "edge", "a" * 40, "plan", [], ROOT / "temp/appgw-allowlist-refusal", release={}, allowlist_baseline=config)
        with self.assertRaisesRegex(MigrationError, "WAF security probe is not supported"):
            require_front_door_edge(config, "The WAF security probe")
        require_front_door_edge(native_customer(), "The WAF security probe")

    def test_resolution_reads_private_ingress_addresses_and_network_isolation(self):
        config = app_gateway_customer()
        azure = FakeAzure(config, gateway_output(config))
        resolved = resolve_origin(config, "edge", azure)
        self.assertEqual(resolved["parameters"]["edge"]["backendAddresses"], {"api": "10.30.4.10", "admin": "10.30.4.11"})
        self.assertTrue(resolved["parameters"]["edge"]["networkIsolation"])
        self.assertNotIn("backendAddresses", config["parameters"]["edge"])
        values = {key: item["value"] for key, item in parameters_for(resolved, 9, "edge")[1]["parameters"].items()}
        self.assertEqual((values["backendAddresses"], values["networkIsolation"]), ({"api": "10.30.4.10", "admin": "10.30.4.11"}, True))


class AppGatewayRuntimeTests(unittest.TestCase):
    def test_live_gateway_matches_outputs_and_detects_drift(self):
        config = app_gateway_customer()
        edge = gateway_output(config)
        state = deployed_app_gateway(config, FakeAzure(config, edge))
        self.assertEqual((state["edgeId"], state["edgeHeader"]), (EDGE_ID, APP_GATEWAY_HEADER))
        self.assertNotIn("apiTrafficEnabled", state)
        opened = live_gateway(config, {**edge, "apiTrafficEnabled": True})
        drifts = {
            "traffic state": opened,
            "edge identity": json.loads(json.dumps(live_gateway(config, edge)).replace(EDGE_ID, FRONT_DOOR_ID)),
            "path allowlist": json.loads(json.dumps(live_gateway(config, edge)).replace('"/v1/embeddings"', '"/v1/models"')),
            "http2": json.loads(json.dumps(live_gateway(config, edge)).replace('"enableHttp2": false', '"enableHttp2": true')),
        }
        for label, gateway in drifts.items():
            with self.subTest(label), self.assertRaises(MigrationError):
                deployed_app_gateway(config, FakeAzure(config, edge, gateway))
        widened = app_gateway_customer()
        widened["parameters"]["edge"]["adminAllowedCidrs"] = ["20.30.40.0/24"]
        with self.assertRaisesRegex(MigrationError, "allowlist"):
            deployed_app_gateway(widened, FakeAzure(config, edge))

    def test_ingress_binding_switches_between_edge_types(self):
        from scripts.edge_binding import bind_native_ingress, native_ingress_document_state
        config = app_gateway_customer()
        for plane in ("api", "admin"):
            documents = render_ingress(config, plane, IMAGE, config["privateIngress"][plane]["allowedCidrs"])
            documents = [item for item in documents if item["kind"] != "Namespace"]
            front_door_bound = copy.deepcopy(documents)
            config_map = next(item for item in front_door_bound if item["kind"] == "ConfigMap")
            dynamic = yaml.safe_load(config_map["data"]["routes.yaml"])
            from scripts.private_ingress import native_admin_rule, native_api_rule
            renderer = native_api_rule if plane == "api" else native_admin_rule
            dynamic["http"]["routers"][plane]["rule"] = renderer(f"llm-{plane}." + config["baseDomain"], FRONT_DOOR_ID)
            config_map["data"]["routes.yaml"] = yaml.safe_dump(dynamic, sort_keys=False)
            resources = {item["kind"].lower(): item for item in front_door_bound}
            for item in resources.values():
                item["metadata"]["uid"] = item["metadata"]["name"] + "-" + item["kind"]
            resources["deployment"]["spec"]["template"]["metadata"]["annotations"] = {"llmgw/front-door-id": FRONT_DOOR_ID}
            client = Mock()
            client.get.side_effect = lambda kind, name: copy.deepcopy(resources[kind])
            with self.subTest(plane=plane):
                *_documents, current, desired = bind_native_ingress(config, client, EDGE_ID, plane, APP_GATEWAY_HEADER)
                self.assertEqual(current["frontDoorHeaderBound"], FRONT_DOOR_ID)
                self.assertEqual((desired["frontDoorHeaderBound"], desired["edgeHeaderBound"]), (None, EDGE_ID))
                desired_map, desired_deployment = _documents[2], _documents[3]
                rule = yaml.safe_load(desired_map["data"]["routes.yaml"])["http"]["routers"][plane]["rule"]
                self.assertEqual(edge_header_bindings(rule), [(APP_GATEWAY_HEADER, EDGE_ID)])
                annotations = desired_deployment["spec"]["template"]["metadata"]["annotations"]
                self.assertEqual(annotations["llmgw/edge-id"], EDGE_ID)
                self.assertNotIn("llmgw/front-door-id", annotations)
                with self.assertRaisesRegex(MigrationError, "trusts another Front Door"):
                    bind_native_ingress(config, client, "44444444-4444-4444-8444-444444444444", plane, FRONT_DOOR_HEADER)
                bound = copy.deepcopy(resources)
                bound["configmap"], bound["deployment"] = desired_map, desired_deployment
                state = native_ingress_document_state(config, bound["configmap"], bound["deployment"], bound["service"], EDGE_ID, plane, APP_GATEWAY_HEADER)
                self.assertEqual(state["edgeHeaderBound"], EDGE_ID)
                with self.assertRaisesRegex(MigrationError, "another or no Front Door"):
                    native_ingress_document_state(config, bound["configmap"], bound["deployment"], bound["service"], EDGE_ID, plane, FRONT_DOOR_HEADER)
                rerendered = [item for item in render_ingress(config, plane, IMAGE, config["privateIngress"][plane]["allowedCidrs"]) if item["kind"] != "Namespace"]
                preserved = preserve_native_edge_binding(config, rerendered, bound["configmap"])
                preserved_rule = yaml.safe_load(next(item for item in preserved if item["kind"] == "ConfigMap")["data"]["routes.yaml"])["http"]["routers"][plane]["rule"]
                self.assertEqual(edge_header_bindings(preserved_rule), [(APP_GATEWAY_HEADER, EDGE_ID)])
                self.assertEqual(next(item for item in preserved if item["kind"] == "Deployment")["spec"]["template"]["metadata"]["annotations"]["llmgw/edge-id"], EDGE_ID)

    def test_release_reports_use_the_gateway_identity(self):
        report = {
            "phase": "canary", "authenticationMode": "native", "auditMode": "native", "telemetryEnabled": False,
            "environmentName": "prod", "baseDomain": "customer.test.invalid", "edgeType": "applicationGateway", "edgeId": EDGE_ID,
            "adminAllowedCidrs": ["20.30.40.50/32"], "logAnalyticsWorkspaceName": "target-logs", "rateLimitPerMinute": 600,
            "adminRateLimitPerMinute": 120, "wafMode": "Detection", "changeTicket": "CHG-1", "approvedBy": [FRONT_DOOR_ID, EDGE_ID],
        }
        required = release_checks(report)
        self.assertIn("app_gateway_backend_health", required)
        self.assertIn("admin_listener_isolation", required)
        self.assertNotIn("private_link_approval", required)
        with self.assertRaisesRegex(ValueError, "Missing passing evidence"):
            validate_release(report)
        for change, message in (({"edgeId": ""}, "Application Gateway edge ID"), ({"frontDoorId": FRONT_DOOR_ID}, "no Private Link origins or Front Door ID")):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                validate_release({**report, **change})
        from scripts.stage9_release import generate
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "not supported for applicationGateway"):
            generate({**report, "phase": "prepare", "environmentName": "test", "checks": {}}, Path(directory))

    def test_dns_points_both_planes_at_the_gateway_name(self):
        from scripts.dns_runtime import change_dns
        config = app_gateway_customer()
        config["dns"] = {"zoneResourceId": "/subscriptions/" + config["azure"]["subscriptionId"] + "/resourceGroups/dns/providers/Microsoft.Network/dnsZones/" + config["baseDomain"], "ttl": 300}
        original = {plane: {"etag": "before-" + plane, "properties": {"TTL": 300, "CNAMERecord": {"cname": f"llm-{plane}-x.azurefd.net"}, "metadata": {}}} for plane in ("api", "admin")}

        class Client:
            records = copy.deepcopy(original)
            saved = None
            def read(self): return copy.deepcopy(self.records)
            def checkpoint(self): return copy.deepcopy(self.saved)
            def save(self, value): self.saved = copy.deepcopy(value)
            def write(self, plane, desired, previous):
                self.records[plane] = {"etag": "next-" + plane, "properties": copy.deepcopy(desired)}
        client = Client()
        edge = gateway_output(config, apiTrafficEnabled=True, adminTrafficEnabled=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            plan = change_dns(config, "dns-publish", "plan", "a" * 40, path, "", client=client, edge=edge)
            change_dns(config, "dns-publish", "execute", "a" * 40, path, plan["planSha256"], client=client, edge=edge)
            self.assertEqual({plane: record["properties"]["CNAMERecord"]["cname"] for plane, record in client.records.items()}, {"api": edge["publicIpFqdn"], "admin": edge["publicIpFqdn"]})
            self.assertEqual(client.saved["before"], {plane: original[plane]["properties"] for plane in original})
            rollback = change_dns(config, "dns-rollback", "plan", "a" * 40, path, "", client=client, edge=edge)
            change_dns(config, "dns-rollback", "execute", "a" * 40, path, rollback["planSha256"], client=client, edge=edge)
            self.assertEqual({plane: record["properties"] for plane, record in client.records.items()}, {plane: original[plane]["properties"] for plane in original})
            with self.assertRaisesRegex(MigrationError, "public IP DNS name"):
                change_dns(config, "dns-publish", "plan", "a" * 40, path, "", client=client, edge={**edge, "publicIpFqdn": "gw.azurefd.net"})


class AppGatewayTemplateTests(unittest.TestCase):
    def test_compiled_template_meets_the_gateway_contract(self):
        from scripts.validate_stage9 import validate_app_gateway_template
        compiled = json.loads(subprocess.run(["az", "bicep", "build", "--file", str(ROOT / "infra/edge-appgw/main.bicep"), "--stdout"], capture_output=True, text=True, check=True).stdout)
        validate_app_gateway_template(compiled)
        broken = copy.deepcopy(compiled)
        broken["resources"]["gateway"]["properties"]["enableHttp2"] = True
        with self.assertRaises(AssertionError):
            validate_app_gateway_template(broken)


if __name__ == "__main__":
    unittest.main()
